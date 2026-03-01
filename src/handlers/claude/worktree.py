"""Git worktree command handlers: /worktree add|list|switch|merge."""

from pathlib import Path

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.database.models import Session
from src.git.service import GitError, GitService
from src.utils.formatting import SlackFormatter

from ..base import CommandContext, HandlerDependencies, slack_command, get_command_name


def register_worktree_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register worktree command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """
    git_service = GitService()

    async def _handle_worktree(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /worktree [add|list|switch|merge] <args> command."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id,
            thread_ts=ctx.thread_ts,
            default_cwd=config.DEFAULT_WORKING_DIR,
        )

        try:
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
                await _send_usage(ctx)
                return

            subcommand = parts[0]

            if subcommand == "add" and len(parts) >= 2:
                await _handle_add(ctx, deps, session, git_service, parts[1])
            elif subcommand == "list":
                await _handle_list(ctx, session, git_service)
            elif subcommand == "switch" and len(parts) >= 2:
                await _handle_switch(ctx, deps, session, git_service, parts[1])
            elif subcommand == "merge" and len(parts) >= 2:
                await _handle_merge(ctx, deps, session, git_service, parts[1])
            else:
                await _send_usage(ctx)

        except GitError as e:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Git error: {e}",
                blocks=SlackFormatter.error_message(f"Git error: {e}"),
            )

    # Register both /worktree and /wt alias
    handle_worktree = slack_command()(_handle_worktree)
    app.command("/worktree")(handle_worktree)
    app.command("/wt")(handle_worktree)


async def _handle_add(
    ctx: CommandContext,
    deps: HandlerDependencies,
    session: Session,
    git_service: GitService,
    branch_name: str,
) -> None:
    """Create a new worktree and switch session to it."""
    worktree_path = await git_service.add_worktree(
        session.working_directory, branch_name
    )

    # Update session: new cwd, clear session IDs for fresh context
    await deps.db.update_session_cwd(ctx.channel_id, ctx.thread_ts, worktree_path)
    await deps.db.clear_session_claude_id(ctx.channel_id, ctx.thread_ts)
    if session.codex_session_id:
        await deps.db.clear_session_codex_id(ctx.channel_id, ctx.thread_ts)

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"Created worktree: {branch_name}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":heavy_check_mark: *Worktree created*\n\n"
                        f"*Branch:* `{branch_name}`\n"
                        f"*Path:* `{worktree_path}`\n\n"
                        f"Session working directory updated. "
                        f"Claude session cleared for fresh context."
                    ),
                },
            }
        ],
    )


async def _handle_list(
    ctx: CommandContext,
    session: Session,
    git_service: GitService,
) -> None:
    """List all worktrees, highlighting the current one."""
    worktrees = await git_service.list_worktrees(session.working_directory)

    if not worktrees:
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text="No worktrees found",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": ":information_source: No worktrees found.",
                    },
                }
            ],
        )
        return

    session_cwd = str(Path(session.working_directory).resolve())

    lines = []
    for wt in worktrees:
        wt_resolved = str(Path(wt.path).resolve())
        is_current = session_cwd.startswith(wt_resolved)
        pointer = " :point_left: _current_" if is_current else ""
        main_tag = " _(main)_" if wt.is_main else ""
        lines.append(f"  `{wt.branch}`{main_tag} - `{wt.path}`{pointer}")

    text = "\n".join(lines)

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text="Git worktrees",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":git: *Git Worktrees*\n\n{text}",
                },
            }
        ],
    )


async def _handle_switch(
    ctx: CommandContext,
    deps: HandlerDependencies,
    session: Session,
    git_service: GitService,
    target: str,
) -> None:
    """Switch session to an existing worktree by branch name."""
    worktrees = await git_service.list_worktrees(session.working_directory)

    match = None
    for wt in worktrees:
        if wt.branch == target:
            match = wt
            break

    if match is None:
        available = ", ".join(f"`{wt.branch}`" for wt in worktrees)
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Worktree not found: {target}",
            blocks=SlackFormatter.error_message(
                f"No worktree found for branch `{target}`.\n"
                f"Available: {available}"
            ),
        )
        return

    await deps.db.update_session_cwd(ctx.channel_id, ctx.thread_ts, match.path)
    await deps.db.clear_session_claude_id(ctx.channel_id, ctx.thread_ts)
    if session.codex_session_id:
        await deps.db.clear_session_codex_id(ctx.channel_id, ctx.thread_ts)

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"Switched to worktree: {target}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":heavy_check_mark: Switched to worktree `{target}`\n"
                        f"*Path:* `{match.path}`\n\n"
                        f"Claude session cleared for fresh context."
                    ),
                },
            }
        ],
    )


async def _handle_merge(
    ctx: CommandContext,
    deps: HandlerDependencies,
    session: Session,
    git_service: GitService,
    branch_name: str,
) -> None:
    """Merge a worktree's branch into the main worktree's branch."""
    worktrees = await git_service.list_worktrees(session.working_directory)

    main_wt = None
    source_wt = None
    for wt in worktrees:
        if wt.is_main:
            main_wt = wt
        if wt.branch == branch_name:
            source_wt = wt

    if main_wt is None:
        raise GitError("Could not find main worktree")

    if source_wt is None:
        available = ", ".join(f"`{wt.branch}`" for wt in worktrees if not wt.is_main)
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Branch not found: {branch_name}",
            blocks=SlackFormatter.error_message(
                f"No worktree found for branch `{branch_name}`.\n"
                f"Available branches: {available}"
            ),
        )
        return

    # Switch session to main worktree before merging
    await deps.db.update_session_cwd(ctx.channel_id, ctx.thread_ts, main_wt.path)
    await deps.db.clear_session_claude_id(ctx.channel_id, ctx.thread_ts)
    if session.codex_session_id:
        await deps.db.clear_session_codex_id(ctx.channel_id, ctx.thread_ts)

    # Perform merge in the main worktree
    success, message = await git_service.merge_branch(main_wt.path, branch_name)

    if success:
        cleanup_note = ""
        try:
            await git_service.remove_worktree(main_wt.path, source_wt.path)
            cleanup_note = "\nWorktree removed after successful merge."
        except GitError:
            cleanup_note = (
                "\nNote: Worktree was not removed. "
                "Use `/worktree list` to see active worktrees."
            )

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Merged {branch_name} successfully",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":heavy_check_mark: *Merge successful*\n\n"
                            f"Merged `{branch_name}` into `{main_wt.branch}`\n"
                            f"Session switched to main worktree: `{main_wt.path}`"
                            f"{cleanup_note}"
                        ),
                    },
                }
            ],
        )
    else:
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Merge conflicts with {branch_name}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":warning: *Merge conflicts detected*\n\n"
                            f"Merging `{branch_name}` into `{main_wt.branch}`\n\n"
                            f"```\n{message}\n```\n\n"
                            f"Session switched to main worktree: `{main_wt.path}`\n"
                            f"Resolve conflicts and commit, or run "
                            f"`git merge --abort` to cancel."
                        ),
                    },
                }
            ],
        )


async def _send_usage(ctx: CommandContext) -> None:
    """Send usage information for /worktree command."""
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text="Worktree usage",
        blocks=SlackFormatter.error_message(
            "Usage:\n"
            "  `/worktree add <branch-name>` - Create new worktree\n"
            "  `/worktree list` - List all worktrees\n"
            "  `/worktree switch <branch-name>` - Switch to worktree\n"
            "  `/worktree merge <branch-name>` - Merge branch into main"
        ),
    )
