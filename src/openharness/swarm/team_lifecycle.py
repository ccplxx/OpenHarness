"""Swarm 团队持久化生命周期管理模块。

团队以 JSON 文件存储于磁盘：
    ~/.openharness/teams/<name>/team.json

本模块提供 TeamMember、TeamFile、AllowedPath、TeamLifecycleManager
以及完整的 CRUD 辅助函数，与 TS 源码 teamHelpers.ts API 对齐。
TeamLifecycleManager 可与 coordinator_mode.py 中的内存 TeamRegistry
协同工作而无需修改该模块。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from openharness.swarm.mailbox import get_team_dir
from openharness.swarm.types import BackendType


# ---------------------------------------------------------------------------
# Name sanitisation (matching TS sanitizeName / sanitizeAgentName)
# ---------------------------------------------------------------------------


def sanitize_name(name: str) -> str:
    """将所有非字母数字字符替换为连字符并转小写。

    镜像 TS ``sanitizeName``：
    ``name.replace(/[^a-zA-Z0-9]/g, '-').toLowerCase()``
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", name).lower()


def sanitize_agent_name(name: str) -> str:
    """将 ``@`` 替换为 ``-`` 以避免 agentName@teamName 格式的歧义。

    镜像 TS ``sanitizeAgentName``：
    ``name.replace(/@/g, '-')``
    """
    return name.replace("@", "-")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class AllowedPath:
    """所有团队成员无需请求权限即可编辑的路径。"""

    path: str
    """绝对目录路径。"""

    tool_name: str
    """适用的工具名称（如 'Edit'、'Write'）。"""

    added_by: str
    """添加此规则的智能体名称。"""

    added_at: float = field(default_factory=time.time)
    """规则添加时的时间戳。"""

    def to_dict(self) -> dict[str, Any]:
        """将允许路径规则序列化为字典。

        Returns:
            包含 path、tool_name、added_by、added_at 的字典。
        """

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AllowedPath":
        """从字典反序列化构建 AllowedPath 实例。

        兼容 camelCase 和 snake_case 字段名。

        Args:
            data: 包含允许路径字段的字典。

        Returns:
            反序列化后的 :class:`AllowedPath` 实例。
        """
            path=data["path"],
            tool_name=data.get("tool_name", data.get("toolName", "")),
            added_by=data.get("added_by", data.get("addedBy", "")),
            added_at=data.get("added_at", data.get("addedAt", time.time())),
        )


@dataclass
class TeamMember:
    """Swarm 团队的成员。"""

    agent_id: str
    name: str
    backend_type: BackendType
    joined_at: float

    # Optional fields matching TS TeamFile member shape
    agent_type: str | None = None
    """智能体的类型/角色（如 'researcher'、'test-runner'）。"""

    model: str | None = None
    """该智能体使用的模型标识符。"""

    prompt: str | None = None
    """该智能体的初始系统提示词。"""

    color: str | None = None
    """分配的显示颜色（如 'red'、'blue'、'green'）。"""

    plan_mode_required: bool = False
    """该智能体是否需要在行动前进入计划模式审批。"""

    session_id: str | None = None
    """该智能体的实际会话 UUID（用于发现）。"""

    subscriptions: list[str] = field(default_factory=list)
    """该智能体订阅的事件主题。"""

    is_active: bool = True
    """空闲时为 False；活跃时为 True/未定义。"""

    mode: str | None = None
    """该智能体当前的权限模式（如 'auto'、'manual'）。"""

    tmux_pane_id: str = ""
    """面板后端智能体的 Tmux/iTerm2 面板 ID。"""

    cwd: str = ""
    """该智能体的工作目录。"""

    worktree_path: str | None = None
    """Git worktree 路径（若智能体在隔离的 worktree 中操作）。"""

    permissions: list[str] = field(default_factory=list)
    """遗留权限字符串列表。"""

    status: Literal["active", "idle", "stopped"] = "active"
    """该智能体的粗略状态。"""

    def to_dict(self) -> dict[str, Any]:
        """将团队成员信息序列化为字典。

        Returns:
            包含所有成员字段的字典。
        """
            "backend_type": self.backend_type,
            "joined_at": self.joined_at,
            "agent_type": self.agent_type,
            "model": self.model,
            "prompt": self.prompt,
            "color": self.color,
            "plan_mode_required": self.plan_mode_required,
            "session_id": self.session_id,
            "subscriptions": self.subscriptions,
            "is_active": self.is_active,
            "mode": self.mode,
            "tmux_pane_id": self.tmux_pane_id,
            "cwd": self.cwd,
            "worktree_path": self.worktree_path,
            "permissions": self.permissions,
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TeamMember":
        """从字典反序列化构建 TeamMember 实例。

        Args:
            data: 包含成员字段的字典。

        Returns:
            反序列化后的 :class:`TeamMember` 实例。
        """
            agent_id=data["agent_id"],
            name=data["name"],
            backend_type=data["backend_type"],
            joined_at=data["joined_at"],
            agent_type=data.get("agent_type"),
            model=data.get("model"),
            prompt=data.get("prompt"),
            color=data.get("color"),
            plan_mode_required=data.get("plan_mode_required", False),
            session_id=data.get("session_id"),
            subscriptions=data.get("subscriptions", []),
            is_active=data.get("is_active", True),
            mode=data.get("mode"),
            tmux_pane_id=data.get("tmux_pane_id", ""),
            cwd=data.get("cwd", ""),
            worktree_path=data.get("worktree_path"),
            permissions=data.get("permissions", []),
            status=data.get("status", "active"),
        )


@dataclass
class TeamFile:
    """持久化的团队元数据，以 team.json 存储在团队目录中。"""

    name: str
    created_at: float

    description: str = ""

    lead_agent_id: str = ""
    """团队领导者的智能体 ID。"""

    lead_session_id: str | None = None
    """领导者的实际会话 UUID（用于发现）。"""

    hidden_pane_ids: list[str] = field(default_factory=list)
    """当前在 UI 中隐藏的面板 ID。"""

    members: dict[str, TeamMember] = field(default_factory=dict)
    """agent_id → TeamMember 的映射字典。"""

    team_allowed_paths: list[AllowedPath] = field(default_factory=list)
    """所有 teammate 无需请求权限即可编辑的路径。"""

    allowed_paths: list[str] = field(default_factory=list)
    """遗留的允许路径字符串列表。"""

    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """将团队文件元数据序列化为字典，用于 JSON 持久化。

        members 和 team_allowed_paths 会递归调用各自的 ``to_dict``。

        Returns:
            包含所有团队字段的字典。
        """
            "created_at": self.created_at,
            "lead_agent_id": self.lead_agent_id,
            "lead_session_id": self.lead_session_id,
            "hidden_pane_ids": self.hidden_pane_ids,
            "members": {k: v.to_dict() for k, v in self.members.items()},
            "team_allowed_paths": [p.to_dict() for p in self.team_allowed_paths],
            "allowed_paths": self.allowed_paths,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TeamFile":
        """从字典反序列化构建 TeamFile 实例。

        会递归反序列化 members 和 team_allowed_paths。

        Args:
            data: 包含团队文件字段的字典。

        Returns:
            反序列化后的 :class:`TeamFile` 实例。
        """
        members = {
            k: TeamMember.from_dict(v)
            for k, v in data.get("members", {}).items()
        }
        team_allowed_paths = [
            AllowedPath.from_dict(p)
            for p in data.get("team_allowed_paths", [])
        ]
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            created_at=data["created_at"],
            lead_agent_id=data.get("lead_agent_id", ""),
            lead_session_id=data.get("lead_session_id"),
            hidden_pane_ids=data.get("hidden_pane_ids", []),
            members=members,
            team_allowed_paths=team_allowed_paths,
            allowed_paths=data.get("allowed_paths", []),
            metadata=data.get("metadata", {}),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Path) -> None:
        """原子性地将团队文件写入 *path*。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        tmp.rename(path)

    @classmethod
    def load(cls, path: Path) -> "TeamFile":
        """从 *path* 加载 TeamFile。

        Raises:
            FileNotFoundError: *path* 不存在时。
            json.JSONDecodeError: 文件不是有效 JSON 时。
        """
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

_TEAM_FILE_NAME = "team.json"


def _team_file_path(name: str) -> Path:
    """返回指定团队名称的 ``team.json`` 文件路径。

    Args:
        name: 团队名称。

    Returns:
        ``~/.openharness/teams/<name>/team.json`` 的 Path 对象。
    """


def get_team_file_path(team_name: str) -> Path:
    """公开访问器：返回指定团队的 ``team.json`` 路径。

    Args:
        team_name: 团队名称。

    Returns:
        team.json 的 Path 对象。
    """
    return _team_file_path(team_name)


# ---------------------------------------------------------------------------
# Synchronous read/write helpers (for sync contexts)
# ---------------------------------------------------------------------------


def read_team_file(team_name: str) -> TeamFile | None:
    """读取并返回指定团队的 TeamFile，缺失时返回 ``None``。

    使用同步 I/O——适用于同步上下文，如类 React 渲染路径或信号处理器。
    """
    path = _team_file_path(team_name)
    if not path.exists():
        return None
    try:
        return TeamFile.load(path)
    except (json.JSONDecodeError, KeyError):
        return None


def write_team_file(team_name: str, team_file: TeamFile) -> None:
    """将 *team_file* 持久化到磁盘（同步）。"""
    team_file.save(_team_file_path(team_name))


# ---------------------------------------------------------------------------
# Async read/write helpers
# ---------------------------------------------------------------------------


async def read_team_file_async(team_name: str) -> TeamFile | None:
    """ :func:`read_team_file` 的异步包装器。"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, read_team_file, team_name)


async def write_team_file_async(team_name: str, team_file: TeamFile) -> None:
    """ :func:`write_team_file` 的异步包装器。"""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, write_team_file, team_name, team_file)


# ---------------------------------------------------------------------------
# Member management helpers (standalone functions)
# ---------------------------------------------------------------------------


def remove_teammate_from_team_file(
    team_name: str,
    identifier: dict[str, str | None],
) -> bool:
    """通过 agent_id 或名称从团队文件中移除 teammate。

    Args:
        team_name: 团队名称。
        identifier: 包含可选 ``agent_id`` 和/或 ``name`` 键的字典。

    Returns:
        成功移除成员返回 True，否则 False。
    """
    agent_id = identifier.get("agent_id")
    name = identifier.get("name")
    if not agent_id and not name:
        return False

    team_file = read_team_file(team_name)
    if not team_file:
        return False

    original_len = len(team_file.members)
    to_remove = [
        k
        for k, m in team_file.members.items()
        if (agent_id and m.agent_id == agent_id) or (name and m.name == name)
    ]
    for k in to_remove:
        del team_file.members[k]

    if len(team_file.members) == original_len:
        return False

    write_team_file(team_name, team_file)
    return True


def add_hidden_pane_id(team_name: str, pane_id: str) -> bool:
    """将 *pane_id* 添加到团队文件的隐藏面板列表。

    Returns:
        成功返回 True，团队不存在返回 False。
    """
    team_file = read_team_file(team_name)
    if not team_file:
        return False

    if pane_id not in team_file.hidden_pane_ids:
        team_file.hidden_pane_ids.append(pane_id)
        write_team_file(team_name, team_file)
    return True


def remove_hidden_pane_id(team_name: str, pane_id: str) -> bool:
    """从团队文件的隐藏面板列表中移除 *pane_id*。

    Returns:
        成功返回 True，团队不存在返回 False。
    """
    team_file = read_team_file(team_name)
    if not team_file:
        return False

    try:
        team_file.hidden_pane_ids.remove(pane_id)
        write_team_file(team_name, team_file)
    except ValueError:
        pass
    return True


def remove_member_from_team(team_name: str, tmux_pane_id: str) -> bool:
    """通过 tmux 面板 ID 移除团队成员（同时从隐藏面板列表移除）。

    Returns:
        找到并移除成员返回 True，否则 False。
    """
    team_file = read_team_file(team_name)
    if not team_file:
        return False

    to_remove = [
        k
        for k, m in team_file.members.items()
        if m.tmux_pane_id == tmux_pane_id
    ]
    if not to_remove:
        return False

    for k in to_remove:
        del team_file.members[k]

    # Also clean up hidden_pane_ids
    try:
        team_file.hidden_pane_ids.remove(tmux_pane_id)
    except ValueError:
        pass

    write_team_file(team_name, team_file)
    return True


def remove_member_by_agent_id(team_name: str, agent_id: str) -> bool:
    """通过智能体 ID 移除团队成员。

    适用于可能共享同一 tmux_pane_id 的进程内 teammate。

    Returns:
        找到并移除成员返回 True，否则 False。
    """
    team_file = read_team_file(team_name)
    if not team_file:
        return False

    if agent_id not in team_file.members:
        return False

    del team_file.members[agent_id]
    write_team_file(team_name, team_file)
    return True


# ---------------------------------------------------------------------------
# Mode and active-status helpers
# ---------------------------------------------------------------------------


def set_member_mode(
    team_name: str,
    member_name: str,
    mode: str,
) -> bool:
    """设置团队成员的权限模式。

    团队领导者更改 teammate 模式时调用。

    Args:
        team_name: 团队名称。
        member_name: 待更新成员的 *name*（非 agent_id）。
        mode: 新的权限模式字符串（如 ``'auto'``、``'manual'``）。

    Returns:
        成功返回 True，团队或成员不存在返回 False。
    """
    team_file = read_team_file(team_name)
    if not team_file:
        return False

    member = next(
        (m for m in team_file.members.values() if m.name == member_name), None
    )
    if not member:
        return False

    if member.mode == mode:
        return True

    # Immutably update
    for k, m in team_file.members.items():
        if m.name == member_name:
            team_file.members[k] = TeamMember(
                **{**m.to_dict(), "mode": mode}  # type: ignore[arg-type]
            )
            break

    write_team_file(team_name, team_file)
    return True


def sync_teammate_mode(
    mode: str,
    team_name_override: str | None = None,
) -> None:
    """将当前智能体的权限模式同步到团队配置文件。

    若 ``CLAUDE_CODE_AGENT_NAME`` 或解析的团队名称未设置则为空操作。

    Args:
        mode: 待同步的权限模式。
        team_name_override: 可选的团队名称覆盖。
    """
    team_name = team_name_override or os.environ.get("CLAUDE_CODE_TEAM_NAME")
    agent_name = os.environ.get("CLAUDE_CODE_AGENT_NAME")
    if team_name and agent_name:
        set_member_mode(team_name, agent_name, mode)


def set_multiple_member_modes(
    team_name: str,
    mode_updates: list[dict[str, str]],
) -> bool:
    """在单次原子写入中设置多个团队成员的权限模式。

    Args:
        team_name: 团队名称。
        mode_updates: 包含 ``member_name`` 和 ``mode`` 键的字典列表。

    Returns:
        找到团队文件返回 True（即使无变更）。
    """
    team_file = read_team_file(team_name)
    if not team_file:
        return False

    update_map = {u["member_name"]: u["mode"] for u in mode_updates}
    any_changed = False

    for k, m in list(team_file.members.items()):
        new_mode = update_map.get(m.name)
        if new_mode is not None and m.mode != new_mode:
            team_file.members[k] = TeamMember(
                **{**m.to_dict(), "mode": new_mode}  # type: ignore[arg-type]
            )
            any_changed = True

    if any_changed:
        write_team_file(team_name, team_file)
    return True


async def set_member_active(
    team_name: str,
    member_name: str,
    is_active: bool,
) -> None:
    """设置团队成员的活跃状态（异步）。

    teammate 进入空闲（is_active=False）或开始新轮次（is_active=True）时调用。

    Args:
        team_name: 团队名称。
        member_name: 待更新成员的 *name*。
        is_active: 成员是否活跃。
    """
    team_file = await read_team_file_async(team_name)
    if not team_file:
        return

    member = next(
        (m for m in team_file.members.values() if m.name == member_name), None
    )
    if not member:
        return

    if member.is_active == is_active:
        return

    for k, m in list(team_file.members.items()):
        if m.name == member_name:
            team_file.members[k] = TeamMember(
                **{**m.to_dict(), "is_active": is_active}  # type: ignore[arg-type]
            )
            break

    await write_team_file_async(team_name, team_file)


# ---------------------------------------------------------------------------
# Session cleanup tracking
# ---------------------------------------------------------------------------

_session_created_teams: set[str] = set()


def register_team_for_session_cleanup(team_name: str) -> None:
    """将团队标记为本会话创建，以便退出时清理。

    在初始 write_team_file 后立即调用。
    显式删除团队后应调用 :func:`unregister_team_for_session_cleanup` 以防止重复清理。
    """
    _session_created_teams.add(team_name)


def unregister_team_for_session_cleanup(team_name: str) -> None:
    """从会话清理追踪中移除团队（如显式删除后）。"""
    _session_created_teams.discard(team_name)


async def _kill_orphaned_teammate_panes(team_name: str) -> None:
    """尽力终止团队所有面板后端 teammate 的面板。

    从 :func:`cleanup_session_teams` 在非优雅领导者退出
    （SIGINT/SIGTERM）时调用。仅删除目录会使 teammate 进程
    在打开的 tmux/iTerm2 面板中成为孤儿；此函数先终止它们。

    镜像 TS teamHelpers.ts 中的 ``killOrphanedTeammatePanes``。
    """
    from openharness.swarm.registry import get_backend_registry
    from openharness.swarm.spawn_utils import is_inside_tmux
    from openharness.swarm.types import is_pane_backend

    team_file = read_team_file(team_name)
    if not team_file:
        return

    pane_members = [
        m
        for m in team_file.members.values()
        if m.name != "team-lead"
        and m.tmux_pane_id
        and m.backend_type
        and is_pane_backend(m.backend_type)
    ]
    if not pane_members:
        return

    registry = get_backend_registry()
    use_external_session = not is_inside_tmux()

    async def _kill_one(member: TeamMember) -> None:
        try:
            executor = registry.get_executor(member.backend_type)
            await executor.kill_pane(
                member.tmux_pane_id,
                use_external_session=use_external_session,
            )
        except Exception:
            pass

    await asyncio.gather(*(_kill_one(m) for m in pane_members), return_exceptions=True)


async def cleanup_session_teams() -> None:
    """清理本会话创建但未显式删除的所有团队。

    先终止孤立的 teammate 面板，然后移除通过
    :func:`register_team_for_session_cleanup` 注册的每个团队的目录。
    可安全多次调用。
    """
    if not _session_created_teams:
        return

    teams = list(_session_created_teams)
    # Kill panes first — on SIGINT the teammate processes are still running;
    # deleting directories alone would orphan them in open tmux/iTerm2 panes.
    await asyncio.gather(
        *(_kill_orphaned_teammate_panes(t) for t in teams),
        return_exceptions=True,
    )
    await asyncio.gather(
        *(cleanup_team_directories(t) for t in teams),
        return_exceptions=True,
    )
    _session_created_teams.clear()


# ---------------------------------------------------------------------------
# Worktree cleanup
# ---------------------------------------------------------------------------


async def _destroy_worktree(worktree_path: str) -> None:
    """尽力移除 git worktree。

    先尝试 ``git worktree remove --force``；回退到 ``shutil.rmtree``。
    """
    wt = Path(worktree_path)
    git_file = wt / ".git"
    main_repo_path: str | None = None

    try:
        content = git_file.read_text(encoding="utf-8").strip()
        match = re.match(r"^gitdir:\s*(.+)$", content)
        if match:
            worktree_git_dir = match.group(1)
            main_git_dir = Path(worktree_git_dir) / ".." / ".."
            main_repo_path = str(main_git_dir / "..")
    except OSError:
        pass

    if main_repo_path:
        try:
            result = subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                cwd=main_repo_path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return
            if "not a working tree" in (result.stderr or ""):
                return
        except (subprocess.SubprocessError, OSError):
            pass

    try:
        shutil.rmtree(worktree_path, ignore_errors=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Team directory cleanup
# ---------------------------------------------------------------------------


async def cleanup_team_directories(team_name: str) -> None:
    """清理指定团队的团队目录和任务目录。

    同时移除为团队成员创建的 git worktree。在 Swarm 会话终止时调用。

    Args:
        team_name: 待清理的团队名称。
    """
    # Read team file to get worktree paths BEFORE deleting the team directory
    team_file = read_team_file(team_name)
    worktree_paths: list[str] = []
    if team_file:
        for member in team_file.members.values():
            if member.worktree_path:
                worktree_paths.append(member.worktree_path)

    # Clean up worktrees first
    for wt_path in worktree_paths:
        await _destroy_worktree(wt_path)

    # Remove the team directory
    team_dir = get_team_dir(team_name)
    try:
        shutil.rmtree(team_dir, ignore_errors=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# TeamLifecycleManager
# ---------------------------------------------------------------------------


class TeamLifecycleManager:
    """管理 Swarm 团队的磁盘生命周期。

    将团队元数据持久化到 ``~/.openharness/teams/<name>/team.json``。
    与邮箱系统的目录布局集成——此处创建的团队目录与
    :class:`TeammateMailbox` 使用的相同，因此无需额外设置即可
    添加和消息智能体。

    此类是无状态的：每个方法都直接读写磁盘，因此多次实例化是安全的。
    """

    # ------------------------------------------------------------------
    # Team CRUD
    # ------------------------------------------------------------------

    def create_team(self, name: str, description: str = "") -> TeamFile:
        """创建新团队并持久化到磁盘。

        Raises:
            ValueError: 同名团队已存在时。
        """
        path = _team_file_path(name)
        if path.exists():
            raise ValueError(f"Team '{name}' already exists at {path}")

        team = TeamFile(
            name=name,
            description=description,
            created_at=time.time(),
        )
        team.save(path)
        return team

    def delete_team(self, name: str) -> None:
        """移除团队目录及其所有内容（包括邮箱）。

        Raises:
            ValueError: 团队不存在时。
        """
        team_dir = get_team_dir(name)
        team_file = team_dir / _TEAM_FILE_NAME
        if not team_file.exists():
            raise ValueError(f"Team '{name}' does not exist")
        shutil.rmtree(team_dir)

    def get_team(self, name: str) -> TeamFile | None:
        """返回指定名称的 TeamFile，不存在则返回 ``None``。"""
        path = _team_file_path(name)
        if not path.exists():
            return None
        try:
            return TeamFile.load(path)
        except (json.JSONDecodeError, KeyError):
            return None

    def list_teams(self) -> list[TeamFile]:
        """返回 ``~/.openharness/teams/`` 中找到的所有团队，按名称排序。"""
        base = Path.home() / ".openharness" / "teams"
        if not base.exists():
            return []

        teams: list[TeamFile] = []
        for team_dir in sorted(base.iterdir()):
            team_file = team_dir / _TEAM_FILE_NAME
            if not team_file.exists():
                continue
            try:
                teams.append(TeamFile.load(team_file))
            except (json.JSONDecodeError, KeyError):
                continue
        return teams

    # ------------------------------------------------------------------
    # Member management
    # ------------------------------------------------------------------

    def add_member(self, team_name: str, member: TeamMember) -> TeamFile:
        """将 *member* 添加到 *team_name* 并持久化。

        若同 ``agent_id`` 的成员已存在则替换。

        Raises:
            ValueError: 团队不存在时。
        """
        path = _team_file_path(team_name)
        team = self._require_team(team_name, path)
        team.members[member.agent_id] = member
        team.save(path)
        return team

    def remove_member(self, team_name: str, agent_id: str) -> TeamFile:
        """从 *team_name* 移除指定 *agent_id* 的成员并持久化。

        Raises:
            ValueError: 团队或成员不存在时。
        """
        path = _team_file_path(team_name)
        team = self._require_team(team_name, path)
        if agent_id not in team.members:
            raise ValueError(
                f"Agent '{agent_id}' is not a member of team '{team_name}'"
            )
        del team.members[agent_id]
        team.save(path)
        return team

    # ------------------------------------------------------------------
    # Mode helpers (proxy to standalone functions)
    # ------------------------------------------------------------------

    def set_member_mode(
        self, team_name: str, member_name: str, mode: str
    ) -> bool:
        """设置团队成员的权限模式。"""
        return set_member_mode(team_name, member_name, mode)

    async def set_member_active(
        self, team_name: str, member_name: str, is_active: bool
    ) -> None:
        """设置团队成员的活跃状态。"""
        await set_member_active(team_name, member_name, is_active)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_team(self, name: str, path: Path) -> TeamFile:
        """加载团队文件，若不存在则抛出 ValueError。

        Args:
            name: 团队名称（用于错误消息）。
            path: team.json 的路径。

        Returns:
            加载的 :class:`TeamFile` 实例。

        Raises:
            ValueError: 团队文件不存在时。
        """
