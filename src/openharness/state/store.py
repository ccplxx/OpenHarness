"""Observable application state store."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from openharness.state.app_state import AppState


Listener = Callable[[AppState], None]  # 一个接受 AppState 参数、无返回值的回调函数


class AppStateStore:
    """Very small observable state store.
    只负责状态管理和通知，不关心监听器的具体实现
    """

    def __init__(self, initial_state: AppState) -> None:
        self._state = initial_state
        self._listeners: list[Listener] = []

    def get(self) -> AppState:
        """Return the current state snapshot."""
        return self._state

    def set(self, **updates) -> AppState:
        """Update the state and notify listeners."""
        self._state = replace(self._state, **updates)
        # 状态变更时自动通知所有订阅者
        for listener in list(self._listeners):
            listener(self._state)
        return self._state

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        """Register a listener and return an unsubscribe callback.
        闭包设计：subscribe() 返回一个闭包函数用于取消订阅
        自动清理：调用返回的 _unsubscribe() 会从监听器列表中移除该监听器
        防御性编程：取消订阅前检查监听器是否仍在列表中 (if listener in self._listeners)
        """
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsubscribe


# 订阅者示例
"""
class UIComponent:
    def __init__(self, store: AppStateStore):
        self.store = store
        self.unsubscribe = None
    
    def mount(self):
        # 组件挂载时订阅
        self.unsubscribe = self.store.subscribe(self._on_state_change)
    
    def unmount(self):
        # 组件卸载时取消订阅
        if self.unsubscribe:
            self.unsubscribe()
            self.unsubscribe = None
    
    def _on_state_change(self, state: AppState):
        # 响应状态变化
        self.update_ui(state.theme)

"""