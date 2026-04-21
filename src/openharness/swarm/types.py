"""Swarm 后端类型定义模块。

本模块定义了 Swarm 子系统的所有核心类型，包括后端类型字面量、面板后端协议、
智能体身份与生成配置、生成结果、消息结构以及执行器协议，为多后端协作提供
统一的类型契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Backend type literals
# ---------------------------------------------------------------------------

BackendType = Literal["subprocess", "in_process", "tmux", "iterm2"]
"""所有支持的后端类型，包括子进程、进程内、tmux 和 iTerm2。"""

PaneBackendType = Literal["tmux", "iterm2"]
"""面板式（可视化）后端类型的子集，仅包含 tmux 和 iTerm2。"""

PaneId = str
"""终端面板的不透明标识符。

对于 tmux，这是面板 ID（如 ``"%1"``）。
对于 iTerm2，这是 ``it2`` 返回的会话 ID。
"""


# ---------------------------------------------------------------------------
# Pane backend types
# ---------------------------------------------------------------------------


@dataclass
class CreatePaneResult:
    """创建新智能体面板的结果。

    Attributes:
        pane_id: 新创建面板的 ID。
        is_first_teammate: 是否为首个智能体面板（影响布局策略）。
    """

    pane_id: PaneId
    """新创建面板的 ID。"""

    is_first_teammate: bool
    """是否为首个智能体面板（影响布局策略）。"""


@runtime_checkable
class PaneBackend(Protocol):
    """面板管理后端协议（tmux / iTerm2）。

    抽象了在 Swarm 模式下创建和管理终端面板的操作，
    包括面板创建、命令发送、颜色设置、标题管理、布局重平衡和显隐控制。
    """

    @property
    def type(self) -> BackendType:
        """后端类型标识符。"""
        ...

    @property
    def display_name(self) -> str:
        """后端的人类可读显示名称。"""
        ...

    @property
    def supports_hide_show(self) -> bool:
        """该后端是否支持面板的隐藏和显示。"""
        ...

    async def is_available(self) -> bool:
        """检查该后端在当前系统上是否可用。

        对于 tmux：检查 tmux 二进制文件是否存在。
        对于 iTerm2：检查 it2 CLI 是否已安装和配置。
        """
        ...

    async def is_running_inside(self) -> bool:
        """检查当前是否运行在该后端的环境中。

        对于 tmux：检查是否在 tmux 会话中（``$TMUX`` 已设置）。
        对于 iTerm2：检查是否运行在 iTerm2 内。
        """
        ...

    async def create_teammate_pane_in_swarm_view(
        self,
        name: str,
        color: str | None = None,
    ) -> CreatePaneResult:
        """在 Swarm 视图中为智能体创建新面板。

        Args:
            name: 智能体的显示名称。
            color: 面板边框/标题的可选颜色名称。

        Returns:
            :class:`CreatePaneResult`，包含面板 ID 和首个智能体标志。
        """
        ...

    async def send_command_to_pane(
        self,
        pane_id: PaneId,
        command: str,
        *,
        use_external_session: bool = False,
    ) -> None:
        """向指定面板发送 Shell 命令执行。

        Args:
            pane_id: 目标面板 ID。
            command: 待执行的命令字符串。
            use_external_session: 若为 True，使用外部会话套接字（仅 tmux）。
        """
        ...

    async def set_pane_border_color(
        self,
        pane_id: PaneId,
        color: str,
        *,
        use_external_session: bool = False,
    ) -> None:
        """设置指定面板的边框颜色。"""
        ...

    async def set_pane_title(
        self,
        pane_id: PaneId,
        name: str,
        color: str | None = None,
        *,
        use_external_session: bool = False,
    ) -> None:
        """设置面板边框/标题中显示的名称。"""
        ...

    async def enable_pane_border_status(
        self,
        window_target: str | None = None,
        *,
        use_external_session: bool = False,
    ) -> None:
        """启用面板边框状态显示（在边框中显示标题）。"""
        ...

    async def rebalance_panes(
        self,
        window_target: str,
        has_leader: bool,
    ) -> None:
        """重新平衡面板布局以达到理想的排列。

        Args:
            window_target: 包含面板的窗口。
            has_leader: 是否有领导者面板（影响布局策略）。
        """
        ...

    async def kill_pane(
        self,
        pane_id: PaneId,
        *,
        use_external_session: bool = False,
    ) -> bool:
        """关闭/终止指定面板。

        Returns:
            面板成功关闭返回 True。
        """
        ...

    async def hide_pane(
        self,
        pane_id: PaneId,
        *,
        use_external_session: bool = False,
    ) -> bool:
        """将面板移至隐藏窗口以隐藏，面板仍在运行但不可见。

        Returns:
            面板成功隐藏返回 True。
        """
        ...

    async def show_pane(
        self,
        pane_id: PaneId,
        target_window_or_pane: str,
        *,
        use_external_session: bool = False,
    ) -> bool:
        """将之前隐藏的面板重新加入主窗口显示。

        Returns:
            面板成功显示返回 True。
        """
        ...

    def list_panes(self) -> list[PaneId]:
        """返回该后端管理的所有已知面板 ID 列表。"""
        ...


# ---------------------------------------------------------------------------
# Backend detection result
# ---------------------------------------------------------------------------


@dataclass
class BackendDetectionResult:
    """后端自动检测结果。

    Attributes:
        backend: 应使用的后端类型字符串。
        is_native: 是否运行在该后端的原生环境中。
        needs_setup: 当检测到 iTerm2 但 it2 未安装时为 True。
    """

    backend: str
    """后端类型字符串（如 ``"tmux"``、``"in_process"``）。"""

    is_native: bool
    """是否运行在该后端的原生环境中。"""

    needs_setup: bool = False
    """是否需要额外安装配置（如安装 ``it2``）。"""


# ---------------------------------------------------------------------------
# Teammate identity & spawn configuration
# ---------------------------------------------------------------------------


@dataclass
class TeammateIdentity:
    """智能体代理的身份标识字段。

    Attributes:
        agent_id: 唯一代理标识符（格式：agentName@teamName）。
        name: 代理名称（如 'researcher'、'tester'）。
        team: 该代理所属的团队名称。
        color: 分配的 UI 颜色。
        parent_session_id: 父会话 ID，用于上下文关联。
    """

    agent_id: str
    """唯一代理标识符（格式：agentName@teamName）。"""

    name: str
    """代理名称（如 'researcher'、'tester'）。"""

    team: str
    """该代理所属的团队名称。"""

    color: str | None = None
    """分配的 UI 颜色。"""

    parent_session_id: str | None = None
    """父会话 ID，用于上下文关联。"""


@dataclass
class TeammateSpawnConfig:
    """智能体生成配置（适用于所有执行模式）。

    Attributes:
        name: 人类可读的代理名称。
        team: 所属团队名称。
        prompt: 代理的初始提示/任务。
        cwd: 代理的工作目录。
        parent_session_id: 父会话 ID。
        model: 可选的模型覆盖。
        command: 可选的显式命令覆盖（用于子进程后端）。
        system_prompt: 从工作流配置解析的系统提示词。
        system_prompt_mode: 系统提示词应用方式：替换或追加到默认值。
        color: 可选的 UI 颜色。
        color_override: 显式颜色覆盖（优先于 color）。
        permissions: 授予该代理的工具权限列表。
        plan_mode_required: 该代理是否必须在实施前进入计划模式。
        allow_permission_prompts: 为 False 时未列出工具自动拒绝。
        worktree_path: 可选的 git worktree 路径。
        session_id: 显式会话 ID。
        subscriptions: 该代理订阅的事件主题列表。
        task_type: 后台任务类型。
    """

    name: str
    """人类可读的代理名称（如 ``"researcher"``）。"""

    team: str
    """所属团队名称。"""

    prompt: str
    """代理的初始提示/任务。"""

    cwd: str
    """代理的工作目录。"""

    parent_session_id: str
    """父会话 ID（用于对话记录关联）。"""

    model: str | None = None
    """可选的模型覆盖。"""

    command: str | None = None
    """可选的显式命令覆盖（用于子进程后端）。"""

    system_prompt: str | None = None
    """从工作流配置解析的系统提示词。"""

    system_prompt_mode: Literal["default", "replace", "append"] | None = None
    """系统提示词应用方式：替换或追加到默认值。"""

    color: str | None = None
    """可选的 UI 颜色。"""

    color_override: str | None = None
    """显式颜色覆盖（优先于 color）。"""

    permissions: list[str] = field(default_factory=list)
    """授予该代理的工具权限列表。"""

    plan_mode_required: bool = False
    """该代理是否必须在实施前进入计划模式。"""

    allow_permission_prompts: bool = False
    """为 False（默认）时，未列出的工具自动拒绝。"""

    worktree_path: str | None = None
    """可选的 git worktree 路径，用于隔离文件系统访问。"""

    session_id: str | None = None
    """显式会话 ID（未提供时自动生成）。"""

    subscriptions: list[str] = field(default_factory=list)
    """该代理订阅的事件主题列表。"""

    task_type: Literal["local_agent", "remote_agent", "in_process_teammate"] = "local_agent"
    """子进程后端记录的后台任务类型。"""


# ---------------------------------------------------------------------------
# Spawn result & messaging
# ---------------------------------------------------------------------------


@dataclass
class SpawnResult:
    """智能体生成结果。

    Attributes:
        task_id: 任务管理器中的任务 ID。
        agent_id: 唯一代理标识符（格式：agentName@teamName）。
        backend_type: 用于生成该代理的后端类型。
        success: 是否生成成功。
        error: 失败时的错误信息。
        pane_id: 面板式后端的面板 ID。
    """

    task_id: str
    """任务管理器中的任务 ID。"""

    agent_id: str
    """唯一代理标识符（格式：agentName@teamName）。"""

    backend_type: BackendType
    """用于生成该代理的后端类型。"""

    success: bool = True
    error: str | None = None

    pane_id: PaneId | None = None
    """面板式后端的面板 ID（tmux / iTerm2）。"""


@dataclass
class TeammateMessage:
    """发送给智能体的消息。

    Attributes:
        text: 消息文本内容。
        from_agent: 发送方代理名称。
        color: 可选的颜色标识。
        timestamp: 可选的时间戳。
        summary: 可选的摘要信息。
    """

    text: str
    from_agent: str
    color: str | None = None
    timestamp: str | None = None
    summary: str | None = None


# ---------------------------------------------------------------------------
# TeammateExecutor protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TeammateExecutor(Protocol):
    """智能体执行后端协议。

    抽象了跨子进程、进程内和 tmux 后端的生成、消息发送和关闭操作。
    """

    type: BackendType

    def is_available(self) -> bool:
        """检查该后端在当前系统上是否可用。"""
        ...

    async def spawn(self, config: TeammateSpawnConfig) -> SpawnResult:
        """根据给定配置生成新的智能体。"""
        ...

    async def send_message(self, agent_id: str, message: TeammateMessage) -> None:
        """通过 stdin 向运行中的智能体发送消息。"""
        ...

    async def shutdown(self, agent_id: str, *, force: bool = False) -> bool:
        """终止一个智能体。

        Args:
            agent_id: 待终止的代理。
            force: 若为 True，立即强制终止；若为 False，尝试优雅关闭。

        Returns:
            代理成功终止返回 True。
        """
        ...


# ---------------------------------------------------------------------------
# Type guard helpers
# ---------------------------------------------------------------------------


def is_pane_backend(backend_type: BackendType) -> bool:
    """判断给定后端类型是否为终端面板后端（tmux 或 iterm2）。"""
    return backend_type in ("tmux", "iterm2")
