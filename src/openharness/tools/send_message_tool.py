"""向运行中的代理任务发送消息的工具。

本模块提供 SendMessageTool，用于向正在运行的本地代理任务发送后续消息。
支持两种消息路由方式：
- 普通任务：通过 task_id 直接写入任务 stdin
- Swarm 代理：通过 agent_id（格式 name@team）经由后端发送 TeammateMessage
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from openharness.swarm.registry import get_backend_registry
from openharness.swarm.types import TeammateMessage
from openharness.tasks.manager import get_task_manager
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult

logger = logging.getLogger(__name__)


class SendMessageToolInput(BaseModel):
    """向运行中任务发送消息工具的输入参数。

    Attributes:
        task_id: 目标任务的 task_id 或 swarm agent_id（格式 name@team）
        message: 要发送给任务的消息文本
    """

    task_id: str = Field(description="Target local agent task id or swarm agent_id (name@team)")
    message: str = Field(description="Message to write to the task stdin")


class SendMessageTool(BaseTool):
    """向运行中的本地代理任务发送后续消息的工具。

    支持普通任务和 Swarm 代理两种路由方式。
    """

    name = "send_message"
    description = "Send a follow-up message to a running local agent task."
    input_model = SendMessageToolInput

    async def execute(self, arguments: SendMessageToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行消息发送。

        根据 task_id 格式判断路由方式：含 @ 的走 Swarm 通道，否则走 TaskManager。

        Args:
            arguments: 包含目标 ID 和消息内容的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            发送确认信息
        """
        del context
        # Swarm agents use agent_id format (name@team); legacy tasks use plain task IDs
        if "@" in arguments.task_id:
            return await self._send_swarm_message(arguments.task_id, arguments.message)
        try:
            await get_task_manager().write_to_task(arguments.task_id, arguments.message)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"Sent message to task {arguments.task_id}")

    async def _send_swarm_message(self, agent_id: str, message: str) -> ToolResult:
        """通过 Swarm 后端向代理发送消息。

        使用 subprocess 后端匹配 AgentTool 的生成路径，
        SubprocessBackend 维护 agent_id 到 task_id 的映射。

        Args:
            agent_id: Swarm 代理 ID（格式 name@team）
            message: 消息文本

        Returns:
            发送确认信息或错误
        """
        registry = get_backend_registry()
        # Use subprocess backend to match AgentTool's spawn path.
        # The SubprocessBackend tracks agent_id -> task_id mappings so
        # send_message resolves correctly for any agent spawned by AgentTool.
        executor = registry.get_executor("subprocess")

        teammate_msg = TeammateMessage(text=message, from_agent="coordinator")
        try:
            await executor.send_message(agent_id, teammate_msg)
        except ValueError as exc:
            return ToolResult(output=str(exc), is_error=True)
        except Exception as exc:
            logger.error("Failed to send message to %s: %s", agent_id, exc)
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=f"Sent message to agent {agent_id}")
