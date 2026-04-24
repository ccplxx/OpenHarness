"""路径解析模块（openharness.config.paths）

本模块负责解析 OpenHarness 运行时所需的所有目录和文件路径，遵循类 XDG 约定，
以 ``~/.openharness/`` 作为默认基础目录。所有路径解析函数均支持通过环境变量
覆盖默认路径，且在目录不存在时自动创建。

支持的环境变量覆盖：
    - OPENHARNESS_CONFIG_DIR：覆盖配置目录路径。
    - OPENHARNESS_DATA_DIR：覆盖数据目录路径。
    - OPENHARNESS_LOGS_DIR：覆盖日志目录路径。

路径层次结构：
    ~/.openharness/
    ├── settings.json          # 主配置文件
    ├── data/                  # 数据目录（缓存、历史等）
    │   ├── sessions/          # 会话存储
    │   ├── tasks/             # 后台任务输出
    │   ├── feedback/          # 反馈存储
    │   └── cron_jobs.json     # 定时任务注册表
    └── logs/                  # 日志目录

项目级配置（每个项目独立）：
    <project>/.openharness/
    ├── issue.md               # 问题上下文
    ├── pr_comments.md         # PR 评论上下文
    └── autopilot/             # Autopilot 状态目录
        ├── registry.json      # 任务注册表
        ├── repo_journal.jsonl # 仓库日志
        ├── active_repo_context.md  # 活跃仓库上下文
        ├── autopilot_policy.yaml    # Autopilot 策略
        ├── verification_policy.yaml # 验证策略
        ├── release_policy.yaml      # 发布策略
        └── runs/              # 运行产物目录
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_BASE_DIR = ".openharness"
_CONFIG_FILE_NAME = "settings.json"


def get_config_dir() -> Path:
    """返回配置目录路径，若不存在则自动创建。

    解析顺序：
        1. 环境变量 OPENHARNESS_CONFIG_DIR
        2. 默认路径 ~/.openharness/

    返回：
        配置目录的 Path 对象，保证目录已存在。
    """
    env_dir = os.environ.get("OPENHARNESS_CONFIG_DIR")
    if env_dir:
        config_dir = Path(env_dir)
    else:
        config_dir = Path.home() / _DEFAULT_BASE_DIR

    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_config_file_path() -> Path:
    """返回主配置文件路径（~/.openharness/settings.json）。

    返回：
        配置文件的 Path 对象。
    """
    return get_config_dir() / _CONFIG_FILE_NAME


def get_data_dir() -> Path:
    """返回数据目录路径（用于缓存、历史等），若不存在则自动创建。

    解析顺序：
        1. 环境变量 OPENHARNESS_DATA_DIR
        2. 默认路径 ~/.openharness/data/

    返回：
        数据目录的 Path 对象，保证目录已存在。
    """
    env_dir = os.environ.get("OPENHARNESS_DATA_DIR")
    if env_dir:
        data_dir = Path(env_dir)
    else:
        data_dir = get_config_dir() / "data"

    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_logs_dir() -> Path:
    """返回日志目录路径，若不存在则自动创建。

    解析顺序：
        1. 环境变量 OPENHARNESS_LOGS_DIR
        2. 默认路径 ~/.openharness/logs/

    返回：
        日志目录的 Path 对象，保证目录已存在。
    """
    env_dir = os.environ.get("OPENHARNESS_LOGS_DIR")
    if env_dir:
        logs_dir = Path(env_dir)
    else:
        logs_dir = get_config_dir() / "logs"

    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def get_sessions_dir() -> Path:
    """返回会话存储目录路径，若不存在则自动创建。

    会话目录位于数据目录下的 sessions/ 子目录，用于持久化
    用户与 AI 的交互会话数据。

    返回：
        会话存储目录的 Path 对象，保证目录已存在。
    """
    sessions_dir = get_data_dir() / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir


def get_tasks_dir() -> Path:
    """返回后台任务输出目录路径，若不存在则自动创建。

    任务目录位于数据目录下的 tasks/ 子目录，用于存储
    后台异步执行的命令输出结果。

    返回：
        后台任务输出目录的 Path 对象，保证目录已存在。
    """
    tasks_dir = get_data_dir() / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    return tasks_dir


def get_feedback_dir() -> Path:
    """返回反馈存储目录路径，若不存在则自动创建。

    反馈目录位于数据目录下的 feedback/ 子目录，用于存储
    用户提交的反馈数据。

    返回：
        反馈存储目录的 Path 对象，保证目录已存在。
    """
    feedback_dir = get_data_dir() / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    return feedback_dir


def get_feedback_log_path() -> Path:
    """返回反馈日志文件路径。

    返回：
        反馈日志文件 feedback.log 的 Path 对象。
    """
    return get_feedback_dir() / "feedback.log"


def get_cron_registry_path() -> Path:
    """返回定时任务注册表文件路径。

    返回：
        定时任务注册表 cron_jobs.json 的 Path 对象。
    """
    return get_data_dir() / "cron_jobs.json"


def get_project_config_dir(cwd: str | Path) -> Path:
    """返回项目级 .openharness 配置目录路径，若不存在则自动创建。

    每个项目可以在其根目录下维护独立的 .openharness/ 配置目录，
    用于存储项目特有的上下文、Autopilot 状态等数据。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        项目级配置目录的 Path 对象，保证目录已存在。
    """
    project_dir = Path(cwd).resolve() / ".openharness"
    project_dir.mkdir(parents=True, exist_ok=True)
    return project_dir


def get_project_issue_file(cwd: str | Path) -> Path:
    """返回项目级问题上下文文件路径。

    该文件用于存储当前项目关联的 Issue 上下文信息，
    供 AI 在交互时参考。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        issue.md 文件的 Path 对象。
    """
    return get_project_config_dir(cwd) / "issue.md"


def get_project_pr_comments_file(cwd: str | Path) -> Path:
    """返回项目级 PR 评论上下文文件路径。

    该文件用于存储当前项目关联的 Pull Request 评论信息，
    供 AI 在代码审查时参考。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        pr_comments.md 文件的 Path 对象。
    """
    return get_project_config_dir(cwd) / "pr_comments.md"


def get_project_autopilot_dir(cwd: str | Path) -> Path:
    """返回项目级 Autopilot 状态目录路径，若不存在则自动创建。

    Autopilot 目录存储自动化驾驶模式下的任务注册表、策略文件、
    运行产物等数据。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        autopilot/ 目录的 Path 对象，保证目录已存在。
    """
    autopilot_dir = get_project_config_dir(cwd) / "autopilot"
    autopilot_dir.mkdir(parents=True, exist_ok=True)
    return autopilot_dir


def get_project_autopilot_registry_path(cwd: str | Path) -> Path:
    """返回 Autopilot 任务注册表文件路径。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        registry.json 文件的 Path 对象。
    """
    return get_project_autopilot_dir(cwd) / "registry.json"


def get_project_repo_journal_path(cwd: str | Path) -> Path:
    """返回只追加仓库日志文件路径。

    该文件以 JSONL 格式记录仓库变更的日志条目，
    供 Autopilot 在自动化流程中追踪仓库演进。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        repo_journal.jsonl 文件的 Path 对象。
    """
    return get_project_autopilot_dir(cwd) / "repo_journal.jsonl"


def get_project_active_repo_context_path(cwd: str | Path) -> Path:
    """返回合成的活跃仓库上下文文件路径。

    该文件包含由系统合成的当前仓库上下文摘要，
    供 AI 在交互时获取项目整体结构的概要信息。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        active_repo_context.md 文件的 Path 对象。
    """
    return get_project_autopilot_dir(cwd) / "active_repo_context.md"


def get_project_autopilot_policy_path(cwd: str | Path) -> Path:
    """返回 Autopilot 策略文件路径。

    该 YAML 文件定义了 Autopilot 自动化驾驶模式的行为策略，
    如代码修改范围、提交策略等。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        autopilot_policy.yaml 文件的 Path 对象。
    """
    return get_project_autopilot_dir(cwd) / "autopilot_policy.yaml"


def get_project_verification_policy_path(cwd: str | Path) -> Path:
    """返回验证策略文件路径。

    该 YAML 文件定义了代码变更后的验证策略，
    如测试执行、构建检查等。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        verification_policy.yaml 文件的 Path 对象。
    """
    return get_project_autopilot_dir(cwd) / "verification_policy.yaml"


def get_project_release_policy_path(cwd: str | Path) -> Path:
    """返回发布策略文件路径。

    该 YAML 文件定义了自动化发布的策略，
    如版本号计算、发布分支管理等。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        release_policy.yaml 文件的 Path 对象。
    """
    return get_project_autopilot_dir(cwd) / "release_policy.yaml"


def get_project_autopilot_runs_dir(cwd: str | Path) -> Path:
    """返回 Autopilot 运行产物目录路径，若不存在则自动创建。

    该目录存储每次 Autopilot 运行的输出产物，
    如代码变更、执行日志等。

    参数：
        cwd: 项目的工作目录路径。

    返回：
        runs/ 目录的 Path 对象，保证目录已存在。
    """
    runs_dir = get_project_autopilot_dir(cwd) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    return runs_dir
