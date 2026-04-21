"""React 终端前端的 JSON-lines 后端宿主。

本模块实现 ReactBackendHost 类，通过结构化的 stdin/stdout JSON-lines 协议
驱动 OpenHarness 运行时，是 React TUI 前端与 Python 引擎之间的桥梁。

核心功能：
- 从 stdin 读取 FrontendRequest（用户输入、权限响应、命令选择）
- 通过 BackendEvent 向 stdout 输出结构化事件（文本增量、工具事件、状态快照等）
- 管理权限确认和问题回答的异步 Future 机制
- 支持 TodoWrite 的待办更新和计划模式变更事件
- 提供多种配置选择器（模型、提供商、权限模式、主题等）

协议格式：每行输出以 "OHJSON:" 前缀 + JSON 序列化的 BackendEvent。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from openharness.api.client import SupportsStreamingMessages
from openharness.auth.manager import AuthManager
from openharness.config.settings import CLAUDE_MODEL_ALIAS_OPTIONS, resolve_model_setting
from openharness.bridge import get_bridge_manager
from openharness.themes import list_themes
from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)
from openharness.output_styles import load_output_styles
from openharness.tasks import get_task_manager
from openharness.ui.protocol import BackendEvent, FrontendRequest, TranscriptItem
from openharness.ui.runtime import build_runtime, close_runtime, handle_line, start_runtime
from openharness.services.session_backend import SessionBackend

log = logging.getLogger(__name__)

log = logging.getLogger(__name__)

_PROTOCOL_PREFIX = "OHJSON:"
"""后端事件输出的行前缀，用于前端识别协议消息。"""


@dataclass(frozen=True)
class BackendHostConfig:
    """单次后端宿主会话的配置。

    包含模型、轮次限制、API 认证、权限模式、会话恢复数据等
    全部初始化参数。
    """

    model: str | None = None
    max_turns: int | None = None
    base_url: str | None = None
    system_prompt: str | None = None
    api_key: str | None = None
    api_format: str | None = None
    active_profile: str | None = None
    api_client: SupportsStreamingMessages | None = None
    cwd: str | None = None
    restore_messages: list[dict] | None = None
    restore_tool_metadata: dict[str, object] | None = None
    enforce_max_turns: bool = True
    permission_mode: str | None = None
    session_backend: SessionBackend | None = None
    extra_skill_dirs: tuple[str, ...] = ()
    extra_plugin_roots: tuple[str, ...] = ()


class ReactBackendHost:
    """通过结构化 stdin/stdout 协议驱动 OpenHarness 运行时的后端宿主。

    核心工作流：
    1. 构建运行时、发送 ready 事件
    2. 从 stdin 异步读取前端请求
    3. 分发请求：submit_line（用户输入）、权限/问题响应、命令选择等
    4. 将引擎流式事件转换为 BackendEvent 输出到 stdout
    5. 管理权限确认和问题回答的异步等待
    """

    def __init__(self, config: BackendHostConfig) -> None:
        self._config = config
        self._bundle = None
        self._write_lock = asyncio.Lock()
        self._request_queue: asyncio.Queue[FrontendRequest] = asyncio.Queue()
        self._permission_requests: dict[str, asyncio.Future[bool]] = {}
        self._question_requests: dict[str, asyncio.Future[str]] = {}
        self._permission_lock = asyncio.Lock()
        self._busy = False
        self._running = True
        # Track last tool input per name for rich event emission
        self._last_tool_inputs: dict[str, dict] = {}

    async def run(self) -> int:
        """运行后端宿主主循环。

        初始化运行时、发送 ready 和状态快照事件，
        然后循环读取前端请求并分发处理，直到收到 shutdown 请求
        或处理函数返回应退出信号。返回退出码 0。
        """
        self._bundle = await build_runtime(
            model=self._config.model,
            max_turns=self._config.max_turns,
            base_url=self._config.base_url,
            system_prompt=self._config.system_prompt,
            api_key=self._config.api_key,
            api_format=self._config.api_format,
            active_profile=self._config.active_profile,
            api_client=self._config.api_client,
            cwd=self._config.cwd,
            restore_messages=self._config.restore_messages,
            restore_tool_metadata=self._config.restore_tool_metadata,
            permission_prompt=self._ask_permission,
            ask_user_prompt=self._ask_question,
            enforce_max_turns=self._config.enforce_max_turns,
            permission_mode=self._config.permission_mode,
            session_backend=self._config.session_backend,
            extra_skill_dirs=self._config.extra_skill_dirs,
            extra_plugin_roots=self._config.extra_plugin_roots,
        )
        await start_runtime(self._bundle)
        await self._emit(
            BackendEvent.ready(
                self._bundle.app_state.get(),
                get_task_manager().list_tasks(),
                [f"/{command.name}" for command in self._bundle.commands.list_commands()],
            )
        )
        await self._emit(self._status_snapshot())

        reader = asyncio.create_task(self._read_requests())
        try:
            while self._running:
                request = await self._request_queue.get()
                if request.type == "shutdown":
                    await self._emit(BackendEvent(type="shutdown"))
                    break
                if request.type in ("permission_response", "question_response"):
                    continue
                if request.type == "list_sessions":
                    await self._handle_list_sessions()
                    continue
                if request.type == "select_command":
                    await self._handle_select_command(request.command or "")
                    continue
                if request.type == "apply_select_command":
                    if self._busy:
                        await self._emit(BackendEvent(type="error", message="Session is busy"))
                        continue
                    self._busy = True
                    try:
                        should_continue = await self._apply_select_command(
                            request.command or "",
                            request.value or "",
                        )
                    finally:
                        self._busy = False
                    if not should_continue:
                        await self._emit(BackendEvent(type="shutdown"))
                        break
                    continue
                if request.type != "submit_line":
                    await self._emit(BackendEvent(type="error", message=f"Unknown request type: {request.type}"))
                    continue
                if self._busy:
                    await self._emit(BackendEvent(type="error", message="Session is busy"))
                    continue
                line = (request.line or "").strip()
                if not line:
                    continue
                self._busy = True
                try:
                    should_continue = await self._process_line(line)
                finally:
                    self._busy = False
                if not should_continue:
                    await self._emit(BackendEvent(type="shutdown"))
                    break
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            if self._bundle is not None:
                await close_runtime(self._bundle)
        return 0

    async def _read_requests(self) -> None:
        """异步从 stdin 读取前端请求并放入请求队列。

        解析 JSON 行为 FrontendRequest，直接处理权限/问题响应
        （通过 Future 机制），其他请求入队供主循环处理。
        stdin EOF 时发送 shutdown 请求。
        """
        while True:
            raw = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not raw:
                await self._request_queue.put(FrontendRequest(type="shutdown"))
                return
            payload = raw.decode("utf-8").strip()
            if not payload:
                continue
            try:
                request = FrontendRequest.model_validate_json(payload)
            except Exception as exc:  # pragma: no cover - defensive protocol handling
                await self._emit(BackendEvent(type="error", message=f"Invalid request: {exc}"))
                continue
            if request.type == "permission_response" and request.request_id in self._permission_requests:
                future = self._permission_requests[request.request_id]
                if not future.done():
                    future.set_result(bool(request.allowed))
                continue
            if request.type == "question_response" and request.request_id in self._question_requests:
                future = self._question_requests[request.request_id]
                if not future.done():
                    future.set_result(request.answer or "")
                continue
            await self._request_queue.put(request)

    async def _process_line(self, line: str, *, transcript_line: str | None = None) -> bool:
        """处理一行用户输入并返回是否应继续会话。

        发送用户对话条目，委托 handle_line 处理，
        将流式事件转换为 BackendEvent 输出，
        处理待办更新、计划模式变更等特殊事件。
        """
        assert self._bundle is not None
        await self._emit(
            BackendEvent(type="transcript_item", item=TranscriptItem(role="user", text=transcript_line or line))
        )

        async def _print_system(message: str) -> None:
            await self._emit(
                BackendEvent(type="transcript_item", item=TranscriptItem(role="system", text=message))
            )

        async def _render_event(event: StreamEvent) -> None:
            if isinstance(event, AssistantTextDelta):
                await self._emit(BackendEvent(type="assistant_delta", message=event.text))
                return
            if isinstance(event, CompactProgressEvent):
                await self._emit(
                    BackendEvent(
                        type="compact_progress",
                        compact_phase=event.phase,
                        compact_trigger=event.trigger,
                        attempt=event.attempt,
                        compact_checkpoint=event.checkpoint,
                        compact_metadata=event.metadata,
                        message=event.message,
                    )
                )
                return
            if isinstance(event, AssistantTurnComplete):
                await self._emit(
                    BackendEvent(
                        type="assistant_complete",
                        message=event.message.text.strip(),
                        item=TranscriptItem(role="assistant", text=event.message.text.strip()),
                    )
                )
                await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
                return
            if isinstance(event, ToolExecutionStarted):
                self._last_tool_inputs[event.tool_name] = event.tool_input or {}
                await self._emit(
                    BackendEvent(
                        type="tool_started",
                        tool_name=event.tool_name,
                        tool_input=event.tool_input,
                        item=TranscriptItem(
                            role="tool",
                            text=f"{event.tool_name} {json.dumps(event.tool_input, ensure_ascii=True)}",
                            tool_name=event.tool_name,
                            tool_input=event.tool_input,
                        ),
                    )
                )
                return
            if isinstance(event, ToolExecutionCompleted):
                await self._emit(
                    BackendEvent(
                        type="tool_completed",
                        tool_name=event.tool_name,
                        output=event.output,
                        is_error=event.is_error,
                        item=TranscriptItem(
                            role="tool_result",
                            text=event.output,
                            tool_name=event.tool_name,
                            is_error=event.is_error,
                        ),
                    )
                )
                await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
                await self._emit(self._status_snapshot())
                # Emit todo_update when TodoWrite tool runs
                if event.tool_name in ("TodoWrite", "todo_write"):
                    tool_input = self._last_tool_inputs.get(event.tool_name, {})
                    # TodoWrite input may have 'todos' list or markdown content field
                    todos = tool_input.get("todos") or tool_input.get("content") or []
                    if isinstance(todos, list) and todos:
                        lines = []
                        for item in todos:
                            if isinstance(item, dict):
                                checked = item.get("status", "") in ("done", "completed", "x", True)
                                text = item.get("content") or item.get("text") or str(item)
                                lines.append(f"- [{'x' if checked else ' '}] {text}")
                        if lines:
                            await self._emit(BackendEvent(type="todo_update", todo_markdown="\n".join(lines)))
                    else:
                        await self._emit_todo_update_from_output(event.output)
                # Emit plan_mode_change when plan-related tools complete
                if event.tool_name in ("set_permission_mode", "plan_mode"):
                    assert self._bundle is not None
                    new_mode = self._bundle.app_state.get().permission_mode
                    await self._emit(BackendEvent(type="plan_mode_change", plan_mode=new_mode))
                return
            if isinstance(event, ErrorEvent):
                await self._emit(BackendEvent(type="error", message=event.message))
                await self._emit(
                    BackendEvent(type="transcript_item", item=TranscriptItem(role="system", text=event.message))
                )
                return
            if isinstance(event, StatusEvent):
                await self._emit(
                    BackendEvent(type="transcript_item", item=TranscriptItem(role="system", text=event.message))
                )
                return

        async def _clear_output() -> None:
            await self._emit(BackendEvent(type="clear_transcript"))

        should_continue = await handle_line(
            self._bundle,
            line,
            print_system=_print_system,
            render_event=_render_event,
            clear_output=_clear_output,
        )
        await self._emit(self._status_snapshot())
        await self._emit(BackendEvent.tasks_snapshot(get_task_manager().list_tasks()))
        await self._emit(BackendEvent(type="line_complete"))
        return should_continue

    async def _apply_select_command(self, command_name: str, value: str) -> bool:
        """应用选择器命令（如 /model、/theme 等）并返回是否应继续。"""
        command = command_name.strip().lstrip("/").lower()
        selected = value.strip()
        line = self._build_select_command_line(command, selected)
        if line is None:
            await self._emit(BackendEvent(type="error", message=f"Unknown select command: {command_name}"))
            await self._emit(BackendEvent(type="line_complete"))
            return True
        return await self._process_line(line, transcript_line=f"/{command}")

    def _build_select_command_line(self, command: str, value: str) -> str | None:
        """将选择器命令名和值构建为斜杠命令行字符串。

        如 ("model", "sonnet") → "/model sonnet"。
        未知命令返回 None。
        """
        if command == "provider":
            return f"/provider {value}"
        if command == "resume":
            return f"/resume {value}" if value else "/resume"
        if command == "permissions":
            return f"/permissions {value}"
        if command == "theme":
            return f"/theme {value}"
        if command == "output-style":
            return f"/output-style {value}"
        if command == "effort":
            return f"/effort {value}"
        if command == "passes":
            return f"/passes {value}"
        if command == "turns":
            return f"/turns {value}"
        if command == "fast":
            return f"/fast {value}"
        if command == "vim":
            return f"/vim {value}"
        if command == "voice":
            return f"/voice {value}"
        if command == "model":
            return f"/model {value}"
        return None

    def _status_snapshot(self) -> BackendEvent:
        """生成当前状态快照事件，包含应用状态、MCP 和 Bridge 会话信息。"""
        assert self._bundle is not None
        return BackendEvent.status_snapshot(
            state=self._bundle.app_state.get(),
            mcp_servers=self._bundle.mcp_manager.list_statuses(),
            bridge_sessions=get_bridge_manager().list_sessions(),
        )

    async def _emit_todo_update_from_output(self, output: str) -> None:
        """从工具输出中提取 Markdown 清单并发出 todo_update 事件。

        查找以 "- [" 开头的行作为清单条目。
        """
        # TodoWrite tools typically echo back the written content
        # We look for markdown checklist patterns in the output
        lines = output.splitlines()
        checklist_lines = [line for line in lines if line.strip().startswith("- [")]
        if checklist_lines:
            markdown = "\n".join(checklist_lines)
            await self._emit(BackendEvent(type="todo_update", todo_markdown=markdown))

    def _emit_swarm_status(self, teammates: list[dict], notifications: list[dict] | None = None) -> None:
        """同步发出 swarm_status 事件（调度为协程任务）。"""
        import asyncio
        loop = asyncio.get_event_loop()
        loop.create_task(
            self._emit(BackendEvent(type="swarm_status", swarm_teammates=teammates, swarm_notifications=notifications))
        )

    async def _handle_list_sessions(self) -> None:
        """处理 /resume 命令的会话列表请求。

        列出最近 10 个会话快照，以 select_request 事件发送给前端。
        """
        import time as _time

        assert self._bundle is not None
        sessions = self._bundle.session_backend.list_snapshots(self._bundle.cwd, limit=10)
        options = []
        for s in sessions:
            ts = _time.strftime("%m/%d %H:%M", _time.localtime(s["created_at"]))
            summary = s.get("summary", "")[:50] or "(no summary)"
            options.append({
                "value": s["session_id"],
                "label": f"{ts}  {s['message_count']}msg  {summary}",
            })
        await self._emit(
            BackendEvent(
                type="select_request",
                modal={"kind": "select", "title": "Resume Session", "command": "resume"},
                select_options=options,
            )
        )

    async def _handle_select_command(self, command_name: str) -> None:
        """处理选择器命令请求，生成对应选项列表的 select_request 事件。

        支持：provider、permissions、theme、output-style、effort、
        passes、turns、fast、vim、voice、model 等选择器。
        """
        assert self._bundle is not None
        command = command_name.strip().lstrip("/").lower()
        if command == "resume":
            await self._handle_list_sessions()
            return

        settings = self._bundle.current_settings()
        state = self._bundle.app_state.get()
        _, active_profile = settings.resolve_profile()
        current_model = settings.model

        if command == "provider":
            statuses = AuthManager(settings).get_profile_statuses()
            options = [
                {
                    "value": name,
                    "label": info["label"],
                    "description": f"{info['provider']} / {info['auth_source']}" + (" [missing auth]" if not info["configured"] else ""),
                    "active": info["active"],
                }
                for name, info in statuses.items()
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Provider Profile", "command": "provider"},
                    select_options=options,
                )
            )
            return

        if command == "permissions":
            options = [
                {
                    "value": "default",
                    "label": "Default",
                    "description": "Ask before write/execute operations",
                    "active": settings.permission.mode.value == "default",
                },
                {
                    "value": "full_auto",
                    "label": "Auto",
                    "description": "Allow all tools automatically",
                    "active": settings.permission.mode.value == "full_auto",
                },
                {
                    "value": "plan",
                    "label": "Plan Mode",
                    "description": "Block all write operations",
                    "active": settings.permission.mode.value == "plan",
                },
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Permission Mode", "command": "permissions"},
                    select_options=options,
                )
            )
            return

        if command == "theme":
            options = [
                {
                    "value": name,
                    "label": name,
                    "active": name == settings.theme,
                }
                for name in list_themes()
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Theme", "command": "theme"},
                    select_options=options,
                )
            )
            return

        if command == "output-style":
            options = [
                {
                    "value": style.name,
                    "label": style.name,
                    "description": style.source,
                    "active": style.name == settings.output_style,
                }
                for style in load_output_styles()
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Output Style", "command": "output-style"},
                    select_options=options,
                )
            )
            return

        if command == "effort":
            options = [
                {"value": "low", "label": "Low", "description": "Fastest responses", "active": settings.effort == "low"},
                {"value": "medium", "label": "Medium", "description": "Balanced reasoning", "active": settings.effort == "medium"},
                {"value": "high", "label": "High", "description": "Deepest reasoning", "active": settings.effort == "high"},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Reasoning Effort", "command": "effort"},
                    select_options=options,
                )
            )
            return

        if command == "passes":
            current = int(state.passes or settings.passes)
            options = [
                {"value": str(value), "label": f"{value} pass{'es' if value != 1 else ''}", "active": value == current}
                for value in range(1, 9)
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Reasoning Passes", "command": "passes"},
                    select_options=options,
                )
            )
            return

        if command == "turns":
            current = self._bundle.engine.max_turns
            values = {32, 64, 128, 200, 256, 512}
            if isinstance(current, int):
                values.add(current)
            options = [{"value": "unlimited", "label": "Unlimited", "description": "Do not hard-stop this session", "active": current is None}]
            options.extend(
                {"value": str(value), "label": f"{value} turns", "active": value == current}
                for value in sorted(values)
            )
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Max Turns", "command": "turns"},
                    select_options=options,
                )
            )
            return

        if command == "fast":
            current = bool(state.fast_mode)
            options = [
                {"value": "on", "label": "On", "description": "Prefer shorter, faster responses", "active": current},
                {"value": "off", "label": "Off", "description": "Use normal response mode", "active": not current},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Fast Mode", "command": "fast"},
                    select_options=options,
                )
            )
            return

        if command == "vim":
            current = bool(state.vim_enabled)
            options = [
                {"value": "on", "label": "On", "description": "Enable Vim keybindings", "active": current},
                {"value": "off", "label": "Off", "description": "Use standard keybindings", "active": not current},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Vim Mode", "command": "vim"},
                    select_options=options,
                )
            )
            return

        if command == "voice":
            current = bool(state.voice_enabled)
            options = [
                {"value": "on", "label": "On", "description": state.voice_reason or "Enable voice mode", "active": current},
                {"value": "off", "label": "Off", "description": "Disable voice mode", "active": not current},
            ]
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Voice Mode", "command": "voice"},
                    select_options=options,
                )
            )
            return

        if command == "model":
            options = self._model_select_options(current_model, active_profile.provider, active_profile.allowed_models)
            await self._emit(
                BackendEvent(
                    type="select_request",
                    modal={"kind": "select", "title": "Model", "command": "model"},
                    select_options=options,
                )
            )
            return

        await self._emit(BackendEvent(type="error", message=f"No selector available for /{command}"))

    def _model_select_options(self, current_model: str, provider: str, allowed_models: list[str] | None = None) -> list[dict[str, object]]:
        """生成模型选择器的选项列表。

        优先使用配置文件允许的模型列表，否则根据提供商类型
        （Anthropic/OpenAI/Moonshot/DashScope/Gemini/MiniMax 等）
        生成推荐模型选项。
        """
        if allowed_models:
            return [
                {
                    "value": value,
                    "label": value,
                    "description": "Allowed for this profile",
                    "active": value == current_model,
                }
                for value in allowed_models
            ]
        provider_name = provider.lower()
        if provider_name in {"anthropic", "anthropic_claude"}:
            resolved_current = resolve_model_setting(current_model, provider_name)
            return [
                {
                    "value": value,
                    "label": label,
                    "description": description,
                    "active": value == current_model
                    or resolve_model_setting(value, provider_name) == resolved_current,
                }
                for value, label, description in CLAUDE_MODEL_ALIAS_OPTIONS
            ]
        families: list[tuple[str, str]] = []
        if provider_name in {"openai-codex", "openai", "openai-compatible", "openrouter", "github_copilot"}:
            families.extend(
                [
                    ("gpt-5.4", "OpenAI flagship"),
                    ("gpt-5", "General GPT-5"),
                    ("gpt-4.1", "Stable GPT-4.1"),
                    ("o4-mini", "Fast reasoning"),
                ]
            )
        elif provider_name in {"moonshot", "moonshot-compatible"}:
            families.extend(
                [
                    ("kimi-k2.5", "Moonshot K2.5"),
                    ("kimi-k2-turbo-preview", "Faster Moonshot"),
                ]
            )
        elif provider_name == "dashscope":
            families.extend(
                [
                    ("qwen3.5-flash", "Fast Qwen"),
                    ("qwen3-max", "Strong Qwen"),
                    ("deepseek-r1", "Reasoning model"),
                ]
            )
        elif provider_name == "gemini":
            families.extend(
                [
                    ("gemini-2.5-pro", "Gemini Pro"),
                    ("gemini-2.5-flash", "Gemini Flash"),
                ]
            )
        elif provider_name == "minimax":
            families.extend(
                [
                    ("MiniMax-M2.7", "MiniMax flagship"),
                    ("MiniMax-M2.7-highspeed", "MiniMax fast"),
                ]
            )

        seen: set[str] = set()
        options: list[dict[str, object]] = []
        for value, description in [(current_model, "Current model"), *families]:
            if not value or value in seen:
                continue
            seen.add(value)
            options.append(
                {
                    "value": value,
                    "label": value,
                    "description": description,
                    "active": value == current_model,
                }
            )
        return options

    async def _ask_permission(self, tool_name: str, reason: str) -> bool:
        """通过前端模态弹窗请求用户权限确认。

        发送 modal_request 事件，等待前端返回 permission_response，
        超时 300 秒自动拒绝。
        """
        async with self._permission_lock:
            request_id = uuid4().hex
            future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            self._permission_requests[request_id] = future
            await self._emit(
                BackendEvent(
                    type="modal_request",
                    modal={
                        "kind": "permission",
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "reason": reason,
                    },
                )
            )
            try:
                return await asyncio.wait_for(future, timeout=300)
            except asyncio.TimeoutError:
                log.warning("Permission request %s timed out after 300s, denying", request_id)
                return False
            finally:
                self._permission_requests.pop(request_id, None)

    async def _ask_question(self, question: str) -> str:
        """通过前端模态弹窗向用户提问并等待回答。"""
        request_id = uuid4().hex
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._question_requests[request_id] = future
        await self._emit(
            BackendEvent(
                type="modal_request",
                modal={
                    "kind": "question",
                    "request_id": request_id,
                    "question": question,
                },
            )
        )
        try:
            return await future
        finally:
            self._question_requests.pop(request_id, None)

    async def _emit(self, event: BackendEvent) -> None:
        """将 BackendEvent 序列化为 JSON 行并写入 stdout。

        使用 "OHJSON:" 前缀 + JSON + 换行格式，通过写锁保证原子性。
        """
        log.debug("emit event: type=%s tool=%s", event.type, getattr(event, "tool_name", None))
        async with self._write_lock:
            payload = _PROTOCOL_PREFIX + event.model_dump_json() + "\n"
            buffer = getattr(sys.stdout, "buffer", None)
            if buffer is not None:
                buffer.write(payload.encode("utf-8"))
                buffer.flush()
                return
            sys.stdout.write(payload)
            sys.stdout.flush()


async def run_backend_host(
    *,
    model: str | None = None,
    max_turns: int | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    active_profile: str | None = None,
    cwd: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    restore_messages: list[dict] | None = None,
    restore_tool_metadata: dict[str, object] | None = None,
    enforce_max_turns: bool = True,
    permission_mode: str | None = None,
    session_backend: SessionBackend | None = None,
    extra_skill_dirs: tuple[str | Path, ...] = (),
    extra_plugin_roots: tuple[str | Path, ...] = (),
) -> int:
    """运行结构化 React 后端宿主进程。

    构建配置、创建 ReactBackendHost 实例并运行主循环。
    若指定 cwd 则先切换工作目录。返回进程退出码。
    """
    if cwd:
        os.chdir(cwd)
    host = ReactBackendHost(
        BackendHostConfig(
            model=model,
            max_turns=max_turns,
            base_url=base_url,
            system_prompt=system_prompt,
            api_key=api_key,
            api_format=api_format,
            active_profile=active_profile,
            api_client=api_client,
            cwd=cwd,
            restore_messages=restore_messages,
            restore_tool_metadata=restore_tool_metadata,
            enforce_max_turns=enforce_max_turns,
            permission_mode=permission_mode,
            session_backend=session_backend,
            extra_skill_dirs=tuple(str(Path(path).expanduser().resolve()) for path in extra_skill_dirs),
            extra_plugin_roots=tuple(str(Path(path).expanduser().resolve()) for path in extra_plugin_roots),
        )
    )
    return await host.run()


__all__ = ["run_backend_host", "ReactBackendHost", "BackendHostConfig"]
