"""Git Worktree 创建与进入工具。

本模块提供 EnterWorktreeTool，用于创建 git worktree 并返回其路径。
支持创建新分支或切换到已有分支的 worktree。
Worktree 默认路径为 .openharness/worktrees/<branch-slug>，也可自定义指定。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
import re

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class EnterWorktreeToolInput(BaseModel):
    """Git Worktree 创建工具的输入参数。

    Attributes:
        branch: 目标分支名称
        path: 可选的 worktree 路径
        create_branch: 是否创建新分支，默认为 True
        base_ref: 创建新分支时的基准引用，默认为 HEAD
    """

    branch: str = Field(description="Target branch name for the worktree")
    path: str | None = Field(default=None, description="Optional worktree path")
    create_branch: bool = Field(default=True)
    base_ref: str = Field(default="HEAD", description="Base ref when creating a new branch")


class EnterWorktreeTool(BaseTool):
    """创建 git worktree 并返回其路径的工具。

    支持创建新分支或切换到已有分支的 worktree。
    """

    name = "enter_worktree"
    description = "Create a git worktree and return its path."
    input_model = EnterWorktreeToolInput

    async def execute(
        self,
        arguments: EnterWorktreeToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行 git worktree 创建。

        验证当前目录是否为 git 仓库，解析 worktree 路径，
        然后执行 git worktree add 命令。

        Args:
            arguments: 包含分支名和路径选项的输入参数
            context: 工具执行上下文

        Returns:
            worktree 创建结果及路径信息
        """
        top_level = _git_output(context.cwd, "rev-parse", "--show-toplevel")
        if top_level is None:
            return ToolResult(output="enter_worktree requires a git repository", is_error=True)

        repo_root = Path(top_level)
        worktree_path = _resolve_worktree_path(repo_root, arguments.branch, arguments.path)
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "worktree", "add"]
        if arguments.create_branch:
            cmd.extend(["-b", arguments.branch, str(worktree_path), arguments.base_ref])
        else:
            cmd.extend([str(worktree_path), arguments.branch])
        result = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        output = (result.stdout or result.stderr).strip() or f"Created worktree {worktree_path}"
        if result.returncode != 0:
            return ToolResult(output=output, is_error=True)
        return ToolResult(output=f"{output}\nPath: {worktree_path}")


def _git_output(cwd: Path, *args: str) -> str | None:
    """执行 git 命令并返回输出。

    Args:
        cwd: 执行命令的工作目录
        *args: git 子命令和参数

    Returns:
        命令的标准输出（去除首尾空白），失败时返回 None
    """
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _resolve_worktree_path(repo_root: Path, branch: str, path: str | None) -> Path:
    """解析 worktree 的目标路径。

    如果指定了 path 则使用该路径（相对路径基于仓库根目录），
    否则使用 .openharness/worktrees/<branch-slug> 作为默认路径。

    Args:
        repo_root: git 仓库根目录
        branch: 分支名称（用于生成默认路径的 slug）
        path: 可选的自定义路径

    Returns:
        解析后的绝对路径
    """
    if path:
        resolved = Path(path).expanduser()
        if not resolved.is_absolute():
            resolved = repo_root / resolved
        return resolved.resolve()
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-") or "worktree"
    return (repo_root / ".openharness" / "worktrees" / slug).resolve()
