"""Queue command handlers: /q and /qc."""

import asyncio
from typing import Optional

from loguru import logger
from slack_bolt.async_app import AsyncApp

from src.config import config
from src.tasks.manager import TaskManager
from src.utils.execution_scope import build_session_scope
from src.utils.formatters.base import escape_markdown
from src.utils.formatters.command import error_message
from src.utils.formatters.queue import (
    queue_item_complete,
    queue_item_running,
    queue_status,
)

from ..base import CommandContext, HandlerDependencies, slack_command
from ..command_router import execute_for_session

# Default timeout for queue processors (1 hour)
QUEUE_PROCESSOR_TIMEOUT = 3600
_QUEUE_START_LOCKS: dict[str, asyncio.Lock] = {}
_QUEUE_START_LOCKS_GUARD = asyncio.Lock()


def _queue_task_id(channel_id: str, thread_ts: Optional[str]) -> str:
    """Build a stable task id for a queue processor scoped to channel/thread."""
    return f"queue_{build_session_scope(channel_id, thread_ts)}"


async def _create_queue_task(
    coro,
    channel_id: str,
    thread_ts: Optional[str],
    task_logger=None,
) -> asyncio.Task:
    """Create a queue processor task with proper tracking.

    Uses TaskManager for lifecycle management with automatic cleanup.
    """
    task = asyncio.create_task(coro)
    task_id = _queue_task_id(channel_id, thread_ts)

    await TaskManager.register(
        task_id=task_id,
        task=task,
        channel_id=channel_id,
        task_type="queue_processor",
        timeout_seconds=QUEUE_PROCESSOR_TIMEOUT,
    )

    def done_callback(t: asyncio.Task) -> None:
        if not t.cancelled():
            exc = t.exception()
            if exc:
                log = task_logger or logger
                log.error(f"Queue processor failed: {exc}", exc_info=exc)

    task.add_done_callback(done_callback)
    return task


async def _is_queue_processor_running(channel_id: str, thread_ts: Optional[str]) -> bool:
    """Check if a queue processor is already running for a scope."""
    task_id = _queue_task_id(channel_id, thread_ts)
    tracked = await TaskManager.get(task_id)
    return tracked is not None and not tracked.is_done


async def _get_queue_start_lock(task_id: str) -> asyncio.Lock:
    """Return a per-scope lock used to serialize queue processor startup."""
    async with _QUEUE_START_LOCKS_GUARD:
        if task_id not in _QUEUE_START_LOCKS:
            _QUEUE_START_LOCKS[task_id] = asyncio.Lock()
        return _QUEUE_START_LOCKS[task_id]


async def ensure_queue_processor(
    channel_id: str,
    thread_ts: Optional[str],
    deps: HandlerDependencies,
    client,
    task_logger=None,
) -> None:
    """Ensure the queue processor is active for this channel/thread scope."""
    task_id = _queue_task_id(channel_id, thread_ts)
    start_lock = await _get_queue_start_lock(task_id)
    async with start_lock:
        if await _is_queue_processor_running(channel_id, thread_ts):
            return
        await _create_queue_task(
            _process_queue(channel_id, deps, client, task_logger, thread_ts=thread_ts),
            channel_id,
            thread_ts,
            task_logger,
        )


def register_queue_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register queue command handlers."""

    @app.command("/q")
    @slack_command(require_text=True, usage_hint="Usage: /q <prompt>")
    async def handle_queue_add(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /q <prompt> command - add command to FIFO queue."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )

        # Add to queue in this session scope.
        await deps.db.add_to_queue(
            session_id=session.id,
            channel_id=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            prompt=ctx.text,
        )

        # Get current queue state for this scope.
        pending = await deps.db.get_pending_queue_items(ctx.channel_id, ctx.thread_ts)
        running = await deps.db.get_running_queue_item(ctx.channel_id, ctx.thread_ts)

        # Confirm to user.
        position = len(pending)
        if running:
            position += 1  # Account for currently running item.

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Added to queue (position {position}): {ctx.text[:100]}...",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":inbox_tray: Added to queue (position #{position})\n"
                        f"> {escape_markdown(ctx.text[:200])}"
                        f"{'...' if len(ctx.text) > 200 else ''}",
                    },
                },
            ],
        )

        await ensure_queue_processor(ctx.channel_id, ctx.thread_ts, deps, ctx.client, ctx.logger)

    @app.command("/qc")
    @slack_command(require_text=True, usage_hint="Usage: /qc <view|clear|remove [item_id]>")
    async def handle_queue_command(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /qc queue control subcommands."""
        parts = ctx.text.split()
        subcommand = parts[0].lower()
        args = parts[1:]

        if subcommand == "view":
            if args:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qc view"),
                )
                return

            pending = await deps.db.get_pending_queue_items(ctx.channel_id, ctx.thread_ts)
            running = await deps.db.get_running_queue_item(ctx.channel_id, ctx.thread_ts)

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Queue status",
                blocks=queue_status(pending, running),
            )
            return

        if subcommand == "clear":
            if args:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qc clear"),
                )
                return

            cleared = await deps.db.clear_queue(ctx.channel_id, ctx.thread_ts)

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=f"Cleared {cleared} item(s) from queue",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":wastebasket: Cleared {cleared} pending item(s) from queue.",
                        },
                    },
                ],
            )
            return

        if subcommand == "remove":
            if len(args) > 1:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Invalid queue command",
                    blocks=error_message("Usage: /qc remove [item_id]"),
                )
                return

            if args:
                try:
                    item_id = int(args[0])
                except ValueError:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        text="Invalid item ID",
                        blocks=error_message("Invalid item ID. Usage: /qc remove [item_id]"),
                    )
                    return
            else:
                pending = await deps.db.get_pending_queue_items(ctx.channel_id, ctx.thread_ts)
                if not pending:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        text="Queue is empty",
                        blocks=error_message("Queue is empty. Nothing to remove."),
                    )
                    return
                item_id = pending[0].id

            removed = await deps.db.remove_queue_item(item_id, ctx.channel_id, ctx.thread_ts)

            if removed:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"Removed item #{item_id} from queue",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f":wastebasket: Removed item #{item_id} from queue.",
                            },
                        },
                    ],
                )
            else:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"Item #{item_id} not found or not pending",
                    blocks=error_message(
                        f"Item #{item_id} not found or is already running/completed."
                    ),
                )
            return

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Invalid queue command",
            blocks=error_message("Usage: /qc <view|clear|remove [item_id]>"),
        )


async def _process_queue(
    channel_id: str,
    deps: HandlerDependencies,
    client,
    task_logger,
    thread_ts: Optional[str] = None,
) -> None:
    """Process queue items sequentially for a channel/thread scope."""
    log = task_logger or logger
    scope = build_session_scope(channel_id, thread_ts)
    running_item = None
    running_message_ts = None

    try:
        while True:
            pending = await deps.db.get_pending_queue_items(channel_id, thread_ts)
            if not pending:
                log.info(f"Queue empty for scope {scope}, stopping processor")
                break

            # Ensure we never overlap with a currently running Codex turn in this scope.
            while deps.codex_executor and await deps.codex_executor.has_active_turn(scope):
                log.debug(f"Queue waiting for active Codex turn to finish in scope {scope}")
                await asyncio.sleep(0.5)

            item = pending[0]
            running_item = item
            running_message_ts = None
            log.info(f"Queue processing item #{item.id} in scope {scope}")
            await deps.db.update_queue_item_status(item.id, "running")

            message_ts = None
            try:
                response = await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=f"Processing queue item #{item.id}",
                    blocks=queue_item_running(item),
                )
                message_ts = response["ts"]
                running_message_ts = message_ts

                session = await deps.db.get_or_create_session(
                    channel_id,
                    thread_ts=thread_ts,
                    default_cwd=config.DEFAULT_WORKING_DIR,
                )

                route = await execute_for_session(
                    deps=deps,
                    session=session,
                    prompt=item.prompt,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    execution_id=f"queue_{item.id}",
                    slack_client=client,
                    logger=log,
                )
                result = route.result

                if result.success:
                    await deps.db.update_queue_item_status(
                        item.id, "completed", output=result.output
                    )
                else:
                    await deps.db.update_queue_item_status(
                        item.id,
                        "failed",
                        output=result.output,
                        error_message=result.error,
                    )

                await client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=f"Completed queue item #{item.id}",
                    blocks=queue_item_complete(item, result),
                )

            except Exception as e:
                log.error(f"Queue item {item.id} failed in scope {scope}: {e}")
                await deps.db.update_queue_item_status(item.id, "failed", error_message=str(e))
                if message_ts:
                    await client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text=f"Queue item #{item.id} failed",
                        blocks=error_message(f"Queue item failed: {e}"),
                    )
                else:
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=f"Queue item #{item.id} failed",
                        blocks=error_message(f"Queue item failed: {e}"),
                    )

            running_item = None
            running_message_ts = None
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        log.info(f"Queue processor cancelled for scope {scope}")
        if running_item is not None:
            await deps.db.update_queue_item_status(
                running_item.id,
                "cancelled",
                error_message="Queue processor cancelled",
            )
            if running_message_ts:
                await client.chat_update(
                    channel=channel_id,
                    ts=running_message_ts,
                    text=f"Queue item #{running_item.id} cancelled",
                    blocks=error_message("Queue item cancelled while processing."),
                )
            else:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=f"Queue item #{running_item.id} cancelled",
                    blocks=error_message("Queue item cancelled while processing."),
                )
        raise
