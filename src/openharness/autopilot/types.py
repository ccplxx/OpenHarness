"""仓库自动驾驶模块的数据模型定义。

本模块定义了 autopilot 系统中所有核心数据结构，包括任务状态枚举、
任务来源枚举、任务卡片、日志条目、注册表、验证步骤和执行结果等 Pydantic 模型。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RepoTaskStatus = Literal[
    "queued",  # 排队
    "accepted",  # 接受
    "preparing",  # 准备
    "running",  # 执行
    "verifying",  # 验证
    "pr_open",
    "waiting_ci",
    "repairing",
    "completed",
    "merged",
    "failed",
    "rejected",
    "superseded",
]
"""仓库任务的生命周期状态类型。

定义了任务从入队到终态的完整状态流转路径：
queued → accepted → preparing → running → verifying → pr_open → waiting_ci → completed/merged/failed。
还包含 repairing（修复重试）、rejected（人工拒绝）、superseded（被新任务取代）等状态。
"""

RepoTaskSource = Literal[
    "ohmo_request",
    "manual_idea",
    "github_issue",
    "github_pr",
    "claude_code_candidate",
]
"""仓库任务的来源类型。

标识任务的触发渠道：
- ohmo_request: 由 ohmo 助手发起的请求
- manual_idea: 人工手动提交的想法
- github_issue: 来自 GitHub Issue 的任务
- github_pr: 来自 GitHub PR 的任务
- claude_code_candidate: 来自 claude-code 的候选评估任务
"""


class RepoTaskCard(BaseModel):
    """标准化的仓库级工作项卡片。

    每个卡片代表一个独立的autopilot任务，包含唯一标识、指纹、标题、正文、
    来源信息、当前状态、评分及评分理由、标签和元数据等字段。
    指纹用于去重，评分用于优先级排序。
    """

    id: str
    fingerprint: str
    title: str
    body: str = ""
    source_kind: RepoTaskSource  # 来源
    source_ref: str = ""
    status: RepoTaskStatus = "queued"  # 状态
    score: int = 0
    score_reasons: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    updated_at: float


class RepoJournalEntry(BaseModel):
    """仅追加的仓库日志事件。

    记录 autopilot 系统中的每一次重要操作（如任务入队、状态变更、扫描结果等），
    以时间戳、事件类型、摘要和可选的任务 ID、元数据描述。日志以 JSONL 格式持久化，
    用于审计追踪和上下文重建。
    """

    timestamp: float
    kind: str
    summary: str
    task_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RepoAutopilotRegistry(BaseModel):
    """仓库自动驾驶的完整注册表。

    包含版本号、最后更新时间和所有任务卡片列表，
    是 autopilot 系统的核心持久化数据结构，以 JSON 格式存储在项目目录中。
    """

    version: int = 1
    updated_at: float = 0.0
    cards: list[RepoTaskCard] = Field(default_factory=list)


class RepoVerificationStep(BaseModel):
    """单条验证命令的执行结果。

    记录一次验证命令（如 pytest、ruff check、tsc）的执行情况，
    包括命令内容、返回码、状态（success/failed/skipped/error）以及 stdout/stderr 输出。
    """

    command: str
    returncode: int
    status: Literal["success", "failed", "skipped", "error"]
    stdout: str = ""
    stderr: str = ""


class RepoRunResult(BaseModel):
    """单次 autopilot 执行尝试的结果。

    记录一次任务执行的完整输出，包括关联卡片 ID、最终状态、
    助手摘要、运行报告路径、验证报告路径、验证步骤列表、尝试次数、
    worktree 路径以及关联的 PR 信息。
    """

    card_id: str
    status: RepoTaskStatus
    assistant_summary: str = ""
    run_report_path: str = ""
    verification_report_path: str = ""
    verification_steps: list[RepoVerificationStep] = Field(default_factory=list)
    attempt_count: int = 0
    worktree_path: str = ""
    pr_number: int | None = None
    pr_url: str = ""
