"""GitHub Copilot API 客户端模块。

本模块实现了 OpenHarness 与 GitHub Copilot API 的交互客户端。
Copilot 的聊天端点与 OpenAI API 兼容，因此消息和工具的转换
委托给内部的 :class:`OpenAICompatibleClient` 处理。

认证使用持久化的 GitHub OAuth 令牌（``Authorization: Bearer <token>``），
无需额外的令牌交换。客户端在初始化时自动添加 Copilot 特有的请求头
（如 ``Openai-Intent: conversation-edits``）和 User-Agent 标识。
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from openai import AsyncOpenAI

from openharness.api.client import (
    ApiMessageRequest,
    ApiStreamEvent,
)
from openharness.api.copilot_auth import (
    copilot_api_base,
    load_copilot_auth,
)
from openharness.api.errors import AuthenticationFailure
from openharness.api.openai_client import OpenAICompatibleClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Header constants
# ---------------------------------------------------------------------------

_VERSION = "0.1.0"  # OpenHarness version for User-Agent

# Default model for Copilot requests when the configured model is not
# available in the Copilot model catalog.
COPILOT_DEFAULT_MODEL = "gpt-4o"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class CopilotClient:
    """支持 Copilot 的 API 客户端，实现 ``SupportsStreamingMessages`` 协议。

    直接使用 GitHub OAuth 令牌作为 Bearer 令牌访问 Copilot API，
    无需令牌交换或会话管理。内部委托 :class:`OpenAICompatibleClient`
    处理所有消息和工具转换。

    Attributes:
        _token: GitHub OAuth 令牌。
        _enterprise_url: GitHub Enterprise URL（可能为 ``None``）。
        _model: 默认模型名称，可在请求时被覆盖。
        _inner: 内部的 OpenAI 兼容客户端实例。
    """

    def __init__(
        self,
        github_token: str | None = None,
        *,
        enterprise_url: str | None = None,
        model: str | None = None,
    ) -> None:
        """初始化 Copilot 客户端。

        优先使用显式传入的令牌，若未提供则从持久化的认证文件中加载。
        企业 URL 的解析优先级为：显式参数 > 持久化认证文件 > None（公共 GitHub）。

        Args:
            github_token: GitHub OAuth 令牌（``ghu_...`` / ``gho_...``），
                若为 ``None`` 则从 ``~/.openharness/copilot_auth.json`` 加载。
            enterprise_url: 可选的 GitHub Enterprise 域名，
                若为 ``None`` 则从持久化认证文件加载（回退到公共 GitHub）。
            model: 默认请求的模型名称，可在请求时通过 ``ApiMessageRequest.model`` 覆盖。

        Raises:
            AuthenticationFailure: 当未找到 GitHub Copilot 令牌时抛出。
        """
        auth_info = load_copilot_auth()
        token = github_token or (auth_info.github_token if auth_info else None)
        if not token:
            raise AuthenticationFailure(
                "No GitHub Copilot token found. Run 'oh auth copilot-login' first."
            )

        # Resolve enterprise_url: explicit arg > persisted auth > None (public)
        ent_url = enterprise_url or (auth_info.enterprise_url if auth_info else None)

        self._token = token
        self._enterprise_url = ent_url
        self._model = model

        # Build the inner OpenAI-compatible client once.
        base_url = copilot_api_base(ent_url)
        default_headers: dict[str, str] = {
            "User-Agent": f"openharness/{_VERSION}",
            "Openai-Intent": "conversation-edits",
        }
        raw_openai = AsyncOpenAI(
            api_key=token,
            base_url=base_url,
            default_headers=default_headers,
        )
        self._inner = OpenAICompatibleClient(
            api_key=token,
            base_url=base_url,
        )
        # Swap the underlying SDK client so Copilot headers are used.
        self._inner._client = raw_openai  # noqa: SLF001

        log.info(
            "CopilotClient initialised (api_base=%s, enterprise=%s)",
            base_url,
            ent_url or "none",
        )

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        """从 Copilot API 流式获取聊天补全结果。

        实现 OpenHarness 查询引擎期望的 ``SupportsStreamingMessages`` 协议。
        若构造时指定了默认模型，则覆盖请求中的模型标识；
        否则直接使用请求中的模型标识。

        Args:
            request: API 消息请求对象。

        Returns:
            异步迭代器，依次产生流式事件。
        """
        effective_model = self._model or request.model
        patched = ApiMessageRequest(
            model=effective_model,
            messages=request.messages,
            system_prompt=request.system_prompt,
            max_tokens=request.max_tokens,
            tools=request.tools,
        )
        async for event in self._inner.stream_message(patched):
            yield event
