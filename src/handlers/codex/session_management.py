"""Codex session management command handlers: /codex-clear, /codex-sessions, /codex-cleanup, /codex-status, /pty."""

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.pty.pool import PTYSessionPool
from src.utils.formatting import SlackFormatter

from ..base import CommandContext, HandlerDependencies, get_command_name, slack_command


def register_codex_session_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register Codex session management commands."""

    @app.command(get_command_name("/codex-clear"))
    @slack_command()
    async def handle_codex_clear(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Clear the Codex session (start fresh)."""
        # Stop PTY session if exists
        if config.USE_PTY_SESSIONS:
            stopped = await PTYSessionPool.remove(ctx.channel_id, ctx.thread_ts)
            if stopped:
                ctx.logger.info(f"Stopped PTY session for {ctx.channel_id}:{ctx.thread_ts}")

        # Clear database session ID
        await deps.db.clear_session_codex_id(ctx.channel_id, ctx.thread_ts)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": ":broom: Codex session cleared. Next message will start a fresh Codex session.",
                    },
                }
            ],
        )

    @app.command(get_command_name("/codex-sessions"))
    @slack_command()
    async def handle_codex_sessions(ctx: CommandContext, deps: HandlerDependencies = deps):
        """List all sessions for this channel."""
        sessions = await deps.db.get_sessions_by_channel(ctx.channel_id)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=SlackFormatter.session_list(sessions),
        )

    @app.command(get_command_name("/codex-cleanup"))
    @slack_command()
    async def handle_codex_cleanup(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Clean up inactive sessions."""
        # Parse days argument
        try:
            days = int(ctx.text) if ctx.text else 30
            if days < 1:
                days = 1
            elif days > 365:
                days = 365
        except ValueError:
            days = 30

        deleted_count = await deps.db.delete_inactive_sessions(days)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=SlackFormatter.session_cleanup_result(deleted_count, days),
        )

    @app.command(get_command_name("/codex-status"))
    @slack_command()
    async def handle_codex_status(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Show current Codex session status."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, ctx.thread_ts, config.DEFAULT_WORKING_DIR
        )

        sandbox_mode = session.sandbox_mode or config.CODEX_SANDBOX_MODE
        approval_mode = session.approval_mode or config.CODEX_APPROVAL_MODE
        model = session.model or config.DEFAULT_MODEL or "(default)"
        has_session = ":white_check_mark:" if session.codex_session_id else ":x:"

        # Get PTY session info if enabled
        pty_info = None
        if config.USE_PTY_SESSIONS:
            pty_info = PTYSessionPool.get_session_info(ctx.channel_id, ctx.thread_ts)

        fields = [
            {
                "type": "mrkdwn",
                "text": f"*Working Dir:*\n`{session.working_directory}`",
            },
            {
                "type": "mrkdwn",
                "text": f"*Model:*\n`{model}`",
            },
            {
                "type": "mrkdwn",
                "text": f"*Sandbox:*\n`{sandbox_mode}`",
            },
            {
                "type": "mrkdwn",
                "text": f"*Approval:*\n`{approval_mode}`",
            },
            {
                "type": "mrkdwn",
                "text": f"*Active Session:*\n{has_session}",
            },
            {
                "type": "mrkdwn",
                "text": f"*Session Type:*\n{'Thread' if session.thread_ts else 'Channel'}",
            },
        ]

        # Add PTY status if available
        if pty_info:
            state_emoji = {
                "idle": ":large_green_circle:",
                "busy": ":large_blue_circle:",
                "starting": ":large_yellow_circle:",
                "error": ":red_circle:",
                "stopped": ":white_circle:",
            }.get(pty_info["state"], ":white_circle:")

            fields.append({
                "type": "mrkdwn",
                "text": f"*PTY State:*\n{state_emoji} {pty_info['state']}",
            })
            fields.append({
                "type": "mrkdwn",
                "text": f"*PTY PID:*\n`{pty_info['pid']}`",
            })

        context_text = f"Last active: {session.last_active.strftime('%Y-%m-%d %H:%M:%S')}"
        if pty_info:
            context_text += f" | PTY idle: {int(pty_info['idle_seconds'])}s"

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Codex Session Status*",
                    },
                },
                {
                    "type": "section",
                    "fields": fields,
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": context_text,
                        }
                    ],
                },
            ],
        )

    @app.command("/pty")
    @slack_command()
    async def handle_pty(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Show PTY session info for all active sessions."""
        if not config.USE_PTY_SESSIONS:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text="PTY sessions are disabled. Set USE_PTY_SESSIONS=true to enable.",
            )
            return

        sessions = PTYSessionPool.get_session_info()

        if not sessions:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text="No active PTY sessions.",
            )
            return

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Active PTY Sessions ({len(sessions)})*",
                },
            },
        ]

        for s in sessions:
            state_emoji = {
                "idle": ":large_green_circle:",
                "busy": ":large_blue_circle:",
                "starting": ":large_yellow_circle:",
                "error": ":red_circle:",
                "stopped": ":white_circle:",
            }.get(s["state"], ":white_circle:")

            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{state_emoji} *{s['session_id']}*\n"
                        f"State: `{s['state']}` | PID: `{s['pid']}` | "
                        f"Idle: {int(s['idle_seconds'])}s\n"
                        f"Dir: `{s['working_directory']}`"
                    ),
                },
            })

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=blocks,
        )
