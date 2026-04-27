"""Swarm 智能体子进程生成工具集。

本模块提供生成 Teammate 子进程所需的共享工具函数，包括：

* :func:`get_teammate_command` — 解析用于启动 teammate 进程的可执行命令，
  优先级：环境变量 > 当前 Python 解释器 > PATH 上的 entry-point。
* :func:`build_inherited_cli_flags` — 构建需从父会话传播到子进程的 CLI 标志
  （权限模式、模型覆盖、设置路径、插件目录等），所有值均经 shell 转义以防注入。
* :func:`build_inherited_env_vars` — 构建需转发给子进程的环境变量字典，
  包括 API 密钥、代理设置、CA 证书和 OpenHarness 原生配置。
* :func:`is_tmux_available` / :func:`is_inside_tmux` — tmux 环境检测辅助。

tmux 可能启动一个不继承父进程环境的新 login shell，因此本模块显式转发
关键环境变量以确保 teammate 进程能正确连接 API 提供方、代理和 CA 证书。
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys


# Environment variable to override the teammate command
TEAMMATE_COMMAND_ENV_VAR = "OPENHARNESS_TEAMMATE_COMMAND"


# ---------------------------------------------------------------------------
# Environment variables forwarded to spawned teammates.
#
# Tmux may start a fresh login shell that does NOT inherit the parent
# process environment, so we forward any of these that are set.
# ---------------------------------------------------------------------------

_TEAMMATE_ENV_VARS = [
    # --- API provider selection -------------------------------------------
    # Without these, teammates would default to the wrong endpoint provider
    # and fail all API calls (analogous to GitHub issue #23561 in the TS source).
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    # --- Config directory override ----------------------------------------
    # Allows operator-level config to be visible inside teammate processes.
    "CLAUDE_CONFIG_DIR",
    # --- Remote / CCR markers ---------------------------------------------
    # CCR-aware code paths check CLAUDE_CODE_REMOTE.  Auth finds its own
    # way; the FD env var wouldn't help across tmux boundaries anyway.
    "CLAUDE_CODE_REMOTE",
    # Auto-memory gate checks REMOTE && !MEMORY_DIR to disable memory on
    # ephemeral CCR filesystems.  Forwarding REMOTE alone would flip
    # teammates to memory-off when the parent has it on.
    "CLAUDE_CODE_REMOTE_MEMORY_DIR",
    # --- Upstream proxy settings ------------------------------------------
    # The parent's MITM relay is reachable from teammates on the same
    # container network.  Forward proxy vars so teammates route
    # customer-configured traffic through the relay for credential injection.
    # Without these, teammates bypass the proxy entirely.
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
    # --- CA bundle overrides ----------------------------------------------
    # Custom CA certificates must be visible to teammates when TLS inspection
    # is in use; missing these causes SSL verification failures.
    "SSL_CERT_FILE",
    "NODE_EXTRA_CA_CERTS",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    # --- OpenHarness-native provider settings --------------------------------
    # These are read by settings._apply_env_overrides() and must survive across
    # tmux boundaries so teammates use the same provider as the leader.
    "OPENHARNESS_API_FORMAT",
    "OPENHARNESS_BASE_URL",
    "OPENHARNESS_MODEL",
    "OPENAI_API_KEY",
]


def get_teammate_command() -> str:
    """返回用于生成 teammate 进程的可执行命令。

    解析优先级：
    1. ``OPENHARNESS_TEAMMATE_COMMAND`` 环境变量——允许运维人员
       指向特定的二进制文件或包装脚本。
    2. 当前运行 ``openharness`` 模块的 Python 解释器。
       使生成的 teammate 继承与领导者进程相同的 venv/源码树。
    3. PATH 上的 ``openharness`` entry-point（已安装包的回退）。
    """
    override = os.environ.get(TEAMMATE_COMMAND_ENV_VAR)
    if override:
        return override

    # Prefer the current interpreter so teammates inherit the same runtime and
    # editable-install source tree as the parent process.
    if sys.executable:
        return sys.executable

    entry_point = shutil.which("openharness")
    if entry_point:
        return entry_point
    return "python"


def build_inherited_cli_flags(
    *,
    model: str | None = None,
    system_prompt: str | None = None,
    system_prompt_mode: str | None = None,
    permission_mode: str | None = None,
    plan_mode_required: bool = False,
    settings_path: str | None = None,
    teammate_mode: str | None = None,
    plugin_dirs: list[str] | None = None,
    extra_flags: list[str] | None = None,
) -> list[str]:
    """构建需从当前会话传播到生成 teammate 的 CLI 标志。

    确保 teammate 继承重要的设置，如权限模式、模型选择和插件配置。

    所有标志值通过 :func:`shlex.quote` 进行 shell 转义，防止后续
    将列表拼接为 shell 命令字符串时的命令注入。

    Args:
        model: Model override to forward (e.g. ``"claude-opus-4-6"``).
        system_prompt: System prompt override to forward to the teammate.
        system_prompt_mode: One of ``"replace"``/``"default"`` or ``"append"``.
            ``append`` maps to ``--append-system-prompt``; anything else uses
            ``--system-prompt``.
        permission_mode: One of ``"bypassPermissions"``, ``"acceptEdits"``, or None.
        plan_mode_required: When True, bypass-permissions flag is suppressed
            (plan mode takes precedence over bypass for safety).
        settings_path: Path to a settings JSON file to propagate via
            ``--settings``.  Shell-quoted for safety.
        teammate_mode: Teammate execution mode (``"auto"``, ``"in_process"``,
            ``"tmux"``).  Forwarded as ``--teammate-mode`` so tmux teammates
            use the same mode as the leader.
        plugin_dirs: List of plugin directory paths.  Each is forwarded as a
            separate ``--plugin-dir <path>`` flag so inline plugins are
            visible inside teammate processes.
        extra_flags: Additional pre-built flag strings to append verbatim.
            Callers are responsible for quoting any values in these strings.

    Returns:
        可传递给 :mod:`subprocess` 的 CLI 标志字符串列表。
    """
    flags: list[str] = []

    # --- Permission mode ---------------------------------------------------
    # Plan mode takes precedence over bypass permissions for safety.
    if not plan_mode_required:
        if permission_mode == "bypassPermissions":
            flags.append("--dangerously-skip-permissions")
        elif permission_mode == "acceptEdits":
            flags.extend(["--permission-mode", "acceptEdits"])

    # --- Model override ----------------------------------------------------
    # "inherit" means use the parent's model via the OPENHARNESS_MODEL env var.
    if model and model != "inherit":
        flags.extend(["--model", shlex.quote(model)])

    # --- System prompt override ------------------------------------------
    # Agent definitions can carry a dedicated worker system prompt. Forward it
    # explicitly so subprocess teammates preserve their role/personality.
    if system_prompt:
        prompt_flag = "--append-system-prompt" if system_prompt_mode == "append" else "--system-prompt"
        flags.extend([prompt_flag, shlex.quote(system_prompt)])

    # --- Settings path propagation ----------------------------------------
    # Ensures teammates load the same settings JSON as the leader process.
    if settings_path:
        flags.extend(["--settings", shlex.quote(settings_path)])

    # --- Plugin directories -----------------------------------------------
    # Each enabled plugin directory is forwarded individually so that inline
    # plugins (loaded via --plugin-dir) are available inside teammates.
    for plugin_dir in plugin_dirs or []:
        flags.extend(["--plugin-dir", shlex.quote(plugin_dir)])

    # --- Teammate mode propagation ----------------------------------------
    # Forwards the session-level teammate mode so tmux-spawned teammates do
    # not re-detect the mode independently and possibly choose a different one.
    if teammate_mode:
        flags.extend(["--teammate-mode", shlex.quote(teammate_mode)])

    if extra_flags:
        flags.extend(extra_flags)

    return flags


def build_inherited_env_vars() -> dict[str, str]:
    """构建需转发给生成 teammate 的环境变量。

    始终包含 ``OPENHARNESS_AGENT_TEAMS=1``，以及当前进程中
    已设置的提供方/代理环境变量。

    Returns:
        要合并到子进程环境的环境变量名称 → 值字典。
    """
    env: dict[str, str] = {
        "OPENHARNESS_AGENT_TEAMS": "1",
        # Spawned workers should behave like workers, not recursively re-enter
        # coordinator mode just because the parent leader had the flag set.
        "CLAUDE_CODE_COORDINATOR_MODE": "0",
    }

    for key in _TEAMMATE_ENV_VARS:
        value = os.environ.get(key)
        if value:
            env[key] = value

    return env


def is_tmux_available() -> bool:
    """判断 ``tmux`` 二进制是否在 PATH 上可用。"""
    return shutil.which("tmux") is not None


def is_inside_tmux() -> bool:
    """判断当前进程是否运行在 tmux 会话内。"""
    return bool(os.environ.get("TMUX"))
