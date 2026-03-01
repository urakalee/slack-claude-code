"""Basic command handlers: /cd, /ls, /pwd."""

from pathlib import Path

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.formatting import SlackFormatter

from .base import CommandContext, HandlerDependencies, get_command_name, slack_command


def register_basic_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register basic command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command(get_command_name("/ls"))
    @slack_command()
    async def handle_ls(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /ls [path] command - list directory contents and show cwd."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )
        base_path = Path(session.working_directory).expanduser()

        # Resolve target path (relative or absolute)
        is_cwd = not ctx.text
        if ctx.text:
            target_path = (base_path / ctx.text).resolve()
        else:
            target_path = base_path.resolve()

        # Validate path exists and is a directory
        if not target_path.exists():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Path does not exist: {target_path}",
                blocks=SlackFormatter.error_message(f"Path does not exist: {target_path}"),
            )
            return

        if not target_path.is_dir():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Not a directory: {target_path}",
                blocks=SlackFormatter.error_message(f"Not a directory: {target_path}"),
            )
            return

        # List directory contents
        try:
            entries = list(target_path.iterdir())
            # Sort: directories first, then files, alphabetically
            dirs = sorted([e for e in entries if e.is_dir()], key=lambda x: x.name.lower())
            files = sorted([e for e in entries if e.is_file()], key=lambda x: x.name.lower())
            sorted_entries = dirs + files

            # Convert to (name, is_dir) tuples for formatter
            entry_tuples = [(e.name, e.is_dir()) for e in sorted_entries]

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Contents of {target_path}",
                blocks=SlackFormatter.directory_listing(
                    str(target_path), entry_tuples, is_cwd=is_cwd
                ),
            )
        except OSError as e:
            # OSError is parent of PermissionError, FileNotFoundError, etc.
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: {e}",
                blocks=SlackFormatter.error_message(f"Cannot access directory: {e}"),
            )

    @app.command(get_command_name("/cd"))
    @slack_command()
    async def handle_cd(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /cd [path] command - change working directory with relative path support."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        if not ctx.text:
            # No argument - show current directory
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":file_folder: Current working directory: `{session.working_directory}`",
            )
            return

        # Resolve path relative to current working directory
        base_path = Path(session.working_directory).expanduser()
        target_path = (base_path / ctx.text).resolve()

        # Validate path exists and is a directory
        if not target_path.exists():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Path does not exist: {target_path}",
                blocks=SlackFormatter.error_message(f"Path does not exist: {target_path}"),
            )
            return

        if not target_path.is_dir():
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Error: Not a directory: {target_path}",
                blocks=SlackFormatter.error_message(f"Not a directory: {target_path}"),
            )
            return

        # Update working directory
        await deps.db.update_session_cwd(ctx.channel_id, ctx.thread_ts, str(target_path))
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Working directory updated to: {target_path}",
            blocks=SlackFormatter.cwd_updated(str(target_path)),
        )

    @app.command(get_command_name("/pwd"))
    @slack_command()
    async def handle_pwd(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /pwd command - print current working directory."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f":file_folder: Current working directory: `{session.working_directory}`",
        )
