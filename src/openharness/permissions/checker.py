"""权限检查模块（openharness.permissions.checker）

本模块实现 OpenHarness 工具执行的核心权限检查逻辑。权限检查器（PermissionChecker）
根据当前配置的权限模式、显式工具白名单/黑名单、路径规则、命令拒绝模式以及
内置敏感路径防护策略，综合判定某个工具调用是否被允许执行。

检查优先级（从高到低）：
    1. 内置敏感路径防护 — 始终生效，无法被用户配置覆盖，防止 LLM 通过提示注入
       窃取 SSH 密钥、云凭据等高价值信息。
    2. 显式工具拒绝列表（denied_tools） — 配置中明确禁止的工具直接拒绝。
    3. 显式工具允许列表（allowed_tools） — 配置中明确允许的工具直接放行。
    4. 路径级别规则（path_rules） — 基于文件路径 glob 模式的允许/拒绝规则。
    5. 命令拒绝模式（denied_commands） — 基于命令字符串 glob 模式的拒绝规则。
    6. 权限模式判定 — FULL_AUTO 全部放行，DEFAULT 对变更操作需确认，PLAN 阻止变更操作。

主要类：
    - PermissionDecision：权限决策结果数据类。
    - PathRule：基于 glob 的路径权限规则数据类。
    - PermissionChecker：权限检查器，执行上述优先级判定逻辑。
"""

from __future__ import annotations

import fnmatch  # fnmatch 是 Python 内置的轻量级模式匹配工具。当你需要 快速、简单的通配符匹配，而又不想编写复杂的正则表达式时，fnmatch 是最佳选择。
import logging
from dataclasses import dataclass

from openharness.config.settings import PermissionSettings
from openharness.permissions.modes import PermissionMode

log = logging.getLogger(__name__)

# 内置敏感路径模式元组。
#
# 定义了一系列使用 fnmatch 语法的 glob 模式，用于匹配不应被 LLM 工具
# 访问的高价值凭据和密钥文件路径。此防护始终生效，无论权限模式或用户
# 配置如何，均不可覆盖，属于纵深防御（defence-in-depth）措施。
#
# 涵盖的敏感路径类型：
#   - SSH 密钥和配置（~/.ssh/*）
#   - AWS 凭据（~/.aws/credentials、~/.aws/config）
#   - GCP 凭据（~/.config/gcloud/*）
#   - Azure 凭据（~/.azure/*）
#   - GPG 密钥（~/.gnupg/*）
#   - Docker 凭据（~/.docker/config.json）
#   - Kubernetes 凭据（~/.kube/config）
#   - OpenHarness 自身凭据存储
SENSITIVE_PATH_PATTERNS: tuple[str, ...] = (
    # SSH keys and config
    "*/.ssh/*",
    # AWS credentials
    "*/.aws/credentials",
    "*/.aws/config",
    # GCP credentials
    "*/.config/gcloud/*",
    # Azure credentials
    "*/.azure/*",
    # GPG keys
    "*/.gnupg/*",
    # Docker credentials
    "*/.docker/config.json",
    # Kubernetes credentials
    "*/.kube/config",
    # OpenHarness own credential stores
    "*/.openharness/credentials.json",
    "*/.openharness/copilot_auth.json",
)


@dataclass(frozen=True)
class PermissionDecision:
    """权限决策结果数据类。

    表示对某个工具调用进行权限检查后的判定结果。该类为不可变数据类
    （frozen=True），确保决策一旦生成就不会被意外修改。

    属性：
        allowed: 是否允许该工具执行。True 表示允许，False 表示拒绝。
        requires_confirmation: 是否需要用户确认后才能执行。仅在 DEFAULT 模式下
            对变更类工具返回 True，表示工具可执行但需用户手动批准。
        reason: 决策原因的文本说明，用于向用户展示为何允许或拒绝该操作。
    """

    allowed: bool
    requires_confirmation: bool = False
    reason: str = ""


@dataclass(frozen=True)
class PathRule:
    """基于 glob 模式的路径权限规则数据类。

    每条规则由一个 fnmatch 语法的路径模式和一个允许/拒绝标志组成，
    用于对文件路径进行细粒度的访问控制。当工具操作的文件路径匹配
    某条规则的 pattern 时，根据 allow 字段决定是放行还是拒绝。

    属性：
        pattern: fnmatch 语法的路径匹配模式，如 "*.env"、"*/secrets/*"。
        allow: True 表示允许访问匹配的路径，False 表示拒绝访问。
    """

    pattern: str
    allow: bool  # True = allow, False = deny


class PermissionChecker:
    """权限检查器，根据配置的权限模式和规则评估工具是否可执行。

    权限检查器是权限系统的核心组件，接收 PermissionSettings 配置对象，
    按照严格的优先级顺序依次检查内置敏感路径防护、工具拒绝/允许列表、
    路径规则、命令拒绝模式以及权限模式，最终输出一个 PermissionDecision
    决策结果。

    检查优先级（从高到低）：
        1. 内置敏感路径防护（SENSITIVE_PATH_PATTERNS）— 始终最高优先级，不可覆盖。
        2. 工具拒绝列表（denied_tools）— 明确禁止的工具直接拒绝。
        3. 工具允许列表（allowed_tools）— 明确允许的工具直接放行。
        4. 路径规则（path_rules）— 路径匹配的拒绝规则生效。
        5. 命令拒绝模式（denied_commands）— 命令匹配的拒绝规则生效。
        6. 权限模式判定 — 根据当前模式决定最终行为。
    """

    def __init__(self, settings: PermissionSettings) -> None:
        """初始化权限检查器。

        从 PermissionSettings 配置对象中提取路径规则并解析为 PathRule 列表。
        支持两种规则格式：对象形式（通过属性访问 pattern 和 allow 字段）和
        字典形式（通过键访问）。对于格式不正确的规则会记录警告日志并跳过。

        参数：
            settings: 权限配置对象，包含模式、工具列表、路径规则等信息。
        """
        self._settings = settings
        # Parse path rules from settings
        self._path_rules: list[PathRule] = []  # 解析读取权限配置信息
        for rule in getattr(settings, "path_rules", []):
            pattern = getattr(rule, "pattern", None) or (rule.get("pattern") if isinstance(rule, dict) else None)
            allow = getattr(rule, "allow", True) if not isinstance(rule, dict) else rule.get("allow", True)
            if isinstance(pattern, str) and pattern.strip():
                self._path_rules.append(PathRule(pattern=pattern.strip(), allow=allow))
            else:
                log.warning(
                    "Skipping path rule with missing, empty, or non-string 'pattern' field: %r",
                    rule,
                )

    def evaluate(
        self,
        tool_name: str,
        *,
        is_read_only: bool,
        file_path: str | None = None,
        command: str | None = None,
    ) -> PermissionDecision:
        """评估工具调用是否被允许执行。

        按照优先级顺序依次执行各项检查，任何一项检查产生确定结论即立即返回，
        不再继续后续检查。这是权限系统的核心判定方法。

        参数：
            tool_name: 待执行的工具名称，如 "bash"、"write_file" 等。
            is_read_only: 该工具是否为只读操作（不修改文件系统或执行状态）。
            file_path: 工具操作涉及的文件路径，可为 None。用于敏感路径防护
                和路径规则匹配。
            command: 工具执行的命令字符串，可为 None。用于命令拒绝模式匹配。

        返回：
            PermissionDecision 对象，包含是否允许、是否需确认及原因说明。
        """
        # 敏感路径检测，直接拒绝
        # Built-in sensitive path protection — always active, cannot be
        # overridden by user settings or permission mode.  This is a
        # defence-in-depth measure against LLM-directed or prompt-injection
        # driven access to credential files.
        if file_path:
            for candidate_path in _policy_match_paths(file_path):
                for pattern in SENSITIVE_PATH_PATTERNS:  # 遍历所有敏感路径
                    if fnmatch.fnmatch(candidate_path, pattern):
                        return PermissionDecision(
                            allowed=False,
                            reason=(
                                f"Access denied: {file_path} is a sensitive credential path "
                                f"(matched built-in pattern '{pattern}')"
                            ),
                        )

        # Explicit tool deny list
        if tool_name in self._settings.denied_tools:
            return PermissionDecision(allowed=False, reason=f"{tool_name} is explicitly denied")

        # Explicit tool allow list
        if tool_name in self._settings.allowed_tools:
            return PermissionDecision(allowed=True, reason=f"{tool_name} is explicitly allowed")

        # Check path-level rules
        if file_path and self._path_rules:
            for candidate_path in _policy_match_paths(file_path):
                for rule in self._path_rules:
                    if fnmatch.fnmatch(candidate_path, rule.pattern):
                        if not rule.allow:
                            return PermissionDecision(
                                allowed=False,
                                reason=f"Path {file_path} matches deny rule: {rule.pattern}",
                            )

        # Check command deny patterns (e.g. deny "rm -rf /")
        if command:
            for pattern in getattr(self._settings, "denied_commands", []):
                if isinstance(pattern, str) and fnmatch.fnmatch(command, pattern):
                    return PermissionDecision(
                        allowed=False,
                        reason=f"Command matches deny pattern: {pattern}",
                    )

        # Full auto: allow everything
        if self._settings.mode == PermissionMode.FULL_AUTO:
            return PermissionDecision(allowed=True, reason="Auto mode allows all tools")

        # Read-only tools always allowed
        if is_read_only:
            return PermissionDecision(allowed=True, reason="read-only tools are allowed")

        # Plan mode: block mutating tools
        if self._settings.mode == PermissionMode.PLAN:
            return PermissionDecision(
                allowed=False,
                reason="Plan mode blocks mutating tools until the user exits plan mode",
            )

        # Default mode: require confirmation for mutating tools
        bash_hint = _bash_permission_hint(command)
        reason = (
            "Mutating tools require user confirmation in default mode. "
            "Approve the prompt when asked, or run /permissions full_auto "
            "if you want to allow them for this session."
        )
        if bash_hint:
            reason = f"{reason} {bash_hint}"
        return PermissionDecision(
            allowed=False,
            requires_confirmation=True,
            reason=reason,
        )


def _policy_match_paths(file_path: str) -> tuple[str, ...]:
    """返回应参与策略匹配的路径形式。

    某些目录范围工具（如 grep、glob）可能以目录根路径作为操作目标，
    例如 ``/home/user/.ssh``。为了使 glob 风格的拒绝模式（如 ``*/.ssh/*``
    和 ``/etc/*``）也能匹配目录根本身，本函数在原始路径的基础上额外
    返回带尾部斜杠的变体。

    参数：
        file_path: 待匹配的文件或目录路径。

    返回：
        包含原始路径（去除尾部斜杠）和带尾部斜杠路径的元组。
        若路径去除斜杠后为空，则仅返回原始路径。
    """
    normalized = file_path.rstrip("/")
    if not normalized:
        return (file_path,)
    return (normalized, normalized + "/")


def _bash_permission_hint(command: str | None) -> str:
    """为 Bash 命令生成权限提示信息。

    检测命令是否为包安装或项目脚手架创建命令（如 npm install、pip install、
    create-next-app 等），若是则返回一条提示信息，说明此类命令会变更工作区，
    因此在默认模式下不会自动执行。该提示会附加到 PermissionDecision 的
    reason 字段中，帮助用户理解为何需要确认。

    参数：
        command: 待检测的命令字符串，可为 None。

    返回：
        若命令匹配已知的安装/脚手架模式，返回提示文本；否则返回空字符串。
    """
    if not command:
        return ""
    lowered = command.lower()
    install_markers = (
        "npm install",
        "pnpm install",
        "yarn install",
        "bun install",
        "pip install",
        "uv pip install",
        "poetry install",
        "cargo install",
        "create-next-app",
        "npm create ",
        "pnpm create ",
        "yarn create ",
        "bun create ",
        "npx create-",
        "npm init ",
        "pnpm init ",
        "yarn init ",
    )
    if any(marker in lowered for marker in install_markers):
        return (
            "Package installation and scaffolding commands change the workspace, "
            "so they will not run automatically in default mode."
        )
    return ""
