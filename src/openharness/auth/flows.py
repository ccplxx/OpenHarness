"""认证流程模块，提供多种交互式认证方式。

本模块定义了 OpenHarness 支持的各种交互式认证流程，每个流程以独立的类实现，
均继承自抽象基类 :class:`AuthFlow`，并提供统一的 ``run()`` 方法执行认证并返回凭据值。

支持的认证流程包括：

1. **API Key 流程**（:class:`ApiKeyFlow`）：通过终端提示用户输入 API 密钥。
2. **设备码流程**（:class:`DeviceCodeFlow`）：通过 GitHub OAuth 设备码授权流程获取令牌，
   适用于 GitHub Copilot 等基于 GitHub OAuth 的服务。
3. **浏览器流程**（:class:`BrowserFlow`）：打开浏览器 URL 引导用户完成认证，
   然后由用户粘贴返回的令牌。

所有流程均为自包含的，只需调用 ``run()`` 方法即可完成整个认证过程。
"""

from __future__ import annotations

import logging
import platform
import subprocess
import sys
from abc import ABC, abstractmethod
from typing import Any

log = logging.getLogger(__name__)


class AuthFlow(ABC):
    """所有认证流程的抽象基类。

    定义了认证流程的统一接口，所有子类必须实现 ``run()`` 方法，
    该方法执行具体的认证交互并返回获取到的凭据值字符串。
    """

    @abstractmethod
    def run(self) -> str:
        """执行认证流程并返回获取到的凭据值。

        Returns:
            认证成功后获取的凭据字符串（如 API Key 或 OAuth 令牌）。
        """


# ---------------------------------------------------------------------------
# ApiKeyFlow — directly prompt for and store an API key
# ---------------------------------------------------------------------------


class ApiKeyFlow(AuthFlow):
    """API 密钥认证流程，通过终端提示用户输入 API Key。

    该流程使用 ``getpass`` 模块安全地提示用户输入 API 密钥（不回显输入内容），
    并返回用户输入的密钥值。适用于直接使用 API Key 进行认证的提供商。
    """

    def __init__(self, provider: str, prompt_text: str | None = None) -> None:
        """初始化 API Key 认证流程。

        Args:
            provider: 提供商名称，用于生成默认的提示文本。
            prompt_text: 自定义的提示文本，若为 ``None`` 则使用默认的
                ``"Enter your {provider} API key"`` 格式。
        """
        self.provider = provider
        self.prompt_text = prompt_text or f"Enter your {provider} API key"

    def run(self) -> str:
        """执行 API Key 认证流程。

        使用 ``getpass`` 安全地提示用户输入 API Key（输入内容不会回显到终端），
        并去除首尾空白字符。若用户输入为空则抛出异常。

        Returns:
            用户输入的 API Key 字符串。

        Raises:
            ValueError: 当用户输入为空时抛出。
        """
        import getpass

        key = getpass.getpass(f"{self.prompt_text}: ").strip()
        if not key:
            raise ValueError("API key cannot be empty.")
        return key


# ---------------------------------------------------------------------------
# DeviceCodeFlow — GitHub OAuth device-code flow (refactored from copilot_auth)
# ---------------------------------------------------------------------------


class DeviceCodeFlow(AuthFlow):
    """GitHub OAuth 设备码认证流程。

    实现了 GitHub 的设备码授权流程（Device Code Grant），适用于无浏览器访问能力
    或需要离线授权的场景。流程步骤如下：

    1. 向 GitHub 请求设备码和验证 URL。
    2. 在终端显示验证 URL 和用户码，并尝试自动打开浏览器。
    3. 轮询 GitHub 等待用户完成授权。
    4. 授权成功后返回访问令牌。

    该流程可用于任何支持设备码授权的 GitHub OAuth 应用，默认用于 GitHub Copilot。
    """

    def __init__(
        self,
        client_id: str | None = None,
        github_domain: str = "github.com",
        enterprise_url: str | None = None,
        *,
        progress_callback: Any | None = None,
    ) -> None:
        """初始化设备码认证流程。

        Args:
            client_id: GitHub OAuth 应用的 Client ID，若为 ``None`` 则使用
                Copilot 默认的 Client ID。
            github_domain: GitHub 域名，默认为 ``github.com``。
            enterprise_url: GitHub Enterprise 的 URL，若提供则覆盖 ``github_domain``。
            progress_callback: 轮询进度回调函数，接收 ``(poll_num, elapsed)`` 参数，
                若为 ``None`` 则使用默认的终端输出进度显示。
        """
        from openharness.api.copilot_auth import COPILOT_CLIENT_ID

        self.client_id = client_id or COPILOT_CLIENT_ID
        self.enterprise_url = enterprise_url
        self.github_domain = github_domain if not enterprise_url else enterprise_url
        self.progress_callback = progress_callback

    @staticmethod
    def _try_open_browser(url: str) -> bool:
        """尝试在默认浏览器中打开指定的 URL。

        根据当前操作系统选择合适的命令打开浏览器：
        - macOS: 使用 ``open`` 命令
        - Windows: 使用 ``start`` 命令
        - Linux/WSL: 使用 ``xdg-open`` 命令

        Args:
            url: 要在浏览器中打开的 URL 地址。

        Returns:
            若浏览器启动成功返回 ``True``，否则返回 ``False``。
        """
        try:
            plat = platform.system()
            if plat == "Darwin":
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            if plat == "Windows":
                subprocess.Popen(
                    ["start", "", url],
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return True
            # Linux / WSL
            proc = subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                proc.wait(timeout=2)
                return proc.returncode == 0
            except subprocess.TimeoutExpired:
                return True
        except Exception:
            return False

    def run(self) -> str:
        """执行 GitHub 设备码认证流程。

        完整执行设备码授权流程：请求设备码 -> 显示验证信息 -> 尝试打开浏览器 ->
        轮询等待授权 -> 返回访问令牌。在轮询过程中，通过进度回调显示等待时间。

        Returns:
            GitHub OAuth 访问令牌字符串。

        Raises:
            RuntimeError: 当轮询超时或授权被拒绝时抛出。
        """
        from openharness.api.copilot_auth import poll_for_access_token, request_device_code

        print("Starting GitHub device flow...", flush=True)
        dc = request_device_code(client_id=self.client_id, github_domain=self.github_domain)

        print(flush=True)
        print(f"  Open: {dc.verification_uri}", flush=True)
        print(f"  Code: {dc.user_code}", flush=True)
        print(flush=True)

        opened = self._try_open_browser(dc.verification_uri)
        if opened:
            print("(Browser opened — enter the code shown above.)", flush=True)
        else:
            print("Open the URL above in your browser and enter the code.", flush=True)
        print(flush=True)

        if self.progress_callback is None:

            def _default_progress(poll_num: int, elapsed: float) -> None:
                mins = int(elapsed) // 60
                secs = int(elapsed) % 60
                print(f"\r  Polling... ({mins}m {secs:02d}s elapsed)", end="", flush=True)

            self.progress_callback = _default_progress

        print("Waiting for authorisation...", flush=True)
        try:
            token = poll_for_access_token(
                dc.device_code,
                dc.interval,
                client_id=self.client_id,
                github_domain=self.github_domain,
                progress_callback=self.progress_callback,
            )
        except RuntimeError as exc:
            print(flush=True)
            print(f"Error: {exc}", file=sys.stderr, flush=True)
            raise

        print(flush=True)
        return token


# ---------------------------------------------------------------------------
# BrowserFlow — open a URL and wait for the user to complete auth
# ---------------------------------------------------------------------------


class BrowserFlow(AuthFlow):
    """浏览器认证流程，打开 URL 引导用户完成认证。

    该流程在默认浏览器中打开认证 URL，等待用户完成认证后，
    提示用户粘贴从浏览器获取的令牌或授权码。适用于需要在浏览器中
    完成 OAuth 授权但不需要后端回调服务的场景。
    """

    def __init__(self, auth_url: str, prompt_text: str = "Paste the token from your browser") -> None:
        """初始化浏览器认证流程。

        Args:
            auth_url: 需要在浏览器中打开的认证页面 URL。
            prompt_text: 提示用户粘贴令牌的文本，默认为
                ``"Paste the token from your browser"``。
        """
        self.auth_url = auth_url
        self.prompt_text = prompt_text

    def run(self) -> str:
        """执行浏览器认证流程。

        尝试在默认浏览器中打开认证 URL，然后使用 ``getpass`` 安全地
        提示用户粘贴从浏览器获取的令牌。若浏览器无法自动打开，
        会显示手动访问的提示信息。

        Returns:
            用户粘贴的令牌字符串。

        Raises:
            ValueError: 当用户未提供令牌（输入为空）时抛出。
        """
        import getpass

        print(f"Opening browser for authentication: {self.auth_url}", flush=True)
        opened = DeviceCodeFlow._try_open_browser(self.auth_url)
        if not opened:
            print(f"Could not open browser automatically. Visit: {self.auth_url}", flush=True)

        token = getpass.getpass(f"{self.prompt_text}: ").strip()
        if not token:
            raise ValueError("No token provided.")
        return token
