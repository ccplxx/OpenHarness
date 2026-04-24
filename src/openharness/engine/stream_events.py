"""查询引擎流式事件定义。

本模块定义了查询引擎在执行过程中通过 AsyncIterator 向外暴露的所有事件类型，
采用 frozen dataclass 实现不可变事件对象，保证事件在传播过程中的安全性。

事件类型包括：
- AssistantTextDelta：模型生成的增量文本片段
- AssistantTurnComplete：模型完成一个完整的回复轮次
- ToolExecutionStarted / ToolExecutionCompleted：工具执行的开始/完成通知
- ErrorEvent：需要展示给用户的错误信息
- StatusEvent：临时系统状态消息
- CompactProgressEvent：对话压缩进度的结构化事件

StreamEvent 为上述所有事件类型的联合类型，用于类型标注。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage


@dataclass(frozen=True)
class AssistantTextDelta:
    """增量助手文本事件。

    在模型流式输出过程中，每收到一个文本片段即产生此事件，
    用于实现实时打字效果的 UI 更新。
    """

    text: str


@dataclass(frozen=True)
class AssistantTurnComplete:
    """助手轮次完成事件。

    当模型完成一个完整的回复轮次（包括文本和可能的工具调用）时产生，
    携带完整的消息对象和本轮的 token 用量快照。
    """

    message: ConversationMessage  # role + content
    usage: UsageSnapshot  # input token + output token


@dataclass(frozen=True)
class ToolExecutionStarted:
    """工具执行开始事件。

    在引擎即将执行某个工具调用时产生，携带工具名称和输入参数，
    用于 UI 展示工具执行状态。
    """

    tool_name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True)
class ToolExecutionCompleted:
    """工具执行完成事件。

    当一个工具执行完毕时产生，携带工具名称、输出内容和错误标记，
    用于 UI 更新工具执行结果。
    """

    tool_name: str
    output: str
    is_error: bool = False


@dataclass(frozen=True)
class ErrorEvent:
    """错误事件，需要展示给用户。

    携带错误消息和可恢复标记。recoverable=True 表示用户可以重试，
    recoverable=False 表示不可恢复的致命错误。
    """

    message: str
    recoverable: bool = True


@dataclass(frozen=True)
class StatusEvent:
    """临时系统状态消息事件。

    用于向用户展示瞬态的状态更新，如重试提示、压缩进度等，
    不需要用户交互即可自动消失。
    """

    message: str


@dataclass(frozen=True)
class CompactProgressEvent:
    """对话压缩的结构化进度事件。

    用于报告自动/手动/响应式压缩的各阶段进度，包括：
    - hooks_start/end：钩子执行阶段
    - context_collapse_start/end：上下文折叠阶段
    - session_memory_start/end：会话记忆阶段
    - compact_start/retry/end/failed：压缩执行阶段

    trigger 字段标识触发来源（auto=自动、manual=手动、reactive=响应式）。
    """

    phase: Literal[
        "hooks_start",
        "context_collapse_start",
        "context_collapse_end",
        "session_memory_start",
        "session_memory_end",
        "compact_start",
        "compact_retry",
        "compact_end",
        "compact_failed",
    ]
    trigger: Literal["auto", "manual", "reactive"]
    message: str | None = None
    attempt: int | None = None
    checkpoint: str | None = None
    metadata: dict[str, Any] | None = None


StreamEvent = (
    AssistantTextDelta
    | AssistantTurnComplete
    | ToolExecutionStarted
    | ToolExecutionCompleted
    | ErrorEvent
    | StatusEvent
    | CompactProgressEvent
)
"""流式事件联合类型。

查询引擎通过 AsyncIterator 产出的所有事件类型的联合类型，
用于类型标注和模式匹配。
"""
