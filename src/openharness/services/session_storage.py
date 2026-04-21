"""会话持久化辅助模块。

本模块提供基于本地文件系统的会话快照存储与恢复功能，包括快照的保存、加载、
列表查询、按 ID 查找以及 Markdown 导出。会话数据以 JSON 格式存储，使用原子
写入确保数据完整性，并通过 SHA1 哈希项目路径来隔离不同项目的会话目录。
"""

from __future__ import annotations

import json
import time
from hashlib import sha1
from pathlib import Path
from typing import Any
from uuid import uuid4

from openharness.api.usage import UsageSnapshot
from openharness.config.paths import get_sessions_dir
from openharness.engine.messages import ConversationMessage, sanitize_conversation_messages
from openharness.utils.fs import atomic_write_text


_PERSISTED_TOOL_METADATA_KEYS = (
    "permission_mode",
    "read_file_state",
    "invoked_skills",
    "async_agent_state",
    "async_agent_tasks",
    "recent_work_log",
    "recent_verified_work",
    "task_focus_state",
    "compact_checkpoints",
    "compact_last",
)
"""需要持久化保存的工具元数据键名列表。

仅保存与权限模式、文件读取状态、技能调用、异步代理和压缩检查点等
关键状态相关的元数据，避免存储过多临时信息。
"""


def _sanitize_metadata(value: Any) -> Any:
    """递归地将元数据值转换为 JSON 可序列化格式。

    将 Path 转为字符串，递归处理 dict/list/tuple/set，
    其他不可序列化类型一律调用 str() 转换。

    Args:
        value: 待清洗的元数据值。

    Returns:
        Any: 可安全 JSON 序列化的值。
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _sanitize_metadata(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize_metadata(item) for item in value]
    return str(value)


def _persistable_tool_metadata(tool_metadata: dict[str, object] | None) -> dict[str, Any]:
    """从工具元数据中提取可持久化的子集。

    仅保留 _PERSISTED_TOOL_METADATA_KEYS 中定义的键，并对其值进行序列化清洗。

    Args:
        tool_metadata: 原始工具元数据字典。

    Returns:
        dict[str, Any]: 清洗后可安全持久化的元数据子集。
    """
    if not isinstance(tool_metadata, dict):
        return {}
    payload: dict[str, Any] = {}
    for key in _PERSISTED_TOOL_METADATA_KEYS:
        if key in tool_metadata:
            payload[key] = _sanitize_metadata(tool_metadata[key])
    return payload


def get_project_session_dir(cwd: str | Path) -> Path:
    """返回项目对应的会话存储目录。

    使用项目路径的 SHA1 哈希前 12 位作为目录名后缀，避免路径冲突。
    目录不存在时自动创建。

    Args:
        cwd: 项目工作目录路径。

    Returns:
        Path: 会话存储目录的绝对路径。
    """
    path = Path(cwd).resolve()
    digest = sha1(str(path).encode("utf-8")).hexdigest()[:12]
    session_dir = get_sessions_dir() / f"{path.name}-{digest}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def save_session_snapshot(
    *,
    cwd: str | Path,
    model: str,
    system_prompt: str,
    messages: list[ConversationMessage],
    usage: UsageSnapshot,
    session_id: str | None = None,
    tool_metadata: dict[str, object] | None = None,
) -> Path:
    """持久化一个会话快照，同时保存为最新快照和按 ID 命名的快照文件。

    从首条用户消息中提取摘要（前 80 字符），使用原子写入确保数据完整性。
    消息在保存前会经过 sanitize_conversation_messages 清洗。

    Args:
        cwd: 项目工作目录路径。
        model: 使用的模型名称。
        system_prompt: 系统提示词。
        messages: 对话消息列表。
        usage: Token 使用量快照。
        session_id: 可选的会话 ID，未指定时自动生成。
        tool_metadata: 可选的工具元数据字典。

    Returns:
        Path: 最新快照文件的路径。
    """
    session_dir = get_project_session_dir(cwd)
    sid = session_id or uuid4().hex[:12]
    now = time.time()
    messages = sanitize_conversation_messages(messages)
    # Extract a summary from the first user message
    summary = ""
    for msg in messages:
        if msg.role == "user" and msg.text.strip():
            summary = msg.text.strip()[:80]
            break

    payload = {
        "session_id": sid,
        "cwd": str(Path(cwd).resolve()),
        "model": model,
        "system_prompt": system_prompt,
        "messages": [message.model_dump(mode="json") for message in messages],
        "usage": usage.model_dump(),
        "tool_metadata": _persistable_tool_metadata(tool_metadata),
        "created_at": now,
        "summary": summary,
        "message_count": len(messages),
    }
    data = json.dumps(payload, indent=2) + "\n"

    # Save as latest
    latest_path = session_dir / "latest.json"
    atomic_write_text(latest_path, data)

    # Save by session ID
    session_path = session_dir / f"session-{sid}.json"
    atomic_write_text(session_path, data)

    return latest_path


def _sanitize_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化持久化的快照数据，确保消息格式向前兼容。

    将存储中的原始消息通过 ConversationMessage 模型验证和清洗，
    更新 message_count 计数，保证旧格式快照也能被正确加载。

    Args:
        payload: 原始快照字典。

    Returns:
        dict[str, Any]: 规范化后的快照字典。
    """
    raw_messages = payload.get("messages", [])
    if isinstance(raw_messages, list):
        messages = sanitize_conversation_messages(
            [ConversationMessage.model_validate(item) for item in raw_messages]
        )
        payload = dict(payload)
        payload["messages"] = [message.model_dump(mode="json") for message in messages]
        payload["message_count"] = len(messages)
    return payload


def load_session_snapshot(cwd: str | Path) -> dict[str, Any] | None:
    """加载项目的最新会话快照。

    Args:
        cwd: 项目工作目录路径。

    Returns:
        dict[str, Any] | None: 快照字典，不存在则返回 None。
    """
    path = get_project_session_dir(cwd) / "latest.json"
    if not path.exists():
        return None
    return _sanitize_snapshot_payload(json.loads(path.read_text(encoding="utf-8")))


def list_session_snapshots(cwd: str | Path, limit: int = 20) -> list[dict[str, Any]]:
    """列出项目的已保存会话快照，按创建时间降序排列。

    同时扫描 session-*.json 命名文件和 latest.json，自动提取摘要信息。
    若 latest.json 对应的命名文件不存在，也会将其纳入结果。

    Args:
        cwd: 项目工作目录路径。
        limit: 最多返回的快照数量，默认 20。

    Returns:
        list[dict[str, Any]]: 快照摘要信息列表，每个元素包含 session_id、
            summary、message_count、model、created_at 字段。
    """
    session_dir = get_project_session_dir(cwd)
    sessions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Named session files
    for path in sorted(session_dir.glob("session-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            sid = data.get("session_id", path.stem.replace("session-", ""))
            seen_ids.add(sid)
            summary = data.get("summary", "")
            if not summary:
                # Extract from first user message
                for msg in data.get("messages", []):
                    if msg.get("role") == "user":
                        texts = [b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text"]
                        summary = " ".join(texts).strip()[:80]
                        if summary:
                            break
            sessions.append({
                "session_id": sid,
                "summary": summary,
                "message_count": data.get("message_count", len(data.get("messages", []))),
                "model": data.get("model", ""),
                "created_at": data.get("created_at", path.stat().st_mtime),
            })
        except (json.JSONDecodeError, OSError):
            continue
        if len(sessions) >= limit:
            break

    # Also include latest.json if it has no corresponding session file
    latest_path = session_dir / "latest.json"
    if latest_path.exists() and len(sessions) < limit:
        try:
            data = json.loads(latest_path.read_text(encoding="utf-8"))
            sid = data.get("session_id", "latest")
            if sid not in seen_ids:
                summary = data.get("summary", "")
                if not summary:
                    for msg in data.get("messages", []):
                        if msg.get("role") == "user":
                            texts = [b.get("text", "") for b in msg.get("content", []) if b.get("type") == "text"]
                            summary = " ".join(texts).strip()[:80]
                            if summary:
                                break
                sessions.append({
                    "session_id": sid,
                    "summary": summary or "(latest session)",
                    "message_count": data.get("message_count", len(data.get("messages", []))),
                    "model": data.get("model", ""),
                    "created_at": data.get("created_at", latest_path.stat().st_mtime),
                })
        except (json.JSONDecodeError, OSError):
            pass

    # Sort by created_at descending
    sessions.sort(key=lambda s: s.get("created_at", 0), reverse=True)
    return sessions[:limit]


def load_session_by_id(cwd: str | Path, session_id: str) -> dict[str, Any] | None:
    """根据会话 ID 加载指定的会话快照。

    优先查找 session-{id}.json 命名文件，若不存在则回退检查
    latest.json 中的 session_id 是否匹配。

    Args:
        cwd: 项目工作目录路径。
        session_id: 目标会话 ID。

    Returns:
        dict[str, Any] | None: 快照字典，未找到返回 None。
    """
    session_dir = get_project_session_dir(cwd)
    # Try named session first
    path = session_dir / f"session-{session_id}.json"
    if path.exists():
        return _sanitize_snapshot_payload(json.loads(path.read_text(encoding="utf-8")))
    # Fallback to latest.json if session_id matches
    latest = session_dir / "latest.json"
    if latest.exists():
        data = _sanitize_snapshot_payload(json.loads(latest.read_text(encoding="utf-8")))
        if data.get("session_id") == session_id or session_id == "latest":
            return data
    return None


def export_session_markdown(
    *,
    cwd: str | Path,
    messages: list[ConversationMessage],
) -> Path:
    """将会话记录导出为 Markdown 文件。

    将每条消息按角色分节，工具调用以 code block 形式嵌入，
    工具结果同样以 code block 呈现，输出为 transcript.md。

    Args:
        cwd: 项目工作目录路径。
        messages: 对话消息列表。

    Returns:
        Path: 导出的 Markdown 文件路径。
    """
    session_dir = get_project_session_dir(cwd)
    path = session_dir / "transcript.md"
    parts: list[str] = ["# OpenHarness Session Transcript"]
    for message in messages:
        parts.append(f"\n## {message.role.capitalize()}\n")
        text = message.text.strip()
        if text:
            parts.append(text)
        for block in message.tool_uses:
            parts.append(f"\n```tool\n{block.name} {json.dumps(block.input, ensure_ascii=True)}\n```")
        for block in message.content:
            if getattr(block, "type", "") == "tool_result":
                parts.append(f"\n```tool-result\n{block.content}\n```")
    atomic_write_text(path, "\n".join(parts).strip() + "\n")
    return path
