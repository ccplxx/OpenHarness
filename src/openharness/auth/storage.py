"""OpenHarness 凭据存储模块。

本模块提供凭据的安全持久化存储与读取功能，支持两种后端：

1. **文件后端**（始终可用）：将凭据存储在 ``~/.openharness/credentials.json``
   文件中，使用 POSIX 文件权限（mode 600）保护。该后端适用于所有环境，
   包括容器、CI 和 WSL 等无法使用系统密钥环的场景。

2. **系统密钥环后端**（可选）：若安装了 ``keyring`` 包且存在可用的后端，
   则优先使用系统密钥环存储凭据，提供更高的安全性。在无密钥环的环境中
   会自动回退到文件后端。

安全模型
--------
当无密钥环后端可用时（常见于容器、CI 和 WSL 环境），凭据以**明文 JSON**
形式存储，仅通过 POSIX 文件权限（mode 600）保护。本模块中的
``_obfuscate`` / ``_deobfuscate`` 辅助函数是轻量级的 XOR 可逆混淆，
用于非机密数据；它们**不是加密**，绝不能用于保护密钥或密码等敏感信息。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from openharness.config.paths import get_config_dir
from openharness.utils.file_lock import exclusive_file_lock
from openharness.utils.fs import atomic_write_text

log = logging.getLogger(__name__)

_CREDS_FILE_NAME = "credentials.json"
_KEYRING_SERVICE = "openharness"


def _creds_lock_path() -> Path:
    """返回凭据文件的排他锁路径。

    锁文件与凭据文件同目录，扩展名为 ``.json.lock``，用于在并发
    读写凭据文件时确保数据一致性。

    Returns:
        锁文件的 :class:`Path` 对象。
    """
    return _creds_path().with_suffix(".json.lock")


@dataclass(frozen=True)
class ExternalAuthBinding:
    """指向由外部 CLI 管理的凭据的绑定信息数据类。

    该数据类描述了外部 CLI 工具（如 Codex CLI、Claude CLI）管理的凭据
    的位置和类型信息，用于在 OpenHarness 中引用外部管理的凭据，
    而非直接存储凭据值。

    Attributes:
        provider: 提供商标识（如 ``openai_codex``、``anthropic_claude``）。
        source_path: 外部凭据源的路径（文件路径或 ``keychain:`` 前缀的 Keychain 标识）。
        source_kind: 来源类型标识（如 ``codex_auth_json``、``claude_credentials_json``、
            ``claude_credentials_keychain``）。
        managed_by: 管理该凭据的外部工具名称（如 ``codex-cli``、``claude-cli``）。
        profile_label: 配置文件的可读标签，默认为空。
    """

    provider: str
    source_path: str
    source_kind: str
    managed_by: str
    profile_label: str = ""


# ---------------------------------------------------------------------------
# File-based backend (always available)
# ---------------------------------------------------------------------------


def _creds_path() -> Path:
    """返回凭据文件的路径。

    凭据文件位于 OpenHarness 配置目录下，文件名为 ``credentials.json``。

    Returns:
        凭据文件的 :class:`Path` 对象。
    """
    return get_config_dir() / _CREDS_FILE_NAME


def _load_creds_file() -> dict[str, Any]:
    """从凭据文件中加载所有凭据数据。

    读取并解析凭据 JSON 文件。若文件不存在或格式无效，返回空字典而非抛出异常。

    Returns:
        包含所有凭据数据的字典，以提供商名称为顶层键。
    """
    path = _creds_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to read credentials file: %s", exc)
        return {}


def _save_creds_file(data: dict[str, Any]) -> None:
    """将凭据数据安全地写入凭据文件。

    使用原子写入方式（先写入临时文件再重命名）确保写入操作的原子性，
    避免在写入过程中因异常导致文件损坏。文件权限设为 0600（仅所有者可读写）。

    Args:
        data: 要写入的凭据数据字典。
    """
    path = _creds_path()
    atomic_write_text(
        path,
        json.dumps(data, indent=2) + "\n",
        mode=0o600,
    )


# ---------------------------------------------------------------------------
# Keyring backend (optional)
# ---------------------------------------------------------------------------


_keyring_checked: bool = False
_keyring_usable: bool = False


def _keyring_available() -> bool:
    """检查系统密钥环后端是否可用。

    首次调用时检查 ``keyring`` 包是否安装且后端是否可用，结果在进程
    生命周期内缓存。仅导入 ``keyring`` 包不足以判断可用性，因为包
    可能在无功能后端的环境中安装（如无头 Linux / WSL / 容器），
    因此通过尝试读取一个探测键来验证后端是否真正可用。

    Returns:
        若系统密钥环后端可用返回 ``True``，否则返回 ``False``。
    """
    global _keyring_checked, _keyring_usable  # noqa: PLW0603
    if _keyring_checked:
        return _keyring_usable
    _keyring_checked = True
    try:
        import keyring

        # Probe the backend — merely importing keyring is not enough because
        # the package may be installed without a functioning backend (e.g. on
        # headless Linux / WSL / containers).
        keyring.get_password(_KEYRING_SERVICE, "__probe__")
        _keyring_usable = True
    except ImportError:
        _keyring_usable = False
    except Exception as exc:
        log.info("System keyring unavailable, using file backend: %s", exc)
        _keyring_usable = False
    return _keyring_usable


def _keyring_key(provider: str, key: str) -> str:
    """生成密钥环中凭据的存储键名。

    将提供商名称和凭据键名组合为 ``provider:key`` 格式的唯一键名，
    用于在系统密钥环中标识和查找凭据。

    Args:
        provider: 提供商名称。
        key: 凭据键名。

    Returns:
        格式为 ``provider:key`` 的密钥环键名字符串。
    """
    return f"{provider}:{key}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def store_credential(provider: str, key: str, value: str, *, use_keyring: bool | None = None) -> None:
    """持久化存储指定提供商的凭据。

    优先使用系统密钥环存储凭据；若密钥环不可用或存储失败，
    则回退到文件后端。文件存储使用排他文件锁确保并发安全。

    Args:
        provider: 提供商名称。
        key: 凭据键名（如 ``"api_key"``）。
        value: 凭据值。
        use_keyring: 是否强制使用密钥环，若为 ``None`` 则在可用时自动使用。
    """
    if use_keyring is None:
        use_keyring = _keyring_available()

    if use_keyring:
        try:
            import keyring

            keyring.set_password(_KEYRING_SERVICE, _keyring_key(provider, key), value)
            log.debug("Stored %s/%s in keyring", provider, key)
            return
        except Exception as exc:
            log.warning("Keyring store failed, falling back to file: %s", exc)

    with exclusive_file_lock(_creds_lock_path()):
        data = _load_creds_file()
        data.setdefault(provider, {})[key] = value
        _save_creds_file(data)
    log.debug("Stored %s/%s in credentials file", provider, key)


def load_credential(provider: str, key: str, *, use_keyring: bool | None = None) -> str | None:
    """加载指定提供商的已存储凭据。

    优先从系统密钥环读取凭据；若密钥环不可用或未找到凭据，
    则回退到文件后端查找。

    Args:
        provider: 提供商名称。
        key: 凭据键名（如 ``"api_key"``）。
        use_keyring: 是否强制使用密钥环，若为 ``None`` 则在可用时自动使用。

    Returns:
        凭据值字符串，若未找到则返回 ``None``。
    """
    if use_keyring is None:
        use_keyring = _keyring_available()

    if use_keyring:
        try:
            import keyring

            value = keyring.get_password(_KEYRING_SERVICE, _keyring_key(provider, key))
            if value is not None:
                return value
        except Exception as exc:
            log.warning("Keyring load failed, falling back to file: %s", exc)

    data = _load_creds_file()
    return data.get(provider, {}).get(key)


def clear_provider_credentials(provider: str, *, use_keyring: bool | None = None) -> None:
    """清除指定提供商的所有已存储凭据。

    同时从密钥环和文件后端中删除该提供商的凭据数据。
    在密钥环中，尝试删除常见的凭据键名（``api_key``、``token``、``github_token``），
    忽略不存在的键。在文件后端中，删除该提供商的整个数据段。

    Args:
        provider: 提供商名称。
        use_keyring: 是否强制使用密钥环，若为 ``None`` 则在可用时自动使用。
    """
    if use_keyring is None:
        use_keyring = _keyring_available()

    if use_keyring:
        try:
            import keyring
            from keyring.errors import PasswordDeleteError

            # Try common keys; silently ignore missing ones.
            for key in ("api_key", "token", "github_token"):
                try:
                    keyring.delete_password(_KEYRING_SERVICE, _keyring_key(provider, key))
                except (PasswordDeleteError, Exception):
                    pass
        except ImportError:
            pass

    with exclusive_file_lock(_creds_lock_path()):
        data = _load_creds_file()
        if provider in data:
            del data[provider]
            _save_creds_file(data)
    log.debug("Cleared credentials for provider: %s", provider)


def list_stored_providers() -> list[str]:
    """获取文件存储中已有凭据的提供商列表。

    仅检查文件后端中的凭据，不包括密钥环中的凭据。

    Returns:
        有凭据存储的提供商名称列表。
    """
    return list(_load_creds_file().keys())


def store_external_binding(binding: ExternalAuthBinding) -> None:
    """持久化存储外部认证绑定的元数据。

    将外部认证绑定信息（提供商、来源路径、来源类型、管理工具等）
    保存到凭据文件中，以便后续通过 :func:`load_external_binding` 加载。
    使用排他文件锁确保并发安全。

    Args:
        binding: 要存储的外部认证绑定对象。
    """
    with exclusive_file_lock(_creds_lock_path()):
        data = _load_creds_file()
        entry = data.setdefault(binding.provider, {})
        entry["external_binding"] = asdict(binding)
        _save_creds_file(data)
    log.debug("Stored external auth binding for provider: %s", binding.provider)


def load_external_binding(provider: str) -> ExternalAuthBinding | None:
    """加载指定提供商的外部认证绑定元数据。

    从凭据文件中读取并解析该提供商的外部绑定信息。
    若绑定数据格式异常（缺少必要字段），会记录警告并返回 ``None``。

    Args:
        provider: 提供商名称。

    Returns:
        外部认证绑定对象，若不存在则返回 ``None``。
    """
    entry = _load_creds_file().get(provider, {})
    if not isinstance(entry, dict):
        return None
    raw = entry.get("external_binding")
    if not isinstance(raw, dict):
        return None
    try:
        return ExternalAuthBinding(
            provider=str(raw["provider"]),
            source_path=str(raw["source_path"]),
            source_kind=str(raw["source_kind"]),
            managed_by=str(raw["managed_by"]),
            profile_label=str(raw.get("profile_label", "") or ""),
        )
    except KeyError:
        log.warning("Ignoring malformed external auth binding for provider: %s", provider)
        return None


# ---------------------------------------------------------------------------
# Obfuscation helpers (XOR round-trip — NOT encryption)
# ---------------------------------------------------------------------------
# These exist for lightweight obfuscation of non-secret data (e.g. session
# tokens where the goal is to prevent casual reading, not resist attack).
# Do NOT use for API keys or passwords — those belong in the keyring or in
# the plain-text file protected by POSIX permissions.
# ---------------------------------------------------------------------------


def _obfuscation_key() -> bytes:
    """生成基于用户主目录路径的混淆密钥。

    使用用户主目录路径加上固定盐值 ``openharness-v1`` 作为种子，
    通过 SHA-256 哈希生成 32 字节的混淆密钥。该密钥在同一用户
    环境下是确定性的，确保混淆后的数据可以被正确还原。

    Returns:
        32 字节的 SHA-256 哈希值作为混淆密钥。
    """
    seed = str(Path.home()).encode() + b"openharness-v1"
    import hashlib

    return hashlib.sha256(seed).digest()


def _obfuscate(plaintext: str) -> str:
    """对明文字符串进行轻量级混淆（XOR + Base64URL 编码）。

    **注意：这不是加密！** 仅用于防止数据被随意读取，不能抵御攻击。
    先将明文 UTF-8 编码后与混淆密钥进行 XOR 运算，然后将结果
    进行 Base64URL 编码。该函数与 :func:`_deobfuscate` 互为逆操作。

    Args:
        plaintext: 要混淆的明文字符串。

    Returns:
        混淆后的 Base64URL 编码字符串。
    """
    import base64

    key = _obfuscation_key()
    data = plaintext.encode("utf-8")
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return base64.urlsafe_b64encode(xored).decode("ascii")


def _deobfuscate(ciphertext: str) -> str:
    """对混淆后的字符串进行还原，是 :func:`_obfuscate` 的逆操作。

    先对 Base64URL 编码的密文进行解码，然后与混淆密钥进行 XOR 运算
    还原原始明文。

    Args:
        ciphertext: 混淆后的 Base64URL 编码字符串。

    Returns:
        还原后的明文字符串。
    """
    import base64

    key = _obfuscation_key()
    data = base64.urlsafe_b64decode(ciphertext.encode("ascii"))
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return xored.decode("utf-8")


# Backward compatibility — deprecated, will be removed in a future version.
encrypt = _obfuscate
decrypt = _deobfuscate
