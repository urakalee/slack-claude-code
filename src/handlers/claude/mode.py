"""Mode command handler: /mode for Claude and Codex sessions."""

from slack_bolt.async_app import AsyncApp

from src.codex.capabilities import (
    SUPPORTED_COMPAT_MODE_ALIASES,
    codex_mode_alias_for_approval,
    normalize_codex_approval_mode,
    resolve_codex_compat_mode,
)
from src.config import config
from src.database.models import Session
from src.utils.formatters.command import error_message

from ..base import CommandContext, HandlerDependencies, slack_command

# Mode aliases: short name -> CLI mode value
CLAUDE_MODE_ALIASES = {
    "bypass": config.DEFAULT_BYPASS_MODE,
    "accept": "acceptEdits",
    "default": "default",
    "plan": "plan",
    "ask": "default",
    "delegate": "delegate",
}

# Reverse lookup for display
CLAUDE_MODE_DISPLAY = {v: k for k, v in CLAUDE_MODE_ALIASES.items()}


def register_mode_command(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register mode command handler.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command("/mode")
    @slack_command(
        require_text=False,
        usage_hint=(
            "Usage: /mode [bypass|accept|plan|ask|default|delegate|"
            "approval <mode>|sandbox <mode>]"
        ),
    )
    async def handle_mode(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /mode command - view or set permission mode for session."""
        text = ctx.text.strip().lower() if ctx.text else ""

        # Get session
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        backend = session.get_backend()

        if backend == "codex":
            await _handle_codex_mode(ctx, deps, session, text)
            return

        # No argument: show current mode
        if not text:
            current_mode = session.permission_mode or config.CLAUDE_PERMISSION_MODE
            display_mode = CLAUDE_MODE_DISPLAY.get(current_mode, current_mode)

            mode_list = "\n".join(
                f"• `{alias}` - {_get_mode_description(alias)}"
                for alias in CLAUDE_MODE_ALIASES
            )

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Current mode: {display_mode}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Current permission mode:* `{display_mode}`\n\n*Available modes:*\n{mode_list}",
                        },
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Use `/mode <name>` to change the mode for this session.",
                            }
                        ],
                    },
                ],
            )
            return

        # Check if it's a valid mode alias
        if text not in CLAUDE_MODE_ALIASES:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Unknown mode: {text}",
                blocks=error_message(
                    f"Unknown mode: `{text}`\n\nValid modes: {', '.join(f'`{m}`' for m in CLAUDE_MODE_ALIASES)}"
                ),
            )
            return

        # Set the mode
        cli_mode = CLAUDE_MODE_ALIASES[text]
        await deps.db.update_session_mode(ctx.channel_id, ctx.thread_ts, cli_mode)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Mode set to: {text}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":heavy_check_mark: Permission mode set to `{text}`\n\n{_get_mode_description(text)}",
                    },
                },
            ],
        )


async def _handle_codex_mode(
    ctx: CommandContext,
    deps: HandlerDependencies,
    session: Session,
    text: str,
) -> None:
    """Handle /mode for Codex sessions."""
    tokens = text.split(None, 1) if text else []
    primary = tokens[0] if tokens else ""
    value = tokens[1].strip().lower() if len(tokens) > 1 else ""

    if primary == "approval":
        if not value:
            current_mode = normalize_codex_approval_mode(
                session.approval_mode or config.CODEX_APPROVAL_MODE
            )
            valid_modes = ", ".join(f"`{mode}`" for mode in config.VALID_APPROVAL_MODES)
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Current approval mode: {current_mode}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*Current Codex approval mode:* `{current_mode}`\n\n"
                                f"*Valid modes:* {valid_modes}\n\n"
                                "Use `/mode approval <mode>` to update."
                            ),
                        },
                    }
                ],
            )
            return

        if value not in config.VALID_APPROVAL_MODES:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Invalid approval mode: {value}",
                blocks=error_message(
                    f"Invalid approval mode: `{value}`\n\n"
                    f"Valid modes: {', '.join(f'`{m}`' for m in config.VALID_APPROVAL_MODES)}"
                ),
            )
            return

        normalized_value = normalize_codex_approval_mode(value)
        await deps.db.update_session_approval_mode(
            ctx.channel_id, ctx.thread_ts, normalized_value
        )
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Codex approval mode set to: {normalized_value}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":heavy_check_mark: Codex approval mode set to `{normalized_value}`",
                    },
                }
            ],
        )
        return

    if primary == "sandbox":
        if not value:
            current_mode = session.sandbox_mode or config.CODEX_SANDBOX_MODE
            valid_modes = ", ".join(f"`{mode}`" for mode in config.VALID_SANDBOX_MODES)
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Current sandbox mode: {current_mode}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"*Current Codex sandbox mode:* `{current_mode}`\n\n"
                                f"*Valid modes:* {valid_modes}\n\n"
                                "Use `/mode sandbox <mode>` to update."
                            ),
                        },
                    }
                ],
            )
            return

        if value not in config.VALID_SANDBOX_MODES:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Invalid sandbox mode: {value}",
                blocks=error_message(
                    f"Invalid sandbox mode: `{value}`\n\n"
                    f"Valid modes: {', '.join(f'`{m}`' for m in config.VALID_SANDBOX_MODES)}"
                ),
            )
            return

        await deps.db.update_session_sandbox_mode(ctx.channel_id, ctx.thread_ts, value)
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Codex sandbox mode set to: {value}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":heavy_check_mark: Codex sandbox mode set to `{value}`",
                    },
                }
            ],
        )
        return

    if not text:
        current_mode = _get_codex_display_mode(
            permission_mode=session.permission_mode,
            approval_mode=session.approval_mode or config.CODEX_APPROVAL_MODE,
        )
        mode_list = "\n".join(
            f"• `{alias}` - {_get_codex_mode_description(alias)}"
            for alias in SUPPORTED_COMPAT_MODE_ALIASES
        )

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Current mode: {current_mode}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Current Codex mode:* `{current_mode}`\n\n*Available modes:*\n{mode_list}",
                    },
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                "Use `/mode approval <mode>` and `/mode sandbox <mode>` "
                                "for direct Codex controls."
                            ),
                        }
                    ],
                },
            ],
        )
        return

    resolved = resolve_codex_compat_mode(text)
    if resolved.error:
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Invalid Codex mode: {text}",
            blocks=error_message(resolved.error),
        )
        return

    cli_mode = _map_codex_alias_to_permission_mode(text)
    await deps.db.update_session_mode(ctx.channel_id, ctx.thread_ts, cli_mode)
    if resolved.approval_mode:
        await deps.db.update_session_approval_mode(
            ctx.channel_id, ctx.thread_ts, resolved.approval_mode
        )

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"Codex mode set to: {text}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":heavy_check_mark: Codex mode set to `{text}`\n\n"
                        f"{_get_codex_mode_description(text)}"
                    ),
                },
            }
        ],
    )


def _map_codex_alias_to_permission_mode(alias: str) -> str:
    """Map Codex `/mode` alias to stored permission mode."""
    if alias == "bypass":
        return config.DEFAULT_BYPASS_MODE
    if alias == "plan":
        return "plan"
    return "default"


def _get_codex_display_mode(
    permission_mode: str | None, approval_mode: str | None
) -> str:
    """Get Codex mode alias for display."""
    if (permission_mode or "").strip().lower() == "plan":
        return "plan"
    alias = codex_mode_alias_for_approval(approval_mode)
    # `ask` and `default` are equivalent for Codex approval mode `on-request`.
    # Prefer `default` as the canonical display to avoid implying the user
    # explicitly selected `ask`.
    if alias == "ask":
        return "default"
    return alias


def _get_codex_mode_description(mode: str) -> str:
    """Get human-readable mode description for Codex sessions."""
    descriptions = {
        "bypass": "Set approval mode to `never`.",
        "ask": "Set approval mode to `on-request`.",
        "default": "Alias of `ask` for compatibility.",
        "plan": "Plan-first mode; ask for a concrete plan before execution.",
    }
    return descriptions.get(mode, "")


def _get_mode_description(mode: str) -> str:
    """Get a human-readable description for a mode."""
    descriptions = {
        "bypass": "Auto-approve all operations (files, commands, etc.)",
        "accept": "Auto-accept file edits only",
        "plan": "Plan mode - assistant provides a plan before execution",
        "ask": "Default behavior - assistant asks for permission before operations",
        "default": "Default assistant behavior",
        "delegate": "Delegate permission decisions",
    }
    return descriptions.get(mode, "")
