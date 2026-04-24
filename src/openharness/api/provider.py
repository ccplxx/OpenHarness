"""提供商认证与能力检测辅助模块。

本模块提供提供商（Provider）的元数据解析和能力检测功能，
用于 UI 展示和诊断。核心功能包括：

1. **提供商检测**（:func:`detect_provider`）：根据配置设置推断当前活跃的提供商，
   返回提供商名称、认证类型和语音支持状态等信息。
2. **认证状态查询**（:func:`auth_status`）：返回当前提供商认证状态的紧凑字符串描述，
   供状态栏或诊断输出使用。
3. **ProviderInfo 数据类**：封装提供商的元数据，包括名称、认证类型、语音支持状态等。
"""

from __future__ import annotations

from dataclasses import dataclass

from openharness.auth.external import describe_external_binding
from openharness.auth.storage import load_external_binding
from openharness.api.registry import detect_provider_from_registry
from openharness.config.settings import Settings

_AUTH_KIND: dict[str, str] = {
    "anthropic": "api_key",
    "openai_compat": "api_key",
    "copilot": "oauth_device",
    "openai_codex": "external_oauth",
    "anthropic_claude": "external_oauth",
}

_VOICE_REASON: dict[str, str] = {
    "anthropic": (
        "voice mode shell exists, but live voice auth/streaming is not configured in this build"
    ),
    "openai_compat": "voice mode is not wired for OpenAI-compatible providers in this build",
    "copilot": "voice mode is not supported for GitHub Copilot",
    "openai_codex": "voice mode is not supported for Codex subscription auth",
    "anthropic_claude": "voice mode is not supported for Claude subscription auth",
}


@dataclass(frozen=True)
class ProviderInfo:
    """已解析的提供商元数据，用于 UI 展示和诊断。

    Attributes:
        name: 提供商规范名称（如 ``"anthropic"``、``"openai-compatible"``、``"github_copilot"``）。
        auth_kind: 认证类型（``"api_key"``、``"oauth_device"``、``"external_oauth"``）。
        voice_supported: 是否支持语音模式（当前所有提供商均返回 ``False``）。
        voice_reason: 语音模式不支持的原因说明。
    """

    name: str
    auth_kind: str
    voice_supported: bool
    voice_reason: str


def detect_provider(settings: Settings) -> ProviderInfo:
    """使用注册表推断当前活跃的提供商及其能力集。

    按以下优先级检测提供商：
    1. Codex 订阅（``openai_codex``）
    2. Claude 订阅（``anthropic_claude``）
    3. Copilot（``api_format="copilot"``）
    4. 通过注册表根据模型名、API Key 前缀和 Base URL 关键词检测
    5. 回退：根据 ``api_format`` 选择 Anthropic 或 OpenAI 兼容默认

    Args:
        settings: 当前的配置设置对象。

    Returns:
        包含提供商名称、认证类型和能力信息的 :class:`ProviderInfo` 对象。
    """
    if settings.provider == "openai_codex":
        return ProviderInfo(
            name="openai-codex",
            auth_kind="external_oauth",
            voice_supported=False,
            voice_reason=_VOICE_REASON["openai_codex"],
        )
    if settings.provider == "anthropic_claude":
        return ProviderInfo(
            name="claude-subscription",
            auth_kind="external_oauth",
            voice_supported=False,
            voice_reason=_VOICE_REASON["anthropic_claude"],
        )
    if settings.api_format == "copilot":
        return ProviderInfo(
            name="github_copilot",
            auth_kind="oauth_device",
            voice_supported=False,
            voice_reason=_VOICE_REASON["copilot"],
        )

    spec = detect_provider_from_registry(
        model=settings.model,
        api_key=settings.api_key or None,
        base_url=settings.base_url,
    )

    if spec is not None:
        backend = spec.backend_type
        return ProviderInfo(
            name=spec.name,
            auth_kind=_AUTH_KIND.get(backend, "api_key"),
            voice_supported=False,
            voice_reason=_VOICE_REASON.get(backend, "voice mode is not supported for this provider"),
        )

    # Fallback: use api_format to pick a sensible default
    if settings.api_format == "openai":
        return ProviderInfo(
            name="openai-compatible",
            auth_kind="api_key",
            voice_supported=False,
            voice_reason=_VOICE_REASON["openai_compat"],
        )
    return ProviderInfo(
        name="anthropic",
        auth_kind="api_key",
        voice_supported=False,
        voice_reason=_VOICE_REASON["anthropic"],
    )


def auth_status(settings: Settings) -> str:
    """返回当前提供商认证状态的紧凑字符串描述。

    根据提供商类型和认证配置，返回以下格式的状态字符串：
    - ``"configured"`` — 已配置认证凭据。
    - ``"configured (enterprise: <url>)"`` — 已配置企业版 Copilot。
    - ``"configured (external: <source>)"`` — 已配置外部认证源。
    - ``"missing (run 'oh auth copilot-login')"`` — 缺少凭据并提示登录命令。
    - ``"expired"`` / ``"refreshable"`` — Claude 订阅令牌过期/可刷新。
    - ``"invalid base_url"`` — Claude 订阅的 Base URL 为第三方端点。
    - ``"missing"`` — 通用缺失状态。

    Args:
        settings: 当前的配置设置对象。

    Returns:
        认证状态描述字符串。
    """
    if settings.api_format == "copilot":
        from openharness.api.copilot_auth import load_copilot_auth

        auth_info = load_copilot_auth()
        if not auth_info:
            return "missing (run 'oh auth copilot-login')"
        if auth_info.enterprise_url:
            return f"configured (enterprise: {auth_info.enterprise_url})"
        return "configured"
    try:
        resolved = settings.resolve_auth()
    except ValueError as exc:
        if settings.provider == "openai_codex":
            return "missing (run 'oh auth codex-login')"
        if settings.provider == "anthropic_claude":
            binding = load_external_binding("anthropic_claude")
            if binding is not None:
                external_state = describe_external_binding(binding)
                if external_state.state != "missing":
                    return external_state.state
            message = str(exc)
            if "third-party" in message:
                return "invalid base_url"
            return "missing (run 'oh auth claude-login')"
        return "missing"
    if resolved.source.startswith("external:"):
        return f"configured ({resolved.source.removeprefix('external:')})"
    return "configured"
