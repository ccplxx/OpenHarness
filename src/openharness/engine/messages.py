"""对话消息模型。

本模块定义了查询引擎所使用的对话消息数据结构，采用 Pydantic BaseModel 实现，
支持多种内容块（文本、图片、工具调用、工具结果）的统一建模与序列化。
这些模型是引擎与 LLM API 之间消息传递的核心抽象层，负责：

- 将用户输入与模型输出规范化为结构化的消息对象
- 在本地表示与 API 线格式（Anthropic SDK 格式）之间进行双向转换
- 对恢复的对话历史进行清洗，确保消息序列满足 API 的交替角色约束
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class TextBlock(BaseModel):
    """纯文本内容块。

    表示消息中的普通文本内容，是最基础的内容块类型，
    用于承载用户输入文本或模型生成的文字回复。
    """

    type: Literal["text"] = "text"
    text: str


class ImageBlock(BaseModel):
    """内联编码的图片内容块。

    用于多模态 LLM 的图片输入场景，将本地图片文件读取为 base64 编码后
    以内联方式嵌入消息内容，支持 PNG、JPEG、GIF、WebP 等常见图片格式。
    """

    type: Literal["image"] = "image"
    media_type: str
    data: str
    source_path: str = ""

    @classmethod
    def from_path(cls, path: str | Path) -> "ImageBlock":
        """从本地文件路径加载图片到 base64 编码的内容块。

        自动推断 MIME 类型，仅接受 image/* 类型的文件，
        否则抛出 ValueError。
        """
        resolved = Path(path).expanduser().resolve()
        media_type, _ = mimetypes.guess_type(str(resolved))
        if not media_type or not media_type.startswith("image/"):
            raise ValueError(f"Unsupported image attachment: {resolved}")
        payload = base64.b64encode(resolved.read_bytes()).decode("ascii")
        return cls(media_type=media_type, data=payload, source_path=str(resolved))


class ToolUseBlock(BaseModel):
    """工具调用请求块。

    表示模型在回复中请求执行某个命名工具的意图，包含工具名称、
    自动生成的调用 ID（用于与 ToolResultBlock 配对）以及工具输入参数。
    """

    type: Literal["tool_use"] = "tool_use"
    id: str = Field(default_factory=lambda: f"toolu_{uuid4().hex}")
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    """工具执行结果块。

    将工具执行的结果（或错误信息）回传给模型，通过 tool_use_id
    与对应的 ToolUseBlock 配对，is_error 标记用于区分正常结果与异常。
    """

    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock = Annotated[
    TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock,
    Field(discriminator="type"),
]
"""内容块联合类型。

通过 Pydantic 的判别联合（discriminated union）机制，根据 type 字段
自动反序列化为对应的内容块子类型，确保消息内容的类型安全解析。
"""


class ConversationMessage(BaseModel):
    """单条对话消息。

    表示对话历史中的一条消息，包含角色（user 或 assistant）和内容块列表。
    提供从纯文本/内容块构造、文本提取、工具调用提取、API 序列化、
    空消息检测等便捷方法，是引擎消息传递的核心数据结构。
    """

    role: Literal["user", "assistant"]
    content: list[ContentBlock] = Field(default_factory=list)

    @field_validator("content", mode="before")
    @classmethod
    def _normalize_content(cls, value: Any) -> list[Any]:
        """在内容块验证之前规范化遗留/空值载荷。

        将 None 值转换为空列表，兼容旧版消息格式中 content 为 null 的情况。
        """
        if value is None:
            return []
        return value

    @classmethod
    def from_user_text(cls, text: str) -> "ConversationMessage":
        """从原始文本构造用户消息。"""
        return cls(role="user", content=[TextBlock(text=text)])

    @classmethod
    def from_user_content(cls, content: list[ContentBlock]) -> "ConversationMessage":
        """从显式内容块列表构造用户消息。"""
        return cls(role="user", content=list(content))

    @property
    def text(self) -> str:
        """返回消息中所有文本块的拼接内容。"""
        return "".join(
            block.text for block in self.content if isinstance(block, TextBlock)
        )

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        """返回消息中包含的所有工具调用块。"""
        return [block for block in self.content if isinstance(block, ToolUseBlock)]

    def to_api_param(self) -> dict[str, Any]:
        """将消息转换为 Anthropic SDK 消息参数格式。

        对消息中的每个内容块调用 serialize_content_block 进行序列化，
        生成符合 API 线格式的字典结构。
        """
        return {
            "role": self.role,
            "content": [serialize_content_block(block) for block in self.content],
        }

    def is_effectively_empty(self) -> bool:
        """判断消息是否不携带任何有效内容。

        当所有文本块均为空白且不存在图片、工具调用、工具结果块时返回 True，
        用于过滤会话恢复后出现的空助手消息。
        """
        if self.content:
            for block in self.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    return False
                if isinstance(block, (ImageBlock, ToolUseBlock, ToolResultBlock)):
                    return False
        return True


def sanitize_conversation_messages(messages: list[ConversationMessage]) -> list[ConversationMessage]:
    """将恢复的对话历史规范化为 API 安全的消息序列。

    执行以下清洗操作：
    1. 丢弃遗留的空助手消息（is_effectively_empty 为 True 的 assistant 消息）
    2. 修剪末尾不完整的工具调用轮次——例如助手发出了 tool_use 请求
       但尚未收到匹配的 user tool_result 响应。这种断裂尾部在会话
       中途被中断时产生，若不处理会导致兼容 OpenAI 的 API 拒绝恢复后的对话。
    3. 移除与已删除 tool_use 不匹配的孤立 tool_result 块。
    """
    sanitized: list[ConversationMessage] = []
    pending_tool_use_ids: set[str] = set()
    pending_tool_use_index: int | None = None

    for message in messages:
        if message.role == "assistant" and message.is_effectively_empty():
            continue

        tool_uses = message.tool_uses if message.role == "assistant" else []
        tool_results = [
            block for block in message.content if isinstance(block, ToolResultBlock)
        ] if message.role == "user" else []

        matched_pending_tool_results = False
        if pending_tool_use_ids:
            result_ids = {block.tool_use_id for block in tool_results}
            if message.role != "user" or not pending_tool_use_ids.issubset(result_ids):
                if pending_tool_use_index is not None and pending_tool_use_index < len(sanitized):
                    sanitized.pop(pending_tool_use_index)
                pending_tool_use_ids = set()
                pending_tool_use_index = None
            else:
                matched_pending_tool_results = True
                pending_tool_use_ids = set()
                pending_tool_use_index = None

        if message.role == "user" and tool_results and not matched_pending_tool_results:
            content = [
                block for block in message.content if not isinstance(block, ToolResultBlock)
            ]
            if not content:
                continue
            message = ConversationMessage(role="user", content=content)

        sanitized.append(message)

        if tool_uses:
            pending_tool_use_ids = {block.id for block in tool_uses}
            pending_tool_use_index = len(sanitized) - 1

    if pending_tool_use_ids and pending_tool_use_index is not None and pending_tool_use_index < len(sanitized):
        sanitized.pop(pending_tool_use_index)

    return sanitized


def serialize_content_block(block: ContentBlock) -> dict[str, Any]:
    """将本地内容块转换为 API 线格式（provider wire format）。

    根据 block 的实际类型（TextBlock / ImageBlock / ToolUseBlock / ToolResultBlock）
    生成对应的结构化字典，用于 API 请求体的消息内容序列化。
    """
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}

    if isinstance(block, ImageBlock):
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": block.media_type,
                "data": block.data,
            },
        }

    if isinstance(block, ToolUseBlock):
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }

    return {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": block.content,
        "is_error": block.is_error,
    }


def assistant_message_from_api(raw_message: Any) -> ConversationMessage:
    """将 Anthropic SDK 原始消息对象转换为本地 ConversationMessage。

    遍历原始消息的 content 列表，将 text 和 tool_use 类型的块
    映射为对应的本地模型（TextBlock / ToolUseBlock），忽略其他类型。
    """
    content: list[ContentBlock] = []

    for raw_block in getattr(raw_message, "content", []):
        block_type = getattr(raw_block, "type", None)
        if block_type == "text":
            content.append(TextBlock(text=getattr(raw_block, "text", "")))
        elif block_type == "tool_use":
            content.append(
                ToolUseBlock(
                    id=getattr(raw_block, "id", f"toolu_{uuid4().hex}"),
                    name=getattr(raw_block, "name", ""),
                    input=dict(getattr(raw_block, "input", {}) or {}),
                )
            )

    return ConversationMessage(role="assistant", content=content)
