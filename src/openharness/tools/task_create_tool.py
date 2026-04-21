"""后台任务创建工具。

本模块提供 TaskCreateTool，用于创建后台任务。支持两种任务类型：
- local_bash：本地 Shell 命令任务，需提供 command 参数
- local_agent：本地代理任务，需提供 prompt 参数

创建的任务由 BackgroundTaskManager 管理，可通过 task 系列工具查询和控制。
"""

from __future__ import annotations

import os

from pydantic import BaseModel, Field

from openharness.tasks.manager import get_task_manager
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class TaskCreateToolInput(BaseModel):
    """后台任务创建工具的输入参数。

    Attributes:
        type: 任务类型：local_bash 或 local_agent，默认 local_bash
        description: 任务简短描述
        command: Shell 命令（local_bash 类型必需）
        prompt: 代理提示词（local_agent 类型必需）
        model: 可选的模型名称覆盖
    """

    type: str = Field(default="local_bash", description="Task type: local_bash or local_agent")
    description: str = Field(description="Short task description")
    command: str | None = Field(default=None, description="Shell command for local_bash")
    prompt: str | None = Field(default=None, description="Prompt for local_agent")
    model: str | None = Field(default=None)


class TaskCreateTool(BaseTool):
    """创建后台任务的工具。

    支持 Shell 命令任务和本地代理任务两种类型。
    """

    name = "task_create"
    description = "Create a background shell or local-agent task."
    input_model = TaskCreateToolInput

    async def execute(self, arguments: TaskCreateToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行后台任务创建。

        根据任务类型调用不同的 TaskManager 方法创建任务。

        Args:
            arguments: 包含任务类型、描述和命令/提示词的输入参数
            context: 工具执行上下文

        Returns:
            创建的任务 ID 和类型信息
        """
        manager = get_task_manager()
        if arguments.type == "local_bash":
            if not arguments.command:
                return ToolResult(output="command is required for local_bash tasks", is_error=True)
            task = await manager.create_shell_task(
                command=arguments.command,
                description=arguments.description,
                cwd=context.cwd,
            )
        elif arguments.type == "local_agent":
            if not arguments.prompt:
                return ToolResult(output="prompt is required for local_agent tasks", is_error=True)
            try:
                task = await manager.create_agent_task(
                    prompt=arguments.prompt,
                    description=arguments.description,
                    cwd=context.cwd,
                    model=arguments.model,
                    api_key=os.environ.get("ANTHROPIC_API_KEY"),
                )
            except ValueError as exc:
                return ToolResult(output=str(exc), is_error=True)
        else:
            return ToolResult(output=f"unsupported task type: {arguments.type}", is_error=True)

        return ToolResult(output=f"Created task {task.id} ({task.type})")
