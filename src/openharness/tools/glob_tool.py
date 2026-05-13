"""文件系统 Glob 匹配工具。

本模块提供 GlobTool，用于列出匹配指定 glob 模式的文件。
优先使用 ripgrep 的文件遍历器（尊重 .gitignore，可跳过 .venv 等大目录），
当 ripgrep 不可用时回退到 Python 的 Path.glob。
在 git 仓库中会自动包含隐藏文件（如 .github/）。
该工具为只读工具。
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from pydantic import AliasChoices, BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class GlobToolInput(BaseModel):
    """文件 Glob 匹配工具的输入参数。

    Attributes:
        pattern: 相对于工作目录的 glob 匹配模式
        root: 可选的搜索根目录
        limit: 最大匹配数，范围 1-5000，默认 200
    """

    pattern: str = Field(
        description="Glob pattern relative to the working directory",
        validation_alias=AliasChoices("pattern", "path"),
    )
    root: str | None = Field(default=None, description="Optional search root")
    limit: int = Field(default=200, ge=1, le=5000)


class GlobTool(BaseTool):
    """列出匹配 glob 模式文件的工具。

    优先使用 ripgrep 文件遍历器，不可用时回退到 Python glob。
    """

    name = "glob"
    description = "List files matching a glob pattern."
    input_model = GlobToolInput

    def is_read_only(self, arguments: GlobToolInput) -> bool:
        """该工具为只读，不会修改任何文件。"""

    async def execute(self, arguments: GlobToolInput, context: ToolExecutionContext) -> ToolResult:
        root, pattern = _resolve_glob_request(context.cwd, arguments.root, arguments.pattern)
        matches = await _glob(root, pattern, limit=arguments.limit)
        if not matches:
            return ToolResult(output="(no matches)")
        return ToolResult(output="\n".join(matches))


def _resolve_path(base: Path, candidate: str | None) -> Path:
    """解析文件路径。

    展开用户目录符号（~），将相对路径基于 base 解析为绝对路径。

    Args:
        base: 基准路径
        candidate: 候选路径字符串，可为 None

    Returns:
        解析后的绝对路径
    """
    path = Path(candidate or ".").expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _resolve_glob_request(base: Path, root_arg: str | None, pattern: str) -> tuple[Path, str]:
    """Return a concrete search root plus a root-relative glob pattern."""
    if not pattern.strip():
        return (_resolve_path(base, root_arg) if root_arg else base, pattern)

    candidate = Path(pattern).expanduser()
    if not candidate.is_absolute():
        return (_resolve_path(base, root_arg) if root_arg else base, pattern)

    parts = candidate.parts
    first_glob_index = next(
        (index for index, part in enumerate(parts) if _has_glob_magic(part)),
        None,
    )
    if first_glob_index is None:
        return candidate.parent.resolve(), candidate.name

    root_parts = parts[:first_glob_index]
    root = Path(*root_parts).resolve() if root_parts else Path(candidate.anchor or "/").resolve()
    relative_pattern = str(Path(*parts[first_glob_index:]))
    return root, relative_pattern


def _has_glob_magic(value: str) -> bool:
    return any(char in value for char in "*?[")


def _looks_like_git_repo(path: Path) -> bool:
    """判断路径是否看起来像 git 仓库。

    启发式判断：向上遍历最多 6 层目录，检查是否存在 .git 目录。
    对于代码仓库，隐藏目录（如 .github/）通常相关；
    对于普通目录（如用户主目录），搜索隐藏路径会爆炸搜索空间。

    Args:
        path: 要检查的路径

    Returns:
        若为 git 仓库返回 True
    """
    current = path
    for _ in range(6):
        git_dir = current / ".git"
        if git_dir.exists():
            return True
        if current.parent == current:
            break
        current = current.parent
    return False


_GLOB_RG_TIMEOUT_SECONDS = 30.0


async def _glob(root: Path, pattern: str, *, limit: int) -> list[str]:
    """快速 glob 实现。

    当 ripgrep 可用时使用其文件遍历器（尊重 .gitignore，跳过 .venv 等大目录），
    否则回退到 Python 的 Path.glob。对包含 ** 或 / 的模式优先使用 ripgrep。

    Args:
        root: 搜索根目录
        pattern: glob 匹配模式
        limit: 最大匹配数

    Returns:
        排序后的匹配路径列表
    """
    if not root.exists() or not root.is_dir():
        return []

    rg = shutil.which("rg")
    # `Path.glob("**/*")` will traverse hidden and ignored paths (like `.venv/`)
    # and can be very slow on real workspaces. Prefer `rg --files`.
    if rg and ("**" in pattern or "/" in pattern):
        include_hidden = _looks_like_git_repo(root)
        cmd = [rg, "--files"]
        if include_hidden:
            cmd.append("--hidden")
        cmd.extend(["--glob", pattern, "."])

        from openharness.sandbox.session import get_docker_sandbox

        session = get_docker_sandbox()
        if session is not None and session.is_running:
            process = await session.exec_command(
                cmd,
                cwd=root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

        lines: list[str] = []

        async def _read_stdout() -> None:
            assert process.stdout is not None
            while len(lines) < limit:
                raw = await process.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    lines.append(line)

        try:
            try:
                await asyncio.wait_for(_read_stdout(), timeout=_GLOB_RG_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                pass
        finally:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()

        # Sorting keeps unit tests and user output deterministic for small results.
        lines.sort()
        return lines

    # Fallback: non-recursive patterns are usually cheap; keep Python semantics.
    return sorted(
        str(path.relative_to(root))
        for path in root.glob(pattern)
    )[:limit]
