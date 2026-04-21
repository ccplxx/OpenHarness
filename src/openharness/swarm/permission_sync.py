"""Swarm 领导者-工作者权限同步协议模块。

提供基于文件（pending/resolved 目录）和基于邮箱两种权限请求/响应
协调机制，用于 Swarm 工作者与领导者之间的权限审批通信。

基于文件的流程（目录存储）：
    1. 工作者调用 ``write_permission_request()`` → pending/{id}.json
    2. 领导者调用 ``read_pending_permissions()`` 列出待处理请求
    3. 领导者调用 ``resolve_permission()`` → 移动到 resolved/{id}.json
    4. 工作者调用 ``read_resolved_permission(id)`` 或 ``poll_for_response(id)``

基于邮箱的流程：
    1. 工作者调用 ``send_permission_request_via_mailbox()``
    2. 领导者轮询邮箱，通过 ``send_permission_response_via_mailbox()`` 发送响应
    3. 工作者调用 ``poll_permission_response()`` 轮询自身邮箱

文件路径：
    ~/.openharness/teams/<teamName>/permissions/pending/<id>.json
    ~/.openharness/teams/<teamName>/permissions/resolved/<id>.json
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import string
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from openharness.swarm.lockfile import exclusive_file_lock
from openharness.swarm.mailbox import (
    MailboxMessage,
    TeammateMailbox,
    create_permission_request_message,
    create_permission_response_message,
    create_sandbox_permission_request_message,
    create_sandbox_permission_response_message,
    get_team_dir,
    write_to_mailbox,
)

if TYPE_CHECKING:
    from openharness.permissions.checker import PermissionChecker


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _get_team_name() -> str | None:
    """从环境变量 ``CLAUDE_CODE_TEAM_NAME`` 读取当前团队名称。"""
    return os.environ.get("CLAUDE_CODE_TEAM_NAME")


def _get_agent_id() -> str | None:
    """从环境变量 ``CLAUDE_CODE_AGENT_ID`` 读取当前智能体 ID。"""
    return os.environ.get("CLAUDE_CODE_AGENT_ID")


def _get_agent_name() -> str | None:
    """从环境变量 ``CLAUDE_CODE_AGENT_NAME`` 读取当前智能体名称。"""
    return os.environ.get("CLAUDE_CODE_AGENT_NAME")


def _get_teammate_color() -> str | None:
    """从环境变量 ``CLAUDE_CODE_AGENT_COLOR`` 读取当前智能体的 UI 颜色。"""
    return os.environ.get("CLAUDE_CODE_AGENT_COLOR")


# ---------------------------------------------------------------------------
# Read-only tool heuristic
# ---------------------------------------------------------------------------

_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "glob",
        "grep",
        "web_fetch",
        "web_search",
        "task_get",
        "task_list",
        "task_output",
        "cron_list",
    }
)


def _is_read_only(tool_name: str) -> bool:
    """判断工具是否为只读/安全工具。

    只读工具（如 ``read_file``、``grep``、``glob`` 等）在权限评估时
    自动批准，无需 leader 人工审核。

    Args:
        tool_name: 工具名称字符串。

    Returns:
        若工具在只读工具集合中返回 True，否则 False。
    """
    return tool_name in _READ_ONLY_TOOLS


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SwarmPermissionRequest:
    """从工作者转发给团队领导者的权限请求。

    所有字段与 TS SwarmPermissionRequestSchema 对齐。
    """

    id: str
    """该请求的唯一标识符。"""

    worker_id: str
    """请求方工作者的智能体 ID（CLAUDE_CODE_AGENT_ID）。"""

    worker_name: str
    """请求方工作者的智能体名称（CLAUDE_CODE_AGENT_NAME）。"""

    team_name: str
    """用于路由的团队名称。"""

    tool_name: str
    """需要权限的工具名称（如 'Bash'、'Edit'）。"""

    tool_use_id: str
    """工作者执行上下文中的原始工具调用 ID。"""

    description: str
    """请求操作的人类可读描述。"""

    input: dict[str, Any]
    """序列化的工具输入参数。"""

    # Optional / defaulted fields
    permission_suggestions: list[Any] = field(default_factory=list)
    """工作者本地权限系统产生的建议规则更新。"""

    worker_color: str | None = None
    """请求方工作者的分配颜色（CLAUDE_CODE_AGENT_COLOR）。"""

    status: Literal["pending", "approved", "rejected"] = "pending"
    """请求的当前状态。"""

    resolved_by: Literal["worker", "leader"] | None = None
    """谁处理了该请求。"""

    resolved_at: float | None = None
    """请求被处理时的时间戳（Unix 纪元秒）。"""

    feedback: str | None = None
    """可选的拒绝原因或领导者评论。"""

    updated_input: dict[str, Any] | None = None
    """处理者修改后的输入（如有更改）。"""

    permission_updates: list[Any] | None = None
    """处理时应用的"始终允许"规则。"""

    created_at: float = field(default_factory=time.time)
    """请求创建时的时间戳。"""

    def to_dict(self) -> dict[str, Any]:
        """将权限请求序列化为字典，用于 JSON 持久化。

        Returns:
            包含所有请求字段的字典。
        """
            "id": self.id,
            "worker_id": self.worker_id,
            "worker_name": self.worker_name,
            "team_name": self.team_name,
            "tool_name": self.tool_name,
            "tool_use_id": self.tool_use_id,
            "description": self.description,
            "input": self.input,
            "permission_suggestions": self.permission_suggestions,
            "worker_color": self.worker_color,
            "status": self.status,
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at,
            "feedback": self.feedback,
            "updated_input": self.updated_input,
            "permission_updates": self.permission_updates,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SwarmPermissionRequest":
        """从字典反序列化构建 SwarmPermissionRequest 实例。

        同时兼容 camelCase 和 snake_case 字段名（与 TS 互操作）。

        Args:
            data: 包含请求字段的字典。

        Returns:
            反序列化后的 :class:`SwarmPermissionRequest` 实例。
        """
            id=data["id"],
            worker_id=data.get("worker_id", data.get("workerId", "")),
            worker_name=data.get("worker_name", data.get("workerName", "")),
            team_name=data.get("team_name", data.get("teamName", "")),
            tool_name=data.get("tool_name", data.get("toolName", "")),
            tool_use_id=data.get("tool_use_id", data.get("toolUseId", "")),
            description=data.get("description", ""),
            input=data.get("input", {}),
            permission_suggestions=data.get(
                "permission_suggestions",
                data.get("permissionSuggestions", []),
            ),
            worker_color=data.get("worker_color", data.get("workerColor")),
            status=data.get("status", "pending"),
            resolved_by=data.get("resolved_by", data.get("resolvedBy")),
            resolved_at=data.get("resolved_at", data.get("resolvedAt")),
            feedback=data.get("feedback"),
            updated_input=data.get("updated_input", data.get("updatedInput")),
            permission_updates=data.get(
                "permission_updates", data.get("permissionUpdates")
            ),
            created_at=data.get("created_at", data.get("createdAt", time.time())),
        )


@dataclass
class PermissionResolution:
    """领导者/工作者处理权限请求时返回的解决数据。"""

    decision: Literal["approved", "rejected"]
    """决定：approved 或 rejected。"""

    resolved_by: Literal["worker", "leader"]
    """谁处理了该请求。"""

    feedback: str | None = None
    """拒绝时的可选反馈消息。"""

    updated_input: dict[str, Any] | None = None
    """处理者修改后的可选更新输入。"""

    permission_updates: list[Any] | None = None
    """要应用的权限更新（如"始终允许"规则）。"""


@dataclass
class PermissionResponse:
    """工作者轮询的遗留响应类型（向后兼容）。"""

    request_id: str
    """此响应对应的请求 ID。"""

    decision: Literal["approved", "denied"]
    """决定：approved 或 denied。"""

    timestamp: str
    """响应创建时的 ISO 时间戳。"""

    feedback: str | None = None
    """拒绝时的可选反馈消息。"""

    updated_input: dict[str, Any] | None = None
    """处理者修改后的可选更新输入。"""

    permission_updates: list[Any] | None = None
    """要应用的权限更新。"""


@dataclass
class SwarmPermissionResponse:
    """从领导者发送回请求工作者的响应。"""

    request_id: str
    """此响应对应的 :class:`SwarmPermissionRequest` 的 ID。"""

    allowed: bool
    """工具使用是否被批准。"""

    feedback: str | None = None
    """可选的拒绝原因或领导者评论。"""

    updated_rules: list[dict[str, Any]] = field(default_factory=list)
    """领导者决定应用的权限规则更新。"""


# ---------------------------------------------------------------------------
# Request ID generation
# ---------------------------------------------------------------------------


def generate_request_id() -> str:
    """生成唯一的权限请求 ID。

    格式为 ``perm-{timestamp_ms}-{random7}``，与 TS 实现对齐：
    ``perm-${Date.now()}-${Math.random().toString(36).substring(2, 9)}``
    """
    ts = int(time.time() * 1000)
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=7))
    return f"perm-{ts}-{rand}"


def generate_sandbox_request_id() -> str:
    """生成唯一的沙箱权限请求 ID。

    格式为 ``sandbox-{timestamp_ms}-{random7}``。
    """
    ts = int(time.time() * 1000)
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=7))
    return f"sandbox-{ts}-{rand}"


# ---------------------------------------------------------------------------
# Permission directory helpers
# ---------------------------------------------------------------------------


def get_permission_dir(team_name: str) -> Path:
    """返回权限目录路径 ``~/.openharness/teams/{teamName}/permissions/``。"""
    return get_team_dir(team_name) / "permissions"


def _get_pending_dir(team_name: str) -> Path:
    """返回待处理权限请求的目录路径。"""
    return get_permission_dir(team_name) / "pending"


def _get_resolved_dir(team_name: str) -> Path:
    """返回已处理权限请求的目录路径。"""
    return get_permission_dir(team_name) / "resolved"


def _ensure_permission_dirs(team_name: str) -> None:
    """确保权限目录结构（根目录、pending、resolved）存在，不存在则创建。"""
    for d in (
        get_permission_dir(team_name),
        _get_pending_dir(team_name),
        _get_resolved_dir(team_name),
    ):
        d.mkdir(parents=True, exist_ok=True)


def _pending_request_path(team_name: str, request_id: str) -> Path:
    """返回指定请求 ID 的待处理文件路径。"""
    return _get_pending_dir(team_name) / f"{request_id}.json"


def _resolved_request_path(team_name: str, request_id: str) -> Path:
    """返回指定请求 ID 的已处理文件路径。"""
    return _get_resolved_dir(team_name) / f"{request_id}.json"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_permission_request(
    tool_name: str,
    tool_use_id: str,
    tool_input: dict[str, Any],
    description: str = "",
    permission_suggestions: list[Any] | None = None,
    team_name: str | None = None,
    worker_id: str | None = None,
    worker_name: str | None = None,
    worker_color: str | None = None,
) -> SwarmPermissionRequest:
    """构建新的 :class:`SwarmPermissionRequest`（自动生成 ID）。

    缺失的工作者/团队字段从环境变量读取
    （``CLAUDE_CODE_AGENT_ID``、``CLAUDE_CODE_AGENT_NAME``、
    ``CLAUDE_CODE_TEAM_NAME``、``CLAUDE_CODE_AGENT_COLOR``）。

    Args:
        tool_name: 请求权限的工具名称。
        tool_use_id: 执行上下文中的原始工具调用 ID。
        tool_input: 工具的输入参数。
        description: 操作的可选人类可读描述。
        permission_suggestions: 建议的权限规则字典列表。
        team_name: 团队名称（回退到 ``CLAUDE_CODE_TEAM_NAME``）。
        worker_id: 工作者智能体 ID（回退到 ``CLAUDE_CODE_AGENT_ID``）。
        worker_name: 工作者智能体名称（回退到 ``CLAUDE_CODE_AGENT_NAME``）。
        worker_color: 工作者颜色（回退到 ``CLAUDE_CODE_AGENT_COLOR``）。

    Returns:
        处于 *pending* 状态的新 :class:`SwarmPermissionRequest`。

    Raises:
        ValueError: 若 team_name、worker_id 或 worker_name 无法解析。
    """
    resolved_team = team_name or _get_team_name() or ""
    resolved_id = worker_id or _get_agent_id() or ""
    resolved_name = worker_name or _get_agent_name() or ""
    resolved_color = worker_color or _get_teammate_color()

    return SwarmPermissionRequest(
        id=generate_request_id(),
        worker_id=resolved_id,
        worker_name=resolved_name,
        worker_color=resolved_color,
        team_name=resolved_team,
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        description=description,
        input=tool_input,
        permission_suggestions=permission_suggestions or [],
        status="pending",
        created_at=time.time(),
    )


# ---------------------------------------------------------------------------
# File-based storage: write / read / resolve / cleanup
# ---------------------------------------------------------------------------


def _sync_write_permission_request(
    request: SwarmPermissionRequest,
) -> SwarmPermissionRequest:
    """同步写入权限请求到 pending 目录（带文件锁和原子写入）。

    使用 ``.tmp`` 文件 + ``os.replace`` 实现原子写入，防止并发读冲突。
    通过 :func:`exclusive_file_lock` 获取排他锁防止并发写冲突。

    Args:
        request: 待写入的权限请求对象。

    Returns:
        写入后的请求对象（同一实例）。
    """
    _ensure_permission_dirs(request.team_name)
    pending_path = _pending_request_path(request.team_name, request.id)
    lock_path = _get_pending_dir(request.team_name) / ".lock"
    tmp_path = pending_path.with_suffix(".json.tmp")

    with exclusive_file_lock(lock_path):
        tmp_path.write_text(json.dumps(request.to_dict(), indent=2), encoding="utf-8")
        os.replace(tmp_path, pending_path)
    return request


async def write_permission_request(
    request: SwarmPermissionRequest,
) -> SwarmPermissionRequest:
    """将 *request* 写入 pending 目录（带文件锁）。

    工作者需要领导者审批权限时调用。

    Args:
        request: 待持久化的权限请求。

    Returns:
        写入后的请求对象（同一实例，便于链式调用）。
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_write_permission_request, request)


async def read_pending_permissions(
    team_name: str | None = None,
) -> list[SwarmPermissionRequest]:
    """读取团队的所有待处理权限请求。

    由团队领导者调用查看需要关注的请求。按创建时间从早到晚排序。

    Args:
        team_name: 团队名称（回退到 ``CLAUDE_CODE_TEAM_NAME``）。

    Returns:
        待处理的 :class:`SwarmPermissionRequest` 列表。
    """
    team = team_name or _get_team_name()
    if not team:
        return []

    pending_dir = _get_pending_dir(team)
    if not pending_dir.exists():
        return []

    requests: list[SwarmPermissionRequest] = []
    for path in sorted(pending_dir.glob("*.json")):
        if path.name == ".lock":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            requests.append(SwarmPermissionRequest.from_dict(data))
        except (json.JSONDecodeError, KeyError):
            continue

    requests.sort(key=lambda r: r.created_at)
    return requests


async def read_resolved_permission(
    request_id: str,
    team_name: str | None = None,
) -> SwarmPermissionRequest | None:
    """按 ID 读取已处理的权限请求。

    由工作者调用检查其请求是否已被处理。

    Args:
        request_id: 待查询的权限请求 ID。
        team_name: 团队名称（回退到 ``CLAUDE_CODE_TEAM_NAME``）。

    Returns:
        已处理的 :class:`SwarmPermissionRequest`，若尚未处理则返回 ``None``。
    """
    team = team_name or _get_team_name()
    if not team:
        return None

    resolved_path = _resolved_request_path(team, request_id)
    if not resolved_path.exists():
        return None

    try:
        data = json.loads(resolved_path.read_text(encoding="utf-8"))
        return SwarmPermissionRequest.from_dict(data)
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def _sync_resolve_permission(
    request_id: str,
    resolution: PermissionResolution,
    team: str,
) -> bool:
    """同步处理权限请求：从 pending 移动到 resolved 目录。

    读取 pending 中的原始请求，合并 resolution 数据后原子写入
    resolved 目录，然后删除 pending 文件。全程持有排他锁。

    Args:
        request_id: 待处理的权限请求 ID。
        resolution: 处理结果数据。
        team: 团队名称。

    Returns:
        成功处理返回 True，请求不存在或解析失败返回 False。
    """
    _ensure_permission_dirs(team)
    pending_path = _pending_request_path(team, request_id)
    resolved_path = _resolved_request_path(team, request_id)
    lock_path = _get_pending_dir(team) / ".lock"
    tmp_path = resolved_path.with_suffix(".json.tmp")

    with exclusive_file_lock(lock_path):
        if not pending_path.exists():
            return False

        try:
            data = json.loads(pending_path.read_text(encoding="utf-8"))
            request = SwarmPermissionRequest.from_dict(data)
        except (json.JSONDecodeError, KeyError):
            return False

        resolved_request = SwarmPermissionRequest(
            id=request.id,
            worker_id=request.worker_id,
            worker_name=request.worker_name,
            worker_color=request.worker_color,
            team_name=request.team_name,
            tool_name=request.tool_name,
            tool_use_id=request.tool_use_id,
            description=request.description,
            input=request.input,
            permission_suggestions=request.permission_suggestions,
            status="approved" if resolution.decision == "approved" else "rejected",
            resolved_by=resolution.resolved_by,
            resolved_at=time.time(),
            feedback=resolution.feedback,
            updated_input=resolution.updated_input,
            permission_updates=resolution.permission_updates,
            created_at=request.created_at,
        )

        tmp_path.write_text(
            json.dumps(resolved_request.to_dict(), indent=2), encoding="utf-8"
        )
        os.replace(tmp_path, resolved_path)
        try:
            pending_path.unlink()
        except OSError:
            pass

    return True


async def resolve_permission(
    request_id: str,
    resolution: PermissionResolution,
    team_name: str | None = None,
) -> bool:
    """处理权限请求，将其从 pending/ 移动到 resolved/。

    由团队领导者（或工作者自处理时）调用。

    Args:
        request_id: 待处理的权限请求 ID。
        resolution: 处理结果数据（决定、处理者等）。
        team_name: 团队名称（回退到 ``CLAUDE_CODE_TEAM_NAME``）。

    Returns:
        找到并成功处理返回 True，否则 False。
    """
    team = team_name or _get_team_name()
    if not team:
        return False
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_resolve_permission, request_id, resolution, team
    )


def _sync_cleanup_old_resolutions(team: str, max_age_seconds: float) -> int:
    """同步清理过期的已处理权限请求文件。

    遍历 resolved 目录，删除 ``resolved_at`` 或 ``created_at``
    距当前时间超过 *max_age_seconds* 的文件。解析失败的文件也会被删除。

    Args:
        team: 团队名称。
        max_age_seconds: 最大保留时间（秒）。

    Returns:
        已删除的文件数量。
    """
    resolved_dir = _get_resolved_dir(team)
    if not resolved_dir.exists():
        return 0

    now = time.time()
    cleaned = 0

    for path in resolved_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            resolved_at = data.get("resolved_at") or data.get("created_at", 0)
            if now - resolved_at >= max_age_seconds:
                path.unlink()
                cleaned += 1
        except (json.JSONDecodeError, KeyError, OSError):
            try:
                path.unlink()
                cleaned += 1
            except OSError:
                pass

    return cleaned


async def cleanup_old_resolutions(
    team_name: str | None = None,
    max_age_seconds: float = 3600.0,
) -> int:
    """清理过期的已处理权限文件。

    定期调用以防止文件积累。

    Args:
        team_name: 团队名称（回退到 ``CLAUDE_CODE_TEAM_NAME``）。
        max_age_seconds: 最大保留时间（秒），默认 1 小时。

    Returns:
        已删除的文件数量。
    """
    team = team_name or _get_team_name()
    if not team:
        return 0
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _sync_cleanup_old_resolutions, team, max_age_seconds
    )


async def delete_resolved_permission(
    request_id: str,
    team_name: str | None = None,
) -> bool:
    """工作者处理完毕后删除已处理的权限文件。

    Args:
        request_id: 权限请求 ID。
        team_name: 团队名称（回退到 ``CLAUDE_CODE_TEAM_NAME``）。

    Returns:
        找到并删除文件返回 True，否则 False。
    """
    team = team_name or _get_team_name()
    if not team:
        return False

    resolved_path = _resolved_request_path(team, request_id)
    try:
        resolved_path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Legacy / backward-compat helpers
# ---------------------------------------------------------------------------


async def poll_for_response(
    request_id: str,
    _agent_name: str | None = None,
    team_name: str | None = None,
) -> PermissionResponse | None:
    """轮询权限响应（工作者端便捷函数）。

    将已处理的请求转换为简化的遗留响应格式。

    Args:
        request_id: 待检查的权限请求 ID。
        _agent_name: 未使用；保留用于 API 兼容性。
        team_name: 团队名称（回退到 ``CLAUDE_CODE_TEAM_NAME``）。

    Returns:
        :class:`PermissionResponse`，若尚未处理则返回 ``None``。
    """
    from datetime import datetime, timezone

    resolved = await read_resolved_permission(request_id, team_name)
    if not resolved:
        return None

    ts = resolved.resolved_at or resolved.created_at
    return PermissionResponse(
        request_id=resolved.id,
        decision="approved" if resolved.status == "approved" else "denied",
        timestamp=datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3]
        + "Z",
        feedback=resolved.feedback,
        updated_input=resolved.updated_input,
        permission_updates=resolved.permission_updates,
    )


async def remove_worker_response(
    request_id: str,
    _agent_name: str | None = None,
    team_name: str | None = None,
) -> None:
    """工作者处理完毕后移除响应（delete_resolved_permission 的别名）。"""
    await delete_resolved_permission(request_id, team_name)


# Alias: submitPermissionRequest → writePermissionRequest
submit_permission_request = write_permission_request
"""向后兼容别名：submitPermissionRequest → writePermissionRequest。"""


# ---------------------------------------------------------------------------
# Team leader / worker role detection
# ---------------------------------------------------------------------------


def is_team_leader(team_name: str | None = None) -> bool:
    """判断当前智能体是否为团队领导者。

    团队领导者没有设置智能体 ID，或其 ID 为 'team-lead'。
    """
    team = team_name or _get_team_name()
    if not team:
        return False
    agent_id = _get_agent_id()
    return not agent_id or agent_id == "team-lead"


def is_swarm_worker() -> bool:
    """判断当前智能体是否为 Swarm 工作者。"""
    team_name = _get_team_name()
    agent_id = _get_agent_id()
    return bool(team_name) and bool(agent_id) and not is_team_leader()


# ---------------------------------------------------------------------------
# Leader name lookup
# ---------------------------------------------------------------------------


async def get_leader_name(team_name: str | None = None) -> str | None:
    """从团队文件获取领导者的智能体名称。

    用于将权限请求发送到领导者的邮箱。

    Args:
        team_name: 团队名称（回退到 ``CLAUDE_CODE_TEAM_NAME``）。

    Returns:
        领导者名称字符串，若团队文件缺失返回 ``None``。
        若找不到 lead 成员则回退到 ``'team-lead'``。
    """
    from openharness.swarm.team_lifecycle import read_team_file_async

    team = team_name or _get_team_name()
    if not team:
        return None

    team_file = await read_team_file_async(team)
    if not team_file:
        return None

    lead_id = team_file.lead_agent_id
    if lead_id and lead_id in team_file.members:
        return team_file.members[lead_id].name

    return "team-lead"


# ---------------------------------------------------------------------------
# Mailbox-based permission send/receive
# ---------------------------------------------------------------------------


async def send_permission_request_via_mailbox(
    request: SwarmPermissionRequest,
) -> bool:
    """通过邮箱系统将权限请求发送给领导者。

    这是基于邮箱的权限请求转发方式。
    向领导者的邮箱写入 ``permission_request`` 消息。

    Args:
        request: 待发送的权限请求。

    Returns:
        消息成功发送返回 True。
    """
    leader_name = await get_leader_name(request.team_name)
    if not leader_name:
        return False

    try:
        msg = create_permission_request_message(
            sender=request.worker_name,
            recipient=leader_name,
            request_data={
                "request_id": request.id,
                "agent_id": request.worker_name,
                "tool_name": request.tool_name,
                "tool_use_id": request.tool_use_id,
                "description": request.description,
                "input": request.input,
                "permission_suggestions": request.permission_suggestions,
            },
        )

        await write_to_mailbox(
            leader_name,
            {
                "from": request.worker_name,
                "text": json.dumps(msg.payload),
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
                ),
                "color": request.worker_color,
            },
            request.team_name,
        )
        return True
    except OSError:
        return False


async def send_permission_response_via_mailbox(
    worker_name: str,
    resolution: PermissionResolution,
    request_id: str,
    team_name: str | None = None,
) -> bool:
    """通过邮箱系统将权限响应发送给工作者。

    由领导者在批准/拒绝权限请求时调用。

    Args:
        worker_name: 接收响应的工作者名称。
        resolution: 权限处理结果。
        request_id: 原始请求 ID。
        team_name: 团队名称（回退到 ``CLAUDE_CODE_TEAM_NAME``）。

    Returns:
        消息成功发送返回 True。
    """
    team = team_name or _get_team_name()
    if not team:
        return False

    sender_name = _get_agent_name() or "team-lead"
    subtype = "success" if resolution.decision == "approved" else "error"

    try:
        msg = create_permission_response_message(
            sender=sender_name,
            recipient=worker_name,
            response_data={
                "request_id": request_id,
                "subtype": subtype,
                "error": resolution.feedback if subtype == "error" else None,
                "updated_input": resolution.updated_input,
                "permission_updates": resolution.permission_updates,
            },
        )

        await write_to_mailbox(
            worker_name,
            {
                "from": sender_name,
                "text": json.dumps(msg.payload),
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
                ),
            },
            team,
        )
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Sandbox permission mailbox helpers
# ---------------------------------------------------------------------------


async def send_sandbox_permission_request_via_mailbox(
    host: str,
    request_id: str,
    team_name: str | None = None,
) -> bool:
    """通过邮箱系统将沙箱权限请求发送给领导者。

    由工作者在沙箱运行时需要网络访问审批时调用。

    Args:
        host: 请求网络访问的主机名。
        request_id: 此请求的唯一 ID。
        team_name: 可选团队名称。

    Returns:
        消息成功发送返回 True。
    """
    team = team_name or _get_team_name()
    if not team:
        return False

    leader_name = await get_leader_name(team)
    if not leader_name:
        return False

    worker_id = _get_agent_id()
    worker_name = _get_agent_name()
    worker_color = _get_teammate_color()

    if not worker_id or not worker_name:
        return False

    try:
        msg = create_sandbox_permission_request_message(
            sender=worker_name,
            recipient=leader_name,
            request_data={
                "requestId": request_id,
                "workerId": worker_id,
                "workerName": worker_name,
                "workerColor": worker_color,
                "host": host,
            },
        )

        await write_to_mailbox(
            leader_name,
            {
                "from": worker_name,
                "text": json.dumps(msg.payload),
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
                ),
                "color": worker_color,
            },
            team,
        )
        return True
    except OSError:
        return False


async def send_sandbox_permission_response_via_mailbox(
    worker_name: str,
    request_id: str,
    host: str,
    allow: bool,
    team_name: str | None = None,
) -> bool:
    """通过邮箱系统将沙箱权限响应发送给工作者。

    由领导者在批准/拒绝沙箱网络访问请求时调用。

    Args:
        worker_name: 接收响应的工作者名称。
        request_id: 原始请求 ID。
        host: 被批准/拒绝的主机名。
        allow: 是否允许连接。
        team_name: 可选团队名称。

    Returns:
        消息成功发送返回 True。
    """
    team = team_name or _get_team_name()
    if not team:
        return False

    sender_name = _get_agent_name() or "team-lead"

    try:
        msg = create_sandbox_permission_response_message(
            sender=sender_name,
            recipient=worker_name,
            response_data={
                "requestId": request_id,
                "host": host,
                "allow": allow,
            },
        )

        await write_to_mailbox(
            worker_name,
            {
                "from": sender_name,
                "text": json.dumps(msg.payload),
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()
                ),
            },
            team,
        )
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Worker helpers: send request / poll response (original mailbox-only approach)
# ---------------------------------------------------------------------------


async def send_permission_request(
    request: SwarmPermissionRequest,
    team_name: str,
    worker_id: str,
    leader_id: str = "leader",
) -> None:
    """序列化 *request* 并写入领导者的邮箱。

    这是原始的结构化载荷方式。新代码建议使用
    :func:`send_permission_request_via_mailbox`。

    Args:
        request: 待转发的权限请求。
        team_name: 用于邮箱路由的 Swarm 团队名称。
        worker_id: 发送方工作者的智能体 ID。
        leader_id: 领导者的智能体 ID（默认 ``"leader"``）。
    """
    payload: dict[str, Any] = {
        "request_id": request.id,
        "tool_name": request.tool_name,
        "tool_use_id": request.tool_use_id,
        "input": request.input,
        "description": request.description,
        "permission_suggestions": request.permission_suggestions,
        "worker_id": worker_id,
    }
    msg = MailboxMessage(
        id=str(uuid.uuid4()),
        type="permission_request",
        sender=worker_id,
        recipient=leader_id,
        payload=payload,
        timestamp=time.time(),
    )
    leader_mailbox = TeammateMailbox(team_name, leader_id)
    await leader_mailbox.write(msg)


async def poll_permission_response(
    team_name: str,
    worker_id: str,
    request_id: str,
    timeout: float = 60.0,
) -> SwarmPermissionResponse | None:
    """轮询工作者自身邮箱，直到收到匹配的 ``permission_response``。

    每 0.5 秒检查一次，最多等待 *timeout* 秒。找到匹配 *request_id*
    的响应后，将消息标记已读并返回解码后的 :class:`SwarmPermissionResponse`。

    Args:
        team_name: Swarm 团队名称。
        worker_id: 工作者智能体 ID（拥有此邮箱）。
        request_id: 要匹配的 :class:`SwarmPermissionRequest` 的 ID。
        timeout: 返回 ``None`` 前的最大等待秒数。

    Returns:
        :class:`SwarmPermissionResponse`，超时返回 ``None``。
    """
    worker_mailbox = TeammateMailbox(team_name, worker_id)
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        messages = await worker_mailbox.read_all(unread_only=True)
        for msg in messages:
            if msg.type == "permission_response":
                payload = msg.payload
                if payload.get("request_id") == request_id:
                    await worker_mailbox.mark_read(msg.id)
                    return SwarmPermissionResponse(
                        request_id=payload["request_id"],
                        allowed=bool(payload.get("allowed", False)),
                        feedback=payload.get("feedback"),
                        updated_rules=payload.get("updated_rules", []),
                    )
        await asyncio.sleep(0.5)

    return None


# ---------------------------------------------------------------------------
# Leader helper: evaluate and send response
# ---------------------------------------------------------------------------


async def handle_permission_request(
    request: SwarmPermissionRequest,
    checker: "PermissionChecker",
) -> SwarmPermissionResponse:
    """使用现有的 :class:`PermissionChecker` 评估 *request*。

    只读工具自动批准，无需咨询检查器。其他工具调用检查器的 ``evaluate``
    方法；若工具被允许或仅需确认（且无阻止条件），则批准；否则拒绝。

    Args:
        request: 来自工作者的权限请求。
        checker: 已配置的 :class:`~openharness.permissions.checker.PermissionChecker`。

    Returns:
        包含决定的 :class:`SwarmPermissionResponse`。
    """
    if _is_read_only(request.tool_name):
        return SwarmPermissionResponse(
            request_id=request.id,
            allowed=True,
            feedback=None,
        )

    file_path: str | None = (
        request.input.get("file_path")  # type: ignore[assignment]
        or request.input.get("path")
        or None
    )
    command: str | None = request.input.get("command")  # type: ignore[assignment]

    decision = checker.evaluate(
        request.tool_name,
        is_read_only=False,
        file_path=file_path,
        command=command,
    )

    allowed = decision.allowed
    feedback: str | None = None if allowed else decision.reason

    return SwarmPermissionResponse(
        request_id=request.id,
        allowed=allowed,
        feedback=feedback,
    )


# ---------------------------------------------------------------------------
# Leader helper: write response back to a worker's mailbox
# ---------------------------------------------------------------------------


async def send_permission_response(
    response: SwarmPermissionResponse,
    team_name: str,
    worker_id: str,
    leader_id: str = "leader",
) -> None:
    """将 *response* 写入工作者的邮箱。

    这是原始的结构化载荷方式。新代码建议使用
    :func:`send_permission_response_via_mailbox`。

    Args:
        response: 待发送的处理结果。
        team_name: Swarm 团队名称。
        worker_id: 目标工作者的智能体 ID。
        leader_id: 发送方领导者的智能体 ID（默认 ``"leader"``）。
    """
    payload: dict[str, Any] = {
        "request_id": response.request_id,
        "allowed": response.allowed,
        "feedback": response.feedback,
        "updated_rules": response.updated_rules,
    }
    msg = MailboxMessage(
        id=str(uuid.uuid4()),
        type="permission_response",
        sender=leader_id,
        recipient=worker_id,
        payload=payload,
        timestamp=time.time(),
    )
    worker_mailbox = TeammateMailbox(team_name, worker_id)
    await worker_mailbox.write(msg)
