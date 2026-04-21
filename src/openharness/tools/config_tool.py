"""OpenHarness 配置读写工具。

本模块提供 ConfigTool，用于读取和修改 OpenHarness 的运行时配置。
支持两种操作：
- show：以 JSON 格式展示当前所有配置
- set：设置指定配置键的值

配置通过 load_settings/save_settings 进行持久化存储。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.config.settings import load_settings, save_settings
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class ConfigToolInput(BaseModel):
    """配置读写工具的输入参数。

    Attributes:
        action: 操作类型：show（展示配置）或 set（设置配置）
        key: 要设置的配置键名（set 操作时必填）
        value: 要设置的配置值（set 操作时必填）
    """

    action: str = Field(default="show", description="show or set")
    key: str | None = Field(default=None)
    value: str | None = Field(default=None)


class ConfigTool(BaseTool):
    """读取或更新 OpenHarness 运行时配置的工具。

    支持查看完整配置和设置单个配置键值。
    """

    name = "config"
    description = "Read or update OpenHarness settings."
    input_model = ConfigToolInput

    async def execute(self, arguments: ConfigToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行配置读写操作。

        Args:
            arguments: 包含操作类型和键值的输入参数
            context: 工具执行上下文（未使用）

        Returns:
            配置 JSON 字符串（show）或更新确认信息（set）
        """
        del context
        settings = load_settings()
        if arguments.action == "show":
            return ToolResult(output=settings.model_dump_json(indent=2))
        if arguments.action == "set" and arguments.key and arguments.value is not None:
            if not hasattr(settings, arguments.key):
                return ToolResult(output=f"Unknown config key: {arguments.key}", is_error=True)
            setattr(settings, arguments.key, arguments.value)
            save_settings(settings)
            return ToolResult(output=f"Updated {arguments.key}")
        return ToolResult(output="Usage: action=show or action=set with key/value", is_error=True)
