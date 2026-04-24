"""OpenAI 兼容 API 客户端模块。

本模块实现了 OpenHarness 与 OpenAI 兼容 API 的交互客户端，适用于
阿里巴巴 DashScope、GitHub Models、DeepSeek、Moonshot 等基于 OpenAI
API 格式的提供商。

核心功能包括：

1. **消息格式转换**：将 Anthropic 风格的对话消息转换为 OpenAI Chat 格式，
   处理系统提示、工具调用/结果、多模态内容等差异。
2. **工具定义转换**：将 Anthropic 格式的工具定义转换为 OpenAI function-calling 格式。
3. **流式传输**：通过 OpenAI SDK 的流式接口获取增量文本和工具调用。
4. **推理模型支持**：处理 GPT-5、o1/o3/o4 等推理模型的特殊参数（``max_completion_tokens``）
   和推理内容（``reasoning_content``）。
5. **自动重试**：内置指数退避重试逻辑，处理可恢复的瞬时错误。
6. **URL 规范化**：自动处理自定义 Base URL 的路径规范化。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator
from urllib.parse import urlsplit, urlunsplit

from openai import AsyncOpenAI

from openharness.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiRetryEvent,
    ApiStreamEvent,
    ApiTextDeltaEvent,
)
from openharness.api.errors import (
    AuthenticationFailure,
    OpenHarnessApiError,
    RateLimitFailure,
    RequestFailure,
)
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import (
    ConversationMessage,
    ContentBlock,
    ImageBlock,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

log = logging.getLogger(__name__)

MAX_RETRIES = 3
BASE_DELAY = 1.0
MAX_DELAY = 30.0
_MAX_COMPLETION_TOKEN_MODEL_PREFIXES = ("gpt-5", "o1", "o3", "o4")


def _token_limit_param_for_model(model: str, max_tokens: int) -> dict[str, int]:
    """根据目标 OpenAI 模型返回正确的令牌限制参数。

    GPT-5 及当前的推理模型系列（o1、o3、o4）拒绝 ``max_tokens`` 参数，
    要求使用 ``max_completion_tokens`` 代替。其他模型使用标准的 ``max_tokens``。

    Args:
        model: 目标模型标识字符串。
        max_tokens: 最大令牌数。

    Returns:
        包含正确参数名的字典（``{"max_tokens": ...}`` 或 ``{"max_completion_tokens": ...}``）。
    """
    normalized = model.strip().lower()
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    if normalized.startswith(_MAX_COMPLETION_TOKEN_MODEL_PREFIXES):
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _convert_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 Anthropic 格式的工具定义转换为 OpenAI function-calling 格式。

    转换规则：
    - Anthropic 格式：``{"name": "...", "description": "...", "input_schema": {...}}``
    - OpenAI 格式：``{"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}``
    - ``input_schema`` 字段重命名为 ``parameters``。

    Args:
        tools: Anthropic 格式的工具定义列表。

    Returns:
        OpenAI 格式的工具定义列表。
    """
    result = []
    for tool in tools:
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}),
            },
        })
    return result


def _convert_messages_to_openai(
    messages: list[ConversationMessage],
    system_prompt: str | None,
) -> list[dict[str, Any]]:
    """将 Anthropic 风格的对话消息转换为 OpenAI Chat 格式。

    处理两种格式之间的关键差异：
    - Anthropic 中系统提示是独立参数，OpenAI 中是 ``role="system"`` 的消息。
    - Anthropic 中工具调用/结果是内容块，OpenAI 中工具调用在助手消息的
      ``tool_calls`` 字段，工具结果是独立的 ``role="tool"`` 消息。

    Args:
        messages: Anthropic 风格的对话消息列表。
        system_prompt: 系统提示文本，若为 ``None`` 则不添加系统消息。

    Returns:
        OpenAI Chat 格式的消息列表。
    """
    openai_messages: list[dict[str, Any]] = []

    if system_prompt:
        openai_messages.append({"role": "system", "content": system_prompt})

    for msg in messages:
        if msg.role == "assistant":
            openai_msg = _convert_assistant_message(msg)
            openai_messages.append(openai_msg)
        elif msg.role == "user":
            # User messages may contain text or tool_result blocks
            tool_results = [b for b in msg.content if isinstance(b, ToolResultBlock)]
            user_blocks = [b for b in msg.content if isinstance(b, (TextBlock, ImageBlock))]

            if tool_results:
                # Each tool result becomes a separate message with role="tool"
                for tr in tool_results:
                    openai_messages.append({
                        "role": "tool",
                        "tool_call_id": tr.tool_use_id,
                        "content": tr.content,
                    })
            if user_blocks:
                content = _convert_user_content_to_openai(user_blocks)
                if isinstance(content, str):
                    if content.strip():
                        openai_messages.append({"role": "user", "content": content})
                elif content:
                    openai_messages.append({"role": "user", "content": content})
            if not tool_results and not user_blocks:
                # Empty user message (shouldn't happen, but handle gracefully)
                openai_messages.append({"role": "user", "content": ""})

    return openai_messages


def _convert_user_content_to_openai(blocks: list[ContentBlock]) -> str | list[dict[str, Any]]:
    """将用户文本/图片内容块转换为 OpenAI Chat 内容格式。

    若内容中包含图片，返回多模态内容列表（含 ``text`` 和 ``image_url`` 类型）；
    若仅包含文本，返回纯文本字符串。

    Args:
        blocks: 内容块列表（TextBlock 或 ImageBlock）。

    Returns:
        纯文本字符串或多模态内容列表。
    """
    has_image = any(isinstance(block, ImageBlock) for block in blocks)
    if not has_image:
        return "".join(block.text for block in blocks if isinstance(block, TextBlock))

    content: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, TextBlock) and block.text:
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{block.media_type};base64,{block.data}",
                },
            })
    return content


def _convert_assistant_message(msg: ConversationMessage) -> dict[str, Any]:
    """将助手对话消息转换为 OpenAI 格式。

    处理文本内容、工具调用和推理内容。对于支持思维模型的提供商
    （如 Kimi k2.5），每个包含工具调用的助手消息都需要
    ``reasoning_content`` 字段。原始推理文本在流式解析时
    存储在 ``msg._reasoning`` 中，此处进行回放。

    Args:
        msg: 助手角色的对话消息对象。

    Returns:
        OpenAI 格式的助手消息字典。
    """
    text_parts = [b.text for b in msg.content if isinstance(b, TextBlock)]
    tool_uses = [b for b in msg.content if isinstance(b, ToolUseBlock)]

    openai_msg: dict[str, Any] = {"role": "assistant"}

    content = "".join(text_parts)
    openai_msg["content"] = content if content else None

    # Replay reasoning_content for thinking models (stored by streaming parser)
    reasoning = getattr(msg, "_reasoning", None)
    if reasoning:
        openai_msg["reasoning_content"] = reasoning
    elif tool_uses:
        # Thinking models require this field even if empty
        openai_msg["reasoning_content"] = ""

    if tool_uses:
        openai_msg["tool_calls"] = [
            {
                "id": tu.id,
                "type": "function",
                "function": {
                    "name": tu.name,
                    "arguments": json.dumps(tu.input),
                },
            }
            for tu in tool_uses
        ]

    return openai_msg


def _parse_assistant_response(response: Any) -> ConversationMessage:
    """将 OpenAI ChatCompletion 响应解析为对话消息对象。

    提取响应中第一个选择的消息内容，包括文本和工具调用。
    工具调用的参数从 JSON 字符串解析为字典。

    Args:
        response: OpenAI ChatCompletion 响应对象。

    Returns:
        包含文本和工具调用内容块的助手对话消息。
    """
    choice = response.choices[0]
    message = choice.message
    content: list[ContentBlock] = []

    if message.content:
        content.append(TextBlock(text=message.content))

    if message.tool_calls:
        for tc in message.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append(ToolUseBlock(
                id=tc.id,
                name=tc.function.name,
                input=args,
            ))

    return ConversationMessage(role="assistant", content=content)


def _normalize_openai_base_url(base_url: str | None) -> str | None:
    """规范化自定义的 OpenAI 兼容 Base URL。

    处理以下情况：
    - 空值或空白字符串返回 ``None``。
    - 无协议或无主机的 URL 保持原样（去除尾部斜杠）。
    - 有效 URL 去除路径部分的尾部斜杠。
    - 若路径为空，自动补充 ``/v1`` 默认路径。

    Args:
        base_url: 原始的 Base URL 字符串。

    Returns:
        规范化后的 Base URL，若输入无效则返回 ``None``。
    """
    if not base_url:
        return None
    trimmed = base_url.strip()
    if not trimmed:
        return None
    parts = urlsplit(trimmed)
    if not parts.scheme or not parts.netloc:
        return trimmed.rstrip("/")
    path = parts.path.rstrip("/")
    if not path:
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


class OpenAICompatibleClient:
    """OpenAI 兼容 API 客户端（适用于 DashScope、GitHub Models 等）。

    实现与 :class:`AnthropicApiClient` 相同的 :class:`SupportsStreamingMessages`
    协议，因此可在代理循环中作为替代品使用。支持流式传输、工具调用、
    推理内容和自动重试。
    """

    def __init__(self, api_key: str, *, base_url: str | None = None, timeout: float | None = None) -> None:
        """初始化 OpenAI 兼容 API 客户端。

        Args:
            api_key: API 密钥。
            base_url: 自定义 API 端点 URL，若为 ``None`` 使用官方默认地址。
            timeout: 请求超时秒数，若为 ``None`` 使用 SDK 默认值。
        """
        kwargs: dict[str, Any] = {"api_key": api_key}
        normalized_base_url = _normalize_openai_base_url(base_url)
        if normalized_base_url:
            kwargs["base_url"] = normalized_base_url
        if timeout is not None:
            kwargs["timeout"] = timeout
        self._client = AsyncOpenAI(**kwargs)

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """以流式方式发送消息请求，返回文本增量和最终消息。

        接口与 Anthropic 客户端一致，实现 :class:`SupportsStreamingMessages` 协议。
        内置指数退避重试机制，自动处理可恢复的瞬时错误。

        Args:
            request: API 消息请求对象。

        Returns:
            异步迭代器，依次产生文本增量、重试或消息完成事件。

        Raises:
            AuthenticationFailure: 认证失败。
            RateLimitFailure: 速率限制且重试次数已耗尽。
            RequestFailure: 其他不可恢复的请求错误。
        """
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                async for event in self._stream_once(request):
                    yield event
                return
            except OpenHarnessApiError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= MAX_RETRIES or not self._is_retryable(exc):
                    raise self._translate_error(exc) from exc

                delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                log.warning(
                    "OpenAI API request failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, MAX_RETRIES + 1, delay, exc,
                )
                yield ApiRetryEvent(
                    message=str(exc),
                    attempt=attempt + 1,
                    max_attempts=MAX_RETRIES + 1,
                    delay_seconds=delay,
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            raise self._translate_error(last_error) from last_error

    async def _stream_once(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """单次 OpenAI 流式聊天补全尝试。

        将请求参数转换为 OpenAI 格式，调用 SDK 的流式接口，
        实时收集文本增量、推理内容和工具调用，最终汇总为
        完整的消息完成事件。对于推理模型，将推理内容暂存在
        消息的 ``_reasoning`` 属性中，供后续消息转换时回放。

        Args:
            request: API 消息请求对象。

        Returns:
            异步迭代器，产生文本增量和消息完成事件。
        """
        openai_messages = _convert_messages_to_openai(request.messages, request.system_prompt)
        openai_tools = _convert_tools_to_openai(request.tools) if request.tools else None

        params: dict[str, Any] = {
            "model": request.model,
            "messages": openai_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        params.update(_token_limit_param_for_model(request.model, request.max_tokens))
        if openai_tools:
            params["tools"] = openai_tools
            # Some providers (Kimi) error on empty reasoning_content in
            # tool-call follow-ups.  Omit the entire stream_options key if
            # tools are present – avoids triggering model-side thinking mode
            # that requires reasoning_content on every assistant message.
            params.pop("stream_options", None)

        # Collect full response while streaming text deltas
        collected_content = ""
        collected_reasoning = ""
        collected_tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        usage_data: dict[str, int] = {}

        stream = await self._client.chat.completions.create(**params)
        async for chunk in stream:
            if not chunk.choices:
                # Usage-only chunk (some providers send this at the end)
                if chunk.usage:
                    usage_data = {
                        "input_tokens": chunk.usage.prompt_tokens or 0,
                        "output_tokens": chunk.usage.completion_tokens or 0,
                    }
                continue

            delta = chunk.choices[0].delta
            chunk_finish = chunk.choices[0].finish_reason

            if chunk_finish:
                finish_reason = chunk_finish

            # Accumulate reasoning_content from thinking models (not shown to user)
            reasoning_piece = getattr(delta, "reasoning_content", None) or ""
            if reasoning_piece:
                collected_reasoning += reasoning_piece

            # Stream text content to user
            if delta.content:
                collected_content += delta.content
                yield ApiTextDeltaEvent(text=delta.content)

            # Accumulate tool calls
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in collected_tool_calls:
                        collected_tool_calls[idx] = {
                            "id": tc_delta.id or "",
                            "name": "",
                            "arguments": "",
                        }
                    entry = collected_tool_calls[idx]
                    if tc_delta.id:
                        entry["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            entry["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            entry["arguments"] += tc_delta.function.arguments

            # Usage in chunk (if provider sends it)
            if chunk.usage:
                usage_data = {
                    "input_tokens": chunk.usage.prompt_tokens or 0,
                    "output_tokens": chunk.usage.completion_tokens or 0,
                }

        # Build the final ConversationMessage
        content: list[ContentBlock] = []
        if collected_content:
            content.append(TextBlock(text=collected_content))

        for _idx in sorted(collected_tool_calls.keys()):
            tc = collected_tool_calls[_idx]
            # Skip phantom/empty tool calls that some providers send
            if not tc["name"]:
                continue
            try:
                args = json.loads(tc["arguments"])
            except (json.JSONDecodeError, TypeError):
                args = {}
            content.append(ToolUseBlock(
                id=tc["id"],
                name=tc["name"],
                input=args,
            ))

        final_message = ConversationMessage(role="assistant", content=content)

        # Stash reasoning for thinking models so _convert_assistant_message
        # can replay it when the message is sent back to the API
        if collected_reasoning:
            final_message._reasoning = collected_reasoning  # type: ignore[attr-defined]

        yield ApiMessageCompleteEvent(
            message=final_message,
            usage=UsageSnapshot(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
            ),
            stop_reason=finish_reason,
        )

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """判断异常是否为可重试的瞬时错误。

        可重试的错误包括：
        - HTTP 状态码为 429、500、502、503 的错误。
        - Python 标准的网络错误（ConnectionError、TimeoutError、OSError）。

        Args:
            exc: 待检查的异常对象。

        Returns:
            若异常可安全重试返回 ``True``，否则返回 ``False``。
        """
        status = getattr(exc, "status_code", None)
        if status and status in {429, 500, 502, 503}:
            return True
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return True
        return False

    @staticmethod
    def _translate_error(exc: Exception) -> OpenHarnessApiError:
        """将异常转换为 OpenHarness 统一的错误类型。

        - 401/403 → :class:`AuthenticationFailure`
        - 429 → :class:`RateLimitFailure`
        - 其他 → :class:`RequestFailure`

        Args:
            exc: 待转换的异常对象。

        Returns:
            对应的 OpenHarness API 错误实例。
        """
        status = getattr(exc, "status_code", None)
        msg = str(exc)
        if status == 401 or status == 403:
            return AuthenticationFailure(msg)
        if status == 429:
            return RateLimitFailure(msg)
        return RequestFailure(msg)
