"""配置系统模块（openharness.config）

本模块为 OpenHarness 提供统一的配置管理能力，包括设置项的加载与持久化、
文件路径解析以及 API 密钥处理。模块将分散在子模块中的核心公共 API 重新导出，
使得外部代码可以通过 ``from openharness.config import load_settings`` 等
简洁方式访问。

主要导出：
    - Settings：主设置模型，包含 API 配置、行为选项、UI 偏好等所有配置项。
    - ProviderProfile：命名的工作流配置文件，描述 API 提供商连接方式。
    - load_settings：从配置文件加载设置，合并环境变量与默认值。
    - save_settings：将设置持久化写入配置文件。
    - get_config_dir / get_config_file_path / get_data_dir / get_logs_dir：
      路径解析函数，遵循 XDG 风格约定。
    - default_provider_profiles：内置提供商配置目录。
    - auth_source_provider_name / default_auth_source_for_provider：
      认证源与提供商名称的映射工具。

子模块：
    - paths：配置与数据目录的路径解析。
    - schema：兼容性通道配置模型。
    - settings：主设置模型与加载/保存逻辑。
"""

from openharness.config.paths import (
    get_config_dir,
    get_config_file_path,
    get_data_dir,
    get_logs_dir,
)
from openharness.config.settings import (
    ProviderProfile,
    Settings,
    auth_source_provider_name,
    default_auth_source_for_provider,
    default_provider_profiles,
    load_settings,
    save_settings,
)

__all__ = [
    "ProviderProfile",
    "Settings",
    "auth_source_provider_name",
    "default_auth_source_for_provider",
    "default_provider_profiles",
    "get_config_dir",
    "get_config_file_path",
    "get_data_dir",
    "get_logs_dir",
    "load_settings",
    "save_settings",
]
