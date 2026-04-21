"""退出计划（Plan）权限模式的工具。

本模块提供 ExitPlanModeTool，用于将 OpenHarness 的权限模式从 PLAN 模式
切换回 DEFAULT 模式，恢复代理的正常读写执行权限。
"""

from __future__ import annotations

from pydantic import BaseModel

from openharness.config.settings import load_settings, save_settings
from openharness.permissions import PermissionMode
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class ExitPlanModeToolInput(BaseModel):
    """退出计划模式工具的输入参数（无额外参数）。"""


class ExitPlanModeTool(BaseTool):
    """将权限模式从 PLAN 切换回 DEFAULT 模式的工具。

    恢复代理的正常读写执行权限。
    """

    name = "exit_plan_mode"
    description = "Switch permission mode back to default."
    input_model = ExitPlanModeToolInput

    async def execute(self, arguments: ExitPlanModeToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行权限模式切换回默认。

        Args:
            arguments: 输入参数（无额外参数）
            context: 工具执行上下文（未使用）

        Returns:
            模式切换确认信息
        """
        del arguments, context
        settings = load_settings()
        settings.permission.mode = PermissionMode.DEFAULT
        save_settings(settings)
        return ToolResult(output="Permission mode set to default")
