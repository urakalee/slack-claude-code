"""Claude CLI passthrough command handlers."""

import asyncio
import uuid
from pathlib import Path

from slack_bolt.async_app import AsyncApp

from src.codex.capabilities import (
    get_codex_hint_for_claude_command,
    is_claude_only_slash_command,
    normalize_codex_approval_mode,
)
from src.config import (
    CODEX_MODELS,
    EFFORT_LEVELS,
    config,
    get_backend_for_model,
    is_supported_codex_model,
    looks_like_codex_model,
    parse_model_effort,
)
from src.utils.execution_scope import build_session_scope
from src.utils.formatters.command import command_response_with_tables, error_message
from src.utils.formatters.streaming import processing_message

from ..base import CommandContext, HandlerDependencies, slack_command


def register_claude_cli_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register Claude CLI passthrough command handlers.

    These commands pass through to the Claude Code CLI commands.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    async def _cancel_executor_operations(
        executor,
        ctx: CommandContext,
    ) -> int:
        """Cancel operations for the current scope when possible."""
        if not executor:
            return 0
        if ctx.thread_ts:
            session_scope = build_session_scope(ctx.channel_id, ctx.thread_ts)
            return await executor.cancel_by_scope(session_scope)
        return await executor.cancel_by_channel(ctx.channel_id)

    async def _cancel_codex_operations(
        ctx: CommandContext,
        deps: HandlerDependencies,
    ) -> int:
        """Cancel active Codex operations for this channel/thread."""
        return await _cancel_executor_operations(deps.codex_executor, ctx)

    async def _send_claude_command(
        ctx: CommandContext,
        claude_command: str,
        deps: HandlerDependencies,
    ) -> None:
        """Send a Claude CLI command and return the result.

        Parameters
        ----------
        ctx : CommandContext
            The command context.
        claude_command : str
            The Claude CLI command to execute (e.g., "/clear", "/cost").
        deps : HandlerDependencies
            Handler dependencies.
        """
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        command_name = claude_command.strip().split(" ", 1)[0]
        if session.get_backend() == "codex" and is_claude_only_slash_command(
            command_name
        ):
            hint = get_codex_hint_for_claude_command(command_name)
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=f"{command_name} is not supported for Codex sessions.",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f":warning: `{command_name}` is Claude-specific and not available "
                                "for Codex sessions.\n\n"
                                f"{hint}"
                            ),
                        },
                    },
                ],
            )
            return

        # Send processing message
        response = await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Running: {claude_command}",
            blocks=processing_message(claude_command),
        )
        message_ts = response["ts"]

        try:
            result = await deps.executor.execute(
                prompt=claude_command,
                working_directory=session.working_directory,
                session_id=build_session_scope(ctx.channel_id, ctx.thread_ts),
                resume_session_id=session.claude_session_id,
                execution_id=str(uuid.uuid4()),
                permission_mode=session.permission_mode,
                model=session.model,
                channel_id=ctx.channel_id,
                thread_ts=ctx.thread_ts,
            )

            # Update session if needed
            if result.session_id:
                await deps.db.update_session_claude_id(
                    ctx.channel_id, ctx.thread_ts, result.session_id
                )

            output = result.output or result.error or ""
            if not output and result.detailed_output:
                output = result.detailed_output
            if not output:
                output = "Command completed (no output)"

            # Format response with table support (may produce multiple messages)
            message_blocks_list = command_response_with_tables(
                prompt=claude_command,
                output=output,
                command_id=None,
                duration_ms=result.duration_ms,
                cost_usd=result.cost_usd,
                is_error=not result.success,
            )

            # Update the first message
            await ctx.client.chat_update(
                channel=ctx.channel_id,
                ts=message_ts,
                text=output[:100] + "..." if len(output) > 100 else output,
                blocks=message_blocks_list[0],
            )

            # Post additional messages for tables
            for blocks in message_blocks_list[1:]:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Table",
                    blocks=blocks,
                )

        except Exception as e:
            ctx.logger.error(f"Claude CLI command failed: {e}")
            await ctx.client.chat_update(
                channel=ctx.channel_id,
                ts=message_ts,
                text=f"Error: {str(e)}",
                blocks=error_message(str(e)),
            )

    @app.command("/clear")
    @slack_command()
    async def handle_clear(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /clear command - cancel processes and reset conversation sessions."""
        # Step 1: Cancel/stop active executions for this channel
        cancelled_count = await _cancel_executor_operations(deps.executor, ctx)
        cancelled_count += await _cancel_codex_operations(ctx, deps)

        # Brief wait for graceful shutdown
        if cancelled_count > 0:
            await asyncio.sleep(0.5)

        # Step 2: Clear backend session IDs so next message starts fresh
        await deps.db.clear_session_claude_id(ctx.channel_id, ctx.thread_ts)
        await deps.db.clear_session_codex_id(ctx.channel_id, ctx.thread_ts)
        ctx.logger.info("Cleared Claude and Codex session IDs")

        # Note: We don't send /clear to Claude CLI because it only works in
        # interactive mode, not with -p flag. Clearing the session ID above
        # is sufficient - the next message will start a new conversation.

        # Step 3: Notify user
        if cancelled_count > 0:
            message = f"Cancelled {cancelled_count} active process(es) and cleared conversation."
        else:
            message = (
                "Conversation cleared. Your next message will start a fresh session."
            )

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=message,
            blocks=[
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f":white_check_mark: {message}"},
                }
            ],
        )

    @app.command("/esc")
    @slack_command()
    async def handle_esc(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /esc command - interrupt current operation (like pressing Escape)."""
        # Interrupt all active executions for this channel
        cancelled_count = await _cancel_executor_operations(deps.executor, ctx)
        cancelled_count += await _cancel_codex_operations(ctx, deps)

        if cancelled_count > 0:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":stop_sign: Interrupted {cancelled_count} running operation(s).",
            )
        else:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=":information_source: No active operations to interrupt.",
            )

    @app.command("/add-dir")
    @slack_command(require_text=True, usage_hint="Usage: /add-dir <path>")
    async def handle_add_dir(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /add-dir <path> command - add directory to context."""
        directory = ctx.text.strip()

        # Resolve and validate path
        resolved_dir = Path(directory).expanduser().resolve()
        if not resolved_dir.exists():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Path does not exist: {resolved_dir}",
                blocks=error_message(f"Path does not exist: `{resolved_dir}`"),
            )
            return
        if not resolved_dir.is_dir():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Not a directory: {resolved_dir}",
                blocks=error_message(f"Not a directory: `{resolved_dir}`"),
            )
            return

        await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )

        # Add resolved directory to session's added_dirs list
        added_dirs = await deps.db.add_session_dir(
            ctx.channel_id, ctx.thread_ts, str(resolved_dir)
        )

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Added directory: {resolved_dir}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":file_folder: *Directory Added*\n\n"
                            f"Added `{resolved_dir}` to context.\n\n"
                            f"*Current directories ({len(added_dirs)}):*\n"
                            + "\n".join(f"• `{d}`" for d in added_dirs)
                        ),
                    },
                }
            ],
        )

    @app.command("/remove-dir")
    @slack_command(require_text=True, usage_hint="Usage: /remove-dir <path>")
    async def handle_remove_dir(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /remove-dir <path> command - remove directory from context."""
        directory = ctx.text.strip()

        # Get current dirs to check if it exists
        current_dirs = await deps.db.get_session_dirs(ctx.channel_id, ctx.thread_ts)

        if directory not in current_dirs:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=f"Directory not found: {directory}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f":warning: Directory `{directory}` is not in the context.\n\n"
                                f"*Current directories ({len(current_dirs)}):*\n"
                                + (
                                    "\n".join(f"• `{d}`" for d in current_dirs)
                                    if current_dirs
                                    else "_No directories added_"
                                )
                            ),
                        },
                    }
                ],
            )
            return

        # Remove directory from session's added_dirs list
        remaining_dirs = await deps.db.remove_session_dir(
            ctx.channel_id, ctx.thread_ts, directory
        )

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Removed directory: {directory}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":file_folder: *Directory Removed*\n\n"
                            f"Removed `{directory}` from context.\n\n"
                            f"*Remaining directories ({len(remaining_dirs)}):*\n"
                            + (
                                "\n".join(f"• `{d}`" for d in remaining_dirs)
                                if remaining_dirs
                                else "_No directories added_"
                            )
                        ),
                    },
                }
            ],
        )

    @app.command("/list-dirs")
    @slack_command()
    async def handle_list_dirs(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /list-dirs command - list directories in context."""
        added_dirs = await deps.db.get_session_dirs(ctx.channel_id, ctx.thread_ts)

        # Get working directory for context
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text=f"Directories in context: {len(added_dirs)}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":file_folder: *Directories in Context*\n\n"
                            f"*Working directory:* `{session.working_directory}`\n\n"
                            f"*Added directories ({len(added_dirs)}):*\n"
                            + (
                                "\n".join(f"• `{d}`" for d in added_dirs)
                                if added_dirs
                                else "_No additional directories added_"
                            )
                            + "\n\n_Use `/add-dir <path>` to add directories, `/remove-dir <path>` to remove._"
                        ),
                    },
                }
            ],
        )

    @app.command("/compact")
    @slack_command()
    async def handle_compact(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /compact [instructions] command - compact conversation."""
        if ctx.text:
            await _send_claude_command(ctx, f"/compact {ctx.text}", deps)
        else:
            await _send_claude_command(ctx, "/compact", deps)

    @app.command("/cost")
    @slack_command()
    async def handle_cost(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /cost command - show session cost."""
        await _send_claude_command(ctx, "/cost", deps)

    @app.command("/claude-help")
    @slack_command()
    async def handle_claude_help(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /claude-help command - show Claude Code help."""
        await _send_claude_command(ctx, "/help", deps)

    @app.command("/doctor")
    @slack_command()
    async def handle_doctor(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /doctor command - run Claude Code diagnostics."""
        await _send_claude_command(ctx, "/doctor", deps)

    @app.command("/claude-config")
    @slack_command()
    async def handle_claude_config(
        ctx: CommandContext, deps: HandlerDependencies = deps
    ):
        """Handle /claude-config command - show Claude Code config."""
        await _send_claude_command(ctx, "/config", deps)

    @app.command("/context")
    @slack_command()
    async def handle_context(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /context command - visualize current context usage."""
        await _send_claude_command(ctx, "/context", deps)

    @app.command("/model")
    @slack_command()
    async def handle_model(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /model [name] command - show or change AI model."""
        # Get session to check/update model
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        claude_model_display: dict[str | None, str] = {
            None: "Default (recommended)",
            "default": "Default (recommended)",
            "opus": "Default (recommended)",
            "claude-opus-4-6": "Default (recommended)",
            "claude-opus-4-6[1m]": "Opus (1M context)",
            "sonnet": "Sonnet",
            "claude-sonnet-4-6": "Sonnet",
            "claude-sonnet-4-6[1m]": "Sonnet (1M context)",
            "haiku": "Haiku",
            "claude-haiku-4-5": "Haiku",
        }

        if ctx.text:
            # Direct model selection via command argument
            model_name = ctx.text.strip().lower()

            # Normalize model names (support both Claude and Codex models)
            claude_base_model_map: dict[str, str | None] = {
                # Claude models
                "default": None,
                "default (recommended)": None,
                "recommended": None,
                "opus": None,
                "opus-4.6": None,
                "claude-opus-4-6": None,
                "opus-1m": "claude-opus-4-6[1m]",
                "opus (1m context)": "claude-opus-4-6[1m]",
                "claude-opus-4-6[1m]": "claude-opus-4-6[1m]",
                "sonnet": "sonnet",
                "sonnet-4.6": "sonnet",
                "claude-sonnet-4-6": "sonnet",
                "sonnet-1m": "claude-sonnet-4-6[1m]",
                "sonnet (1m context)": "claude-sonnet-4-6[1m]",
                "claude-sonnet-4-6[1m]": "claude-sonnet-4-6[1m]",
                "haiku": "haiku",
                "haiku-4.5": "haiku",
                "claude-haiku-4-5": "haiku",
            }
            codex_base_model_map = {
                # Codex models
                "codex": "gpt-5.3-codex",
                "gpt-5.3-codex": "gpt-5.3-codex",
                "gpt-5.3-codex-spark": "gpt-5.3-codex-spark",
                "gpt-5.2-codex": "gpt-5.2-codex",
                "gpt-5.1-codex-max": "gpt-5.1-codex-max",
                "gpt-5.2": "gpt-5.2",
                "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
            }
            base_name, effort = parse_model_effort(model_name)
            if base_name in claude_base_model_map:
                resolved_base = claude_base_model_map[base_name]
            else:
                resolved_base = codex_base_model_map.get(base_name, base_name)
            if resolved_base is None:
                normalized = None
            elif effort and looks_like_codex_model(resolved_base):
                normalized = f"{resolved_base}-{effort}"
            else:
                normalized = resolved_base

            if (
                normalized
                and looks_like_codex_model(normalized)
                and not is_supported_codex_model(normalized)
            ):
                supported = "\n".join(f"• `{model}`" for model in sorted(CODEX_MODELS))
                effort_levels = ", ".join(f"`{level}`" for level in EFFORT_LEVELS)
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text=f"Unsupported Codex model: {normalized}",
                    blocks=error_message(
                        f"Unsupported Codex model: `{normalized}`\n\n"
                        f"Supported Codex models:\n{supported}\n\n"
                        f"Optional effort suffixes: {effort_levels}, `extra-high`"
                    ),
                )
                return

            backend = get_backend_for_model(normalized)

            await deps.db.update_session_model(
                ctx.channel_id, ctx.thread_ts, normalized
            )

            backend_label = "Claude Code" if backend == "claude" else "OpenAI Codex"
            selected_display = claude_model_display.get(
                normalized, normalized or "Default (recommended)"
            )
            model_id_line = ""
            if normalized and selected_display != normalized:
                model_id_line = f"\n_Model ID: `{normalized}`_"
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":heavy_check_mark: Model changed to *{selected_display}* ({backend_label})",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f":heavy_check_mark: Model changed to *{selected_display}*"
                                f"{model_id_line}\n_Backend: {backend_label}_"
                            ),
                        },
                    }
                ],
            )
        else:
            # Show current model and allow selection via buttons
            current_model = session.model
            if current_model in {"default", "opus", "claude-opus-4-6"}:
                normalized_current_model = None
            else:
                normalized_current_model = current_model
            current_backend = get_backend_for_model(normalized_current_model)

            # Available models (organized by backend)
            claude_models = [
                {
                    "name": "default",
                    "value": None,
                    "display": "Default (recommended)",
                    "desc": "Opus 4.6 · Most capable for complex work",
                },
                {
                    "name": "opus-1m",
                    "value": "claude-opus-4-6[1m]",
                    "display": "Opus (1M context)",
                    "desc": "Opus 4.6 with 1M context · Billed as extra usage · $10/$37.50 per Mtok",
                },
                {
                    "name": "sonnet",
                    "value": "sonnet",
                    "display": "Sonnet",
                    "desc": "Sonnet 4.6 · Best for everyday tasks",
                },
                {
                    "name": "sonnet-1m",
                    "value": "claude-sonnet-4-6[1m]",
                    "display": "Sonnet (1M context)",
                    "desc": "Sonnet 4.6 with 1M context · Billed as extra usage · $6/$22.50 per Mtok",
                },
                {
                    "name": "haiku",
                    "value": "haiku",
                    "display": "Haiku",
                    "desc": "Haiku 4.5 · Fastest for quick answers",
                },
            ]

            codex_models = [
                {
                    "name": "gpt-5.3-codex",
                    "value": "gpt-5.3-codex",
                    "display": "GPT-5.3 Codex",
                    "desc": "Latest frontier agentic coding model",
                },
                {
                    "name": "gpt-5.3-codex-spark",
                    "value": "gpt-5.3-codex-spark",
                    "display": "GPT-5.3 Codex Spark",
                    "desc": "Ultra-fast coding model",
                },
                {
                    "name": "gpt-5.2-codex",
                    "value": "gpt-5.2-codex",
                    "display": "GPT-5.2 Codex",
                    "desc": "Frontier agentic coding model",
                },
                {
                    "name": "gpt-5.1-codex-max",
                    "value": "gpt-5.1-codex-max",
                    "display": "GPT-5.1 Codex Max",
                    "desc": "Codex-optimized flagship for deep and fast reasoning",
                },
                {
                    "name": "gpt-5.2",
                    "value": "gpt-5.2",
                    "display": "GPT-5.2",
                    "desc": "Latest frontier model with improvements across knowledge, reasoning and coding",
                },
                {
                    "name": "gpt-5.1-codex-mini",
                    "value": "gpt-5.1-codex-mini",
                    "display": "GPT-5.1 Codex Mini",
                    "desc": "Optimized for codex. Cheaper, faster, but less capable",
                },
            ]
            effort_labels = {
                "low": "Low",
                "medium": "Medium",
                "high": "High",
                "xhigh": "Extra-High",
            }
            effort_variants = []
            for model in codex_models:
                for effort_key, effort_label in effort_labels.items():
                    effort_variants.append(
                        {
                            "name": f"{model['name']}-{effort_key}",
                            "value": f"{model['value']}-{effort_key}",
                            "display": f"{model['display']} ({effort_label})",
                            "desc": model["desc"],
                        }
                    )
            codex_models = codex_models + effort_variants

            # Get display name for current model
            all_models = claude_models + codex_models
            current_display = next(
                (
                    m["display"]
                    for m in all_models
                    if m["value"] == normalized_current_model
                ),
                claude_model_display.get(
                    normalized_current_model,
                    normalized_current_model or "Default (recommended)",
                ),
            )

            backend_label = (
                "Claude Code" if current_backend == "claude" else "OpenAI Codex"
            )

            # Build button blocks
            blocks = [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Current Model:* {current_display}\n*Backend:* {backend_label}\n\nSelect a model:",
                    },
                },
                {"type": "divider"},
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*Claude Code Models*"},
                },
            ]

            for model in claude_models:
                is_current = model["value"] == normalized_current_model
                button_text = f"{'✓ ' if is_current else ''}{model['display']}"

                # Build button accessory
                button_accessory = {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": button_text,
                        "emoji": True,
                    },
                    "action_id": f"select_model_{model['name']}",
                    "value": f"{ctx.channel_id}|{ctx.thread_ts or ''}",
                }

                # Only add style if it's the current model
                if is_current:
                    button_accessory["style"] = "primary"

                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{model['display']}*\n{model['desc']}",
                        },
                        "accessory": button_accessory,
                    }
                )

            blocks.append({"type": "divider"})
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "*OpenAI Codex Models*"},
                }
            )

            for model in codex_models:
                is_current = model["value"] == normalized_current_model
                button_text = f"{'✓ ' if is_current else ''}{model['display']}"

                # Build button accessory
                button_accessory = {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": button_text,
                        "emoji": True,
                    },
                    "action_id": f"select_model_{model['name']}",
                    "value": f"{ctx.channel_id}|{ctx.thread_ts or ''}",
                }

                # Only add style if it's the current model
                if is_current:
                    button_accessory["style"] = "primary"

                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"*{model['display']}*\n{model['desc']}",
                        },
                        "accessory": button_accessory,
                    }
                )

            # Add custom model option
            blocks.append({"type": "divider"})

            # Check if current model is a custom one (not in predefined lists)
            predefined_models = {m["value"] for m in all_models}
            is_custom_model = normalized_current_model not in predefined_models

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "*Custom Model*\nEnter any model ID (e.g., `claude-sonnet-4-6[1m]` or `gpt-5.3-codex-extra-high`)"
                            + (
                                f"\n_Currently using: `{normalized_current_model}`_"
                                if is_custom_model
                                else ""
                            )
                        ),
                    },
                    "accessory": {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Enter Custom Model",
                            "emoji": True,
                        },
                        "action_id": "select_model_custom",
                        "value": f"{ctx.channel_id}|{ctx.thread_ts or ''}",
                    },
                }
            )

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Current model: {current_display}",
                blocks=blocks,
            )

    @app.command("/init")
    @slack_command()
    async def handle_init(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /init command - initialize project with CLAUDE.md."""
        await _send_claude_command(ctx, "/init", deps)

    @app.command("/memory")
    @slack_command()
    async def handle_memory(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /memory command - edit CLAUDE.md memory files."""
        await _send_claude_command(ctx, "/memory", deps)

    @app.command("/review")
    @slack_command()
    async def handle_review(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /review command - request code review."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        if session.get_backend() == "codex":
            if not deps.codex_executor:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Codex executor is not configured.",
                    blocks=error_message("Codex executor is not configured."),
                )
                return
            if not session.codex_session_id:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="No active Codex session.",
                    blocks=error_message(
                        "No active Codex thread for this session yet. Send a Codex message first."
                    ),
                )
                return

            tokens = ctx.text.split() if ctx.text else []
            if tokens and tokens[0].lower() in {"status", "read"}:
                thread_arg = tokens[1] if len(tokens) > 1 else "current"
                thread_id = (
                    session.codex_session_id
                    if thread_arg == "current"
                    else thread_arg.strip()
                )
                if not thread_id:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        text="No active Codex session.",
                        blocks=error_message(
                            "No active Codex thread for this session yet. Send a Codex message first."
                        ),
                    )
                    return
                try:
                    result = await deps.codex_executor.thread_read(
                        thread_id=thread_id,
                        working_directory=session.working_directory,
                        include_turns=True,
                    )
                    thread = result.get("thread", {})
                    turns = thread.get("turns", [])
                    if turns:
                        recent_turns = turns[-5:]
                        turn_lines = []
                        for turn in recent_turns:
                            turn_lines.append(
                                f"• `{turn.get('id', 'unknown')}` status=`{turn.get('status', 'unknown')}` "
                                f"created=`{turn.get('createdAt', 'n/a')}`"
                            )
                        turns_text = "\n".join(turn_lines)
                        latest_status = recent_turns[-1].get(
                            "status", thread.get("status", "unknown")
                        )
                    else:
                        turns_text = "No turns found."
                        latest_status = thread.get("status", "unknown")
                    summary = (
                        f"*Codex Review Status*\n"
                        f"Thread: `{thread.get('id', thread_id)}`\n"
                        f"Name: {thread.get('name') or '(unnamed)'}\n"
                        f"Status: `{latest_status}`\n"
                        f"Turns: `{len(turns)}`\n\n"
                        f"*Recent Turns*\n{turns_text}"
                    )
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        text="Codex review status",
                        blocks=[
                            {
                                "type": "section",
                                "text": {"type": "mrkdwn", "text": summary},
                            }
                        ],
                    )
                except Exception as e:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        thread_ts=ctx.thread_ts,
                        text=f"Failed to fetch review status: {e}",
                        blocks=error_message(str(e)),
                    )
                return

            target: dict
            if ctx.text:
                target = {"type": "custom", "instructions": ctx.text}
            else:
                target = {"type": "uncommittedChanges"}

            try:
                result = await deps.codex_executor.review_start(
                    thread_id=session.codex_session_id,
                    target=target,
                    working_directory=session.working_directory,
                )
                review_thread_id = result.get("reviewThreadId")
                turn = result.get("turn", {})
                turn_id = turn.get("id", "unknown")
                review_summary = (
                    f":mag: Started Codex review for thread `{session.codex_session_id}`.\n"
                    f"Turn: `{turn_id}`"
                )
                if review_thread_id:
                    review_summary += f"\nReview thread: `{review_thread_id}`"
                    review_summary += f"\nUse `/review status {review_thread_id}` to inspect progress."
                else:
                    review_summary += (
                        "\nUse `/review status` to inspect latest turn status."
                    )
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Codex review started",
                    blocks=[
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": review_summary},
                        }
                    ],
                )
            except Exception as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"Failed to start review: {e}",
                    blocks=error_message(str(e)),
                )
            return
        await _send_claude_command(ctx, "/review", deps)

    @app.command("/permissions")
    @slack_command()
    async def handle_permissions(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /permissions command - view or update permissions."""
        # Note: /permissions only works in Claude CLI interactive mode, not with -p flag.
        # In print mode, slash commands get interpreted as skill invocations.
        # Show info about how to manage permissions in Slack mode.
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        if session.get_backend() == "codex":
            current_approval = normalize_codex_approval_mode(
                session.approval_mode or config.CODEX_APPROVAL_MODE
            )
            current_sandbox = session.sandbox_mode or config.CODEX_SANDBOX_MODE
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Codex permission settings",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                ":lock: *Codex Permissions*\n\n"
                                f"*Approval mode:* `{current_approval}`\n"
                                f"*Sandbox mode:* `{current_sandbox}`\n\n"
                                "Use:\n"
                                "• `/mode approval <mode>` to control approvals\n"
                                "• `/mode sandbox <mode>` to control filesystem access\n"
                                "• `/mode bypass|ask|default|plan` for compatibility session mode"
                            ),
                        },
                    }
                ],
            )
            return

        current_mode = session.permission_mode or "default"

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Permission settings",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":lock: *Permissions*\n\n"
                            f"*Current mode:* `{current_mode}`\n\n"
                            "Use `/mode` to change permission modes:\n"
                            "• `/mode ask` - Ask for approval on sensitive operations\n"
                            "• `/mode plan` - Plan-only mode (no execution)\n"
                            "• `/mode accept` - Auto-approve file edits\n"
                            "• `/mode bypass` - Skip all permission checks"
                        ),
                    },
                }
            ],
        )

    @app.command("/stats")
    @slack_command()
    async def handle_stats(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /stats command - show usage stats and history."""
        # Note: /stats only works in Claude CLI interactive mode, not with -p flag.
        # In print mode, slash commands get interpreted as skill invocations.
        # For now, show a message explaining this limitation.
        # Cost per request is shown in each response footer.
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Stats are not available in Slack mode.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            ":bar_chart: *Usage Stats*\n\n"
                            "The `/stats` command is not available in Slack mode. "
                            "Claude CLI's `/stats` only works in interactive terminal mode.\n\n"
                            "*Tip:* Cost and duration are shown in each response footer. "
                            "Use `/cost` to see session cost details."
                        ),
                    },
                }
            ],
        )

    @app.command("/todos")
    @slack_command()
    async def handle_todos(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /todos command - list current TODO items."""
        await _send_claude_command(ctx, "/todos", deps)

    @app.command("/mcp")
    @slack_command()
    async def handle_mcp(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /mcp command - show MCP server configuration."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )
        if session.get_backend() == "codex":
            if not deps.codex_executor:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Codex executor is not configured.",
                    blocks=error_message("Codex executor is not configured."),
                )
                return
            try:
                status = await deps.codex_executor.mcp_server_status_list(
                    session.working_directory
                )
                servers = status.get("data", [])
                if not servers:
                    summary = "No MCP servers detected."
                else:
                    lines = []
                    for server in servers[:10]:
                        name = server.get("name", "unknown")
                        auth_status = server.get("authStatus", "unknown")
                        tools = server.get("tools", {})
                        resources = server.get("resources", [])
                        lines.append(
                            f"• *{name}*\nauth: `{auth_status}` • tools: `{len(tools)}` • resources: `{len(resources)}`"
                        )
                    summary = "*Codex MCP Servers*\n" + "\n\n".join(lines)
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text="Codex MCP status",
                    blocks=[
                        {"type": "section", "text": {"type": "mrkdwn", "text": summary}}
                    ],
                )
            except Exception as e:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    text=f"Failed to load MCP status: {e}",
                    blocks=error_message(str(e)),
                )
            return
        if ctx.text:
            await _send_claude_command(ctx, f"/mcp {ctx.text}", deps)
        else:
            await _send_claude_command(ctx, "/mcp", deps)
