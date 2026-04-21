"""后台任务元数据更新工具。

本模块提供 TaskUpdateTool，用于更新后台任务的元数据信息以跟踪进度。
支持更新：
- description：任务描述
- progress：进度百分比（0-100）
- status_note：简短的状态说明
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.tasks.manager import get_task_manager
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class TaskUpdateToolInput(BaseModel):
    """任务元数据更新工具的输入参数。

    Attributes:
        task_id: 任务标识符
        description: 更新的任务描述
        progress: 进度百分比（0-100）
        status_note: 简短的任务状态说明
    """

    task_id: str = Field(description="Task identifier")
    description: str | None = Field(default=None, description="Updated task description")
    progress: int | None = Field(default=None, ge=0, le=100, description="Progress percentage")
    status_note: str | None = Field(default=None, description="Short human-readable task note")


class TaskUpdateTool(BaseTool):
    """更新后台任务元数据以跟踪进度的工具。

    支持更新描述、进度百分比和状态说明。
    """

    name = "task_update"
    description = "Update a task description, progress, or status note."
    input_model = TaskUpdateToolInput

    async def execute(
        self,
        arguments: TaskUpdateToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行任务元数据更新。

        Args:
            arguments: 包含任务 ID 和更新字段的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            更新确认信息，包含已更新的字段
        """
        del context
        try:
            task = get_task_manager().update_task(
                arguments.task_id,
                description=arguments.description,
                progress=arguments.progress,
                status_note=arguments.status_note,
            )
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)

        parts = [f"Updated task {task.id}"]
        if arguments.description:
            parts.append(f"description={task.description}")
        if arguments.progress is not None:
            parts.append(f"progress={task.metadata.get('progress', '')}%")
        if arguments.status_note:
            parts.append(f"note={task.metadata.get('status_note', '')}")
        return ToolResult(output=" ".join(parts))
