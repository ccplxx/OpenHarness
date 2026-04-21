"""休眠等待工具。

本模块提供 SleepTool，用于暂停工具执行一段短时间（最长 30 秒）。
常用于轮询场景中等待异步操作完成。
该工具为只读工具。
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class SleepToolInput(BaseModel):
    """休眠工具的输入参数。

    Attributes:
        seconds: 休眠时长（秒），范围 0-30，默认 1.0
    """

    seconds: float = Field(default=1.0, ge=0.0, le=30.0)


class SleepTool(BaseTool):
    """暂停工具执行一段短时间的工具。

    常用于轮询场景中等待异步操作完成。
    """

    name = "sleep"
    description = "Sleep for a short duration."
    input_model = SleepToolInput

    def is_read_only(self, arguments: SleepToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""

    async def execute(self, arguments: SleepToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行休眠等待。

        Args:
            arguments: 包含休眠时长的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            休眠完成确认信息
        """
        del context
        await asyncio.sleep(arguments.seconds)
        return ToolResult(output=f"Slept for {arguments.seconds} seconds")
