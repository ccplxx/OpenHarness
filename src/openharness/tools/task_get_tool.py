"""后台任务详情查询工具。

本模块提供 TaskGetTool，用于根据 task_id 查询后台任务的详细状态信息。
该工具为只读工具。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.tasks.manager import get_task_manager
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class TaskGetToolInput(BaseModel):
    """任务详情查询工具的输入参数。

    Attributes:
        task_id: 任务标识符
    """

    task_id: str = Field(description="Task identifier")


class TaskGetTool(BaseTool):
    """返回后台任务详细状态的工具。"""

    name = "task_get"
    description = "Get details for a background task."
    input_model = TaskGetToolInput

    def is_read_only(self, arguments: TaskGetToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""

    async def execute(self, arguments: TaskGetToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行任务详情查询。

        Args:
            arguments: 包含任务 ID 的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            任务状态详情文本
        """
        del context
        task = get_task_manager().get_task(arguments.task_id)
        if task is None:
            return ToolResult(output=f"No task found with ID: {arguments.task_id}", is_error=True)
        return ToolResult(output=str(task))
