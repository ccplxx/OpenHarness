"""基于字符串替换的文件编辑工具。

本模块提供 FileEditTool，用于在现有文件中通过查找并替换字符串来编辑文件内容。
支持两种替换模式：
- 单次替换（默认）：只替换第一个匹配
- 全部替换：替换文件中所有匹配

在 Docker 沙箱环境中，会验证路径是否在允许范围内。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class FileEditToolInput(BaseModel):
    """文件编辑工具的输入参数。

    Attributes:
        path: 要编辑的文件路径
        old_str: 要替换的原始文本
        new_str: 替换后的新文本
        replace_all: 是否替换所有匹配（默认只替换第一个）
    """

    path: str = Field(description="Path of the file to edit")
    old_str: str = Field(description="Existing text to replace")
    new_str: str = Field(description="Replacement text")
    replace_all: bool = Field(default=False)


class FileEditTool(BaseTool):
    """通过字符串替换编辑现有文件的工具。

    支持单次替换和全部替换两种模式。
    """

    name = "edit_file"
    description = "Edit an existing file by replacing a string."
    input_model = FileEditToolInput

    async def execute(
        self,
        arguments: FileEditToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行文件编辑操作。

        读取文件内容，查找并替换指定字符串，然后写回文件。
        在 Docker 沙箱环境中会验证路径安全性。

        Args:
            arguments: 包含路径、旧文本和新文本的输入参数
            context: 工具执行上下文

        Returns:
            编辑结果信息或错误（文件不存在/旧文本未找到）
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

        original = path.read_text(encoding="utf-8")
        if arguments.old_str not in original:
            return ToolResult(output="old_str was not found in the file", is_error=True)

        if arguments.replace_all:
            updated = original.replace(arguments.old_str, arguments.new_str)
        else:
            updated = original.replace(arguments.old_str, arguments.new_str, 1)

        path.write_text(updated, encoding="utf-8")
        return ToolResult(output=f"Updated {path}")


def _resolve_path(base: Path, candidate: str) -> Path:
    """解析文件路径。

    展开用户目录符号（~），将相对路径基于 base 解析为绝对路径。

    Args:
        base: 基准路径（通常为工作目录）
        candidate: 候选路径字符串

    Returns:
        解析后的绝对路径
    """
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()
