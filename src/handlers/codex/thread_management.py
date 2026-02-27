"""Codex thread lifecycle command handlers."""

from typing import Optional

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.database.models import Session
from src.utils.formatters.command import error_message

from ..base import CommandContext, HandlerDependencies, slack_command


def _resolve_thread_id(token: Optional[str], session: Session) -> Optional[str]:
    """Resolve a thread id argument, supporting `current` alias."""
    normalized = (token or "").strip()
    if not normalized or normalized == "current":
        return session.codex_session_id
    return normalized


def register_codex_thread_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register `/codex-thread` command handlers."""

    @app.command("/codex-thread")
    @slack_command(require_text=False)
    async def handle_codex_thread(
        ctx: CommandContext, deps: HandlerDependencies = deps
    ):
        """Manage Codex thread lifecycle operations."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, ctx.thread_ts, config.DEFAULT_WORKING_DIR
        )
        if session.get_backend() != "codex":
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="/codex-thread is only available for Codex sessions.",
                blocks=error_message(
                    "`/codex-thread` is only available in Codex sessions."
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

        tokens = ctx.text.split() if ctx.text else []
        if not tokens:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Usage: /codex-thread <subcommand>",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "*Codex Thread Commands*\n"
                                "• `/codex-thread list`\n"
                                "• `/codex-thread read <thread_id|current>`\n"
                                "• `/codex-thread fork <thread_id|current>`\n"
                                "• `/codex-thread archive <thread_id|current>`\n"
                                "• `/codex-thread unarchive <thread_id|current>`\n"
                                "• `/codex-thread rollback <num_turns> [thread_id|current]`\n"
                                "• `/codex-thread compact [thread_id|current]`"
                            ),
                        },
                    }
                ],
            )
            return

        subcommand = tokens[0].lower()
        working_directory = session.working_directory

        try:
            if subcommand == "list":
                result = await deps.codex_executor.thread_list(
                    working_directory, limit=20
                )
                threads = result.get("data", [])
                if not threads:
                    text = "No threads found."
                else:
                    lines = []
                    for thread in threads[:10]:
                        thread_id = thread.get("id", "unknown")
                        name = thread.get("name") or "(unnamed)"
                        status = thread.get("status", "unknown")
                        updated_at = thread.get("updatedAt", "unknown")
                        lines.append(
                            f"• `{thread_id}`\nname: {name}\nstatus: {status}\nupdated: {updated_at}"
                        )
                    text = "*Recent threads*\n" + "\n\n".join(lines)

                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Codex thread list",
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": text},
                        }
                    ],
                )
                return

            if subcommand == "read":
                thread_id = _resolve_thread_id(
                    tokens[1] if len(tokens) > 1 else None, session
                )
                if not thread_id:
                    raise RuntimeError(
                        "No active Codex thread. Run a Codex message first."
                    )
                result = await deps.codex_executor.thread_read(
                    thread_id, working_directory, True
                )
                thread = result.get("thread", {})
                turns = thread.get("turns", [])
                summary = (
                    f"*Thread:* `{thread.get('id', thread_id)}`\n"
                    f"*Name:* {thread.get('name') or '(unnamed)'}\n"
                    f"*Status:* {thread.get('status', 'unknown')}\n"
                    f"*Turns:* {len(turns)}\n"
                    f"*Preview:* {thread.get('preview') or '(none)'}"
                )
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"Thread {thread_id}",
                    blocks=[
                        {"type": "section", "text": {"type": "mrkdwn", "text": summary}}
                    ],
                )
                return

            if subcommand == "fork":
                thread_id = _resolve_thread_id(
                    tokens[1] if len(tokens) > 1 else None, session
                )
                if not thread_id:
                    raise RuntimeError(
                        "No active Codex thread. Run a Codex message first."
                    )
                result = await deps.codex_executor.thread_fork(
                    thread_id, working_directory
                )
                thread = result.get("thread", {})
                new_thread_id = thread.get("id")
                if new_thread_id:
                    await deps.db.update_session_codex_id(
                        ctx.channel_id, ctx.thread_ts, new_thread_id
                    )
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"Forked thread {thread_id}",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f":white_check_mark: Forked `{thread_id}` -> "
                                    f"`{new_thread_id or 'unknown'}`"
                                ),
                            },
                        }
                    ],
                )
                return

            if subcommand in {"archive", "unarchive", "compact"}:
                thread_id = _resolve_thread_id(
                    tokens[1] if len(tokens) > 1 else None, session
                )
                if not thread_id:
                    raise RuntimeError(
                        "No active Codex thread. Run a Codex message first."
                    )
                if subcommand == "archive":
                    await deps.codex_executor.thread_archive(
                        thread_id, working_directory
                    )
                elif subcommand == "unarchive":
                    await deps.codex_executor.thread_unarchive(
                        thread_id, working_directory
                    )
                else:
                    await deps.codex_executor.thread_compact_start(
                        thread_id, working_directory
                    )
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"{subcommand} requested",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f":white_check_mark: `{subcommand}` requested for `{thread_id}`.",
                            },
                        }
                    ],
                )
                return

            if subcommand == "rollback":
                if len(tokens) < 2:
                    raise RuntimeError(
                        "Usage: /codex-thread rollback <num_turns> [thread_id|current]"
                    )
                try:
                    num_turns = max(1, int(tokens[1]))
                except ValueError as exc:
                    raise RuntimeError("num_turns must be an integer") from exc
                thread_id = _resolve_thread_id(
                    tokens[2] if len(tokens) > 2 else None, session
                )
                if not thread_id:
                    raise RuntimeError(
                        "No active Codex thread. Run a Codex message first."
                    )
                await deps.codex_executor.thread_rollback(
                    thread_id, num_turns, working_directory
                )
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"Rollback requested for {thread_id}",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": (
                                    f":white_check_mark: Rolled back `{thread_id}` by `{num_turns}` turn(s)."
                                ),
                            },
                        }
                    ],
                )
                return

            raise RuntimeError(f"Unknown subcommand: `{subcommand}`")
        except Exception as e:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=f"/codex-thread failed: {e}",
                blocks=error_message(str(e)),
            )
