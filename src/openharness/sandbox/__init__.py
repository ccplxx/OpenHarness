"""OpenHarness 沙箱集成模块。

本模块统一导出沙箱子系统的核心组件，包括沙箱运行时适配器（srt）、Docker 后端、
路径验证器和会话管理器，为上层工具执行提供隔离的运行环境。
"""

from openharness.sandbox.adapter import (
    SandboxAvailability,
    SandboxUnavailableError,
    build_sandbox_runtime_config,
    get_sandbox_availability,
    wrap_command_for_sandbox,
)
from openharness.sandbox.docker_backend import DockerSandboxSession, get_docker_availability
from openharness.sandbox.path_validator import validate_sandbox_path
from openharness.sandbox.session import (
    get_docker_sandbox,
    is_docker_sandbox_active,
    start_docker_sandbox,
    stop_docker_sandbox,
)

__all__ = [
    "DockerSandboxSession",
    "SandboxAvailability",
    "SandboxUnavailableError",
    "build_sandbox_runtime_config",
    "get_docker_availability",
    "get_docker_sandbox",
    "get_sandbox_availability",
    "is_docker_sandbox_active",
    "start_docker_sandbox",
    "stop_docker_sandbox",
    "validate_sandbox_path",
    "wrap_command_for_sandbox",
]

