"""轻量级 Python 代码智能工具。

本模块提供 LspTool，基于 LSP（Language Server Protocol）为 Python 工作区
提供代码智能功能，包括：
- document_symbol：列出文件中的符号（函数、类等）
- workspace_symbol：在整个工作区搜索符号
- go_to_definition：跳转到符号定义位置
- find_references：查找符号的所有引用
- hover：获取符号的类型签名和文档字符串

仅支持 .py 文件。该工具为只读工具。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from openharness.services.lsp import (
    find_references,
    go_to_definition,
    hover,
    list_document_symbols,
    workspace_symbol_search,
)
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class LspToolInput(BaseModel):
    """代码智能查询工具的输入参数。

    Attributes:
        operation: 代码智能操作类型
        file_path: 源文件路径（文件级操作必需）
        symbol: 显式符号名称（用于定位查找）
        line: 1-based 行号（用于基于位置的查找）
        character: 1-based 字符偏移（用于基于位置的查找）
        query: 子串查询（workspace_symbol 操作必需）
    """

    operation: Literal[
        "document_symbol",
        "workspace_symbol",
        "go_to_definition",
        "find_references",
        "hover",
    ] = Field(description="The code intelligence operation to perform")
    file_path: str | None = Field(default=None, description="Path to the source file for file-based operations")
    symbol: str | None = Field(default=None, description="Explicit symbol name to look up")
    line: int | None = Field(default=None, ge=1, description="1-based line number for position-based lookups")
    character: int | None = Field(default=None, ge=1, description="1-based character offset for position-based lookups")
    query: str | None = Field(default=None, description="Substring query for workspace_symbol")

    @model_validator(mode="after")
    def validate_arguments(self) -> "LspToolInput":
        """验证输入参数的完整性。

        workspace_symbol 操作需要 query 参数；
        其他操作需要 file_path 参数；
        document_symbol 以外的操作需要 symbol 或 line 参数。
        """
        if self.operation == "workspace_symbol":
            if not self.query:
                raise ValueError("workspace_symbol requires query")
            return self
        if not self.file_path:
            raise ValueError(f"{self.operation} requires file_path")
        if self.operation == "document_symbol":
            return self
        if not self.symbol and self.line is None:
            raise ValueError(f"{self.operation} requires symbol or line")
        return self


class LspTool(BaseTool):
    """Python 源文件的只读代码智能工具。

    基于 LSP 提供符号列表、定义跳转、引用查找和悬停信息功能。
    """

    name = "lsp"
    description = (
        "Inspect Python code symbols, definitions, references, and hover information "
        "across the current workspace."
    )
    input_model = LspToolInput

    def is_read_only(self, arguments: LspToolInput) -> bool:
        """该工具为只读，不会修改任何文件。"""

    async def execute(self, arguments: LspToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行代码智能查询。

        根据 operation 类型调用不同的 LSP 服务函数。

        Args:
            arguments: 包含操作类型和查询参数的输入
            context: 工具执行上下文

        Returns:
            格式化的代码智能查询结果
        """
        root = context.cwd.resolve()
        if arguments.operation == "workspace_symbol":
            results = workspace_symbol_search(root, arguments.query or "")
            return ToolResult(output=_format_symbol_locations(results, root))

        assert arguments.file_path is not None  # validated above
        file_path = _resolve_path(root, arguments.file_path)
        if not file_path.exists():
            return ToolResult(output=f"File not found: {file_path}", is_error=True)
        if file_path.suffix != ".py":
            return ToolResult(output="The lsp tool currently supports Python files only.", is_error=True)

        if arguments.operation == "document_symbol":
            return ToolResult(output=_format_symbol_locations(list_document_symbols(file_path), root))

        if arguments.operation == "go_to_definition":
            results = go_to_definition(
                root=root,
                file_path=file_path,
                symbol=arguments.symbol,
                line=arguments.line,
                character=arguments.character,
            )
            return ToolResult(output=_format_symbol_locations(results, root))

        if arguments.operation == "find_references":
            results = find_references(
                root=root,
                file_path=file_path,
                symbol=arguments.symbol,
                line=arguments.line,
                character=arguments.character,
            )
            return ToolResult(output=_format_references(results, root))

        result = hover(
            root=root,
            file_path=file_path,
            symbol=arguments.symbol,
            line=arguments.line,
            character=arguments.character,
        )
        if result is None:
            return ToolResult(output="(no hover result)")
        parts = [
            f"{result.kind} {result.name}",
            f"path: {_display_path(result.path, root)}:{result.line}:{result.character}",
        ]
        if result.signature:
            parts.append(f"signature: {result.signature}")
        if result.docstring:
            parts.append(f"docstring: {result.docstring.strip()}")
        return ToolResult(output="\n".join(parts))


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


def _display_path(path: Path, root: Path) -> str:
    """格式化显示路径。

    尝试返回相对路径，失败则返回绝对路径。

    Args:
        path: 目标路径
        root: 工作区根目录

    Returns:
        相对路径或绝对路径字符串
    """
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _format_symbol_locations(results, root: Path) -> str:
    """格式化符号位置信息列表。

    每个符号显示类型、名称、文件路径和位置，以及可选的签名和文档字符串。

    Args:
        results: LSP 返回的符号位置列表
        root: 工作区根目录

    Returns:
        格式化的符号位置文本
    """
    if not results:
        return "(no results)"
    lines = []
    for item in results:
        lines.append(
            f"{item.kind} {item.name} - {_display_path(item.path, root)}:{item.line}:{item.character}"
        )
        if item.signature:
            lines.append(f"  signature: {item.signature}")
        if item.docstring:
            lines.append(f"  docstring: {item.docstring.strip()}")
    return "\n".join(lines)


def _format_references(results: list[tuple[Path, int, str]], root: Path) -> str:
    """格式化引用查找结果。

    每个引用显示为 文件路径:行号:行内容 格式。

    Args:
        results: 引用结果列表，每项为 (路径, 行号, 行文本) 元组
        root: 工作区根目录

    Returns:
        格式化的引用文本
    """
    if not results:
        return "(no results)"
    return "\n".join(f"{_display_path(path, root)}:{line}:{text}" for path, line, text in results)

