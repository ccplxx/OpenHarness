"""会话存储后端抽象层模块。

本模块定义了会话持久化的抽象接口（SessionBackend Protocol）和默认实现
（OpenHarnessSessionBackend），遵循依赖倒置原则，使上层引擎无需关心
具体的存储细节。默认后端基于本地文件系统（~/.openharness/data/sessions）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.services import session_storage


class SessionBackend(Protocol):
    """会话持久化与恢复的抽象接口协议。

    定义了保存快照、加载快照、列出快照、按 ID 加载以及导出 Markdown 等
    会话生命周期操作的统一契约，所有后端实现均须满足此协议。
    """

    def get_session_dir(self, cwd: str | Path) -> Path:
        """返回当前项目对应的会话文件存储目录。"""

    def save_snapshot(
        self,
        *,
        cwd: str | Path,
        model: str,
        system_prompt: str,
        messages: list[ConversationMessage],
        usage: UsageSnapshot,
        session_id: str | None = None,
        tool_metadata: dict[str, object] | None = None,
    ) -> Path:
        """持久化一个会话快照并返回其文件路径。"""

    def load_latest(self, cwd: str | Path) -> dict | None:
        """加载当前项目的最新会话快照。"""

    def list_snapshots(self, cwd: str | Path, limit: int = 20) -> list[dict]:
        """列出当前项目最近的会话快照。"""

    def load_by_id(self, cwd: str | Path, session_id: str) -> dict | None:
        """根据会话 ID 加载指定的会话快照。"""

    def export_markdown(
        self,
        *,
        cwd: str | Path,
        messages: list[ConversationMessage],
    ) -> Path:
        """将当前对话记录导出为 Markdown 文件。"""


@dataclass(frozen=True)
class OpenHarnessSessionBackend:
    """默认会话后端实现，基于 ``~/.openharness/data/sessions`` 本地文件系统存储。

    所有方法均委托给 session_storage 模块中的具体函数实现。

    Attributes:
        无额外属性，所有方法均为无状态委托。
    """

    def get_session_dir(self, cwd: str | Path) -> Path:
        """返回项目对应的会话目录，委托给 session_storage.get_project_session_dir。"""
        return session_storage.get_project_session_dir(cwd)

    def save_snapshot(
        self,
        *,
        cwd: str | Path,
        model: str,
        system_prompt: str,
        messages: list[ConversationMessage],
        usage: UsageSnapshot,
        session_id: str | None = None,
        tool_metadata: dict[str, object] | None = None,
    ) -> Path:
        """保存会话快照，委托给 session_storage.save_session_snapshot。"""
        return session_storage.save_session_snapshot(
            cwd=cwd,
            model=model,
            system_prompt=system_prompt,
            messages=messages,
            usage=usage,
            session_id=session_id,
            tool_metadata=tool_metadata,
        )

    def load_latest(self, cwd: str | Path) -> dict | None:
        """加载最新会话快照，委托给 session_storage.load_session_snapshot。"""
        return session_storage.load_session_snapshot(cwd)

    def list_snapshots(self, cwd: str | Path, limit: int = 20) -> list[dict]:
        """列出会话快照，委托给 session_storage.list_session_snapshots。"""
        return session_storage.list_session_snapshots(cwd, limit=limit)

    def load_by_id(self, cwd: str | Path, session_id: str) -> dict | None:
        """按 ID 加载会话快照，委托给 session_storage.load_session_by_id。"""
        return session_storage.load_session_by_id(cwd, session_id)

    def export_markdown(
        self,
        *,
        cwd: str | Path,
        messages: list[ConversationMessage],
    ) -> Path:
        """导出 Markdown 对话记录，委托给 session_storage.export_session_markdown。"""
        return session_storage.export_session_markdown(cwd=cwd, messages=messages)


DEFAULT_SESSION_BACKEND: SessionBackend = OpenHarnessSessionBackend()
"""全局默认会话后端实例，基于本地文件系统存储。"""
