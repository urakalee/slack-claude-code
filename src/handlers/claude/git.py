"""Git integration command handlers consolidated under `/git`."""

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.git.models import GitStatus
from src.git.service import GitError, GitService
from src.utils.formatters.command import error_message

from ..base import CommandContext, HandlerDependencies, slack_command


async def _send_not_git_repo(ctx: CommandContext, working_directory: str) -> None:
    """Send a standardized non-repo error response."""
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text="Not a git repository",
        blocks=error_message(f"Not a git repository: {working_directory}"),
    )


async def _send_git_error(ctx: CommandContext, message: str) -> None:
    """Send a standardized git error response."""
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"Git error: {message}",
        blocks=error_message(f"Git error: {message}"),
    )


async def _send_git_usage(ctx: CommandContext) -> None:
    """Send usage information for `/git` command."""
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text="Git usage",
        blocks=error_message(
            "Usage:\n"
            "  `/git status`\n"
            "  `/git diff [--staged|--cached]`\n"
            "  `/git commit <message>`\n"
            "  `/git branch`\n"
            "  `/git branch create <name>`\n"
            "  `/git branch switch <name>`\n"
            "  `/worktree ...` (worktree operations)"
        ),
    )


def _format_status_text(status: GitStatus) -> str:
    """Build a Slack-friendly git status summary body."""
    status_lines: list[str] = [f"*Branch:* `{status.branch}`"]

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

    if status.modified:
        status_lines.append("\n*Unstaged changes:*")
        for file_path in status.modified[:10]:
            status_lines.append(f"  :pencil2: {file_path}")
        if len(status.modified) > 10:
            status_lines.append(f"  _... and {len(status.modified) - 10} more_")

    if status.untracked:
        status_lines.append("\n*Untracked files:*")
        for file_path in status.untracked[:10]:
            status_lines.append(f"  :question: {file_path}")
        if len(status.untracked) > 10:
            status_lines.append(f"  _... and {len(status.untracked) - 10} more_")

    if not status.staged and not status.modified and not status.untracked:
        status_lines.append("\n:heavy_check_mark: Working tree clean")

    return "\n".join(status_lines)


async def _handle_status(
    ctx: CommandContext, git_service: GitService, working_directory: str
) -> None:
    """Handle `/git status`."""
    status = await git_service.get_status(working_directory)
    status_text = _format_status_text(status)

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


async def _handle_diff(
    ctx: CommandContext,
    git_service: GitService,
    working_directory: str,
    tokens: list[str],
) -> None:
    """Handle `/git diff [--staged|--cached]`."""
    if not set(tokens).issubset({"--staged", "--cached"}):
        await _send_git_usage(ctx)
        return

    staged = "--staged" in tokens or "--cached" in tokens
    diff = await git_service.get_diff(working_directory, staged=staged)

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


async def _handle_commit(
    ctx: CommandContext,
    git_service: GitService,
    working_directory: str,
    message: str,
) -> None:
    """Handle `/git commit <message>`."""
    if not message:
        await _send_git_usage(ctx)
        return

    commit_sha = await git_service.commit_changes(working_directory, message)

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"Committed: {commit_sha[:7]}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":heavy_check_mark: *Committed* `{commit_sha[:7]}`\n> {message}",
                },
            }
        ],
    )


async def _handle_branch(
    ctx: CommandContext,
    git_service: GitService,
    working_directory: str,
    tokens: list[str],
) -> None:
    """Handle `/git branch` commands."""
    if not tokens:
        status = await git_service.get_status(working_directory)
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

    if len(tokens) == 2 and tokens[0] == "create":
        branch_name = tokens[1]
        success = await git_service.create_branch(working_directory, branch_name, switch=True)
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
        return

    if len(tokens) == 2 and tokens[0] == "switch":
        branch_name = tokens[1]
        success = await git_service.switch_branch(working_directory, branch_name)
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
        return

    await _send_git_usage(ctx)


def register_git_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register `/git` command handler."""
    git_service = GitService()

    @app.command("/git")
    @slack_command()
    async def handle_git(ctx: CommandContext, deps: HandlerDependencies = deps) -> None:
        """Dispatch `/git` subcommands."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )

        text = ctx.text.strip()
        if not text:
            await _send_git_usage(ctx)
            return

        head, _, tail = text.partition(" ")
        command = head.lower()
        tail = tail.strip()

        if command == "help":
            await _send_git_usage(ctx)
            return

        if not await git_service.validate_git_repo(session.working_directory):
            await _send_not_git_repo(ctx, session.working_directory)
            return

        try:
            if command == "status":
                if tail:
                    await _send_git_usage(ctx)
                    return
                await _handle_status(ctx, git_service, session.working_directory)
                return

            if command == "diff":
                tokens = tail.split() if tail else []
                await _handle_diff(ctx, git_service, session.working_directory, tokens)
                return

            if command == "commit":
                await _handle_commit(ctx, git_service, session.working_directory, tail)
                return

            if command == "branch":
                tokens = tail.split() if tail else []
                await _handle_branch(ctx, git_service, session.working_directory, tokens)
                return

            await _send_git_usage(ctx)
        except GitError as e:
            await _send_git_error(ctx, str(e))
