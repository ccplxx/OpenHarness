"""模块级 Docker 沙箱会话注册表。

本模块维护全局唯一的 Docker 沙箱会话实例，提供沙箱的启动、停止和
状态查询接口。通过 atexit 注册安全网，确保进程退出时容器被正确清理。
"""

from __future__ import annotations

import atexit
import logging
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openharness.config import Settings
    from openharness.sandbox.docker_backend import DockerSandboxSession

logger = logging.getLogger(__name__)

_active_session: DockerSandboxSession | None = None
"""当前活跃的 Docker 沙箱会话实例，全局唯一。"""


def get_docker_sandbox():
    """获取当前活跃的 Docker 沙箱会话实例。

    Returns:
        DockerSandboxSession | None: 活跃的沙箱会话，不存在则返回 None。
    """
    return _active_session


def is_docker_sandbox_active() -> bool:
    """检查 Docker 沙箱会话是否正在运行。

    Returns:
        bool: 沙箱会话存在且容器运行中返回 True。
    """
    return _active_session is not None and _active_session.is_running


async def start_docker_sandbox(
    settings: Settings,
    session_id: str,
    cwd: Path,
) -> None:
    """为当前 OpenHarness 会话启动 Docker 沙箱。

    先检测 Docker 沙箱可用性，不可用时根据 fail_if_unavailable 配置
    决定是抛出异常还是静默跳过。启动成功后注册 atexit 清理回调，
    确保进程异常退出时容器也能被停止。

    Args:
        settings: OpenHarness 配置对象。
        session_id: 会话唯一标识符。
        cwd: 项目工作目录路径。

    Raises:
        SandboxUnavailableError: Docker 沙箱不可用且 fail_if_unavailable 为 True。
    """
    global _active_session  # noqa: PLW0603

    from openharness.sandbox.docker_backend import DockerSandboxSession, get_docker_availability

    availability = get_docker_availability(settings)
    if not availability.available:
        if settings.sandbox.fail_if_unavailable:
            from openharness.sandbox.adapter import SandboxUnavailableError

            raise SandboxUnavailableError(
                availability.reason or "Docker sandbox is unavailable"
            )
        logger.warning("Docker sandbox unavailable: %s", availability.reason)
        return

    session = DockerSandboxSession(settings=settings, session_id=session_id, cwd=cwd)
    await session.start()
    _active_session = session

    # Safety net: stop the container if the process exits without close_runtime()
    atexit.register(session.stop_sync)


async def stop_docker_sandbox() -> None:
    """停止当前活跃的 Docker 沙箱会话，并将全局会话引用置为 None。"""
    global _active_session  # noqa: PLW0603
    if _active_session is not None:
        await _active_session.stop()
        _active_session = None
