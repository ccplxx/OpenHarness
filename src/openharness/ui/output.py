"""终端渲染辅助模块，提供 Rich Markdown、语法高亮和加载动画。

本模块实现 OutputRenderer 类，负责将引擎流式事件渲染到终端，
包括：增量文本流式输出、Markdown 格式重渲染、代码语法高亮、
工具执行面板（Bash/Read/Edit/Grep 各有定制样式）、
压缩进度指示、思考动画（spinner）以及状态栏显示。
"""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax

from openharness.engine.stream_events import (
    AssistantTextDelta,
    AssistantTurnComplete,
    CompactProgressEvent,
    StreamEvent,
    ToolExecutionCompleted,
    ToolExecutionStarted,
)


class OutputRenderer:
    """使用 Rich 格式化将模型和工具事件渲染到终端。

    支持两种显示风格：default（完整格式）和 minimal（精简格式）。
    核心功能包括：
    - 增量文本流式输出，轮次结束后对含 Markdown 的文本重渲染
    - 工具执行状态展示（启动/完成/错误面板）
    - Bash 命令面板、代码文件语法高亮、Grep 结果高亮
    - 压缩进度阶段提示
    - 思考中动画（spinner）
    - 状态栏（模型/token/权限模式）
    """

    def __init__(self, style_name: str = "default") -> None:
        self.console = Console()
        self._assistant_line_open = False
        self._assistant_buffer = ""
        self._style_name = style_name
        self._spinner_status = None
        self._last_tool_input: dict | None = None

    def set_style(self, style_name: str) -> None:
        self._style_name = style_name

    def show_thinking(self) -> None:
        """在首个助手 token 到达前显示"思考中"加载动画。

        minimal 风格下不显示动画。若已有动画在运行则跳过。
        """
        if self._spinner_status is not None:
            return
        if self._style_name == "minimal":
            return
        self._spinner_status = self.console.status(
            "[cyan]Thinking...[/cyan]", spinner="dots"
        )
        self._spinner_status.start()

    def start_assistant_turn(self) -> None:
        """开始新的助手轮次输出。

        停止思考动画，输出轮次起始标记（default 为绿色圆点，minimal 为 "a> "），
        清空助手缓冲区。
        """
        self._stop_spinner()  # Stop the thinking spinner when output starts
        if self._assistant_line_open:
            self.console.print()
        self._assistant_buffer = ""
        self._assistant_line_open = True
        if self._style_name == "minimal":
            self.console.print("a> ", end="", style="green")
        else:
            self.console.print("[green bold]\u23fa[/green bold] ", end="")

    def render_event(self, event: StreamEvent) -> None:
        """渲染单个流式事件到终端。

        根据事件类型分发处理：
        - AssistantTextDelta：流式输出文本并缓存到缓冲区
        - AssistantTurnComplete：结束轮次，若缓冲区含 Markdown 则重渲染
        - CompactProgressEvent：显示压缩进度阶段提示
        - ToolExecutionStarted：显示工具调用摘要和加载动画
        - ToolExecutionCompleted：根据工具类型渲染定制输出面板
        """
        if isinstance(event, AssistantTextDelta):
            self._assistant_buffer += event.text
            # Stream raw text for responsiveness
            self.console.print(event.text, end="", markup=False, highlight=False)
            return

        if isinstance(event, AssistantTurnComplete):
            if self._assistant_line_open:
                self.console.print()
                # Re-render with markdown if the buffer contains markdown indicators
                if _has_markdown(self._assistant_buffer) and self._style_name != "minimal":
                    self.console.print()
                    self.console.print(Markdown(self._assistant_buffer.strip()))
                self._assistant_line_open = False
                self._assistant_buffer = ""
            return

        if isinstance(event, CompactProgressEvent):
            self._stop_spinner()
            if event.message:
                label = event.message
            elif event.phase == "hooks_start":
                label = (
                    "Preparing retry compaction..."
                    if event.trigger == "reactive"
                    else "Preparing conversation compaction..."
                )
            elif event.phase == "session_memory_start":
                label = "Condensing earlier conversation..."
            elif event.phase == "session_memory_end":
                label = "Conversation condensed."
            elif event.phase == "context_collapse_start":
                label = "Collapsing oversized context..."
            elif event.phase == "context_collapse_end":
                label = "Context collapse complete."
            elif event.phase == "compact_start":
                label = (
                    "Context is too large. Compacting and retrying..."
                    if event.trigger == "reactive"
                    else "Compacting conversation memory..."
                )
            elif event.phase == "compact_retry":
                label = "Retrying compaction..."
            elif event.phase == "compact_end":
                label = "Compaction complete."
            elif event.phase == "compact_failed":
                label = "Compaction failed."
            else:
                label = "Compacting..."
            self.console.print(f"[yellow]\u2139 {label}[/yellow]")
            return

        if isinstance(event, ToolExecutionStarted):
            self._stop_spinner()
            if self._assistant_line_open:
                self.console.print()
                self._assistant_line_open = False
            tool_name = event.tool_name
            summary = _summarize_tool_input(tool_name, event.tool_input)
            self._last_tool_input = event.tool_input
            if self._style_name == "minimal":
                self.console.print(f"  > {tool_name} {summary}")
            else:
                self.console.print(
                    f"  [bold cyan]\u23f5 {tool_name}[/bold cyan] [dim]{summary}[/dim]"
                )
                self._start_spinner(tool_name)
            return

        if isinstance(event, ToolExecutionCompleted):
            self._stop_spinner()
            tool_name = event.tool_name
            output = event.output
            is_error = event.is_error
            if self._style_name == "minimal":
                self.console.print(f"    {output}")
                return
            if is_error:
                self.console.print(Panel(output, title=f"{tool_name} error", border_style="red", padding=(0, 1)))
                return
            # Render tool output based on tool type
            tool_input = getattr(event, "tool_input", None) or self._last_tool_input
            self._render_tool_output(tool_name, tool_input, output)

    def print_system(self, message: str) -> None:
        """打印系统消息（黄色 ⛸ 前缀，minimal 风格下无装饰）。"""
        self._stop_spinner()
        if self._assistant_line_open:
            self.console.print()
            self._assistant_line_open = False
        if self._style_name == "minimal":
            self.console.print(message)
        else:
            self.console.print(f"[yellow]\u2139 {message}[/yellow]")

    def print_status_line(
        self,
        *,
        model: str = "unknown",
        input_tokens: int = 0,
        output_tokens: int = 0,
        permission_mode: str = "default",
    ) -> None:
        """在每轮结束后打印紧凑的状态栏，包含模型、token 用量和权限模式。"""
        parts = [f"[cyan]model: {model}[/cyan]"]
        if input_tokens > 0 or output_tokens > 0:
            down = "\u2193"
            up = "\u2191"
            parts.append(f"tokens: {_fmt_num(input_tokens)}{down} {_fmt_num(output_tokens)}{up}")
        parts.append(f"mode: {permission_mode}")
        sep = " \u2502 "
        line = sep.join(parts)
        self.console.print(f"[dim]{line}[/dim]")

    def clear(self) -> None:
        self.console.clear()

    def _start_spinner(self, tool_name: str) -> None:
        """启动工具执行中的加载动画（minimal 风格下跳过）。"""
        if self._style_name == "minimal":
            return
        self._spinner_status = self.console.status(f"Running {tool_name}...", spinner="dots")
        self._spinner_status.start()

    def _stop_spinner(self) -> None:
        """停止正在运行的加载动画。"""
        if self._spinner_status is not None:
            self._spinner_status.stop()
            self._spinner_status = None

    def _render_tool_output(self, tool_name: str, tool_input: dict | None, output: str) -> None:
        """根据工具类型定制渲染工具输出。

        - Bash：面板显示，标题为命令
        - Read/FileRead：根据文件扩展名语法高亮
        - Edit/FileEdit：绿色面板显示编辑结果
        - Grep：青色面板显示搜索结果
        - 其他：截断的暗色文本，超过15行时折叠
        """
        lower = tool_name.lower()
        # Bash: show in a panel
        if lower == "bash":
            cmd = (tool_input or {}).get("command", "")
            title = f"$ {cmd[:80]}" if cmd else "Bash"
            self.console.print(Panel(output[:2000], title=title, border_style="dim", padding=(0, 1)))
            return
        # Read/FileRead: syntax highlight by file extension
        if lower in ("read", "fileread", "file_read"):
            file_path = str((tool_input or {}).get("file_path", ""))
            ext = file_path.rsplit(".", 1)[-1] if "." in file_path else ""
            lexer = _ext_to_lexer(ext)
            if lexer and len(output) < 5000:
                self.console.print(Syntax(output, lexer, theme="monokai", line_numbers=True, word_wrap=True))
            else:
                self.console.print(Panel(output[:2000], title=file_path, border_style="dim", padding=(0, 1)))
            return
        # Edit/FileEdit: show as diff-style
        if lower in ("edit", "fileedit", "file_edit"):
            file_path = str((tool_input or {}).get("file_path", ""))
            self.console.print(Panel(output[:2000], title=f"Edit: {file_path}", border_style="green", padding=(0, 1)))
            return
        # Grep: highlight results
        if lower in ("grep", "greptool"):
            self.console.print(Panel(output[:2000], title="Search results", border_style="cyan", padding=(0, 1)))
            return
        # Default: dimmed text with truncation
        lines = output.split("\n")
        if len(lines) > 15:
            display = "\n".join(lines[:12]) + f"\n... ({len(lines) - 12} more lines)"
        else:
            display = output
        self.console.print(f"    [dim]{display}[/dim]")


def _has_markdown(text: str) -> bool:
    """检查文本是否可能包含 Markdown 格式标记。

    通过检测常见的 Markdown 指示符（代码块、标题、列表、加粗等）
    来判断是否需要在轮次结束后用 Rich Markdown 重渲染。
    """
    indicators = ["```", "## ", "### ", "- ", "* ", "1. ", "**", "__", "> "]
    return any(ind in text for ind in indicators)


def _summarize_tool_input(tool_name: str, tool_input: dict | None) -> str:
    """生成工具输入的简短摘要用于行内显示。

    针对不同工具提取关键参数：Bash 提取命令、Read/Write/Edit 提取文件路径、
    Grep 提取搜索模式、Glob 提取匹配模式，其他工具取首个键值对。
    """
    if not tool_input:
        return ""
    lower = tool_name.lower()
    if lower == "bash" and "command" in tool_input:
        return str(tool_input["command"])[:120]
    if lower in ("read", "fileread", "file_read") and "file_path" in tool_input:
        return str(tool_input["file_path"])
    if lower in ("write", "filewrite", "file_write") and "file_path" in tool_input:
        return str(tool_input["file_path"])
    if lower in ("edit", "fileedit", "file_edit") and "file_path" in tool_input:
        return str(tool_input["file_path"])
    if lower in ("grep", "greptool") and "pattern" in tool_input:
        return f"/{tool_input['pattern']}/"
    if lower in ("glob", "globtool") and "pattern" in tool_input:
        return str(tool_input["pattern"])
    entries = list(tool_input.items())
    if entries:
        k, v = entries[0]
        return f"{k}={str(v)[:60]}"
    return ""


def _ext_to_lexer(ext: str) -> str | None:
    """将文件扩展名映射为 Pygments/Rich 语法高亮器的词法分析器名称。

    支持 Python、JavaScript、TypeScript、Rust、Go、Java、C/C++、C#、
    Shell、JSON、YAML、TOML、XML、HTML、CSS、SQL、Markdown 等常见语言。
    .txt 扩展名返回 None（不进行语法高亮）。
    """
    mapping = {
        "py": "python", "js": "javascript", "ts": "typescript", "tsx": "tsx",
        "jsx": "jsx", "rs": "rust", "go": "go", "rb": "ruby", "java": "java",
        "c": "c", "cpp": "cpp", "h": "c", "hpp": "cpp", "cs": "csharp",
        "sh": "bash", "bash": "bash", "zsh": "bash", "json": "json",
        "yaml": "yaml", "yml": "yaml", "toml": "toml", "xml": "xml",
        "html": "html", "css": "css", "sql": "sql", "md": "markdown",
        "txt": None,
    }
    return mapping.get(ext.lower())


def _fmt_num(n: int) -> str:
    """格式化数字，1000 及以上显示为 "x.xk" 形式。"""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
