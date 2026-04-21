"""MCP 资源列表工具。

本模块提供 ListMcpResourcesTool，用于列出所有已连接 MCP 服务器上
可用的资源。每个资源以 服务器名:URI 描述 的格式展示。
该工具为只读工具。
"""

from __future__ import annotations

from pydantic import BaseModel

from openharness.mcp.client import McpClientManager
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class ListMcpResourcesToolInput(BaseModel):
    """MCP 资源列表工具的输入参数（无额外参数）。"""


class ListMcpResourcesTool(BaseTool):
    """列出 MCP 服务器可用资源的工具。

    展示所有已连接 MCP 服务器上的资源列表。
    """

    name = "list_mcp_resources"
    description = "List MCP resources available from connected servers."
    input_model = ListMcpResourcesToolInput

    def __init__(self, manager: McpClientManager) -> None:
        """初始化 MCP 资源列表工具。

        Args:
            manager: MCP 客户端管理器实例
        """

    def is_read_only(self, arguments: ListMcpResourcesToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""

    async def execute(self, arguments: ListMcpResourcesToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行 MCP 资源列表查询。

        Args:
            arguments: 输入参数（无额外参数）
            context: 工具执行上下文（未使用）

        Returns:
            MCP 资源列表或 "(no MCP resources)"
        """
        del arguments, context
        resources = self._manager.list_resources()
        if not resources:
            return ToolResult(output="(no MCP resources)")
        return ToolResult(
            output="\n".join(f"{item.server_name}:{item.uri} {item.description}".strip() for item in resources)
        )
