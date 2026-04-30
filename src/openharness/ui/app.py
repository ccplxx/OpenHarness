"""交互式会话入口点。

本模块提供 OpenHarness 的三种运行模式入口：

- run_repl：默认交互式应用（React TUI 或纯后端宿主模式）
- run_print_mode：非交互式打印模式（提交提示→流式输出→退出）
- run_task_worker：stdin 驱动的无头 worker（后台代理任务进程）

此外还包含协调者模式下异步代理任务的通知处理逻辑：
- 解码任务 worker 的 stdin 输入
- 等待并格式化异步代理完成通知
- 将通知作为后续用户消息提交给引擎
"""

from __future__ import annotations

import asyncio
import json
import sys

from openharness.coordinator.coordinator_mode import (
    TaskNotification,
    format_task_notification,
    is_coordinator_mode,
)
from openharness.engine.query import MaxTurnsExceeded
from openharness.prompts.context import build_runtime_system_prompt
from openharness.tasks.manager import get_task_manager

from openharness.api.client import SupportsStreamingMessages
from openharness.engine.stream_events import StreamEvent
from openharness.ui.backend_host import run_backend_host
from openharness.ui.react_launcher import launch_react_tui
from openharness.ui.runtime import (
    RuntimeBundle,
    build_runtime, 
    close_runtime, 
    handle_line, 
    start_runtime, 
)


_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "killed"})
"""异步代理任务的终态状态集合，到达这些状态表示任务已结束。"""


def _decode_task_worker_line(raw: str) -> str:
    """为无头任务 worker 规范化一行 stdin 输入。

    任务管理器驱动的代理 worker 可能接收：
    - 纯文本行（初始提示或简单后续）
    - 来自 send_message / teammate 后端的 JSON 对象（含 text 字段）
    """
    stripped = raw.strip()
    if not stripped:
        return ""
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return stripped
    if isinstance(payload, dict):
        text = payload.get("text")
        if isinstance(text, str):
            return text.strip()
    return stripped


def _async_agent_task_entries(tool_metadata: dict[str, object] | None) -> list[dict[str, object]]:
    """从工具元数据中提取异步代理任务条目列表。"""
    if not isinstance(tool_metadata, dict):
        return []
    value = tool_metadata.get("async_agent_tasks")
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _pending_async_agent_entries(tool_metadata: dict[str, object] | None) -> list[dict[str, object]]:
    """筛选尚未发送通知的异步代理任务条目。

    过滤条件：有 task_id 且 notification_sent 不为 True。
    """
    pending: list[dict[str, object]] = []
    for entry in _async_agent_task_entries(tool_metadata):
        task_id = str(entry.get("task_id") or "").strip()
        if not task_id:
            continue
        if bool(entry.get("notification_sent")):
            continue
        pending.append(entry)
    return pending


def _build_async_task_summary(entry: dict[str, object], *, task_status: str, return_code: int | None) -> str:
    """根据任务条目、状态和退出码构建异步代理任务的摘要文本。"""
    description = str(entry.get("description") or entry.get("agent_id") or "background task").strip()
    if task_status == "completed":
        return f'Agent "{description}" completed'
    if task_status == "killed":
        return f'Agent "{description}" was stopped'
    if return_code is not None:
        return f'Agent "{description}" failed with exit code {return_code}'
    return f'Agent "{description}" failed'


async def _wait_for_completed_async_agent_entries(
    tool_metadata: dict[str, object] | None,
    *,
    poll_interval_seconds: float = 0.1,
) -> list[dict[str, object]]:
    """轮询等待异步代理任务到达终态。

    持续检查所有未通知的任务条目，直到至少有一个到达终态
    （completed/failed/killed）或全部标记为 missing。
    返回已完成任务的条目列表。
    """
    manager = get_task_manager()
    while True:
        pending = _pending_async_agent_entries(tool_metadata)
        if not pending:
            return []
        completed: list[dict[str, object]] = []
        for entry in pending:
            task_id = str(entry.get("task_id") or "").strip()
            task = manager.get_task(task_id)
            if task is None:
                entry["notification_sent"] = True
                entry["status"] = "missing"
                continue
            entry["status"] = task.status
            if task.status in _TERMINAL_TASK_STATUSES:
                entry["return_code"] = task.return_code
                completed.append(entry)
        if completed:
            return completed
        await asyncio.sleep(poll_interval_seconds)


def _format_completed_task_notifications(completed: list[dict[str, object]]) -> str:
    """将已完成任务的条目格式化为 XML 通知消息。

    使用 format_task_notification 为每个完成任务生成 <task-notification> XML，
    读取任务输出（最多 8000 字节），标记已通知状态。
    多个通知之间用空行分隔。
    """
    manager = get_task_manager()
    notifications: list[str] = []
    for entry in completed:
        task_id = str(entry.get("task_id") or "").strip()
        agent_id = str(entry.get("agent_id") or task_id).strip()
        task = manager.get_task(task_id)
        if task is None:
            continue
        output = manager.read_task_output(task_id, max_bytes=8000).strip()
        notifications.append(
            format_task_notification(
                TaskNotification(
                    task_id=agent_id,
                    status=task.status,
                    summary=_build_async_task_summary(
                        entry,
                        task_status=task.status,
                        return_code=task.return_code,
                    ),
                    result=output or None,
                )
            )
        )
        entry["notification_sent"] = True
        entry["notified_status"] = task.status
    return "\n\n".join(notifications)


async def _submit_print_follow_up(
    bundle: RuntimeBundle,
    message: str,
    *,
    prompt_seed: str,
    print_system,
    render_event,
) -> None:
    """将后续消息提交给引擎并渲染结果（用于打印模式）。

    重新构建系统提示词，提交消息，处理 MaxTurnsExceeded，
    保存会话快照。
    """
    from openharness.ui.runtime import _format_pending_tool_results

    settings = bundle.current_settings()
    if bundle.enforce_max_turns:
        bundle.engine.set_max_turns(settings.max_turns)
    system_prompt = build_runtime_system_prompt(
        settings,
        cwd=bundle.cwd,
        latest_user_prompt=prompt_seed,
        extra_skill_dirs=bundle.extra_skill_dirs,
        extra_plugin_roots=bundle.extra_plugin_roots,
    )
    bundle.engine.set_system_prompt(system_prompt)
    try:
        async for event in bundle.engine.submit_message(message):
            await render_event(event)
    except MaxTurnsExceeded as exc:
        await print_system(f"Stopped after {exc.max_turns} turns (max_turns).")
        pending = _format_pending_tool_results(bundle.engine.messages)
        if pending:
            await print_system(pending)
    bundle.session_backend.save_snapshot(
        cwd=bundle.cwd,
        model=settings.model,
        system_prompt=system_prompt,
        messages=bundle.engine.messages,
        usage=bundle.engine.total_usage,
        session_id=bundle.session_id,
        tool_metadata=bundle.engine.tool_metadata,
    )


async def _drain_coordinator_async_agents(
    bundle: RuntimeBundle,
    *,
    prompt_seed: str,
    output_format: str,
    print_system,
    render_event,
) -> None:
    """排空协调者模式下的异步代理任务通知。

    循环等待未通知的异步代理任务完成，将完成通知作为后续消息
    提交给引擎处理，直到所有任务都已通知或无需等待。
    """
    engine = getattr(bundle, "engine", None)
    if engine is None:
        return
    while True:
        pending = _pending_async_agent_entries(getattr(engine, "tool_metadata", None))
        if not pending:
            return
        if output_format == "text":
            await print_system(
                f"Waiting for {len(pending)} background agent task(s) to finish..."
            )
        completed = await _wait_for_completed_async_agent_entries(getattr(engine, "tool_metadata", None))
        notification_payload = _format_completed_task_notifications(completed)
        if not notification_payload.strip():
            return
        await _submit_print_follow_up(
            bundle,
            notification_payload,
            prompt_seed=prompt_seed,
            print_system=print_system,
            render_event=render_event,
        )


async def run_repl(
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    backend_only: bool = False,
    restore_messages: list[dict] | None = None,
    restore_tool_metadata: dict[str, object] | None = None,
    permission_mode: str | None = None,
) -> None:
    """运行默认的 OpenHarness 交互式应用（React TUI）。

    当 backend_only=True 时，仅启动后端宿主进程（供 React 前端连接）；
    否则启动完整的 React TUI 前端（Ink 终端 UI），前端自动生成后端进程。
    """
    if backend_only:
        await run_backend_host(
            cwd=cwd,
            model=model,
            max_turns=max_turns,
            base_url=base_url,
            system_prompt=system_prompt,
            api_key=api_key,
            api_format=api_format,
            api_client=api_client,
            restore_messages=restore_messages,
            restore_tool_metadata=restore_tool_metadata,
            enforce_max_turns=max_turns is not None,
            permission_mode=permission_mode,
        )
        return

    exit_code = await launch_react_tui(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
        permission_mode=permission_mode,
    )
    if exit_code != 0:
        raise SystemExit(exit_code)


async def run_task_worker(
    *,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    permission_mode: str | None = None,
) -> None:
    """运行 stdin 驱动的无头 worker，用于后台代理任务。

    此模式专为子进程 teammate 和其他任务管理器驱动的代理进程设计，
    故意避开 React TUI / Ink 路径，无需控制 TTY 即可运行。
    从 stdin 读取一行输入，处理后输出结果并退出。
    """

    async def _noop_permission(_tool_name: str, _reason: str) -> bool:
        return True

    async def _noop_ask(_question: str) -> str:
        return ""

    async def _print_system(message: str) -> None:
        print(message, flush=True)

    async def _render_event(event: StreamEvent) -> None:
        from openharness.engine.stream_events import AssistantTextDelta, AssistantTurnComplete, ErrorEvent, StatusEvent

        if isinstance(event, AssistantTextDelta):
            sys.stdout.write(event.text)
            sys.stdout.flush()
        elif isinstance(event, AssistantTurnComplete):
            sys.stdout.write("\n")
            sys.stdout.flush()
        elif isinstance(event, ErrorEvent):
            print(event.message, flush=True)
        elif isinstance(event, StatusEvent) and event.message:
            print(event.message, flush=True)

    async def _clear_output() -> None:
        return None

    bundle = await build_runtime(
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
        api_client=api_client,
        permission_prompt=_noop_permission,
        ask_user_prompt=_noop_ask,
        enforce_max_turns=max_turns is not None,
        permission_mode=permission_mode,
    )
    await start_runtime(bundle)
    try:
        while True:
            raw = await asyncio.to_thread(sys.stdin.readline)
            if raw == "":
                break
            line = _decode_task_worker_line(raw)
            if not line:
                continue
            await handle_line(
                bundle,
                line,
                print_system=_print_system,
                render_event=_render_event,
                clear_output=_clear_output,
            )
            # Background agent tasks are one-shot workers. If the coordinator
            # needs to send a follow-up later, BackgroundTaskManager already
            # knows how to restart the task and write the next stdin payload.
            break
    finally:
        await close_runtime(bundle)


async def run_print_mode(
    *,
    prompt: str,
    output_format: str = "text",
    cwd: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    append_system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    api_client: SupportsStreamingMessages | None = None,
    permission_mode: str | None = None,
    max_turns: int | None = None,
) -> None:
    """非交互模式：提交提示，流式输出，退出。

    支持三种输出格式：
    - text：纯文本流式输出到 stdout
    - json：完成后输出 JSON 结果对象
    - stream-json：逐事件输出 JSON 行
    """
    from openharness.engine.stream_events import (
        AssistantTextDelta,
        AssistantTurnComplete,
        CompactProgressEvent,
        ErrorEvent,
        StatusEvent,
        ToolExecutionCompleted,
        ToolExecutionStarted,
    )

    async def _noop_permission(tool_name: str, reason: str) -> bool:
        return True

    async def _noop_ask(question: str) -> str:
        return ""

    bundle = await build_runtime(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        base_url=base_url,
        system_prompt=system_prompt,
        api_key=api_key,
        api_format=api_format,
        enforce_max_turns=True,
        api_client=api_client,
        permission_prompt=_noop_permission, # 权限确认回调类型
        ask_user_prompt=_noop_ask,  # 用户输入回调类型
    )
    await start_runtime(bundle)  # 执行session_start hook，这里没有收集hook的结果？

    collected_text = ""
    events_list: list[dict] = []

    try:
        async def _print_system(message: str) -> None:
            nonlocal collected_text
            if output_format == "text":
                print(message, file=sys.stderr)
            elif output_format == "stream-json":
                obj = {"type": "system", "message": message}
                print(json.dumps(obj), flush=True)
                events_list.append(obj)

        async def _render_event(event: StreamEvent) -> None:
            nonlocal collected_text
            if isinstance(event, AssistantTextDelta):
                collected_text += event.text
                if output_format == "text":
                    sys.stdout.write(event.text)
                    sys.stdout.flush()  # 强制将输出缓冲区中的内容立即写入终端（或文件）而不是等待缓冲区满了或程序结束才输出。
                elif output_format == "stream-json":
                    obj = {"type": "assistant_delta", "text": event.text}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, AssistantTurnComplete):
                if output_format == "text":
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                elif output_format == "stream-json":
                    obj = {"type": "assistant_complete", "text": event.message.text.strip()}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, ToolExecutionStarted):
                if output_format == "stream-json":
                    obj = {"type": "tool_started", "tool_name": event.tool_name, "tool_input": event.tool_input}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, ToolExecutionCompleted):
                if output_format == "stream-json":
                    obj = {"type": "tool_completed", "tool_name": event.tool_name, "output": event.output, "is_error": event.is_error}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, ErrorEvent):
                if output_format == "text":
                    print(event.message, file=sys.stderr)
                elif output_format == "stream-json":
                    obj = {"type": "error", "message": event.message, "recoverable": event.recoverable}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, CompactProgressEvent):
                if output_format == "text" and event.message:
                    print(event.message, file=sys.stderr)
                elif output_format == "stream-json":
                    obj = {
                        "type": "compact_progress",
                        "phase": event.phase,
                        "trigger": event.trigger,
                        "attempt": event.attempt,
                        "message": event.message,
                    }
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)
            elif isinstance(event, StatusEvent):
                if output_format == "text":
                    print(event.message, file=sys.stderr)
                elif output_format == "stream-json":
                    obj = {"type": "status", "message": event.message}
                    print(json.dumps(obj), flush=True)
                    events_list.append(obj)

        async def _clear_output() -> None:
            pass

        await handle_line(
            bundle,
            prompt,
            print_system=_print_system,
            render_event=_render_event,
            clear_output=_clear_output,
        )
        if is_coordinator_mode():
            await _drain_coordinator_async_agents(
                bundle,
                prompt_seed=prompt,
                output_format=output_format,
                print_system=_print_system,
                render_event=_render_event,
            )

        if output_format == "json":
            result = {"type": "result", "text": collected_text.strip()}
            print(json.dumps(result))
    finally:
        await close_runtime(bundle)
