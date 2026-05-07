"""Slash command registry."""

from __future__ import annotations
from typing import Iterable
from openharness.plugins.types import PluginCommandDefinition

from openharness.commands.handlers import *
from openharness.commands.types import CommandResult, SlashCommand, CommandRegistry, CommandContext

def _render_plugin_command_prompt(command: PluginCommandDefinition, args: str, session_id: str | None = None) -> str:
    prompt = command.content
    raw_args = args.strip()
    if command.is_skill and command.base_dir:
        prompt = f"Base directory for this skill: {command.base_dir}\n\n{prompt}"
    prompt = prompt.replace("${ARGUMENTS}", raw_args).replace("$ARGUMENTS", raw_args)
    if session_id:
        prompt = prompt.replace("${CLAUDE_SESSION_ID}", session_id)
    if raw_args and "${ARGUMENTS}" not in command.content and "$ARGUMENTS" not in command.content:
        prompt = f"{prompt}\n\nArguments: {raw_args}"
    return prompt


def create_default_command_registry(
    plugin_commands: Iterable[PluginCommandDefinition] | None = None,
) -> CommandRegistry:
    """Create the built-in command registry."""
    registry = CommandRegistry()

    async def help_handler(_: str, context: CommandContext) -> CommandResult:
        del context
        return CommandResult(message=registry.help_text())

    registry.register(SlashCommand("help", "Show available commands", help_handler))
    registry.register(
        SlashCommand("exit", "Exit OpenHarness", exit_handler, aliases=("quit",))
    )
    registry.register(SlashCommand("clear", "Clear conversation history", clear_handler))
    registry.register(SlashCommand("version", "Show the installed OpenHarness version", version_handler))
    registry.register(SlashCommand("status", "Show session status", status_handler))
    registry.register(SlashCommand("context", "Show the active runtime system prompt", context_handler))
    registry.register(SlashCommand("summary", "Summarize conversation history", summary_handler))
    registry.register(SlashCommand("compact", "Compact older conversation history", compact_handler))
    registry.register(SlashCommand("cost", "Show token usage and estimated cost", cost_handler))
    registry.register(SlashCommand("usage", "Show usage and token estimates", usage_handler))
    registry.register(SlashCommand("stats", "Show session statistics", stats_handler))
    registry.register(SlashCommand("memory", "Inspect and manage project memory", memory_handler))
    registry.register(SlashCommand("hooks", "Show configured hooks", hooks_handler))
    registry.register(SlashCommand("resume", "Restore the latest saved session", resume_handler))
    registry.register(SlashCommand("session", "Inspect the current session storage", session_handler))
    registry.register(SlashCommand("export", "Export the current transcript", export_handler))
    registry.register(SlashCommand("share", "Create a shareable transcript snapshot", share_handler))
    registry.register(SlashCommand("copy", "Copy the latest response or provided text", copy_handler))
    registry.register(SlashCommand("tag", "Create a named snapshot of the current session", tag_handler))
    registry.register(SlashCommand("rewind", "Remove the latest conversation turn(s)", rewind_handler))
    registry.register(SlashCommand("files", "List files in the current workspace", files_handler))
    registry.register(SlashCommand("init", "Initialize project OpenHarness files", init_handler))
    registry.register(SlashCommand("bridge", "Inspect bridge helpers and spawn bridge sessions", bridge_handler))
    registry.register(SlashCommand("login", "Show auth status or store an API key", login_handler))
    registry.register(SlashCommand("logout", "Clear the stored API key", logout_handler))
    registry.register(SlashCommand("feedback", "Save CLI feedback to the local feedback log", feedback_handler))
    registry.register(SlashCommand("onboarding", "Show the quickstart guide", onboarding_handler))
    registry.register(SlashCommand("skills", "List or show available skills", skills_handler))
    registry.register(SlashCommand("config", "Show or update configuration", config_handler))
    registry.register(SlashCommand("mcp", "Show MCP status", mcp_handler))
    registry.register(
        SlashCommand(
            "plugin",
            "Manage plugins",
            plugin_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )
    registry.register(
        SlashCommand(
            "reload-plugins",
            "Reload plugin discovery for this workspace",
            reload_plugins_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )
    registry.register(
        SlashCommand(
            "permissions",
            "Show or update permission mode",
            permissions_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )
    registry.register(
        SlashCommand(
            "plan",
            "Toggle plan permission mode",
            plan_handler,
            remote_invocable=False,
            remote_admin_opt_in=True,
        )
    )
    registry.register(SlashCommand("fast", "Show or update fast mode", fast_handler))
    registry.register(SlashCommand("effort", "Show or update reasoning effort", effort_handler))
    registry.register(SlashCommand("passes", "Show or update reasoning pass count", passes_handler))
    registry.register(SlashCommand("turns", "Show or update maximum agentic turn count", turns_handler))
    registry.register(SlashCommand("continue", "Continue the previous tool loop if it was interrupted", continue_handler))
    registry.register(SlashCommand("provider", "Show or switch provider profiles", provider_handler))
    registry.register(SlashCommand("model", "Show or update the default model", model_handler))
    registry.register(SlashCommand("theme", "List, set, show or preview TUI themes", theme_handler))
    registry.register(SlashCommand("output-style", "Show or update output style", output_style_handler))
    registry.register(SlashCommand("keybindings", "Show resolved keybindings", keybindings_handler))
    registry.register(SlashCommand("vim", "Show or update Vim mode", vim_handler))
    registry.register(SlashCommand("voice", "Show or update voice mode", voice_handler))
    registry.register(SlashCommand("doctor", "Show environment diagnostics", doctor_handler))
    registry.register(SlashCommand("diff", "Show git diff output", diff_handler))
    registry.register(SlashCommand("branch", "Show git branch information", branch_handler))
    registry.register(SlashCommand("commit", "Show status or create a git commit", commit_handler))
    registry.register(SlashCommand("issue", "Show or update project issue context", issue_handler))
    registry.register(SlashCommand("pr_comments", "Show or update project PR comments context", pr_comments_handler))
    registry.register(SlashCommand("privacy-settings", "Show local privacy and storage settings", privacy_settings_handler))
    registry.register(SlashCommand("rate-limit-options", "Show ways to reduce provider rate pressure", rate_limit_options_handler))
    registry.register(SlashCommand("release-notes", "Show recent OpenHarness release notes", release_notes_handler))
    registry.register(SlashCommand("upgrade", "Show upgrade instructions", upgrade_handler))
    registry.register(SlashCommand("agents", "List or inspect agent and teammate tasks", agents_handler))
    registry.register(SlashCommand("subagents", "Show subagent usage and inspect worker tasks", agents_handler))
    registry.register(SlashCommand("tasks", "Manage background tasks", tasks_handler))
    registry.register(SlashCommand("autopilot", "Manage repo autopilot intake and context", autopilot_handler))
    registry.register(SlashCommand("ship", "Queue and execute an ohmo-driven repo task", ship_handler))

    for plugin_command in plugin_commands or ():
        if not plugin_command.user_invocable:
            continue

        async def _plugin_command_handler(
            args: str,
            context: CommandContext,
            *,
            command: PluginCommandDefinition = plugin_command,
        ) -> CommandResult:
            prompt = _render_plugin_command_prompt(
                command,
                args,
                getattr(context, "session_id", None),
            )
            if command.disable_model_invocation:
                return CommandResult(message=prompt)
            return CommandResult(
                submit_prompt=prompt,
                submit_model=command.model,
            )

        registry.register(
            SlashCommand(
                plugin_command.name,
                plugin_command.description,
                _plugin_command_handler,
            )
        )
    return registry
