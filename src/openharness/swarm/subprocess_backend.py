"""基于子进程的 TeammateExecutor 实现。

本模块实现 :class:`SubprocessBackend`，将每个 teammate 作为独立子进程运行，
通过 :class:`~openharness.tasks.manager.BackgroundTaskManager` 创建和管理子进程，
利用 stdin/stdout JSON 管道进行消息通信。

子进程后端始终可用（无外部依赖），是所有后端中的安全回退方案。
生成时自动构建继承自父会话的 CLI 标志和环境变量，确保子进程
使用与 leader 相同的模型、权限模式和 API 提供方配置。
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from openharness.swarm.spawn_utils import (
    build_inherited_cli_flags,
    build_inherited_env_vars,
    get_teammate_command,
)
from openharness.swarm.types import (
    BackendType,
    SpawnResult,
    TeammateMessage,
    TeammateSpawnConfig,
)
from openharness.tasks.manager import get_task_manager

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SubprocessBackend:
    """将每个 teammate 作为独立子进程运行的 TeammateExecutor。

    使用现有的 :class:`~openharness.tasks.manager.BackgroundTaskManager`
    创建和管理子进程，通过 stdin/stdout 进行通信。
    """

    type: BackendType = "subprocess"

    # Maps agent_id -> task_id for tracking live agents
    _agent_tasks: dict[str, str]

    def __init__(self) -> None:
        """初始化 SubprocessBackend，创建智能体到任务 ID 的映射表。"""
        self._agent_tasks = {}

    def is_available(self) -> bool:
        """子进程后端始终可用。"""
        return True

    async def spawn(self, config: TeammateSpawnConfig) -> SpawnResult:
        """通过任务管理器将 teammate 作为子进程生成。

        构建适当的 CLI 命令，创建通过 stdin 接受初始提示的
        ``local_agent`` 任务。
        """
        agent_id = f"{config.name}@{config.team}"

        flags = build_inherited_cli_flags(
            model=config.model,
            plan_mode_required=config.plan_mode_required,
        )
        extra_env = build_inherited_env_vars()

        command = config.command
        if command is None:
            # Build environment export prefix for shell invocation
            env_prefix = " ".join(f"{k}={v!r}" for k, v in extra_env.items())

            teammate_cmd = get_teammate_command()
            if (
                teammate_cmd.endswith("python")
                or teammate_cmd.endswith("python3")
                or "/python" in teammate_cmd
            ):
                cmd_parts = [teammate_cmd, "-m", "openharness", "--task-worker"] + flags
            else:
                cmd_parts = [teammate_cmd, "--task-worker"] + flags
            command = f"{env_prefix} {' '.join(cmd_parts)}" if env_prefix else " ".join(cmd_parts)

        manager = get_task_manager()
        try:
            record = await manager.create_agent_task(
                prompt=config.prompt,
                description=f"Teammate: {agent_id}",
                cwd=config.cwd,
                task_type=config.task_type,
                model=config.model,
                command=command,
            )
        except Exception as exc:
            logger.error("Failed to spawn teammate %s: %s", agent_id, exc)
            return SpawnResult(
                task_id="",
                agent_id=agent_id,
                backend_type=self.type,
                success=False,
                error=str(exc),
            )

        self._agent_tasks[agent_id] = record.id
        logger.debug("Spawned teammate %s as task %s", agent_id, record.id)
        return SpawnResult(
            task_id=record.id,
            agent_id=agent_id,
            backend_type=self.type,
        )

    async def send_message(self, agent_id: str, message: TeammateMessage) -> None:
        """通过 stdin 管道向运行中的 teammate 发送消息。

        消息序列化为单行 JSON，使 teammate 能区分结构化消息与普通提示。
        """
        task_id = self._agent_tasks.get(agent_id)
        if task_id is None:
            raise ValueError(f"No active subprocess for agent {agent_id!r}")

        payload = {
            "text": message.text,
            "from": message.from_agent,
            "timestamp": message.timestamp,
        }
        if message.color:
            payload["color"] = message.color
        if message.summary:
            payload["summary"] = message.summary

        manager = get_task_manager()
        await manager.write_to_task(task_id, json.dumps(payload))
        logger.debug("Sent message to %s (task %s)", agent_id, task_id)

    async def shutdown(self, agent_id: str, *, force: bool = False) -> bool:
        """终止子进程 teammate。

        Args:
            agent_id: 待终止的智能体。
            force: 子进程后端忽略此参数；始终发送 SIGTERM，
                短暂等待后发送 SIGKILL（由任务管理器处理）。

        Returns:
            找到并终止任务返回 True。
        """
        task_id = self._agent_tasks.get(agent_id)
        if task_id is None:
            logger.warning("shutdown() called for unknown agent %s", agent_id)
            return False

        manager = get_task_manager()
        try:
            await manager.stop_task(task_id)
        except ValueError as exc:
            logger.debug("stop_task for %s: %s", task_id, exc)
            # Task may have already finished — still clean up mapping
        finally:
            self._agent_tasks.pop(agent_id, None)

        logger.debug("Shut down teammate %s (task %s)", agent_id, task_id)
        return True

    def get_task_id(self, agent_id: str) -> str | None:
        """返回指定智能体在任务管理器中的任务 ID。

        Args:
            agent_id: 智能体标识符（格式 ``name@team``）。

        Returns:
            任务 ID 字符串，若未知则返回 None。
        """
        return self._agent_tasks.get(agent_id)
