"""沙箱运行时（srt）适配器模块。

本模块封装了对 ``srt``（sandbox-runtime CLI）的适配逻辑，包括沙箱可用性检测、
运行时配置构建和命令包装。当沙箱功能启用时，子进程命令会自动包装为
``srt --settings <path> -c <command>`` 形式，实现文件系统和网络访问的隔离控制。
"""

from __future__ import annotations

import json
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openharness.config import Settings, load_settings
from openharness.platforms import get_platform, get_platform_capabilities


class SandboxUnavailableError(RuntimeError):
    """当沙箱功能被要求但不可用时抛出的异常。"""


@dataclass(frozen=True)
class SandboxAvailability:
    """描述当前环境中沙箱运行时的可用性状态。

    通过综合检测配置开关、平台支持、CLI 工具是否存在等因素，
    计算出沙箱是否真正可用。

    Attributes:
        enabled: 沙箱功能是否在配置中启用。
        available: 沙箱运行时 CLI 是否实际可用。
        reason: 不可用时的原因说明，可用时为 None。
        command: 检测到的 srt CLI 路径，不可用时为 None。
    """

    enabled: bool
    available: bool
    reason: str | None = None
    command: str | None = None

    @property
    def active(self) -> bool:
        """判断沙箱是否应被应用于子进程，要求同时启用且可用。"""
        return self.enabled and self.available


def build_sandbox_runtime_config(settings: Settings) -> dict[str, Any]:
    """将 OpenHarness 设置转换为 ``srt`` 运行时配置字典。

    提取网络白名单/黑名单和文件系统读写权限策略，
    生成 ``srt --settings`` 所需的 JSON 结构。

    Args:
        settings: OpenHarness 配置对象。

    Returns:
        dict[str, Any]: 包含 network 和 filesystem 策略的配置字典。
    """
    return {
        "network": {
            "allowedDomains": list(settings.sandbox.network.allowed_domains),
            "deniedDomains": list(settings.sandbox.network.denied_domains),
        },
        "filesystem": {
            "allowRead": list(settings.sandbox.filesystem.allow_read),
            "denyRead": list(settings.sandbox.filesystem.deny_read),
            "allowWrite": list(settings.sandbox.filesystem.allow_write),
            "denyWrite": list(settings.sandbox.filesystem.deny_write),
        },
    }


def get_sandbox_availability(settings: Settings | None = None) -> SandboxAvailability:
    """检测当前运行环境中 ``srt`` 沙箱运行时是否可用。

    按优先级依次检查：配置开关 → 平台支持 → 平台白名单 → srt CLI 存在性 →
    平台特定依赖（Linux/WSL 需要 bwrap，macOS 需要 sandbox-exec）。

    Args:
        settings: 可选的配置对象，未指定时自动加载。

    Returns:
        SandboxAvailability: 包含启用状态、可用状态和原因的可用性信息。
    """
    resolved_settings = settings or load_settings()
    if not resolved_settings.sandbox.enabled:
        return SandboxAvailability(enabled=False, available=False, reason="sandbox is disabled")

    platform_name = get_platform()
    capabilities = get_platform_capabilities(platform_name)
    if not capabilities.supports_sandbox_runtime:
        if platform_name == "windows":
            reason = "sandbox runtime is not supported on native Windows; use WSL for sandboxed execution"
        else:
            reason = f"sandbox runtime is not supported on platform {platform_name}"
        return SandboxAvailability(enabled=True, available=False, reason=reason)

    enabled_platforms = {name.lower() for name in resolved_settings.sandbox.enabled_platforms}
    if enabled_platforms and platform_name not in enabled_platforms:
        return SandboxAvailability(
            enabled=True,
            available=False,
            reason=f"sandbox is disabled for platform {platform_name} by configuration",
        )

    srt = shutil.which("srt")
    if not srt:
        return SandboxAvailability(
            enabled=True,
            available=False,
            reason=(
                "sandbox runtime CLI not found; install it with "
                "`npm install -g @anthropic-ai/sandbox-runtime`"
            ),
        )

    if platform_name in {"linux", "wsl"} and shutil.which("bwrap") is None:
        return SandboxAvailability(
            enabled=True,
            available=False,
            reason="bubblewrap (`bwrap`) is required for sandbox runtime on Linux/WSL",
            command=srt,
        )

    if platform_name == "macos" and shutil.which("sandbox-exec") is None:
        return SandboxAvailability(
            enabled=True,
            available=False,
            reason="`sandbox-exec` is required for sandbox runtime on macOS",
            command=srt,
        )

    return SandboxAvailability(enabled=True, available=True, command=srt)


def wrap_command_for_sandbox(
    command: list[str],
    *,
    settings: Settings | None = None,
) -> tuple[list[str], Path | None]:
    """在沙箱激活时，将命令参数列表包装为 ``srt`` 调用形式。

    若使用 Docker 后端或沙箱不可用，则原样返回命令。若沙箱启用但不可用
    且配置了 fail_if_unavailable，则抛出 SandboxUnavailableError。
    生成的临时配置文件路径随返回值一并返回，供调用方在适当时机清理。

    Args:
        command: 原始命令参数列表。
        settings: 可选的配置对象，未指定时自动加载。

    Returns:
        tuple[list[str], Path | None]: 包装后的命令列表和临时配置文件路径；
            未包装时配置路径为 None。

    Raises:
        SandboxUnavailableError: 沙箱启用但不可用且 fail_if_unavailable 为 True。
    """
    resolved_settings = settings or load_settings()
    if resolved_settings.sandbox.backend == "docker":
        return command, None
    availability = get_sandbox_availability(resolved_settings)
    if not availability.active:
        if resolved_settings.sandbox.enabled and resolved_settings.sandbox.fail_if_unavailable:
            raise SandboxUnavailableError(availability.reason or "sandbox runtime is unavailable")
        return command, None

    settings_path = _write_runtime_settings(build_sandbox_runtime_config(resolved_settings))
    # The ``srt`` argv form does not reliably preserve child exit codes for shell-style
    # commands such as ``bash -lc 'exit 1'``. Build a single escaped command string and
    # pass it through ``-c`` so hook/tool failures still propagate correctly.
    wrapped = [
        availability.command or "srt",
        "--settings",
        str(settings_path),
        "-c",
        shlex.join(command),
    ]
    return wrapped, settings_path


def _write_runtime_settings(payload: dict[str, Any]) -> Path:
    """将沙箱运行时配置写入临时文件，供 ``srt --settings`` 引用。

    Args:
        payload: 运行时配置字典。

    Returns:
        Path: 临时配置文件路径。
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="openharness-sandbox-",
        suffix=".json",
        delete=False,
    )
    try:
        json.dump(payload, tmp)
        tmp.write("\n")
    finally:
        tmp.close()
    return Path(tmp.name)
