"""项目 TODO 文件维护工具。

本模块提供 TodoWriteTool，用于在 Markdown 格式的 TODO 文件中
添加或更新待办事项。支持三种操作：
- 添加新的未勾选项（- [ ] item）
- 将已有未勾选项标记为已完成（- [ ] → - [x]）
- 项目已在目标状态时不做变更
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class TodoWriteToolInput(BaseModel):
    """TODO 写入工具的输入参数。

    Attributes:
        item: TODO 项文本
        checked: 是否已勾选，默认为 False
        path: TODO 文件路径，默认为 TODO.md
    """

    item: str = Field(description="TODO item text")
    checked: bool = Field(default=False)
    path: str = Field(default="TODO.md")


class TodoWriteTool(BaseTool):
    """在 Markdown TODO 文件中添加或更新待办事项的工具。

    支持添加新项、勾选已有项和忽略已存在的项。
    """

    name = "todo_write"
    description = "Add a new TODO item or mark an existing one as done in a markdown checklist file."
    input_model = TodoWriteToolInput

    async def execute(self, arguments: TodoWriteToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行 TODO 项写入操作。

        读取已有文件，根据当前状态和新状态决定是添加、勾选还是不做变更。

        Args:
            arguments: 包含 TODO 项文本和状态的输入参数
            context: 工具执行上下文

        Returns:
            更新确认信息
        """
        path = Path(context.cwd) / arguments.path
        existing = path.read_text(encoding="utf-8") if path.exists() else "# TODO\n"

        unchecked_line = f"- [ ] {arguments.item}"
        checked_line = f"- [x] {arguments.item}"
        target_line = checked_line if arguments.checked else unchecked_line

        if unchecked_line in existing and arguments.checked:
            # Mark existing unchecked item as done (in-place update)
            updated = existing.replace(unchecked_line, checked_line, 1)
        elif target_line in existing:
            # Item already in desired state — no-op
            return ToolResult(output=f"No change needed in {path}")
        else:
            # New item — append
            updated = existing.rstrip() + f"\n{target_line}\n"

        path.write_text(updated, encoding="utf-8")
        return ToolResult(output=f"Updated {path}")
