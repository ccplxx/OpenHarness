"""高层对话引擎。

本模块提供 QueryEngine 类，作为 OpenHarness 对话系统的核心入口，负责：

- 管理对话历史（消息列表）和工具感知的模型交互循环
- 维护 API 客户端、工具注册表、权限检查器等运行时依赖
- 在每次用户提交时构建 QueryContext 并委托 run_query 执行多轮工具调用循环
- 追踪 token 用量（通过 CostTracker）并支持自动上下文压缩
- 支持 coordinator（协调者）运行时上下文注入，实现多代理协作

QueryEngine 是有状态的：它持有完整的消息历史，支持断点续传（continue_pending）
和会话恢复（load_messages），是 REPL / Web 等前端与底层查询循环之间的桥梁。
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

from openharness.api.client import SupportsStreamingMessages
from openharness.engine.cost_tracker import CostTracker
from openharness.coordinator.coordinator_mode import get_coordinator_user_context
from openharness.engine.messages import ConversationMessage, TextBlock, ToolResultBlock
from openharness.engine.query import AskUserPrompt, PermissionPrompt, QueryContext, remember_user_goal, run_query
from openharness.engine.stream_events import AssistantTurnComplete, StreamEvent
from openharness.hooks import HookEvent, HookExecutor
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import ToolRegistry


class QueryEngine:
    """高层对话引擎，持有对话历史并驱动工具感知的模型循环。

    核心职责：
    - 维护完整的对话消息历史（_messages）
    - 通过 CostTracker 累计所有轮次的 token 用量
    - 将用户输入委托给 run_query 执行多轮 agentic 循环
    - 支持动态切换模型、系统提示词、API 客户端、权限检查器等配置
    - 检测中断的工具调用循环并提供 continue_pending 方法继续执行
    """

    def __init__(
        self,
        *,
        api_client: SupportsStreamingMessages,
        tool_registry: ToolRegistry,
        permission_checker: PermissionChecker,
        cwd: str | Path,
        model: str,
        system_prompt: str,
        max_tokens: int = 4096,
        context_window_tokens: int | None = None,
        auto_compact_threshold_tokens: int | None = None,
        max_turns: int | None = 8,
        permission_prompt: PermissionPrompt | None = None,
        ask_user_prompt: AskUserPrompt | None = None,
        hook_executor: HookExecutor | None = None,
        tool_metadata: dict[str, object] | None = None,
    ) -> None:
        self._api_client = api_client
        self._tool_registry = tool_registry
        self._permission_checker = permission_checker
        self._cwd = Path(cwd).resolve()
        self._model = model
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens
        self._context_window_tokens = context_window_tokens
        self._auto_compact_threshold_tokens = auto_compact_threshold_tokens
        self._max_turns = max_turns
        self._permission_prompt = permission_prompt
        self._ask_user_prompt = ask_user_prompt
        self._hook_executor = hook_executor
        self._tool_metadata = tool_metadata or {}
        self._messages: list[ConversationMessage] = []
        self._cost_tracker = CostTracker()

    @property
    def messages(self) -> list[ConversationMessage]:
        """返回当前对话历史的副本。"""
        return list(self._messages)

    @property
    def max_turns(self) -> int | None:
        """返回每次用户输入允许的最大 agentic 轮次数，无限制时为 None。"""
        return self._max_turns

    @property
    def api_client(self) -> SupportsStreamingMessages:
        """返回当前活跃的 API 客户端。"""
        return self._api_client

    @property
    def model(self) -> str:
        """返回当前活跃的模型标识符。"""
        return self._model

    @property
    def system_prompt(self) -> str:
        """返回当前活跃的系统提示词。"""
        return self._system_prompt

    @property
    def tool_metadata(self) -> dict[str, object]:
        """返回可变的工具元数据/跨轮次携带状态字典。"""
        return self._tool_metadata

    @property
    def total_usage(self):
        """返回所有轮次累计的 token 用量。"""
        return self._cost_tracker.total

    def clear(self) -> None:
        """清空内存中的对话历史并重置用量追踪器。"""
        self._messages.clear()
        self._cost_tracker = CostTracker()

    def set_system_prompt(self, prompt: str) -> None:
        """更新后续轮次使用的系统提示词。"""
        self._system_prompt = prompt

    def set_model(self, model: str) -> None:
        """更新后续轮次使用的模型标识符。"""
        self._model = model

    def set_api_client(self, api_client: SupportsStreamingMessages) -> None:
        """更新后续轮次使用的 API 客户端。"""
        self._api_client = api_client

    def set_max_turns(self, max_turns: int | None) -> None:
        """更新每次用户输入允许的最大 agentic 轮次数，至少为 1。"""
        self._max_turns = None if max_turns is None else max(1, int(max_turns))

    def set_permission_checker(self, checker: PermissionChecker) -> None:
        """更新后续轮次使用的权限检查器。"""
        self._permission_checker = checker

    def _build_coordinator_context_message(self) -> ConversationMessage | None:
        """构建携带 coordinator 运行时上下文的合成用户消息。

        当当前会话处于 coordinator 模式且存在 workerToolsContext 时，
        生成一条包含协调者上下文的 user 消息，用于在查询循环中注入
        多代理协作所需的上下文信息。
        """
        context = get_coordinator_user_context()
        worker_tools_context = context.get("workerToolsContext")
        if not worker_tools_context:
            return None
        return ConversationMessage(
            role="user",
            content=[TextBlock(text=f"# Coordinator User Context\n\n{worker_tools_context}")],
        )

    def load_messages(self, messages: list[ConversationMessage]) -> None:
        """替换内存中的对话历史为给定的消息列表。"""
        self._messages = list(messages)

    def has_pending_continuation(self) -> bool:
        """判断对话是否以未完成的工具结果结尾，需要后续模型轮次。

        当最后一条消息为 user 角色且包含 ToolResultBlock，
        且其前面的 assistant 消息包含 ToolUseBlock 时返回 True，
        表示存在中断的工具调用循环需要继续执行。
        """
        if not self._messages:
            return False
        last = self._messages[-1]
        if last.role != "user":
            return False
        if not any(isinstance(block, ToolResultBlock) for block in last.content):
            return False
        for msg in reversed(self._messages[:-1]):
            if msg.role != "assistant":
                continue
            return bool(msg.tool_uses)
        return False

    async def submit_message(self, prompt: str | ConversationMessage) -> AsyncIterator[StreamEvent]:
        """追加用户消息并执行查询循环。

        将用户输入（纯文本或 ConversationMessage）添加到对话历史后，
        构建 QueryContext 并委托 run_query 执行多轮 agentic 循环。
        在执行前触发 USER_PROMPT_SUBMIT 钩子事件，
        并自动记住用户目标到 tool_metadata 中。
        """
        user_message = (
            prompt
            if isinstance(prompt, ConversationMessage)
            else ConversationMessage.from_user_text(prompt)
        )
        if user_message.text.strip():
            remember_user_goal(self._tool_metadata, user_message.text)
        self._messages.append(user_message)
        if self._hook_executor is not None:
            await self._hook_executor.execute(
                HookEvent.USER_PROMPT_SUBMIT,
                {
                    "event": HookEvent.USER_PROMPT_SUBMIT.value,
                    "prompt": user_message.text,
                },
            )
        context = QueryContext(
            api_client=self._api_client,
            tool_registry=self._tool_registry,
            permission_checker=self._permission_checker,
            cwd=self._cwd,
            model=self._model,
            system_prompt=self._system_prompt,
            max_tokens=self._max_tokens,
            context_window_tokens=self._context_window_tokens,
            auto_compact_threshold_tokens=self._auto_compact_threshold_tokens,
            max_turns=self._max_turns,
            permission_prompt=self._permission_prompt,
            ask_user_prompt=self._ask_user_prompt,
            hook_executor=self._hook_executor,
            tool_metadata=self._tool_metadata,
        )
        query_messages = list(self._messages)
        coordinator_context = self._build_coordinator_context_message()
        if coordinator_context is not None:
            query_messages.append(coordinator_context)
        async for event, usage in run_query(context, query_messages):
            if isinstance(event, AssistantTurnComplete):
                self._messages = list(query_messages)
            if usage is not None:
                self._cost_tracker.add(usage)
            yield event

    async def continue_pending(self, *, max_turns: int | None = None) -> AsyncIterator[StreamEvent]:
        """继续被中断的工具调用循环，不追加新的用户消息。

        用于会话恢复场景：当对话历史以未回复的工具结果结尾时，
        无需用户重新输入即可让模型继续处理待完成的工具调用。
        可通过 max_turns 参数覆盖引擎默认的最大轮次限制。
        """
        context = QueryContext(
            api_client=self._api_client,
            tool_registry=self._tool_registry,
            permission_checker=self._permission_checker,
            cwd=self._cwd,
            model=self._model,
            system_prompt=self._system_prompt,
            max_tokens=self._max_tokens,
            context_window_tokens=self._context_window_tokens,
            auto_compact_threshold_tokens=self._auto_compact_threshold_tokens,
            max_turns=max_turns if max_turns is not None else self._max_turns,
            permission_prompt=self._permission_prompt,
            ask_user_prompt=self._ask_user_prompt,
            hook_executor=self._hook_executor,
            tool_metadata=self._tool_metadata,
        )
        async for event, usage in run_query(context, self._messages):
            if usage is not None:
                self._cost_tracker.add(usage)
            yield event
