"""Codex session management command handlers."""

from slack_bolt.async_app import AsyncApp

from src.codex.capabilities import normalize_codex_approval_mode
from src.config import config
from src.utils.execution_scope import build_session_scope
from src.utils.formatters.command import error_message
from src.utils.formatters.session import session_cleanup_result, session_list

from ..base import CommandContext, HandlerDependencies, slack_command


def register_codex_session_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register Codex session management commands."""

    @app.command("/codex-clear")
    @slack_command()
    async def handle_codex_clear(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Clear the Codex session (start fresh)."""
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

    @app.command("/codex-sessions")
    @slack_command()
    async def handle_codex_sessions(
        ctx: CommandContext, deps: HandlerDependencies = deps
    ):
        """List all sessions for this channel."""
        sessions = await deps.db.get_sessions_by_channel(ctx.channel_id)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=session_list(sessions),
        )

    @app.command("/codex-cleanup")
    @slack_command()
    async def handle_codex_cleanup(
        ctx: CommandContext, deps: HandlerDependencies = deps
    ):
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
            blocks=session_cleanup_result(deleted_count, days),
        )

    @app.command("/codex-status")
    @slack_command()
    async def handle_codex_status(
        ctx: CommandContext, deps: HandlerDependencies = deps
    ):
        """Show current Codex session status."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, ctx.thread_ts, config.DEFAULT_WORKING_DIR
        )

        sandbox_mode = session.sandbox_mode or config.CODEX_SANDBOX_MODE
        approval_mode = normalize_codex_approval_mode(
            session.approval_mode or config.CODEX_APPROVAL_MODE
        )
        model = session.model or config.DEFAULT_MODEL or "(default)"
        has_session = ":white_check_mark:" if session.codex_session_id else ":x:"
        active_turn_text = ":x:"
        models_text = "n/a"
        account_text = "n/a"
        mcp_text = "n/a"
        features_text = "n/a"

        if deps.codex_executor:
            scope = build_session_scope(ctx.channel_id, ctx.thread_ts)
            active_turn = await deps.codex_executor.get_active_turn(scope)
            if active_turn:
                turn_id = active_turn.get("turn_id", "unknown")
                active_turn_text = f":white_check_mark: `{turn_id}`"

            try:
                model_list = await deps.codex_executor.model_list(
                    session.working_directory
                )
                models_text = str(len(model_list.get("data", [])))
            except Exception:
                models_text = "unavailable"

            try:
                account_read = await deps.codex_executor.account_read(
                    session.working_directory
                )
                account = account_read.get("account")
                if isinstance(account, dict):
                    account_type = account.get("type", "unknown")
                    if account_type == "chatgpt":
                        account_text = (
                            f"{account_type} ({account.get('planType', 'unknown')}) "
                            f"{account.get('email', '')}".strip()
                        )
                    else:
                        account_text = account_type
                else:
                    account_text = "none"
            except Exception:
                account_text = "unavailable"

            try:
                mcp_status = await deps.codex_executor.mcp_server_status_list(
                    session.working_directory
                )
                mcp_text = str(len(mcp_status.get("data", [])))
            except Exception:
                mcp_text = "unavailable"

            try:
                features = await deps.codex_executor.experimental_feature_list(
                    session.working_directory
                )
                features_text = str(len(features.get("data", [])))
            except Exception:
                features_text = "unavailable"

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
            {
                "type": "mrkdwn",
                "text": f"*Active Turn:*\n{active_turn_text}",
            },
            {
                "type": "mrkdwn",
                "text": f"*Available Models:*\n{models_text}",
            },
            {
                "type": "mrkdwn",
                "text": f"*Account:*\n{account_text}",
            },
            {
                "type": "mrkdwn",
                "text": f"*MCP Servers:*\n{mcp_text}",
            },
        ]

        context_text = (
            f"Last active: {session.last_active.strftime('%Y-%m-%d %H:%M:%S')} • "
            f"Experimental features: {features_text}"
        )

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

    @app.command("/codex-metrics")
    @slack_command()
    async def handle_codex_metrics(
        ctx: CommandContext, deps: HandlerDependencies = deps
    ):
        """Show runtime Codex integration metrics."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, ctx.thread_ts, config.DEFAULT_WORKING_DIR
        )
        if session.get_backend() != "codex":
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="/codex-metrics is only available for Codex sessions.",
                blocks=error_message(
                    "`/codex-metrics` is only available in Codex sessions."
                ),
            )
            return
        if not deps.codex_executor:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Codex executor is not configured.",
                blocks=error_message("Codex executor is not configured."),
            )
            return

        if (ctx.text or "").strip().lower() == "reset":
            await deps.codex_executor.reset_metrics()
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Codex metrics reset",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": ":wastebasket: Reset Codex runtime metrics counters.",
                        },
                    }
                ],
            )
            return

        snapshot = await deps.codex_executor.get_metrics_snapshot()
        text = (
            "*Codex Runtime Metrics*\n"
            f"• active turns: `{snapshot.get('active_turns', 0)}`\n"
            f"• turn starts: `{snapshot.get('turn_start_registered', 0)}`\n"
            f"• turn clears: `{snapshot.get('turn_state_cleared', 0)}`\n"
            f"• steer: `{snapshot.get('steer_successes', 0)}/{snapshot.get('steer_requests', 0)}` "
            f"({snapshot.get('steer_success_rate', 0.0):.0%}) "
            f"failures=`{snapshot.get('steer_failures', 0)}` "
            f"timeouts=`{snapshot.get('steer_timeouts', 0)}`\n"
            f"• interrupts: `{snapshot.get('interrupt_successes', 0)}/{snapshot.get('interrupt_requests', 0)}` "
            f"({snapshot.get('interrupt_success_rate', 0.0):.0%}) "
            f"failures=`{snapshot.get('interrupt_failures', 0)}` "
            f"timeouts=`{snapshot.get('interrupt_timeouts', 0)}`\n"
            f"• queue fallback: `{snapshot.get('queue_fallback_successes', 0)}/{snapshot.get('queue_fallback_attempts', 0)}` "
            f"({snapshot.get('queue_fallback_success_rate', 0.0):.0%}) "
            f"failures=`{snapshot.get('queue_fallback_failures', 0)}`"
        )
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Codex metrics",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
