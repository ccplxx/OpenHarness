"""OpenHarness API 模块入口。

本模块是 ``openharness.api`` 包的入口文件，负责将 API 子系统的核心组件导出为公共 API。
它整合了以下子模块的功能：

- :class:`AnthropicApiClient` — Anthropic 原生 API 客户端，支持流式传输和自动重试。
- :class:`CodexApiClient` — OpenAI Codex 订阅客户端，基于 ChatGPT Codex Responses API。
- :class:`CopilotClient` — GitHub Copilot API 客户端，封装 OpenAI 兼容客户端并附加 Copilot 请求头。
- :class:`OpenAICompatibleClient` — OpenAI 兼容 API 客户端，支持 DashScope、Moonshot 等提供商。
- :class:`OpenHarnessApiError` — API 错误类型的基类。
- :class:`ProviderInfo` / :func:`detect_provider` / :func:`auth_status` — 提供商检测与认证状态工具。
- :class:`UsageSnapshot` — LLM 调用的令牌用量快照。
"""

from openharness.api.client import AnthropicApiClient
from openharness.api.codex_client import CodexApiClient
from openharness.api.copilot_client import CopilotClient
from openharness.api.errors import OpenHarnessApiError
from openharness.api.openai_client import OpenAICompatibleClient
from openharness.api.provider import ProviderInfo, auth_status, detect_provider
from openharness.api.usage import UsageSnapshot

__all__ = [
    "AnthropicApiClient",
    "CodexApiClient",
    "CopilotClient",
    "OpenAICompatibleClient",
    "OpenHarnessApiError",
    "ProviderInfo",
    "UsageSnapshot",
    "auth_status",
    "detect_provider",
]
