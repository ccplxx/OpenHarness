"""团队创建工具。

本模块提供 TeamCreateTool，用于创建轻量级内存中的团队。
团队用于组织和管理一组代理任务，支持设置团队名称和描述。
如果团队名已存在则返回错误。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.coordinator.coordinator_mode import get_team_registry
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class TeamCreateToolInput(BaseModel):
    """团队创建工具的输入参数。

    Attributes:
        name: 团队名称
        description: 团队描述，默认为空
    """

    name: str = Field(description="Team name")
    description: str = Field(default="", description="Team description")


class TeamCreateTool(BaseTool):
    """创建轻量级内存团队的工具。

    团队用于组织和管理一组代理任务。
    """

    name = "team_create"
    description = "Create a lightweight in-memory team for agent tasks."
    input_model = TeamCreateToolInput

    async def execute(self, arguments: TeamCreateToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行团队创建。

        Args:
            arguments: 包含团队名称和描述的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            创建确认信息或名称冲突错误
        """
        del context
        try:
            team = get_team_registry().create_team(arguments.name, arguments.description)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"Created team {team.name}")
