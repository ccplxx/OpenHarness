"""Minimal application state model."""

from __future__ import annotations

from dataclasses import dataclass, field

# AppState 使用 @dataclass 定义，AppStateStore.set() 使用 dataclasses.replace() 创建新实例
# 确保每次状态更新都产生新对象，避免直接修改现有状态导致的副作用
# 监听器接收的是更新后的完整状态快照，而非增量变更
@dataclass
class AppState:
    """Shared mutable UI/session state."""

    model: str
    permission_mode: str
    theme: str
    cwd: str = "."
    provider: str = "unknown"
    auth_status: str = "missing"
    base_url: str = ""
    vim_enabled: bool = False
    voice_enabled: bool = False
    voice_available: bool = False
    voice_reason: str = ""
    fast_mode: bool = False
    effort: str = "medium"
    passes: int = 1
    mcp_connected: int = 0
    mcp_failed: int = 0
    bridge_sessions: int = 0
    output_style: str = "default"
    keybindings: dict[str, str] = field(default_factory=dict)
