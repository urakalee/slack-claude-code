"""Mode command handler: /mode (Claude permission modes)."""

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.formatting import SlackFormatter

from ..base import CommandContext, HandlerDependencies, get_command_name, slack_command

# Mode aliases: short name -> CLI mode value
MODE_ALIASES = {
    "bypass": config.DEFAULT_BYPASS_MODE,
    "accept": "acceptEdits",
    "default": "default",
    "plan": "plan",
    "ask": "default",
    "delegate": "delegate",
}

# Reverse lookup for display
MODE_DISPLAY = {v: k for k, v in MODE_ALIASES.items()}


def register_mode_command(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register mode command handler.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command(get_command_name("/mode"))
    @slack_command(
        require_text=False, usage_hint="Usage: /mode [bypass|accept|plan|ask|default|delegate]"
    )
    async def handle_mode(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /mode command - view or set permission mode for session."""
        text = ctx.text.strip().lower() if ctx.text else ""

        # Get session
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        # No argument: show current mode
        if not text:
            current_mode = session.permission_mode or config.CLAUDE_PERMISSION_MODE
            display_mode = MODE_DISPLAY.get(current_mode, current_mode)

            mode_list = "\n".join(
                f"• `{alias}` - {_get_mode_description(alias)}" for alias in MODE_ALIASES
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
        if text not in MODE_ALIASES:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Unknown mode: {text}",
                blocks=SlackFormatter.error_message(
                    f"Unknown mode: `{text}`\n\nValid modes: {', '.join(f'`{m}`' for m in MODE_ALIASES)}"
                ),
            )
            return

        # Set the mode
        cli_mode = MODE_ALIASES[text]
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


def _get_mode_description(mode: str) -> str:
    """Get a human-readable description for a mode."""
    descriptions = {
        "bypass": "Auto-approve all operations (files, commands, etc.)",
        "accept": "Auto-accept file edits only",
        "plan": "Plan mode - Claude plans before executing",
        "ask": "Default behavior - Claude asks for permission before operations",
        "default": "Default Claude behavior",
        "delegate": "Delegate permission decisions",
    }
    return descriptions.get(mode, "")
