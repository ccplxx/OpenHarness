"""Slash command registry."""

from __future__ import annotations

from pathlib import Path
from dataclasses import dataclass
from openharness.engine.query_engine import QueryEngine
from typing import TYPE_CHECKING, Awaitable, Callable, Iterable
from openharness.services.session_backend import DEFAULT_SESSION_BACKEND, SessionBackend

if TYPE_CHECKING:
    from openharness.state import AppStateStore
    from openharness.tools.base import ToolRegistry

@dataclass
class CommandResult:
    """Result returned by a slash command."""

    message: str | None = None
    should_exit: bool = False
    clear_screen: bool = False
    replay_messages: list | None = None  # ConversationMessage list to replay in TUI
    continue_pending: bool = False
    continue_turns: int | None = None
    refresh_runtime: bool = False
    submit_prompt: str | None = None
    submit_model: str | None = None


@dataclass
class CommandContext:
    """Context available to command handlers."""

    engine: QueryEngine
    hooks_summary: str = ""
    mcp_summary: str = ""
    plugin_summary: str = ""
    cwd: str = "."
    tool_registry: ToolRegistry | None = None
    app_state: AppStateStore | None = None
    session_backend: SessionBackend = DEFAULT_SESSION_BACKEND
    session_id: str | None = None
    extra_skill_dirs: Iterable[str | Path] | None = None
    extra_plugin_roots: Iterable[str | Path] | None = None


# func(str, CommandContext) ==> CommandResult
CommandHandler = Callable[[str, CommandContext], Awaitable[CommandResult]]


@dataclass
class SlashCommand:
    """Definition of a slash command."""

    name: str
    description: str
    handler: CommandHandler
    remote_invocable: bool = True  # 远程调用安全控制字段，用于管理命令在远程环境（如 ohmo 网关）中的可执行性
    remote_admin_opt_in: bool = False  # 为敏感命令提供双重选择加入机制，即使 remote_invocable=False，在特定网关配置下仍允许远程管理员执行。
    aliases: tuple[str, ...] = ()


class CommandRegistry:
    """Map slash commands to handlers."""

    def __init__(self) -> None:
        # Primary commands keyed by canonical name, plus aliases pointing at
        # the same SlashCommand instance. We keep a separate set of canonical
        # names so help/listing output doesn't duplicate aliased entries.
        self._commands: dict[str, SlashCommand] = {}  # alias: command
        self._canonical_names: list[str] = []  # 保存command的名称

    def register(self, command: SlashCommand) -> None:
        """Register a command, plus any aliases pointing at the same handler."""
        if command.name not in self._commands:
            self._canonical_names.append(command.name)
        self._commands[command.name] = command  # 正式名称注册
        for alias in command.aliases:
            self._commands[alias] = command

    def lookup(self, raw_input: str) -> tuple[SlashCommand, str] | None:
        """Parse a slash command and return its handler plus raw args."""
        if not raw_input.startswith("/"):
            return None
        name, _, args = raw_input[1:].partition(" ")
        command = self._commands.get(name)
        if command is None:
            return None
        return command, args.strip()

    def help_text(self) -> str:
        """Return a formatted summary of all registered commands."""
        lines = ["Available commands:"]
        commands = [self._commands[name] for name in self._canonical_names]
        for command in sorted(commands, key=lambda item: item.name):
            lines.append(f"/{command.name:<12} {command.description}")
        return "\n".join(lines)

    def list_commands(self) -> list[SlashCommand]:
        """Return canonical commands in registration order (aliases omitted)."""
        return [self._commands[name] for name in self._canonical_names]