"""Git integration command handlers: /diff, /status, /checkpoint, /undo, /commit, /branch."""

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.git.service import GitError, GitService
from src.utils.formatting import SlackFormatter

from ..base import CommandContext, HandlerDependencies, get_command_name, slack_command


def register_git_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register git integration command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """
    git_service = GitService()

    @app.command(get_command_name("/diff"))
    @slack_command()
    async def handle_diff(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /diff [--staged] command - show git diff of uncommitted changes."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        # Check if --staged flag is present
        staged = "--staged" in ctx.text or "--cached" in ctx.text

        try:
            # Validate git repo
            if not await git_service.validate_git_repo(session.working_directory):
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text="Not a git repository",
                    blocks=SlackFormatter.error_message(
                        f"Not a git repository: {session.working_directory}"
                    ),
                )
                return

            # Get diff
            diff = await git_service.get_diff(session.working_directory, staged=staged)

            if not diff:
                message = "No staged changes" if staged else "No uncommitted changes"
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text=message,
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f":heavy_check_mark: {message}",
                            },
                        }
                    ],
                )
                return

            # Truncate diff if too large
            max_length = 2800
            if len(diff) > max_length:
                diff_truncated = diff[:max_length]
                diff_display = (
                    f"```\n{diff_truncated}\n```\n\n_... (diff truncated, {len(diff)} chars total)_"
                )
            else:
                diff_display = f"```\n{diff}\n```"

            header = ":page_facing_up: Git Diff (Staged)" if staged else ":page_facing_up: Git Diff"

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Git diff: {len(diff)} chars",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"{header}\n{diff_display}",
                        },
                    }
                ],
            )

        except GitError as e:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Git error: {e}",
                blocks=SlackFormatter.error_message(f"Git error: {e}"),
            )

    @app.command(get_command_name("/status"))
    @slack_command()
    async def handle_status(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /status command - show git status."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        try:
            # Validate git repo
            if not await git_service.validate_git_repo(session.working_directory):
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text="Not a git repository",
                    blocks=SlackFormatter.error_message(
                        f"Not a git repository: {session.working_directory}"
                    ),
                )
                return

            # Get status
            status = await git_service.get_status(session.working_directory)

            # Build status message
            status_lines = []
            status_lines.append(f"*Branch:* `{status.branch}`")

            if status.ahead > 0:
                status_lines.append(f"*Ahead:* {status.ahead} commit(s)")
            if status.behind > 0:
                status_lines.append(f"*Behind:* {status.behind} commit(s)")

            if status.staged:
                status_lines.append("\n*Staged changes:*")
                for file_path in status.staged[:10]:
                    status_lines.append(f"  :heavy_check_mark: {file_path}")
                if len(status.staged) > 10:
                    status_lines.append(f"  _... and {len(status.staged) - 10} more_")

            if status.unstaged:
                status_lines.append("\n*Unstaged changes:*")
                for file_path in status.unstaged[:10]:
                    status_lines.append(f"  :pencil2: {file_path}")
                if len(status.unstaged) > 10:
                    status_lines.append(f"  _... and {len(status.unstaged) - 10} more_")

            if status.untracked:
                status_lines.append("\n*Untracked files:*")
                for file_path in status.untracked[:10]:
                    status_lines.append(f"  :question: {file_path}")
                if len(status.untracked) > 10:
                    status_lines.append(f"  _... and {len(status.untracked) - 10} more_")

            if not status.staged and not status.unstaged and not status.untracked:
                status_lines.append("\n:heavy_check_mark: Working tree clean")

            status_text = "\n".join(status_lines)

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Git status: {status.branch}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":git: *Git Status*\n\n{status_text}",
                        },
                    }
                ],
            )

        except GitError as e:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Git error: {e}",
                blocks=SlackFormatter.error_message(f"Git error: {e}"),
            )

    @app.command(get_command_name("/commit"))
    @slack_command(require_text=True, usage_hint="Usage: /commit <message>")
    async def handle_commit(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /commit <message> command - commit staged changes."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        try:
            # Validate git repo
            if not await git_service.validate_git_repo(session.working_directory):
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text="Not a git repository",
                    blocks=SlackFormatter.error_message(
                        f"Not a git repository: {session.working_directory}"
                    ),
                )
                return

            # Commit changes
            commit_sha = await git_service.commit_changes(session.working_directory, ctx.text)

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Committed: {commit_sha[:7]}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f":heavy_check_mark: *Committed* `{commit_sha[:7]}`\n> {ctx.text}",
                        },
                    }
                ],
            )

        except GitError as e:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Git error: {e}",
                blocks=SlackFormatter.error_message(f"Git error: {e}"),
            )

    @app.command(get_command_name("/branch"))
    @slack_command()
    async def handle_branch(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /branch [create <name> | switch <name>] command."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        try:
            # Validate git repo
            if not await git_service.validate_git_repo(session.working_directory):
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text="Not a git repository",
                    blocks=SlackFormatter.error_message(
                        f"Not a git repository: {session.working_directory}"
                    ),
                )
                return

            parts = ctx.text.split() if ctx.text else []

            if not parts:
                # List branches (show current)
                status = await git_service.get_status(session.working_directory)
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text=f"Current branch: {status.branch}",
                    blocks=[
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": f":git: Current branch: `{status.branch}`",
                            },
                        }
                    ],
                )
                return

            subcommand = parts[0]

            if subcommand == "create" and len(parts) >= 2:
                branch_name = parts[1]
                success = await git_service.create_branch(
                    session.working_directory, branch_name, switch=True
                )
                if success:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        text=f"Created and switched to branch: {branch_name}",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f":heavy_check_mark: Created and switched to branch `{branch_name}`",
                                },
                            }
                        ],
                    )

            elif subcommand == "switch" and len(parts) >= 2:
                branch_name = parts[1]
                success = await git_service.switch_branch(session.working_directory, branch_name)
                if success:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        text=f"Switched to branch: {branch_name}",
                        blocks=[
                            {
                                "type": "section",
                                "text": {
                                    "type": "mrkdwn",
                                    "text": f":heavy_check_mark: Switched to branch `{branch_name}`",
                                },
                            }
                        ],
                    )

            else:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text="Invalid usage",
                    blocks=SlackFormatter.error_message(
                        "Usage: `/branch` or `/branch create <name>` or `/branch switch <name>`"
                    ),
                )

        except GitError as e:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Git error: {e}",
                blocks=SlackFormatter.error_message(f"Git error: {e}"),
            )
