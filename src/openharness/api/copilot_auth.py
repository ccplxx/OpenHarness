"""GitHub Copilot OAuth 设备码认证模块。

本模块实现了 GitHub OAuth 设备码授权流程，用于获取 GitHub Copilot 的访问令牌。

认证流程：
1. 请求设备码 → 用户访问 URL 并输入验证码
2. 轮询 OAuth 令牌 → 获取 GitHub 访问令牌
3. 直接使用令牌 → 通过 ``Authorization: Bearer <token>`` 访问 Copilot API

支持两种部署类型：
- **github.com** — 公共 GitHub，API 地址为 ``https://api.githubcopilot.com``
- **企业版** — GitHub Enterprise（数据驻留/自托管），API 地址为 ``https://copilot-api.<domain>``

GitHub OAuth 令牌（及可选的企业 URL）持久化存储在
``~/.openharness/copilot_auth.json`` 文件中。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from openharness.config.paths import get_config_dir
from openharness.utils.fs import atomic_write_text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# OAuth client ID registered by OpenCode for Copilot integrations.
COPILOT_CLIENT_ID = "Ov23li8tweQw6odWQebz"

COPILOT_DEFAULT_API_BASE = "https://api.githubcopilot.com"

# Safety margin added to each poll interval to avoid server-side rate limits.
_POLL_SAFETY_MARGIN = 3.0  # seconds

_AUTH_FILE_NAME = "copilot_auth.json"


def copilot_api_base(enterprise_url: str | None = None) -> str:
    """返回 Copilot API 的 Base URL。

    对于公共 GitHub，返回 ``https://api.githubcopilot.com``。
    对于企业版，返回 ``https://copilot-api.<domain>``。

    Args:
        enterprise_url: GitHub Enterprise 的 URL，若为 ``None`` 则使用公共 GitHub。

    Returns:
        Copilot API 的 Base URL 字符串。
    """
    if enterprise_url:
        domain = enterprise_url.replace("https://", "").replace("http://", "").rstrip("/")
        return f"https://copilot-api.{domain}"
    return COPILOT_DEFAULT_API_BASE


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCodeResponse:
    """GitHub 设备码端点响应的解析结果数据类。

    包含设备码授权流程中所需的全部信息，供后续轮询使用。

    Attributes:
        device_code: 设备码，用于后续轮询访问令牌。
        user_code: 用户需要输入的验证码。
        verification_uri: 用户需要访问的验证 URL。
        interval: 轮询间隔秒数。
        expires_in: 设备码的有效期秒数。
    """

    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


@dataclass
class CopilotAuthInfo:
    """Copilot 的持久化与运行时认证状态数据类。

    存储已持久化的 GitHub OAuth 令牌和可选的企业 URL，
    并提供计算属性获取对应的 Copilot API Base URL。

    Attributes:
        github_token: GitHub OAuth 访问令牌。
        enterprise_url: GitHub Enterprise 的 URL，若为 ``None`` 表示使用公共 GitHub。
    """

    github_token: str
    enterprise_url: str | None = None

    @property
    def api_base(self) -> str:
        """计算并返回 Copilot API 的 Base URL。

        根据是否配置了企业 URL，自动推导对应的 API 地址。

        Returns:
            Copilot API 的 Base URL 字符串。
        """
        return copilot_api_base(self.enterprise_url)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _auth_file_path() -> Path:
    return get_config_dir() / _AUTH_FILE_NAME


def save_copilot_auth(token: str, *, enterprise_url: str | None = None) -> None:
    """将 GitHub OAuth 令牌（及可选的企业 URL）持久化到磁盘。

    使用原子写入方式将认证信息保存到 ``~/.openharness/copilot_auth.json``，
    文件权限设为 0600（仅所有者可读写）。

    Args:
        token: GitHub OAuth 访问令牌。
        enterprise_url: 可选的 GitHub Enterprise URL。
    """
    path = _auth_file_path()
    payload: dict[str, Any] = {"github_token": token}
    if enterprise_url:
        payload["enterprise_url"] = enterprise_url
    atomic_write_text(
        path,
        json.dumps(payload, indent=2) + "\n",
        mode=0o600,
    )
    log.info("Copilot auth saved to %s", path)


def load_copilot_auth() -> CopilotAuthInfo | None:
    """从磁盘加载已持久化的 Copilot 认证信息。

    读取 ``~/.openharness/copilot_auth.json`` 文件，解析其中的
    GitHub OAuth 令牌和企业 URL。若文件不存在、格式无效或缺少
    令牌字段，返回 ``None``。

    Returns:
        Copilot 认证信息对象，若无法加载则返回 ``None``。
    """
    path = _auth_file_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        token = data.get("github_token")
        if not token:
            return None
        return CopilotAuthInfo(
            github_token=token,
            enterprise_url=data.get("enterprise_url"),
        )
    except (json.JSONDecodeError, KeyError, OSError) as exc:
        log.warning("Failed to read Copilot auth file: %s", exc)
        return None


# Keep backward-compatible aliases used by CLI and tests.
save_github_token = save_copilot_auth


def load_github_token() -> str | None:
    """仅加载已持久化的 GitHub OAuth 令牌。

    这是 :func:`load_copilot_auth` 的便捷封装，仅返回令牌字符串。
    保留此函数是为了向后兼容。

    Returns:
        GitHub OAuth 令牌字符串，若未找到则返回 ``None``。
    """
    info = load_copilot_auth()
    return info.github_token if info else None


def clear_github_token() -> None:
    """删除已持久化的 Copilot 认证信息。

    删除 ``~/.openharness/copilot_auth.json`` 文件。
    若文件不存在则静默跳过。
    """
    path = _auth_file_path()
    if path.exists():
        path.unlink()
        log.info("Copilot auth cleared.")


# ---------------------------------------------------------------------------
# OAuth device flow (synchronous – called from CLI)
# ---------------------------------------------------------------------------


def request_device_code(
    *,
    client_id: str = COPILOT_CLIENT_ID,
    github_domain: str = "github.com",
) -> DeviceCodeResponse:
    """启动 OAuth 设备码流程，返回设备码和用户验证码。

    向 GitHub 的设备码端点发送请求，获取设备码、用户验证码和验证 URL。
    用户需要在浏览器中访问验证 URL 并输入验证码完成授权。

    Args:
        client_id: GitHub OAuth 应用的 Client ID。
        github_domain: GitHub 域名，默认为 ``github.com``。

    Returns:
        包含设备码、用户码、验证 URL 等信息的 :class:`DeviceCodeResponse` 对象。

    Raises:
        httpx.HTTPStatusError: 当请求失败时抛出。
    """
    url = f"https://{github_domain}/login/device/code"
    resp = httpx.post(
        url,
        json={"client_id": client_id, "scope": "read:user"},
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return DeviceCodeResponse(
        device_code=data["device_code"],
        user_code=data["user_code"],
        verification_uri=data["verification_uri"],
        interval=data.get("interval", 5),
        expires_in=data.get("expires_in", 900),
    )


def poll_for_access_token(
    device_code: str,
    interval: int,
    *,
    client_id: str = COPILOT_CLIENT_ID,
    github_domain: str = "github.com",
    timeout: float = 900,
    progress_callback: Any | None = None,
) -> str:
    """轮询 GitHub 等待用户完成授权，返回 OAuth 访问令牌。

    按指定间隔持续轮询 GitHub 的令牌端点，直到用户在浏览器中
    完成授权或超时。每次轮询前添加安全裕量以避免触发服务端速率限制。
    若收到 ``slow_down`` 错误，自动增加轮询间隔。

    Args:
        device_code: 设备码，由 :func:`request_device_code` 返回。
        interval: 轮询间隔秒数。
        client_id: GitHub OAuth 应用的 Client ID。
        github_domain: GitHub 域名。
        timeout: 超时秒数，默认为 900 秒（15 分钟）。
        progress_callback: 进度回调函数，接收 ``(poll_number, elapsed_seconds)`` 参数，
            用于向调用者显示轮询进度。

    Returns:
        GitHub OAuth 访问令牌字符串。

    Raises:
        RuntimeError: 当超时或遇到意外错误时抛出。
    """
    url = f"https://{github_domain}/login/oauth/access_token"
    poll_interval = float(interval)
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    poll_count = 0

    while time.monotonic() < deadline:
        time.sleep(poll_interval + _POLL_SAFETY_MARGIN)
        poll_count += 1
        if progress_callback is not None:
            progress_callback(poll_count, time.monotonic() - start)
        resp = httpx.post(
            url,
            json={
                "client_id": client_id,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

        if "access_token" in data:
            return data["access_token"]

        error = data.get("error", "")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            server_interval = data.get("interval")
            if isinstance(server_interval, (int, float)) and server_interval > 0:
                poll_interval = float(server_interval)
            else:
                poll_interval += 5.0
            continue
        # Any other error is terminal.
        desc = data.get("error_description", error)
        raise RuntimeError(f"OAuth device flow failed: {desc}")

    raise RuntimeError("OAuth device flow timed out waiting for user authorisation.")
