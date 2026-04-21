"""向交互用户提问的工具。

本模块提供 AskUserQuestionTool，用于在工具执行过程中向用户发送问题并等待回答。
通过 context.metadata 中的 ask_user_prompt 回调函数实现与用户的交互。
该工具为只读工具，不会修改任何状态。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


AskUserPrompt = Callable[[str], Awaitable[str]]
"""向用户提问并获取回答的异步回调类型。"""


class AskUserQuestionToolInput(BaseModel):
    """用户提问工具的输入参数。

    Attributes:
        question: 向用户提出的具体问题
    """

    question: str = Field(description="The exact question to ask the user")


class AskUserQuestionTool(BaseTool):
    """向交互用户提问并返回回答的工具。

    通过 context.metadata 中的 ask_user_prompt 回调函数实现与用户的交互。
    如果回调函数不可用，则返回错误信息。
    """

    name = "ask_user_question"
    description = "Ask the interactive user a follow-up question and return the answer."
    input_model = AskUserQuestionToolInput

    def is_read_only(self, arguments: AskUserQuestionToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""
        del arguments
        return True

    async def execute(
        self,
        arguments: AskUserQuestionToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行用户提问。

        从上下文元数据中获取 ask_user_prompt 回调，调用它向用户提问并获取回答。

        Args:
            arguments: 包含问题的输入参数
            context: 工具执行上下文

        Returns:
            包含用户回答的 ToolResult，若无回答则返回 "(no response)"
        """
        prompt = context.metadata.get("ask_user_prompt")
        if not callable(prompt):
            return ToolResult(
                output="ask_user_question is unavailable in this session",
                is_error=True,
            )
        answer = str(await prompt(arguments.question)).strip()
        if not answer:
            return ToolResult(output="(no response)")
        return ToolResult(output=answer)
