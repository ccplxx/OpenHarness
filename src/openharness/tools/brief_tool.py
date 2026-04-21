"""文本摘要缩短工具。

本模块提供 BriefTool，用于将长文本截断为指定最大字符数的简短版本。
当文本长度未超过限制时原样返回，超过时截断并添加省略号。
该工具为只读工具。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class BriefToolInput(BaseModel):
    """文本摘要工具的输入参数。

    Attributes:
        text: 需要缩短的文本
        max_chars: 最大字符数，范围 20-2000，默认 200
    """

    text: str = Field(description="Text to shorten")
    max_chars: int = Field(default=200, ge=20, le=2000)


class BriefTool(BaseTool):
    """将文本缩短为紧凑显示版本的工具。

    当文本长度超过 max_chars 时截断并添加省略号。
    """

    name = "brief"
    description = "Shorten a piece of text for compact display."
    input_model = BriefToolInput

    def is_read_only(self, arguments: BriefToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""
        del arguments
        return True

    async def execute(self, arguments: BriefToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行文本缩短操作。

        Args:
            arguments: 包含文本和最大字符数的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            缩短后的文本或原始文本（若未超过限制）
        """
        del context
        text = arguments.text.strip()
        if len(text) <= arguments.max_chars:
            return ToolResult(output=text)
        return ToolResult(output=text[: arguments.max_chars].rstrip() + "...")
