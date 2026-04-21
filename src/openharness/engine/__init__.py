"""引擎核心模块导出。

本模块定义了 openharness.engine 包的公共 API 导出列表，采用延迟导入（lazy import）策略，
通过 __getattr__ 实现按需加载，避免循环依赖并减少启动时间。导出的类型包括：

- 消息模型：ConversationMessage, TextBlock, ImageBlock, ToolUseBlock, ToolResultBlock
- 查询引擎：QueryEngine
- 流式事件：AssistantTextDelta, AssistantTurnComplete, ToolExecutionStarted, ToolExecutionCompleted
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from openharness.engine.messages import (
        ConversationMessage,
        ImageBlock,
        TextBlock,
        ToolResultBlock,
        ToolUseBlock,
    )
    from openharness.engine.query_engine import QueryEngine
    from openharness.engine.stream_events import (
        AssistantTextDelta,
        AssistantTurnComplete,
        ToolExecutionCompleted,
        ToolExecutionStarted,
    )

__all__ = [
    "AssistantTextDelta",
    "AssistantTurnComplete",
    "ConversationMessage",
    "ImageBlock",
    "QueryEngine",
    "TextBlock",
    "ToolExecutionCompleted",
    "ToolExecutionStarted",
    "ToolResultBlock",
    "ToolUseBlock",
]


def __getattr__(name: str):
    """延迟导入属性访问器。

    当外部代码首次访问本模块 __all__ 中列出的符号时，才从对应子模块执行真正的 import，
    从而避免包初始化时产生循环依赖和不必要的加载开销。
    """
    if name in {"ConversationMessage", "ImageBlock", "TextBlock", "ToolResultBlock", "ToolUseBlock"}:
        from openharness.engine.messages import (
            ConversationMessage,
            ImageBlock,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
        )

        return {
            "ConversationMessage": ConversationMessage,
            "ImageBlock": ImageBlock,
            "TextBlock": TextBlock,
            "ToolResultBlock": ToolResultBlock,
            "ToolUseBlock": ToolUseBlock,
        }[name]

    if name == "QueryEngine":
        from openharness.engine.query_engine import QueryEngine

        return QueryEngine

    if name in {
        "AssistantTextDelta",
        "AssistantTurnComplete",
        "ToolExecutionCompleted",
        "ToolExecutionStarted",
    }:
        from openharness.engine.stream_events import (
            AssistantTextDelta,
            AssistantTurnComplete,
            ToolExecutionCompleted,
            ToolExecutionStarted,
        )

        return {
            "AssistantTextDelta": AssistantTextDelta,
            "AssistantTurnComplete": AssistantTurnComplete,
            "ToolExecutionCompleted": ToolExecutionCompleted,
            "ToolExecutionStarted": ToolExecutionStarted,
        }[name]

    raise AttributeError(name)
