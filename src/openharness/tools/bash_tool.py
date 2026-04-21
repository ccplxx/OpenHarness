"""Shell 命令执行工具。

本模块提供 BashTool，用于在本地仓库中执行 Shell 命令。核心特性包括：
- 支持 PTY 模式运行，兼容需要伪终端的命令
- 超时控制：默认 600 秒，超时后强制终止进程并返回已收集的部分输出
- 交互式命令检测：在执行前检测可能需要交互输入的脚手架命令（如 create-next-app），
  并提示用户使用非交互式标志
- 输出截断：超过 12000 字符的输出会被截断
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel, Field

from openharness.sandbox import SandboxUnavailableError
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.utils.shell import create_shell_subprocess


class BashToolInput(BaseModel):
    """Shell 命令执行工具的输入参数。

    Attributes:
        command: 要执行的 Shell 命令
        cwd: 可选的工作目录覆盖
        timeout_seconds: 超时时间（秒），范围 1-600，默认 600
    """

    command: str = Field(description="Shell command to execute")
    cwd: str | None = Field(default=None, description="Working directory override")
    timeout_seconds: int = Field(default=600, ge=1, le=600)


class BashTool(BaseTool):
    """执行 Shell 命令并捕获标准输出和标准错误的工具。

    支持 PTY 模式运行、超时控制、交互式命令预检测和输出截断。
    """

    name = "bash"
    description = "Run a shell command in the local repository."
    input_model = BashToolInput

    async def execute(self, arguments: BashToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行 Shell 命令。

        首先进行交互式命令预检测，然后创建子进程执行命令。
        超时时强制终止进程并返回部分输出；正常完成后返回完整输出。

        Args:
            arguments: 包含命令、工作目录和超时设置的输入参数
            context: 工具执行上下文

        Returns:
            包含命令输出的 ToolResult，超时或非零返回码时 is_error 为 True
        """
        cwd = Path(arguments.cwd).expanduser() if arguments.cwd else context.cwd
        preflight_error = _preflight_interactive_command(arguments.command)
        if preflight_error is not None:
            return ToolResult(
                output=preflight_error,
                is_error=True,
                metadata={"interactive_required": True},
            )
        process: asyncio.subprocess.Process | None = None
        try:
            process = await create_shell_subprocess(
                arguments.command,
                cwd=cwd,
                prefer_pty=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except SandboxUnavailableError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except asyncio.CancelledError:
            if process is not None:
                await _terminate_process(process, force=False)
            raise

        try:
            await asyncio.wait_for(process.wait(), timeout=arguments.timeout_seconds)
        except asyncio.TimeoutError:
            output_buffer = await _drain_available_output(process.stdout)
            await _terminate_process(process, force=True)
            output_buffer.extend(await _read_remaining_output(process))
            return ToolResult(
                output=_format_timeout_output(
                    output_buffer,
                    command=arguments.command,
                    timeout_seconds=arguments.timeout_seconds,
                ),
                is_error=True,
                metadata={"returncode": process.returncode, "timed_out": True},
            )
        except asyncio.CancelledError:
            await _terminate_process(process, force=False)
            raise

        output_buffer = await _read_remaining_output(process)
        text = _format_output(output_buffer)
        return ToolResult(
            output=text,
            is_error=process.returncode != 0,
            metadata={"returncode": process.returncode},
        )


async def _terminate_process(process: asyncio.subprocess.Process, *, force: bool) -> None:
    """终止子进程。

    优先发送 SIGTERM 信号，若 2 秒内进程未退出则发送 SIGKILL 强制终止。

    Args:
        process: 要终止的异步子进程
        force: 是否直接使用 SIGKILL 强制终止
    """
    if process.returncode is not None:
        return
    if force:
        process.kill()
        await process.wait()
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


async def _read_remaining_output(process: asyncio.subprocess.Process) -> bytearray:
    """读取子进程剩余的全部输出。

    Args:
        process: 已结束的异步子进程

    Returns:
        包含所有剩余 stdout 输出的 bytearray
    """
    output_buffer = bytearray()
    if process.stdout is not None:
        output_buffer.extend(await process.stdout.read())
    return output_buffer


async def _drain_available_output(
    stream: asyncio.StreamReader | None,
    *,
    read_timeout: float = 0.05,
) -> bytearray:
    """排空流中当前可用的输出数据。

    以短超时（默认 0.05 秒）循环读取流数据，直到流结束或超时。

    Args:
        stream: 异步流读取器，可为 None
        read_timeout: 每次读取的超时时间（秒）

    Returns:
        收集到的输出数据 bytearray
    """
    output_buffer = bytearray()
    if stream is None:
        return output_buffer
    while True:
        try:
            chunk = await asyncio.wait_for(stream.read(65536), timeout=read_timeout)
        except asyncio.TimeoutError:
            return output_buffer
        if not chunk:
            return output_buffer
        output_buffer.extend(chunk)


def _format_output(output_buffer: bytearray) -> str:
    """格式化输出缓冲区为文本字符串。

    将 bytearray 解码为 UTF-8 文本，替换不可解码字符，统一换行符，
    截断超过 12000 字符的输出。

    Args:
        output_buffer: 原始输出字节缓冲区

    Returns:
        格式化后的文本字符串
    """
    text = output_buffer.decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
    if not text:
        return "(no output)"
    if len(text) > 12000:
        return f"{text[:12000]}\n...[truncated]..."
    return text


def _format_timeout_output(output_buffer: bytearray, *, command: str, timeout_seconds: int) -> str:
    """格式化超时输出信息。

    包含超时提示、部分输出内容和交互式命令建议。

    Args:
        output_buffer: 超时前收集的输出字节
        command: 执行的命令
        timeout_seconds: 超时时间（秒）

    Returns:
        格式化后的超时输出文本
    """
    parts = [f"Command timed out after {timeout_seconds} seconds."]
    text = _format_output(output_buffer)
    if text != "(no output)":
        parts.extend(["", "Partial output:", text])
    hint = _interactive_command_hint(command=command, output=text)
    if hint:
        parts.extend(["", hint])
    return "\n".join(parts)


def _preflight_interactive_command(command: str) -> str | None:
    """预检测命令是否需要交互式输入。

    检查命令是否为脚手架命令（如 create-next-app）且未使用非交互式标志。
    如果检测到交互式命令，返回提示信息；否则返回 None。

    Args:
        command: 要检测的 Shell 命令

    Returns:
        错误提示字符串或 None
    """
    lowered_command = command.lower()
    if not _looks_like_interactive_scaffold(lowered_command):
        return None
    return (
        "This command appears to require interactive input before it can continue. "
        "The bash tool is non-interactive, so it cannot answer installer/scaffold prompts live. "
        "Prefer non-interactive flags (for example --yes, -y, --skip-install, --defaults, --non-interactive), "
        "or run the scaffolding step once in an external terminal before asking the agent to continue."
    )


def _interactive_command_hint(*, command: str, output: str) -> str | None:
    """生成交互式命令建议提示。

    根据命令或输出判断是否需要交互式输入，若需要则返回建议信息。

    Args:
        command: 执行的命令
        output: 命令输出文本

    Returns:
        建议提示字符串或 None
    """
    lowered_command = command.lower()
    if _looks_like_interactive_scaffold(lowered_command) or _looks_like_prompt(output):
        return (
            "This command appears to require interactive input. "
            "The bash tool is non-interactive, so prefer non-interactive flags "
            "(for example --yes, -y, --skip-install, or similar) or run the "
            "scaffolding step once in an external terminal before continuing."
        )
    return None


def _looks_like_interactive_scaffold(lowered_command: str) -> bool:
    """判断命令是否看起来像交互式脚手架命令。

    检查命令是否包含脚手架标记（如 npx create-）且不包含非交互式标记（如 --yes）。

    Args:
        lowered_command: 小写化的命令字符串

    Returns:
        若为交互式脚手架命令返回 True
    """
    scaffold_markers: tuple[str, ...] = (
        "create-next-app",
        "npm create ",
        "pnpm create ",
        "yarn create ",
        "bun create ",
        "pnpm dlx ",
        "npm init ",
        "pnpm init ",
        "yarn init ",
        "bunx create-",
        "npx create-",
    )
    non_interactive_markers: tuple[str, ...] = (
        "--yes",
        " -y",
        "--skip-install",
        "--defaults",
        "--non-interactive",
        "--ci",
    )
    return any(marker in lowered_command for marker in scaffold_markers) and not any(
        marker in lowered_command for marker in non_interactive_markers
    )


def _looks_like_prompt(output: str) -> bool:
    """判断输出文本是否看起来像交互式提示。

    检查输出中是否包含提示标记（如 "would you like"、"select an option"、"?" 等）。

    Args:
        output: 命令输出文本

    Returns:
        若输出包含交互式提示标记返回 True
    """
    if not output:
        return False
    prompt_markers: Iterable[str] = (
        "would you like",
        "ok to proceed",
        "select an option",
        "which",
        "press enter to continue",
        "?",
    )
    lowered_output = output.lower()
    return any(marker in lowered_output for marker in prompt_markers)
