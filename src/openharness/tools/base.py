"""工具抽象基类与注册表。

本模块定义了 OpenHarness 工具系统的核心抽象：
- ToolExecutionContext：工具执行上下文，携带工作目录和元数据
- ToolResult：统一的工具执行结果，包含输出文本、错误标志和元数据
- BaseTool：所有工具的抽象基类，定义工具名称、描述、输入模型和执行接口
- ToolRegistry：工具注册表，管理工具名称到实现的映射，支持 API Schema 导出
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel


@dataclass
class ToolExecutionContext:
    """工具执行的共享上下文。

    Attributes:
        cwd: 当前工作目录路径
        metadata: 执行元数据字典，可传递回调函数、MCP 管理器等
    """

    cwd: Path
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """标准化的工具执行结果。

    Attributes:
        output: 工具输出的文本内容
        is_error: 是否为错误结果，默认为 False
        metadata: 结果元数据，如返回码、超时标志等
    """

    output: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseTool(ABC):
    """所有 OpenHarness 工具的抽象基类。

    子类必须定义 name、description、input_model 类属性，
    并实现 execute 异步方法。

    Attributes:
        name: 工具名称，用于注册和调用
        description: 工具描述，供 LLM 选择工具时参考
        input_model: Pydantic BaseModel 子类，定义工具输入参数
    """

    name: str
    description: str
    input_model: type[BaseModel]

    @abstractmethod
    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        """执行工具的核心逻辑。

        Args:
            arguments: 经过 input_model 验证的输入参数
            context: 工具执行上下文

        Returns:
            标准化的 ToolResult
        """

    def is_read_only(self, arguments: BaseModel) -> bool:
        """判断此次调用是否为只读操作。

        默认返回 False，只读工具应覆盖此方法返回 True。

        Args:
            arguments: 工具输入参数

        Returns:
            若为只读返回 True，否则返回 False
        """
        del arguments
        return False

    def to_api_schema(self) -> dict[str, Any]:
        """返回 Anthropic Messages API 所需的工具 Schema。

        包含工具名称、描述和基于 input_model 生成的 JSON Schema。

        Returns:
            符合 Anthropic API 格式的工具描述字典
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_model.model_json_schema(),
        }


class ToolRegistry:
    """工具注册表，管理工具名称到实现的映射。

    提供工具注册、按名称查找、列出所有工具和导出 API Schema 等功能。
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """注册一个工具实例。

        Args:
            tool: 要注册的 BaseTool 实例
        """
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool | None:
        """按名称查找已注册的工具。

        Args:
            name: 工具名称

        Returns:
            匹配的 BaseTool 实例，若未找到返回 None
        """
        return self._tools.get(name)

    def list_tools(self) -> list[BaseTool]:
        """返回所有已注册的工具列表。"""
        return list(self._tools.values())

    def to_api_schema(self) -> list[dict[str, Any]]:
        """返回所有工具的 API Schema 列表。

        Returns:
            每个工具的 API 格式描述字典组成的列表
        """
        return [tool.to_api_schema() for tool in self._tools.values()]
