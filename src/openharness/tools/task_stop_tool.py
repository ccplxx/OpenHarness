"""后台任务停止工具。

本模块提供 TaskStopTool，用于停止正在运行的后台任务。
通过 TaskManager 发送停止信号终止任务进程。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.tasks.manager import get_task_manager
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class TaskStopToolInput(BaseModel):
    """任务停止工具的输入参数。

    Attributes:
        task_id: 任务标识符
    """

    task_id: str = Field(description="Task identifier")


class TaskStopTool(BaseTool):
    """停止后台任务的工具。

    通过 TaskManager 发送停止信号终止任务。
    """

    name = "task_stop"
    description = "Stop a background task."
    input_model = TaskStopToolInput

    async def execute(self, arguments: TaskStopToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行任务停止操作。

        Args:
            arguments: 包含任务 ID 的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            停止确认信息
        """
        del context
        try:
            task = await get_task_manager().stop_task(arguments.task_id)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"Stopped task {task.id}")
