"""Task exports."""
from __future__ import annotations

from pathlib import Path

from openharness.tasks.manager import BackgroundTaskManager, get_task_manager
from openharness.tasks.types import TaskRecord, TaskStatus, TaskType

async def spawn_local_agent_task(
    *,
    prompt: str,
    description: str,
    cwd: str | Path,
    model: str | None = None,
    api_key: str | None = None,
    command: str | None = None,
) -> TaskRecord:
    """Spawn a local agent subprocess task."""
    return await get_task_manager().create_agent_task(
        prompt=prompt,
        description=description,
        cwd=cwd,
        model=model,
        api_key=api_key,
        command=command,
    )

async def spawn_shell_task(command: str, description: str, cwd: str | Path) -> TaskRecord:
    """Spawn a local shell task."""
    return await get_task_manager().create_shell_task(
        command=command,
        description=description,
        cwd=cwd,
    )


async def stop_task(task_id: str) -> TaskRecord:
    """Stop a running task via the default task manager."""
    return await get_task_manager().stop_task(task_id)


__all__ = [
    "BackgroundTaskManager",
    "TaskRecord",
    "TaskStatus",
    "TaskType",
    "get_task_manager",
    "spawn_local_agent_task",
    "spawn_shell_task",
    "stop_task",
]
