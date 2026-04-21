"""工具搜索工具。

本模块提供 ToolSearchTool，用于在当前可用的工具注册表中
按名称或描述搜索匹配的工具。搜索为大小写不敏感的子串匹配。
该工具为只读工具。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class ToolSearchToolInput(BaseModel):
    """工具搜索工具的输入参数。

    Attributes:
        query: 在工具名称和描述中搜索的子串
    """

    query: str = Field(description="Substring to search in tool names and descriptions")


class ToolSearchTool(BaseTool):
    """搜索工具注册表内容的工具。

    按名称或描述进行大小写不敏感的子串匹配。
    """

    name = "tool_search"
    description = "Search the available tool list by name or description."
    input_model = ToolSearchToolInput

    def is_read_only(self, arguments: ToolSearchToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""

    async def execute(self, arguments: ToolSearchToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行工具搜索。

        从上下文元数据获取工具注册表，搜索匹配的工具。

        Args:
            arguments: 包含搜索子串的输入参数
            context: 工具执行上下文

        Returns:
            匹配工具的名称和描述列表
        """
        registry = context.metadata.get("tool_registry") if hasattr(context, "metadata") else None
        if registry is None:
            return ToolResult(output="Tool registry context not available", is_error=True)
        query = arguments.query.lower()
        matches = [
            tool for tool in registry.list_tools()
            if query in tool.name.lower() or query in tool.description.lower()
        ]
        if not matches:
            return ToolResult(output="(no matches)")
        return ToolResult(output="\n".join(f"{tool.name}: {tool.description}" for tool in matches))
