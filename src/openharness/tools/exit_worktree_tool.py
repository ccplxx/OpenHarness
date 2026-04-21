"""Git Worktree 移除工具。

本模块提供 ExitWorktreeTool，用于通过路径移除已创建的 git worktree。
使用 git worktree remove --force 命令执行移除操作。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class ExitWorktreeToolInput(BaseModel):
    """Git Worktree 移除工具的输入参数。

    Attributes:
        path: 要移除的 worktree 路径
    """

    path: str = Field(description="Worktree path to remove")


class ExitWorktreeTool(BaseTool):
    """移除 git worktree 的工具。

    通过路径强制移除指定的 worktree。
    """

    name = "exit_worktree"
    description = "Remove a git worktree by path."
    input_model = ExitWorktreeToolInput

    async def execute(
        self,
        arguments: ExitWorktreeToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行 git worktree 移除。

        解析路径后执行 git worktree remove --force 命令。

        Args:
            arguments: 包含 worktree 路径的输入参数
            context: 工具执行上下文

        Returns:
            移除结果信息
        """
        path = Path(arguments.path).expanduser()
        if not path.is_absolute():
            path = (context.cwd / path).resolve()
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(path)],
            cwd=context.cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        output = (result.stdout or result.stderr).strip() or f"Removed worktree {path}"
        return ToolResult(output=output, is_error=result.returncode != 0)
