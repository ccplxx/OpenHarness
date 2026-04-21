"""后台任务列表工具。

本模块提供 TaskListTool，用于列出所有后台任务。
支持通过 status 参数过滤特定状态的任务。
输出格式为：任务ID 任务类型 任务状态 任务描述。
该工具为只读工具。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.tasks.manager import get_task_manager
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class TaskListToolInput(BaseModel):
    """任务列表工具的输入参数。

    Attributes:
        status: 可选的状态过滤器
    """

    status: str | None = Field(default=None, description="Optional status filter")


class TaskListTool(BaseTool):
    """列出后台任务的工具。

    支持按状态过滤，输出格式为 任务ID 任务类型 任务状态 任务描述。
    """

    name = "task_list"
    description = "List background tasks."
    input_model = TaskListToolInput

    def is_read_only(self, arguments: TaskListToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""

    async def execute(self, arguments: TaskListToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行任务列表查询。

        Args:
            arguments: 包含可选状态过滤器的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            任务列表文本或 "(no tasks)"
        """
        del context
        tasks = get_task_manager().list_tasks(status=arguments.status)  # type: ignore[arg-type]
        if not tasks:
            return ToolResult(output="(no tasks)")
        return ToolResult(
            output="\n".join(f"{task.id} {task.type} {task.status} {task.description}" for task in tasks)
        )
