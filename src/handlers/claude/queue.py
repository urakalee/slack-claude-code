"""Queue command handlers: /q, /qv, /qc, /qr."""

import asyncio

from loguru import logger
from slack_bolt.async_app import AsyncApp

from src.config import config
from src.tasks.manager import TaskManager
from src.utils.formatting import SlackFormatter

from ..base import CommandContext, HandlerDependencies, get_command_name, slack_command

# Default timeout for queue processors (1 hour)
QUEUE_PROCESSOR_TIMEOUT = 3600


async def _create_queue_task(coro, channel_id: str, task_logger=None) -> asyncio.Task:
    """Create a queue processor task with proper tracking.

    Uses TaskManager for lifecycle management with automatic cleanup.
    """
    task = asyncio.create_task(coro)
    task_id = f"queue_{channel_id}"

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


async def _is_queue_processor_running(channel_id: str) -> bool:
    """Check if a queue processor is already running for a channel."""
    task_id = f"queue_{channel_id}"
    tracked = await TaskManager.get(task_id)
    return tracked is not None and not tracked.is_done


def register_queue_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register queue command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command(get_command_name("/q"))
    @slack_command(require_text=True, usage_hint="Usage: /q <prompt>")
    async def handle_queue_add(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /q <prompt> command - add command to FIFO queue."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        # Add to queue
        await deps.db.add_to_queue(
            session_id=session.id,
            channel_id=ctx.channel_id,
            prompt=ctx.text,
        )

        # Get current queue state
        pending = await deps.db.get_pending_queue_items(ctx.channel_id)
        running = await deps.db.get_running_queue_item(ctx.channel_id)

        # Confirm to user
        position = len(pending)
        if running:
            position += 1  # Account for currently running item

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Added to queue (position {position}): {ctx.text[:100]}...",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":inbox_tray: Added to queue (position #{position})\n"
                        f"> {SlackFormatter._escape_markdown(ctx.text[:200])}"
                        f"{'...' if len(ctx.text) > 200 else ''}",
                    },
                },
            ],
        )

        # Start queue processor if not already running
        if not await _is_queue_processor_running(ctx.channel_id):
            await _create_queue_task(
                _process_queue(ctx.channel_id, deps, ctx.client, ctx.logger),
                ctx.channel_id,
                ctx.logger,
            )

    @app.command(get_command_name("/qv"))
    @slack_command()
    async def handle_queue_view(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /qv command - view queue status."""
        pending = await deps.db.get_pending_queue_items(ctx.channel_id)
        running = await deps.db.get_running_queue_item(ctx.channel_id)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text="Queue status",
            blocks=SlackFormatter.queue_status(pending, running),
        )

    @app.command(get_command_name("/qc"))
    @slack_command()
    async def handle_queue_clear(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /qc command - clear pending queue items."""
        cleared = await deps.db.clear_queue(ctx.channel_id)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
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

    @app.command(get_command_name("/qr"))
    @slack_command(require_text=True, usage_hint="Usage: /qr <item_id>")
    async def handle_queue_remove(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /qr <id> command - remove specific queue item."""
        try:
            item_id = int(ctx.text)
        except ValueError:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text="Invalid item ID",
                blocks=SlackFormatter.error_message("Invalid item ID. Usage: /qr <item_id>"),
            )
            return

        removed = await deps.db.remove_queue_item(item_id)

        if removed:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
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
                text=f"Item #{item_id} not found or not pending",
                blocks=SlackFormatter.error_message(
                    f"Item #{item_id} not found or is already running/completed."
                ),
            )


async def _process_queue(
    channel_id: str,
    deps: HandlerDependencies,
    client,
    task_logger,
) -> None:
    """Process queue items for a channel sequentially.

    Maintains Claude session continuity across queue items.

    Parameters
    ----------
    channel_id : str
        Slack channel ID.
    deps : HandlerDependencies
        Handler dependencies.
    client
        Slack client for API calls.
    task_logger
        Logger instance.
    """
    log = task_logger or logger

    while True:
        # Get next pending item
        pending = await deps.db.get_pending_queue_items(channel_id)
        if not pending:
            log.info(f"Queue empty for channel {channel_id}, stopping processor")
            break

        item = pending[0]

        # Mark as running
        await deps.db.update_queue_item_status(item.id, "running")

        # Notify channel
        response = await client.chat_postMessage(
            channel=channel_id,
            text=f"Processing queue item #{item.id}",
            blocks=SlackFormatter.queue_item_running(item),
        )
        message_ts = response["ts"]

        try:
            # Get session for working directory and claude session continuity
            # Note: Queue processing uses channel-level session (no thread_ts)
            session = await deps.db.get_or_create_session(
                channel_id, thread_ts=None, default_cwd=config.DEFAULT_WORKING_DIR
            )

            # Execute with session resume for continuity
            result = await deps.executor.execute(
                prompt=item.prompt,
                working_directory=session.working_directory,
                session_id=channel_id,
                resume_session_id=session.claude_session_id,
                execution_id=f"queue_{item.id}",
                permission_mode=session.permission_mode,
                model=session.model,
                channel_id=channel_id,
            )

            # Update Claude session for next item
            if result.session_id:
                await deps.db.update_session_claude_id(channel_id, None, result.session_id)

            # Update queue item
            if result.success:
                await deps.db.update_queue_item_status(item.id, "completed", output=result.output)
            else:
                await deps.db.update_queue_item_status(
                    item.id, "failed", output=result.output, error_message=result.error
                )

            # Update Slack message with result
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"Completed queue item #{item.id}",
                blocks=SlackFormatter.queue_item_complete(item, result),
            )

        except Exception as e:
            log.error(f"Queue item {item.id} failed: {e}")
            await deps.db.update_queue_item_status(item.id, "failed", error_message=str(e))
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"Queue item #{item.id} failed",
                blocks=SlackFormatter.error_message(f"Queue item failed: {e}"),
            )

        # Small delay between items to avoid rate limits
        await asyncio.sleep(0.5)
