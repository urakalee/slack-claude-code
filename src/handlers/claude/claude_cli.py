"""Claude CLI passthrough command handlers."""

import asyncio
import signal
import uuid
from pathlib import Path

from slack_bolt.async_app import AsyncApp

from src.config import config, get_backend_for_model, CLAUDE_MODELS, CODEX_MODELS
from src.utils.formatting import SlackFormatter

from ..base import CommandContext, HandlerDependencies, get_command_name, slack_command


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
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        # Send processing message
        response = await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Running: {claude_command}",
            blocks=SlackFormatter.processing_message(claude_command),
        )
        message_ts = response["ts"]

        try:
            result = await deps.executor.execute(
                prompt=claude_command,
                working_directory=session.working_directory,
                session_id=ctx.channel_id,
                resume_session_id=session.claude_session_id,
                execution_id=str(uuid.uuid4()),
                permission_mode=session.permission_mode,
                model=session.model,
                channel_id=ctx.channel_id,
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
            message_blocks_list = SlackFormatter.command_response_with_tables(
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
                blocks=SlackFormatter.error_message(str(e)),
            )

    @app.command(get_command_name("/clear"))
    @slack_command()
    async def handle_clear(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /clear command - cancel processes and reset Claude conversation."""
        # Step 1: Cancel all active executor processes for this channel
        cancelled_count = await deps.executor.cancel_by_channel(ctx.channel_id)
        if deps.codex_executor:
            cancelled_count += await deps.codex_executor.cancel_by_channel(ctx.channel_id)

        # Brief wait for graceful shutdown
        if cancelled_count > 0:
            await asyncio.sleep(0.5)

        # Step 2: Clear the Claude session ID so next message starts fresh
        await deps.db.clear_session_claude_id(ctx.channel_id, ctx.thread_ts)
        ctx.logger.info("Cleared Claude session ID")

        # Note: We don't send /clear to Claude CLI because it only works in
        # interactive mode, not with -p flag. Clearing the session ID above
        # is sufficient - the next message will start a new conversation.

        # Step 3: Notify user
        if cancelled_count > 0:
            message = f"Cancelled {cancelled_count} active process(es) and cleared conversation."
        else:
            message = "Conversation cleared. Your next message will start a fresh session."

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

    @app.command(get_command_name("/esc"))
    @slack_command()
    async def handle_esc(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /esc command - interrupt current operation (like pressing Escape)."""
        # Cancel all active executor processes for this channel
        cancelled_count = await deps.executor.cancel_by_channel(ctx.channel_id)
        if deps.codex_executor:
            cancelled_count += await deps.codex_executor.cancel_by_channel(ctx.channel_id)

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

    @app.command(get_command_name("/add-dir"))
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
                blocks=SlackFormatter.error_message(f"Path does not exist: `{resolved_dir}`"),
            )
            return
        if not resolved_dir.is_dir():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Not a directory: {resolved_dir}",
                blocks=SlackFormatter.error_message(f"Not a directory: `{resolved_dir}`"),
            )
            return

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

    @app.command(get_command_name("/remove-dir"))
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

    @app.command(get_command_name("/list-dirs"))
    @slack_command()
    async def handle_list_dirs(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /list-dirs command - list directories in context."""
        added_dirs = await deps.db.get_session_dirs(ctx.channel_id, ctx.thread_ts)

        # Get working directory for context
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
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

    @app.command(get_command_name("/compact"))
    @slack_command()
    async def handle_compact(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /compact [instructions] command - compact conversation."""
        if ctx.text:
            await _send_claude_command(ctx, f"/compact {ctx.text}", deps)
        else:
            await _send_claude_command(ctx, "/compact", deps)

    @app.command(get_command_name("/cost"))
    @slack_command()
    async def handle_cost(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /cost command - show session cost."""
        await _send_claude_command(ctx, "/cost", deps)

    @app.command(get_command_name("/claude-help"))
    @slack_command()
    async def handle_claude_help(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /claude-help command - show Claude Code help."""
        await _send_claude_command(ctx, "/help", deps)

    @app.command(get_command_name("/doctor"))
    @slack_command()
    async def handle_doctor(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /doctor command - run Claude Code diagnostics."""
        await _send_claude_command(ctx, "/doctor", deps)

    @app.command(get_command_name("/claude-config"))
    @slack_command()
    async def handle_claude_config(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /claude-config command - show Claude Code config."""
        await _send_claude_command(ctx, "/config", deps)

    @app.command(get_command_name("/context"))
    @slack_command()
    async def handle_context(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /context command - visualize current context usage."""
        await _send_claude_command(ctx, "/context", deps)

    @app.command(get_command_name("/model"))
    @slack_command()
    async def handle_model(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /model [name] command - show or change AI model."""
        # Get session to check/update model
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        if ctx.text:
            # Direct model selection via command argument
            model_name = ctx.text.strip().lower()

            # Normalize model names (support both Claude and Codex models)
            # Base model aliases (without effort suffix)
            base_model_map = {
                # Claude models
                "opus": "opus",
                "opus-4": "opus",
                "opus-4.5": "claude-opus-4-5-20250929",
                "opus-4.6": "opus",
                "sonnet": "sonnet",
                "sonnet-4": "sonnet",
                "sonnet-4.5": "sonnet",
                "haiku": "haiku",
                "haiku-4": "haiku",
                # Codex models
                "codex": "gpt-5.3-codex",
                "gpt-5.3-codex": "gpt-5.3-codex",
                "gpt-5.2-codex": "gpt-5.2-codex",
                "gpt-5.1-codex-max": "gpt-5.1-codex-max",
                "gpt-5.2": "gpt-5.2",
                "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
                "gpt-5-codex": "gpt-5-codex",
                "gpt-5": "gpt-5",
                "o3": "o3",
                "o4-mini": "o4-mini",
            }

            # Check if model name has an effort suffix (e.g., "codex-high")
            from src.config import EFFORT_LEVELS, parse_model_effort

            base_name, effort = parse_model_effort(model_name)
            resolved_base = base_model_map.get(base_name, base_name)
            if effort:
                normalized = f"{resolved_base}-{effort}"
            else:
                normalized = base_model_map.get(model_name, model_name)
            backend = get_backend_for_model(normalized)

            await deps.db.update_session_model(ctx.channel_id, ctx.thread_ts, normalized)

            backend_label = "Claude Code" if backend == "claude" else "OpenAI Codex"
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":heavy_check_mark: Model changed to *{normalized}* ({backend_label})",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":heavy_check_mark: Model changed to *{normalized}*\n_Backend: {backend_label}_",
                        },
                    }
                ],
            )
        else:
            # Show current model and allow selection via buttons
            # Get current model from session (default to opus)
            current_model = session.model or "opus"
            current_backend = get_backend_for_model(current_model)

            # Available models (organized by backend)
            claude_models = [
                {"name": "opus", "display": "Claude Opus 4.6", "desc": "Most capable model"},
                {"name": "claude-opus-4-5-20250929", "display": "Claude Opus 4.5", "desc": "Previous generation Opus"},
                {"name": "sonnet", "display": "Claude Sonnet 4.5", "desc": "Balanced performance and speed"},
                {"name": "haiku", "display": "Claude Haiku 4", "desc": "Fastest and most cost-effective"},
            ]

            # Generate Codex models with effort level variants
            codex_base_models = [
                ("gpt-5.3-codex", "GPT-5.3 Codex", "Latest frontier agentic coding model"),
                ("gpt-5.2-codex", "GPT-5.2 Codex", "Frontier agentic coding model"),
                ("gpt-5.1-codex-max", "GPT-5.1 Codex Max", "Deep and fast reasoning"),
                ("gpt-5.2", "GPT-5.2", "Latest frontier model"),
                ("gpt-5.1-codex-mini", "GPT-5.1 Codex Mini", "Cheaper, faster, less capable"),
            ]
            effort_labels = {
                "low": "Low",
                "medium": "Med",
                "high": "High",
                "xhigh": "XHigh",
            }
            codex_models = []
            for base_name, display, desc in codex_base_models:
                for effort_key, effort_label in effort_labels.items():
                    codex_models.append({
                        "name": f"{base_name}-{effort_key}",
                        "display": f"{display} ({effort_label})",
                        "desc": desc,
                    })
            # Legacy models (no effort levels)
            codex_models.extend([
                {"name": "gpt-5-codex", "display": "GPT-5 Codex (Legacy)", "desc": "Legacy coding model"},
                {"name": "o3", "display": "O3 (Legacy)", "desc": "OpenAI reasoning model"},
                {"name": "o4-mini", "display": "O4 Mini (Legacy)", "desc": "Fast and efficient"},
            ])

            # Get display name for current model
            all_models = claude_models + codex_models
            current_display = next(
                (m["display"] for m in all_models if m["name"] == current_model),
                current_model,
            )

            backend_label = "Claude Code" if current_backend == "claude" else "OpenAI Codex"

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
                is_current = model["name"] == current_model
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
                is_current = model["name"] == current_model
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
            predefined_models = {m["name"] for m in all_models}
            is_custom_model = current_model not in predefined_models

            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*Custom Model*\nEnter any model ID (e.g., `claude-opus-4-6-20250101`)"
                            + (f"\n_Currently using: `{current_model}`_" if is_custom_model else "")
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
                text=f"Current model: {current_model}",
                blocks=blocks,
            )

    @app.command(get_command_name("/init"))
    @slack_command()
    async def handle_init(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /init command - initialize project with CLAUDE.md."""
        await _send_claude_command(ctx, "/init", deps)

    @app.command(get_command_name("/memory"))
    @slack_command()
    async def handle_memory(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /memory command - edit CLAUDE.md memory files."""
        await _send_claude_command(ctx, "/memory", deps)

    @app.command(get_command_name("/review"))
    @slack_command()
    async def handle_review(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /review command - request code review."""
        await _send_claude_command(ctx, "/review", deps)

    @app.command(get_command_name("/permissions"))
    @slack_command()
    async def handle_permissions(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /permissions command - view or update permissions."""
        # Note: /permissions only works in Claude CLI interactive mode, not with -p flag.
        # In print mode, slash commands get interpreted as skill invocations.
        # Show info about how to manage permissions in Slack mode.
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )
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
                            "• `/mode default` - Ask for approval on sensitive operations\n"
                            "• `/mode plan` - Plan-only mode (no execution)\n"
                            "• `/mode acceptEdits` - Auto-approve file edits\n"
                            "• `/mode bypassPermissions` - Skip all permission checks"
                        ),
                    },
                }
            ],
        )

    @app.command(get_command_name("/stats"))
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

    @app.command(get_command_name("/todos"))
    @slack_command()
    async def handle_todos(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /todos command - list current TODO items."""
        await _send_claude_command(ctx, "/todos", deps)

    @app.command(get_command_name("/mcp"))
    @slack_command()
    async def handle_mcp(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /mcp command - show MCP server configuration."""
        if ctx.text:
            await _send_claude_command(ctx, f"/mcp {ctx.text}", deps)
        else:
            await _send_claude_command(ctx, "/mcp", deps)
