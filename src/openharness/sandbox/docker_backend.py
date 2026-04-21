"""基于 Docker 的沙箱后端模块。

本模块实现了一个长期运行的 Docker 容器作为沙箱隔离环境，支持容器的创建、
启停、命令执行等生命周期管理。通过绑定挂载项目目录、禁用网络和设置资源限制
来确保工具执行的安全隔离。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from openharness.config import Settings
from openharness.platforms import get_platform, get_platform_capabilities
from openharness.sandbox.adapter import SandboxAvailability, SandboxUnavailableError

logger = logging.getLogger(__name__)


def get_docker_availability(settings: Settings) -> SandboxAvailability:
    """检测 Docker 是否可作为沙箱后端使用。

    依次检查：配置是否启用 Docker 后端 → 平台是否支持 Docker 沙箱 →
    Docker CLI 是否存在 → Docker 守护进程是否运行。

    Args:
        settings: OpenHarness 配置对象。

    Returns:
        SandboxAvailability: Docker 沙箱的可用性信息。
    """
    if not settings.sandbox.enabled or settings.sandbox.backend != "docker":
        return SandboxAvailability(
            enabled=False, available=False, reason="Docker sandbox is not enabled"
        )

    platform_name = get_platform()
    capabilities = get_platform_capabilities(platform_name)
    if not capabilities.supports_docker_sandbox:
        return SandboxAvailability(
            enabled=True,
            available=False,
            reason=f"Docker sandbox is not supported on platform {platform_name}",
        )

    docker = shutil.which("docker")
    if not docker:
        return SandboxAvailability(
            enabled=True,
            available=False,
            reason="Docker CLI not found; install Docker Desktop or Docker Engine",
        )

    try:
        subprocess.run(
            [docker, "info"],
            capture_output=True,
            timeout=5,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return SandboxAvailability(
            enabled=True,
            available=False,
            reason="Docker daemon is not running",
            command=docker,
        )

    return SandboxAvailability(enabled=True, available=True, command=docker)


@dataclass
class DockerSandboxSession:
    """管理一个 OpenHarness 会话对应的长期运行 Docker 沙箱容器。

    容器以分离模式（-d）运行，绑定挂载项目目录，禁用网络访问，
    并可配置 CPU 和内存资源限制。提供异步和同步的启停接口，
    以及在容器内执行命令的能力。

    Attributes:
        settings: OpenHarness 配置对象。
        session_id: 会话唯一标识符，用于构造容器名。
        cwd: 项目工作目录路径，将作为绑定挂载的源和容器工作目录。
    """

    settings: Settings
    session_id: str
    cwd: Path
    _container_name: str = field(init=False)
    _running: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        """初始化后根据 session_id 生成容器名称。"""
        self._container_name = f"openharness-sandbox-{self.session_id}"

    @property
    def container_name(self) -> str:
        """返回沙箱容器的名称。"""
        return self._container_name

    @property
    def is_running(self) -> bool:
        """返回沙箱容器是否正在运行。"""
        return self._running

    def _build_run_argv(self) -> list[str]:
        """构建 ``docker run`` 命令参数列表。

        包含容器名、网络禁用、资源限制、绑定挂载、环境变量等配置，
        最终以 ``tail -f /dev/null`` 保持容器持续运行。

        Returns:
            list[str]: 完整的 docker run 参数列表。
        """
        docker = shutil.which("docker") or "docker"
        sandbox = self.settings.sandbox
        docker_cfg = sandbox.docker
        cwd_str = str(self.cwd.resolve())

        argv = [
            docker,
            "run",
            "-d",
            "--rm",
            "--name",
            self._container_name,
        ]

        # Docker backend currently supports only fully disabled networking.
        # Domain-level allow/deny policies exist for the srt backend, but Docker
        # does not enforce them yet. Fail closed instead of silently widening
        # egress to unrestricted bridge networking.
        if sandbox.network.allowed_domains or sandbox.network.denied_domains:
            logger.warning(
                "Docker sandbox does not enforce allowed_domains/denied_domains yet; "
                "keeping network disabled"
            )
        argv.extend(["--network", "none"])

        # Resource limits
        if docker_cfg.cpu_limit > 0:
            argv.extend(["--cpus", str(docker_cfg.cpu_limit)])
        if docker_cfg.memory_limit:
            argv.extend(["--memory", docker_cfg.memory_limit])

        # Bind-mount project directory at the same path
        argv.extend(["-v", f"{cwd_str}:{cwd_str}"])
        argv.extend(["-w", cwd_str])

        # Extra mounts
        for mount in docker_cfg.extra_mounts:
            argv.extend(["-v", mount])

        # Extra environment variables
        for key, value in docker_cfg.extra_env.items():
            argv.extend(["-e", f"{key}={value}"])

        argv.extend([docker_cfg.image, "tail", "-f", "/dev/null"])
        return argv

    async def start(self) -> None:
        """创建并启动沙箱容器。

        首先确保 Docker 镜像可用（必要时自动构建），然后通过
        ``docker run`` 启动容器。启动失败时抛出 SandboxUnavailableError。

        Raises:
            SandboxUnavailableError: 镜像不可用或容器启动失败。
        """
        from openharness.sandbox.docker_image import ensure_image_available

        docker_cfg = self.settings.sandbox.docker
        available = await ensure_image_available(
            docker_cfg.image, docker_cfg.auto_build_image
        )
        if not available:
            raise SandboxUnavailableError(
                f"Docker image {docker_cfg.image!r} is not available and "
                "auto_build_image is disabled"
            )

        argv = self._build_run_argv()
        logger.info("Starting Docker sandbox: %s", " ".join(argv))

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            msg = stderr.decode("utf-8", errors="replace").strip()
            raise SandboxUnavailableError(f"Failed to start Docker sandbox: {msg}")

        self._running = True
        logger.info("Docker sandbox started: %s", self._container_name)

    async def stop(self) -> None:
        """异步停止并移除沙箱容器，发送 ``docker stop -t 5`` 后等待退出。"""
        if not self._running:
            return
        docker = shutil.which("docker") or "docker"
        try:
            process = await asyncio.create_subprocess_exec(
                docker,
                "stop",
                "-t",
                "5",
                self._container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=15)
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("Error stopping Docker sandbox: %s", exc)
        finally:
            self._running = False
            logger.info("Docker sandbox stopped: %s", self._container_name)

    def stop_sync(self) -> None:
        """同步停止容器，适用于 atexit 注册的清理回调。"""
        if not self._running:
            return
        docker = shutil.which("docker") or "docker"
        try:
            subprocess.run(
                [docker, "stop", "-t", "3", self._container_name],
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
        finally:
            self._running = False

    async def exec_command(
        self,
        argv: list[str],
        *,
        cwd: str | Path,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> asyncio.subprocess.Process:
        """在沙箱容器内执行命令。

        通过 ``docker exec`` 在运行中的容器内启动子进程，
        返回与 ``asyncio.create_subprocess_exec`` 相同接口的进程对象。

        Args:
            argv: 待执行的命令参数列表。
            cwd: 容器内的工作目录。
            stdin: 标准输入文件描述符。
            stdout: 标准输出文件描述符。
            stderr: 标准错误文件描述符。
            env: 额外的环境变量字典。

        Returns:
            asyncio.subprocess.Process: 异步子进程对象。

        Raises:
            SandboxUnavailableError: 容器未在运行时抛出。
        """
        if not self._running:
            raise SandboxUnavailableError("Docker sandbox session is not running")

        docker = shutil.which("docker") or "docker"
        cmd: list[str] = [docker, "exec"]
        cmd.extend(["-w", str(Path(cwd).resolve())])

        if env:
            for key, value in env.items():
                cmd.extend(["-e", f"{key}={value}"])

        cmd.append(self._container_name)
        cmd.extend(argv)

        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
        )
