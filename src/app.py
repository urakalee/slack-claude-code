#!/usr/bin/env python3
"""
Slack Claude Code Bot - Main Application Entry Point

A Slack app that allows running Claude Code CLI commands from Slack,
with each channel representing a separate session.
"""

import asyncio
import os
import random
import re
import signal
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from loguru import logger
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp
from slack_sdk.errors import SlackApiError

from src.approval.plan_manager import PlanApprovalManager
from src.claude.subprocess_executor import SubprocessExecutor as ClaudeExecutor
from src.codex.subprocess_executor import SubprocessExecutor as CodexExecutor
from src.config import PLANS_DIR, config, get_backend_for_model
from src.database.migrations import init_database
from src.database.repository import DatabaseRepository
from src.handlers import register_commands
from src.handlers.actions import register_actions
from src.handlers.claude.queue import ensure_queue_processor
from src.handlers.command_router import execute_for_session
from src.handlers.response_delivery import deliver_command_response
from src.question.manager import QuestionManager
from src.utils.file_downloader import (
    FileDownloadError,
    FileTooLargeError,
    download_slack_file,
)
from src.utils.formatters.command import (
    error_message,
)
from src.utils.formatters.streaming import processing_message, streaming_update
from src.utils.execution_scope import build_session_scope
from src.utils.streaming import StreamingMessageState, create_streaming_callback


def configure_logging() -> None:
    """Configure log sinks for stderr and data-directory log file."""
    data_dir = Path(config.DATABASE_PATH).expanduser().resolve().parent
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "slack_claude.log"

    logger.remove()
    logger.add(sys.stderr, level="INFO", backtrace=False, diagnose=False)
    logger.add(
        log_path,
        level="DEBUG",
        rotation="00:00",
        retention="3 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )


async def slack_api_with_retry(
    api_call,
    max_retries: int = 3,
    base_delay: float = 1.0,
) -> Any:
    """
    Execute a Slack API call with retry logic for transient failures.

    Handles both SlackApiError and network errors (TimeoutError, CancelledError).

    Args:
        api_call: Async callable that performs the Slack API call
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds for exponential backoff

    Returns:
        The result of the API call

    Raises:
        The last exception if all retries fail
    """
    last_error = None
    for attempt in range(max_retries):
        try:
            return await api_call()
        except asyncio.CancelledError:
            raise
        except (SlackApiError, TimeoutError, OSError) as e:
            last_error = e
            if attempt < max_retries - 1:
                delay = base_delay * (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    f"Slack API error (attempt {attempt + 1}/{max_retries}): "
                    f"{type(e).__name__}: {e}, retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    f"Slack API call failed after {max_retries} attempts: "
                    f"{type(e).__name__}: {e}"
                )
                raise
    raise last_error


async def shutdown(
    claude_executor: ClaudeExecutor,
    codex_executor: CodexExecutor | None = None,
) -> None:
    """Graceful shutdown: cleanup active processes."""
    logger.info("Shutting down - cleaning up active processes...")
    await claude_executor.shutdown()
    if codex_executor:
        await codex_executor.shutdown()
    logger.info("All processes terminated")


async def post_channel_notification(
    client,
    db: DatabaseRepository,
    channel_id: str,
    thread_ts: str | None,
    notification_type: str,
    max_retries: int = 3,
) -> None:
    """
    Post a brief notification to the channel (not thread) to trigger Slack sounds and unread badges.

    Args:
        client: Slack WebClient
        db: Database repository
        channel_id: Slack channel ID
        thread_ts: Thread timestamp (for linking)
        notification_type: "completion" or "permission"
        max_retries: Maximum number of retry attempts (default: 3)
    """
    try:
        settings = await db.get_notification_settings(channel_id)

        if notification_type == "completion" and not settings.notify_on_completion:
            return
        elif notification_type == "permission" and not settings.notify_on_permission:
            return

        # Build thread link if we have a thread_ts
        if thread_ts:
            thread_link = (
                f"https://slack.com/archives/{channel_id}/p{thread_ts.replace('.', '')}"
            )
            if notification_type == "completion":
                message = f"✅ Claude finished • <{thread_link}|View thread>"
            else:
                message = (
                    f"⚠️ Claude needs permission • <{thread_link}|Respond in thread>"
                )
        else:
            if notification_type == "completion":
                message = "✅ Claude finished"
            else:
                message = "⚠️ Claude needs permission"

        await slack_api_with_retry(
            lambda: client.chat_postMessage(
                channel=channel_id,
                text=message,
            ),
            max_retries=max_retries,
        )
        logger.debug(f"Posted {notification_type} notification to channel {channel_id}")

    except Exception as e:
        # Don't fail the main operation if all notification attempts fail
        logger.error(
            f"Failed to post channel notification after {max_retries} attempts: {e}"
        )


async def _route_codex_message_to_active_turn_or_queue(
    client,
    deps,
    session,
    channel_id: str,
    thread_ts: str | None,
    prompt: str,
    logger,
) -> bool:
    """Route a Codex message to an active turn, or queue it on steer failure.

    Returns
    -------
    bool
        True when the message was handled by steer or queue fallback, False when
        no active turn exists and normal execution should continue.
    """
    if not deps.codex_executor:
        return False

    session_scope = build_session_scope(channel_id, thread_ts)
    if not await deps.codex_executor.has_active_turn(session_scope):
        return False

    cmd_history = await deps.db.add_command(session.id, prompt)
    await deps.db.update_command_status(cmd_history.id, "running")

    steer_error: str | None = None
    steer_result = None
    try:
        steer_result = await deps.codex_executor.steer_active_turn(
            session_scope=session_scope,
            text=prompt,
        )
    except Exception as e:
        steer_error = str(e)
        logger.error(
            f"Failed to steer active Codex turn in scope {session_scope}: {steer_error}"
        )

    if steer_result and steer_result.success:
        await deps.db.update_command_status(
            cmd_history.id,
            "completed",
            output=(
                "Routed to active Codex turn via turn/steer."
                f" turn_id={steer_result.turn_id or 'unknown'}"
            ),
        )
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Message merged into active Codex execution.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":compass: Routed your message to the active Codex run "
                            "using `turn/steer`."
                        ),
                    },
                }
            ],
        )
        return True

    steer_error = steer_error or (
        steer_result.error if steer_result else "unknown error"
    )
    try:
        queued_item = await deps.db.add_to_queue(
            session_id=session.id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            prompt=prompt,
        )
        await deps.codex_executor.record_queue_fallback(success=True)
    except Exception as e:
        await deps.codex_executor.record_queue_fallback(success=False)
        queue_error = str(e)
        await deps.db.update_command_status(
            cmd_history.id,
            "failed",
            output=(
                "Steer failed and queue fallback failed."
                f" steer_error={steer_error} queue_error={queue_error}"
            ),
            error_message=queue_error,
        )
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text="Failed to queue message after steer failure.",
            blocks=error_message(
                "Active Codex run could not be steered and queue fallback failed.\n"
                f"steer_error: {steer_error}\nqueue_error: {queue_error}"
            ),
        )
        return True

    await deps.db.update_command_status(
        cmd_history.id,
        "completed",
        output=(
            f"Steer failed ({steer_error}). " f"Auto-queued item #{queued_item.id}."
        ),
    )
    await ensure_queue_processor(
        channel_id=channel_id,
        thread_ts=thread_ts,
        deps=deps,
        client=client,
        task_logger=logger,
    )
    await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=f"Steer unavailable; queued message as item #{queued_item.id}.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":inbox_tray: Active Codex run is busy and steering failed.\n"
                        f"Queued as item *#{queued_item.id}* in this session scope."
                    ),
                },
            }
        ],
    )
    return True


async def _execute_codex_message(
    client,
    deps,
    session,
    channel_id: str,
    thread_ts: str | None,
    prompt: str,
    logger,
    user_id: str | None = None,
) -> None:
    """Execute a message using the Codex backend."""
    # Create command history entry
    cmd_history = await deps.db.add_command(session.id, prompt)
    await deps.db.update_command_status(cmd_history.id, "running")

    # Send initial processing message
    response = await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=f"Processing: {prompt[:100]}...",
        blocks=processing_message(prompt),
    )
    message_ts = response["ts"]

    # Setup streaming state
    execution_id = str(uuid.uuid4())

    async def on_streaming_error(error_msg: str) -> None:
        """Handle streaming errors by posting to Slack channel."""
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f":warning: {error_msg}",
            )
        except Exception as e:
            logger.error(f"Failed to post streaming error notification: {e}")

    streaming_state = StreamingMessageState(
        channel_id=channel_id,
        message_ts=message_ts,
        prompt=prompt,
        client=client,
        logger=logger,
        track_tools=True,
        smart_concat=True,
        db_session_id=session.id,
        on_error=on_streaming_error,
    )
    streaming_state.start_heartbeat()
    on_chunk = create_streaming_callback(streaming_state)

    try:
        if not deps.codex_executor:
            raise RuntimeError("Codex executor is not configured")
        logger.info("Executing Codex prompt via command router")
        route = await execute_for_session(
            deps=deps,
            session=session,
            prompt=prompt,
            channel_id=channel_id,
            thread_ts=thread_ts,
            execution_id=execution_id,
            on_chunk=on_chunk,
            slack_client=client,
            user_id=user_id,
            logger=logger,
        )
        result = route.result

        # Update command history after any question loops / plan execution.
        if result.success:
            await deps.db.update_command_status(
                cmd_history.id, "completed", result.output
            )
        else:
            await deps.db.update_command_status(
                cmd_history.id, "failed", result.output, result.error
            )

        # Stop heartbeat before final response
        await streaming_state.stop_heartbeat()

        # Send final response
        output = result.output or result.error or "No output"

        await deliver_command_response(
            client=client,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            prompt=prompt,
            output=output,
            command_id=cmd_history.id,
            duration_ms=result.duration_ms,
            cost_usd=result.cost_usd,
            is_error=not result.success,
            logger=logger,
            api_with_retry=slack_api_with_retry,
        )

    except asyncio.CancelledError:
        logger.info("Codex command execution was cancelled")
        await streaming_state.stop_heartbeat()
        await QuestionManager.cancel_for_session(str(session.id))
        await deps.db.update_command_status(
            cmd_history.id, "cancelled", error_message="Cancelled"
        )
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text="Command cancelled",
            blocks=error_message("Command was cancelled"),
        )
    except SlackApiError as e:
        logger.error(
            f"Slack API error executing Codex command: {e}\n{traceback.format_exc()}"
        )
        await streaming_state.stop_heartbeat()
        await QuestionManager.cancel_for_session(str(session.id))
        await deps.db.update_command_status(
            cmd_history.id, "failed", error_message=str(e)
        )
        try:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f":x: Slack API Error: {str(e)[:200]}",
                blocks=error_message(f"Slack API Error: {str(e)}"),
            )
        except Exception as notify_error:
            logger.error(f"Failed to post Slack API error notification: {notify_error}")
    except Exception as e:
        logger.error(
            f"Error executing Codex command: {type(e).__name__}: {e}\n{traceback.format_exc()}"
        )
        await streaming_state.stop_heartbeat()
        await QuestionManager.cancel_for_session(str(session.id))
        await deps.db.update_command_status(
            cmd_history.id, "failed", error_message=str(e)
        )
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            text=f"Error: {str(e)}",
            blocks=error_message(str(e)),
        )


async def main():
    """Main application entry point."""
    configure_logging()

    # Validate configuration
    errors = config.validate_required()
    if errors:
        logger.error("Configuration errors:")
        for error in errors:
            logger.error(f"  - {error}")
        sys.exit(1)

    # Initialize database
    logger.info(f"Initializing database at {config.DATABASE_PATH}")
    await init_database(config.DATABASE_PATH)

    # Create app components
    db = DatabaseRepository(config.DATABASE_PATH)

    # Initialize Claude executor (always available)
    claude_executor = ClaudeExecutor(db=db)

    # Initialize Codex executor (optional)
    codex_executor = CodexExecutor(db=db)

    # Create Slack app
    app = AsyncApp(
        token=config.SLACK_BOT_TOKEN,
        signing_secret=config.SLACK_SIGNING_SECRET,
    )

    # Register handlers (both Claude and Codex)
    deps = register_commands(
        app,
        db,
        claude_executor,
        codex_executor=codex_executor,
    )
    register_actions(app, deps)

    # Add a simple health check
    @app.event("app_mention")
    async def handle_mention(event, say, logger):
        """Respond to @mentions."""
        await say(
            text="Hi! I'm Claude Code Bot. Just send me a message to run commands."
        )

    @app.event("message")
    async def handle_message(event, client, logger):
        """Handle messages and pipe them to Claude Code."""
        logger.info(f"Message event received: {event.get('text', '')[:50]}...")

        # Ignore bot messages to avoid responding to ourselves
        if event.get("bot_id"):
            logger.debug(f"Ignoring bot message: bot_id={event.get('bot_id')}")
            return

        # Ignore system subtypes but allow user messages with subtypes (e.g., file_share from mobile)
        ignored_subtypes = {
            "bot_message",
            "message_changed",
            "message_deleted",
            "channel_join",
            "channel_leave",
            "channel_topic",
            "channel_purpose",
            "channel_name",
            "channel_archive",
            "channel_unarchive",
            "ekm_access_denied",
            "me_message",
        }
        subtype = event.get("subtype")
        if subtype and subtype in ignored_subtypes:
            logger.debug(f"Ignoring system subtype message: subtype={subtype}")
            return

        channel_id = event.get("channel")
        user_id = event.get("user")  # Extract user ID for plan approval
        thread_ts = event.get("thread_ts")  # Extract thread timestamp
        prompt = event.get("text", "").strip()
        # Extract uploaded files - ensure it's always a list
        files_data = event.get("files")
        files: list = files_data if isinstance(files_data, list) else []

        # Allow messages with files but no text
        if not prompt and not files:
            logger.debug("Empty prompt and no files, ignoring")
            return

        # Get or create session (thread-aware)
        session = await deps.db.get_or_create_session(
            channel_id, thread_ts=thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )
        logger.info(f"Using session: {session.session_display_name()}")

        # Cancel any pending questions for this session - unblocks handlers stuck
        # at wait_for_answer() when user sends a new message instead of clicking buttons
        cancelled_questions = await QuestionManager.cancel_for_session(str(session.id))
        if cancelled_questions:
            logger.info(
                f"Cancelled {cancelled_questions} pending question(s) for session {session.id} "
                "due to new message"
            )
        cancelled_plan_approvals = await PlanApprovalManager.cancel_for_session(
            str(session.id)
        )
        if cancelled_plan_approvals:
            logger.info(
                f"Cancelled {cancelled_plan_approvals} pending plan approval(s) for session {session.id} "
                "due to new message"
            )

        # Validate working directory exists
        if not os.path.isdir(session.working_directory):
            logger.error(
                f"Working directory does not exist: {session.working_directory}"
            )
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"⚠️ Working directory does not exist: `{session.working_directory}`\n\nUse `/cd <path>` to set a valid working directory.",
            )
            return

        # Process file uploads
        uploaded_files = []
        if files:
            logger.info(f"Processing {len(files)} uploaded file(s)")

            # Create .slack_uploads directory in session working directory
            uploads_dir = os.path.join(session.working_directory, ".slack_uploads")
            os.makedirs(uploads_dir, exist_ok=True)

            for file_info in files:
                try:
                    file_name = file_info.get("name", "unknown")
                    file_id = file_info["id"]
                    logger.info(f"Processing file: {file_name}")

                    # download_slack_file handles both regular files and snippets
                    # It detects snippets from the full file info and extracts content
                    local_path, metadata = await download_slack_file(
                        client=client,
                        file_id=file_id,
                        slack_bot_token=config.SLACK_BOT_TOKEN,
                        destination_dir=uploads_dir,
                        max_size_bytes=config.MAX_FILE_SIZE_MB * 1024 * 1024,
                    )

                    # Track in database
                    uploaded_file = await deps.db.add_uploaded_file(
                        session_id=session.id,
                        slack_file_id=file_id,
                        filename=file_name,
                        local_path=local_path,
                        mimetype=file_info.get("mimetype", ""),
                        size=metadata.get("size", file_info.get("size", 0)),
                    )
                    uploaded_files.append(uploaded_file)
                    logger.info(f"File processed and tracked: {local_path}")

                    # For images, show thumbnail in thread
                    if file_info.get("mimetype", "").startswith("image/"):
                        thumb_url = file_info.get("thumb_360") or file_info.get(
                            "thumb_160"
                        )
                        if thumb_url:
                            await client.chat_postMessage(
                                channel=channel_id,
                                thread_ts=thread_ts
                                or event.get("ts"),  # Use message ts if not in thread
                                text=f"📎 Uploaded: {file_info['name']}",
                                blocks=[
                                    {
                                        "type": "section",
                                        "text": {
                                            "type": "mrkdwn",
                                            "text": f":frame_with_picture: Uploaded image: *{file_info['name']}*",
                                        },
                                    },
                                    {
                                        "type": "image",
                                        "image_url": thumb_url,
                                        "alt_text": file_info["name"],
                                    },
                                ],
                            )

                except FileTooLargeError as e:
                    logger.warning(f"File too large: {file_info.get('name')} - {e}")
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts or event.get("ts"),
                        text=f"⚠️ File too large: {file_info['name']} ({e.size_mb:.1f}MB, max: {e.max_mb}MB)",
                    )
                except FileDownloadError as e:
                    logger.error(f"File download failed: {file_info.get('name')} - {e}")
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts or event.get("ts"),
                        text=f"⚠️ Failed to download file: {file_info['name']} - {str(e)}",
                    )
                except Exception as e:
                    logger.error(
                        f"Unexpected error processing file {file_info.get('name')}: {e}\n{traceback.format_exc()}"
                    )
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts or event.get("ts"),
                        text=f"⚠️ Error processing file: {file_info['name']} - {str(e)}",
                    )

        # Enhance prompt with uploaded file references
        if uploaded_files:
            file_refs = "\n".join(
                [f"- {f.filename} (at {f.local_path})" for f in uploaded_files]
            )

            if prompt:
                prompt = f"{prompt}\n\nUploaded files:\n{file_refs}"
            else:
                # No text, only files - provide default prompt
                prompt = f"Please analyze these uploaded files:\n{file_refs}"

        # Determine which backend to use based on session model
        backend = get_backend_for_model(session.model)
        logger.info(f"Using backend: {backend} (model: {session.model})")

        # Route to appropriate execution path
        if backend == "codex":
            handled_active_turn = await _route_codex_message_to_active_turn_or_queue(
                client=client,
                deps=deps,
                session=session,
                channel_id=channel_id,
                thread_ts=thread_ts,
                prompt=prompt,
                logger=logger,
            )
            if handled_active_turn:
                return

            await _execute_codex_message(
                client=client,
                deps=deps,
                session=session,
                channel_id=channel_id,
                thread_ts=thread_ts,
                prompt=prompt,
                logger=logger,
                user_id=user_id,
            )
            return

        # Claude backend - continue with Claude-specific execution below

        # Create command history entry
        cmd_history = await deps.db.add_command(session.id, prompt)
        await deps.db.update_command_status(cmd_history.id, "running")

        # Send initial processing message (in thread if applicable)
        response = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,  # Reply in thread if this is a thread message
            text=f"Processing: {prompt[:100]}...",  # Fallback for notifications
            blocks=processing_message(prompt),
        )
        message_ts = response["ts"]

        # Setup streaming state with tool tracking
        execution_id = str(uuid.uuid4())

        # Error callback for streaming failures - posts error to channel
        async def on_streaming_error(error_msg: str) -> None:
            """Handle streaming errors by posting to Slack channel."""
            try:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=f":warning: {error_msg}",
                )
            except Exception as e:
                logger.error(f"Failed to post streaming error notification: {e}")

        streaming_state = StreamingMessageState(
            channel_id=channel_id,
            message_ts=message_ts,
            prompt=prompt,
            client=client,
            logger=logger,
            track_tools=True,
            smart_concat=True,
            db_session_id=session.id,
            on_error=on_streaming_error,
        )
        # Start heartbeat to show progress during idle periods
        streaming_state.start_heartbeat()
        pending_question = None  # Track if we detect an AskUserQuestion

        # Factory function to create on_chunk callback with proper closures
        def create_on_chunk_callback(state: StreamingMessageState):
            async def on_chunk(msg):
                nonlocal pending_question

                # Detect AskUserQuestion tool before updating state
                if msg.tool_activities:
                    for tool in msg.tool_activities:
                        logger.debug(
                            f"Tool activity: {tool.name} (id={tool.id[:8]}..., result={'has result' if tool.result else 'None'})"
                        )
                        if tool.name == "AskUserQuestion" and tool.result is None:
                            if tool.id not in state.tool_activities:
                                # Create pending question when we first see the tool
                                pending_question = (
                                    await QuestionManager.create_pending_question(
                                        session_id=str(session.id),
                                        channel_id=channel_id,
                                        thread_ts=thread_ts,
                                        tool_use_id=tool.id,
                                        tool_input=tool.input,
                                    )
                                )
                                logger.info(
                                    f"Detected AskUserQuestion tool, created pending question {pending_question.question_id}"
                                )
                            else:
                                logger.debug(
                                    f"AskUserQuestion tool {tool.id[:8]}... already tracked, skipping"
                                )

                # Update streaming state
                content = msg.content if msg.type == "assistant" else ""
                tools = msg.tool_activities
                if content or tools:
                    await state.append_and_update(content or "", tools)

            return on_chunk

        on_chunk = create_on_chunk_callback(streaming_state)

        # In plan mode, ask Claude to report where it wrote the plan file
        # The Claude CLI has a designated plan file path that only it can write to
        # We can't specify a custom path - Claude must use its designated file
        execution_prompt = prompt
        if session.permission_mode == "plan":
            # Ensure the plans directory exists (Claude's designated file will be here)
            plans_dir = PLANS_DIR
            os.makedirs(plans_dir, exist_ok=True)
            execution_prompt = (
                f"{prompt}\n\n"
                f"[Plan mode: After writing your plan to the designated plan file, "
                f"include a single line in your response exactly:\n"
                f"Created Plan: <full path to plan file>]"
            )
            logger.info(
                f"Plan mode: session {session.id} execution {execution_id}, "
                "using Claude's designated plan file"
            )

        try:
            result = await claude_executor.execute(
                prompt=execution_prompt,
                working_directory=session.working_directory,
                session_id=build_session_scope(channel_id, thread_ts),
                resume_session_id=session.claude_session_id,  # Resume previous session if exists
                execution_id=execution_id,
                on_chunk=on_chunk,
                permission_mode=session.permission_mode,  # Use session's mode (falls back to config)
                db_session_id=session.id,  # Pass for smart context tracking
                model=session.model,  # Use session's selected model
                channel_id=channel_id,  # For channel-specific cancellation
                thread_ts=thread_ts,
            )

            # Update session with Claude session ID for resume
            if result.session_id:
                await deps.db.update_session_claude_id(
                    channel_id, thread_ts, result.session_id
                )

            # Handle AskUserQuestion - loop to handle multiple questions
            question_count = 0
            max_questions = config.timeouts.execution.max_questions_per_conversation
            while (
                pending_question
                and result.session_id
                and question_count < max_questions
            ):
                question_count += 1
                logger.info(
                    "Claude asked a question, posting to Slack and waiting for response"
                )

                # Update the main message to show Claude is waiting
                try:
                    text_preview = (
                        streaming_state.accumulated_output[:100] + "..."
                        if len(streaming_state.accumulated_output) > 100
                        else streaming_state.accumulated_output
                    )
                    await client.chat_update(
                        channel=channel_id,
                        ts=message_ts,
                        text=text_preview,
                        blocks=streaming_update(
                            prompt,
                            streaming_state.accumulated_output
                            + "\n\n_Waiting for your response..._",
                            tool_activities=streaming_state.get_tool_list(),
                        ),
                    )
                except Exception as e:
                    logger.warning(f"Failed to update message: {e}")

                # Post the question to Slack with context from Claude's message
                await QuestionManager.post_question_to_slack(
                    pending_question,
                    client,
                    deps.db,
                    context_text=streaming_state.accumulated_output,
                )

                # Wait for user to answer (no timeout)
                answers = await QuestionManager.wait_for_answer(
                    pending_question.question_id,
                )

                if answers:
                    # User answered - format and send as follow-up to Claude
                    answer_text = QuestionManager.format_answer_for_claude(
                        pending_question
                    )
                    logger.info(
                        f"User answered question, sending to Claude: {answer_text[:100]}"
                    )

                    # Reset pending_question before continuing - on_chunk may set a new one
                    pending_question = None

                    # Stop the old heartbeat
                    await streaming_state.stop_heartbeat()

                    # Post a new message below the answered question for continued streaming
                    continue_response = await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text="Continuing...",
                        blocks=processing_message("Processing your answer..."),
                    )
                    new_message_ts = continue_response["ts"]

                    # Create new streaming state for the continuation
                    streaming_state = StreamingMessageState(
                        channel_id=channel_id,
                        message_ts=new_message_ts,
                        prompt=prompt,
                        client=client,
                        logger=logger,
                        track_tools=True,
                        smart_concat=True,
                        db_session_id=session.id,
                        on_error=on_streaming_error,
                    )
                    streaming_state.start_heartbeat()

                    # Create new on_chunk callback for new streaming state
                    on_chunk = create_on_chunk_callback(streaming_state)

                    # Continue the conversation with the user's answer
                    # This will resume the session
                    result = await claude_executor.execute(
                        prompt=answer_text,
                        working_directory=session.working_directory,
                        session_id=build_session_scope(channel_id, thread_ts),
                        resume_session_id=result.session_id,  # Resume the same session
                        execution_id=str(uuid.uuid4()),
                        on_chunk=on_chunk,  # Use updated chunk handler
                        permission_mode=session.permission_mode,
                        db_session_id=session.id,
                        model=session.model,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                    )

                    # Update the new message_ts for any future operations
                    message_ts = new_message_ts

                    # Update session with new Claude session ID
                    if result.session_id:
                        await deps.db.update_session_claude_id(
                            channel_id, thread_ts, result.session_id
                        )
                    # Loop continues - will check if pending_question was set by on_chunk
                else:
                    # Question was cancelled - update message and break
                    logger.info("Question was cancelled")
                    result.output = (
                        streaming_state.accumulated_output
                        + "\n\n_Question was cancelled._"
                    )
                    result.success = False
                    break

            # Check if we hit the question limit
            if question_count >= max_questions and pending_question:
                logger.warning(
                    f"Hit maximum question limit ({max_questions}), stopping question loop"
                )
                result.output = (
                    streaming_state.accumulated_output
                    + f"\n\n_Reached maximum question limit ({max_questions}). Please start a new conversation._"
                )
                result.success = False

            # Update command history
            if result.success:
                await deps.db.update_command_status(
                    cmd_history.id, "completed", result.output
                )
            else:
                await deps.db.update_command_status(
                    cmd_history.id, "failed", result.output, result.error
                )

            # Check if plan mode should request approval before exiting
            # If we're in plan mode and ExitPlanMode was called, request user approval
            if result.has_pending_plan_approval:
                logger.info("ExitPlanMode detected, requesting user approval")

                # Get plan content - prefer the plan file on disk, then fall back to
                # Plan subagent output if needed. This avoids attaching unrelated text.
                plan_file_path = None
                plan_content = ""
                # Prefer plan files written during this session.
                # Retry with small delays because the file may not be flushed to disk yet
                # when we terminate the subprocess immediately after detecting ExitPlanMode.
                plan_start_time = streaming_state.started_at
                plan_override_path = None
                plan_override_source = None
                plan_override_regex = re.compile(
                    r"(?im)^(?:Plan file|Created Plan):\s*(.+)$"
                )
                for source_label, source_text in (
                    ("Plan agent output", result.plan_subagent_result),
                    ("assistant output", result.output),
                ):
                    if not source_text:
                        continue
                    matches = plan_override_regex.findall(source_text)
                    if matches:
                        raw_path = matches[-1].strip().strip("`\"'")
                        if raw_path:
                            plan_override_path = os.path.expanduser(raw_path)
                            plan_override_source = source_label
                            logger.info(
                                f"Plan override path declared by {source_label}: {plan_override_path}"
                            )
                            break
                logger.info(
                    "Plan discovery order: "
                    f"override_path={plan_override_path or 'none'}"
                    f"{f' ({plan_override_source})' if plan_override_source else ''} -> "
                    "write_or_edit_activity(any .md; prefer ~/.claude/plans) -> "
                    "plan_subagent_result(fallback)"
                )
                logger.info(
                    "Plan discovery timing: "
                    f"start_time={plan_start_time:.0f}, "
                    "grace_seconds=10.0"
                )
                max_retries = 20
                retry_delay = 0.5  # seconds (20 * 0.5 = 10 seconds max)

                for attempt in range(max_retries):
                    # Explicit override from Plan agent output (if provided)
                    if plan_override_path and os.path.isfile(plan_override_path):
                        try:
                            mtime = os.path.getmtime(plan_override_path)
                            if mtime >= plan_start_time:
                                with open(plan_override_path) as f:
                                    plan_content = f.read()
                                if plan_content:
                                    plan_file_path = plan_override_path
                                    logger.info(
                                        f"Plan file read successfully from override path on attempt {attempt + 1}: "
                                        f"{plan_override_path} (mtime={mtime:.0f})"
                                    )
                                    break
                        except PermissionError:
                            logger.warning(
                                f"Cannot read plan file (permission denied): {plan_override_path}"
                            )
                            break  # Don't retry permission errors
                        except Exception as e:
                            logger.warning(
                                f"Failed to read plan file {plan_override_path}: {e}"
                            )

                    # Prefer any plan file written during this session via Write tools
                    candidate_path = streaming_state.get_recent_plan_write_path(
                        plan_start_time - 5.0
                    )
                    if candidate_path:
                        try:
                            mtime = os.path.getmtime(candidate_path)
                            with open(candidate_path) as f:
                                plan_content = f.read()
                            if plan_content:
                                plan_file_path = candidate_path
                                logger.info(
                                    f"Plan file read successfully from Write activity on attempt {attempt + 1}: "
                                    f"{candidate_path} (mtime={mtime:.0f})"
                                )
                                break
                        except PermissionError:
                            logger.warning(
                                f"Cannot read plan file (permission denied): {candidate_path}"
                            )
                            break  # Don't retry permission errors
                        except Exception as e:
                            logger.warning(
                                f"Failed to read plan file {candidate_path}: {e}"
                            )

                    # Wait before retrying (except on last attempt)
                    if attempt < max_retries - 1:
                        logger.debug(
                            f"Plan file not ready, waiting {retry_delay}s before retry "
                            f"({attempt + 1}/{max_retries})"
                        )
                        await asyncio.sleep(retry_delay)

                # FALLBACK: If no plan file was written but we have Plan subagent output,
                # persist it to a new plan file to avoid attaching stale content.
                if not plan_content and result.plan_subagent_result:
                    logger.warning(
                        "No plan file written by tools; persisting Plan subagent output as fallback"
                    )
                    fallback_dir = PLANS_DIR
                    os.makedirs(fallback_dir, exist_ok=True)
                    fallback_name = (
                        f"plan-session-{session.id}-fallback-{int(time.time())}.md"
                    )
                    fallback_path = os.path.join(fallback_dir, fallback_name)
                    try:
                        with open(fallback_path, "w") as f:
                            f.write(result.plan_subagent_result)
                        plan_file_path = fallback_path
                        plan_content = result.plan_subagent_result
                        logger.info(
                            f"Saved Plan subagent output to fallback file {fallback_path}"
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to save Plan subagent output to file: {e}"
                        )

                # If still no plan content, show error
                if not plan_content:
                    logger.warning(
                        "No plan file found for approval. "
                        f"Searched path: {plan_file_path}, working_dir: {session.working_directory}"
                    )
                    if result.plan_write_timeout:
                        plan_content = (
                            "⚠️ Plan not ready yet — timed out waiting for Claude to write the plan file.\n\n"
                            "Claude should have written a plan to a markdown file before requesting approval. "
                            "Please check that the plan was written correctly."
                        )
                    else:
                        plan_content = (
                            "⚠️ No plan file was found.\n\n"
                            "Claude should have written a plan to a markdown file before requesting approval. "
                            "Please check that the plan was written correctly."
                        )

                if plan_file_path and os.path.isfile(plan_file_path):
                    try:
                        mtime = os.path.getmtime(plan_file_path)
                        age = time.time() - mtime
                        logger.info(
                            f"Selected plan file for approval: {plan_file_path} "
                            f"(mtime={mtime:.0f}, age={age:.1f}s)"
                        )
                    except OSError as e:
                        logger.warning(
                            f"Failed to stat selected plan file {plan_file_path}: {e}"
                        )

                # Request approval via Slack buttons and wait for response
                approved = await PlanApprovalManager.request_approval(
                    session_id=str(session.id),
                    channel_id=channel_id,
                    plan_content=plan_content,
                    claude_session_id=result.session_id or "",
                    prompt=prompt,
                    user_id=user_id,
                    thread_ts=thread_ts,
                    slack_client=client,
                    plan_file_path=plan_file_path,
                )

                if approved:
                    logger.info("Plan approved, switching session to bypass mode")
                    await deps.db.update_session_mode(
                        channel_id, thread_ts, config.DEFAULT_BYPASS_MODE
                    )

                    # Post initial message and automatically execute the plan
                    exec_response = await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text="Plan approved - executing...",
                        blocks=processing_message(
                            ":white_check_mark: *Plan approved!* Executing implementation..."
                        ),
                    )
                    exec_message_ts = exec_response["ts"]

                    # Create new streaming state for execution
                    streaming_state = StreamingMessageState(
                        channel_id=channel_id,
                        message_ts=exec_message_ts,
                        prompt="[Plan Execution]",
                        client=client,
                        logger=logger,
                        track_tools=True,
                        smart_concat=True,
                        db_session_id=session.id,
                        on_error=on_streaming_error,
                    )
                    streaming_state.start_heartbeat()
                    on_chunk = create_on_chunk_callback(streaming_state)

                    # Resume Claude session to execute the plan
                    result = await claude_executor.execute(
                        prompt="Plan approved. Please proceed with the implementation.",
                        working_directory=session.working_directory,
                        session_id=build_session_scope(channel_id, thread_ts),
                        resume_session_id=result.session_id,
                        execution_id=str(uuid.uuid4()),
                        on_chunk=on_chunk,
                        permission_mode=config.DEFAULT_BYPASS_MODE,
                        db_session_id=session.id,
                        model=session.model,
                        channel_id=channel_id,
                        thread_ts=thread_ts,
                    )

                    # Update message_ts for final response
                    message_ts = exec_message_ts

                    # Update session with new Claude session ID
                    if result.session_id:
                        await deps.db.update_session_claude_id(
                            channel_id, thread_ts, result.session_id
                        )
                else:
                    logger.info("Plan rejected or timed out, staying in plan mode")
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text="Plan not approved - staying in plan mode",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": ":x: *Plan not approved.* Staying in plan mode.\n\n_Provide feedback to revise the plan, or use `/mode bypass` to switch modes manually._",
                                },
                            }
                        ],
                    )

            # Stop heartbeat before sending final response
            await streaming_state.stop_heartbeat()

            # Send final response
            output = result.output or result.error or "No output"

            await deliver_command_response(
                client=client,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=message_ts,
                prompt=prompt,
                output=output,
                command_id=cmd_history.id,
                duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
                is_error=not result.success,
                logger=logger,
                detailed_output=result.detailed_output,
                post_detail_button=True,
                notify_on_snippet_failure=True,
                api_with_retry=slack_api_with_retry,
            )

        except asyncio.CancelledError:
            logger.info("Command execution was cancelled")
            await streaming_state.stop_heartbeat()
            if pending_question:
                await QuestionManager.cancel(pending_question.question_id)
            await deps.db.update_command_status(
                cmd_history.id, "cancelled", error_message="Cancelled"
            )
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text="Command cancelled",
                blocks=error_message("Command was cancelled"),
            )
        except SlackApiError as e:
            logger.error(
                f"Slack API error executing command: {e}\n{traceback.format_exc()}"
            )
            await streaming_state.stop_heartbeat()
            if pending_question:
                await QuestionManager.cancel(pending_question.question_id)
            await deps.db.update_command_status(
                cmd_history.id, "failed", error_message=str(e)
            )
            # Try to post a new error message instead of updating (in case update is failing)
            try:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=f":x: Slack API Error: {str(e)[:200]}",
                    blocks=error_message(f"Slack API Error: {str(e)}"),
                )
            except Exception as notify_error:
                logger.error(
                    f"Failed to post Slack API error notification: {notify_error}"
                )
        except (OSError, IOError) as e:
            logger.error(f"I/O error executing command: {e}\n{traceback.format_exc()}")
            await streaming_state.stop_heartbeat()
            if pending_question:
                await QuestionManager.cancel(pending_question.question_id)
            await deps.db.update_command_status(
                cmd_history.id, "failed", error_message=str(e)
            )
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"I/O Error: {str(e)}",
                blocks=error_message(f"I/O Error: {str(e)}"),
            )
        except Exception as e:
            # Catch remaining unexpected errors to prevent crash, but log details
            logger.error(
                f"Unexpected error executing command: {type(e).__name__}: {e}\n{traceback.format_exc()}"
            )
            await streaming_state.stop_heartbeat()
            if pending_question:
                await QuestionManager.cancel(pending_question.question_id)
            await deps.db.update_command_status(
                cmd_history.id, "failed", error_message=str(e)
            )
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                text=f"Error: {str(e)}",
                blocks=error_message(str(e)),
            )

    # Start Socket Mode handler
    handler = AsyncSocketModeHandler(app, config.SLACK_APP_TOKEN)

    # Setup shutdown handler
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def signal_handler():
        logger.info("Received shutdown signal")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    logger.info("Starting Slack Claude Code Bot...")
    logger.info(f"Default working directory: {config.DEFAULT_WORKING_DIR}")

    # Start the handler
    await handler.connect_async()
    logger.info("Connected to Slack")

    # Wait for shutdown signal
    await shutdown_event.wait()

    # Cleanup
    await shutdown(claude_executor, codex_executor)
    await handler.close_async()


def run():
    # Check for subcommands (e.g., aislack config ...)
    if len(sys.argv) > 1 and sys.argv[0].endswith(
        ("aislack", "aislack.exe", "ccslack", "ccslack.exe")
    ):
        subcommand = sys.argv[1].lower()
        if subcommand == "config":
            # Forward to config CLI with remaining args
            sys.argv = sys.argv[1:]  # Remove 'aislack' from argv
            from src.cli import run as config_run

            return config_run()

    asyncio.run(main())


if __name__ == "__main__":
    run()
