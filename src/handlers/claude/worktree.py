"""Git worktree command handlers: /worktree add|list|switch|merge|remove|prune."""

import json
import shlex
from typing import Optional

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.database.models import Session
from src.git.models import Worktree
from src.git.service import GitError, GitService
from src.handlers.worktree_ops import (
    find_current_worktree,
    find_worktree_by_target,
    switch_session_to_worktree,
    worktree_is_clean,
)
from src.utils.formatters.command import error_message

from ..base import CommandContext, HandlerDependencies, slack_command


def _parse_worktree_tokens(tokens: list[str]) -> tuple[list[str], set[str], dict[str, str]]:
    """Parse positional args and flags for /worktree subcommands."""
    flag_only = {
        "--stay",
        "--verbose",
        "--keep-worktree",
        "--force",
        "--delete-branch",
        "--dry-run",
    }
    value_flags = {"--from", "--into"}

    args: list[str] = []
    flags: set[str] = set()
    values: dict[str, str] = {}

    idx = 0
    while idx < len(tokens):
        token = tokens[idx]
        if token in flag_only:
            flags.add(token)
            idx += 1
            continue
        if token in value_flags:
            if idx + 1 >= len(tokens):
                raise GitError(f"Missing value for flag `{token}`")
            values[token] = tokens[idx + 1]
            idx += 2
            continue
        args.append(token)
        idx += 1

    return args, flags, values


def _worktree_tags(worktree: Worktree, is_current: bool) -> str:
    """Format short status tags for worktree list output."""
    tags: list[str] = []
    if worktree.is_main:
        tags.append("main")
    if is_current:
        tags.append("current")
    if worktree.is_detached:
        tags.append("detached")
    if worktree.is_locked:
        tags.append("locked")
    if worktree.is_prunable:
        tags.append("prunable")
    if not tags:
        return ""
    return f" _({', '.join(tags)})_"


def _build_worktree_action_blocks(worktree: Worktree) -> list[dict]:
    """Build action buttons for a non-current worktree row."""
    payload = json.dumps({"branch": worktree.branch, "path": worktree.path}, separators=(",", ":"))
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Switch"},
                    "action_id": "worktree_switch",
                    "value": payload,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Merge -> current"},
                    "action_id": "worktree_merge_current",
                    "value": payload,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Remove"},
                    "style": "danger",
                    "action_id": "worktree_remove",
                    "value": payload,
                    "confirm": {
                        "title": {"type": "plain_text", "text": "Remove worktree?"},
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                f"Remove worktree `{worktree.branch}` at `{worktree.path}`? "
                                "Uncommitted changes will fail unless you force remove."
                            ),
                        },
                        "confirm": {"type": "plain_text", "text": "Remove"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                },
            ],
        }
    ]


async def _send_git_error(ctx: CommandContext, message: str) -> None:
    """Send a standardized git error response."""
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"Git error: {message}",
        blocks=error_message(f"Git error: {message}"),
    )


def register_worktree_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register worktree command handlers."""
    git_service = GitService()

    async def _handle_worktree(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /worktree command dispatch."""
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
                    blocks=error_message(f"Not a git repository: {session.working_directory}"),
                )
                return

            if not ctx.text:
                await _send_usage(ctx)
                return

            try:
                parts = shlex.split(ctx.text)
            except ValueError as e:
                raise GitError(f"Could not parse command: {e}")

            if not parts:
                await _send_usage(ctx)
                return

            subcommand = parts[0].lower()
            args, flags, values = _parse_worktree_tokens(parts[1:])

            if (
                subcommand == "add"
                and len(args) == 1
                and flags.issubset({"--stay"})
                and set(values.keys()).issubset({"--from"})
            ):
                await _handle_add(
                    ctx,
                    deps,
                    session,
                    git_service,
                    args[0],
                    from_ref=values.get("--from"),
                    stay=("--stay" in flags),
                )
                return

            if (
                subcommand == "list"
                and not args
                and flags.issubset({"--verbose"})
                and not values
            ):
                await _handle_list(ctx, session, git_service, verbose=("--verbose" in flags))
                return

            if subcommand == "switch" and len(args) == 1 and not flags and not values:
                await _handle_switch(ctx, deps, session, git_service, args[0])
                return

            if (
                subcommand == "merge"
                and len(args) == 1
                and flags.issubset({"--keep-worktree"})
                and set(values.keys()).issubset({"--into"})
            ):
                await _handle_merge(
                    ctx,
                    deps,
                    session,
                    git_service,
                    args[0],
                    into_target=values.get("--into"),
                    keep_worktree=("--keep-worktree" in flags),
                )
                return

            if (
                subcommand == "remove"
                and len(args) == 1
                and flags.issubset({"--force", "--delete-branch"})
                and not values
            ):
                await _handle_remove(
                    ctx,
                    deps,
                    session,
                    git_service,
                    args[0],
                    force=("--force" in flags),
                    delete_branch=("--delete-branch" in flags),
                )
                return

            if (
                subcommand == "prune"
                and not args
                and flags.issubset({"--dry-run"})
                and not values
            ):
                await _handle_prune(ctx, session, git_service, dry_run=("--dry-run" in flags))
                return

            await _send_usage(ctx)

        except GitError as e:
            await _send_git_error(ctx, str(e))

    handle_worktree = slack_command()(_handle_worktree)
    app.command("/worktree")(handle_worktree)
    app.command("/wt")(handle_worktree)


async def _handle_add(
    ctx: CommandContext,
    deps: HandlerDependencies,
    session: Session,
    git_service: GitService,
    branch_name: str,
    from_ref: Optional[str] = None,
    stay: bool = False,
) -> None:
    """Create a new worktree and optionally switch session to it."""
    worktree_path = await git_service.add_worktree(
        session.working_directory, branch_name, from_ref=from_ref
    )

    switch_note = "Session kept on current worktree."
    if not stay:
        await switch_session_to_worktree(
            deps,
            session,
            ctx.channel_id,
            ctx.thread_ts,
            worktree_path,
        )
        switch_note = "Session switched to new worktree."

    from_note = f"\n*Base ref:* `{from_ref}`" if from_ref else ""

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
                        f"*Branch:* `{branch_name}`{from_note}\n"
                        f"*Path:* `{worktree_path}`\n\n"
                        f"{switch_note}"
                    ),
                },
            }
        ],
    )


async def _handle_list(
    ctx: CommandContext,
    session: Session,
    git_service: GitService,
    verbose: bool = False,
) -> None:
    """List worktrees with optional status metadata and action buttons."""
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

    current = find_current_worktree(session.working_directory, worktrees)

    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":git: *Git Worktrees*",
            },
        }
    ]

    for worktree in worktrees:
        is_current = bool(current and worktree.path == current.path)
        detail = ""
        if verbose:
            try:
                status = await git_service.get_status(worktree.path)
                detail = (
                    f"\n• status: `{'clean' if status.is_clean else 'dirty'}`"
                    f" | ahead: `{status.ahead}` | behind: `{status.behind}`"
                )
            except GitError:
                detail = "\n• status: `unknown`"

        tag_text = _worktree_tags(worktree, is_current)
        reason_lines = ""
        if worktree.lock_reason:
            reason_lines += f"\n• lock reason: `{worktree.lock_reason}`"
        if worktree.prunable_reason:
            reason_lines += f"\n• prunable reason: `{worktree.prunable_reason}`"

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"`{worktree.branch}`{tag_text}\n"
                        f"`{worktree.path}`"
                        f"{detail}{reason_lines}"
                    ),
                },
            }
        )

        if not is_current and not worktree.is_main and not worktree.is_detached:
            blocks.extend(_build_worktree_action_blocks(worktree))

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text="Git worktrees",
        blocks=blocks,
    )


async def _handle_switch(
    ctx: CommandContext,
    deps: HandlerDependencies,
    session: Session,
    git_service: GitService,
    target: str,
) -> None:
    """Switch session to an existing worktree by branch name or path."""
    worktrees = await git_service.list_worktrees(session.working_directory)
    match = find_worktree_by_target(target, worktrees)

    if match is None:
        available = ", ".join(f"`{worktree.branch}`" for worktree in worktrees)
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Worktree not found: {target}",
            blocks=error_message(
                f"No worktree found for `{target}`.\n" f"Available branches: {available}"
            ),
        )
        return

    changed = await switch_session_to_worktree(
        deps,
        session,
        ctx.channel_id,
        ctx.thread_ts,
        match.path,
    )

    switched_text = "Switched" if changed else "Already using"
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"{switched_text} worktree: {match.branch}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":heavy_check_mark: {switched_text} worktree `{match.branch}`\n"
                        f"*Path:* `{match.path}`"
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
    source_target: str,
    into_target: Optional[str] = None,
    keep_worktree: bool = False,
) -> None:
    """Merge a source worktree branch into current or selected target worktree branch."""
    worktrees = await git_service.list_worktrees(session.working_directory)

    source_wt = find_worktree_by_target(source_target, worktrees)
    if source_wt is None:
        available = ", ".join(f"`{worktree.branch}`" for worktree in worktrees)
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Source not found: {source_target}",
            blocks=error_message(
                f"No worktree found for source `{source_target}`.\n" f"Available: {available}"
            ),
        )
        return

    current_wt = find_current_worktree(session.working_directory, worktrees)
    if current_wt is None:
        raise GitError("Could not determine current worktree from session path")

    target_wt = current_wt
    if into_target:
        into_match = find_worktree_by_target(into_target, worktrees)
        if into_match is None:
            raise GitError(f"Target worktree `{into_target}` not found")
        target_wt = into_match

    if source_wt.is_detached:
        raise GitError("Cannot merge from detached HEAD worktree")

    if source_wt.path == target_wt.path:
        raise GitError("Source and target worktrees are the same")

    if not await worktree_is_clean(git_service, target_wt.path):
        raise GitError(
            f"Target worktree `{target_wt.branch}` has uncommitted changes. " "Commit/stash first."
        )

    await switch_session_to_worktree(
        deps,
        session,
        ctx.channel_id,
        ctx.thread_ts,
        target_wt.path,
    )

    success, message = await git_service.merge_branch(target_wt.path, source_wt.branch)

    if not success:
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            text=f"Merge conflicts with {source_wt.branch}",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f":warning: *Merge conflicts detected*\n\n"
                            f"Merging `{source_wt.branch}` into `{target_wt.branch}`\n\n"
                            f"```\n{message}\n```"
                        ),
                    },
                }
            ],
        )
        return

    cleanup_note = "\nSource worktree kept."
    if not keep_worktree:
        try:
            if await worktree_is_clean(git_service, source_wt.path):
                await git_service.remove_worktree(target_wt.path, source_wt.path)
                cleanup_note = "\nSource worktree removed (clean)."
            else:
                cleanup_note = "\nSource worktree kept because it is dirty."
        except GitError as e:
            cleanup_note = f"\nSource worktree was not removed: {e}"

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"Merged {source_wt.branch} into {target_wt.branch}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":heavy_check_mark: *Merge successful*\n\n"
                        f"Merged `{source_wt.branch}` into `{target_wt.branch}`\n"
                        f"Session now points to `{target_wt.path}`"
                        f"{cleanup_note}"
                    ),
                },
            }
        ],
    )


async def _handle_remove(
    ctx: CommandContext,
    deps: HandlerDependencies,
    session: Session,
    git_service: GitService,
    target: str,
    force: bool = False,
    delete_branch: bool = False,
) -> None:
    """Remove a non-main, non-current worktree by branch name or path."""
    worktrees = await git_service.list_worktrees(session.working_directory)

    match = find_worktree_by_target(target, worktrees)
    if match is None:
        raise GitError(f"No worktree found for `{target}`")

    current = find_current_worktree(session.working_directory, worktrees)
    if current and current.path == match.path:
        raise GitError("Cannot remove the current session worktree")

    if match.is_main:
        raise GitError("Cannot remove the main worktree")

    if not force and not await worktree_is_clean(git_service, match.path):
        raise GitError("Worktree has uncommitted changes; retry with `--force`")

    await git_service.remove_worktree(session.working_directory, match.path, force=force)

    delete_note = ""
    if delete_branch and not match.is_detached:
        try:
            await git_service.delete_branch(session.working_directory, match.branch)
            delete_note = "\nLocal branch deleted."
        except GitError as e:
            delete_note = f"\nWorktree removed, but branch deletion failed: {e}"

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text=f"Removed worktree: {match.branch}",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":wastebasket: *Worktree removed*\n\n"
                        f"*Branch:* `{match.branch}`\n"
                        f"*Path:* `{match.path}`"
                        f"{delete_note}"
                    ),
                },
            }
        ],
    )


async def _handle_prune(
    ctx: CommandContext,
    session: Session,
    git_service: GitService,
    dry_run: bool = False,
) -> None:
    """Run git worktree prune, optionally in dry-run mode."""
    result = await git_service.prune_worktrees(session.working_directory, dry_run=dry_run)
    mode_text = "(dry-run)" if dry_run else ""

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text="Pruned worktrees",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (f":broom: *Worktree prune* {mode_text}\n\n" f"```\n{result}\n```"),
                },
            }
        ],
    )


async def _send_usage(ctx: CommandContext) -> None:
    """Send usage information for /worktree command."""
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        text="Worktree usage",
        blocks=error_message(
            "Usage:\n"
            "  `/worktree add <branch> [--from <ref>] [--stay]`\n"
            "  `/worktree list [--verbose]`\n"
            "  `/worktree switch <branch-or-path>`\n"
            "  `/worktree merge <source> [--into <target>] [--keep-worktree]`\n"
            "  `/worktree remove <branch-or-path> [--force] [--delete-branch]`\n"
            "  `/worktree prune [--dry-run]`"
        ),
    )
