"""Jupyter Notebook 编辑工具。

本模块提供 NotebookEditTool，用于编辑 Jupyter Notebook（.ipynb）文件的单元格。
无需依赖 nbformat 库，直接操作 Notebook JSON 结构。支持：
- 替换或追加单元格内容
- 创建新的 Notebook 文件
- 自动填充缺失的单元格
- 同时支持 code 和 markdown 类型单元格
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class NotebookEditToolInput(BaseModel):
    """Notebook 编辑工具的输入参数。

    Attributes:
        path: .ipynb 文件路径
        cell_index: 单元格索引（从 0 开始）
        new_source: 替换或追加到目标单元格的源代码
        cell_type: 单元格类型：code 或 markdown，默认 code
        mode: 编辑模式：replace（替换）或 append（追加），默认 replace
        create_if_missing: 文件不存在时是否创建，默认 True
    """

    path: str = Field(description="Path to the .ipynb file")
    cell_index: int = Field(description="Zero-based cell index", ge=0)
    new_source: str = Field(description="Replacement or appended source for the target cell")
    cell_type: Literal["code", "markdown"] = Field(default="code")
    mode: Literal["replace", "append"] = Field(default="replace")
    create_if_missing: bool = Field(default=True)


class NotebookEditTool(BaseTool):
    """编辑 Jupyter Notebook 单元格的工具。

    无需 nbformat 库，直接操作 Notebook JSON 结构。
    """

    name = "notebook_edit"
    description = "Create or edit a Jupyter notebook cell."
    input_model = NotebookEditToolInput

    async def execute(
        self,
        arguments: NotebookEditToolInput,
        context: ToolExecutionContext,
    ) -> ToolResult:
        """执行 Notebook 编辑操作。

        加载或创建 Notebook，定位或创建目标单元格，
        根据模式替换或追加内容后写回文件。

        Args:
            arguments: 包含路径、单元格索引和内容的输入参数
            context: 工具执行上下文

        Returns:
            编辑确认信息
        """
        path = _resolve_path(context.cwd, arguments.path)
        notebook = _load_notebook(path, create_if_missing=arguments.create_if_missing)
        if notebook is None:
            return ToolResult(output=f"Notebook not found: {path}", is_error=True)

        cells = notebook.setdefault("cells", [])
        while len(cells) <= arguments.cell_index:
            cells.append(_empty_cell(arguments.cell_type))

        cell = cells[arguments.cell_index]
        cell["cell_type"] = arguments.cell_type
        cell.setdefault("metadata", {})
        if arguments.cell_type == "code":
            cell.setdefault("outputs", [])
            cell.setdefault("execution_count", None)

        existing = _normalize_source(cell.get("source", ""))
        updated = arguments.new_source if arguments.mode == "replace" else f"{existing}{arguments.new_source}"
        cell["source"] = updated

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(notebook, indent=2) + "\n", encoding="utf-8")
        return ToolResult(output=f"Updated notebook cell {arguments.cell_index} in {path}")


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


def _load_notebook(path: Path, *, create_if_missing: bool) -> dict | None:
    """加载或创建 Notebook JSON 结构。

    如果文件存在则解析 JSON，不存在时根据 create_if_missing 参数
    创建空 Notebook 结构或返回 None。

    Args:
        path: Notebook 文件路径
        create_if_missing: 文件不存在时是否创建

    Returns:
        Notebook 字典结构，或 None（文件不存在且不创建）
    """
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    if not create_if_missing:
        return None
    return {
        "cells": [],
        "metadata": {"language_info": {"name": "python"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def _empty_cell(cell_type: str) -> dict:
    """创建空的 Notebook 单元格结构。

    Args:
        cell_type: 单元格类型（code 或 markdown）

    Returns:
        符合 Notebook JSON 规范的单元格字典
    """
    if cell_type == "markdown":
        return {"cell_type": "markdown", "metadata": {}, "source": ""}
    return {
        "cell_type": "code",
        "metadata": {},
        "source": "",
        "outputs": [],
        "execution_count": None,
    }


def _normalize_source(source: str | list[str]) -> str:
    """规范化单元格源代码为字符串。

    Notebook 的 source 字段可能是字符串或字符串列表，
    此函数将列表形式连接为单个字符串。

    Args:
        source: 原始源代码（字符串或字符串列表）

    Returns:
        规范化后的源代码字符串
    """
    if isinstance(source, list):
        return "".join(source)
    return str(source)
