"""Swarm 智能体的 Git Worktree 隔离模块。

本模块为 Swarm 模式下的智能体提供文件系统级隔离，通过 git worktree
为每个智能体创建独立的工作目录和分支，使其可以并行修改代码而不冲突。

核心组件：
* :func:`validate_worktree_slug` — 验证和清理 worktree 标识符，
  防止路径遍历和非法字符。
* :class:`WorktreeInfo` — 描述一个受管理 worktree 的元数据。
* :class:`WorktreeManager` — 提供 worktree 的创建、删除、列举和
  过期清理功能，自动符号链接 ``node_modules`` 等大型公共目录以避免重复。

Worktree 存储在 ``~/.openharness/worktrees/<slug>/`` 下，slug 中的 ``/``
被替换为 ``+`` 以保持扁平目录布局。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

_VALID_SEGMENT = re.compile(r"^[a-zA-Z0-9._-]+$")
_MAX_SLUG_LENGTH = 64
_COMMON_SYMLINK_DIRS = ("node_modules", ".venv", "__pycache__", ".tox")


def validate_worktree_slug(slug: str) -> str:
    """验证和清理 worktree 标识符。

    规则：
    - 总长度不超过 64 个字符
    - 每个 ``/`` 分隔的段必须匹配 [a-zA-Z0-9._-]+
    - ``.`` 和 ``..`` 段被拒绝（防止路径遍历）
    - 前导/尾随 ``/`` 被拒绝

    验证通过返回原 slug，否则抛出 ValueError。
    """
    if not slug:
        raise ValueError("Worktree slug must not be empty")

    if len(slug) > _MAX_SLUG_LENGTH:
        raise ValueError(
            f"Worktree slug must be {_MAX_SLUG_LENGTH} characters or fewer (got {len(slug)})"
        )

    # Reject absolute paths
    if slug.startswith("/") or slug.startswith("\\"):
        raise ValueError(f"Worktree slug must not be an absolute path: {slug!r}")

    for segment in slug.split("/"):
        if segment in (".", ".."):
            raise ValueError(
                f'Worktree slug {slug!r}: must not contain "." or ".." path segments'
            )
        if not _VALID_SEGMENT.match(segment):
            raise ValueError(
                f"Worktree slug {slug!r}: each segment must be non-empty and contain only "
                "letters, digits, dots, underscores, and dashes"
            )

    return slug


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class WorktreeInfo:
    """受管理的 git worktree 的元数据。"""

    slug: str
    path: Path
    branch: str
    original_path: Path
    created_at: float
    agent_id: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _flatten_slug(slug: str) -> str:
    """将 slug 中的 ``/`` 替换为 ``+`` 以避免嵌套目录/分支问题。

    Args:
        slug: 原始 slug 字符串。

    Returns:
        扁平化后的 slug，所有 ``/`` 被替换为 ``+``。
    """
    return slug.replace("/", "+")


def _worktree_branch(slug: str) -> str:
    """根据 slug 生成 worktree 分支名称。

    格式为 ``worktree-<flattened-slug>``，其中 slug 的 ``/`` 被替换为 ``+``。

    Args:
        slug: 原始 slug 字符串。

    Returns:
        生成的分支名称。
    """
    return f"worktree-{_flatten_slug(slug)}"


async def _run_git(*args: str, cwd: Path) -> tuple[int, str, str]:
    """运行 git 命令，返回 (返回码, stdout, stderr)。"""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": ""},
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode(errors="replace").strip(),
        stderr_bytes.decode(errors="replace").strip(),
    )


async def _symlink_common_dirs(repo_path: Path, worktree_path: Path) -> None:
    """将主仓库的大型公共目录符号链接到 worktree 以避免重复。"""
    for dir_name in _COMMON_SYMLINK_DIRS:
        src = repo_path / dir_name
        dst = worktree_path / dir_name
        if dst.exists() or dst.is_symlink():
            continue
        if not src.exists():
            continue
        try:
            dst.symlink_to(src)
        except OSError:
            pass  # Non-fatal: disk full, unsupported fs, etc.


async def _remove_symlinks(worktree_path: Path) -> None:
    """移除由 _symlink_common_dirs 创建的符号链接。"""
    for dir_name in _COMMON_SYMLINK_DIRS:
        dst = worktree_path / dir_name
        if dst.is_symlink():
            try:
                dst.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# WorktreeManager
# ---------------------------------------------------------------------------

class WorktreeManager:
    """管理用于隔离智能体执行的 git worktree。

    Worktree 存储在 ``base_dir/<slug>/`` 下（``/`` 替换为 ``+``
    以保持扁平布局）。JSON 元数据文件追踪活跃的 worktree 及其
    关联的智能体 ID，以便清理过期条目。
    """

    def __init__(self, base_dir: Path | None = None) -> None:
        """初始化 WorktreeManager。

        Args:
            base_dir: worktree 存储根目录，默认为 ``~/.openharness/worktrees``。
        """
        self.base_dir: Path = base_dir or Path.home() / ".openharness" / "worktrees"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_worktree(
        self,
        repo_path: Path,
        slug: str,
        branch: str | None = None,
        agent_id: str | None = None,
    ) -> WorktreeInfo:
        """为 *slug* 创建（或恢复）git worktree。

        若 worktree 目录已存在且为有效的 git worktree，
        则直接恢复而不重新运行 ``git worktree add``。

        Args:
            repo_path: 主仓库的绝对路径。
            slug: 人类可读标识符（通过 validate_worktree_slug 验证）。
            branch: 要检出的分支名称；默认为生成的 ``worktree-<slug>`` 名称。
            agent_id: 可选的拥有此 worktree 的智能体标识符。

        Returns:
            描述 worktree 的 WorktreeInfo。
        """
        validate_worktree_slug(slug)
        repo_path = repo_path.resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

        flat_slug = _flatten_slug(slug)
        worktree_path = self.base_dir / flat_slug
        worktree_branch = branch or _worktree_branch(slug)

        # Fast resume: check whether the worktree is already registered
        if worktree_path.exists():
            code, _, _ = await _run_git(
                "rev-parse", "--git-dir", cwd=worktree_path
            )
            if code == 0:
                return WorktreeInfo(
                    slug=slug,
                    path=worktree_path,
                    branch=worktree_branch,
                    original_path=repo_path,
                    created_at=worktree_path.stat().st_mtime,
                    agent_id=agent_id,
                )

        # New worktree: -B resets an orphan branch left by a prior remove
        code, _, stderr = await _run_git(
            "worktree", "add", "-B", worktree_branch, str(worktree_path), "HEAD",
            cwd=repo_path,
        )
        if code != 0:
            raise RuntimeError(f"git worktree add failed: {stderr}")

        await _symlink_common_dirs(repo_path, worktree_path)

        return WorktreeInfo(
            slug=slug,
            path=worktree_path,
            branch=worktree_branch,
            original_path=repo_path,
            created_at=time.time(),
            agent_id=agent_id,
        )

    async def remove_worktree(self, slug: str) -> bool:
        """按 slug 移除 worktree。

        先清理符号链接，然后运行 ``git worktree remove --force``。

        Returns:
            成功移除返回 True；不存在返回 False。
        """
        validate_worktree_slug(slug)
        flat_slug = _flatten_slug(slug)
        worktree_path = self.base_dir / flat_slug

        if not worktree_path.exists():
            return False

        # Remove symlinks before git removes the directory
        await _remove_symlinks(worktree_path)

        # Determine repo root from the worktree's git metadata
        code, git_common, _ = await _run_git(
            "rev-parse", "--git-common-dir", cwd=worktree_path
        )
        if code == 0 and git_common:
            # git_common points to .git inside the main repo
            repo_path = Path(git_common).resolve().parent
            if repo_path.exists():
                await _run_git(
                    "worktree", "remove", "--force", str(worktree_path),
                    cwd=repo_path,
                )
                return True

        # Fallback: try to remove via absolute path from any working directory
        # If repo_path detection failed, attempt removal with cwd=base_dir
        code, _, _ = await _run_git(
            "worktree", "remove", "--force", str(worktree_path),
            cwd=self.base_dir,
        )
        return code == 0

    async def list_worktrees(self) -> list[WorktreeInfo]:
        """返回 base_dir 下所有已知 worktree 的 WorktreeInfo。"""
        if not self.base_dir.exists():
            return []

        results: list[WorktreeInfo] = []
        for child in self.base_dir.iterdir():
            if not child.is_dir():
                continue
            code, _, _ = await _run_git("rev-parse", "--git-dir", cwd=child)
            if code != 0:
                continue

            # Recover branch name from HEAD
            rc, branch_out, _ = await _run_git(
                "rev-parse", "--abbrev-ref", "HEAD", cwd=child
            )
            branch = branch_out if rc == 0 else "unknown"

            # Recover original repo path from git-common-dir
            rc2, common_dir, _ = await _run_git(
                "rev-parse", "--git-common-dir", cwd=child
            )
            if rc2 == 0 and common_dir:
                original_path = Path(common_dir).resolve().parent
            else:
                original_path = child

            # Slug is the directory name (flat form); restore '/' from '+'
            slug = child.name.replace("+", "/")
            results.append(
                WorktreeInfo(
                    slug=slug,
                    path=child,
                    branch=branch,
                    original_path=original_path,
                    created_at=child.stat().st_mtime,
                )
            )

        return results

    async def cleanup_stale(self, active_agent_ids: set[str] | None = None) -> list[str]:
        """移除无活跃智能体的 worktree。

        Args:
            active_agent_ids: 仍在运行的智能体 ID 集合。为 None 时，
                所有带 agent_id 的 worktree 均视为过期。

        Returns:
            已移除的 slug 列表。
        """
        worktrees = await self.list_worktrees()
        removed: list[str] = []
        for info in worktrees:
            if info.agent_id is None:
                continue
            if active_agent_ids is not None and info.agent_id in active_agent_ids:
                continue
            ok = await self.remove_worktree(info.slug)
            if ok:
                removed.append(info.slug)
        return removed
