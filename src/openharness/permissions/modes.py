"""权限模式定义模块（openharness.permissions.modes）

本模块定义了 OpenHarness 支持的权限运行模式枚举 ``PermissionMode``。
权限模式决定了工具（Tool）在执行时的行为策略——是否自动执行、是否需要用户确认、
或者仅处于规划状态而阻止变更操作。

三种模式：
    - DEFAULT（默认模式）：只读工具自动执行，变更类工具需用户确认。
    - PLAN（规划模式）：只读工具自动执行，所有变更类工具被阻止，直到用户退出规划模式。
    - FULL_AUTO（全自动模式）：所有工具自动执行，无需用户确认。
"""

from __future__ import annotations

from enum import Enum


class PermissionMode(str, Enum):
    """权限运行模式枚举。

    继承自 ``str`` 和 ``Enum``，使得枚举值可直接作为字符串使用，
    便于配置文件序列化与比较操作。

    模式说明：
        DEFAULT: 默认模式。只读工具自动放行，变更类（mutating）工具需用户确认。
                 适用于日常交互场景，在安全性与效率之间取得平衡。

        PLAN: 规划模式。只读工具自动放行，所有变更类工具被阻止。
              适用于用户只想让 AI 分析和规划、但不希望执行任何修改的场景。
              用户退出规划模式后，变更操作才能继续。

        FULL_AUTO: 全自动模式。所有工具（包括变更类）自动放行，无需用户确认。
                   适用于自动化流水线或用户对 AI 行为高度信任的场景，但风险较高。
    """

    DEFAULT = "default"  # 默认模式，只读 + 确认
    PLAN = "plan"  # 规划模式，只读不写
    FULL_AUTO = "full_auto"  # 全自动
