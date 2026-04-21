"""进程内智能体执行后端模块。

在当前 Python 进程内将 teammate 智能体作为 asyncio Task 运行，
使用 :mod:`contextvars` 实现每个 teammate 的上下文隔离（Python 版
Node.js AsyncLocalStorage）。

架构概览
--------
* :class:`TeammateAbortController` — 双信号终止控制器，提供优雅取消
  和强制终止两种语义。
* :class:`TeammateContext` — 数据类，持有身份标识、终止控制器和
  运行时统计（tool_use_count、total_tokens、status）。
* :func:`get_teammate_context` / :func:`set_teammate_context` — ContextVar
  访问器，使 teammate 任务内的任何代码无需显式参数传递即可获取自身身份。
* :func:`start_in_process_teammate` — 实际的协程函数，负责设置上下文、
  驱动查询引擎循环和退出清理。
* :class:`InProcessBackend` — 实现
  :class:`~openharness.swarm.types.TeammateExecutor` 协议，
  管理活跃 asyncio Task 字典。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from openharness.swarm.mailbox import (
    TeammateMailbox,
    create_idle_notification,
)
from openharness.swarm.types import (
    BackendType,
    SpawnResult,
    TeammateMessage,
    TeammateSpawnConfig,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abort controller
# ---------------------------------------------------------------------------


class TeammateAbortController:
    """进程内 teammate 的双信号终止控制器。

    提供*优雅*取消（设置 ``cancel_event``，智能体完成当前工具调用后退出）
    和*强制*终止（设置 ``force_cancel``，立即取消 asyncio Task）两种语义。

    镜像 TS 源码 ``spawnInProcess.ts`` 和 ``InProcessBackend.ts`` 中的
    ``AbortController`` / 链式控制器模式。
    """

    def __init__(self) -> None:
        """初始化双信号终止控制器，创建优雅取消和强制终止两个事件。"""
        self.cancel_event: asyncio.Event = asyncio.Event()
        """设置后请求优雅取消智能体循环。"""

        self.force_cancel: asyncio.Event = asyncio.Event()
        """设置后请求立即（强制）终止。"""

        self._reason: str | None = None

    @property
    def is_cancelled(self) -> bool:
        """若任一取消信号已设置则返回 True。"""
        return self.cancel_event.is_set() or self.force_cancel.is_set()

    def request_cancel(self, reason: str | None = None, *, force: bool = False) -> None:
        """请求取消 teammate。

        Args:
            reason: 取消原因的可读字符串（用于日志记录）。
            force: 为 True 时设置 ``force_cancel`` 实现立即终止；
                   为 False 时设置 ``cancel_event`` 实现优雅关闭。
        """
        self._reason = reason
        if force:
            logger.debug(
                "[TeammateAbortController] Force-cancel requested: %s", reason or "(no reason)"
            )
            self.force_cancel.set()
            self.cancel_event.set()  # Also set graceful so both checks fire
        else:
            logger.debug(
                "[TeammateAbortController] Graceful cancel requested: %s",
                reason or "(no reason)",
            )
            self.cancel_event.set()

    @property
    def reason(self) -> str | None:
        """最近一次 :meth:`request_cancel` 调用提供的原因。"""
        return self._reason


# ---------------------------------------------------------------------------
# Per-teammate context isolation via ContextVar
# ---------------------------------------------------------------------------


TeammateStatus = Literal["starting", "running", "idle", "stopping", "stopped"]


@dataclass
class TeammateContext:
    """每个 teammate 的隔离状态，必须在并发智能体间相互隔离。

    存储在 :data:`ContextVar` 中，使每个 asyncio Task 看到自己的副本，
    无需加锁。
    """

    agent_id: str
    """唯一智能体标识符（格式 ``agentName@teamName``）。"""

    agent_name: str
    """人类可读名称，如 ``"researcher"``。"""

    team_name: str
    """该 teammate 所属的团队名称。"""

    parent_session_id: str | None = None
    """生成此 teammate 的领导者会话 ID，用于对话记录关联。"""

    color: str | None = None
    """可选的 UI 颜色字符串。"""

    plan_mode_required: bool = False
    """该智能体是否必须在实施前进入计划模式。"""

    abort_controller: TeammateAbortController = field(
        default_factory=TeammateAbortController
    )
    """双信号终止控制器（优雅取消 + 强制终止）。"""

    message_queue: asyncio.Queue[TeammateMessage] = field(
        default_factory=asyncio.Queue
    )
    """轮次间传递的待处理消息队列。

    执行循环在查询迭代之间排空此队列，使来自领导者的消息
    作为新的用户轮次注入而非丢失。
    """

    status: TeammateStatus = "starting"
    """该 teammate 的生命周期状态。"""

    started_at: float = field(default_factory=time.time)
    """该 teammate 生成时的 Unix 时间戳。"""

    tool_use_count: int = 0
    """该 teammate 生命周期内的工具调用次数。"""

    total_tokens: int = 0
    """所有查询轮次的累积 token 数（输入 + 输出）。"""

    # Backwards-compatible shim so existing code that reads ``cancel_event``
    # continues to work without modification.
    @property
    def cancel_event(self) -> asyncio.Event:
        """优雅取消事件（委托到 :attr:`abort_controller`）。"""
        return self.abort_controller.cancel_event


_teammate_context_var: ContextVar[TeammateContext | None] = ContextVar(
    "_teammate_context_var", default=None
)


def get_teammate_context() -> TeammateContext | None:
    """返回当前运行的 teammate 任务的 :class:`TeammateContext`。

    在进程内 teammate 之外调用时返回 ``None``。
    """
    return _teammate_context_var.get()


def set_teammate_context(ctx: TeammateContext) -> None:
    """将 *ctx* 绑定到当前异步上下文（任务本地）。"""
    _teammate_context_var.set(ctx)


# ---------------------------------------------------------------------------
# Agent execution loop
# ---------------------------------------------------------------------------


async def start_in_process_teammate(
    *,
    config: TeammateSpawnConfig,
    agent_id: str,
    abort_controller: TeammateAbortController,
    query_context: Any | None = None,
) -> None:
    """运行进程内 teammate 的智能体查询循环。

    此协程由 :class:`InProcessBackend` 作为 :class:`asyncio.Task` 启动，执行：

    1. 将新的 :class:`TeammateContext` 绑定到当前异步上下文。
    2. 驱动查询引擎循环（复用
       :func:`~openharness.engine.query.run_query`）。
    3. 在轮次间轮询 teammate 的邮箱，处理传入消息和关闭请求。
       任何 ``user_message`` 被推入上下文的
       :attr:`~TeammateContext.message_queue` 并作为额外的用户轮次注入。
    4. 完成后向领导者写入空闲通知。
    5. 正常退出或取消时执行清理。

    Args:
        config: 来自领导者的生成配置。
        agent_id: 完全限定的智能体标识符（格式 ``name@team``）。
        abort_controller: 该 teammate 的双信号终止控制器。
        query_context: 可选的预构建
            :class:`~openharness.engine.query.QueryContext`。
            为 None 时运行存根模式，尊重取消信号以便测试和直接调用。
    """
    ctx = TeammateContext(
        agent_id=agent_id,
        agent_name=config.name,
        team_name=config.team,
        parent_session_id=config.parent_session_id,
        color=config.color,
        plan_mode_required=config.plan_mode_required,
        abort_controller=abort_controller,
        started_at=time.time(),
        status="starting",
    )
    set_teammate_context(ctx)

    mailbox = TeammateMailbox(team_name=config.team, agent_id=agent_id)

    logger.debug("[in_process] %s: starting", agent_id)

    try:
        ctx.status = "running"

        if query_context is not None:
            await _run_query_loop(query_context, config, ctx, mailbox)
        else:
            # Minimal stub: log that we received the prompt and honour cancel.
            # Replace this branch with a real QueryContext builder once the
            # harness wires up the full engine for in-process teammates.
            logger.info(
                "[in_process] %s: no query_context supplied — stub run for prompt: %.80s",
                agent_id,
                config.prompt,
            )
            ctx.status = "idle"
            for _ in range(10):
                if abort_controller.is_cancelled:
                    logger.debug("[in_process] %s: cancelled during stub run", agent_id)
                    return
                await asyncio.sleep(0.1)

    except asyncio.CancelledError:
        logger.debug("[in_process] %s: task cancelled", agent_id)
        raise
    except Exception:
        logger.exception("[in_process] %s: unhandled exception in agent loop", agent_id)
    finally:
        ctx.status = "stopped"
        # Notify the leader that this teammate has gone idle / finished.
        with contextlib.suppress(Exception):
            idle_msg = create_idle_notification(
                sender=agent_id,
                recipient="leader",
                summary=f"{config.name} finished (tools={ctx.tool_use_count}, tokens={ctx.total_tokens})",
            )
            leader_mailbox = TeammateMailbox(team_name=config.team, agent_id="leader")
            await leader_mailbox.write(idle_msg)

        logger.debug(
            "[in_process] %s: exiting (tools=%d, tokens=%d)",
            agent_id,
            ctx.tool_use_count,
            ctx.total_tokens,
        )


async def _drain_mailbox(
    mailbox: TeammateMailbox,
    ctx: TeammateContext,
) -> bool:
    """读取待处理的邮箱消息，处理关闭请求和用户消息。

    Returns:
        若收到关闭消息返回 True（调用方应停止循环）。
    """
    try:
        pending = await mailbox.read_all(unread_only=True)
    except Exception:
        pending = []

    for msg in pending:
        try:
            await mailbox.mark_read(msg.id)
        except Exception:
            pass

        if msg.type == "shutdown":
            logger.debug("[in_process] %s: received shutdown message", ctx.agent_id)
            ctx.abort_controller.request_cancel(reason="shutdown message received")
            return True

        elif msg.type == "user_message":
            # Enqueue the message so the query loop can inject it as a new turn.
            logger.debug("[in_process] %s: queuing user_message from mailbox", ctx.agent_id)
            content = msg.payload.get("content", "") if isinstance(msg.payload, dict) else str(msg.payload)
            teammate_msg = TeammateMessage(
                text=content,
                from_agent=msg.sender,
                color=msg.payload.get("color") if isinstance(msg.payload, dict) else None,
                timestamp=str(msg.timestamp),
            )
            await ctx.message_queue.put(teammate_msg)

    return False


async def _run_query_loop(
    query_context: Any,
    config: TeammateSpawnConfig,
    ctx: TeammateContext,
    mailbox: TeammateMailbox,
) -> None:
    """驱动 :func:`~openharness.engine.query.run_query` 直到完成或取消。

    在轮次之间执行：
    - 排空邮箱，处理关闭请求和用户消息。
    - 将排队的用户消息注入为新轮次。
    - 检查终止控制器。
    - 跟踪 tool_use_count 和 total_tokens。
    """
    # Deferred import to avoid circular dependencies at module load time.
    from openharness.engine.query import run_query
    from openharness.engine.messages import ConversationMessage

    messages: list[ConversationMessage] = [
        ConversationMessage.from_user_text(config.prompt)
    ]

    async for event, usage in run_query(query_context, messages):
        # Track token usage if usage info is provided
        if usage is not None:
            with contextlib.suppress(AttributeError, TypeError):
                ctx.total_tokens += getattr(usage, "input_tokens", 0)
                ctx.total_tokens += getattr(usage, "output_tokens", 0)

        # Track tool use events
        with contextlib.suppress(AttributeError, TypeError):
            if getattr(event, "type", None) in ("tool_use", "tool_call"):
                ctx.tool_use_count += 1

        # Check for cancellation or shutdown between events
        if ctx.abort_controller.is_cancelled:
            logger.debug(
                "[in_process] %s: abort_controller cancelled, stopping query loop",
                ctx.agent_id,
            )
            return

        # Drain mailbox — handle shutdown requests immediately
        should_stop = await _drain_mailbox(mailbox, ctx)
        if should_stop:
            return

        # Drain message queue and inject as new turns
        while not ctx.message_queue.empty():
            try:
                queued = ctx.message_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            logger.debug(
                "[in_process] %s: injecting queued message from %s",
                ctx.agent_id,
                queued.from_agent,
            )
            messages.append(ConversationMessage(role="user", content=queued.text))

    ctx.status = "idle"


# ---------------------------------------------------------------------------
# InProcessBackend
# ---------------------------------------------------------------------------


@dataclass
class _TeammateEntry:
    """进程内 teammate 的注册条目。

    Attributes:
        task: 关联的 asyncio Task 对象。
        abort_controller: 双信号终止控制器。
        task_id: 任务管理器中的任务 ID。
        started_at: 启动时的 Unix 时间戳。
    """

    task: asyncio.Task[None]
    abort_controller: TeammateAbortController
    task_id: str
    started_at: float = field(default_factory=time.time)


class InProcessBackend:
    """将智能体作为 asyncio Task 在当前进程内运行的 TeammateExecutor。

    通过 :mod:`contextvars` 提供上下文隔离：每个生成的
    :class:`asyncio.Task` 运行在自己的上下文副本中，
    因此 :func:`get_teammate_context` 为每个并发智能体返回正确的身份。
    """

    type: BackendType = "in_process"

    def __init__(self) -> None:
        """初始化 InProcessBackend，创建活跃智能体注册表。"""
        # Maps agent_id -> _TeammateEntry
        self._active: dict[str, _TeammateEntry] = {}

    # ------------------------------------------------------------------
    # TeammateExecutor protocol
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """进程内后端始终可用——无外部依赖。"""
        return True

    async def spawn(self, config: TeammateSpawnConfig) -> SpawnResult:
        """将进程内 teammate 作为 asyncio Task 生成。

        创建 :class:`TeammateAbortController`，通过 :mod:`contextvars` 的
        创建时复制语义绑定到新 Task，并在 :attr:`_active` 中注册。
        """
        agent_id = f"{config.name}@{config.team}"
        task_id = f"in_process_{uuid.uuid4().hex[:12]}"

        if agent_id in self._active:
            entry = self._active[agent_id]
            if not entry.task.done():
                logger.warning(
                    "[InProcessBackend] spawn(): %s is already running", agent_id
                )
                return SpawnResult(
                    task_id=task_id,
                    agent_id=agent_id,
                    backend_type=self.type,
                    success=False,
                    error=f"Agent {agent_id!r} is already running",
                )

        abort_controller = TeammateAbortController()

        # asyncio.create_task() copies the current Context automatically,
        # so each Task starts with an independent ContextVar state.
        task = asyncio.create_task(
            start_in_process_teammate(
                config=config,
                agent_id=agent_id,
                abort_controller=abort_controller,
            ),
            name=f"teammate-{agent_id}",
        )

        entry = _TeammateEntry(
            task=task,
            abort_controller=abort_controller,
            task_id=task_id,
        )
        self._active[agent_id] = entry

        def _on_done(t: asyncio.Task[None]) -> None:
            self._active.pop(agent_id, None)
            if not t.cancelled() and t.exception() is not None:
                self._on_teammate_error(agent_id, t.exception())  # type: ignore[arg-type]

        task.add_done_callback(_on_done)

        logger.debug("[InProcessBackend] spawned %s (task_id=%s)", agent_id, task_id)
        return SpawnResult(
            task_id=task_id,
            agent_id=agent_id,
            backend_type=self.type,
        )

    async def send_message(self, agent_id: str, message: TeammateMessage) -> None:
        """将 *message* 写入 teammate 的基于文件的邮箱。

        智能体名称和团队从 *agent_id*（``name@team`` 格式）推断。
        这镜像了面板后端的工作方式，使 swarm 栈的其余部分保持后端无关。

        若 teammate 运行在进程内且其 :class:`TeammateContext` 可访问，
        消息也会直接推入 ``ctx.message_queue``，实现无文件系统往返的低延迟投递。
        """
        if "@" not in agent_id:
            raise ValueError(
                f"Invalid agent_id {agent_id!r}: expected 'agentName@teamName'"
            )
        agent_name, team_name = agent_id.split("@", 1)

        from openharness.swarm.mailbox import MailboxMessage

        msg = MailboxMessage(
            id=str(uuid.uuid4()),
            type="user_message",
            sender=message.from_agent,
            recipient=agent_id,
            payload={
                "content": message.text,
                **({"color": message.color} if message.color else {}),
            },
            timestamp=message.timestamp and float(message.timestamp) or time.time(),
        )
        mailbox = TeammateMailbox(team_name=team_name, agent_id=agent_name)
        await mailbox.write(msg)
        logger.debug("[InProcessBackend] sent message to %s", agent_id)

    async def shutdown(
        self, agent_id: str, *, force: bool = False, timeout: float = 10.0
    ) -> bool:
        """终止运行中的进程内 teammate。

        Args:
            agent_id: 待终止的智能体标识符。
            force: 若为 True，立即取消 asyncio Task，不等待优雅关闭。
            timeout: 设置取消事件后等待任务完成的时间（秒），
                超时后回退到 :meth:`asyncio.Task.cancel`。

        Returns:
            找到智能体并成功发起终止返回 True。
        """
        entry = self._active.get(agent_id)
        if entry is None:
            logger.debug(
                "[InProcessBackend] shutdown(): %s not found in active tasks", agent_id
            )
            return False

        if entry.task.done():
            self._active.pop(agent_id, None)
            return True

        if force:
            entry.abort_controller.request_cancel(reason="force shutdown", force=True)
            entry.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(entry.task), timeout=timeout)
        else:
            # Graceful: request cancel and wait for self-exit
            entry.abort_controller.request_cancel(reason="graceful shutdown")
            try:
                await asyncio.wait_for(asyncio.shield(entry.task), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "[InProcessBackend] %s did not exit within %.1fs — forcing cancel",
                    agent_id,
                    timeout,
                )
                entry.abort_controller.request_cancel(reason="timeout — forcing", force=True)
                entry.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry.task

        await self._cleanup_teammate(agent_id)
        logger.debug("[InProcessBackend] shut down %s", agent_id)
        return True

    # ------------------------------------------------------------------
    # Enhanced lifecycle management
    # ------------------------------------------------------------------

    async def _cleanup_teammate(self, agent_id: str) -> None:
        """在智能体任务完成后执行完整清理。

        - 从 :attr:`_active` 移除条目。
        - 取消终止控制器（若尚未取消）。
        - 记录清理日志。

        从任务的 done 回调和 :meth:`shutdown` 中自动调用。
        """
        entry = self._active.pop(agent_id, None)
        if entry is None:
            return

        # Ensure the abort controller is signalled so any waiters unblock
        if not entry.abort_controller.is_cancelled:
            entry.abort_controller.request_cancel(reason="cleanup")

        logger.debug(
            "[InProcessBackend] _cleanup_teammate: %s removed from registry", agent_id
        )

    def _on_teammate_error(self, agent_id: str, error: Exception) -> None:
        """处理 teammate Task 中未捕获的异常。

        记录结构化错误日志并从注册表中移除条目。
        未来可向领导者邮箱发送 TaskNotification。
        """
        duration = 0.0
        entry = self._active.get(agent_id)
        if entry is not None:
            duration = time.time() - entry.started_at
            self._active.pop(agent_id, None)

        logger.error(
            "[InProcessBackend] Teammate %s raised an unhandled exception "
            "(duration=%.1fs): %s: %s",
            agent_id,
            duration,
            type(error).__name__,
            error,
        )

    def get_teammate_status(self, agent_id: str) -> dict[str, Any] | None:
        """返回指定智能体的状态字典（含使用统计）。

        若智能体不在活跃注册表中返回 None。

        返回的字典包含::

            {
                "agent_id": str,
                "task_id": str,
                "is_done": bool,
                "duration_s": float,
            }
        """
        entry = self._active.get(agent_id)
        if entry is None:
            return None

        return {
            "agent_id": agent_id,
            "task_id": entry.task_id,
            "is_done": entry.task.done(),
            "duration_s": time.time() - entry.started_at,
        }

    def list_teammates(self) -> list[tuple[str, bool, float]]:
        """返回 ``(agent_id, is_running, duration_seconds)`` 元组列表。

        ``is_running`` 在任务存活且未完成时为 True。
        ``duration_seconds`` 为生成以来的挂钟时间。
        """
        now = time.time()
        result = []
        for agent_id, entry in self._active.items():
            is_running = not entry.task.done()
            duration = now - entry.started_at
            result.append((agent_id, is_running, duration))
        return result

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def is_active(self, agent_id: str) -> bool:
        """若 teammate 有正在运行（未完成）的 Task 则返回 True。"""
        entry = self._active.get(agent_id)
        if entry is None:
            return False
        return not entry.task.done()

    def active_agents(self) -> list[str]:
        """返回当前有运行中 Task 的智能体 ID 列表。"""
        return [aid for aid, entry in self._active.items() if not entry.task.done()]

    async def shutdown_all(self, *, force: bool = False, timeout: float = 10.0) -> None:
        """优雅（或强制）终止所有活跃的 teammate。"""
        agent_ids = list(self._active.keys())
        await asyncio.gather(
            *(self.shutdown(aid, force=force, timeout=timeout) for aid in agent_ids),
            return_exceptions=True,
        )
