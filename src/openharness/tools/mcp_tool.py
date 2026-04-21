"""MCP 工具适配器。

本模块提供 McpToolAdapter，将 MCP（Model Context Protocol）服务器上的工具
适配为 OpenHarness 标准工具接口。核心功能：
- 自动将 MCP 工具的 JSON Schema 转换为 Pydantic BaseModel 输入模型
- 工具名称格式为 mcp__<server>__<tool>，确保命名空间隔离
- 通过 McpClientManager 调用远程 MCP 工具
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, create_model

from openharness.mcp.client import McpClientManager, McpServerNotConnectedError
from openharness.mcp.types import McpToolInfo
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class McpToolAdapter(BaseTool):
    """将单个 MCP 工具适配为 OpenHarness 标准工具接口的适配器。

    自动将 MCP 工具名称转换为 mcp__<server>__<tool> 格式，
    将 JSON Schema 输入转换为 Pydantic 模型。
    """

    def __init__(self, manager: McpClientManager, tool_info: McpToolInfo) -> None:
        """初始化 MCP 工具适配器。

        根据服务器名和工具名生成规范化名称，从 JSON Schema 生成输入模型。

        Args:
            manager: MCP 客户端管理器
            tool_info: MCP 工具信息
        """
        self._manager = manager
        self._tool_info = tool_info
        server_segment = _sanitize_tool_segment(tool_info.server_name)
        tool_segment = _sanitize_tool_segment(tool_info.name)
        self.name = f"mcp__{server_segment}__{tool_segment}"
        self.description = tool_info.description or f"MCP tool {tool_info.name}"
        self.input_model = _input_model_from_schema(self.name, tool_info.input_schema)

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        """执行 MCP 工具调用。

        通过 McpClientManager 调用远程 MCP 服务器上的工具。

        Args:
            arguments: 经过输入模型验证的参数
            context: 工具执行上下文（未使用）

        Returns:
            MCP 工具的输出结果
        """
        del context
        try:
            output = await self._manager.call_tool(
                self._tool_info.server_name,
                self._tool_info.name,
                arguments.model_dump(mode="json", exclude_none=True),
            )
        except McpServerNotConnectedError as exc:
            return ToolResult(output=str(exc), is_error=True)
        return ToolResult(output=output)


_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _input_model_from_schema(tool_name: str, schema: dict[str, object]) -> type[BaseModel]:
    """从 JSON Schema 动态创建 Pydantic 输入模型。

    解析 JSON Schema 的 properties 和 required 字段，
    映射 JSON 类型到 Python 类型，使用 pydantic.create_model 动态生成模型。

    Args:
        tool_name: 工具名称（用于生成模型类名）
        schema: MCP 工具的 JSON Schema 字典

    Returns:
        动态生成的 Pydantic BaseModel 子类
    """
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return create_model(f"{tool_name.title()}Input")

    fields = {}
    required = set(schema.get("required", [])) if isinstance(schema.get("required", []), list) else set()
    for key in properties:
        prop = properties[key] if isinstance(properties[key], dict) else {}
        py_type = _JSON_TYPE_MAP.get(str(prop.get("type", "")), object)
        if key in required:
            fields[key] = (py_type, Field(default=...))
        else:
            fields[key] = (py_type | None, Field(default=None))
    return create_model(f"{tool_name.title().replace('-', '_')}Input", **fields)


def _sanitize_tool_segment(value: str) -> str:
    """清洗工具名称段，确保符合命名规范。

    将非字母数字和下划线连字符的字符替换为下划线，
    确保首字符为字母（否则添加 mcp_ 前缀）。

    Args:
        value: 原始工具名称段

    Returns:
        清洗后的名称段
    """
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    if not sanitized:
        return "tool"
    if not sanitized[0].isalpha():
        return f"mcp_{sanitized}"
    return sanitized
