"""Docker 镜像可用性检测与构建模块。

本模块负责确保沙箱 Docker 镜像的存在，支持从本地 Dockerfile 构建默认镜像。
当镜像不存在时，可根据配置自动触发构建流程，确保 Docker 沙箱后端正常运行。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_IMAGE = "openharness-sandbox:latest"
"""默认沙箱 Docker 镜像标签。"""

_DOCKERFILE_CONTENT = """\
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \\
    ripgrep bash git && \\
    rm -rf /var/lib/apt/lists/*
RUN useradd -m -s /bin/bash ohuser
USER ohuser
"""
"""内置的默认 Dockerfile 内容，基于 python:3.11-slim 并安装 ripgrep、bash、git。"""


def get_dockerfile_content() -> str:
    """返回默认沙箱镜像的 Dockerfile 内容。"""
    return _DOCKERFILE_CONTENT


async def _image_exists(image: str) -> bool:
    """检查指定的 Docker 镜像是否在本地存在。

    通过 ``docker image inspect`` 命令检测，返回码为 0 表示存在。

    Args:
        image: 镜像标签（如 openharness-sandbox:latest）。

    Returns:
        bool: 镜像存在返回 True。
    """
    docker = shutil.which("docker") or "docker"
    process = await asyncio.create_subprocess_exec(
        docker,
        "image",
        "inspect",
        image,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await process.communicate()
    return process.returncode == 0


async def build_default_image(image: str = _DEFAULT_IMAGE) -> bool:
    """构建默认的沙箱 Docker 镜像。

    优先使用沙箱目录下的 Dockerfile，若不存在则通过 stdin 传入内置的
    Dockerfile 内容进行构建。

    Args:
        image: 目标镜像标签，默认为 ``openharness-sandbox:latest``。

    Returns:
        bool: 构建成功返回 True，失败返回 False。
    """
    docker = shutil.which("docker") or "docker"
    dockerfile_path = Path(__file__).parent / "Dockerfile"

    if dockerfile_path.exists():
        cmd = [docker, "build", "-t", image, "-f", str(dockerfile_path), str(dockerfile_path.parent)]
    else:
        # Fallback: pipe Dockerfile content via stdin
        cmd = [docker, "build", "-t", image, "-"]

    logger.info("Building Docker sandbox image %r ...", image)

    if dockerfile_path.exists():
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await process.communicate(input=_DOCKERFILE_CONTENT.encode("utf-8"))
        if process.returncode == 0:
            logger.info("Docker sandbox image %r built successfully", image)
            return True
        logger.warning("Failed to build Docker sandbox image %r", image)
        return False

    _, stderr_bytes = await process.communicate()
    if process.returncode == 0:
        logger.info("Docker sandbox image %r built successfully", image)
        return True

    logger.warning(
        "Failed to build Docker sandbox image %r: %s",
        image,
        stderr_bytes.decode("utf-8", errors="replace").strip(),
    )
    return False


async def ensure_image_available(image: str, auto_build: bool) -> bool:
    """确保沙箱镜像可用，必要时自动构建。

    先检查本地是否已存在指定镜像，若不存在且 auto_build 为 True 则
    触发自动构建。

    Args:
        image: 目标镜像标签。
        auto_build: 镜像不存在时是否自动构建。

    Returns:
        bool: 镜像可用返回 True，不可用返回 False。
    """
    if await _image_exists(image):
        return True
    if not auto_build:
        logger.warning("Docker image %r not found and auto_build_image is disabled", image)
        return False
    return await build_default_image(image)
