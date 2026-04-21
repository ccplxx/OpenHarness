"""进入计划（Plan）权限模式的工具。

本模块提供 EnterPlanModeTool，用于将 OpenHarness 的权限模式切换为 PLAN 模式。
在 PLAN 模式下，代理只进行规划和分析，不执行任何写操作。
切换通过修改持久化设置实现。
"""

from __future__ import annotations

from pydantic import BaseModel

from openharness.config.settings import load_settings, save_settings
from openharness.permissions import PermissionMode
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class EnterPlanModeToolInput(BaseModel):
    """进入计划模式工具的输入参数（无额外参数）。"""


class EnterPlanModeTool(BaseTool):
    """将权限模式切换为 PLAN 模式的工具。

    在 PLAN 模式下，代理只进行规划和分析，不执行写操作。
    """

    name = "enter_plan_mode"
    description = "Switch permission mode to plan."
    input_model = EnterPlanModeToolInput

    async def execute(self, arguments: EnterPlanModeToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行权限模式切换。

        加载当前设置，将权限模式设为 PLAN 并保存。

        Args:
            arguments: 输入参数（无额外参数）
            context: 工具执行上下文（未使用）

        Returns:
            模式切换确认信息
        """
        del arguments, context
        settings = load_settings()
        settings.permission.mode = PermissionMode.PLAN
        save_settings(settings)
        return ToolResult(output="Permission mode set to plan")
