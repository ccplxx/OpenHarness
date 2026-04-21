"""Swarm 团队基于文件的异步消息队列模块。

每条消息以独立 JSON 文件存储：
    ~/.openharness/teams/<team>/agents/<agent_id>/inbox/<timestamp>_<message_id>.json

原子写入使用 ``.tmp`` 临时文件 + ``os.rename`` 防止部分读取。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from openharness.swarm.lockfile import exclusive_file_lock


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

MessageType = Literal[
    "user_message",
    "permission_request",
    "permission_response",
    "sandbox_permission_request",
    "sandbox_permission_response",
    "shutdown",
    "idle_notification",
]


@dataclass
class MailboxMessage:
    """Swarm 智能体间交换的单条消息。"""

    id: str
    type: MessageType
    sender: str
    recipient: str
    payload: dict[str, Any]
    timestamp: float
    read: bool = False

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """将消息序列化为字典，用于 JSON 持久化。

        Returns:
            包含 id、type、sender、recipient、payload、timestamp、read 的字典。
        """
        return {
            "id": self.id,
            "type": self.type,
            "sender": self.sender,
            "recipient": self.recipient,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "read": self.read,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MailboxMessage":
        """从字典反序列化构建 MailboxMessage 实例。

        Args:
            data: 包含消息字段的字典，需包含 id、type、sender、recipient、timestamp。

        Returns:
            反序列化后的 :class:`MailboxMessage` 实例。
        """
        return cls(
            id=data["id"],
            type=data["type"],
            sender=data["sender"],
            recipient=data["recipient"],
            payload=data.get("payload", {}),
            timestamp=data["timestamp"],
            read=data.get("read", False),
        )


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def get_team_dir(team_name: str) -> Path:
    """返回团队目录路径 ``~/.openharness/teams/<team_name>/``，不存在则创建。"""
    base = Path.home() / ".openharness" / "teams" / team_name
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_agent_mailbox_dir(team_name: str, agent_id: str) -> Path:
    """返回智能体收件箱目录 ``~/.openharness/teams/<team_name>/agents/<agent_id>/inbox/``，不存在则创建。"""
    inbox = get_team_dir(team_name) / "agents" / agent_id / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    return inbox


# ---------------------------------------------------------------------------
# TeammateMailbox
# ---------------------------------------------------------------------------


class TeammateMailbox:
    """Swarm 团队中单个智能体的基于文件的邮箱。

    每条消息存储为独立的 JSON 文件，命名为 ``<timestamp>_<id>.json``，
    位于智能体的收件箱目录中。写入操作为原子的：先写入 ``.tmp`` 临时文件，
    然后重命名到位，确保读取者永远不会看到部分写入的消息。
    """

    def __init__(self, team_name: str, agent_id: str) -> None:
        """初始化邮箱实例。

        Args:
            team_name: 所属团队名称。
            agent_id: 智能体标识符。
        """
        self.team_name = team_name
        self.agent_id = agent_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_mailbox_dir(self) -> Path:
        """返回收件箱目录路径，不存在则创建。"""
        return get_agent_mailbox_dir(self.team_name, self.agent_id)

    def _lock_path(self) -> Path:
        """返回邮箱目录的写锁文件路径（``.write_lock``）。"""
        return self.get_mailbox_dir() / ".write_lock"

    async def write(self, msg: MailboxMessage) -> None:
        """原子性地将 *msg* 以 JSON 文件写入收件箱。

        文件先写入 ``<name>.tmp``，然后重命名到收件箱目录，
        确保并发读取者不会观察到部分写入。

        此方法使用线程池执行阻塞 I/O 操作，并获取排他锁防止并发写冲突。
        """
        inbox = self.get_mailbox_dir()
        filename = f"{msg.timestamp:.6f}_{msg.id}.json"
        final_path = inbox / filename
        tmp_path = inbox / f"{filename}.tmp"
        lock_path = inbox / ".write_lock"

        payload = json.dumps(msg.to_dict(), indent=2)

        def _write_atomic() -> None:
            with exclusive_file_lock(lock_path):
                tmp_path.write_text(payload, encoding="utf-8")
                os.replace(tmp_path, final_path)

        # Offload blocking I/O to thread pool
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write_atomic)

    async def read_all(self, unread_only: bool = True) -> list[MailboxMessage]:
        """返回收件箱中的消息，按时间戳排序（最早优先）。

        Args:
            unread_only: 为 True（默认）时仅返回未读消息；
                为 False 时返回所有消息（含已读）。
        """
        inbox = self.get_mailbox_dir()

        def _read_all() -> list[MailboxMessage]:
            messages: list[MailboxMessage] = []
            for path in sorted(inbox.glob("*.json")):
                # Skip lock files and temp files
                if path.name.startswith(".") or path.name.endswith(".tmp"):
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    msg = MailboxMessage.from_dict(data)
                    if not unread_only or not msg.read:
                        messages.append(msg)
                except (json.JSONDecodeError, KeyError):
                    # Skip corrupted message files rather than crashing.
                    continue
            return messages

        # Offload blocking I/O to thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _read_all)

    async def mark_read(self, message_id: str) -> None:
        """将指定 ID 的消息标记为已读（原地更新）。"""
        inbox = self.get_mailbox_dir()
        lock_path = self._lock_path()

        def _mark_read() -> bool:
            with exclusive_file_lock(lock_path):
                for path in inbox.glob("*.json"):
                    # Skip lock files and temp files
                    if path.name.startswith(".") or path.name.endswith(".tmp"):
                        continue
                    try:
                        data = json.loads(path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        continue

                    if data.get("id") == message_id:
                        data["read"] = True
                        tmp_path = path.with_suffix(".json.tmp")
                        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                        os.replace(tmp_path, path)
                        return True
                return False

        # Offload blocking I/O to thread pool
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _mark_read)

    async def clear(self) -> None:
        """移除收件箱中所有消息文件。"""
        inbox = self.get_mailbox_dir()
        lock_path = self._lock_path()

        def _clear() -> None:
            with exclusive_file_lock(lock_path):
                for path in inbox.glob("*.json"):
                    # Skip lock files
                    if path.name.startswith("."):
                        continue
                    try:
                        path.unlink()
                    except OSError:
                        pass

        # Offload blocking I/O to thread pool
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _clear)


# ---------------------------------------------------------------------------
# Factory helpers (basic)
# ---------------------------------------------------------------------------


def _make_message(
    msg_type: MessageType,
    sender: str,
    recipient: str,
    payload: dict[str, Any],
) -> MailboxMessage:
    """内部工厂函数：创建带有自动生成 ID 和当前时间戳的 MailboxMessage。

    Args:
        msg_type: 消息类型字面量。
        sender: 发送方标识符。
        recipient: 接收方标识符。
        payload: 消息载荷字典。

    Returns:
        新建的 :class:`MailboxMessage` 实例。
    """
    return MailboxMessage(
        id=str(uuid.uuid4()),
        type=msg_type,
        sender=sender,
        recipient=recipient,
        payload=payload,
        timestamp=time.time(),
    )


def create_user_message(sender: str, recipient: str, content: str) -> MailboxMessage:
    """创建纯文本用户消息。"""
    return _make_message("user_message", sender, recipient, {"content": content})


def create_shutdown_request(sender: str, recipient: str) -> MailboxMessage:
    """创建关闭请求消息。"""
    return _make_message("shutdown", sender, recipient, {})


def create_idle_notification(
    sender: str, recipient: str, summary: str
) -> MailboxMessage:
    """创建带简短摘要的空闲通知消息。"""
    return _make_message(
        "idle_notification", sender, recipient, {"summary": summary}
    )


# ---------------------------------------------------------------------------
# Permission message factory functions (matching TS teammateMailbox.ts)
# ---------------------------------------------------------------------------


def create_permission_request_message(
    sender: str,
    recipient: str,
    request_data: dict[str, Any],
) -> MailboxMessage:
    """创建从工作者到领导者的权限请求消息。

    Args:
        sender: 发送方工作者的智能体名称。
        recipient: 接收方领导者的智能体名称。
        request_data: 包含以下键的字典：request_id、agent_id、tool_name、
            tool_use_id、description、input、permission_suggestions。

    Returns:
        类型为 ``permission_request`` 的 :class:`MailboxMessage`。
    """
    payload: dict[str, Any] = {
        "type": "permission_request",
        "request_id": request_data.get("request_id", ""),
        "agent_id": request_data.get("agent_id", sender),
        "tool_name": request_data.get("tool_name", ""),
        "tool_use_id": request_data.get("tool_use_id", ""),
        "description": request_data.get("description", ""),
        "input": request_data.get("input", {}),
        "permission_suggestions": request_data.get("permission_suggestions", []),
    }
    return _make_message("permission_request", sender, recipient, payload)


def create_permission_response_message(
    sender: str,
    recipient: str,
    response_data: dict[str, Any],
) -> MailboxMessage:
    """创建从领导者到工作者的权限响应消息。

    Args:
        sender: 发送方领导者的智能体名称。
        recipient: 接收方工作者的智能体名称。
        response_data: 包含以下键的字典：request_id、subtype（'success'|'error'）、
            error（可选）、updated_input（可选）、permission_updates（可选）。

    Returns:
        类型为 ``permission_response`` 的 :class:`MailboxMessage`。
    """
    subtype = response_data.get("subtype", "success")
    if subtype == "error":
        payload: dict[str, Any] = {
            "type": "permission_response",
            "request_id": response_data.get("request_id", ""),
            "subtype": "error",
            "error": response_data.get("error", "Permission denied"),
        }
    else:
        payload = {
            "type": "permission_response",
            "request_id": response_data.get("request_id", ""),
            "subtype": "success",
            "response": {
                "updated_input": response_data.get("updated_input"),
                "permission_updates": response_data.get("permission_updates"),
            },
        }
    return _make_message("permission_response", sender, recipient, payload)


def create_sandbox_permission_request_message(
    sender: str,
    recipient: str,
    request_data: dict[str, Any],
) -> MailboxMessage:
    """创建从工作者到领导者的沙箱权限请求消息。

    Args:
        sender: 发送方工作者的智能体名称。
        recipient: 接收方领导者的智能体名称。
        request_data: 包含以下键的字典：requestId、workerId、workerName、
            workerColor（可选）、host。

    Returns:
        类型为 ``sandbox_permission_request`` 的 :class:`MailboxMessage`。
    """
    payload: dict[str, Any] = {
        "type": "sandbox_permission_request",
        "requestId": request_data.get("requestId", ""),
        "workerId": request_data.get("workerId", sender),
        "workerName": request_data.get("workerName", sender),
        "workerColor": request_data.get("workerColor"),
        "hostPattern": {"host": request_data.get("host", "")},
        "createdAt": int(time.time() * 1000),
    }
    return _make_message("sandbox_permission_request", sender, recipient, payload)


def create_sandbox_permission_response_message(
    sender: str,
    recipient: str,
    response_data: dict[str, Any],
) -> MailboxMessage:
    """创建从领导者到工作者的沙箱权限响应消息。

    Args:
        sender: 发送方领导者的智能体名称。
        recipient: 接收方工作者的智能体名称。
        response_data: 包含以下键的字典：requestId、host、allow。

    Returns:
        类型为 ``sandbox_permission_response`` 的 :class:`MailboxMessage`。
    """
    from datetime import datetime, timezone

    payload: dict[str, Any] = {
        "type": "sandbox_permission_response",
        "requestId": response_data.get("requestId", ""),
        "host": response_data.get("host", ""),
        "allow": bool(response_data.get("allow", False)),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    }
    return _make_message("sandbox_permission_response", sender, recipient, payload)


# ---------------------------------------------------------------------------
# Type-guard helpers (matching TS isPermissionRequest etc.)
# ---------------------------------------------------------------------------


def is_permission_request(msg: MailboxMessage) -> dict[str, Any] | None:
    """若 *msg* 是权限请求消息则返回其载荷字典，否则返回 None。

    同时兼容文本封装消息格式（检查 payload.text 中的 JSON 内容）。
    """
    if msg.type == "permission_request":
        return msg.payload
    # Also check text field for compatibility with text-envelope messages
    text = msg.payload.get("text", "")
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("type") == "permission_request":
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def is_permission_response(msg: MailboxMessage) -> dict[str, Any] | None:
    """若 *msg* 是权限响应消息则返回其载荷字典，否则返回 None。

    同时兼容文本封装消息格式。
    """
    if msg.type == "permission_response":
        return msg.payload
    text = msg.payload.get("text", "")
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("type") == "permission_response":
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def is_sandbox_permission_request(msg: MailboxMessage) -> dict[str, Any] | None:
    """若 *msg* 是沙箱权限请求消息则返回其载荷字典，否则返回 None。

    同时兼容文本封装消息格式。
    """
    if msg.type == "sandbox_permission_request":
        return msg.payload
    text = msg.payload.get("text", "")
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("type") == "sandbox_permission_request":
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def is_sandbox_permission_response(msg: MailboxMessage) -> dict[str, Any] | None:
    """若 *msg* 是沙箱权限响应消息则返回其载荷字典，否则返回 None。

    同时兼容文本封装消息格式。
    """
    if msg.type == "sandbox_permission_response":
        return msg.payload
    text = msg.payload.get("text", "")
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and parsed.get("type") == "sandbox_permission_response":
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return None


# ---------------------------------------------------------------------------
# Global mailbox convenience functions (matching TS writeToMailbox etc.)
# ---------------------------------------------------------------------------


async def write_to_mailbox(
    recipient_name: str,
    message: dict[str, Any],
    team_name: str | None = None,
) -> None:
    """将 TeammateMessage 格式的字典写入接收者的邮箱。

    镜像 TS ``writeToMailbox(recipientName, message, teamName)`` 函数。
    *message* 字典至少应包含 ``from`` 和 ``text`` 键（序列化后的消息内容），
    可选包含 ``timestamp``、``color`` 和 ``summary``。

    Args:
        recipient_name: 接收方智能体的名称/ID。
        message: 包含 ``from``、``text`` 及可选字段的字典。
        team_name: 可选团队名称；默认为 ``CLAUDE_CODE_TEAM_NAME`` 环境变量，
            然后为 ``"default"``。
    """
    team = team_name or os.environ.get("CLAUDE_CODE_TEAM_NAME", "default")
    text = message.get("text", "")

    # Detect message type from serialised text content so routing works
    msg_type: MessageType = "user_message"
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "type" in parsed:
            t = parsed["type"]
            if t in (
                "permission_request",
                "permission_response",
                "sandbox_permission_request",
                "sandbox_permission_response",
                "shutdown",
                "idle_notification",
            ):
                msg_type = t  # type: ignore[assignment]
    except (json.JSONDecodeError, TypeError):
        pass

    msg = MailboxMessage(
        id=str(uuid.uuid4()),
        type=msg_type,
        sender=message.get("from", "unknown"),
        recipient=recipient_name,
        payload={
            "text": text,
            "color": message.get("color"),
            "summary": message.get("summary"),
            "timestamp": message.get("timestamp"),
        },
        timestamp=time.time(),
    )
    mailbox = TeammateMailbox(team, recipient_name)
    await mailbox.write(msg)
