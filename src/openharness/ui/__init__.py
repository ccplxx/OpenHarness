"""UI 模块导出。

本模块定义了 openharness.ui 包的公共 API，导出：
- run_repl：运行默认的交互式应用（React TUI）
- run_print_mode：运行非交互式打印模式
"""

from openharness.ui.app import run_repl, run_print_mode

__all__ = ["run_repl", "run_print_mode"]
