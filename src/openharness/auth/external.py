"""外部 CLI 订阅凭据集成模块。

本模块负责与由外部 CLI 工具（如 OpenAI Codex CLI、Anthropic Claude CLI）管理的订阅凭据进行
交互。它能够从外部 CLI 的凭据文件或 macOS Keychain 中读取、解析、刷新凭据，并将其标准化为
OpenHarness 内部可用的运行时凭据对象。

核心功能包括：

1. **凭据加载**：从 Codex CLI 的 ``auth.json`` 或 Claude CLI 的 ``.credentials.json`` /
   macOS Keychain 中读取凭据，并转换为标准化的 :class:`ExternalAuthCredential` 对象。
2. **OAuth 令牌刷新**：当 Claude OAuth 访问令牌过期时，自动使用刷新令牌通过 Anthropic
   的 OAuth 端点获取新的访问令牌，并将更新后的凭据回写到原始数据源。
3. **凭据状态描述**：为外部认证绑定提供人类可读的状态信息（已配置、已过期、可刷新、缺失等）。
4. **HTTP 请求头构建**：生成与 Claude CLI 兼容的 OAuth 请求头，包括 beta 功能标志、
   用户代理、计费归因等信息。
5. **JWT 解析**：解码 JSON Web Token 以提取用户信息和过期时间。

安全说明：
    本模块仅读取外部 CLI 已存储的凭据，不会以明文形式持久化 OAuth 访问令牌。
    凭据刷新后直接回写到原始数据源（文件或 Keychain），确保与外部 CLI 的凭据同步。
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openharness.auth.storage import ExternalAuthBinding
from openharness.utils.fs import atomic_write_text

CODEX_PROVIDER = "openai_codex"
CLAUDE_PROVIDER = "anthropic_claude"
CLAUDE_CODE_VERSION_FALLBACK = "2.1.92"
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_OAUTH_TOKEN_ENDPOINTS = (
    "https://platform.claude.com/v1/oauth/token",
    "https://console.anthropic.com/v1/oauth/token",
)
CLAUDE_COMMON_BETAS = (
    "interleaved-thinking-2025-05-14",
    "fine-grained-tool-streaming-2025-05-14",
)
CLAUDE_AI_OAUTH_SCOPES = (
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
)
CLAUDE_OAUTH_ONLY_BETAS = (
    "claude-code-20250219",
    "oauth-2025-04-20",
)
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
_KEYCHAIN_BINDING_PREFIX = "keychain:"

_claude_code_version_cache: str | None = None
_claude_code_session_id: str | None = None


@dataclass(frozen=True)
class ExternalAuthCredential:
    """标准化的外部认证凭据数据类，在运行时使用。

    该数据类将不同来源（Codex CLI、Claude CLI 等）的外部凭据统一为标准格式，
    供 OpenHarness 运行时使用。它包含凭据值、认证类型、来源路径、管理工具标识、
    刷新令牌及过期时间等关键信息。

    Attributes:
        provider: 凭据所属的提供商标识（如 ``openai_codex``、``anthropic_claude``）。
        value: 实际的凭据值（API Key 或 OAuth 访问令牌）。
        auth_kind: 认证类型（``api_key`` 表示 API 密钥，``auth_token`` 表示 OAuth 令牌）。
        source_path: 凭据来源的文件路径或 Keychain 标识。
        managed_by: 管理该凭据的外部工具名称（如 ``codex-cli``、``claude-cli``）。
        profile_label: 用户可读的配置标签（如用户邮箱或 CLI 名称），默认为空。
        refresh_token: OAuth 刷新令牌，用于在访问令牌过期时获取新的访问令牌，默认为空。
        expires_at_ms: 访问令牌的过期时间（毫秒级 Unix 时间戳），若为 ``None`` 表示不过期。
    """

    provider: str
    value: str
    auth_kind: str
    source_path: Path
    managed_by: str
    profile_label: str = ""
    refresh_token: str = ""
    expires_at_ms: int | None = None


@dataclass(frozen=True)
class ExternalAuthState:
    """外部认证源的人类可读状态数据类。

    该数据类用于描述外部认证绑定的当前状态，包括是否已配置、
    状态类别、来源类型及详细信息，供 UI 展示和诊断使用。

    Attributes:
        configured: 该认证源是否已正确配置且可用。
        state: 状态标识，取值包括 ``configured``（已配置）、``missing``（缺失）、
            ``invalid``（无效）、``expired``（已过期）、``refreshable``（可刷新）。
        source: 凭据来源类型，取值包括 ``external``（外部源）、``missing``（缺失）。
        detail: 状态的详细描述信息（如文件路径或错误原因），默认为空。
    """

    configured: bool
    state: str
    source: str
    detail: str = ""


def default_binding_for_provider(provider: str) -> ExternalAuthBinding:
    """返回指定提供商的默认外部认证绑定配置。

    根据提供商类型和当前运行环境（操作系统、环境变量），自动推断外部 CLI
    凭据的默认存储位置和来源类型。对于 Codex 提供商，默认读取
    ``CODEX_HOME/auth.json``；对于 Claude 提供商，会依次检查
    ``CLAUDE_CONFIG_DIR`` 环境变量、macOS Keychain 和 ``CLAUDE_HOME`` 目录。

    Args:
        provider: 提供商标识字符串，支持 ``openai_codex`` 和 ``anthropic_claude``。

    Returns:
        对应提供商的默认 :class:`ExternalAuthBinding` 绑定对象。

    Raises:
        ValueError: 当传入不支持的提供商标识时抛出。
    """
    if provider == CODEX_PROVIDER:
        codex_home = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
        return ExternalAuthBinding(
            provider=provider,
            source_path=str(codex_home / "auth.json"),
            source_kind="codex_auth_json",
            managed_by="codex-cli",
            profile_label="Codex CLI",
        )
    if provider == CLAUDE_PROVIDER:
        configured_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
        if configured_dir:
            return ExternalAuthBinding(
                provider=provider,
                source_path=str(Path(configured_dir).expanduser() / ".credentials.json"),
                source_kind="claude_credentials_json",
                managed_by="claude-cli",
                profile_label="Claude CLI",
            )
        if platform.system() == "Darwin":
            return ExternalAuthBinding(
                provider=provider,
                source_path=f"{_KEYCHAIN_BINDING_PREFIX}{CLAUDE_KEYCHAIN_SERVICE}",
                source_kind="claude_credentials_keychain",
                managed_by="claude-cli",
                profile_label="Claude CLI",
            )
        claude_home = Path(os.environ.get("CLAUDE_HOME", "~/.claude")).expanduser()
        return ExternalAuthBinding(
            provider=provider,
            source_path=str(claude_home / ".credentials.json"),
            source_kind="claude_credentials_json",
            managed_by="claude-cli",
            profile_label="Claude CLI",
        )
    raise ValueError(f"Unsupported external auth provider: {provider}")


def load_external_credential(
    binding: ExternalAuthBinding,
    *,
    refresh_if_needed: bool = False,
) -> ExternalAuthCredential:
    """从外部认证绑定中读取运行时凭据。

    根据绑定信息中的提供商类型，从对应的凭据源（文件或 Keychain）加载凭据，
    并转换为标准化的 :class:`ExternalAuthCredential` 对象。对于 Claude 提供商，
    可选择在访问令牌过期时自动刷新。

    Args:
        binding: 外部认证绑定对象，包含提供商、来源路径和来源类型等信息。
        refresh_if_needed: 若为 ``True``，当 Claude 凭据已过期且存在刷新令牌时，
            自动执行 OAuth 令牌刷新并将新凭据回写到源文件或 Keychain。

    Returns:
        加载（及可能刷新）后的标准化外部凭据对象。

    Raises:
        ValueError: 当凭据源文件不存在、JSON 格式无效、缺少访问令牌、
            或提供商类型不支持时抛出。
    """
    if binding.provider == CODEX_PROVIDER:
        source_path = Path(binding.source_path).expanduser()
        if not source_path.exists():
            raise ValueError(f"External auth source not found: {source_path}")
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in external auth source: {source_path}") from exc
        return _load_codex_credential(payload, source_path, binding)
    if binding.provider == CLAUDE_PROVIDER:
        payload, source_path, keychain_service, keychain_account = _load_claude_payload(binding)
        return _load_claude_credential(
            payload,
            source_path,
            binding,
            refresh_if_needed=refresh_if_needed,
            keychain_service=keychain_service,
            keychain_account=keychain_account,
        )
    raise ValueError(f"Unsupported external auth provider: {binding.provider}")


def _load_codex_credential(
    payload: dict[str, Any],
    source_path: Path,
    binding: ExternalAuthBinding,
) -> ExternalAuthCredential:
    """从 Codex CLI 的 JSON 凭据数据中解析并构建标准化凭据对象。

    从 Codex 的 ``auth.json`` 中提取访问令牌和刷新令牌。优先从 ``tokens`` 字段
    读取，若无则回退到 ``OPENAI_API_KEY`` 字段。同时通过 JWT 解码提取用户邮箱
    作为配置标签，以及令牌过期时间。

    Args:
        payload: 从 Codex 凭据文件中解析的 JSON 字典。
        source_path: 凭据文件的路径，用于在返回的凭据对象中标识来源。
        binding: 外部认证绑定对象，提供管理工具标识等信息。

    Returns:
        标准化的 Codex 外部凭据对象。
    """
    tokens = payload.get("tokens")
    access_token = ""
    refresh_token = ""
    if isinstance(tokens, dict):
        access_token = str(tokens.get("access_token", "") or "")
        refresh_token = str(tokens.get("refresh_token", "") or "")
    if not access_token:
        access_token = str(payload.get("OPENAI_API_KEY", "") or "")
    if not access_token:
        raise ValueError("Codex auth source does not contain an access token.")

    email = _decode_json_web_token_claim(access_token, ["https://api.openai.com/profile", "email"])
    expires_at_ms = _decode_jwt_expiry(access_token)
    return ExternalAuthCredential(
        provider=CODEX_PROVIDER,
        value=access_token,
        auth_kind="api_key",
        source_path=source_path,
        managed_by=binding.managed_by,
        profile_label=email or binding.profile_label,
        refresh_token=refresh_token,
        expires_at_ms=expires_at_ms,
    )


def _load_claude_credential(
    payload: dict[str, Any],
    source_path: Path,
    binding: ExternalAuthBinding,
    *,
    refresh_if_needed: bool,
    keychain_service: str | None = None,
    keychain_account: str | None = None,
) -> ExternalAuthCredential:
    """从 Claude CLI 的凭据数据中解析并构建标准化凭据对象，支持自动刷新。

    从 Claude 的凭据中提取 ``claudeAiOauth`` 部分的访问令牌、刷新令牌和过期时间。
    当 ``refresh_if_needed`` 为 ``True`` 且凭据已过期时，使用刷新令牌获取新的
    访问令牌，并将更新后的凭据回写到原始数据源（文件或 macOS Keychain）。

    Args:
        payload: 从 Claude 凭据文件或 Keychain 中解析的 JSON 字典。
        source_path: 凭据来源的路径标识。
        binding: 外部认证绑定对象。
        refresh_if_needed: 是否在凭据过期时自动刷新。
        keychain_service: macOS Keychain 服务名称（仅在 Keychain 来源时使用）。
        keychain_account: macOS Keychain 账户名称（仅在 Keychain 来源时使用）。

    Returns:
        标准化的 Claude 外部凭据对象（可能已刷新）。

    Raises:
        ValueError: 当缺少 ``claudeAiOauth`` 字段、缺少访问令牌、或凭据过期且无法刷新时抛出。
    """
    claude_oauth = payload.get("claudeAiOauth")
    if not isinstance(claude_oauth, dict):
        raise ValueError("Claude auth source does not contain claudeAiOauth.")

    access_token = str(claude_oauth.get("accessToken", "") or "")
    refresh_token = str(claude_oauth.get("refreshToken", "") or "")
    expires_at_raw = claude_oauth.get("expiresAt")
    if not access_token:
        raise ValueError("Claude auth source does not contain an access token.")

    expires_at_ms = _coerce_int(expires_at_raw)
    credential = ExternalAuthCredential(
        provider=CLAUDE_PROVIDER,
        value=access_token,
        auth_kind="auth_token",
        source_path=source_path,
        managed_by=binding.managed_by,
        profile_label=keychain_account or binding.profile_label,
        refresh_token=refresh_token,
        expires_at_ms=expires_at_ms,
    )
    if refresh_if_needed and is_credential_expired(credential):
        if not refresh_token:
            raise ValueError(
                f"Claude credentials at {source_path} are expired and cannot be refreshed."
            )
        refreshed = refresh_claude_oauth_credential(refresh_token)
        if binding.source_kind == "claude_credentials_keychain":
            _write_claude_credentials_to_keychain(
                service=keychain_service or CLAUDE_KEYCHAIN_SERVICE,
                account=keychain_account or os.environ.get("USER", ""),
                payload=payload,
                access_token=str(refreshed["access_token"]),
                refresh_token=str(refreshed["refresh_token"]),
                expires_at_ms=int(refreshed["expires_at_ms"]),
            )
        else:
            write_claude_credentials(
                source_path,
                access_token=str(refreshed["access_token"]),
                refresh_token=str(refreshed["refresh_token"]),
                expires_at_ms=int(refreshed["expires_at_ms"]),
            )
        credential = ExternalAuthCredential(
            provider=CLAUDE_PROVIDER,
            value=str(refreshed["access_token"]),
            auth_kind="auth_token",
            source_path=source_path,
            managed_by=binding.managed_by,
            profile_label=keychain_account or binding.profile_label,
            refresh_token=str(refreshed["refresh_token"]),
            expires_at_ms=int(refreshed["expires_at_ms"]),
        )
    return credential


def _load_claude_payload(
    binding: ExternalAuthBinding,
) -> tuple[dict[str, Any], Path, str | None, str | None]:
    """根据绑定信息加载 Claude 凭据的原始 JSON 数据。

    当绑定类型为 Keychain 时，通过 macOS ``security`` 命令从 Keychain 中读取；
    否则从绑定的文件路径中读取 JSON 文件。

    Args:
        binding: 外部认证绑定对象，包含来源路径和来源类型。

    Returns:
        四元组 ``(payload, source_path, keychain_service, keychain_account)``：
        - ``payload``: 解析后的 JSON 字典。
        - ``source_path``: 实际的凭据来源路径。
        - ``keychain_service``: Keychain 服务名（仅 Keychain 来源时有值）。
        - ``keychain_account``: Keychain 账户名（仅 Keychain 来源时有值）。

    Raises:
        ValueError: 当凭据源文件不存在或 JSON 格式无效时抛出。
    """
    if binding.source_kind == "claude_credentials_keychain":
        return _read_claude_credentials_from_keychain(binding)

    source_path = Path(binding.source_path).expanduser()
    if not source_path.exists():
        raise ValueError(f"External auth source not found: {source_path}")
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in external auth source: {source_path}") from exc
    return payload, source_path, None, None


def _read_claude_credentials_from_keychain(
    binding: ExternalAuthBinding,
) -> tuple[dict[str, Any], Path, str, str | None]:
    """从 macOS Keychain 中读取 Claude CLI 的凭据数据。

    使用 macOS 的 ``security`` 命令行工具，从指定的 Keychain 服务中读取凭据的
    JSON 数据和元数据（包括 Keychain 路径和账户名）。

    Args:
        binding: 外部认证绑定对象，其 ``source_path`` 应为 ``keychain:`` 前缀格式。

    Returns:
        四元组 ``(payload, keychain_path, service, account)``：
        - ``payload``: 从 Keychain 中解析的 JSON 字典。
        - ``keychain_path``: Keychain 文件路径。
        - ``service``: Keychain 服务名称。
        - ``account``: Keychain 账户名（可能为 ``None``）。

    Raises:
        ValueError: 当 Keychain 中未找到指定服务的凭据或 JSON 格式无效时抛出。
    """
    service = binding.source_path.removeprefix(_KEYCHAIN_BINDING_PREFIX).strip() or CLAUDE_KEYCHAIN_SERVICE
    try:
        raw_payload = subprocess.check_output(
            ["security", "find-generic-password", "-w", "-s", service],
            text=True,
        )
        metadata = subprocess.check_output(
            ["security", "find-generic-password", "-s", service],
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Claude Keychain credential not found for service: {service}") from exc

    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in Claude Keychain secret for service: {service}") from exc

    keychain_path = _extract_keychain_path(metadata) or (Path.home() / "Library/Keychains/login.keychain-db")
    account = _extract_keychain_attr(metadata, "acct")
    return payload, keychain_path, service, account


def _extract_keychain_path(metadata: str) -> Path | None:
    """从 macOS ``security`` 命令的输出中提取 Keychain 文件路径。

    使用正则表达式匹配 ``keychain: "<path>"`` 格式的行，获取 Keychain
    数据库文件的实际路径。

    Args:
        metadata: ``security find-generic-password`` 命令的完整输出文本。

    Returns:
        Keychain 文件路径，若未匹配到则返回 ``None``。
    """
    match = re.search(r'^keychain:\s+"([^"]+)"$', metadata, re.MULTILINE)
    if not match:
        return None
    return Path(match.group(1))


def _extract_keychain_attr(metadata: str, attr_name: str) -> str | None:
    """从 macOS ``security`` 命令的输出中提取指定属性的值。

    使用正则表达式匹配 ``"<attr_name>"<blob>="<value>"`` 格式，
    提取如账户名（``acct``）等 Keychain 条目属性。

    Args:
        metadata: ``security find-generic-password`` 命令的完整输出文本。
        attr_name: 要提取的属性名称（如 ``acct``）。

    Returns:
        属性值字符串，若未匹配到则返回 ``None``。
    """
    match = re.search(rf'"{re.escape(attr_name)}"<blob>="([^"]*)"', metadata)
    if not match:
        return None
    return match.group(1)


def describe_external_binding(binding: ExternalAuthBinding) -> ExternalAuthState:
    """返回外部认证绑定的人类可读状态描述。

    检查绑定对应的凭据源是否存在、是否有效、是否过期等状态，
    并返回包含状态标识和详细描述的 :class:`ExternalAuthState` 对象。
    对于 Claude 提供商的过期令牌，还会区分是否可刷新。

    Args:
        binding: 要检查的外部认证绑定对象。

    Returns:
        描述该绑定当前状态的 :class:`ExternalAuthState` 对象。
    """
    source_path = Path(binding.source_path).expanduser()
    if binding.source_kind != "claude_credentials_keychain" and not source_path.exists():
        return ExternalAuthState(
            configured=False,
            state="missing",
            source="missing",
            detail=f"external auth source not found: {source_path}",
        )
    try:
        credential = load_external_credential(binding, refresh_if_needed=False)
    except ValueError as exc:
        detail = str(exc)
        if "not found" in detail.lower():
            return ExternalAuthState(
                configured=False,
                state="missing",
                source="missing",
                detail=detail,
            )
        return ExternalAuthState(
            configured=False,
            state="invalid",
            source="external",
            detail=detail,
        )
    resolved_source = credential.source_path
    if binding.provider == CLAUDE_PROVIDER and is_credential_expired(credential):
        if credential.refresh_token:
            return ExternalAuthState(
                configured=True,
                state="refreshable",
                source="external",
                detail=f"expired token can be refreshed from {resolved_source}",
            )
        return ExternalAuthState(
            configured=False,
            state="expired",
            source="external",
            detail=f"expired token at {resolved_source}",
        )
    return ExternalAuthState(
        configured=True,
        state="configured",
        source="external",
        detail=str(resolved_source),
    )


def is_credential_expired(credential: ExternalAuthCredential, *, now_ms: int | None = None) -> bool:
    """判断外部凭据是否已过期。

    比较凭据的过期时间戳与当前时间。若凭据没有过期时间（``expires_at_ms`` 为 ``None``），
    则视为永不过期。

    Args:
        credential: 待检查的外部凭据对象。
        now_ms: 当前时间的毫秒级 Unix 时间戳，若为 ``None`` 则自动获取当前时间。

    Returns:
        若凭据已过期返回 ``True``，否则返回 ``False``。
    """
    if credential.expires_at_ms is None:
        return False
    if now_ms is None:
        import time

        now_ms = int(time.time() * 1000)
    return credential.expires_at_ms <= now_ms


def get_claude_code_version() -> str:
    """获取本地安装的 Claude Code CLI 版本号。

    依次尝试执行 ``claude --version`` 和 ``claude-code --version`` 命令，
    解析输出中的版本号。若均未找到可用的 CLI 命令，则返回内置的回退版本号。
    结果在进程生命周期内缓存，避免重复执行子进程调用。

    Returns:
        Claude Code 的版本号字符串（如 ``"2.1.92"``）。
    """
    global _claude_code_version_cache
    if _claude_code_version_cache is not None:
        return _claude_code_version_cache
    for command in ("claude", "claude-code"):
        try:
            result = subprocess.run(
                [command, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            continue
        version = (result.stdout or "").strip().split(" ", 1)[0]
        if result.returncode == 0 and version and version[0].isdigit():
            _claude_code_version_cache = version
            return version
    _claude_code_version_cache = CLAUDE_CODE_VERSION_FALLBACK
    return _claude_code_version_cache


def get_claude_code_session_id() -> str:
    """获取当前进程的稳定 Claude Code 风格会话标识符。

    在进程首次调用时生成一个 UUIDv4 会话 ID，并在进程生命周期内保持不变。
    该标识符用于 Claude OAuth 请求头中，使 Anthropic 后端能够关联同一会话的请求。

    Returns:
        UUID 格式的会话标识符字符串。
    """
    global _claude_code_session_id
    if _claude_code_session_id is None:
        _claude_code_session_id = str(uuid.uuid4())
    return _claude_code_session_id


def claude_oauth_betas() -> list[str]:
    """获取 Claude OAuth 请求所需的 Beta 功能标志列表。

    返回用于 Anthropic SDK Beta 端点的功能标志列表，包括通用 Beta 标志
    （交织思维、细粒度工具流式传输）和仅限 OAuth 的 Beta 标志
    （Claude Code、OAuth）。

    Returns:
        Beta 功能标志字符串列表。
    """
    return list(CLAUDE_COMMON_BETAS + CLAUDE_OAUTH_ONLY_BETAS)


def claude_attribution_header() -> str:
    """生成 Claude Code 的计费归因请求头字符串。

    构建包含 Claude Code 版本号和入口点信息的计费归因前缀，
    用于系统提示中标识请求来源，确保 OAuth 订阅流量被正确计费归因。

    Returns:
        格式为 ``x-anthropic-billing-header: cc_version=<version>; cc_entrypoint=cli;`` 的字符串。
    """
    version = get_claude_code_version()
    return (
        "x-anthropic-billing-header: "
        f"cc_version={version}; cc_entrypoint=cli;"
    )


def claude_oauth_headers() -> dict[str, str]:
    """生成 Claude Code 订阅 OAuth 流量所需的 HTTP 请求头。

    构建包含 Anthropic Beta 功能标志、用户代理、应用标识和会话 ID 的
    请求头字典，模拟 Claude CLI 的请求头格式，使 Anthropic 后端能够
    正确识别和处理来自 OpenHarness 的 OAuth 订阅请求。

    Returns:
        包含 ``anthropic-beta``、``user-agent``、``x-app`` 和
        ``X-Claude-Code-Session-Id`` 等键的请求头字典。
    """
    all_betas = ",".join(claude_oauth_betas())
    return {
        "anthropic-beta": all_betas,
        "user-agent": f"claude-cli/{get_claude_code_version()} (external, cli)",
        "x-app": "cli",
        "X-Claude-Code-Session-Id": get_claude_code_session_id(),
    }


def refresh_claude_oauth_credential(
    refresh_token: str,
    *,
    scopes: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """刷新 Claude OAuth 访问令牌，不修改本地文件。

    使用提供的刷新令牌，通过 Anthropic 的 OAuth Token 端点获取新的访问令牌。
    依次尝试多个端点地址，直到成功获取或全部失败。返回新的访问令牌、
    刷新令牌、过期时间和授权范围。

    Args:
        refresh_token: OAuth 刷新令牌。
        scopes: 请求的 OAuth 授权范围列表，若为 ``None`` 则使用默认的
            Claude AI OAuth 范围。

    Returns:
        包含以下键的字典：
        - ``access_token``: 新的访问令牌。
        - ``refresh_token``: 新的刷新令牌（若响应中未提供则使用原刷新令牌）。
        - ``expires_at_ms``: 新令牌的过期时间（毫秒级 Unix 时间戳）。
        - ``scopes``: 授权范围字符串（可能为 ``None``）。

    Raises:
        ValueError: 当刷新令牌为空、响应中缺少访问令牌、或所有端点均刷新失败时抛出。
    """
    if not refresh_token:
        raise ValueError("refresh_token is required")

    requested_scopes = list(scopes or CLAUDE_AI_OAUTH_SCOPES)
    payload = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLAUDE_OAUTH_CLIENT_ID,
            "scope": " ".join(requested_scopes),
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"claude-cli/{get_claude_code_version()} (external, cli)",
    }
    last_error: Exception | None = None
    for endpoint in CLAUDE_OAUTH_TOKEN_ENDPOINTS:
        request = urllib.request.Request(endpoint, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                body = ""
            if "invalid_grant" in body:
                last_error = ValueError(
                    "Claude OAuth refresh token is invalid or expired. "
                    "Run `claude auth login` to refresh the official Claude CLI "
                    "credentials, then run `oh auth claude-login` again."
                )
                continue
            detail = f"{exc.code} {exc.reason}"
            if body:
                detail = f"{detail}: {body}"
            last_error = ValueError(f"Claude OAuth refresh failed at {endpoint}: {detail}")
            continue
        except Exception as exc:
            last_error = exc
            continue
        access_token = str(result.get("access_token", "") or "")
        if not access_token:
            raise ValueError("Claude OAuth refresh response missing access_token")
        next_refresh = str(result.get("refresh_token", refresh_token) or refresh_token)
        expires_in = int(result.get("expires_in", 3600) or 3600)
        return {
            "access_token": access_token,
            "refresh_token": next_refresh,
            "expires_at_ms": int(time.time() * 1000) + expires_in * 1000,
            "scopes": result.get("scope"),
        }
    if last_error is not None:
        raise ValueError(f"Claude OAuth refresh failed: {last_error}") from last_error
    raise ValueError("Claude OAuth refresh failed")


def write_claude_credentials(
    source_path: Path,
    *,
    access_token: str,
    refresh_token: str,
    expires_at_ms: int,
) -> None:
    """将刷新后的 Claude 凭据回写到上游凭据文件。

    读取现有凭据文件（若存在），更新其中的 ``claudeAiOauth`` 部分，
    然后使用原子写入方式安全地保存文件（文件权限设为 0600）。
    此操作确保与 Claude CLI 的凭据文件格式保持兼容。

    Args:
        source_path: Claude 凭据文件的路径。
        access_token: 新的 OAuth 访问令牌。
        refresh_token: 新的 OAuth 刷新令牌。
        expires_at_ms: 新令牌的过期时间（毫秒级 Unix 时间戳）。
    """
    existing: dict[str, Any] = {}
    if source_path.exists():
        try:
            existing = json.loads(source_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing["claudeAiOauth"] = _merge_claude_oauth_payload(
        existing.get("claudeAiOauth"),
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=expires_at_ms,
    )
    atomic_write_text(
        source_path,
        json.dumps(existing, indent=2) + "\n",
        mode=0o600,
    )


def _write_claude_credentials_to_keychain(
    *,
    service: str,
    account: str,
    payload: dict[str, Any],
    access_token: str,
    refresh_token: str,
    expires_at_ms: int,
) -> None:
    """将刷新后的 Claude 凭据回写到 macOS Keychain。

    更新现有的凭据 JSON 数据中的 ``claudeAiOauth`` 部分，
    然后使用 macOS ``security add-generic-password`` 命令将完整的
    凭据数据写回 Keychain（``-U`` 参数表示更新已有条目）。

    Args:
        service: Keychain 服务名称。
        account: Keychain 账户名称。
        payload: 现有的完整凭据 JSON 数据。
        access_token: 新的 OAuth 访问令牌。
        refresh_token: 新的 OAuth 刷新令牌。
        expires_at_ms: 新令牌的过期时间（毫秒级 Unix 时间戳）。
    """
    next_payload = dict(payload)
    next_payload["claudeAiOauth"] = _merge_claude_oauth_payload(
        payload.get("claudeAiOauth"),
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at_ms=expires_at_ms,
    )
    subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            service,
            "-a",
            account,
            "-w",
            json.dumps(next_payload, separators=(",", ":")),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _merge_claude_oauth_payload(
    previous: Any,
    *,
    access_token: str,
    refresh_token: str,
    expires_at_ms: int,
) -> dict[str, Any]:
    """合并新的 Claude OAuth 凭据与已有的 OAuth 负载数据。

    构建新的 ``claudeAiOauth`` 字典，包含访问令牌、刷新令牌和过期时间，
    并保留旧负载中的 ``scopes``、``rateLimitTier`` 和 ``subscriptionType`` 字段。

    Args:
        previous: 旧的 ``claudeAiOauth`` 负载数据（可能为任意类型）。
        access_token: 新的 OAuth 访问令牌。
        refresh_token: 新的 OAuth 刷新令牌。
        expires_at_ms: 新令牌的过期时间（毫秒级 Unix 时间戳）。

    Returns:
        合并后的 ``claudeAiOauth`` 字典。
    """
    next_oauth: dict[str, Any] = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }
    if isinstance(previous, dict):
        for key in ("scopes", "rateLimitTier", "subscriptionType"):
            if key in previous:
                next_oauth[key] = previous[key]
    return next_oauth


def is_third_party_anthropic_endpoint(base_url: str | None) -> bool:
    """判断给定的 Base URL 是否为使用 Anthropic 兼容 API 的第三方端点。

    通过检查 URL 中是否包含 ``anthropic.com`` 或 ``claude.com`` 域名来判断。
    若 URL 不包含这些域名，则认为是第三方端点（如中转代理或其他兼容服务）。

    Args:
        base_url: API 的 Base URL 字符串，若为 ``None`` 则返回 ``False``。

    Returns:
        若为第三方 Anthropic 兼容端点返回 ``True``，否则返回 ``False``。
    """
    if not base_url:
        return False
    normalized = base_url.rstrip("/").lower()
    return "anthropic.com" not in normalized and "claude.com" not in normalized


def _coerce_int(value: Any) -> int | None:
    """将各种类型的值强制转换为整数。

    支持布尔值（返回 ``None``，避免 ``True``/``False`` 被误转为 1/0）、
    整数（直接返回）、浮点数（截断为整数）和纯数字字符串的转换。
    其他类型或不合法的值返回 ``None``。

    Args:
        value: 待转换的值，可以是任意类型。

    Returns:
        转换后的整数值，若无法转换则返回 ``None``。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed.isdigit():
            return int(trimmed)
    return None


def _decode_jwt_expiry(token: str) -> int | None:
    """从 JWT 令牌中解码过期时间。

    提取 JWT 的 ``exp`` 声明（过期时间，秒级 Unix 时间戳），
    并将其转换为毫秒级时间戳。支持整数、浮点数和数字字符串格式的 ``exp`` 值。

    Args:
        token: JWT 令牌字符串。

    Returns:
        毫秒级的过期时间戳，若令牌格式无效或无 ``exp`` 声明则返回 ``None``。
    """
    exp = _decode_json_web_token_claim(token, ["exp"])
    if exp is None:
        return None
    if isinstance(exp, int):
        return exp * 1000
    if isinstance(exp, float):
        return int(exp * 1000)
    if isinstance(exp, str) and exp.strip().isdigit():
        return int(exp.strip()) * 1000
    return None


def _decode_json_web_token_claim(token: str, path: list[str]) -> Any | None:
    """从 JWT 令牌中解码指定的声明（claim）。

    解码 JWT 的 Payload 部分（第二段 Base64URL 编码数据），
    然后按照给定的路径列表逐层查找目标声明。例如，路径
    ``["https://api.openai.com/profile", "email"]`` 将提取嵌套字段中的邮箱地址。

    Args:
        token: JWT 令牌字符串。
        path: 声明路径列表，用于在解码后的 Payload 字典中逐层查找。

    Returns:
        找到的声明值，若令牌格式无效或路径不存在则返回 ``None``。
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        encoded = parts[1]
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        return None

    current: Any = payload
    for key in path:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return None
    return current
