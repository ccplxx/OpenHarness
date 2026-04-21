"""团队删除工具。

本模块提供 TeamDeleteTool，用于删除内存中的团队。
如果团队不存在则返回错误。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.coordinator.coordinator_mode import get_team_registry
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class TeamDeleteToolInput(BaseModel):
    """团队删除工具的输入参数。

    Attributes:
        name: 要删除的团队名称
    """

    name: str = Field(description="Team name")


class TeamDeleteTool(BaseTool):
    """删除内存团队的工具。

    如果团队不存在则返回错误。
    """

    name = "team_delete"
    description = "Delete an in-memory team."
    input_model = TeamDeleteToolInput

    async def execute(self, arguments: TeamDeleteToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行团队删除。

        Args:
            arguments: 包含团队名称的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            删除确认信息或团队不存在的错误
        """
        del context
        try:
            get_team_registry().delete_team(arguments.name)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"Deleted team {arguments.name}")
