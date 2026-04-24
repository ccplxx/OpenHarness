"""OpenHarness 统一认证管理模块。

本模块是 ``openharness.auth`` 包的入口文件，负责将认证子系统的核心组件导出为公共 API。
它整合了以下三个子模块的功能：

- :mod:`openharness.auth.flows` — 提供交互式认证流程（API Key、设备码、浏览器）。
- :mod:`openharness.auth.manager` — 提供统一的认证状态管理与配置操作入口（:class:`AuthManager`）。
- :mod:`openharness.auth.storage` — 提供凭据的持久化存储与读取（文件存储 + 系统密钥环）。

外部使用者只需 ``from openharness.auth import AuthManager, store_credential, ...`` 即可完成
认证相关的所有操作，无需直接引用子模块。

注意：
    ``encrypt`` 和 ``decrypt`` 已弃用，仅为向后兼容而保留，未来版本将被移除。
    如需轻量混淆功能，请直接使用 ``_obfuscate`` / ``_deobfuscate``。
"""

from openharness.auth.flows import ApiKeyFlow, BrowserFlow, DeviceCodeFlow
from openharness.auth.manager import AuthManager
from openharness.auth.storage import (
    clear_provider_credentials,
    decrypt,
    encrypt,
    load_credential,
    load_external_binding,
    store_credential,
    store_external_binding,
)

__all__ = [
    "AuthManager",
    "ApiKeyFlow",
    "BrowserFlow",
    "DeviceCodeFlow",
    "store_credential",
    "load_credential",
    "store_external_binding",
    "load_external_binding",
    "clear_provider_credentials",
    # Deprecated — use _obfuscate/_deobfuscate directly if needed.
    # Kept for backward compatibility; will be removed in a future version.
    "encrypt",
    "decrypt",
]
