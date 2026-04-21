"""本地 Cron 定时任务列表工具。

本模块提供 CronListTool，用于列出所有已配置的本地 cron 定时任务。
展示信息包括调度器运行状态、每个任务的启用状态、调度表达式、
执行命令、上次运行时间与状态、下次运行时间等。
该工具为只读工具。
"""

from __future__ import annotations

from pydantic import BaseModel

from openharness.services.cron import load_cron_jobs
from openharness.services.cron_scheduler import is_scheduler_running
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class CronListToolInput(BaseModel):
    """Cron 任务列表工具的输入参数（无额外参数）。"""


class CronListTool(BaseTool):
    """列出本地 cron 定时任务的工具。

    展示调度器状态和所有任务的详细信息。
    """

    name = "cron_list"
    description = "List configured local cron jobs with schedule, status, and next run time."
    input_model = CronListToolInput

    def is_read_only(self, arguments: CronListToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""

    async def execute(
        self,
        arguments: CronListToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行 cron 任务列表查询。

        Args:
            arguments: 输入参数（无额外参数）
            context: 工具执行上下文（未使用）

        Returns:
            调度器状态和所有任务详情的格式化文本
        """
        del arguments, context
        jobs = load_cron_jobs()
        if not jobs:
            return ToolResult(output="No cron jobs configured.")

        scheduler = "running" if is_scheduler_running() else "stopped"
        lines = [f"Scheduler: {scheduler}", ""]

        for job in jobs:
            enabled = "on" if job.get("enabled", True) else "off"
            last_run = job.get("last_run", "never")
            if last_run != "never":
                last_run = last_run[:19]
            next_run = job.get("next_run", "n/a")
            if next_run != "n/a":
                next_run = next_run[:19]
            last_status = job.get("last_status", "")
            status_str = f" ({last_status})" if last_status else ""
            lines.append(
                f"[{enabled}] {job['name']}  {job.get('schedule', '?')}\n"
                f"     cmd: {job['command']}\n"
                f"     last: {last_run}{status_str}  next: {next_run}"
            )
        return ToolResult(output="\n".join(lines))
