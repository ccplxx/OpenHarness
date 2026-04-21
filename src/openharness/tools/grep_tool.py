"""文件内容正则搜索工具。

本模块提供 GrepTool，用于在文件中搜索匹配正则表达式的内容。
优先使用 ripgrep 进行高性能搜索，当 ripgrep 不可用时回退到纯 Python 实现。
支持单文件搜索和目录递归搜索，支持大小写敏感/不敏感模式。
搜索结果格式为 文件路径:行号:匹配行内容。
该工具为只读工具。
"""

from __future__ import annotations

import asyncio
import re
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class GrepToolInput(BaseModel):
    """文件内容搜索工具的输入参数。

    Attributes:
        pattern: 要搜索的正则表达式
        root: 搜索根目录，默认为当前工作目录
        file_glob: 文件匹配模式，默认为 **/*（所有文件）
        case_sensitive: 是否区分大小写，默认为 True
        limit: 最大匹配数，范围 1-2000，默认 200
        timeout_seconds: 超时时间（秒），范围 1-120，默认 20
    """

    pattern: str = Field(description="Regular expression to search for")
    root: str | None = Field(default=None, description="Search root directory")
    file_glob: str = Field(default="**/*")
    case_sensitive: bool = Field(default=True)
    limit: int = Field(default=200, ge=1, le=2000)
    timeout_seconds: int = Field(default=20, ge=1, le=120)


class GrepTool(BaseTool):
    """在文本文件中搜索正则表达式匹配的工具。

    优先使用 ripgrep，不可用时回退到纯 Python 实现。
    """

    name = "grep"
    description = "Search file contents with a regular expression."
    input_model = GrepToolInput

    def is_read_only(self, arguments: GrepToolInput) -> bool:
        """该工具为只读，不会修改任何文件。"""

    async def execute(self, arguments: GrepToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行文件内容搜索。

        根据搜索目标是文件还是目录选择不同的搜索策略。
        对于文件直接搜索，对于目录优先使用 ripgrep 递归搜索。

        Args:
            arguments: 包含搜索模式和选项的输入参数
            context: 工具执行上下文

        Returns:
            格式化的搜索结果（文件路径:行号:匹配内容）
        """
        root = _resolve_path(context.cwd, arguments.root) if arguments.root else context.cwd
        if root.is_file():
            display_base = _display_base(root, context.cwd)
            matches = await _rg_grep_file(
                path=root,
                pattern=arguments.pattern,
                case_sensitive=arguments.case_sensitive,
                limit=arguments.limit,
                display_base=display_base,
                timeout_seconds=arguments.timeout_seconds,
            )
            if matches is not None:
                return _format_rg_result(matches, arguments.timeout_seconds)

            return ToolResult(
                output=_python_grep_files(
                    paths=[root],
                    pattern=arguments.pattern,
                    case_sensitive=arguments.case_sensitive,
                    limit=arguments.limit,
                    display_base=display_base,
                )
            )

        # Prefer ripgrep for performance; fallback to Python when unavailable.
        matches = await _rg_grep(
            root=root,
            pattern=arguments.pattern,
            file_glob=arguments.file_glob,
            case_sensitive=arguments.case_sensitive,
            limit=arguments.limit,
            timeout_seconds=arguments.timeout_seconds,
        )
        if matches is not None:
            return _format_rg_result(matches, arguments.timeout_seconds)

        # Python fallback (kept for portability).
        return ToolResult(
            output=_python_grep_files(
                paths=root.glob(arguments.file_glob),
                pattern=arguments.pattern,
                case_sensitive=arguments.case_sensitive,
                limit=arguments.limit,
                display_base=root,
            )
        )


def _display_base(path: Path, cwd: Path) -> Path:
    """计算显示路径的基准目录。

    如果路径是 cwd 的子路径，使用 cwd 作为基准以显示相对路径；
    否则使用路径的父目录作为基准。

    Args:
        path: 目标路径
        cwd: 当前工作目录

    Returns:
        适合用于显示相对路径的基准路径
    """
    try:
        path.relative_to(cwd)
    except ValueError:
        return path.parent
    return cwd


def _python_grep_files(
    *,
    paths,
    pattern: str,
    case_sensitive: bool,
    limit: int,
    display_base: Path,
) -> str:
    """纯 Python 实现的文件内容搜索（ripgrep 不可用时的回退方案）。

    遍历文件列表，逐行搜索正则匹配，跳过二进制文件。

    Args:
        paths: 可迭代的文件路径集合
        pattern: 正则表达式
        case_sensitive: 是否区分大小写
        limit: 最大匹配数
        display_base: 显示路径的基准目录

    Returns:
        格式化的搜索结果文本
    """
    # Python fallback (kept for portability).
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled = re.compile(pattern, flags)
    collected: list[str] = []

    for path in paths:
        if len(collected) >= limit:
            break
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                collected.append(f"{_format_path(path, display_base)}:{line_no}:{line}")
                if len(collected) >= limit:
                    break

    if not collected:
        return "(no matches)"
    return "\n".join(collected)


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


def _format_rg_result(matches: list[str], timeout_seconds: int) -> ToolResult:
    """格式化 ripgrep 搜索结果。

    检查是否存在超时标记，如果超时则在输出中添加超时提示。
    超时时 is_error 设为 True。

    Args:
        matches: ripgrep 输出的匹配行列表
        timeout_seconds: 超时时间（秒）

    Returns:
        格式化的 ToolResult
    """
    timed_out = bool(matches and matches[-1] == _timeout_marker(timeout_seconds))
    rendered = matches[:-1] if timed_out else matches
    output = "\n".join(rendered) if rendered else "(no matches)"
    if timed_out:
        output = (
            f"{output}\n\n[grep timed out after {timeout_seconds} seconds]"
            if output != "(no matches)"
            else f"[grep timed out after {timeout_seconds} seconds]"
        )
    return ToolResult(output=output, is_error=timed_out)


async def _rg_grep(
    *,
    root: Path,
    pattern: str,
    file_glob: str,
    case_sensitive: bool,
    limit: int,
    timeout_seconds: int,
) -> list[str] | None:
    """使用 ripgrep 在目录中搜索匹配内容。

    构建 ripgrep 命令行参数，启动子进程并收集结果。
    支持超时控制和 Docker 沙箱环境。
    rg 返回码 0 表示找到匹配，1 表示未找到，其他值表示错误需回退。

    Args:
        root: 搜索根目录
        pattern: 正则表达式
        file_glob: 文件过滤模式
        case_sensitive: 是否区分大小写
        limit: 最大匹配数
        timeout_seconds: 超时时间（秒）

    Returns:
        匹配行列表，ripgrep 不可用或出错时返回 None
    """
    rg = shutil.which("rg")
    if not rg:
        return None

    include_hidden = (root / ".git").exists() or (root / ".gitignore").exists()
    cmd: list[str] = [
        rg,
        "--no-heading",
        "--line-number",
        "--color",
        "never",
    ]
    if include_hidden:
        cmd.append("--hidden")
    if not case_sensitive:
        cmd.append("-i")
    if file_glob:
        cmd.extend(["--glob", file_glob])
    # `--` ensures patterns like `-foo` aren't parsed as flags.
    cmd.extend(["--", pattern, "."])

    from openharness.sandbox.session import get_docker_sandbox

    session = get_docker_sandbox()
    if session is not None and session.is_running:
        process = await session.exec_command(
            cmd,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,  # 8 MB per line — avoids LimitOverrunError on long lines
        )

    matches: list[str] = []
    try:
        await asyncio.wait_for(
            _collect_rg_matches(process, matches, limit=limit),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        matches.append(_timeout_marker(timeout_seconds))
        await _terminate_process(process)
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    finally:
        if len(matches) >= limit and process.returncode is None:
            await _terminate_process(process)
        elif process.returncode is None:
            await process.wait()

    # rg exits 0 when matches are found, 1 when none are found.
    # Any other return code indicates an error; fall back to Python.
    if process.returncode in {0, 1, -15, -9}:
        return matches
    return None


async def _rg_grep_file(
    *,
    path: Path,
    pattern: str,
    case_sensitive: bool,
    limit: int,
    display_base: Path,
    timeout_seconds: int,
) -> list[str] | None:
    """使用 ripgrep 在单个文件中搜索匹配内容。

    类似 _rg_grep，但针对单个文件搜索，输出格式带文件路径前缀。

    Args:
        path: 目标文件路径
        pattern: 正则表达式
        case_sensitive: 是否区分大小写
        limit: 最大匹配数
        display_base: 显示路径的基准目录
        timeout_seconds: 超时时间（秒）

    Returns:
        匹配行列表，ripgrep 不可用或出错时返回 None
    """
    rg = shutil.which("rg")
    if not rg:
        return None

    cmd: list[str] = [
        rg,
        "--no-heading",
        "--line-number",
        "--color",
        "never",
    ]
    if not case_sensitive:
        cmd.append("-i")
    cmd.extend(["--", pattern, path.name])

    from openharness.sandbox.session import get_docker_sandbox

    session = get_docker_sandbox()
    if session is not None and session.is_running:
        process = await session.exec_command(
            cmd,
            cwd=path.parent,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(path.parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=8 * 1024 * 1024,  # 8 MB per line — avoids LimitOverrunError on long lines
        )

    matches: list[str] = []
    try:
        await asyncio.wait_for(
            _collect_rg_file_matches(
                process,
                matches,
                limit=limit,
                path=path,
                display_base=display_base,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        matches.append(_timeout_marker(timeout_seconds))
        await _terminate_process(process)
    except asyncio.CancelledError:
        await _terminate_process(process)
        raise
    finally:
        if len(matches) >= limit and process.returncode is None:
            await _terminate_process(process)
        elif process.returncode is None:
            await process.wait()

    if process.returncode in {0, 1, -15, -9}:
        return matches
    return None


def _timeout_marker(timeout_seconds: int) -> str:
    """生成超时标记字符串。

    用于在匹配列表中标记搜索超时，以便后续格式化时识别。

    Args:
        timeout_seconds: 超时时间（秒）

    Returns:
        超时标记字符串
    """
    return f"__OPENHARNESS_GREP_TIMEOUT__:{timeout_seconds}"


async def _collect_rg_matches(
    process: asyncio.subprocess.Process,
    matches: list[str],
    *,
    limit: int,
) -> None:
    """从 ripgrep 进程的 stdout 收集匹配行。

    逐行读取进程输出，解码为文本后添加到 matches 列表。
    跳过超长行（超过缓冲区限制）继续读取。

    Args:
        process: ripgrep 子进程
        matches: 用于收集结果的列表
        limit: 最大匹配数
    """
    assert process.stdout is not None
    while len(matches) < limit:
        try:
            raw = await process.stdout.readline()
        except ValueError:
            # Line exceeded the stream buffer limit; skip it and continue.
            continue
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if line:
            matches.append(line)


async def _collect_rg_file_matches(
    process: asyncio.subprocess.Process,
    matches: list[str],
    *,
    limit: int,
    path: Path,
    display_base: Path,
) -> None:
    """从 ripgrep 进程收集单文件搜索结果并添加路径前缀。

    与 _collect_rg_matches 类似，但在每行结果前添加文件路径。

    Args:
        process: ripgrep 子进程
        matches: 用于收集结果的列表
        limit: 最大匹配数
        path: 目标文件路径
        display_base: 显示路径的基准目录
    """
    assert process.stdout is not None
    while len(matches) < limit:
        try:
            raw = await process.stdout.readline()
        except ValueError:
            # Line exceeded the stream buffer limit; skip it and continue.
            continue
        if not raw:
            break
        line = raw.decode("utf-8", errors="replace").rstrip("\n")
        if not line:
            continue
        matches.append(f"{_format_path(path, display_base)}:{line}")


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    """终止子进程。

    先发送 SIGTERM，2 秒内未退出则发送 SIGKILL。

    Args:
        process: 要终止的异步子进程
    """
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
    return None


def _format_path(path: Path, display_base: Path) -> str:
    """格式化文件路径为相对或绝对路径字符串。

    尝试将路径转换为相对于 display_base 的相对路径，失败则使用绝对路径。

    Args:
        path: 目标路径
        display_base: 基准路径

    Returns:
        相对路径或绝对路径字符串
    """
    try:
        return str(path.relative_to(display_base))
    except ValueError:
        return str(path)
