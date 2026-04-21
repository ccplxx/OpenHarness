"""文件锁辅助模块的向后兼容重导出。

实际实现位于 :mod:`openharness.utils.file_lock`，本模块保留以使现有调用方
（Swarm 邮箱、权限同步、外部插件）无需修改即可继续使用。
"""

from __future__ import annotations

from openharness.utils.file_lock import (
    SwarmLockError,
    SwarmLockUnavailableError,
    _exclusive_posix_lock,
    _exclusive_windows_lock,
    exclusive_file_lock,
)

__all__ = [
    "SwarmLockError",
    "SwarmLockUnavailableError",
    "_exclusive_posix_lock",
    "_exclusive_windows_lock",
    "exclusive_file_lock",
]
