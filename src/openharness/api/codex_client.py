"""OpenAI Codex 订阅客户端模块，基于 ChatGPT Codex Responses API。

本模块实现了 OpenHarness 与 OpenAI Codex 订阅服务的交互客户端。
Codex 订阅通过 ChatGPT 平台的 Codex Responses API 提供模型访问，
使用 OAuth 访问令牌（JWT）进行认证。

核心功能包括：

1. **JWT 解析**：从 Codex 访问令牌中提取账户 ID 等元数据。
2. **URL 解析**：自动构建 Codex Responses API 的请求 URL。
3. **请求头构建**：生成包含认证信息和平台标识的 HTTP 请求头。
4. **消息格式转换**：将 OpenHarness 内部的对话消息格式转换为 Codex API 格式。
5. **SSE 流式解析**：解析服务器推送事件（Server-Sent Events）流，提取文本增量和工具调用。
6. **自动重试**：内置指数退避重试逻辑，处理可恢复的瞬时错误。
7. **错误转换**：将 HTTP 状态码错误转换为 OpenHarness 统一的错误类型。
"""

from __future__ import annotations

import base64
import json
import platform
from typing import Any, AsyncIterator

import httpx

from openharness.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiRetryEvent,
    ApiStreamEvent,
    ApiTextDeltaEvent,
)
from openharness.api.errors import AuthenticationFailure, OpenHarnessApiError, RateLimitFailure, RequestFailure
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, ImageBlock, TextBlock, ToolResultBlock, ToolUseBlock

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
MAX_RETRIES = 3
BASE_DELAY_SECONDS = 1.0
MAX_DELAY_SECONDS = 30.0


def _extract_account_id(token: str) -> str:
    """从 Codex 访问令牌（JWT）中提取 ChatGPT 账户 ID。

    解码 JWT 的 Payload 部分，从中提取 ``chatgpt_account_id`` 字段，
    该字段用于构建 Codex API 请求头中的 ``chatgpt-account-id`` 参数。

    Args:
        token: Codex OAuth 访问令牌字符串。

    Returns:
        ChatGPT 账户 ID 字符串。

    Raises:
        AuthenticationFailure: 当令牌格式无效、缺少账户元数据或账户 ID 时抛出。
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthenticationFailure("Codex access token is not a valid JWT.")
    try:
        payload = json.loads(
            base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8")
        )
    except Exception as exc:
        raise AuthenticationFailure("Could not decode Codex access token payload.") from exc
    auth_claim = payload.get(JWT_CLAIM_PATH)
    if not isinstance(auth_claim, dict):
        raise AuthenticationFailure("Codex access token is missing account metadata.")
    account_id = auth_claim.get("chatgpt_account_id")
    if not isinstance(account_id, str) or not account_id:
        raise AuthenticationFailure("Codex access token is missing chatgpt_account_id.")
    return account_id


def _resolve_codex_url(base_url: str | None) -> str:
    """解析并构建 Codex Responses API 的完整 URL。

    根据提供的 Base URL 推导出 Codex Responses 端点。若未提供
    或提供的 URL 不包含 ``chatgpt.com/backend-api``，则使用默认地址。
    自动追加 ``/codex/responses`` 路径。

    Args:
        base_url: 自定义的 Base URL，若为 ``None`` 则使用默认地址。

    Returns:
        完整的 Codex Responses API URL 字符串。
    """
    trimmed = (base_url or "").strip()
    if trimmed and "chatgpt.com/backend-api" not in trimmed:
        trimmed = ""
    raw = (trimmed or DEFAULT_CODEX_BASE_URL).rstrip("/")
    if raw.endswith("/codex/responses"):
        return raw
    if raw.endswith("/codex"):
        return f"{raw}/responses"
    return f"{raw}/codex/responses"


def _build_codex_headers(token: str, *, session_id: str | None = None) -> dict[str, str]:
    """构建 Codex API 请求所需的 HTTP 请求头。

    生成包含 Bearer 认证、ChatGPT 账户 ID、来源标识、User-Agent、
    Beta 功能标志和内容类型等信息的请求头字典。

    Args:
        token: Codex OAuth 访问令牌。
        session_id: 可选的会话 ID，若提供则添加到请求头中。

    Returns:
        包含所有必需请求头的字典。
    """
    account_id = _extract_account_id(token)
    headers = {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "originator": "openharness",
        "User-Agent": f"openharness ({platform.system().lower()} {platform.machine() or 'unknown'})",
        "OpenAI-Beta": "responses=experimental",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }
    if session_id:
        headers["session_id"] = session_id
    return headers


def _convert_messages_to_codex(messages: list[ConversationMessage]) -> list[dict[str, Any]]:
    """将 OpenHarness 内部对话消息转换为 Codex API 的输入格式。

    转换规则：
    - 用户文本 → ``input_text`` 类型
    - 用户图片 → ``input_image`` 类型（Base64 数据 URL）
    - 工具调用结果 → ``function_call_output`` 类型
    - 助手文本 → ``message`` 类型（含 ``output_text``）
    - 助手工具调用 → ``function_call`` 类型

    Args:
        messages: OpenHarness 对话消息列表。

    Returns:
        Codex API 格式的输入项列表。
    """
    result: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user":
            user_content: list[dict[str, Any]] = []
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    user_content.append({"type": "input_text", "text": block.text})
                elif isinstance(block, ImageBlock):
                    user_content.append({
                        "type": "input_image",
                        "image_url": f"data:{block.media_type};base64,{block.data}",
                    })
            if user_content:
                result.append({"role": "user", "content": user_content})
            for block in msg.content:
                if isinstance(block, ToolResultBlock):
                    result.append({
                        "type": "function_call_output",
                        "call_id": block.tool_use_id,
                        "output": block.content,
                    })
            continue

        assistant_text = "".join(block.text for block in msg.content if isinstance(block, TextBlock))
        if assistant_text:
            result.append({
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": assistant_text, "annotations": []}],
            })
        for block in msg.content:
            if isinstance(block, ToolUseBlock):
                result.append({
                    "type": "function_call",
                    "id": f"fc_{block.id[:58]}",
                    "call_id": block.id,
                    "name": block.name,
                    "arguments": json.dumps(block.input, separators=(",", ":")),
                })
    return result


def _convert_tools_to_codex(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将 Anthropic 格式的工具定义转换为 Codex API 格式。

    将 ``input_schema`` 字段重命名为 ``parameters``，并添加
    ``type: "function"`` 包装，符合 Codex API 的工具定义格式。

    Args:
        tools: Anthropic 格式的工具定义列表。

    Returns:
        Codex API 格式的工具定义列表。
    """
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": tool.get("input_schema", {}),
        }
        for tool in tools
    ]


def _usage_from_response(response: dict[str, Any]) -> UsageSnapshot:
    """从 Codex 响应中提取令牌用量信息。

    Args:
        response: Codex API 的完整响应字典。

    Returns:
        令牌用量快照，若无用量数据则返回默认的空快照。
    """
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return UsageSnapshot()
    return UsageSnapshot(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
    )


def _stop_reason_from_response(response: dict[str, Any], *, has_tool_calls: bool) -> str | None:
    """从 Codex 响应中推导停止原因。

    将 Codex API 的 ``status`` 字段转换为 OpenHarness 统一的停止原因：
    - ``completed`` + 有工具调用 → ``tool_use``
    - ``completed`` + 无工具调用 → ``stop``
    - ``incomplete`` → ``length``
    - ``failed`` / ``cancelled`` → ``error``

    Args:
        response: Codex API 的完整响应字典。
        has_tool_calls: 响应中是否包含工具调用。

    Returns:
        停止原因字符串，若无法识别则返回 ``None``。
    """
    status = response.get("status")
    if has_tool_calls and status == "completed":
        return "tool_use"
    if status == "completed":
        return "stop"
    if status == "incomplete":
        return "length"
    if status in {"failed", "cancelled"}:
        return "error"
    return None


def _format_error_message(status_code: int, payload: str) -> str:
    """格式化 Codex API 的错误响应消息。

    尝试从 JSON 响应中提取嵌套的 ``error.message`` 或 ``detail`` 字段，
    若无法解析则使用原始响应文本或状态码构造错误消息。

    Args:
        status_code: HTTP 状态码。
        payload: 响应体原始文本。

    Returns:
        格式化后的错误消息字符串。
    """
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message.strip():
                return message
        detail = parsed.get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail
    text = payload.strip()
    if text:
        return text
    return f"Codex request failed with status {status_code}"


def _format_codex_stream_error(event: dict[str, Any], *, fallback: str) -> str:
    """格式化 Codex SSE 流中的错误事件消息。

    从错误事件字典中提取 ``message``、``code`` 和 ``request_id`` 等字段，
    组合为人类可读的错误消息。若无法提取有效信息，则使用回退文本。

    Args:
        event: SSE 错误事件字典。
        fallback: 无法提取错误信息时的回退文本。

    Returns:
        格式化后的错误消息字符串。
    """
    error = event.get("error")
    payload = error if isinstance(error, dict) else event
    message = payload.get("message") if isinstance(payload, dict) else None
    code = payload.get("code") if isinstance(payload, dict) else None
    request_id = (
        (payload.get("request_id") if isinstance(payload, dict) else None)
        or event.get("request_id")
    )

    parts: list[str] = []
    if isinstance(message, str) and message.strip():
        parts.append(message.strip())
    elif isinstance(code, str) and code.strip():
        parts.append(code.strip())
    else:
        parts.append(fallback)

    if isinstance(code, str) and code.strip():
        parts.append(f"(code={code.strip()})")
    if isinstance(request_id, str) and request_id.strip():
        parts.append(f"[request_id={request_id.strip()}]")
    return " ".join(parts)


def _translate_status_error(status_code: int, message: str) -> OpenHarnessApiError:
    """根据 HTTP 状态码将错误转换为对应的 OpenHarness API 错误类型。

    - 401/403 → :class:`AuthenticationFailure`
    - 429 → :class:`RateLimitFailure`
    - 其他 → :class:`RequestFailure`

    Args:
        status_code: HTTP 状态码。
        message: 错误描述消息。

    Returns:
        对应的 OpenHarness API 错误实例。
    """
    if status_code in {401, 403}:
        return AuthenticationFailure(message)
    if status_code == 429:
        return RateLimitFailure(message)
    return RequestFailure(message)


class CodexApiClient:
    """基于 ChatGPT/Codex 订阅的 Codex Responses API 客户端。

    该客户端实现了 :class:`SupportsStreamingMessages` 协议，
    通过 Codex Responses API 的 SSE 流式端点与 OpenAI Codex 订阅服务交互。
    使用 OAuth 访问令牌进行认证，内置自动重试和错误转换逻辑。
    """

    def __init__(self, auth_token: str, *, base_url: str | None = None) -> None:
        """初始化 Codex API 客户端。

        Args:
            auth_token: Codex OAuth 访问令牌（JWT 格式）。
            base_url: 可选的自定义 API Base URL，若为 ``None`` 则使用默认的
                ChatGPT 后端地址。
        """
        self._auth_token = auth_token
        self._base_url = base_url
        self._url = _resolve_codex_url(base_url)

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """以流式方式发送消息请求到 Codex API。

        实现带自动重试的流式消息传输。对于可恢复的瞬时错误，
        使用指数退避策略自动重试，并在重试前产生重试事件。

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
            except Exception as exc:
                last_error = exc
                if attempt >= MAX_RETRIES or not self._is_retryable(exc):
                    raise self._translate_error(exc) from exc
                delay = min(BASE_DELAY_SECONDS * (2 ** attempt), MAX_DELAY_SECONDS)
                import asyncio

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
        """单次 Codex 流式消息传输尝试。

        构建请求参数和请求头，通过 httpx 发送 SSE 流式请求。
        解析 SSE 事件流中的文本增量、工具调用完成和响应完成事件，
        最终汇总为完整的消息完成事件。

        Args:
            request: API 消息请求对象。

        Returns:
            异步迭代器，产生文本增量和消息完成事件。

        Raises:
            RequestFailure: 当 Codex 响应失败或遇到流错误时抛出。
        """
        body: dict[str, Any] = {
            "model": request.model,
            "store": False,
            "stream": True,
            "instructions": request.system_prompt or "You are OpenHarness.",
            "input": _convert_messages_to_codex(request.messages),
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }
        if request.tools:
            body["tools"] = _convert_tools_to_codex(request.tools)

        content: list[TextBlock | ToolUseBlock] = []
        current_text_parts: list[str] = []
        completed_response: dict[str, Any] | None = None

        headers = _build_codex_headers(self._auth_token)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            async with client.stream("POST", self._url, headers=headers, json=body) as response:
                if response.status_code >= 400:
                    payload = await response.aread()
                    message = _format_error_message(response.status_code, payload.decode("utf-8", "replace"))
                    raise httpx.HTTPStatusError(message, request=response.request, response=response)

                async for event in self._iter_sse_events(response):
                    event_type = event.get("type")
                    if event_type == "response.output_text.delta":
                        delta = event.get("delta")
                        if isinstance(delta, str) and delta:
                            current_text_parts.append(delta)
                            yield ApiTextDeltaEvent(text=delta)
                    elif event_type == "response.output_item.done":
                        item = event.get("item")
                        if not isinstance(item, dict):
                            continue
                        item_type = item.get("type")
                        if item_type == "message":
                            text = ""
                            raw_content = item.get("content")
                            if isinstance(raw_content, list):
                                parts = []
                                for block in raw_content:
                                    if isinstance(block, dict):
                                        if block.get("type") == "output_text":
                                            parts.append(str(block.get("text", "")))
                                        elif block.get("type") == "refusal":
                                            parts.append(str(block.get("refusal", "")))
                                text = "".join(parts)
                            if text:
                                content.append(TextBlock(text=text))
                        elif item_type == "function_call":
                            arguments = item.get("arguments")
                            parsed_arguments: dict[str, Any]
                            if isinstance(arguments, str) and arguments:
                                try:
                                    loaded = json.loads(arguments)
                                except json.JSONDecodeError:
                                    loaded = {}
                            else:
                                loaded = {}
                            parsed_arguments = loaded if isinstance(loaded, dict) else {}
                            call_id = item.get("call_id")
                            name = item.get("name")
                            if isinstance(call_id, str) and call_id and isinstance(name, str) and name:
                                content.append(ToolUseBlock(id=call_id, name=name, input=parsed_arguments))
                    elif event_type == "response.completed":
                        response_payload = event.get("response")
                        if isinstance(response_payload, dict):
                            completed_response = response_payload
                    elif event_type == "response.failed":
                        response_payload = event.get("response")
                        if isinstance(response_payload, dict):
                            raise RequestFailure(
                                _format_codex_stream_error(
                                    response_payload,
                                    fallback="Codex response failed",
                                )
                            )
                        raise RequestFailure("Codex response failed")
                    elif event_type == "error":
                        raise RequestFailure(
                            _format_codex_stream_error(event, fallback="Codex error")
                        )

        if current_text_parts and not any(isinstance(block, TextBlock) for block in content):
            content.insert(0, TextBlock(text="".join(current_text_parts)))

        final_message = ConversationMessage(role="assistant", content=content)
        usage = _usage_from_response(completed_response or {})
        stop_reason = _stop_reason_from_response(
            completed_response or {},
            has_tool_calls=bool(final_message.tool_uses),
        )
        yield ApiMessageCompleteEvent(
            message=final_message,
            usage=usage,
            stop_reason=stop_reason,
        )

    async def _iter_sse_events(self, response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        """解析 httpx 响应中的 SSE 事件流。

        逐行读取 HTTP 响应，提取 ``data:`` 前缀的数据行，
        将连续的数据行合并后解析为 JSON 对象。
        忽略空行（事件分隔符）和 ``[DONE]`` 终止标记。

        Args:
            response: httpx 的流式响应对象。

        Returns:
            异步迭代器，产生解析后的 SSE 事件字典。
        """
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                if data_lines:
                    payload = "\n".join(data_lines).strip()
                    data_lines = []
                    if payload and payload != "[DONE]":
                        try:
                            event = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            yield event
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].strip())
        if data_lines:
            payload = "\n".join(data_lines).strip()
            if payload and payload != "[DONE]":
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    return
                if isinstance(event, dict):
                    yield event

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        """判断异常是否为可重试的瞬时错误。

        可重试的错误包括：
        - HTTP 状态码为 429、500、502、503、504。
        - :class:`RateLimitFailure` 错误。
        - 包含超时、连接、网络、速率等关键词的 :class:`RequestFailure`。
        - httpx 的超时和网络错误。

        Args:
            exc: 待检查的异常对象。

        Returns:
            若异常可安全重试返回 ``True``，否则返回 ``False``。
        """
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {429, 500, 502, 503, 504}
        if isinstance(exc, RateLimitFailure):
            return True
        if isinstance(exc, RequestFailure):
            message = str(exc).lower()
            return any(term in message for term in ["timeout", "connect", "network", "rate", "overloaded"])
        if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
            return True
        return False

    @staticmethod
    def _translate_error(exc: Exception) -> OpenHarnessApiError:
        """将异常转换为 OpenHarness 统一的错误类型。

        - 已是 :class:`OpenHarnessApiError` 的错误直接返回。
        - httpx HTTP 状态码错误根据状态码映射。
        - 其他 httpx 错误转换为 :class:`RequestFailure`。

        Args:
            exc: 待转换的异常对象。

        Returns:
            对应的 OpenHarness API 错误实例。
        """
        if isinstance(exc, OpenHarnessApiError):
            return exc
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return _translate_status_error(status, str(exc))
        if isinstance(exc, httpx.HTTPError):
            return RequestFailure(str(exc))
        return RequestFailure(str(exc))
