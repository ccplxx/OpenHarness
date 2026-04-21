"""Swarm 后端注册表与自动检测模块。

本模块实现 :class:`BackendRegistry`，负责管理所有可用的 TeammateExecutor 后端实例，
并提供自动检测逻辑以选择最合适的执行后端（tmux / in_process / subprocess）。

检测优先级管道（与 TS 源码 ``registry.ts`` 对齐）：
1. ``in_process`` — 当显式请求或此前 spawn 失败触发降级时激活。
2. ``tmux`` — 当进程运行在 tmux 会话内且 tmux 二进制可用时选择。
3. ``subprocess`` — 始终可用，作为安全回退。

此外提供面板后端（tmux / iTerm2）的检测与安装指引生成功能，
以及进程级单例 :func:`get_backend_registry` 便于全局访问。
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import TYPE_CHECKING, Any

from openharness.platforms import get_platform, get_platform_capabilities
from openharness.swarm.spawn_utils import is_tmux_available
from openharness.swarm.types import BackendDetectionResult, BackendType, TeammateExecutor

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def _detect_tmux() -> bool:
    """判断进程是否运行在活跃的 tmux 会话内。

    检查：
    1. ``$TMUX`` 环境变量（tmux 为已附加的客户端设置）。
    2. ``tmux`` 二进制文件在 PATH 上可用。
    """
    if not os.environ.get("TMUX"):
        logger.debug("[BackendRegistry] _detect_tmux: $TMUX not set")
        return False
    if not shutil.which("tmux"):
        logger.debug("[BackendRegistry] _detect_tmux: tmux binary not found on PATH")
        return False
    logger.debug("[BackendRegistry] _detect_tmux: inside tmux session with binary available")
    return True


def _detect_iterm2() -> bool:
    """判断进程是否运行在 iTerm2 终端内。

    检查 ``$ITERM_SESSION_ID``，iTerm2 为每个终端会话设置此变量。
    """
    if os.environ.get("ITERM_SESSION_ID"):
        logger.debug("[BackendRegistry] _detect_iterm2: ITERM_SESSION_ID=%s", os.environ["ITERM_SESSION_ID"])
        return True
    logger.debug("[BackendRegistry] _detect_iterm2: ITERM_SESSION_ID not set")
    return False


def _is_it2_cli_available() -> bool:
    """判断 ``it2`` CLI 是否已安装（用于 iTerm2 面板控制）。"""
    available = shutil.which("it2") is not None
    logger.debug("[BackendRegistry] _is_it2_cli_available: %s", available)
    return available


def _get_tmux_install_instructions() -> str:
    """返回平台特定的 tmux 安装指引。"""
    system = get_platform()
    if system == "macos":
        return (
            "To use agent swarms, install tmux:\n"
            "  brew install tmux\n"
            "Then start a tmux session with: tmux new-session -s claude"
        )
    elif system in {"linux", "wsl"}:
        return (
            "To use agent swarms, install tmux:\n"
            "  sudo apt install tmux    # Ubuntu/Debian\n"
            "  sudo dnf install tmux    # Fedora/RHEL\n"
            "Then start a tmux session with: tmux new-session -s claude"
        )
    elif system == "windows":
        return (
            "To use agent swarms, you need tmux which requires WSL "
            "(Windows Subsystem for Linux).\n"
            "Install WSL first, then inside WSL run:\n"
            "  sudo apt install tmux\n"
            "Then start a tmux session with: tmux new-session -s claude"
        )
    else:
        return (
            "To use agent swarms, install tmux using your system's package manager.\n"
            "Then start a tmux session with: tmux new-session -s claude"
        )


# ---------------------------------------------------------------------------
# BackendRegistry
# ---------------------------------------------------------------------------


class BackendRegistry:
    """将 BackendType 名称映射到 TeammateExecutor 实例的注册表。

    检测优先级管道（镜像 ``registry.ts``）：
    1. ``in_process`` — 当显式请求或无面板后端可用时。
    2. ``tmux`` — 当在 tmux 会话内且 tmux 二进制可用时。
    3. ``subprocess`` — 始终可用，作为安全回退。

    用法::

        registry = BackendRegistry()
        executor = registry.get_executor()           # 自动检测最佳后端
        executor = registry.get_executor("in_process")  # 显式选择
    """

    def __init__(self) -> None:
        """初始化后端注册表，注册内置默认后端并重置检测缓存。"""
        self._backends: dict[BackendType, TeammateExecutor] = {}
        self._detected: BackendType | None = None
        self._detection_result: BackendDetectionResult | None = None
        self._in_process_fallback_active: bool = False
        self._register_defaults()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register_backend(self, executor: TeammateExecutor) -> None:
        """在声明的 ``type`` 键下注册自定义执行器。"""
        self._backends[executor.type] = executor
        logger.debug("Registered backend: %s", executor.type)

    def detect_backend(self) -> BackendType:
        """检测并缓存最强大的可用后端。

        检测优先级：
        1. ``in_process`` — 若此前已激活 in-process 降级。
        2. ``tmux`` — 若在活跃的 tmux 会话内且 tmux 二进制可用。
        3. ``subprocess`` — 始终可用，作为安全回退。

        Returns:
            检测到的 :data:`BackendType` 字符串。
        """
        if self._detected is not None:
            logger.debug(
                "[BackendRegistry] Using cached backend detection: %s", self._detected
            )
            return self._detected

        logger.debug("[BackendRegistry] Starting backend detection...")

        # Priority 1: in-process fallback (activated after a prior failed spawn)
        if self._in_process_fallback_active:
            logger.debug(
                "[BackendRegistry] in_process fallback active — selecting in_process"
            )
            self._detected = "in_process"
            self._detection_result = BackendDetectionResult(
                backend="in_process",
                is_native=True,
            )
            return self._detected

        # Priority 2: tmux (inside session + binary available)
        inside_tmux = _detect_tmux()
        if inside_tmux:
            if "tmux" in self._backends:
                logger.debug("[BackendRegistry] Selected: tmux (running inside tmux session)")
                self._detected = "tmux"
                self._detection_result = BackendDetectionResult(
                    backend="tmux",
                    is_native=True,
                )
                return self._detected
            else:
                logger.debug(
                    "[BackendRegistry] Inside tmux but TmuxBackend not registered — "
                    "falling through to subprocess"
                )

        # Priority 3: subprocess (always available)
        logger.debug("[BackendRegistry] Selected: subprocess (default fallback)")
        self._detected = "subprocess"
        self._detection_result = BackendDetectionResult(
            backend="subprocess",
            is_native=False,
        )
        return self._detected

    def detect_pane_backend(self) -> BackendDetectionResult:
        """检测应使用哪个面板后端（tmux / iTerm2）。

        实现 TS 源码 ``detectAndGetBackend()`` 相同的优先级流程：

        1. 若在 tmux 内，始终使用 tmux。
        2. 若在 iTerm2 内且有 ``it2`` CLI，使用 iTerm2。
        3. 若在 iTerm2 内但无 ``it2``，有 tmux 可用，使用 tmux。
        4. 若在 iTerm2 内且无 tmux，抛出异常并提供安装指引。
        5. 若 tmux 二进制可用（外部会话），使用 tmux。
        6. 否则抛出异常并提供平台特定的安装指引。

        Returns:
            描述所选面板后端的 :class:`BackendDetectionResult`。

        Raises:
            RuntimeError: 无面板后端可用时。
        """
        logger.debug("[BackendRegistry] Starting pane backend detection...")

        in_tmux = _detect_tmux()
        in_iterm2 = _detect_iterm2()

        logger.debug(
            "[BackendRegistry] Environment: in_tmux=%s, in_iterm2=%s",
            in_tmux,
            in_iterm2,
        )

        # Priority 1: inside tmux — always use tmux
        if in_tmux:
            logger.debug("[BackendRegistry] Selected pane backend: tmux (inside tmux session)")
            return BackendDetectionResult(backend="tmux", is_native=True)

        # Priority 2: in iTerm2, try native panes
        if in_iterm2:
            it2_available = _is_it2_cli_available()
            logger.debug(
                "[BackendRegistry] iTerm2 detected, it2 CLI available: %s", it2_available
            )

            if it2_available:
                logger.debug("[BackendRegistry] Selected pane backend: iterm2 (native with it2 CLI)")
                return BackendDetectionResult(backend="iterm2", is_native=True)

            # it2 not available — can we fall back to tmux?
            tmux_bin = is_tmux_available()
            logger.debug(
                "[BackendRegistry] it2 not available, tmux binary available: %s", tmux_bin
            )

            if tmux_bin:
                logger.debug(
                    "[BackendRegistry] Selected pane backend: tmux (fallback in iTerm2, "
                    "it2 setup recommended)"
                )
                return BackendDetectionResult(
                    backend="tmux",
                    is_native=False,
                    needs_setup=True,
                )

            logger.debug(
                "[BackendRegistry] ERROR: in iTerm2 but no it2 CLI and no tmux"
            )
            raise RuntimeError(
                "iTerm2 detected but it2 CLI not installed.\n"
                "Install it2 with: pip install it2"
            )

        # Priority 3: not in tmux or iTerm2 — use tmux external session if available
        tmux_bin = is_tmux_available()
        logger.debug(
            "[BackendRegistry] Not in tmux or iTerm2, tmux binary available: %s", tmux_bin
        )

        if tmux_bin:
            logger.debug("[BackendRegistry] Selected pane backend: tmux (external session mode)")
            return BackendDetectionResult(backend="tmux", is_native=False)

        # No pane backend available
        logger.debug("[BackendRegistry] ERROR: No pane backend available")
        raise RuntimeError(_get_tmux_install_instructions())

    def get_executor(self, backend: BackendType | None = None) -> TeammateExecutor:
        """返回指定后端类型的 TeammateExecutor。

        Args:
            backend: 显式指定后端类型。为 None 时注册表自动检测最佳可用后端。

        Returns:
            已注册的 :class:`~openharness.swarm.types.TeammateExecutor`。

        Raises:
            KeyError: 请求的后端未注册时。
        """
        resolved = backend or self.detect_backend()
        executor = self._backends.get(resolved)
        if executor is None:
            available = list(self._backends.keys())
            raise KeyError(
                f"Backend {resolved!r} is not registered. Available: {available}"
            )
        return executor

    def get_preferred_backend(self, config: dict | None = None) -> BackendType:
        """从设置/配置返回用户偏好的后端。

        无显式偏好时回退到自动检测。

        Args:
            config: 可选设置字典。读取 ``teammate_mode`` 键
                （值：``"auto"``、``"in_process"``、``"tmux"``）。

        Returns:
            解析后的 :data:`BackendType`。
        """
        if config:
            mode = config.get("teammate_mode", "auto")
        else:
            mode = os.environ.get("OPENHARNESS_TEAMMATE_MODE", "auto")

        logger.debug("[BackendRegistry] get_preferred_backend: mode=%s", mode)

        if mode == "in_process":
            return "in_process"
        elif mode == "tmux":
            return "tmux"
        else:
            # "auto" — fall through to detection
            return self.detect_backend()

    def mark_in_process_fallback(self) -> None:
        """记录 spawn 降级到 in-process 模式。

        当无面板后端可用时调用。此后 ``get_executor()`` 将在进程
        生命周期内持续返回 in-process 后端（会话期间环境不会变化）。
        """
        logger.debug("[BackendRegistry] Marking in-process fallback as active")
        self._in_process_fallback_active = True
        # Invalidate cached detection so the next call re-detects
        self._detected = None
        self._detection_result = None

    def get_cached_detection_result(self) -> BackendDetectionResult | None:
        """返回缓存的 :class:`BackendDetectionResult`，若尚未检测则返回 None。"""
        return self._detection_result

    def available_backends(self) -> list[BackendType]:
        """返回已注册后端类型的排序列表。"""
        return sorted(self._backends.keys())  # type: ignore[return-value]

    def health_check(self) -> dict[str, Any]:
        """检查所有已注册后端的健康状况。

        Returns:
            包含 backend_name -> {available: bool, type: str} 映射的字典，
            以及可用后端总数的 total_count。
        """
        results: dict[str, dict[str, Any]] = {}
        available_count = 0

        for backend_type, executor in self._backends.items():
            is_available = executor.is_available()
            results[backend_type] = {
                "available": is_available,
                "type": str(executor.type),
            }
            if is_available:
                available_count += 1

        return {
            "backends": results,
            "total_count": available_count,
        }

    def reset(self) -> None:
        """清除检测缓存并重新注册默认后端。

        用于测试——允许环境变更后重新检测。
        """
        self._detected = None
        self._detection_result = None
        self._in_process_fallback_active = False
        self._backends.clear()
        self._register_defaults()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _register_defaults(self) -> None:
        """注册内置的默认后端实例。

        无条件注册 ``subprocess`` 后端；若平台支持 swarm mailbox，
        则同时注册 ``in_process`` 后端。Tmux 后端需在外部实现后
        通过 :meth:`register_backend` 手动注册。
        """
        from openharness.swarm.subprocess_backend import SubprocessBackend

        self._backends["subprocess"] = SubprocessBackend()
        if get_platform_capabilities().supports_swarm_mailbox:
            from openharness.swarm.in_process import InProcessBackend

            self._backends["in_process"] = InProcessBackend()

        # Tmux backend registration is deferred until implementation exists.
        # If a TmuxBackend is available it can be registered via register_backend().


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: BackendRegistry | None = None


def get_backend_registry() -> BackendRegistry:
    """返回进程级单例 BackendRegistry。

    首次调用时创建实例，后续调用直接返回缓存实例。
    在多线程环境下非线程安全，但 OpenHarness 主进程为单线程事件循环。
    """
    global _registry
    if _registry is None:
        _registry = BackendRegistry()
    return _registry


def mark_in_process_fallback() -> None:
    """模块级便捷函数：在单例注册表上标记 in-process 降级。

    当 spawn 因无面板后端可用而降级到 in-process 模式时调用，
    使后续 :meth:`BackendRegistry.get_executor` 调用持续返回 in-process 后端。
    """
    get_backend_registry().mark_in_process_fallback()
