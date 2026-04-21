"""文件读取工具。

本模块提供 FileReadTool，用于读取 UTF-8 文本文件并以带行号的格式返回。
支持通过 offset 和 limit 参数分页读取文件的指定范围。
自动检测二进制文件（含空字节）并拒绝读取。
在 Docker 沙箱环境中，会验证路径是否在允许范围内。
该工具为只读工具。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class FileReadToolInput(BaseModel):
    """文件读取工具的输入参数。

    Attributes:
        path: 要读取的文件路径
        offset: 起始行号（从 0 开始），默认 0
        limit: 返回的行数，范围 1-2000，默认 200
    """

    path: str = Field(description="Path of the file to read")
    offset: int = Field(default=0, ge=0, description="Zero-based starting line")
    limit: int = Field(default=200, ge=1, le=2000, description="Number of lines to return")


class FileReadTool(BaseTool):
    """读取 UTF-8 文本文件并带行号显示的工具。

    支持分页读取和二进制文件检测。
    """

    name = "read_file"
    description = "Read a text file from the local repository."
    input_model = FileReadToolInput

    def is_read_only(self, arguments: FileReadToolInput) -> bool:
        """该工具为只读，不会修改任何文件。"""
        del arguments
        return True

    async def execute(
        self,
        arguments: FileReadToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行文件读取操作。

        读取文件内容，按行号偏移和限制返回带行号的格式化文本。
        在 Docker 沙箱环境中会验证路径安全性。

        Args:
            arguments: 包含路径、偏移和行数限制的输入参数
            context: 工具执行上下文

        Returns:
            带行号的文件内容文本
        """
        path = _resolve_path(context.cwd, arguments.path)

        from openharness.sandbox.session import is_docker_sandbox_active

        if is_docker_sandbox_active():
            from openharness.sandbox.path_validator import validate_sandbox_path

            allowed, reason = validate_sandbox_path(path, context.cwd)
            if not allowed:
                return ToolResult(output=f"Sandbox: {reason}", is_error=True)

        if not path.exists():
            return ToolResult(output=f"File not found: {path}", is_error=True)
        if path.is_dir():
            return ToolResult(output=f"Cannot read directory: {path}", is_error=True)

        raw = path.read_bytes()
        if b"\x00" in raw:
            return ToolResult(output=f"Binary file cannot be read as text: {path}", is_error=True)

        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        selected = lines[arguments.offset : arguments.offset + arguments.limit]
        numbered = [
            f"{arguments.offset + index + 1:>6}\t{line}"
            for index, line in enumerate(selected)
        ]
        if not numbered:
            return ToolResult(output=f"(no content in selected range for {path})")
        return ToolResult(output="\n".join(numbered))


def _resolve_path(base: Path, candidate: str) -> Path:
    """解析文件路径。

    展开用户目录符号（~），将相对路径基于 base 解析为绝对路径。

    Args:
        base: 基准路径
        candidate: 候选路径字符串

    Returns:
        解析后的绝对路径
    """
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()
