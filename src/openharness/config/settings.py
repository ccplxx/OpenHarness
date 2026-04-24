"""设置模型与加载逻辑模块（openharness.config.settings）

本模块实现 OpenHarness 的主设置模型（Settings）及其加载、保存和解析逻辑。
设置系统采用分层解析策略，按以下优先级（从高到低）合并配置：

    1. CLI 命令行参数（通过 merge_cli_overrides 应用）
    2. 环境变量（ANTHROPIC_API_KEY、OPENHARNESS_MODEL 等）
    3. 配置文件（~/.openharness/settings.json）
    4. 内置默认值

核心概念：
    - Provider Profile（提供商配置文件）：命名的工作流配置，描述如何连接
      特定的 AI 提供商（如 Anthropic、OpenAI、Copilot 等），包含 API 格式、
      认证方式、默认模型、基础 URL 等信息。
    - Flat Fields（扁平字段）：历史遗留的顶层设置字段（provider、api_format、
      base_url、model 等），系统通过 sync/materialize 方法在扁平字段与
      Profile 层之间双向同步，确保向后兼容。

模块还提供模型别名解析（如 "sonnet" → "claude-sonnet-4-6"）、
认证源映射、ANSI 转义序列清理等辅助功能。

主要类：
    - Settings：主设置模型，包含所有配置项。
    - ProviderProfile：命名提供商工作流配置。
    - ResolvedAuth：解析后的认证材料。
    - PermissionSettings / MemorySettings / SandboxSettings：子领域配置。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from openharness.hooks.schemas import HookDefinition
from openharness.mcp.types import McpServerConfig
from openharness.permissions.modes import PermissionMode
from openharness.utils.file_lock import exclusive_file_lock
from openharness.utils.fs import atomic_write_text


# ANSI 转义序列正则模式，用于匹配终端格式化代码（如粗体、颜色等）
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi_escape_sequences(text: str) -> str:
    """移除文本中的 ANSI 转义序列。

    某些环境变量可能包含终端格式化代码（如 '[1m' 表示粗体），
    这些代码可能破坏 API 请求。本函数用于在将环境变量值用于
    API 调用之前进行清理。

    参数：
        text: 可能包含 ANSI 转义序列的输入文本。

    返回：
        去除所有 ANSI 转义序列后的干净文本。
    """
    if not text:
        return text
    return _ANSI_ESCAPE_PATTERN.sub("", text)


class PathRuleConfig(BaseModel):
    """基于 glob 模式的路径权限规则配置。

    与 openharness.permissions.checker.PathRule 对应，但使用 Pydantic
    BaseModel 以便从 JSON 配置文件中反序列化。

    属性：
        pattern: fnmatch 语法的路径匹配模式，如 "*.env"、"*/secrets/*"。
        allow: True 表示允许访问匹配的路径，False 表示拒绝，默认为 True。
    """

    pattern: str
    allow: bool = True


class PermissionSettings(BaseModel):
    """权限模式配置。

    控制工具执行时的权限行为，包括运行模式、工具白名单/黑名单、
    路径规则和命令拒绝模式。这些设置会被 PermissionChecker 使用。

    属性：
        mode: 权限运行模式（DEFAULT / PLAN / FULL_AUTO）。
        allowed_tools: 明确允许的工具名称列表。
        denied_tools: 明确拒绝的工具名称列表。
        path_rules: 基于路径 glob 模式的权限规则列表。
        denied_commands: 基于命令 glob 模式的拒绝规则列表。
    """

    mode: PermissionMode = PermissionMode.DEFAULT
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    path_rules: list[PathRuleConfig] = Field(default_factory=list)
    denied_commands: list[str] = Field(default_factory=list)


class MemorySettings(BaseModel):
    """记忆系统配置。

    控制 OpenHarness 记忆子系统的行为，包括是否启用、文件数量限制、
    上下文窗口大小和自动压缩阈值等。

    属性：
        enabled: 是否启用记忆系统，默认为 True。
        max_files: 记忆系统追踪的最大文件数，默认为 5。
        max_entrypoint_lines: 入口点文件的最大行数，默认为 200。
        context_window_tokens: 上下文窗口的 Token 数量限制，None 表示不限制。
        auto_compact_threshold_tokens: 自动压缩触发的 Token 阈值，None 表示不自动压缩。
    """

    enabled: bool = True
    max_files: int = 5
    max_entrypoint_lines: int = 200
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None


class SandboxNetworkSettings(BaseModel):
    """操作系统级网络限制配置，传递给 sandbox-runtime。

    控制沙盒环境中允许或拒绝的网络域名访问。

    属性：
        allowed_domains: 允许访问的域名列表。
        denied_domains: 拒绝访问的域名列表。
    """

    allowed_domains: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)


class SandboxFilesystemSettings(BaseModel):
    """操作系统级文件系统限制配置，传递给 sandbox-runtime。

    控制沙盒环境中文件系统的读写权限。默认允许写入当前目录。

    属性：
        allow_read: 允许读取的路径列表。
        deny_read: 拒绝读取的路径列表。
        allow_write: 允许写入的路径列表，默认为 ["."]（当前目录）。
        deny_write: 拒绝写入的路径列表。
    """

    allow_read: list[str] = Field(default_factory=list)
    deny_read: list[str] = Field(default_factory=list)
    allow_write: list[str] = Field(default_factory=lambda: ["."])
    deny_write: list[str] = Field(default_factory=list)


class DockerSandboxSettings(BaseModel):
    """Docker 特定的沙盒配置。

    控制沙盒 Docker 容器的镜像、资源限制和挂载等参数。

    属性：
        image: 沙盒 Docker 镜像名称，默认为 "openharness-sandbox:latest"。
        auto_build_image: 是否自动构建沙盒镜像，默认为 True。
        cpu_limit: CPU 使用限制（0.0 表示不限制）。
        memory_limit: 内存使用限制字符串（如 "512m"），空字符串表示不限制。
        extra_mounts: 额外的 Docker 挂载路径列表。
        extra_env: 额外的环境变量字典。
    """

    image: str = "openharness-sandbox:latest"
    auto_build_image: bool = True
    cpu_limit: float = 0.0
    memory_limit: str = ""
    extra_mounts: list[str] = Field(default_factory=list)
    extra_env: dict[str, str] = Field(default_factory=dict)


class SandboxSettings(BaseModel):
    """沙盒运行时集成配置。

    控制 OpenHarness 与 sandbox-runtime 的集成行为，包括是否启用、
    后端类型、网络和文件系统限制以及 Docker 特定配置。

    属性：
        enabled: 是否启用沙盒，默认为 False。
        backend: 沙盒后端类型，默认为 "srt"（sandbox-runtime）。
        fail_if_unavailable: 沙盒不可用时是否失败，默认为 False。
        enabled_platforms: 允许的平台列表，空列表表示所有平台。
        network: 网络限制配置。
        filesystem: 文件系统限制配置。
        docker: Docker 特定配置。
    """

    enabled: bool = False
    backend: str = "srt"
    fail_if_unavailable: bool = False
    enabled_platforms: list[str] = Field(default_factory=list)
    network: SandboxNetworkSettings = Field(default_factory=SandboxNetworkSettings)
    filesystem: SandboxFilesystemSettings = Field(default_factory=SandboxFilesystemSettings)
    docker: DockerSandboxSettings = Field(default_factory=DockerSandboxSettings)


class ProviderProfile(BaseModel):
    """命名提供商工作流配置。

    描述如何连接特定的 AI 提供商，包含 API 格式、认证方式、默认模型、
    基础 URL 等信息。每个 Profile 以唯一名称标识，用户可以在多个
    Profile 之间切换以使用不同的 AI 服务。

    属性：
        label: 用户界面显示的友好名称。
        provider: 提供商标识符（如 "anthropic"、"openai"、"copilot" 等）。
        api_format: API 协议格式（"anthropic"、"openai" 或 "copilot"）。
        auth_source: 认证来源标识（如 "anthropic_api_key"、"copilot_oauth" 等）。
        default_model: 默认模型标识符。
        base_url: API 基础 URL，None 表示使用提供商默认地址。
        last_model: 用户最后选择的模型，None 表示使用 default_model。
        credential_slot: 自定义凭据存储槽位，None 表示使用默认存储。
        allowed_models: 允许使用的模型列表，空列表表示不限制。
        context_window_tokens: 上下文窗口 Token 数量，None 表示使用模型默认值。
        auto_compact_threshold_tokens: 自动压缩阈值，None 表示不自动压缩。
    """

    label: str
    provider: str
    api_format: str
    auth_source: str
    default_model: str
    base_url: str | None = None
    last_model: str | None = None
    credential_slot: str | None = None
    allowed_models: list[str] = Field(default_factory=list)
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None

    @property
    def resolved_model(self) -> str:
        """返回该 Profile 当前活跃的模型标识符。

        优先使用 last_model（用户最后选择的模型），若为空则回退到
        default_model。模型名称通过 resolve_model_setting 进行解析，
        支持别名（如 "sonnet" → "claude-sonnet-4-6"）。

        返回：
            解析后的具体模型 ID 字符串。
        """
        return resolve_model_setting(
            (self.last_model or "").strip() or self.default_model,
            self.provider,
            default_model=self.default_model,
        )


@dataclass(frozen=True)
class ResolvedAuth:
    """解析后的认证材料，用于构造 API 客户端。

    在运行时由 Settings.resolve_auth() 生成，包含已解析的提供商名称、
    认证类型、凭据值和来源信息。该类为不可变数据类，确保认证信息
    在传递过程中不被意外修改。

    属性：
        provider: 提供商标识符。
        auth_kind: 认证类型（如 "api_key"、"oauth_device" 等）。
        value: 认证凭据的值（API 密钥或令牌字符串）。
        source: 凭据来源描述（如 "env:ANTHROPIC_API_KEY"、"file:anthropic" 等）。
        state: 认证状态，默认为 "configured"。
    """

    provider: str
    auth_kind: str
    value: str
    source: str
    state: str = "configured"


# Claude 模型别名选项元组，用于用户界面展示。
# 每个元素为 (别名, 显示名称, 描述) 的三元组。
CLAUDE_MODEL_ALIAS_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("default", "Default", "Recommended model for this profile"),
    ("best", "Best", "Most capable available model"),
    ("sonnet", "Sonnet", "Latest Sonnet for everyday coding"),
    ("opus", "Opus", "Latest Opus for complex reasoning"),
    ("haiku", "Haiku", "Fastest Claude model"),
    ("sonnet[1m]", "Sonnet (1M context)", "Latest Sonnet with 1M context"),
    ("opus[1m]", "Opus (1M context)", "Latest Opus with 1M context"),
    ("opusplan", "Opus Plan Mode", "Use Opus in plan mode and Sonnet otherwise"),
)

# Claude 模型别名到具体模型 ID 的映射字典。
# 用于将用户友好的别名（如 "sonnet"、"opus"）解析为
# Anthropic API 可识别的具体模型标识符。
_CLAUDE_ALIAS_TARGETS: dict[str, str] = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6",
    "haiku": "claude-haiku-4-5",
    "sonnet[1m]": "claude-sonnet-4-6[1m]",
    "opus[1m]": "claude-opus-4-6[1m]",
}


def normalize_anthropic_model_name(model: str) -> str:
    """将 Anthropic 模型名称标准化，与 Hermes 的处理方式一致。

    处理步骤：
        1. 去除首尾空白字符。
        2. 若名称以 ``anthropic/`` 前缀开头，则去除该前缀。
        3. 若名称以 ``claude-`` 开头，将点号分隔符转换为 Anthropic
           标准的连字符形式（如 "claude.3.5" → "claude-3-5"）。

    参数：
        model: 待标准化的模型名称字符串。

    返回：
        标准化后的模型名称。
    """
    normalized = model.strip()
    lower = normalized.lower()
    if lower.startswith("anthropic/"):
        normalized = normalized[len("anthropic/"):]
        lower = normalized.lower()
    if lower.startswith("claude-"):
        return normalized.replace(".", "-")
    return normalized


def default_provider_profiles() -> dict[str, ProviderProfile]:
    """返回内置的提供商工作流配置目录。

    包含 OpenHarness 预定义的所有提供商配置，每个配置以唯一名称为键。
    用户可以在设置中覆盖这些配置或添加自定义配置。

    返回：
        名称到 ProviderProfile 的映射字典。
    """
    return {
        "claude-api": ProviderProfile(
            label="Anthropic-Compatible API",
            provider="anthropic",
            api_format="anthropic",
            auth_source="anthropic_api_key",
            default_model="claude-sonnet-4-6",
        ),
        "claude-subscription": ProviderProfile(
            label="Claude Subscription",
            provider="anthropic_claude",
            api_format="anthropic",
            auth_source="claude_subscription",
            default_model="claude-sonnet-4-6",
        ),
        "openai-compatible": ProviderProfile(
            label="OpenAI-Compatible API",
            provider="openai",
            api_format="openai",
            auth_source="openai_api_key",
            default_model="gpt-5.4",
        ),
        "codex": ProviderProfile(
            label="Codex Subscription",
            provider="openai_codex",
            api_format="openai",
            auth_source="codex_subscription",
            default_model="gpt-5.4",
        ),
        "copilot": ProviderProfile(
            label="GitHub Copilot",
            provider="copilot",
            api_format="copilot",
            auth_source="copilot_oauth",
            default_model="gpt-5.4",
        ),
        "moonshot": ProviderProfile(
            label="Moonshot (Kimi)",
            provider="moonshot",
            api_format="openai",
            auth_source="moonshot_api_key",
            default_model="kimi-k2.5",
            base_url="https://api.moonshot.cn/v1",
        ),
        "gemini": ProviderProfile(
            label="Google Gemini",
            provider="gemini",
            api_format="openai",
            auth_source="gemini_api_key",
            default_model="gemini-2.5-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        ),
        "minimax": ProviderProfile(
            label="MiniMax",
            provider="minimax",
            api_format="openai",
            auth_source="minimax_api_key",
            default_model="MiniMax-M2.7",
            base_url="https://api.minimax.io/v1",
        ),
    }


def builtin_provider_profile_names() -> set[str]:
    """返回内置提供商配置文件的名称集合。

    返回：
        内置 Profile 名称的集合。
    """
    return set(default_provider_profiles())


def display_label_for_profile(profile_name: str, profile: ProviderProfile) -> str:
    """返回 Profile 的用户界面显示标签。

    对于内置 Profile，始终使用当前内置目录中的标签，以确保旧的
    持久化设置不会在菜单中显示过时的文字。自定义 Profile 则
    使用其自身的 label 字段。

    参数：
        profile_name: Profile 的唯一名称。
        profile: Profile 实例。

    返回：
        用于用户界面显示的标签字符串。
    """
    builtin = default_provider_profiles().get(profile_name)
    if builtin is not None:
        return builtin.label
    return profile.label


def is_claude_family_provider(provider: str) -> bool:
    """判断提供商是否属于 Claude/Anthropic 工作流家族。

    参数：
        provider: 提供商标识符。

    返回：
        若提供商为 "anthropic" 或 "anthropic_claude" 则返回 True，否则返回 False。
    """
    return provider in {"anthropic", "anthropic_claude"}


def display_model_setting(profile: ProviderProfile) -> str:
    """返回 Profile 的用户界面模型设置显示值。

    对于 Claude 家族提供商，若未配置具体模型则显示 "default"；
    其他提供商显示 last_model 或 default_model。

    参数：
        profile: ProviderProfile 实例。

    返回：
        用于用户界面显示的模型设置字符串。
    """
    configured = (profile.last_model or "").strip()
    if not configured and is_claude_family_provider(profile.provider):
        return "default"
    return configured or profile.default_model


def resolve_model_setting(
    model_setting: str,
    provider: str,
    *,
    default_model: str | None = None,
    permission_mode: str | None = None,
) -> str:
    """将用户可见的模型设置解析为具体的运行时模型 ID。

    支持模型别名解析，如 "default" 回退到 default_model，
    "best" 解析为最强模型，"opusplan" 根据权限模式在 Opus 和 Sonnet
    之间切换。对于 Claude 家族，还支持 "sonnet"、"opus"、"haiku"
    等别名以及 "[1m]" 后缀表示扩展上下文窗口。

    参数：
        model_setting: 用户配置的模型名称或别名。
        provider: 提供商标识符，影响别名解析策略。
        default_model: 默认模型 ID，当 model_setting 为空或 "default" 时使用。
        permission_mode: 当前权限模式值，影响 "opusplan" 别名的解析。

    返回：
        解析后的具体模型 ID 字符串。
    """
    configured = model_setting.strip()
    normalized = configured.lower()

    if not configured or normalized == "default":
        fallback = (default_model or "").strip()
        if fallback and fallback.lower() != "default":
            return resolve_model_setting(
                fallback,
                provider,
                default_model=None,
                permission_mode=permission_mode,
            )
        if is_claude_family_provider(provider):
            return _CLAUDE_ALIAS_TARGETS["sonnet"]
        return "gpt-5.4"

    if is_claude_family_provider(provider):
        if normalized == "best":
            return _CLAUDE_ALIAS_TARGETS["opus"]
        if normalized == "opusplan":
            if permission_mode == PermissionMode.PLAN.value:
                return _CLAUDE_ALIAS_TARGETS["opus"]
            return _CLAUDE_ALIAS_TARGETS["sonnet"]
        if normalized in _CLAUDE_ALIAS_TARGETS:
            return _CLAUDE_ALIAS_TARGETS[normalized]
        return normalize_anthropic_model_name(configured)

    if provider in {"openai", "openai_codex", "copilot"} and normalized in {"default", "best"}:
        return "gpt-5.4"

    return configured


def auth_source_provider_name(auth_source: str) -> str:
    """将认证源标识映射为存储/运行时提供商标识。

    不同的认证源（如 "anthropic_api_key"、"claude_subscription"）
    对应不同的存储命名空间。此函数建立了认证源到提供商名称的
    标准映射关系。

    参数：
        auth_source: 认证源标识符。

    返回：
        对应的存储/运行时提供商标识。若映射表中无对应项，
        则原样返回 auth_source。
    """
    mapping = {
        "anthropic_api_key": "anthropic",
        "openai_api_key": "openai",
        "codex_subscription": "openai_codex",
        "claude_subscription": "anthropic_claude",
        "copilot_oauth": "copilot",
        "dashscope_api_key": "dashscope",
        "bedrock_api_key": "bedrock",
        "vertex_api_key": "vertex",
        "moonshot_api_key": "moonshot",
        "gemini_api_key": "gemini",
        "minimax_api_key": "minimax",
    }
    return mapping.get(auth_source, auth_source)


def auth_source_uses_api_key(auth_source: str) -> bool:
    """判断认证源是否基于用户提供的 API 密钥。

    通过检查认证源标识是否以 "_api_key" 后缀结尾来判断。
    API 密钥类认证与订阅/OAuth 类认证的处理逻辑不同。

    参数：
        auth_source: 认证源标识符。

    返回：
        若认证源以 "_api_key" 结尾则返回 True，否则返回 False。
    """
    return auth_source.endswith("_api_key")


def credential_storage_provider_name(profile_name: str, profile: ProviderProfile) -> str:
    """返回该 Profile 凭据使用的存储命名空间。

    内置 API 密钥流程默认使用提供商级存储。自定义兼容 Profile
    可设置 ``credential_slot`` 以绑定独立的密钥存储槽位，
    实现同一提供商下多个 Profile 各自持有不同的 API 密钥。

    参数：
        profile_name: Profile 的唯一名称（当前未使用，保留用于未来扩展）。
        profile: ProviderProfile 实例。

    返回：
        凭据存储的命名空间字符串。
    """
    del profile_name
    if auth_source_uses_api_key(profile.auth_source) and profile.credential_slot:
        return f"profile:{profile.credential_slot}"
    return auth_source_provider_name(profile.auth_source)


def default_auth_source_for_provider(provider: str, api_format: str | None = None) -> str:
    """推断提供商/后端的默认认证源。

    根据提供商标识和 API 格式，返回最合适的默认认证方式。
    例如 "anthropic_claude" → "claude_subscription"，
    "copilot" → "copilot_oauth"。

    参数：
        provider: 提供商标识符。
        api_format: API 格式（可选），用于辅助推断。

    返回：
        默认的认证源标识符。
    """
    if provider == "anthropic_claude":
        return "claude_subscription"
    if provider == "openai_codex":
        return "codex_subscription"
    if provider == "copilot":
        return "copilot_oauth"
    if provider == "dashscope":
        return "dashscope_api_key"
    if provider == "bedrock":
        return "bedrock_api_key"
    if provider == "vertex":
        return "vertex_api_key"
    if provider == "moonshot":
        return "moonshot_api_key"
    if provider == "gemini":
        return "gemini_api_key"
    if provider == "minimax":
        return "minimax_api_key"
    if provider == "openai" or api_format == "openai":
        return "openai_api_key"
    return "anthropic_api_key"


def _slugify_profile_name(value: str) -> str:
    """将字符串转换为 URL 友好的 slug 形式。

    字母数字字符保留并转为小写，其他字符替换为连字符，
    连续的连字符合并为一个，首尾的连字符去除。
    若结果为空则返回 "custom"。

    参数：
        value: 待转换的字符串。

    返回：
        slug 形式的字符串。
    """
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned or "custom"


def _infer_profile_name_from_flat_settings(settings: "Settings") -> str:
    """从扁平设置字段推断 Profile 名称。

    根据 Settings 中的 provider、api_format 和 base_url 字段，
    推断最匹配的内置 Profile 名称。若无法匹配内置 Profile，
    则基于 base_url 或 provider 生成 slug 形式的名称。

    参数：
        settings: Settings 实例。

    返回：
        推断的 Profile 名称字符串。
    """
    provider = (settings.provider or "").strip()
    if provider == "openai_codex":
        return "codex"
    if provider == "anthropic_claude":
        return "claude-subscription"
    if provider == "copilot" or settings.api_format == "copilot":
        return "copilot"
    if provider == "openai" and not settings.base_url:
        return "openai-compatible"
    if provider == "anthropic" and not settings.base_url:
        return "claude-api"
    if settings.base_url:
        return _slugify_profile_name(Path(settings.base_url).name or settings.base_url)
    if provider:
        return _slugify_profile_name(provider)
    return "claude-api"


def _profile_from_flat_settings(settings: "Settings") -> tuple[str, ProviderProfile]:
    """从扁平设置字段构建 ProviderProfile。

    用于兼容旧版配置格式，将顶层 provider / api_format / base_url / model
    等字段转换为 Profile 形式。优先匹配内置 Profile（若字段完全一致），
    否则创建新的自定义 Profile。

    参数：
        settings: Settings 实例。

    返回：
        (Profile 名称, ProviderProfile 实例) 的元组。
    """
    defaults = default_provider_profiles()
    name = _infer_profile_name_from_flat_settings(settings)
    existing = defaults.get(name)
    if existing is not None and (
        existing.provider == settings.provider or not settings.provider
    ) and (
        existing.api_format == settings.api_format
    ) and (
        existing.base_url == settings.base_url
    ):
        profile = existing.model_copy(
            update={
                "last_model": settings.model or existing.resolved_model,
            }
        )
        return name, profile

    provider = settings.provider or ("copilot" if settings.api_format == "copilot" else ("openai" if settings.api_format == "openai" else "anthropic"))
    profile = ProviderProfile(
        label=f"Imported {provider}",
        provider=provider,
        api_format=settings.api_format,
        auth_source=default_auth_source_for_provider(provider, settings.api_format),
        default_model=settings.model or defaults.get("claude-api", ProviderProfile(
            label="Claude API",
            provider="anthropic",
            api_format="anthropic",
            auth_source="anthropic_api_key",
            default_model="sonnet",
        )).default_model,
        last_model=settings.model or None,
        base_url=settings.base_url,
    )
    return name, profile


class Settings(BaseModel):
    """OpenHarness 主设置模型。

    包含所有配置项，涵盖 API 连接、行为控制、UI 偏好等方面。
    支持通过 Profile 系统管理多个 AI 提供商配置，同时保持
    与旧版扁平字段格式的向后兼容。

    设置解析优先级（从高到低）：
        1. CLI 命令行参数
        2. 环境变量
        3. 配置文件（~/.openharness/settings.json）
        4. 内置默认值

    属性分组：
        API 配置：api_key, model, max_tokens, base_url, timeout, api_format,
                  provider, active_profile, profiles, max_turns 等。
        行为配置：system_prompt, permission, hooks, memory, sandbox,
                  enabled_plugins, allow_project_plugins, mcp_servers 等。
        UI 配置：theme, output_style, vim_mode, voice_mode, fast_mode,
                 effort, passes, verbose 等。
    """

    # API configuration
    api_key: str = ""
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 16384
    base_url: str | None = None
    timeout: float = 30.0
    context_window_tokens: int | None = None
    auto_compact_threshold_tokens: int | None = None
    api_format: str = "anthropic"  # "anthropic", "openai", or "copilot"
    provider: str = ""
    active_profile: str = "claude-api"
    profiles: dict[str, ProviderProfile] = Field(default_factory=default_provider_profiles)
    max_turns: int = 200

    # Behavior
    system_prompt: str | None = None
    permission: PermissionSettings = Field(default_factory=PermissionSettings)
    hooks: dict[str, list[HookDefinition]] = Field(default_factory=dict)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    enabled_plugins: dict[str, bool] = Field(default_factory=dict)
    allow_project_plugins: bool = False
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict)

    # UI
    theme: str = "default"
    output_style: str = "default"
    vim_mode: bool = False
    voice_mode: bool = False
    fast_mode: bool = False
    effort: str = "medium"
    passes: int = 1
    verbose: bool = False

    def merged_profiles(self) -> dict[str, ProviderProfile]:
        """返回用户保存的 Profile 与内置目录合并后的结果。

        以内置目录为基础，用用户自定义的 Profile 覆盖同名条目。
        对于内置 Profile，若用户未设置 base_url 而内置版本有，
        则自动继承内置的 base_url，确保更新后不会丢失默认地址。

        返回：
            合并后的名称到 ProviderProfile 映射字典。
        """
        merged = default_provider_profiles()
        for name, raw_profile in self.profiles.items():
            profile = (
                raw_profile.model_copy(deep=True)
                if isinstance(raw_profile, ProviderProfile)
                else ProviderProfile.model_validate(raw_profile)
            )
            builtin = merged.get(name)
            if builtin is not None and profile.base_url is None and builtin.base_url is not None:
                profile = profile.model_copy(update={"base_url": builtin.base_url})
            merged[name] = profile
        return merged

    def resolve_profile(self, name: str | None = None) -> tuple[str, ProviderProfile]:
        """解析并返回当前活跃的提供商 Profile。

        若指定名称不存在于已合并的 Profile 目录中，则从扁平设置字段
        构建一个回退 Profile 并加入目录。

        参数：
            name: 指定的 Profile 名称，None 则使用 active_profile 字段。

        返回：
            (Profile 名称, ProviderProfile 实例) 的元组。
        """
        profiles = self.merged_profiles()
        profile_name = (name or self.active_profile or "").strip() or "claude-api"
        if profile_name not in profiles:
            fallback_name, fallback = _profile_from_flat_settings(self)
            profiles[fallback_name] = fallback
            profile_name = fallback_name
        return profile_name, profiles[profile_name].model_copy(deep=True)

    def materialize_active_profile(self) -> Settings:
        """将活跃 Profile 的配置投影回扁平设置字段。

        Profile 系统是配置的"真实来源"，但许多代码仍读取顶层的
        provider / api_format / base_url / model 等扁平字段。此方法
        将 Profile 中的值写入这些扁平字段，确保所有代码路径
        都能获取到正确的配置。

        返回：
            更新了扁平字段的新的 Settings 实例。
        """
        profile_name, profile = self.resolve_profile()
        configured_model = (profile.last_model or "").strip() or profile.default_model
        return self.model_copy(
            update={
                "active_profile": profile_name,
                "profiles": self.merged_profiles(),
                "provider": profile.provider,
                "api_format": profile.api_format,
                "base_url": profile.base_url,
                "context_window_tokens": profile.context_window_tokens,
                "auto_compact_threshold_tokens": profile.auto_compact_threshold_tokens,
                "model": resolve_model_setting(
                    configured_model,
                    profile.provider,
                    default_model=profile.default_model,
                    permission_mode=self.permission.mode.value,
                ),
            }
        )

    def sync_active_profile_from_flat_fields(self) -> Settings:
        """将扁平提供商字段同步回活跃 Profile。

        保持向后兼容性——对于仍通过直接设置顶层 provider / api_format /
        base_url / model 字段来构造 Settings 的调用方，此方法将这些
        扁平字段的值回写到活跃 Profile 中，确保 Profile 层与扁平层
        保持一致。

        返回：
            同步了 Profile 字段的新的 Settings 实例。
        """
        profile_name, profile = self.resolve_profile()
        next_provider = (self.provider or "").strip() or profile.provider
        next_api_format = (self.api_format or "").strip() or profile.api_format
        next_base_url = self.base_url if self.base_url is not None else profile.base_url
        next_context_window_tokens = (
            self.context_window_tokens
            if self.context_window_tokens is not None
            else profile.context_window_tokens
        )
        next_auto_compact_threshold_tokens = (
            self.auto_compact_threshold_tokens
            if self.auto_compact_threshold_tokens is not None
            else profile.auto_compact_threshold_tokens
        )
        flat_model = (self.model or "").strip()
        resolved_profile_model = resolve_model_setting(
            (profile.last_model or "").strip() or profile.default_model,
            profile.provider,
            default_model=profile.default_model,
            permission_mode=self.permission.mode.value,
        )
        if flat_model and flat_model != resolved_profile_model:
            next_model = flat_model
        else:
            next_model = profile.last_model
        current_default_auth = default_auth_source_for_provider(profile.provider, profile.api_format)
        next_auth_source = profile.auth_source
        if not next_auth_source or next_auth_source == current_default_auth:
            next_auth_source = default_auth_source_for_provider(next_provider, next_api_format)

        updated_profile = profile.model_copy(
            update={
                "provider": next_provider,
                "api_format": next_api_format,
                "base_url": next_base_url,
                "auth_source": next_auth_source,
                "last_model": next_model,
                "context_window_tokens": next_context_window_tokens,
                "auto_compact_threshold_tokens": next_auto_compact_threshold_tokens,
            }
        )
        profiles = self.merged_profiles()
        profiles[profile_name] = updated_profile
        return self.model_copy(
            update={
                "active_profile": profile_name,
                "profiles": profiles,
            }
        )

    def resolve_api_key(self) -> str:
        """解析当前提供商的 API 密钥。

        按优先级依次查找：实例值 > 环境变量 > 空。

        特殊处理：
            - Codex 提供商：通过 resolve_auth() 获取凭据。
            - Claude 订阅：抛出异常，提示使用 resolve_auth()。
            - Copilot：返回占位字符串 "copilot-managed"，密钥由
              ``oh auth copilot-login`` 独立管理。
            - 其他：依次检查实例 api_key、ANTHROPIC_API_KEY 环境变量、
              OPENAI_API_KEY 环境变量。

        返回：
            API 密钥字符串。

        异常：
            ValueError: 未找到任何有效的 API 密钥时抛出。
        """
        profile_name, profile = self.resolve_profile()
        del profile_name
        if profile.provider == "openai_codex":
            return self.resolve_auth().value
        if profile.provider == "anthropic_claude":
            raise ValueError(
                "Current provider uses Anthropic auth tokens instead of API keys. "
                "Use resolve_auth() for runtime credential resolution."
            )
        # Copilot format manages its own auth; skip normal key resolution.
        if profile.api_format == "copilot":
            return "copilot-managed"

        if self.api_key:
            return self.api_key

        env_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if env_key:
            return env_key

        # Also check OPENAI_API_KEY for openai-format providers
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        if openai_key:
            return openai_key

        raise ValueError(
            "No API key found. Set ANTHROPIC_API_KEY (or OPENAI_API_KEY for openai-format "
            "providers) environment variable, or configure api_key in "
            "~/.openharness/settings.json"
        )

    def resolve_auth(self) -> ResolvedAuth:
        """解析当前提供商的认证信息，包括订阅桥接。

        根据认证源（auth_source）类型执行不同的解析策略：

        1. **订阅类认证**（codex_subscription / claude_subscription）：
           从外部凭据存储加载绑定信息，再获取实际凭据。
           Claude 订阅不支持第三方端点。
        2. **Copilot OAuth**：返回 OAuth 设备流认证标记。
        3. **API 密钥类认证**：
           依次尝试：Profile 独立槽位 → 环境变量 → 实例 api_key →
           文件存储的凭据。

        返回：
            ResolvedAuth 实例，包含提供商、认证类型、凭据值和来源。

        异常：
            ValueError: 未找到有效凭据或配置不合法时抛出。
        """
        profile_name, profile = self.resolve_profile()
        provider = profile.provider.strip()
        auth_source = profile.auth_source.strip() or default_auth_source_for_provider(provider, profile.api_format)
        if auth_source in {"codex_subscription", "claude_subscription"}:
            from openharness.auth.external import (
                is_third_party_anthropic_endpoint,
                load_external_credential,
            )
            from openharness.auth.storage import load_external_binding

            if auth_source == "claude_subscription" and is_third_party_anthropic_endpoint(profile.base_url):
                raise ValueError(
                    "Claude subscription auth only supports direct Anthropic/Claude endpoints. "
                    "Use an API-key-backed Anthropic-compatible profile for third-party base URLs."
                )
            binding = load_external_binding(auth_source_provider_name(auth_source))
            if binding is None:
                raise ValueError(
                    f"No external auth binding found for {auth_source}. Run 'oh auth "
                    f"{'codex-login' if auth_source == 'codex_subscription' else 'claude-login'}' first."
                )
            credential = load_external_credential(
                binding,
                refresh_if_needed=(auth_source == "claude_subscription"),
            )
            return ResolvedAuth(
                provider=provider,
                auth_kind=credential.auth_kind,
                value=credential.value,
                source=f"external:{credential.source_path}",
                state="configured",
            )

        if auth_source == "copilot_oauth":
            return ResolvedAuth(
                provider="copilot",
                auth_kind="oauth_device",
                value="copilot-managed",
                source="copilot",
                state="configured",
            )

        storage_provider = auth_source_provider_name(auth_source)

        from openharness.auth.storage import load_credential

        if profile.credential_slot:
            scoped_storage_provider = f"profile:{profile.credential_slot}"
            scoped = load_credential(scoped_storage_provider, "api_key", use_keyring=False)
            if scoped is None:
                scoped = load_credential(scoped_storage_provider, "api_key")
            if scoped:
                return ResolvedAuth(
                    provider=provider or auth_source_provider_name(auth_source),
                    auth_kind="api_key",
                    value=scoped,
                    source=f"file:{scoped_storage_provider}",
                    state="configured",
                )

        storage_provider = credential_storage_provider_name(profile_name, profile)

        env_var = {
            "anthropic_api_key": "ANTHROPIC_API_KEY",
            "openai_api_key": "OPENAI_API_KEY",
            "dashscope_api_key": "DASHSCOPE_API_KEY",
            "moonshot_api_key": "MOONSHOT_API_KEY",
            "minimax_api_key": "MINIMAX_API_KEY",
        }.get(auth_source)
        if env_var:
            env_value = os.environ.get(env_var, "")
            if env_value:
                return ResolvedAuth(
                    provider=provider or storage_provider,
                    auth_kind="api_key",
                    value=env_value,
                    source=f"env:{env_var}",
                    state="configured",
                )

        explicit_key = "" if profile.credential_slot else self.api_key
        if explicit_key:
            return ResolvedAuth(
                provider=provider or storage_provider,
                auth_kind="api_key",
                value=explicit_key,
                source="settings_or_env",
                state="configured",
            )

        stored = load_credential(storage_provider, "api_key")
        if stored:
            return ResolvedAuth(
                provider=provider or auth_source_provider_name(auth_source),
                auth_kind="api_key",
                value=stored,
                source=f"file:{storage_provider}",
                state="configured",
            )

        raise ValueError(
            f"No credentials found for auth source '{auth_source}'. "
            "Configure the matching provider or environment variable first."
        )

    def merge_cli_overrides(self, **overrides: Any) -> Settings:
        """应用 CLI 命令行覆盖项，返回新的 Settings 实例。

        仅应用非 None 值的覆盖项。若覆盖项涉及 Profile 相关字段
        （model、base_url、api_format、provider 等），会自动触发
        Profile 同步和物化操作，确保扁平字段与 Profile 层一致。
        对于 model 字段，还会自动清理 ANSI 转义序列。

        参数：
            **overrides: 要覆盖的设置项键值对，值为 None 的项被忽略。

        返回：
            应用了覆盖项的新的 Settings 实例。
        """
        updates = {k: v for k, v in overrides.items() if v is not None}
        # Strip ANSI escape sequences from model name if present
        if "model" in updates and isinstance(updates["model"], str):
            updates["model"] = strip_ansi_escape_sequences(updates["model"])
        merged = self.model_copy(update=updates)
        if not updates:
            return merged
        profile_keys = {
            "model",
            "base_url",
            "api_format",
            "provider",
            "api_key",
            "active_profile",
            "profiles",
            "context_window_tokens",
            "auto_compact_threshold_tokens",
        }
        profile_updates = profile_keys.intersection(updates)
        if not profile_updates:
            return merged
        if profile_updates.issubset({"active_profile"}):
            return merged.materialize_active_profile()
        return merged.sync_active_profile_from_flat_fields().materialize_active_profile()


def _apply_env_overrides(settings: Settings) -> Settings:
    """将环境变量覆盖应用到已加载的设置上。

    环境变量覆盖规则：
        - OPENHARNESS_* 前缀的变量始终覆盖（代表用户显式意图）。
        - 提供商作用域的变量（ANTHROPIC_BASE_URL、ANTHROPIC_MODEL、
          OPENAI_BASE_URL）仅在活跃 Profile 未显式配置对应字段时生效。

    支持的环境变量：
        OPENHARNESS_MODEL, OPENHARNESS_BASE_URL, OPENHARNESS_MAX_TOKENS,
        OPENHARNESS_TIMEOUT, OPENHARNESS_MAX_TURNS,
        OPENHARNESS_CONTEXT_WINDOW_TOKENS,
        OPENHARNESS_AUTO_COMPACT_THRESHOLD_TOKENS,
        OPENHARNESS_API_FORMAT, OPENHARNESS_PROVIDER,
        OPENHARNESS_SANDBOX_ENABLED, OPENHARNESS_SANDBOX_FAIL_IF_UNAVAILABLE,
        OPENHARNESS_SANDBOX_BACKEND, OPENHARNESS_SANDBOX_DOCKER_IMAGE,
        ANTHROPIC_API_KEY, OPENAI_API_KEY, ANTHROPIC_MODEL,
        ANTHROPIC_BASE_URL, OPENAI_BASE_URL

    参数：
        settings: 待覆盖的 Settings 实例。

    返回：
        应用了环境变量覆盖的新的 Settings 实例。
    """
    updates: dict[str, Any] = {}

    # Resolve the active profile to check for explicit settings.
    _, active_profile = settings.resolve_profile()
    profile_has_base_url = active_profile.base_url is not None
    profile_explicit_model = (active_profile.last_model or "").strip()
    profile_has_explicit_model = bool(profile_explicit_model) and profile_explicit_model.lower() not in {"", "default"}

    # --- model ---
    openharness_model = os.environ.get("OPENHARNESS_MODEL")
    if openharness_model:
        updates["model"] = strip_ansi_escape_sequences(openharness_model)
    elif not profile_has_explicit_model:
        anthropic_model = os.environ.get("ANTHROPIC_MODEL")
        if anthropic_model:
            updates["model"] = strip_ansi_escape_sequences(anthropic_model)

    # --- base_url ---
    openharness_base = os.environ.get("OPENHARNESS_BASE_URL")
    if openharness_base:
        updates["base_url"] = openharness_base
    elif not profile_has_base_url:
        generic_base = os.environ.get("ANTHROPIC_BASE_URL") or os.environ.get("OPENAI_BASE_URL")
        if generic_base:
            updates["base_url"] = generic_base

    max_tokens = os.environ.get("OPENHARNESS_MAX_TOKENS")
    if max_tokens:
        updates["max_tokens"] = int(max_tokens)

    timeout = os.environ.get("OPENHARNESS_TIMEOUT")
    if timeout:
        updates["timeout"] = float(timeout)

    max_turns = os.environ.get("OPENHARNESS_MAX_TURNS")
    if max_turns:
        updates["max_turns"] = int(max_turns)

    context_window_tokens = os.environ.get("OPENHARNESS_CONTEXT_WINDOW_TOKENS")
    if context_window_tokens:
        updates["context_window_tokens"] = int(context_window_tokens)

    auto_compact_threshold_tokens = os.environ.get("OPENHARNESS_AUTO_COMPACT_THRESHOLD_TOKENS")
    if auto_compact_threshold_tokens:
        updates["auto_compact_threshold_tokens"] = int(auto_compact_threshold_tokens)

    api_key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if api_key:
        updates["api_key"] = api_key

    api_format = os.environ.get("OPENHARNESS_API_FORMAT")
    if api_format:
        updates["api_format"] = api_format

    provider = os.environ.get("OPENHARNESS_PROVIDER")
    if provider:
        updates["provider"] = provider

    sandbox_enabled = os.environ.get("OPENHARNESS_SANDBOX_ENABLED")
    sandbox_fail = os.environ.get("OPENHARNESS_SANDBOX_FAIL_IF_UNAVAILABLE")
    sandbox_backend = os.environ.get("OPENHARNESS_SANDBOX_BACKEND")
    sandbox_docker_image = os.environ.get("OPENHARNESS_SANDBOX_DOCKER_IMAGE")
    sandbox_updates: dict[str, Any] = {}
    if sandbox_enabled is not None:
        sandbox_updates["enabled"] = _parse_bool_env(sandbox_enabled)
    if sandbox_fail is not None:
        sandbox_updates["fail_if_unavailable"] = _parse_bool_env(sandbox_fail)
    if sandbox_backend is not None:
        sandbox_updates["backend"] = sandbox_backend
    if sandbox_docker_image is not None:
        sandbox_updates["docker"] = settings.sandbox.docker.model_copy(
            update={"image": sandbox_docker_image}
        )
    if sandbox_updates:
        updates["sandbox"] = settings.sandbox.model_copy(update=sandbox_updates)

    if not updates:
        return settings
    return settings.model_copy(update=updates)


def _parse_bool_env(value: str) -> bool:
    """解析布尔值环境变量。

    接受的 True 值（不区分大小写）：1、true、yes、on。
    其他值均视为 False。

    参数：
        value: 环境变量的字符串值。

    返回：
        解析后的布尔值。
    """
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(config_path: Path | None = None) -> Settings:
    """从配置文件加载设置，与默认值合并后返回。

    加载流程：
        1. 若未指定路径，使用默认配置文件位置。
        2. 若配置文件存在，读取并解析为 Settings 实例。
        3. 若配置文件中缺少 profiles 或 active_profile 字段
           （旧版格式），从扁平字段推断并构建 Profile。
        4. 应用环境变量覆盖。
        5. 若配置文件不存在，使用默认值并应用环境变量覆盖。

    参数：
        config_path: 配置文件路径。若为 None，使用默认位置
            （~/.openharness/settings.json）。

    返回：
        合并了文件值与默认值的 Settings 实例。
    """
    if config_path is None:
        from openharness.config.paths import get_config_file_path

        config_path = get_config_file_path()

    if config_path.exists():
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        settings = Settings.model_validate(raw)
        if "profiles" not in raw or "active_profile" not in raw:
            profile_name, profile = _profile_from_flat_settings(settings)
            merged_profiles = settings.merged_profiles()
            merged_profiles[profile_name] = profile
            settings = settings.model_copy(
                update={
                    "active_profile": profile_name,
                    "profiles": merged_profiles,
                }
            )
        return _apply_env_overrides(settings.materialize_active_profile())

    return _apply_env_overrides(Settings().materialize_active_profile())


def save_settings(settings: Settings, config_path: Path | None = None) -> None:
    """将设置持久化写入配置文件。

    保存流程：
        1. 将扁平字段同步回活跃 Profile。
        2. 物化活跃 Profile 到扁平字段。
        3. 获取排他文件锁，防止并发写入冲突。
        4. 以原子写入方式保存 JSON 文件。

    参数：
        settings: 要保存的 Settings 实例。
        config_path: 配置文件路径。若为 None，使用默认位置
            （~/.openharness/settings.json）。
    """
    if config_path is None:
        from openharness.config.paths import get_config_file_path

        config_path = get_config_file_path()

    settings = settings.sync_active_profile_from_flat_fields().materialize_active_profile()
    lock_path = config_path.with_suffix(config_path.suffix + ".lock")
    with exclusive_file_lock(lock_path):
        atomic_write_text(
            config_path,
            settings.model_dump_json(indent=2) + "\n",
        )
