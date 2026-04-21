"""文件写入工具。

本模块提供 FileWriteTool，用于创建或覆盖文本文件。
可选择是否自动创建父目录（默认创建）。
在 Docker 沙箱环境中，会验证路径是否在允许范围内。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class FileWriteToolInput(BaseModel):
    """文件写入工具的输入参数。

    Attributes:
        path: 要写入的文件路径
        content: 完整的文件内容
        create_directories: 是否自动创建父目录，默认为 True
    """

    path: str = Field(description="Path of the file to write")
    content: str = Field(description="Full file contents")
    create_directories: bool = Field(default=True)


class FileWriteTool(BaseTool):
    """创建或覆盖文本文件的工具。

    支持自动创建父目录。
    """

    name = "write_file"
    description = "Create or overwrite a text file in the local repository."
    input_model = FileWriteToolInput

    async def execute(
        self,
        arguments: FileWriteToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行文件写入操作。

        解析路径后，可选创建父目录，然后将内容写入文件。
        在 Docker 沙箱环境中会验证路径安全性。

        Args:
            arguments: 包含路径和内容的输入参数
            context: 工具执行上下文

        Returns:
            写入确认信息
        """
        path = _resolve_path(context.cwd, arguments.path)

        from openharness.sandbox.session import is_docker_sandbox_active

        if is_docker_sandbox_active():
            from openharness.sandbox.path_validator import validate_sandbox_path

            allowed, reason = validate_sandbox_path(path, context.cwd)
            if not allowed:
                return ToolResult(output=f"Sandbox: {reason}", is_error=True)

        if arguments.create_directories:
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(arguments.content, encoding="utf-8")
        return ToolResult(output=f"Wrote {path}")


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
