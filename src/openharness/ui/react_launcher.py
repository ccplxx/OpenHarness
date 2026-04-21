"""React 终端前端启动器。

本模块负责定位、安装和启动 React 终端前端（Ink + tsx 实现），
并构建后端宿主进程的启动命令。处理跨平台兼容性：
- Windows/WSL 上直接调用 tsx 二进制以保持 TTY 原始模式
- 自动检测本地/全局 tsx 安装路径，回退到 npm exec
- 支持打包安装（pip install）和开发仓库两种前端目录布局
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path


def _resolve_theme() -> str:
    """从设置中读取主题名称，失败时默认为 'default'。"""
    try:
        from openharness.config.settings import load_settings
        return load_settings().theme or "default"
    except Exception:
        return "default"


def _resolve_npm() -> str:
    """解析 npm 可执行文件路径（Windows 上为 npm.cmd）。"""
    return shutil.which("npm") or "npm"


def _resolve_tsx(frontend_dir: Path) -> tuple[str, ...]:
    """解析 tsx 命令路径，优先直接调用以绕过 npm exec。

    在 Windows/WSL 上，npm exec -- tsx 的包装链经常产生中间 cmd.exe /
    shell 进程，破坏 TTY stdin 继承，导致 Ink 的 useInput
    （需要 raw-mode stdin）无法工作。直接调用 tsx 二进制
    可保持 TTY 正常。

    解析优先级：
    1. 前端目录的 node_modules/.bin/ 下的本地 tsx
    2. 全局安装的 tsx
    3. 回退到 "npm exec -- tsx"（可能破坏 Windows/WSL 的 TTY）

    返回命令元组，如 ("path/to/tsx",) 或 ("npm", "exec", "--", "tsx")。
    """
    # 1. Prefer the locally-installed binary
    bin_dir = frontend_dir / "node_modules" / ".bin"
    if sys.platform == "win32":
        for name in ("tsx.cmd", "tsx.ps1", "tsx"):
            candidate = bin_dir / name
            if candidate.exists():
                return (str(candidate),)
    else:
        candidate = bin_dir / "tsx"
        if candidate.exists():
            return (str(candidate),)

    # 2. Fall back to a globally-installed tsx
    global_tsx = shutil.which("tsx")
    if global_tsx:
        return (global_tsx,)

    # 3. Last resort — go through npm exec (may break TTY on Windows/WSL)
    return (_resolve_npm(), "exec", "--", "tsx")


def get_frontend_dir() -> Path:
    """返回 React 终端前端目录路径。

    按以下顺序检查：
    1. 打包安装目录（openharness/_frontend/）
    2. 开发仓库布局（<repo>/frontend/terminal/）

    均不存在时返回打包路径（后续会报出清晰的错误信息）。
    """
    # 1. Bundled inside package: openharness/_frontend/
    pkg_frontend = Path(__file__).resolve().parent.parent / "_frontend"
    if (pkg_frontend / "package.json").exists():
        return pkg_frontend

    # 2. Development repo: <repo>/frontend/terminal/
    repo_root = Path(__file__).resolve().parents[3]
    dev_frontend = repo_root / "frontend" / "terminal"
    if (dev_frontend / "package.json").exists():
        return dev_frontend

    # Fallback to package path (will error with clear message)
    return pkg_frontend


def build_backend_command(
    *,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    permission_mode: str | None = None,
) -> list[str]:
    """构建 React 前端用于生成后端宿主进程的命令行。

    返回形如 [python, -m, openharness, --backend-only, ...] 的命令列表，
    包含所有非空参数对应的 CLI 标志。
    """
    command = [sys.executable, "-m", "openharness", "--backend-only"]
    if cwd:
        command.extend(["--cwd", cwd])
    if model:
        command.extend(["--model", model])
    if max_turns is not None:
        command.extend(["--max-turns", str(max_turns)])
    if base_url:
        command.extend(["--base-url", base_url])
    if system_prompt:
        command.extend(["--system-prompt", system_prompt])
    if api_key:
        command.extend(["--api-key", api_key])
    if api_format:
        command.extend(["--api-format", api_format])
    if permission_mode:
        command.extend(["--permission-mode", permission_mode])
    return command


async def launch_react_tui(
    *,
    prompt: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    base_url: str | None = None,
    system_prompt: str | None = None,
    api_key: str | None = None,
    api_format: str | None = None,
    permission_mode: str | None = None,
) -> int:
    """启动 React 终端前端作为默认 UI。

    流程：
    1. 定位前端目录，验证 package.json 存在
    2. 若 node_modules 不存在则自动 npm install
    3. 通过环境变量 OPENHARNESS_FRONTEND_CONFIG 传递后端命令和配置
    4. 使用 tsx 运行 src/index.tsx 启动 Ink 前端
    5. 等待前端进程退出并返回退出码
    """
    frontend_dir = get_frontend_dir()
    package_json = frontend_dir / "package.json"
    if not package_json.exists():
        raise RuntimeError(f"React terminal frontend is missing: {package_json}")

    npm = _resolve_npm()

    if not (frontend_dir / "node_modules").exists():
        install = await asyncio.create_subprocess_exec(
            npm,
            "install",
            "--no-fund",
            "--no-audit",
            cwd=str(frontend_dir),
        )
        if await install.wait() != 0:
            raise RuntimeError("Failed to install React terminal frontend dependencies")

    env = os.environ.copy()
    env["OPENHARNESS_FRONTEND_CONFIG"] = json.dumps(
        {
            "backend_command": build_backend_command(
                cwd=cwd or str(Path.cwd()),
                model=model,
                max_turns=max_turns,
                base_url=base_url,
                system_prompt=system_prompt,
                api_key=api_key,
                api_format=api_format,
                permission_mode=permission_mode,
            ),
            "initial_prompt": prompt,
            "theme": _resolve_theme(),
        }
    )
    tsx_cmd = _resolve_tsx(frontend_dir)
    process = await asyncio.create_subprocess_exec(
        *tsx_cmd,
        "src/index.tsx",
        cwd=str(frontend_dir),
        env=env,
        stdin=None,
        stdout=None,
        stderr=None,
    )
    return await process.wait()


__all__ = ["build_backend_command", "get_frontend_dir", "launch_react_tui"]
