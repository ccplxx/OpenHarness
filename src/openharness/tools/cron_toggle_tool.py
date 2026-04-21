"""本地 Cron 定时任务启用/禁用切换工具。

本模块提供 CronToggleTool，用于按名称启用或禁用已配置的 cron 定时任务。
禁用后任务仍保留在配置中，但调度器不会触发执行。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.services.cron import set_job_enabled
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class CronToggleToolInput(BaseModel):
    """Cron 任务启用/禁用切换工具的输入参数。

    Attributes:
        name: cron 任务名称
        enabled: True 启用，False 禁用
    """

    name: str = Field(description="Cron job name")
    enabled: bool = Field(description="True to enable, False to disable")


class CronToggleTool(BaseTool):
    """启用或禁用本地 cron 定时任务的工具。

    禁用后任务仍保留在配置中，但不会被调度器触发。
    """

    name = "cron_toggle"
    description = "Enable or disable a local cron job by name."
    input_model = CronToggleToolInput

    async def execute(
        self,
        arguments: CronToggleToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行 cron 任务启用/禁用切换。

        Args:
            arguments: 包含任务名称和启用状态的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            切换确认信息或任务不存在的错误
        """
        del context
        if not set_job_enabled(arguments.name, arguments.enabled):
            return ToolResult(
                output=f"Cron job not found: {arguments.name}",
                is_error=True,
            )
        state = "enabled" if arguments.enabled else "disabled"
        return ToolResult(output=f"Cron job '{arguments.name}' is now {state}")
