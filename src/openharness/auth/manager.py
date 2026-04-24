"""OpenHarness 统一认证管理器模块。

本模块提供 :class:`AuthManager` 类，作为 OpenHarness 认证状态管理的中央入口。
它整合了凭据存储（:mod:`openharness.auth.storage`）、外部认证绑定
（:mod:`openharness.auth.external`）和配置系统（:mod:`openharness.config.settings`），
为上层提供统一的认证状态查询、凭据管理、配置文件操作等 API。

核心功能包括：

1. **认证状态查询**：检查各提供商的凭据配置状态（环境变量、文件存储、外部绑定等）。
2. **配置文件管理**：创建、更新、删除和切换提供商配置文件（Profile）。
3. **凭据存储**：安全地存储、加载和清除提供商凭据。
4. **认证源切换**：在不同认证方式（API Key、OAuth、订阅等）之间切换。
5. **提供商切换**：在不同提供商和配置文件之间快速切换。
"""

from __future__ import annotations

import logging
from typing import Any

from openharness.config.settings import (
    ProviderProfile,
    auth_source_provider_name,
    auth_source_uses_api_key,
    builtin_provider_profile_names,
    credential_storage_provider_name,
    default_auth_source_for_provider,
    display_label_for_profile,
    display_model_setting,
)
from openharness.auth.storage import (
    clear_provider_credentials,
    load_external_binding,
    load_credential,
    store_credential,
)

log = logging.getLogger(__name__)

# Providers that OpenHarness knows about.
_KNOWN_PROVIDERS = [
    "anthropic",
    "anthropic_claude",
    "openai",
    "openai_codex",
    "copilot",
    "dashscope",
    "bedrock",
    "vertex",
    "moonshot",
    "gemini",
    "minimax",
]

_AUTH_SOURCES = [
    "anthropic_api_key",
    "openai_api_key",
    "codex_subscription",
    "claude_subscription",
    "copilot_oauth",
    "dashscope_api_key",
    "bedrock_api_key",
    "vertex_api_key",
    "moonshot_api_key",
    "gemini_api_key",
    "minimax_api_key",
]

_PROFILE_BY_PROVIDER = {
    "anthropic": "claude-api",
    "anthropic_claude": "claude-subscription",
    "openai": "openai-compatible",
    "openai_codex": "codex",
    "copilot": "copilot",
    "moonshot": "moonshot",
    "gemini": "gemini",
    "minimax": "minimax",
}


class AuthManager:
    """OpenHarness 提供商认证的中央管理器。

    该类是认证子系统的核心入口，负责读写凭据（通过 :mod:`openharness.auth.storage`）、
    跟踪当前活跃的提供商配置，并提供统一的认证状态查询和配置管理接口。

    支持的提供商包括：Anthropic、OpenAI、Codex、Claude、Copilot、DashScope、
    Bedrock、Vertex、Moonshot、Gemini、Minimax 等。

    支持的认证源包括：各类 API Key、Codex 订阅、Claude 订阅、Copilot OAuth 等。

    Attributes:
        _settings: 配置对象，延迟加载以避免在实例化时导入完整的配置子系统。
    """

    def __init__(self, settings: Any | None = None) -> None:
        """初始化认证管理器。

        Args:
            settings: 可选的配置对象，若为 ``None`` 则在首次访问时延迟加载。
        """
        self._settings = settings

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def settings(self) -> Any:
        """延迟加载的配置属性。

        若未在初始化时提供配置对象，则在首次访问时从
        :mod:`openharness.config` 加载设置。这种延迟加载机制使得
        管理器可以在不导入完整配置子系统的情况下被实例化。

        Returns:
            当前的配置对象。
        """
        if self._settings is None:
            from openharness.config import load_settings

            self._settings = load_settings()
        return self._settings

    def _provider_from_settings(self) -> str:
        """从当前活跃的配置文件中推导出提供商名称。

        Returns:
            当前活跃配置文件对应的提供商名称字符串。
        """
        _, profile = self.settings.resolve_profile()
        return profile.provider

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_active_provider(self) -> str:
        """获取当前活跃提供商的名称。

        Returns:
            当前活跃提供商的名称字符串（如 ``"anthropic"``、``"openai"`` 等）。
        """
        return self._provider_from_settings()

    def get_active_profile(self) -> str:
        """获取当前活跃的提供商配置文件名称。

        Returns:
            当前活跃的配置文件名称字符串。
        """
        return self.settings.resolve_profile()[0]

    def list_profiles(self) -> dict[str, ProviderProfile]:
        """获取所有已配置的提供商配置文件。

        Returns:
            以配置文件名称为键、:class:`ProviderProfile` 对象为值的字典。
        """
        return self.settings.merged_profiles()

    def get_auth_source_statuses(self) -> dict[str, Any]:
        """获取所有认证源的配置状态。

        遍历所有已知的认证源，检查每个认证源的凭据是否已配置，
        包括检查环境变量、文件存储、外部绑定（Codex/Claude 订阅）
        和 Copilot OAuth 等来源。返回每个认证源的配置状态、来源类型、
        当前状态及是否为活跃配置等信息。

        Returns:
            以认证源名称为键的字典，每个值包含：
            - ``configured``: 是否已配置。
            - ``source``: 凭据来源（``env``、``file``、``external``、``missing``）。
            - ``state``: 状态标识（``configured``、``missing``、``expired``、``refreshable`` 等）。
            - ``detail``: 状态详情描述。
            - ``active``: 是否为当前活跃配置的认证源。
            - ``active_profile``: 当前活跃配置文件名称。
        """
        import os

        from openharness.auth.external import describe_external_binding

        active_profile_name, active_profile = self.settings.resolve_profile()
        result: dict[str, Any] = {}
        for source in _AUTH_SOURCES:
            configured = False
            origin = "missing"
            state = "missing"
            detail = ""
            storage_provider = auth_source_provider_name(source)
            if source == "anthropic_api_key":
                if os.environ.get("ANTHROPIC_API_KEY"):
                    configured = True
                    origin = "env"
                    state = "configured"
                elif load_credential(storage_provider, "api_key") or getattr(self.settings, "api_key", ""):
                    configured = True
                    origin = "file"
                    state = "configured"
            elif source == "openai_api_key":
                if os.environ.get("OPENAI_API_KEY"):
                    configured = True
                    origin = "env"
                    state = "configured"
                elif load_credential(storage_provider, "api_key"):
                    configured = True
                    origin = "file"
                    state = "configured"
            elif source in {"codex_subscription", "claude_subscription"}:
                binding = load_external_binding(storage_provider)
                if binding is not None:
                    external_state = describe_external_binding(binding)
                    configured = external_state.configured
                    origin = external_state.source
                    state = external_state.state
                    detail = external_state.detail
            elif source == "copilot_oauth":
                from openharness.api.copilot_auth import load_copilot_auth

                if load_copilot_auth():
                    configured = True
                    origin = "file"
                    state = "configured"
            elif load_credential(storage_provider, "api_key"):
                configured = True
                origin = "file"
                state = "configured"
            result[source] = {
                "configured": configured,
                "source": origin,
                "state": state,
                "detail": detail,
                "active": source == active_profile.auth_source,
                "active_profile": active_profile_name,
            }
        return result

    def get_auth_status(self) -> dict[str, Any]:
        """获取所有已知提供商的认证状态。

        遍历所有已知的提供商，检查每个提供商的凭据是否已配置。
        对于不同类型的提供商，使用不同的检测方式：
        - API Key 类提供商：检查环境变量和文件存储。
        - 外部订阅类提供商（Codex、Claude）：检查外部绑定。
        - Copilot：检查 Copilot OAuth 凭据文件。
        - 云服务提供商（Bedrock、Vertex）：检查文件存储的凭据。

        Returns:
            以提供商名称为键的字典，每个值包含：
            - ``configured``: 是否已配置凭据。
            - ``source``: 凭据来源（``env``、``file``、``external``、``missing``）。
            - ``active``: 是否为当前活跃提供商。
        """
        import os

        active = self.get_active_provider()
        result: dict[str, Any] = {}

        for provider in _KNOWN_PROVIDERS:
            configured = False
            source = "missing"

            if provider == "anthropic":
                if os.environ.get("ANTHROPIC_API_KEY"):
                    configured = True
                    source = "env"
                elif load_credential("anthropic", "api_key") or getattr(self.settings, "api_key", ""):
                    configured = True
                    source = "file"

            elif provider == "anthropic_claude":
                binding = load_external_binding(provider)
                if binding is not None:
                    configured = True
                    source = "external"

            elif provider == "openai":
                if os.environ.get("OPENAI_API_KEY"):
                    configured = True
                    source = "env"
                elif load_credential("openai", "api_key"):
                    configured = True
                    source = "file"

            elif provider == "openai_codex":
                binding = load_external_binding(provider)
                if binding is not None:
                    configured = True
                    source = "external"

            elif provider == "copilot":
                from openharness.api.copilot_auth import load_copilot_auth

                if load_copilot_auth():
                    configured = True
                    source = "file"

            elif provider == "dashscope":
                if os.environ.get("DASHSCOPE_API_KEY"):
                    configured = True
                    source = "env"
                elif load_credential("dashscope", "api_key"):
                    configured = True
                    source = "file"

            elif provider == "moonshot":
                if os.environ.get("MOONSHOT_API_KEY"):
                    configured = True
                    source = "env"
                elif load_credential("moonshot", "api_key"):
                    configured = True
                    source = "file"

            elif provider == "minimax":
                if os.environ.get("MINIMAX_API_KEY"):
                    configured = True
                    source = "env"
                elif load_credential("minimax", "api_key"):
                    configured = True
                    source = "file"

            elif provider in ("bedrock", "vertex"):
                # These typically use environment-level credentials (AWS/GCP).
                cred = load_credential(provider, "api_key")
                if cred:
                    configured = True
                    source = "file"

            result[provider] = {
                "configured": configured,
                "source": source,
                "active": provider == active,
            }

        return result

    def get_profile_statuses(self) -> dict[str, Any]:
        """获取所有提供商配置文件的状态信息。

        结合配置文件列表和认证源状态，返回每个配置文件的详细信息，
        包括显示标签、提供商、API 格式、认证源、配置状态、是否活跃、
        Base URL、默认模型及凭据存储槽位等。

        Returns:
            以配置文件名称为键的字典，每个值包含：
            - ``label``: 配置文件的显示标签。
            - ``provider``: 提供商名称。
            - ``api_format``: API 格式。
            - ``auth_source``: 认证源标识。
            - ``configured``: 认证是否已配置。
            - ``auth_state``: 认证状态（``configured`` 或 ``missing``）。
            - ``active``: 是否为当前活跃配置文件。
            - ``base_url``: API 的 Base URL。
            - ``model``: 显示用的模型设置。
            - ``credential_slot``: 凭据存储槽位标识。
        """
        active = self.get_active_profile()
        auth_sources = self.get_auth_source_statuses()
        statuses: dict[str, Any] = {}
        for name, profile in self.list_profiles().items():
            source_status = auth_sources.get(profile.auth_source, {})
            configured = bool(source_status.get("configured"))
            auth_state = str(source_status.get("state", "missing"))
            if auth_source_uses_api_key(profile.auth_source):
                storage_provider = credential_storage_provider_name(name, profile)
                configured = bool(load_credential(storage_provider, "api_key")) or configured
                if not configured and name == active and getattr(self.settings, "api_key", ""):
                    configured = True
                auth_state = "configured" if configured else "missing"
            statuses[name] = {
                "label": display_label_for_profile(name, profile),
                "provider": profile.provider,
                "api_format": profile.api_format,
                "auth_source": profile.auth_source,
                "configured": configured,
                "auth_state": auth_state,
                "active": name == active,
                "base_url": profile.base_url,
                "model": display_model_setting(profile),
                "credential_slot": profile.credential_slot,
            }
        return statuses

    def save_settings(self) -> None:
        """将内存中的设置持久化到磁盘。

        调用 :func:`openharness.config.save_settings` 将当前配置对象
        的状态写入配置文件。
        """
        from openharness.config import save_settings

        save_settings(self.settings)

    def use_profile(self, name: str) -> None:
        """激活指定的提供商配置文件。

        将当前活跃的配置文件切换为指定名称的配置文件，并持久化设置。
        切换后，所有使用该管理器的认证查询将基于新的活跃配置文件。

        Args:
            name: 要激活的配置文件名称。

        Raises:
            ValueError: 当指定的配置文件名称不存在时抛出。
        """
        profiles = self.settings.merged_profiles()
        if name not in profiles:
            raise ValueError(f"Unknown provider profile: {name!r}")
        updated = self.settings.model_copy(update={"active_profile": name}).materialize_active_profile()
        self._settings = updated
        self.save_settings()
        log.info("Switched active profile to %s", name)

    def upsert_profile(self, name: str, profile: ProviderProfile) -> None:
        """创建或替换提供商配置文件。

        若指定名称的配置文件已存在则替换，否则创建新的配置文件。
        操作后自动持久化设置。

        Args:
            name: 配置文件名称。
            profile: 要创建或替换的配置文件对象。
        """
        profiles = self.settings.merged_profiles()
        profiles[name] = profile
        updated = self.settings.model_copy(update={"profiles": profiles})
        self._settings = updated.materialize_active_profile()
        self.save_settings()

    def update_profile(
        self,
        name: str,
        *,
        label: str | None = None,
        provider: str | None = None,
        api_format: str | None = None,
        base_url: str | None = None,
        auth_source: str | None = None,
        default_model: str | None = None,
        last_model: str | None = None,
        credential_slot: str | None = None,
        allowed_models: list[str] | None = None,
        context_window_tokens: int | None = None,
        auto_compact_threshold_tokens: int | None = None,
    ) -> None:
        """就地更新指定配置文件的属性。

        仅更新提供的非空参数对应的属性，未提供的参数保持原值不变。
        若未指定 ``auth_source``，会根据提供商和 API 格式自动推导默认认证源。
        操作后自动持久化设置。

        Args:
            name: 要更新的配置文件名称。
            label: 配置文件的显示标签。
            provider: 提供商名称。
            api_format: API 格式。
            base_url: API 的 Base URL。
            auth_source: 认证源标识。
            default_model: 默认模型名称。
            last_model: 上次使用的模型名称。
            credential_slot: 凭据存储槽位标识。
            allowed_models: 允许使用的模型列表。
            context_window_tokens: 上下文窗口的令牌数。
            auto_compact_threshold_tokens: 自动压缩的令牌阈值。

        Raises:
            ValueError: 当指定的配置文件名称不存在时抛出。
        """
        profiles = self.settings.merged_profiles()
        if name not in profiles:
            raise ValueError(f"Unknown provider profile: {name!r}")
        current = profiles[name]
        next_provider = provider or current.provider
        next_format = api_format or current.api_format
        updates = {
            "label": label or current.label,
            "provider": next_provider,
            "api_format": next_format,
            "base_url": base_url if base_url is not None else current.base_url,
            "auth_source": auth_source or current.auth_source or default_auth_source_for_provider(next_provider, next_format),
            "default_model": default_model or current.default_model,
            "last_model": last_model if last_model is not None else current.last_model,
            "credential_slot": credential_slot if credential_slot is not None else current.credential_slot,
            "allowed_models": allowed_models if allowed_models is not None else current.allowed_models,
            "context_window_tokens": (
                context_window_tokens
                if context_window_tokens is not None
                else current.context_window_tokens
            ),
            "auto_compact_threshold_tokens": (
                auto_compact_threshold_tokens
                if auto_compact_threshold_tokens is not None
                else current.auto_compact_threshold_tokens
            ),
        }
        profiles[name] = current.model_copy(update=updates)
        updated = self.settings.model_copy(update={"profiles": profiles})
        self._settings = updated.materialize_active_profile()
        self.save_settings()

    def remove_profile(self, name: str) -> None:
        """删除非内置的提供商配置文件。

        不允许删除当前活跃的配置文件或内置配置文件。

        Args:
            name: 要删除的配置文件名称。

        Raises:
            ValueError: 当尝试删除活跃配置文件、内置配置文件或不存在的配置文件时抛出。
        """
        if name == self.get_active_profile():
            raise ValueError("Cannot remove the active profile.")
        if name in builtin_provider_profile_names():
            raise ValueError(f"Cannot remove built-in profile: {name}")
        profiles = self.settings.merged_profiles()
        if name not in profiles:
            raise ValueError(f"Unknown provider profile: {name!r}")
        del profiles[name]
        updated = self.settings.model_copy(update={"profiles": profiles})
        self._settings = updated.materialize_active_profile()
        self.save_settings()

    def switch_auth_source(self, auth_source: str, *, profile_name: str | None = None) -> None:
        """切换指定配置文件的认证源。

        将指定配置文件的认证源切换为新的认证源。若未指定配置文件名称，
        则切换当前活跃配置文件的认证源。

        Args:
            auth_source: 新的认证源标识，必须是已知的认证源之一。
            profile_name: 要切换认证源的配置文件名称，若为 ``None`` 则使用当前活跃配置文件。

        Raises:
            ValueError: 当认证源标识不在已知列表中时抛出。
        """
        if auth_source not in _AUTH_SOURCES:
            raise ValueError(f"Unknown auth source: {auth_source!r}. Known auth sources: {_AUTH_SOURCES}")
        target = profile_name or self.get_active_profile()
        self.update_profile(target, auth_source=auth_source)

    def switch_provider(self, name: str) -> None:
        """向后兼容的提供商/配置文件/认证源统一切换入口。

        按以下优先级匹配并切换：
        1. 若 ``name`` 是认证源名称，则切换认证源。
        2. 若 ``name`` 是配置文件名称，则激活该配置文件。
        3. 若 ``name`` 是已知提供商名称，则激活对应的默认配置文件。

        Args:
            name: 认证源名称、配置文件名称或提供商名称。

        Raises:
            ValueError: 当 ``name`` 不匹配任何已知标识时抛出。
        """
        if name in _AUTH_SOURCES:
            self.switch_auth_source(name)
            return
        profiles = self.list_profiles()
        if name in profiles:
            self.use_profile(name)
            return
        if name in _KNOWN_PROVIDERS:
            self.use_profile(_PROFILE_BY_PROVIDER.get(name, "openai-compatible" if name == "openai" else "claude-api"))
            return
        raise ValueError(
            f"Unknown provider or auth source: {name!r}. "
            f"Known providers: {_KNOWN_PROVIDERS}; auth sources: {_AUTH_SOURCES}"
        )

    def store_credential(self, provider: str, key: str, value: str) -> None:
        """为指定提供商存储凭据。

        通过 :mod:`openharness.auth.storage` 将凭据持久化存储。
        若存储的是当前活跃提供商的 API Key，还会同步更新内存中的
        配置对象并持久化设置，以保持向后兼容性。

        Args:
            provider: 提供商名称。
            key: 凭据键名（如 ``"api_key"``）。
            value: 凭据值。
        """
        store_credential(provider, key, value)
        # Keep the flattened active settings snapshot aligned for compatibility.
        if key == "api_key" and provider == auth_source_provider_name(self.settings.resolve_profile()[1].auth_source):
            try:
                updated = self.settings.model_copy(update={"api_key": value})
                self._settings = updated.materialize_active_profile()
                self.save_settings()
            except Exception as exc:
                log.warning("Could not sync api_key to settings: %s", exc)

    def store_profile_credential(self, profile_name: str, key: str, value: str) -> None:
        """为指定配置文件存储凭据。

        使用配置文件的凭据存储命名空间（可能包含 ``credential_slot``）
        来存储凭据，避免不同配置文件之间的凭据冲突。
        若存储的是当前活跃配置文件的 API Key，还会同步更新内存中的
        配置对象并持久化设置。

        Args:
            profile_name: 配置文件名称。
            key: 凭据键名（如 ``"api_key"``）。
            value: 凭据值。

        Raises:
            ValueError: 当指定的配置文件不存在时抛出。
        """
        profile = self.list_profiles().get(profile_name)
        if profile is None:
            raise ValueError(f"Unknown provider profile: {profile_name!r}")
        storage_provider = credential_storage_provider_name(profile_name, profile)
        store_credential(storage_provider, key, value)
        if key == "api_key" and profile_name == self.get_active_profile():
            try:
                updated = self.settings.model_copy(update={"api_key": value})
                self._settings = updated.materialize_active_profile()
                self.save_settings()
            except Exception as exc:
                log.warning("Could not sync api_key to settings: %s", exc)

    def clear_credential(self, provider: str) -> None:
        """清除指定提供商的所有已存储凭据。

        删除存储后端中该提供商的所有凭据数据。若清除的是当前活跃
        提供商的凭据，还会同步清除内存配置对象中的 API Key 并持久化设置。

        Args:
            provider: 提供商名称。
        """
        clear_provider_credentials(provider)
        # Also clear api_key in settings if this is the active provider.
        if provider == auth_source_provider_name(self.settings.resolve_profile()[1].auth_source):
            try:
                updated = self.settings.model_copy(update={"api_key": ""})
                self._settings = updated.materialize_active_profile()
                self.save_settings()
            except Exception as exc:
                log.warning("Could not clear api_key from settings: %s", exc)

    def clear_profile_credential(self, profile_name: str) -> None:
        """清除指定配置文件的所有已存储凭据。

        使用配置文件的凭据存储命名空间来定位并删除凭据。
        若清除的是当前活跃配置文件的凭据，还会同步清除内存配置
        对象中的 API Key 并持久化设置。

        Args:
            profile_name: 配置文件名称。

        Raises:
            ValueError: 当指定的配置文件不存在时抛出。
        """
        profile = self.list_profiles().get(profile_name)
        if profile is None:
            raise ValueError(f"Unknown provider profile: {profile_name!r}")
        clear_provider_credentials(credential_storage_provider_name(profile_name, profile))
        if profile_name == self.get_active_profile():
            try:
                updated = self.settings.model_copy(update={"api_key": ""})
                self._settings = updated.materialize_active_profile()
                self.save_settings()
            except Exception as exc:
                log.warning("Could not clear api_key from settings: %s", exc)
