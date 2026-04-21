"""本地 Cron 定时任务删除工具。

本模块提供 CronDeleteTool，用于按名称删除已配置的本地 cron 定时任务。
如果指定名称的任务不存在，返回错误信息。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.services.cron import delete_cron_job
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class CronDeleteToolInput(BaseModel):
    """Cron 任务删除工具的输入参数。

    Attributes:
        name: 要删除的 cron 任务名称
    """

    name: str = Field(description="Cron job name")


class CronDeleteTool(BaseTool):
    """删除本地 cron 定时任务的工具。

    按名称删除已配置的 cron 任务。
    """

    name = "cron_delete"
    description = "Delete a local cron-style job by name."
    input_model = CronDeleteToolInput

    async def execute(
        self,
        arguments: CronDeleteToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行 cron 任务删除。

        Args:
            arguments: 包含任务名称的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            删除确认信息或任务不存在的错误
        """
        del context
        if not delete_cron_job(arguments.name):
            return ToolResult(output=f"Cron job not found: {arguments.name}", is_error=True)
        return ToolResult(output=f"Deleted cron job {arguments.name}")
