"""MCP 服务器认证配置工具。

本模块提供 McpAuthTool，用于为 MCP 服务器持久化认证配置。
支持三种认证模式：
- bearer：Bearer Token 认证
- header：自定义 HTTP 头认证
- env：环境变量注入认证

不同传输类型支持不同的认证模式：
- stdio 传输支持 env 和 bearer
- http/ws 传输支持 header 和 bearer

配置保存后会自动尝试重新连接活跃的 MCP 会话。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.config.settings import load_settings, save_settings
from openharness.mcp.types import McpHttpServerConfig, McpStdioServerConfig, McpWebSocketServerConfig
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class McpAuthToolInput(BaseModel):
    """MCP 认证配置工具的输入参数。

    Attributes:
        server_name: 已配置的 MCP 服务器名称
        mode: 认证模式：bearer、header 或 env
        value: 要持久化的密钥值
        key: 可选的 HTTP 头或环境变量键名覆盖
    """

    server_name: str = Field(description="Configured MCP server name")
    mode: str = Field(description="Auth mode: bearer, header, or env")
    value: str = Field(description="Secret value to persist")
    key: str | None = Field(default=None, description="Header or env key override")


class McpAuthTool(BaseTool):
    """为 MCP 服务器持久化认证设置的工具。

    配置保存后自动尝试重新连接活跃的 MCP 会话。
    """

    name = "mcp_auth"
    description = "Configure auth for an MCP server and reconnect active sessions when possible."
    input_model = McpAuthToolInput

    async def execute(self, arguments: McpAuthToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行 MCP 认证配置更新。

        根据服务器传输类型（stdio/http/ws）和认证模式，
        更新环境变量或 HTTP 头，保存设置并尝试重新连接。

        Args:
            arguments: 包含服务器名、认证模式和密钥值的输入参数
            context: 工具执行上下文

        Returns:
            配置保存确认信息或错误
        """
        settings = load_settings()
        mcp_manager = context.metadata.get("mcp_manager")
        config = settings.mcp_servers.get(arguments.server_name)
        if config is None and mcp_manager is not None:
            getter = getattr(mcp_manager, "get_server_config", None)
            if callable(getter):
                config = getter(arguments.server_name)
        if config is None:
            return ToolResult(output=f"Unknown MCP server: {arguments.server_name}", is_error=True)

        if isinstance(config, McpStdioServerConfig):
            if arguments.mode not in {"env", "bearer"}:
                return ToolResult(output="stdio MCP auth supports env or bearer modes", is_error=True)
            env_key = arguments.key or "MCP_AUTH_TOKEN"
            env = dict(config.env or {})
            env[env_key] = f"Bearer {arguments.value}" if arguments.mode == "bearer" else arguments.value
            updated = config.model_copy(update={"env": env})
        elif isinstance(config, (McpHttpServerConfig, McpWebSocketServerConfig)):
            if arguments.mode not in {"header", "bearer"}:
                return ToolResult(output="http/ws MCP auth supports header or bearer modes", is_error=True)
            header_key = arguments.key or "Authorization"
            headers = dict(config.headers)
            headers[header_key] = (
                f"Bearer {arguments.value}" if arguments.mode == "bearer" and header_key == "Authorization" else arguments.value
            )
            updated = config.model_copy(update={"headers": headers})
        else:
            return ToolResult(output="Unsupported MCP server config type", is_error=True)

        settings.mcp_servers[arguments.server_name] = updated
        save_settings(settings)

        if mcp_manager is not None:
            try:
                mcp_manager.update_server_config(arguments.server_name, updated)
                await mcp_manager.reconnect_all()
            except Exception as exc:  # pragma: no cover - defensive
                return ToolResult(
                    output=f"Saved MCP auth for {arguments.server_name}, but reconnect failed: {exc}",
                    is_error=True,
                )

        return ToolResult(output=f"Saved MCP auth for {arguments.server_name}")
