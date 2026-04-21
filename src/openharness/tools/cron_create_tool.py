"""本地 Cron 定时任务创建工具。

本模块提供 CronCreateTool，用于创建或替换本地 cron 风格的定时任务。
使用标准 5 字段 cron 表达式（分 时 日 月 周）定义调度计划。
创建的任务需通过 'oh cron start' 启动调度守护进程才会实际执行。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.services.cron import upsert_cron_job, validate_cron_expression
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class CronCreateToolInput(BaseModel):
    """Cron 任务创建工具的输入参数。

    Attributes:
        name: 唯一的 cron 任务名称
        schedule: Cron 调度表达式（如 '*/5 * * * *' 表示每 5 分钟）
        command: 触发时执行的 Shell 命令
        cwd: 可选的工作目录覆盖
        enabled: 任务是否启用，默认为 True
    """

    name: str = Field(description="Unique cron job name")
    schedule: str = Field(
        description=(
            "Cron schedule expression (e.g. '*/5 * * * *' for every 5 minutes, "
            "'0 9 * * 1-5' for weekdays at 9am)"
        ),
    )
    command: str = Field(description="Shell command to run when triggered")
    cwd: str | None = Field(default=None, description="Optional working directory override")
    enabled: bool = Field(default=True, description="Whether the job is active")


class CronCreateTool(BaseTool):
    """创建或替换本地 cron 定时任务的工具。

    使用标准 5 字段 cron 表达式定义调度计划。
    """

    name = "cron_create"
    description = (
        "Create or replace a local cron job with a standard cron expression. "
        "Use 'oh cron start' to run the scheduler daemon."
    )
    input_model = CronCreateToolInput

    async def execute(
        self,
        arguments: CronCreateToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行 cron 任务创建。

        首先验证 cron 表达式格式，然后创建或替换任务。

        Args:
            arguments: 包含任务名称、调度表达式和命令的输入参数
            context: 工具执行上下文

        Returns:
            创建成功的确认信息或表达式验证错误
        """
        if not validate_cron_expression(arguments.schedule):
            return ToolResult(
                output=(
                    f"Invalid cron expression: {arguments.schedule!r}\n"
                    "Use standard 5-field format: minute hour day month weekday\n"
                    "Examples: '*/5 * * * *' (every 5 min), '0 9 * * 1-5' (weekdays 9am)"
                ),
                is_error=True,
            )

        upsert_cron_job(
            {
                "name": arguments.name,
                "schedule": arguments.schedule,
                "command": arguments.command,
                "cwd": arguments.cwd or str(context.cwd),
                "enabled": arguments.enabled,
            }
        )
        status = "enabled" if arguments.enabled else "disabled"
        return ToolResult(
            output=f"Created cron job '{arguments.name}' [{arguments.schedule}] ({status})"
        )
