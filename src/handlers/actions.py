"""Interactive component action handlers."""

import asyncio
import json
import re
import uuid
from typing import Any

from slack_bolt.async_app import AsyncApp

from src.approval.handler import PermissionManager
from src.approval.plan_manager import PlanApprovalManager
from src.approval.slack_ui import build_approval_result_blocks, build_plan_result_blocks
from src.claude.streaming import ToolActivity
from src.config import config
from src.git.service import GitError, GitService
from src.question.manager import QuestionManager
from src.question.slack_ui import (
    build_custom_answer_modal,
    build_question_result_blocks,
)
from src.utils.detail_cache import DetailCache
from src.utils.formatters.base import markdown_to_mrkdwn
from src.utils.formatters.command import command_response_with_tables, error_message
from src.utils.formatters.job import parallel_job_status, sequential_job_status
from src.utils.formatters.streaming import processing_message
from src.utils.formatters.tool_blocks import format_tool_detail_blocks
from src.utils.model_selection import (
    codex_model_validation_error,
    model_display_name,
    normalize_model_name,
)
from src.utils.streaming import StreamingMessageState, create_streaming_callback

from .base import CommandContext, HandlerDependencies
from .claude.worktree import _handle_merge, _handle_remove, _handle_switch
from .command_router import execute_for_session


async def _get_git_commit_hash(working_directory: str) -> str | None:
    """Get the current git commit hash asynchronously.

    Parameters
    ----------
    working_directory : str
        The working directory of the git repo.

    Returns
    -------
    str | None
        The commit hash or None if unavailable.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        if process.returncode != 0:
            return None
        return stdout.decode().strip()
    except (asyncio.TimeoutError, Exception):
        return None


async def _get_github_file_url(tool: ToolActivity, working_directory: str) -> str | None:
    """Generate a GitHub URL for viewing a file.

    Parameters
    ----------
    tool : ToolActivity
        The tool activity containing file path.
    working_directory : str
        The working directory to determine relative path.

    Returns
    -------
    str | None
        The GitHub URL or None if not applicable.
    """
    if not config.GITHUB_REPO:
        return None

    # Only supported for Read, Edit, Write tools
    if tool.name not in ("Read", "Edit", "Write"):
        return None

    file_path = tool.input.get("file_path")
    if not file_path:
        return None

    # Get relative path from working directory
    if file_path.startswith(working_directory):
        relative_path = file_path[len(working_directory) :].lstrip("/")
    else:
        relative_path = file_path.lstrip("/")

    # Get current commit hash
    commit_hash = await _get_git_commit_hash(working_directory)
    if not commit_hash:
        return None

    # Generate GitHub URL
    return f"https://github.com/{config.GITHUB_REPO}/blob/{commit_hash}/{relative_path}"


async def _get_github_diff_url(working_directory: str) -> str | None:
    """Generate a GitHub URL for viewing the latest commit diff.

    Parameters
    ----------
    working_directory : str
        The working directory of the git repo.

    Returns
    -------
    str | None
        The GitHub diff URL or None if not applicable.
    """
    if not config.GITHUB_REPO:
        return None

    # Get current commit hash
    commit_hash = await _get_git_commit_hash(working_directory)
    if not commit_hash:
        return None

    # Generate GitHub commit URL (shows diff)
    return f"https://github.com/{config.GITHUB_REPO}/commit/{commit_hash}"


async def _handle_approval_action(
    body: dict,
    action: dict,
    client,
    logger,
    resolver,
    block_builder,
    approval_type: str,
) -> None:
    """Generic handler for approval/denial actions.

    Parameters
    ----------
    body : dict
        Slack action body.
    action : dict
        Action data containing approval_id.
    client : Any
        Slack WebClient.
    logger : Any
        Logger instance.
    resolver : Callable
        Async function to resolve approval, returns resolved data or None.
    block_builder : Callable
        Function to build result blocks from resolved data and user_id.
    approval_type : str
        Type for logging/error messages (e.g., "Tool", "Plan").
    """
    try:
        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        user_id = body["user"]["id"]
    except KeyError as e:
        logger.error(f"Missing required field in action body: {e}")
        return

    approval_id = action["value"]

    resolved = await resolver(approval_id, user_id)

    if resolved:
        try:
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=block_builder(resolved, user_id),
            )
        except Exception as e:
            logger.warning(f"Failed to update {approval_type.lower()} message: {e}")
        logger.info(f"{approval_type} {approval_id} resolved by {user_id}")
    else:
        await client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            text=f"{approval_type} request `{approval_id}` not found or already resolved.",
        )


async def _update_question_result_message(
    client: Any,
    logger: Any,
    resolved: Any,
    user_id: str,
    channel_id: str,
    message_ts: str,
) -> None:
    """Update a question message with resolved answers."""
    try:
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=build_question_result_blocks(resolved, user_id),
            text="Question answered",
        )
    except Exception as e:
        logger.warning(f"Failed to update question message: {e}")


async def _set_session_model_and_notify(
    deps: HandlerDependencies,
    client: Any,
    logger: Any,
    channel_id: str,
    thread_ts: str | None,
    model_value: str | None,
    display_name: str,
    log_prefix: str,
) -> None:
    """Persist model selection and post a success or error message."""
    try:
        await deps.db.update_session_model(channel_id, thread_ts, model_value)
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"Model changed to {display_name}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":heavy_check_mark: Model changed to *{display_name}*",
                    },
                }
            ],
        )
        logger.info(f"{log_prefix} changed to {model_value} for channel {channel_id}")
    except Exception as e:
        logger.error(f"{log_prefix} change failed: {e}")
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=f"Error changing model: {str(e)}",
            blocks=error_message(str(e)),
        )


def register_actions(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register all interactive component handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.action("rerun_command")
    async def handle_rerun(ack, action, body, client, logger):
        """Handle rerun button click."""
        await ack()

        try:
            channel_id = body["channel"]["id"]
            command_id = int(action["value"])
            # Get thread_ts from the message context if available
            thread_ts = body.get("message", {}).get("thread_ts")
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid action data: {e}")
            return

        # Get original command
        cmd = await deps.db.get_command_by_id(command_id)
        if not cmd:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=body["user"]["id"],
                text="Command not found.",
            )
            return

        session = await deps.db.get_or_create_session(
            channel_id, thread_ts=thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        # Create new command history entry
        new_cmd = await deps.db.add_command(session.id, cmd.command)
        await deps.db.update_command_status(new_cmd.id, "running")

        # Send processing message (in thread if original was in thread)
        response = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            blocks=processing_message(cmd.command),
        )
        message_ts = response["ts"]

        # Setup streaming state
        execution_id = str(uuid.uuid4())
        streaming_state = StreamingMessageState(
            channel_id=channel_id,
            message_ts=message_ts,
            prompt=cmd.command,
            client=client,
            logger=logger,
            smart_concat=True,
        )
        streaming_state.start_heartbeat()
        on_chunk = create_streaming_callback(streaming_state)

        try:
            route = await execute_for_session(
                deps=deps,
                session=session,
                prompt=cmd.command,
                channel_id=channel_id,
                thread_ts=thread_ts,
                execution_id=execution_id,
                on_chunk=on_chunk,
                slack_client=client,
                user_id=body.get("user", {}).get("id"),
                logger=logger,
            )
            result = route.result

            if result.success:
                await deps.db.update_command_status(new_cmd.id, "completed", result.output)
            else:
                await deps.db.update_command_status(
                    new_cmd.id, "failed", result.output, result.error
                )

            # Stop heartbeat before sending final response
            await streaming_state.stop_heartbeat()

            # Format response with table support (may produce multiple messages)
            output = result.output or result.error or "No output"
            message_blocks_list = command_response_with_tables(
                prompt=cmd.command,
                output=output,
                command_id=new_cmd.id,
                duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
                is_error=not result.success,
            )

            # Update the first message
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=message_blocks_list[0],
            )

            # Post additional messages for tables
            for blocks in message_blocks_list[1:]:
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text="Table",
                    blocks=blocks,
                )

        except asyncio.CancelledError:
            logger.info("Rerun command was cancelled")
            await streaming_state.stop_heartbeat()
            await deps.db.update_command_status(new_cmd.id, "cancelled", error_message="Cancelled")
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=error_message("Command was cancelled"),
            )
        except (OSError, IOError) as e:
            logger.error(f"I/O error rerunning command: {e}")
            await streaming_state.stop_heartbeat()
            await deps.db.update_command_status(new_cmd.id, "failed", error_message=str(e))
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=error_message(f"I/O Error: {str(e)}"),
            )
        except Exception as e:
            # Catch unexpected errors to prevent handler crash
            logger.error(f"Unexpected error rerunning command: {type(e).__name__}: {e}")
            await streaming_state.stop_heartbeat()
            await deps.db.update_command_status(new_cmd.id, "failed", error_message=str(e))
            await client.chat_update(
                channel=channel_id,
                ts=message_ts,
                blocks=error_message(str(e)),
            )

    @app.action("view_output")
    async def handle_view_output(ack, action, body, client, logger):
        """Handle view output button click."""
        await ack()

        try:
            command_id = int(action["value"])
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid action value: {e}")
            return

        cmd = await deps.db.get_command_by_id(command_id)

        if not cmd:
            await client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Command Not Found"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "This command could not be found.",
                            },
                        }
                    ],
                },
            )
            return

        output = cmd.output or "No output"
        # Convert markdown to Slack mrkdwn (flattens paragraphs)
        output = markdown_to_mrkdwn(output)
        # Truncate for modal (max ~3000 chars)
        if len(output) > 2900:
            output = output[:2900] + "\n\n... (output truncated)"

        await client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": f"Command #{cmd.id}"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": [
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": f"*Status:* {cmd.status} | "
                                f"*Created:* {cmd.created_at.strftime('%Y-%m-%d %H:%M')}",
                            }
                        ],
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*Prompt:*\n> {cmd.command}",
                        },
                    },
                    {"type": "divider"},
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"*Output:*\n{output}"},
                    },
                ],
            },
        )

    @app.action("view_parallel_results")
    async def handle_view_parallel_results(ack, action, body, client, logger):
        """Handle view parallel results button click."""
        await ack()

        try:
            job_id = int(action["value"])
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid action value: {e}")
            return

        job = await deps.db.get_parallel_job(job_id)

        if not job:
            await client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Job Not Found"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "This job could not be found.",
                            },
                        }
                    ],
                },
            )
            return

        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Parallel Job #{job.id} Results",
                    "emoji": True,
                },
            },
            {"type": "divider"},
        ]

        for result in job.results:
            terminal_num = result.get("terminal", "?")
            output = result.get("output", result.get("error", "No output"))
            if len(output) > 500:
                output = output[:500] + "\n... (truncated)"

            status = ":heavy_check_mark:" if result.get("success") else ":x:"

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Terminal {terminal_num}* {status}\n```{output}```",
                    },
                }
            )

        if job.aggregation_output:
            agg_output = job.aggregation_output
            if len(agg_output) > 800:
                agg_output = agg_output[:800] + "\n... (truncated)"

            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Aggregated Result:*\n{agg_output}",
                    },
                }
            )

        await client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Parallel Results"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": blocks[:50],  # Modal block limit
            },
        )

    @app.action("cancel_job")
    async def handle_cancel_job(ack, action, body, client, logger):
        """Handle cancel job button click."""
        await ack()

        try:
            channel_id = body["channel"]["id"]
            job_id = int(action["value"])
        except (KeyError, ValueError) as e:
            logger.error(f"Invalid action data: {e}")
            return

        cancelled = await deps.db.cancel_job(job_id)

        if cancelled:
            await client.chat_postMessage(
                channel=channel_id,
                text=f":no_entry: Job #{job_id} cancelled.",
            )

            # Update the job status message if we have it
            job = await deps.db.get_parallel_job(job_id)
            if job and job.message_ts:
                try:
                    if job.job_type == "parallel_analysis":
                        blocks = parallel_job_status(job)
                    else:
                        blocks = sequential_job_status(job)

                    await client.chat_update(
                        channel=channel_id,
                        ts=job.message_ts,
                        blocks=blocks,
                    )
                except Exception as e:
                    logger.warning(f"Failed to update job message: {e}")
        else:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=body["user"]["id"],
                text=f"Job #{job_id} not found or already completed.",
            )

    # -------------------------------------------------------------------------
    # Worktree action handlers
    # -------------------------------------------------------------------------

    async def _run_worktree_action(handler, body: dict, action: dict, client, logger) -> None:
        """Execute a worktree command handler from a button action payload."""
        try:
            channel_id = body["channel"]["id"]
            user_id = body["user"]["id"]
            thread_ts = body.get("message", {}).get("thread_ts")
            action_value = action.get("value", "")
            max_size = config.timeouts.limits.max_action_value_size
            if len(action_value) > max_size:
                raise ValueError(f"Payload too large: {len(action_value)} bytes")
            payload = json.loads(action_value)
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            logger.error(f"Invalid worktree action payload: {e}")
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text=f"Invalid worktree action payload: {e}",
            )
            return

        session = await deps.db.get_or_create_session(
            channel_id,
            thread_ts=thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        ctx = CommandContext(
            channel_id=channel_id,
            user_id=user_id,
            text="",
            command_name="/worktree",
            client=client,
            logger=logger,
            thread_ts=thread_ts,
        )
        git_service = GitService()

        try:
            await handler(ctx, session, payload, git_service)
        except GitError as e:
            await client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=f"Git error: {e}",
            )

    @app.action("worktree_switch")
    async def handle_worktree_switch(ack, action, body, client, logger):
        """Switch session to the selected worktree from list output."""
        await ack()

        async def _handler(ctx, session, payload, git_service):
            target = payload.get("branch") or payload.get("path")
            if not target:
                raise GitError("Missing worktree target in action payload")
            await _handle_switch(ctx, deps, session, git_service, target)

        await _run_worktree_action(_handler, body, action, client, logger)

    @app.action("worktree_merge_current")
    async def handle_worktree_merge_current(ack, action, body, client, logger):
        """Merge selected worktree branch into the current session worktree."""
        await ack()

        async def _handler(ctx, session, payload, git_service):
            source = payload.get("branch") or payload.get("path")
            if not source:
                raise GitError("Missing worktree source in action payload")
            await _handle_merge(
                ctx,
                deps,
                session,
                git_service,
                source_target=source,
                into_target=None,
                keep_worktree=False,
            )

        await _run_worktree_action(_handler, body, action, client, logger)

    @app.action("worktree_remove")
    async def handle_worktree_remove(ack, action, body, client, logger):
        """Remove selected worktree from list output."""
        await ack()

        async def _handler(ctx, session, payload, git_service):
            target = payload.get("branch") or payload.get("path")
            if not target:
                raise GitError("Missing worktree target in action payload")
            await _handle_remove(
                ctx,
                deps,
                session,
                git_service,
                target=target,
                force=False,
                delete_branch=False,
            )

        await _run_worktree_action(_handler, body, action, client, logger)

    # -------------------------------------------------------------------------
    # Permission approval handlers
    # -------------------------------------------------------------------------

    @app.action("approve_tool")
    async def handle_approve_tool(ack, action, body, client, logger):
        """Handle tool approval button click."""
        await ack()

        async def resolver(approval_id, user_id):
            return await PermissionManager.resolve(approval_id, approved=True)

        def block_builder(resolved, user_id):
            return build_approval_result_blocks(
                approval_id=action["value"],
                tool_name=resolved.tool_name,
                approved=True,
                user_id=user_id,
            )

        await _handle_approval_action(
            body, action, client, logger, resolver, block_builder, "Tool approval"
        )

    @app.action("deny_tool")
    async def handle_deny_tool(ack, action, body, client, logger):
        """Handle tool denial button click."""
        await ack()

        async def resolver(approval_id, user_id):
            return await PermissionManager.resolve(approval_id, approved=False)

        def block_builder(resolved, user_id):
            return build_approval_result_blocks(
                approval_id=action["value"],
                tool_name=resolved.tool_name,
                approved=False,
                user_id=user_id,
            )

        await _handle_approval_action(
            body, action, client, logger, resolver, block_builder, "Tool denial"
        )

    @app.action("approve_plan")
    async def handle_approve_plan(ack, action, body, client, logger):
        """Handle plan approval button click."""
        await ack()

        async def resolver(approval_id, user_id):
            return await PlanApprovalManager.resolve(
                approval_id=approval_id, approved=True, resolved_by=user_id
            )

        def block_builder(resolved, user_id):
            return build_plan_result_blocks(
                approval_id=action["value"], approved=True, user_id=user_id
            )

        await _handle_approval_action(
            body, action, client, logger, resolver, block_builder, "Plan approval"
        )

    @app.action("reject_plan")
    async def handle_reject_plan(ack, action, body, client, logger):
        """Handle plan rejection button click."""
        await ack()

        async def resolver(approval_id, user_id):
            return await PlanApprovalManager.resolve(
                approval_id=approval_id, approved=False, resolved_by=user_id
            )

        def block_builder(resolved, user_id):
            return build_plan_result_blocks(
                approval_id=action["value"], approved=False, user_id=user_id
            )

        await _handle_approval_action(
            body, action, client, logger, resolver, block_builder, "Plan rejection"
        )

    # -------------------------------------------------------------------------
    # Tool detail handlers
    # -------------------------------------------------------------------------

    @app.action("view_tool_detail")
    async def handle_view_tool_detail(ack, action, body, client, logger):
        """Handle view tool detail button click.

        Opens a modal with full tool input/output details.
        """
        await ack()

        try:
            action_value = action["value"]
            # Validate size before parsing to prevent resource exhaustion
            max_size = config.timeouts.limits.max_action_value_size
            if len(action_value) > max_size:
                logger.error(f"Action value too large: {len(action_value)} bytes")
                raise ValueError(f"Payload too large: {len(action_value)} bytes")
            tool_data = json.loads(action_value)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Invalid tool data: {e}")
            await client.views_open(
                trigger_id=body["trigger_id"],
                view={
                    "type": "modal",
                    "title": {"type": "plain_text", "text": "Error"},
                    "close": {"type": "plain_text", "text": "Close"},
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "Could not load tool details.",
                            },
                        }
                    ],
                },
            )
            return

        # Reconstruct ToolActivity from the serialized data
        tool = ToolActivity(
            id=tool_data.get("id", "unknown"),
            name=tool_data.get("name", "unknown"),
            input=tool_data.get("input", {}),
            input_summary=tool_data.get("input_summary", ""),
            result=tool_data.get("result"),
            full_result=tool_data.get("full_result"),
            is_error=tool_data.get("is_error", False),
            duration_ms=tool_data.get("duration_ms"),
        )

        # Get formatted blocks
        detail_blocks = format_tool_detail_blocks(tool)

        # Add GitHub link button if GITHUB_REPO is configured
        if config.GITHUB_REPO and tool.name in ("Read", "Edit", "Write"):
            github_url = await _get_github_file_url(tool, config.DEFAULT_WORKING_DIR)
            if github_url:
                detail_blocks.append({"type": "divider"})
                detail_blocks.append(
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "text": {
                                    "type": "plain_text",
                                    "text": "View on GitHub",
                                },
                                "url": github_url,
                                "action_id": f"github_view_{tool.id}",
                            }
                        ],
                    }
                )

        await client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": f"Tool: {tool.name}"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": detail_blocks[:50],  # Modal block limit
            },
        )

    # -------------------------------------------------------------------------
    # Question handlers (AskUserQuestion tool)
    # -------------------------------------------------------------------------

    @app.action(re.compile(r"^question_custom_\d+$"))
    async def handle_question_custom_answer(ack, action, body, client, logger):
        """Handle per-question custom answer button - open modal for text input."""
        await ack()

        try:
            # Parse question_id and question_index from action value
            action_value = action["value"]
            max_size = config.timeouts.limits.max_action_value_size
            if len(action_value) > max_size:
                logger.error(f"Action value too large: {len(action_value)} bytes")
                return
            data = json.loads(action_value)
            question_id = data["q"]
            question_index = data["i"]
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Invalid custom answer action data: {e}")
            return

        # Check if question still exists
        pending = await QuestionManager.get_pending(question_id)
        if not pending:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text="This question has already been answered or timed out.",
            )
            return

        # Get the question header for the modal label
        question_header = "Your Answer"
        if question_index < len(pending.questions):
            question_header = pending.questions[question_index].header or "Your Answer"

        # Open modal for custom answer
        await client.views_open(
            trigger_id=body["trigger_id"],
            view=build_custom_answer_modal(question_id, question_index, question_header),
        )

    # Register handlers for question option buttons (question_select_*)
    # We use a regex pattern to match all question select actions
    @app.action(re.compile(r"^question_select_\d+_\d+$"))
    async def handle_question_select(ack, action, body, client, logger):
        """Handle single-select question button click."""
        await ack()

        try:
            # Validate size before parsing to prevent resource exhaustion
            action_value = action["value"]
            max_size = config.timeouts.limits.max_action_value_size
            if len(action_value) > max_size:
                logger.error(f"Action value too large: {len(action_value)} bytes")
                return
            data = json.loads(action_value)
            question_id = data["q"]
            question_index = data["i"]
            selected_label = data["l"]
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f"Invalid question action data: {e}")
            return

        pending = await QuestionManager.get_pending(question_id)
        if not pending:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text="This question has already been answered or timed out.",
            )
            return

        # Set the answer for this question
        await QuestionManager.set_answer(question_id, question_index, [selected_label])

        # For single-question single-select, auto-resolve on click.
        # For multi-question, wait for the confirm button.
        if len(pending.questions) == 1:
            resolved = await QuestionManager.resolve(question_id)
            if resolved:
                user_id = body["user"]["id"]
                channel_id = body["channel"]["id"]
                message_ts = body["message"]["ts"]
                await _update_question_result_message(
                    client=client,
                    logger=logger,
                    resolved=resolved,
                    user_id=user_id,
                    channel_id=channel_id,
                    message_ts=message_ts,
                )
                logger.info(f"Question {question_id} answered by {user_id}: {resolved.answers}")
        else:
            # Multi-question: show ephemeral confirmation, wait for Confirm button
            answered_count = len(pending.answers)
            total_count = len(pending.questions)
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text=f"Answer recorded ({answered_count}/{total_count}). Click *Confirm* when done.",
            )

    @app.action(re.compile(r"^question_multiselect_\d+$"))
    async def handle_question_multiselect(ack, action, body, client, logger):
        """Handle multi-select checkbox change."""
        await ack()

        # Extract question info from block_id
        block_id = action.get("block_id", "")
        # Format: question_checkbox_{question_id}_{question_index}
        parts = block_id.split("_")
        if len(parts) < 4:
            logger.error(f"Invalid checkbox block_id: {block_id}")
            return

        question_id = parts[2]
        try:
            question_index = int(parts[3])
        except ValueError:
            logger.error(f"Invalid question index in checkbox block_id: {block_id}")
            return

        # Get selected options
        selected_options = action.get("selected_options", [])
        selected_labels = [opt.get("value", "") for opt in selected_options]

        pending = await QuestionManager.get_pending(question_id)
        if not pending:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text="This question has already been answered or timed out.",
            )
            return

        # Set the answer for this question
        await QuestionManager.set_answer(question_id, question_index, selected_labels)

        # For multi-select, the user needs to click "Submit Selections" button
        # to complete the question (see question_multiselect_submit handler below)

    @app.action("question_confirm_submit")
    async def handle_question_confirm_submit(ack, action, body, client, logger):
        """Handle the single confirm button for multi-question or multi-select responses."""
        await ack()

        question_id = action["value"]

        pending = await QuestionManager.get_pending(question_id)
        if not pending:
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text="This question has already been answered or timed out.",
            )
            return

        # For multi-select questions without any selections, set empty list
        for i, question in enumerate(pending.questions):
            if question.multi_select and i not in pending.answers:
                await QuestionManager.set_answer(question_id, i, [])

        # Check if all questions have been answered
        if not await QuestionManager.is_complete(question_id):
            answered_count = len(pending.answers)
            total_count = len(pending.questions)
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text=f"Please answer all questions before confirming ({answered_count}/{total_count} answered).",
            )
            return

        # Resolve the question
        resolved = await QuestionManager.resolve(question_id)
        if resolved:
            user_id = body["user"]["id"]
            channel_id = body["channel"]["id"]
            message_ts = body["message"]["ts"]
            await _update_question_result_message(
                client=client,
                logger=logger,
                resolved=resolved,
                user_id=user_id,
                channel_id=channel_id,
                message_ts=message_ts,
            )
            logger.info(f"Question {question_id} confirmed by {user_id}: {resolved.answers}")

    @app.view("question_custom_submit")
    async def handle_question_custom_submit(ack, body, client, view, logger):
        """Handle custom answer modal submission."""
        await ack()

        # Parse question_id and question_index from private_metadata
        try:
            metadata = json.loads(view["private_metadata"])
            question_id = metadata["q"]
            question_index = metadata["i"]
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.error(f"Invalid custom answer modal metadata: {e}")
            return

        custom_answer = view["state"]["values"]["custom_answer_block"]["custom_answer_input"][
            "value"
        ]

        pending = await QuestionManager.get_pending(question_id)
        if not pending:
            # Question already resolved or timed out
            return

        # Set custom answer for the specific question
        await QuestionManager.set_answer(question_id, question_index, [custom_answer])

        # For single-question, auto-resolve. For multi-question, wait for confirm button.
        if len(pending.questions) == 1:
            resolved = await QuestionManager.resolve(question_id)
            if resolved and resolved.message_ts:
                user_id = body["user"]["id"]
                await _update_question_result_message(
                    client=client,
                    logger=logger,
                    resolved=resolved,
                    user_id=user_id,
                    channel_id=resolved.channel_id,
                    message_ts=resolved.message_ts,
                )
                logger.info(
                    f"Question {question_id} custom answered by {user_id}: {custom_answer[:50]}..."
                )
        else:
            # Multi-question - post ephemeral feedback, wait for confirm button
            try:
                answered_count = len(pending.answers)
                total_count = len(pending.questions)
                msg = (
                    f"Answer recorded ({answered_count}/{total_count}). "
                    "Click *Confirm* when done."
                )
                await client.chat_postEphemeral(
                    channel=pending.channel_id,
                    user=body["user"]["id"],
                    text=msg,
                )
            except Exception as e:
                logger.debug(f"Could not post ephemeral feedback: {e}")

    # -------------------------------------------------------------------------
    # Detailed output viewer
    # -------------------------------------------------------------------------

    @app.action("view_detailed_output")
    async def handle_view_detailed_output(ack, action, body, client, logger):
        """Handle View Details button - show detailed output in modal."""
        await ack()

        command_id = int(action["value"])
        content = DetailCache.get(command_id)

        if not content:
            # Content expired or not found
            await client.chat_postEphemeral(
                channel=body["channel"]["id"],
                user=body["user"]["id"],
                text="Detailed output is no longer available (expired after 1 hour).",
            )
            return

        # Slack modal text blocks have a 3000 char limit
        # Split content into multiple blocks if needed
        blocks = []

        if len(content) <= config.SLACK_BLOCK_TEXT_LIMIT:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"```{content}```"},
                }
            )
        else:
            # Split into chunks
            remaining = content
            while remaining:
                chunk_size = config.SLACK_BLOCK_TEXT_LIMIT - 6  # Account for ```
                if len(remaining) <= chunk_size:
                    chunk = remaining
                    remaining = ""
                else:
                    # Try to break at newline
                    break_point = remaining.rfind("\n", 0, chunk_size)
                    if break_point == -1 or break_point < chunk_size // 2:
                        break_point = chunk_size
                    chunk = remaining[:break_point]
                    remaining = remaining[break_point:].lstrip("\n")

                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": f"```{chunk}```"},
                    }
                )

                # Modal has a limit of ~100 blocks
                if len(blocks) >= 50:
                    blocks.append(
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": f"_... truncated ({len(remaining):,} more chars)_",
                                }
                            ],
                        }
                    )
                    break

        await client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "title": {"type": "plain_text", "text": "Detailed Output"},
                "close": {"type": "plain_text", "text": "Close"},
                "blocks": blocks[:50],  # Modal block limit
            },
        )

    # -------------------------------------------------------------------------
    # Model selection handlers
    # -------------------------------------------------------------------------

    @app.action("select_model_custom")
    async def handle_custom_model_button(ack, action, body, client, logger):
        """Handle custom model button click - open modal for input."""
        await ack()

        # Parse channel_id and thread_ts from value
        value_parts = action["value"].split("|")
        channel_id = value_parts[0]
        thread_ts = value_parts[1] if len(value_parts) > 1 and value_parts[1] else None

        # Open modal for custom model input
        await client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "custom_model_submit",
                "private_metadata": json.dumps({"channel_id": channel_id, "thread_ts": thread_ts}),
                "title": {"type": "plain_text", "text": "Custom Model"},
                "submit": {"type": "plain_text", "text": "Set Model"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "custom_model_block",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "custom_model_input",
                            "placeholder": {
                                "type": "plain_text",
                                "text": "e.g., claude-sonnet-4-6[1m]",
                            },
                        },
                        "label": {"type": "plain_text", "text": "Model ID"},
                        "hint": {
                            "type": "plain_text",
                            "text": "Enter a model ID (or `default`; Codex supports -low/-medium/-high/-extra-high)",
                        },
                    }
                ],
            },
        )

    @app.view("custom_model_submit")
    async def handle_custom_model_submit(ack, body, client, view, logger):
        """Handle custom model modal submission."""
        await ack()

        # Parse channel info from private_metadata
        try:
            metadata = json.loads(view["private_metadata"])
            channel_id = metadata["channel_id"]
            thread_ts = metadata.get("thread_ts")
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Invalid custom model modal metadata: {e}")
            return

        # Get the custom model value
        model_name_raw = view["state"]["values"]["custom_model_block"]["custom_model_input"][
            "value"
        ]
        model_name = model_name_raw.strip().lower()

        if not model_name:
            return

        model_value = normalize_model_name(model_name)
        validation_error = codex_model_validation_error(model_value)
        if validation_error:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=f"Unsupported Codex model: {model_value}",
                blocks=error_message(validation_error),
            )
            return

        display_name = model_display_name(model_value)
        await _set_session_model_and_notify(
            deps=deps,
            client=client,
            logger=logger,
            channel_id=channel_id,
            thread_ts=thread_ts,
            model_value=model_value,
            display_name=display_name,
            log_prefix="Custom model",
        )

    @app.action(re.compile(r"^select_model_(?!custom$).*$"))
    async def handle_model_selection(ack, action, body, client, logger):
        """Handle model selection button click."""
        await ack()

        # Extract model name from action_id (select_model_opus -> opus)
        action_id = action["action_id"]
        model_name = action_id.replace("select_model_", "")

        # Parse channel_id and thread_ts from value
        value_parts = action["value"].split("|")
        channel_id = value_parts[0]
        thread_ts = value_parts[1] if len(value_parts) > 1 and value_parts[1] else None

        model_value = normalize_model_name(model_name)
        display_name = model_display_name(model_value)

        await _set_session_model_and_notify(
            deps=deps,
            client=client,
            logger=logger,
            channel_id=channel_id,
            thread_ts=thread_ts,
            model_value=model_value,
            display_name=display_name,
            log_prefix="Model",
        )
