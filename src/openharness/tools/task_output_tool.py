"""后台任务输出读取工具。

本模块提供 TaskOutputTool，用于读取后台任务的输出日志。
支持通过 max_bytes 参数控制读取的最大字节数（默认 12000）。
该工具为只读工具。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.tasks.manager import get_task_manager
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class TaskOutputToolInput(BaseModel):
    """任务输出读取工具的输入参数。

    Attributes:
        task_id: 任务标识符
        max_bytes: 最大读取字节数，范围 1-100000，默认 12000
    """

    task_id: str = Field(description="Task identifier")
    max_bytes: int = Field(default=12000, ge=1, le=100000)


class TaskOutputTool(BaseTool):
    """读取后台任务输出日志的工具。"""

    name = "task_output"
    description = "Read the output log for a background task."
    input_model = TaskOutputToolInput

    def is_read_only(self, arguments: TaskOutputToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""

    async def execute(self, arguments: TaskOutputToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行任务输出读取。

        Args:
            arguments: 包含任务 ID 和最大字节数的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            任务输出文本或 "(no output)"
        """
        del context
        try:
            output = get_task_manager().read_task_output(arguments.task_id, max_bytes=arguments.max_bytes)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=output or "(no output)")
