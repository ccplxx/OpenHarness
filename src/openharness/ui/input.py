"""基于 prompt_toolkit 的输入辅助模块。

本模块提供 InputSession 类，封装 prompt_toolkit 的异步提示功能，
支持 Vim 模式和语音模式的提示符装饰，用于终端交互场景下的用户输入收集。
"""

from __future__ import annotations

from prompt_toolkit import PromptSession


class InputSession:
    """异步提示输入会话。

    封装 prompt_toolkit 的 PromptSession，提供：
    - 可定制模式装饰的提示符（vim/voice 标记）
    - 异步单行输入（prompt）
    - 异步临时问答（ask）
    """

    def __init__(self) -> None:
        self._session = PromptSession()
        self._prompt = "> "

    def set_modes(self, *, vim_enabled: bool, voice_enabled: bool) -> None:
        """根据活跃模式更新提示符装饰。

        在提示符前添加 [vim] 和/或 [voice] 标记，
        无活跃模式时恢复默认 "> " 提示符。
        """
        parts: list[str] = []
        if vim_enabled:
            parts.append("[vim]")
        if voice_enabled:
            parts.append("[voice]")
        prefix = "".join(parts)
        self._prompt = f"{prefix}> " if prefix else "> "

    async def prompt(self) -> str:
        """提示用户输入一行内容并返回。"""
        return await self._session.prompt_async(self._prompt)

    async def ask(self, question: str) -> str:
        """向用户提出临时问题并等待回答。

        使用 "[question] {question}\\n> " 格式的提示符。
        """
        prompt = f"[question] {question}\n> "
        return await self._session.prompt_async(prompt)
