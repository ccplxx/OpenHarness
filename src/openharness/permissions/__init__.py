"""权限管理模块（openharness.permissions）

本模块为 OpenHarness 提供统一的权限管理能力，控制工具（Tool）在执行时是否被允许、
是否需要用户确认，以及如何处理敏感路径的访问。模块采用延迟导入（lazy import）设计，
仅在真正访问类时才加载依赖，从而减少启动时的导入开销。

主要导出：
    - PermissionChecker：权限检查器，根据权限模式和规则评估工具是否可执行。
    - PermissionDecision：权限决策结果，包含是否允许、是否需确认及原因说明。
    - PermissionMode：权限模式枚举，定义 DEFAULT / PLAN / FULL_AUTO 三种运行模式。

子模块：
    - checker：核心权限检查逻辑与敏感路径防护。
    - modes：权限模式枚举定义。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from openharness.permissions.checker import PermissionChecker, PermissionDecision
    from openharness.permissions.modes import PermissionMode

__all__ = ["PermissionChecker", "PermissionDecision", "PermissionMode"]


def __getattr__(name: str):
    """模块级延迟导入钩子。

    当外部代码从 ``openharness.permissions`` 访问 PermissionChecker、
    PermissionDecision 或 PermissionMode 时，才真正执行子模块的导入。
    这种设计避免了在包初始化阶段加载所有子模块，降低启动开销，
    同时保持 ``from openharness.permissions import PermissionChecker`` 的
    简洁导入风格。

    参数：
        name: 所请求的属性名称。

    返回：
        对应的类对象。

    异常：
        AttributeError: 当请求的属性名不在已知导出列表中时抛出。
    """
    if name in {"PermissionChecker", "PermissionDecision"}:
        from openharness.permissions.checker import PermissionChecker, PermissionDecision

        return {
            "PermissionChecker": PermissionChecker,
            "PermissionDecision": PermissionDecision,
        }[name]
    if name == "PermissionMode":
        from openharness.permissions.modes import PermissionMode

        return PermissionMode
    raise AttributeError(name)
