"""协调者模块导出。

本模块定义了 openharness.coordinator 包的公共 API，导出：
- AgentDefinition：代理定义模型
- get_builtin_agent_definitions：获取内置代理定义列表
- TeamRecord / TeamRegistry：团队注册与管理
- get_team_registry：获取全局团队注册表单例
"""

from openharness.coordinator.agent_definitions import AgentDefinition, get_builtin_agent_definitions
from openharness.coordinator.coordinator_mode import TeamRecord, TeamRegistry, get_team_registry

__all__ = [
    "AgentDefinition",
    "TeamRecord",
    "TeamRegistry",
    "get_builtin_agent_definitions",
    "get_team_registry",
]
