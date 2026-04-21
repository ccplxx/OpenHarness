"""本地定时任务手动触发工具。

本模块提供 RemoteTriggerTool，用于按需立即执行已注册的 cron 定时任务。
根据 cron 任务配置的命令和工作目录创建子进程执行，
支持超时控制（默认 120 秒）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from openharness.services.cron import get_cron_job
from openharness.sandbox import SandboxUnavailableError
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from openharness.utils.shell import create_shell_subprocess


class RemoteTriggerToolInput(BaseModel):
    """定时任务手动触发工具的输入参数。

    Attributes:
        name: Cron 任务名称
        timeout_seconds: 超时时间（秒），范围 1-600，默认 120
    """

    name: str = Field(description="Cron job name")
    timeout_seconds: int = Field(default=120, ge=1, le=600)


class RemoteTriggerTool(BaseTool):
    """按需立即执行已注册 cron 定时任务的工具。

    根据任务配置创建子进程执行命令。
    """

    name = "remote_trigger"
    description = "Trigger a configured local cron-style job immediately."
    input_model = RemoteTriggerToolInput

    async def execute(
        self,
        arguments: RemoteTriggerToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行 cron 任务手动触发。

        查找任务配置，创建子进程执行命令，收集输出并返回。

        Args:
            arguments: 包含任务名称和超时设置的输入参数
            context: 工具执行上下文

        Returns:
            触发结果和任务输出
        """
        job = get_cron_job(arguments.name)
        if job is None:
            return ToolResult(output=f"Cron job not found: {arguments.name}", is_error=True)

        cwd = Path(job.get("cwd") or context.cwd).expanduser()
        try:
            process = await create_shell_subprocess(
                str(job["command"]),
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except SandboxUnavailableError as exc:
            return ToolResult(output=str(exc), is_error=True)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=arguments.timeout_seconds,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(
                output=f"Remote trigger timed out after {arguments.timeout_seconds} seconds",
                is_error=True,
            )

        parts = []
        if stdout:
            parts.append(stdout.decode("utf-8", errors="replace").rstrip())
        if stderr:
            parts.append(stderr.decode("utf-8", errors="replace").rstrip())
        body = "\n".join(part for part in parts if part).strip() or "(no output)"
        return ToolResult(
            output=f"Triggered {arguments.name}\n{body}",
            is_error=process.returncode != 0,
            metadata={"returncode": process.returncode},
        )
