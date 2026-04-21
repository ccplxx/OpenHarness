"""MCP 资源读取工具。

本模块提供 ReadMcpResourceTool，用于通过服务器名和 URI 从 MCP 服务器
读取资源内容。该工具为只读工具。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.mcp.client import McpClientManager, McpServerNotConnectedError
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class ReadMcpResourceToolInput(BaseModel):
    """MCP 资源读取工具的输入参数。

    Attributes:
        server: MCP 服务器名称
        uri: 资源 URI
    """

    server: str = Field(description="MCP server name")
    uri: str = Field(description="Resource URI")


class ReadMcpResourceTool(BaseTool):
    """从 MCP 服务器读取单个资源的工具。

    通过服务器名和 URI 获取资源内容。
    """

    name = "read_mcp_resource"
    description = "Read an MCP resource by server and URI."
    input_model = ReadMcpResourceToolInput

    def __init__(self, manager: McpClientManager) -> None:
        """初始化 MCP 资源读取工具。

        Args:
            manager: MCP 客户端管理器实例
        """

    def is_read_only(self, arguments: ReadMcpResourceToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""

    async def execute(self, arguments: ReadMcpResourceToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行 MCP 资源读取。

        通过 McpClientManager 读取指定服务器和 URI 的资源。

        Args:
            arguments: 包含服务器名和 URI 的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            资源内容文本
        """
        del context
        try:
            output = await self._manager.read_resource(arguments.server, arguments.uri)
        except McpServerNotConnectedError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=output)
