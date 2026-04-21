"""技能（Skill）内容读取工具。

本模块提供 SkillTool，用于按名称读取已加载的技能内容。
技能来源包括内置技能、用户自定义技能和插件技能。
搜索时支持原始名称、小写名称和首字母大写名称的匹配。
该工具为只读工具。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from openharness.skills import load_skill_registry
from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult


class SkillToolInput(BaseModel):
    """技能读取工具的输入参数。

    Attributes:
        name: 技能名称
    """

    name: str = Field(description="Skill name")


class SkillTool(BaseTool):
    """读取已加载技能内容的工具。

    搜索时支持原始名称、小写名称和首字母大写名称的匹配。
    """

    name = "skill"
    description = "Read a bundled, user, or plugin skill by name."
    input_model = SkillToolInput

    def is_read_only(self, arguments: SkillToolInput) -> bool:
        """该工具为只读，不会修改任何状态。"""

    async def execute(self, arguments: SkillToolInput, context: ToolExecutionContext) -> ToolResult:
        """执行技能内容读取。

        加载技能注册表，按名称查找技能并返回其内容。

        Args:
            arguments: 包含技能名称的输入参数
            context: 工具执行上下文

        Returns:
            技能内容文本或未找到错误
        """
        registry = load_skill_registry(
            context.cwd,
            extra_skill_dirs=context.metadata.get("extra_skill_dirs"),
            extra_plugin_roots=context.metadata.get("extra_plugin_roots"),
        )
        skill = registry.get(arguments.name) or registry.get(arguments.name.lower()) or registry.get(arguments.name.title())
        if skill is None:
            return ToolResult(output=f"Skill not found: {arguments.name}", is_error=True)
        return ToolResult(output=skill.content)
