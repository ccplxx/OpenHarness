"""React TUI 后端的结构化协议模型。

本模块定义了 Python 后端与 React 终端前端之间的 JSON-lines 通信协议数据模型：

- FrontendRequest：前端发送给后端的请求（提交输入、权限响应、命令选择等）
- BackendEvent：后端发送给前端的事件（就绪、状态快照、文本增量、工具事件等）
- TranscriptItem：对话记录条目（用户/助手/工具/系统消息）
- TaskSnapshot：后台任务的 UI 安全表示

这些模型使用 Pydantic BaseModel 实现，确保协议消息的类型安全与自动验证。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from openharness.state.app_state import AppState
from openharness.bridge.manager import BridgeSessionRecord
from openharness.mcp.types import McpConnectionStatus
from openharness.tasks.types import TaskRecord


class FrontendRequest(BaseModel):
    """前端发送给 Python 后端的单条请求。

    支持的请求类型包括：
    - submit_line：提交用户输入行
    - permission_response：权限确认响应
    - question_response：问题回答响应
    - list_sessions：列出可恢复会话
    - select_command / apply_select_command：选择/应用配置命令
    - shutdown：关闭后端
    """

    type: Literal[
        "submit_line",
        "permission_response",
        "question_response",
        "list_sessions",
        "select_command",
        "apply_select_command",
        "shutdown",
    ]
    line: str | None = None
    command: str | None = None
    value: str | None = None
    request_id: str | None = None
    allowed: bool | None = None
    answer: str | None = None


class TranscriptItem(BaseModel):
    """前端渲染的一条对话记录条目。

    包含角色（system/user/assistant/tool/tool_result/log）、
    文本内容、可选的工具名称和输入、错误标记。
    """

    role: Literal["system", "user", "assistant", "tool", "tool_result", "log"]
    text: str
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    is_error: bool | None = None


class TaskSnapshot(BaseModel):
    """UI 安全的后台任务表示。

    包含任务 ID、类型、状态、描述和元数据，
    用于向前端传递任务列表信息。
    """

    id: str
    type: str
    status: str
    description: str
    metadata: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_record(cls, record: TaskRecord) -> "TaskSnapshot":
        """从 TaskRecord 创建 TaskSnapshot 实例。"""
        return cls(
            id=record.id,
            type=record.type,
            status=record.status,
            description=record.description,
            metadata=dict(record.metadata),
        )


class BackendEvent(BaseModel):
    """Python 后端发送给 React 前端的单条事件。

    支持的事件类型包括：ready（就绪）、state_snapshot（状态快照）、
    tasks_snapshot（任务快照）、transcript_item（对话条目）、
    compact_progress（压缩进度）、assistant_delta/assistant_complete
    （助手文本）、line_complete（行处理完成）、tool_started/tool_completed
    （工具事件）、clear_transcript（清空对话）、modal_request（模态弹窗请求）、
    select_request（选择器请求）、todo_update（待办更新）、
    plan_mode_change（计划模式变更）、swarm_status（集群状态）、
    error/shutdown（错误/关闭）。
    """

    type: Literal[
        "ready",
        "state_snapshot",
        "tasks_snapshot",
        "transcript_item",
        "compact_progress",
        "assistant_delta",
        "assistant_complete",
        "line_complete",
        "tool_started",
        "tool_completed",
        "clear_transcript",
        "modal_request",
        "select_request",
        "todo_update",
        "plan_mode_change",
        "swarm_status",
        "error",
        "shutdown",
    ]
    select_options: list[dict[str, Any]] | None = None
    message: str | None = None
    item: TranscriptItem | None = None
    state: dict[str, Any] | None = None
    tasks: list[TaskSnapshot] | None = None
    mcp_servers: list[dict[str, Any]] | None = None
    bridge_sessions: list[dict[str, Any]] | None = None
    commands: list[str] | None = None
    modal: dict[str, Any] | None = None
    tool_name: str | None = None
    tool_input: dict[str, Any] | None = None
    output: str | None = None
    is_error: bool | None = None
    compact_phase: str | None = None
    compact_trigger: str | None = None
    attempt: int | None = None
    compact_checkpoint: str | None = None
    compact_metadata: dict[str, Any] | None = None
    # New fields for enhanced events
    todo_markdown: str | None = None
    plan_mode: str | None = None
    swarm_teammates: list[dict[str, Any]] | None = None
    swarm_notifications: list[dict[str, Any]] | None = None

    @classmethod
    def ready(
        cls,
        state: AppState,
        tasks: list[TaskRecord],
        commands: list[str],
    ) -> "BackendEvent":
        """创建 ready 事件，携带初始应用状态、任务列表和可用命令。"""
        return cls(
            type="ready",
            state=_state_payload(state),
            tasks=[TaskSnapshot.from_record(task) for task in tasks],
            mcp_servers=[],
            bridge_sessions=[],
            commands=commands,
        )

    @classmethod
    def state_snapshot(cls, state: AppState) -> "BackendEvent":
        """创建 state_snapshot 事件，携带当前应用状态。"""
        return cls(type="state_snapshot", state=_state_payload(state))

    @classmethod
    def tasks_snapshot(cls, tasks: list[TaskRecord]) -> "BackendEvent":
        """创建 tasks_snapshot 事件，携带当前任务列表。"""
        return cls(
            type="tasks_snapshot",
            tasks=[TaskSnapshot.from_record(task) for task in tasks],
        )

    @classmethod
    def status_snapshot(
        cls,
        *,
        state: AppState,
        mcp_servers: list[McpConnectionStatus],
        bridge_sessions: list[BridgeSessionRecord],
    ) -> "BackendEvent":
        """创建完整的状态快照事件，包含应用状态、MCP 服务器和 Bridge 会话信息。"""
        return cls(
            type="state_snapshot",
            state=_state_payload(state),
            mcp_servers=[
                {
                    "name": server.name,
                    "state": server.state,
                    "detail": server.detail,
                    "transport": server.transport,
                    "auth_configured": server.auth_configured,
                    "tool_count": len(server.tools),
                    "resource_count": len(server.resources),
                }
                for server in mcp_servers
            ],
            bridge_sessions=[
                {
                    "session_id": session.session_id,
                    "command": session.command,
                    "cwd": session.cwd,
                    "pid": session.pid,
                    "status": session.status,
                    "started_at": session.started_at,
                    "output_path": session.output_path,
                }
                for session in bridge_sessions
            ],
        )


def _state_payload(state: AppState) -> dict[str, Any]:
    """将 AppState 转换为前端可消费的字典结构。

    包含模型、工作目录、提供商、认证状态、权限模式、主题、
    Vim/语音模式、快速模式、努力级别、MCP/Bridge 连接状态等字段。
    """
    return {
        "model": state.model,
        "cwd": state.cwd,
        "provider": state.provider,
        "auth_status": state.auth_status,
        "base_url": state.base_url,
        "permission_mode": _format_permission_mode(state.permission_mode),
        "theme": state.theme,
        "vim_enabled": state.vim_enabled,
        "voice_enabled": state.voice_enabled,
        "voice_available": state.voice_available,
        "voice_reason": state.voice_reason,
        "fast_mode": state.fast_mode,
        "effort": state.effort,
        "passes": state.passes,
        "mcp_connected": state.mcp_connected,
        "mcp_failed": state.mcp_failed,
        "bridge_sessions": state.bridge_sessions,
        "output_style": state.output_style,
        "keybindings": dict(state.keybindings),
    }


_MODE_LABELS = {
    "default": "Default",
    "plan": "Plan Mode",
    "full_auto": "Auto",
    "PermissionMode.DEFAULT": "Default",
    "PermissionMode.PLAN": "Plan Mode",
    "PermissionMode.FULL_AUTO": "Auto",
}


def _format_permission_mode(raw: str) -> str:
    """将原始权限模式字符串转换为人类可读的标签。

    如 "default" → "Default"、"plan" → "Plan Mode"、"full_auto" → "Auto"。
    未匹配的值原样返回。
    """
    return _MODE_LABELS.get(raw, raw)


__all__ = [
    "BackendEvent",
    "FrontendRequest",
    "TaskSnapshot",
    "TranscriptItem",
]
