"""交互式权限确认对话框。

本模块提供 ask_permission 函数，在终端中以简单的 y/N 提示
请求用户确认是否允许执行具有副作用的工具操作。
"""

from __future__ import annotations

from prompt_toolkit import PromptSession


async def ask_permission(tool_name: str, reason: str) -> bool:
    """提示用户确认是否允许执行具有副作用的工具操作。

    显示 "Allow tool '{tool_name}'? [{reason}] [y/N]: " 格式的提示，
    用户输入 y 或 yes 时返回 True，否则返回 False。
    """
    session = PromptSession()
    response = await session.prompt_async(
        f"Allow tool '{tool_name}'? [{reason}] [y/N]: "
    )
    return response.strip().lower() in {"y", "yes"}
