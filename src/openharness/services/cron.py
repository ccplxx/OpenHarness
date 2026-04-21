"""本地 Cron 定时任务注册表辅助模块。

本模块提供基于 JSON 文件的 Cron 定时任务持久化机制，包括任务的增删改查、
Cron 表达式校验、下次执行时间计算等功能。所有写操作通过文件锁保证并发安全，
确保多个进程不会同时修改注册表。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from croniter import croniter

from openharness.config.paths import get_cron_registry_path
from openharness.utils.file_lock import exclusive_file_lock
from openharness.utils.fs import atomic_write_text


def _cron_lock_path() -> Path:
    """返回 Cron 注册表文件锁的路径，在注册表路径后追加 .lock 后缀。"""
    path = get_cron_registry_path()
    return path.with_suffix(path.suffix + ".lock")


def load_cron_jobs() -> list[dict[str, Any]]:
    """从磁盘加载已存储的 Cron 任务列表。

    Returns:
        list[dict[str, Any]]: 任务字典列表；若文件不存在或 JSON 解析失败则返回空列表。
    """
    path = get_cron_registry_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def save_cron_jobs(jobs: list[dict[str, Any]]) -> None:
    """将 Cron 任务列表持久化到磁盘，使用原子写入确保数据完整性。

    Args:
        jobs: 待持久化的任务字典列表。
    """
    atomic_write_text(
        get_cron_registry_path(),
        json.dumps(jobs, indent=2) + "\n",
    )


def validate_cron_expression(expression: str) -> bool:
    """校验给定的表达式是否为合法的 Cron 调度表达式。

    Args:
        expression: 待校验的 Cron 表达式字符串。

    Returns:
        bool: 表达式合法返回 True，否则返回 False。
    """
    return croniter.is_valid(expression)


def next_run_time(expression: str, base: datetime | None = None) -> datetime:
    """计算给定 Cron 表达式的下一次执行时间。

    Args:
        expression: Cron 调度表达式。
        base: 计算基准时间，默认为当前 UTC 时间。

    Returns:
        datetime: 下一次执行时间。
    """
    base = base or datetime.now(timezone.utc)
    return croniter(expression, base).get_next(datetime)


def upsert_cron_job(job: dict[str, Any]) -> None:
    """插入或替换一个 Cron 任务。

    若同名任务已存在则替换，否则新增。自动将 ``enabled`` 默认设为 True，
    并在调度表达式合法时计算 ``next_run`` 下次执行时间。写操作受文件锁保护。

    Args:
        job: 任务字典，至少包含 ``name`` 和 ``schedule`` 字段。
    """
    job.setdefault("enabled", True)
    job.setdefault("created_at", datetime.now(timezone.utc).isoformat())

    schedule = job.get("schedule", "")
    if validate_cron_expression(schedule):
        job["next_run"] = next_run_time(schedule).isoformat()

    with exclusive_file_lock(_cron_lock_path()):
        jobs = [existing for existing in load_cron_jobs() if existing.get("name") != job.get("name")]
        jobs.append(job)
        jobs.sort(key=lambda item: str(item.get("name", "")))
        save_cron_jobs(jobs)


def delete_cron_job(name: str) -> bool:
    """按名称删除一个 Cron 任务。

    Args:
        name: 待删除任务的名称。

    Returns:
        bool: 成功删除返回 True，任务不存在返回 False。
    """
    with exclusive_file_lock(_cron_lock_path()):
        jobs = load_cron_jobs()
        filtered = [job for job in jobs if job.get("name") != name]
        if len(filtered) == len(jobs):
            return False
        save_cron_jobs(filtered)
    return True


def get_cron_job(name: str) -> dict[str, Any] | None:
    """按名称查找并返回一个 Cron 任务。

    Args:
        name: 任务名称。

    Returns:
        dict[str, Any] | None: 匹配的任务字典，未找到返回 None。
    """
    for job in load_cron_jobs():
        if job.get("name") == name:
            return job
    return None


def set_job_enabled(name: str, enabled: bool) -> bool:
    """启用或禁用一个 Cron 任务。

    Args:
        name: 任务名称。
        enabled: True 为启用，False 为禁用。

    Returns:
        bool: 操作成功返回 True，任务不存在返回 False。
    """
    with exclusive_file_lock(_cron_lock_path()):
        jobs = load_cron_jobs()
        for job in jobs:
            if job.get("name") == name:
                job["enabled"] = enabled
                save_cron_jobs(jobs)
                return True
    return False


def mark_job_run(name: str, *, success: bool) -> None:
    """任务执行后更新 last_run 时间戳和执行状态，并重新计算 next_run。

    Args:
        name: 任务名称。
        success: 任务是否执行成功。
    """
    with exclusive_file_lock(_cron_lock_path()):
        jobs = load_cron_jobs()
        now = datetime.now(timezone.utc)
        for job in jobs:
            if job.get("name") == name:
                job["last_run"] = now.isoformat()
                job["last_status"] = "success" if success else "failed"
                schedule = job.get("schedule", "")
                if validate_cron_expression(schedule):
                    job["next_run"] = next_run_time(schedule, now).isoformat()
                save_cron_jobs(jobs)
                return
