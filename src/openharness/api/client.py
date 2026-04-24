"""Anthropic API 客户端封装模块，提供带重试逻辑的流式消息传输。

本模块实现了 OpenHarness 与 Anthropic API 交互的核心客户端，包含以下组件：

1. **数据类**：定义 API 消息请求、流式事件（文本增量、消息完成、重试事件）等数据结构。
2. **协议接口**：:class:`SupportsStreamingMessages` 定义了流式消息传输的统一协议，
   所有 API 客户端（Anthropic、OpenAI、Codex、Copilot）均实现此协议。
3. **重试机制**：内置指数退避重试逻辑，自动处理可恢复的瞬时错误（429、500、502、503、529），
   支持 Retry-After 响应头。
4. **OAuth 支持**：支持 Claude OAuth 订阅认证，包括 Beta 功能标志、计费归因头和
   令牌自动刷新。
5. **错误转换**：将 Anthropic SDK 错误转换为 OpenHarness 统一的错误类型。

该客户端是 OpenHarness 查询引擎与 Anthropic 服务之间的桥梁，
通过 ``stream_message()`` 方法以异步迭代器形式提供流式响应。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Protocol

from anthropic import APIError, APIStatusError, AsyncAnthropic

from openharness.api.errors import (
    AuthenticationFailure,
    OpenHarnessApiError,
    RateLimitFailure,
    RequestFailure,
)
from openharness.auth.external import (
    claude_attribution_header,
    claude_oauth_betas,
    claude_oauth_headers,
    get_claude_code_session_id,
)
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, assistant_message_from_api

log = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 3
BASE_DELAY = 1.0  # seconds
MAX_DELAY = 30.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}
OAUTH_BETA_HEADER = "oauth-2025-04-20"


@dataclass(frozen=True)
class ApiMessageRequest:
    """模型调用的输入参数数据类。

    封装了发送给 LLM API 的完整请求参数，包括模型标识、对话消息列表、
    系统提示、最大令牌数和工具定义。

    Attributes:
        model: 目标模型标识（如 ``"claude-sonnet-4-20250514"``）。
        messages: 对话消息列表，包含完整的对话历史。
        system_prompt: 系统提示文本，引导模型行为，默认为 ``None``。
        max_tokens: 最大输出令牌数，默认为 4096。
        tools: 工具定义列表（Anthropic 格式），默认为空列表。
    """

    model: str
    messages: list[ConversationMessage]
    system_prompt: str | None = None
    max_tokens: int = 4096
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ApiTextDeltaEvent:
    """模型产生的增量文本事件数据类。

    在流式传输过程中，每当模型生成一段新文本时产生此事件，
    用于实时向用户展示模型的输出进度。

    Attributes:
        text: 本次增量产生的文本片段。
    """

    text: str


@dataclass(frozen=True)
class ApiMessageCompleteEvent:
    """流式传输的终止事件数据类，包含完整的助手消息。

    当模型完成响应生成时产生此事件，包含完整的助手消息内容、
    令牌用量统计和停止原因。

    Attributes:
        message: 完整的助手对话消息对象，包含文本和工具调用。
        usage: 本次调用的令牌用量快照。
        stop_reason: 停止原因（如 ``"end_turn"``、``"tool_use"``、``"max_tokens"``），
            若为 ``None`` 表示未知原因。
    """

    message: ConversationMessage
    usage: UsageSnapshot  # 当前轮的输入输出token数
    stop_reason: str | None = None


@dataclass(frozen=True)
class ApiRetryEvent:
    """可恢复的上游失败事件数据类，表示即将自动重试。

    当 API 请求遇到可恢复的瞬时错误（如速率限制或服务端临时故障）时，
    在自动重试前产生此事件，通知上层当前的重试状态。

    Attributes:
        message: 错误描述信息。
        attempt: 当前重试次数（从 1 开始）。
        max_attempts: 最大重试次数。
        delay_seconds: 重试前的等待秒数。
    """

    message: str
    attempt: int
    max_attempts: int
    delay_seconds: float

# 流式、完成、重试等event
ApiStreamEvent = ApiTextDeltaEvent | ApiMessageCompleteEvent | ApiRetryEvent


class SupportsStreamingMessages(Protocol):
    """流式消息传输协议，用于查询引擎和测试中。

    所有 API 客户端（Anthropic、OpenAI、Codex、Copilot）均需实现此协议，
    确保查询引擎可以统一地调用不同提供商的流式消息接口。
    """

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """以异步迭代器形式产生请求的流式事件。

        Args:
            request: API 消息请求对象。

        Returns:
            异步迭代器，依次产生文本增量、重试或消息完成事件。
        """


def _is_retryable(exc: Exception) -> bool:
    """判断异常是否为可重试的瞬时错误。

    可重试的错误包括：
    - HTTP 状态码为 429（速率限制）、500、502、503、529 的 API 错误。
    - Anthropic SDK 的网络级错误（如连接中断）。
    - Python 标准的网络错误（ConnectionError、TimeoutError、OSError）。

    Args:
        exc: 待检查的异常对象。

    Returns:
        若异常可安全重试返回 ``True``，否则返回 ``False``。
    """
    if isinstance(exc, APIStatusError):
        return exc.status_code in RETRYABLE_STATUS_CODES
    if isinstance(exc, APIError):
        return True  # Network errors are retryable
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    return False


def _get_retry_delay(attempt: int, exc: Exception | None = None) -> float:
    """计算带指数退避和随机抖动的重试延迟。

    使用指数退避算法（基数 × 2^尝试次数）计算延迟时间，
    并添加 0~25% 的随机抖动以避免惊群效应。对于带有 Retry-After
    响应头的 429 错误，优先使用服务器建议的等待时间。

    Args:
        attempt: 当前重试次数（从 0 开始）。
        exc: 触发重试的异常，可能包含 Retry-After 头信息。

    Returns:
        重试前的等待秒数，不超过 :data:`MAX_DELAY` 上限。
    """
    import random

    # Check for Retry-After header
    if isinstance(exc, APIStatusError):
        retry_after = getattr(exc, "headers", {})
        if hasattr(retry_after, "get"):
            val = retry_after.get("retry-after")
            if val:
                try:
                    return min(float(val), MAX_DELAY)
                except (ValueError, TypeError):
                    pass

    delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
    jitter = random.uniform(0, delay * 0.25)
    return delay + jitter


class AnthropicApiClient:
    """Anthropic 异步 SDK 的轻量级封装，内置重试逻辑和 Claude OAuth 支持。

    该客户端实现了 :class:`SupportsStreamingMessages` 协议，通过
    ``stream_message()`` 方法以异步迭代器形式提供流式响应。
    内置指数退避重试机制，自动处理可恢复的瞬时错误。
    支持 Claude OAuth 订阅认证，包括 Beta 功能标志、计费归因头和
    令牌自动刷新。
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        auth_token: str | None = None,
        base_url: str | None = None,
        claude_oauth: bool = False,
        auth_token_resolver: Callable[[], str] | None = None,
    ) -> None:
        """初始化 Anthropic API 客户端。

        Args:
            api_key: Anthropic API 密钥，用于直接 API Key 认证。
            auth_token: OAuth 访问令牌，用于订阅认证。
            base_url: 自定义 API 端点 URL，若为 ``None`` 使用官方默认地址。
            claude_oauth: 是否启用 Claude OAuth 订阅模式，启用后会添加
                Beta 功能标志和计费归因头。
            auth_token_resolver: 令牌刷新回调函数，用于在请求前获取最新的
                OAuth 访问令牌。仅在 ``claude_oauth=True`` 时生效。
        """
        self._api_key = api_key
        self._auth_token = auth_token
        self._base_url = base_url
        self._claude_oauth = claude_oauth
        self._auth_token_resolver = auth_token_resolver
        self._session_id = get_claude_code_session_id() if claude_oauth else ""
        self._client = self._create_client()

    def _create_client(self) -> AsyncAnthropic:
        """创建 Anthropic 异步 SDK 客户端实例。

        根据初始化参数配置 API Key、认证令牌、自定义请求头和 Base URL。
        当启用 Claude OAuth 模式时，自动添加 OAuth 相关的请求头。

        Returns:
            配置完成的 :class:`AsyncAnthropic` 客户端实例。
        """
        kwargs: dict[str, Any] = {}
        if self._api_key:
            kwargs["api_key"] = self._api_key
        if self._auth_token:
            kwargs["auth_token"] = self._auth_token
            kwargs["default_headers"] = (
                claude_oauth_headers()
                if self._claude_oauth
                else {"anthropic-beta": OAUTH_BETA_HEADER}
            )
        if self._base_url:
            kwargs["base_url"] = self._base_url
        return AsyncAnthropic(**kwargs)

    def _refresh_client_auth(self) -> None:
        """刷新 OAuth 认证令牌并重建客户端。

        仅在 Claude OAuth 模式且提供了令牌刷新回调时生效。
        通过回调获取最新令牌，若与当前令牌不同则重建底层 SDK 客户端，
        确保后续请求使用最新的认证信息。
        """
        if not self._claude_oauth or self._auth_token_resolver is None:
            return
        next_token = self._auth_token_resolver()  # 令牌刷新回调函数，用于在请求前获取最新的令牌
        # 令牌刷新
        if next_token and next_token != self._auth_token:
            self._auth_token = next_token
            self._client = self._create_client()

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """以流式方式发送消息请求，返回文本增量和最终的助手消息。

        实现带自动重试的流式消息传输。对于可恢复的瞬时错误，使用指数退避
        策略自动重试，并在重试前产生 :class:`ApiRetryEvent` 事件通知上层。
        认证错误不会被重试，直接抛出 :class:`AuthenticationFailure`。

        Args:
            request: API 消息请求对象。

        Returns:
            异步迭代器，依次产生 :class:`ApiTextDeltaEvent`、
            :class:`ApiRetryEvent` 或 :class:`ApiMessageCompleteEvent` 事件。

        Raises:
            AuthenticationFailure: 认证失败。
            RateLimitFailure: 速率限制且重试次数已耗尽。
            RequestFailure: 其他不可恢复的请求错误。
        """
        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                self._refresh_client_auth()
                async for event in self._stream_once(request):
                    yield event
                return  # Success
            except OpenHarnessApiError:
                raise  # Auth errors are not retried
            except Exception as exc:
                last_error = exc
                if attempt >= MAX_RETRIES or not _is_retryable(exc):
                    if isinstance(exc, APIError):
                        raise _translate_api_error(exc) from exc
                    raise RequestFailure(str(exc)) from exc

                delay = _get_retry_delay(attempt, exc)
                status = getattr(exc, "status_code", "?")
                log.warning(
                    "API request failed (attempt %d/%d, status=%s), retrying in %.1fs: %s",
                    attempt + 1, MAX_RETRIES + 1, status, delay, exc,
                )
                yield ApiRetryEvent(
                    message=str(exc),
                    attempt=attempt + 1,
                    max_attempts=MAX_RETRIES + 1,
                    delay_seconds=delay,
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            if isinstance(last_error, APIError):
                raise _translate_api_error(last_error) from last_error
            raise RequestFailure(str(last_error)) from last_error

    async def _stream_once(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """单次流式消息传输尝试。

        构建请求参数，调用 Anthropic SDK 的流式接口，解析响应事件。
        在 Claude OAuth 模式下，自动添加计费归因头、Beta 标志和元数据。
        仅提取文本增量事件，最终汇总为完整的消息完成事件。

        Args:
            request: API 消息请求对象。

        Returns:
            异步迭代器，产生 :class:`ApiTextDeltaEvent` 和
            :class:`ApiMessageCompleteEvent` 事件。

        Raises:
            OpenHarnessApiError: 当 API 返回不可恢复的错误时抛出。
        """
        params: dict[str, Any] = {
            "model": request.model,
            "messages": [message.to_api_param() for message in request.messages],
            "max_tokens": request.max_tokens,
        }
        if request.system_prompt:
            params["system"] = request.system_prompt
        if self._claude_oauth:
            attribution = claude_attribution_header()
            params["system"] = (
                f"{attribution}\n{params['system']}"
                if params.get("system")
                else attribution
            )
        if request.tools:
            params["tools"] = request.tools
        if self._claude_oauth:
            params["betas"] = claude_oauth_betas()
            params["metadata"] = {
                "user_id": json.dumps(
                    {
                        "device_id": "openharness",
                        "session_id": self._session_id,
                        "account_uuid": "",
                    },
                    separators=(",", ":"),
                )
            }
            params["extra_headers"] = {"x-client-request-id": str(uuid.uuid4())}

        try:
            stream_api = self._client.beta.messages if self._claude_oauth else self._client.messages
            async with stream_api.stream(**params) as stream:
                async for event in stream:
                    if getattr(event, "type", None) != "content_block_delta":
                        continue
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) != "text_delta":
                        continue
                    text = getattr(delta, "text", "")
                    if text:
                        yield ApiTextDeltaEvent(text=text)

                final_message = await stream.get_final_message()
        except APIError as exc:
            if isinstance(exc, APIStatusError) and exc.status_code in RETRYABLE_STATUS_CODES:
                raise  # Let retry logic handle it
            raise _translate_api_error(exc) from exc

        usage = getattr(final_message, "usage", None)
        yield ApiMessageCompleteEvent(
            message=assistant_message_from_api(final_message),
            usage=UsageSnapshot(
                input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
                output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            ),
            stop_reason=getattr(final_message, "stop_reason", None),
        )


def _translate_api_error(exc: APIError) -> OpenHarnessApiError:
    """将 Anthropic SDK 错误转换为 OpenHarness 统一的错误类型。

    根据异常类名进行映射：
    - ``AuthenticationError`` / ``PermissionDeniedError`` → :class:`AuthenticationFailure`
    - ``RateLimitError`` → :class:`RateLimitFailure`
    - 其他所有错误 → :class:`RequestFailure`

    Args:
        exc: Anthropic SDK 抛出的 API 错误。

    Returns:
        对应的 OpenHarness API 错误实例。
    """
    name = exc.__class__.__name__
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return AuthenticationFailure(str(exc))
    if name == "RateLimitError":
        return RateLimitFailure(str(exc))
    return RequestFailure(str(exc))
