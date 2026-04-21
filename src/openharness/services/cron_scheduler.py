"""后台 Cron 调度器守护进程模块。

本模块实现了一个独立的 Cron 调度器进程，可通过 ``oh cron start`` 启动，
也可通过 :func:`run_scheduler_loop` 嵌入式运行。调度器每隔固定时间间隔
（TICK_INTERVAL_SECONDS）读取 Cron 注册表，检查哪些已启用的任务到期，
执行这些任务，并将执行结果记录到历史日志文件中。同时提供 PID 文件管理、
进程启停控制等守护进程基础设施。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openharness.config.paths import get_data_dir, get_logs_dir
from openharness.services.cron import (
    load_cron_jobs,
    mark_job_run,
    validate_cron_expression,
)
from openharness.sandbox import SandboxUnavailableError
from openharness.utils.shell import create_shell_subprocess

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 30
"""调度器检查到期任务的时间间隔（秒）。"""


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def get_history_path() -> Path:
    """返回 Cron 执行历史文件的路径。"""
    return get_data_dir() / "cron_history.jsonl"


def append_history(entry: dict[str, Any]) -> None:
    """将一条执行记录追加到历史日志文件（JSONL 格式）。

    Args:
        entry: 包含任务名称、执行状态、时间戳等信息的字典。
    """
    path = get_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def load_history(*, limit: int = 50, job_name: str | None = None) -> list[dict[str, Any]]:
    """加载最近的历史执行记录。

    Args:
        limit: 最多返回的记录条数，默认 50。
        job_name: 若指定，则只返回该任务名的记录。

    Returns:
        list[dict[str, Any]]: 按时间排序的执行记录列表（最新在末尾）。
    """
    path = get_history_path()
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if job_name and entry.get("name") != job_name:
            continue
        entries.append(entry)
    return entries[-limit:]


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------

def get_pid_path() -> Path:
    """返回调度器 PID 文件的路径。"""
    return get_data_dir() / "cron_scheduler.pid"


def read_pid() -> int | None:
    """读取当前运行中调度器的进程 PID。

    若 PID 文件不存在或对应进程已终止，则清理过期文件并返回 None。

    Returns:
        int | None: 调度器进程 PID，不存在则返回 None。
    """
    path = get_pid_path()
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    # Check if process is alive
    try:
        os.kill(pid, 0)
    except OSError:
        logger.debug("Removed stale scheduler PID file (pid=%d)", pid)
        path.unlink(missing_ok=True)
        return None
    return pid


def write_pid() -> None:
    """将当前进程 PID 写入 PID 文件。"""
    path = get_pid_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def remove_pid() -> None:
    """删除 PID 文件。"""
    get_pid_path().unlink(missing_ok=True)


def is_scheduler_running() -> bool:
    """检查调度器进程是否仍在运行。

    Returns:
        bool: 调度器进程存活返回 True。
    """
    return read_pid() is not None


def stop_scheduler() -> bool:
    """向运行中的调度器发送 SIGTERM 信号以停止其运行。

    若进程在短时间内未退出，则发送 SIGKILL 强制终止。

    Returns:
        bool: 成功终止返回 True，调度器未运行或终止失败返回 False。
    """
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        remove_pid()
        return False
    # Wait briefly for process to exit
    for _ in range(10):
        try:
            os.kill(pid, 0)
        except OSError:
            remove_pid()
            return True
        time.sleep(0.2)
    # Force kill
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    remove_pid()
    return True


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------

async def execute_job(job: dict[str, Any]) -> dict[str, Any]:
    """运行单个 Cron 任务并返回执行历史记录条目。

    通过创建 Shell 子进程执行任务命令，设置 300 秒超时限制。
    执行完成后更新注册表中的 last_run/next_run 并追加历史记录。
    标准输出和标准错误仅保留最后 2000 个字符。

    Args:
        job: 任务字典，须包含 ``name``、``command`` 字段。

    Returns:
        dict[str, Any]: 包含任务名、执行状态、返回码、输出等信息的历史条目。
    """
    name = job["name"]
    command = job["command"]
    cwd = Path(job.get("cwd") or ".").expanduser()
    started_at = datetime.now(timezone.utc)

    logger.info("Executing cron job %r: %s", name, command)
    try:
        process = await create_shell_subprocess(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=300,
        )
    except asyncio.TimeoutError:
        try:
            process.kill()
            await process.wait()
        except Exception:
            pass
        entry = {
            "name": name,
            "command": command,
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "returncode": -1,
            "status": "timeout",
            "stdout": "",
            "stderr": "Job timed out after 300s",
        }
        mark_job_run(name, success=False)
        append_history(entry)
        return entry
    except SandboxUnavailableError as exc:
        entry = {
            "name": name,
            "command": command,
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "returncode": -1,
            "status": "error",
            "stdout": "",
            "stderr": str(exc),
        }
        mark_job_run(name, success=False)
        append_history(entry)
        return entry
    except Exception as exc:
        entry = {
            "name": name,
            "command": command,
            "started_at": started_at.isoformat(),
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "returncode": -1,
            "status": "error",
            "stdout": "",
            "stderr": str(exc),
        }
        mark_job_run(name, success=False)
        append_history(entry)
        return entry

    success = process.returncode == 0
    entry = {
        "name": name,
        "command": command,
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "returncode": process.returncode,
        "status": "success" if success else "failed",
        "stdout": (stdout.decode("utf-8", errors="replace")[-2000:] if stdout else ""),
        "stderr": (stderr.decode("utf-8", errors="replace")[-2000:] if stderr else ""),
    }
    mark_job_run(name, success=success)
    append_history(entry)
    logger.info("Job %r finished: %s (rc=%s)", name, entry["status"], process.returncode)
    return entry


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------

def _jobs_due(jobs: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """从任务列表中筛选出 next_run 不晚于当前时间的已启用任务。

    Args:
        jobs: 全部任务列表。
        now: 当前时间基准。

    Returns:
        list[dict[str, Any]]: 到期任务的子列表。
    """
    due: list[dict[str, Any]] = []
    for job in jobs:
        if not job.get("enabled", True):
            continue
        schedule = job.get("schedule", "")
        if not validate_cron_expression(schedule):
            continue
        next_run_str = job.get("next_run")
        if not next_run_str:
            continue
        try:
            next_run = datetime.fromisoformat(next_run_str)
            if next_run.tzinfo is None:
                next_run = next_run.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if next_run <= now:
            due.append(job)
    return due


async def run_scheduler_loop(*, once: bool = False) -> None:
    """调度器主循环，持续运行直到收到 SIGTERM 信号或 *once* 为 True。

    每次循环迭代：加载注册表 → 筛选到期任务 → 并发执行 → 等待下一轮。
    当 ``once=True`` 时仅执行一轮后退出（用于测试模式）。

    Args:
        once: 若为 True，仅执行一次检查后退出。
    """
    shutdown = asyncio.Event()

    def _on_signal() -> None:
        logger.info("Received shutdown signal")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    write_pid()
    logger.info("Cron scheduler started (pid=%d, tick=%ds)", os.getpid(), TICK_INTERVAL_SECONDS)

    try:
        while not shutdown.is_set():
            now = datetime.now(timezone.utc)
            jobs = load_cron_jobs()
            due = _jobs_due(jobs, now)

            if due:
                logger.info("Tick: %d job(s) due", len(due))
                # Execute due jobs concurrently
                results = await asyncio.gather(
                    *(execute_job(job) for job in due), return_exceptions=True
                )
                for result in results:
                    if isinstance(result, BaseException):
                        logger.error("Unexpected error executing cron job: %s", result)

            if once:
                break

            try:
                await asyncio.wait_for(shutdown.wait(), timeout=TICK_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass
    finally:
        remove_pid()
        logger.info("Cron scheduler stopped")


# ---------------------------------------------------------------------------
# Daemon entry point (spawned by ``oh cron start``)
# ---------------------------------------------------------------------------

def _run_daemon() -> None:
    """调度器子进程入口点，配置日志后启动主循环。"""
    log_file = get_logs_dir() / "cron_scheduler.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(log_file),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    asyncio.run(run_scheduler_loop())


def start_daemon() -> int:
    """通过 fork 创建调度器守护进程并启动。

    子进程脱离终端（setsid），重定向标准 I/O 至 /dev/null，
    以独立守护进程方式运行调度器主循环。

    Returns:
        int: 子进程的 PID。

    Raises:
        RuntimeError: 调度器已在运行时抛出。
    """
    existing = read_pid()
    if existing is not None:
        raise RuntimeError(f"Scheduler already running (pid={existing})")

    pid = os.fork()
    if pid > 0:
        # Parent — wait a moment for the child to write its PID file
        time.sleep(0.3)
        return pid

    # Child — detach
    os.setsid()
    # Redirect stdio
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)

    _run_daemon()
    sys.exit(0)


def scheduler_status() -> dict[str, Any]:
    """返回调度器的状态信息字典，包含运行状态、PID、任务计数和文件路径。"""
    pid = read_pid()
    log_path = get_logs_dir() / "cron_scheduler.log"
    jobs = load_cron_jobs()
    enabled = [j for j in jobs if j.get("enabled", True)]
    return {
        "running": pid is not None,
        "pid": pid,
        "total_jobs": len(jobs),
        "enabled_jobs": len(enabled),
        "log_file": str(log_path),
        "history_file": str(get_history_path()),
    }
