"""Handler registration for Slack commands and actions."""

from slack_bolt.async_app import AsyncApp

from src.claude.subprocess_executor import SubprocessExecutor as ClaudeExecutor
from src.codex.subprocess_executor import SubprocessExecutor as CodexExecutor
from src.database.repository import DatabaseRepository

from .base import HandlerDependencies
from .basic import register_basic_commands
from .notifications import register_notifications_command

# Claude-specific handlers
from .claude import (
    register_agents_command,
    register_cancel_commands,
    register_claude_cli_commands,
    register_git_commands,
    register_mode_command,
    register_parallel_commands,
    register_queue_commands,
    register_worktree_commands,
)


def register_commands(
    app: AsyncApp,
    db: DatabaseRepository,
    executor: ClaudeExecutor,
    codex_executor: CodexExecutor = None,
) -> HandlerDependencies:
    """Register all slash command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    db : DatabaseRepository
        Database repository instance.
    executor : ClaudeExecutor
        Claude executor instance.
    codex_executor : CodexExecutor, optional
        Codex subprocess executor instance.

    Returns
    -------
    HandlerDependencies
        Container with shared dependencies for access by action handlers.
    """
    deps = HandlerDependencies(
        db=db,
        executor=executor,
        codex_executor=codex_executor,
    )

    # Shared handlers (work with any backend)
    register_basic_commands(app, deps)
    register_notifications_command(app, deps)

    # Claude-specific handlers
    register_parallel_commands(app, deps)
    register_queue_commands(app, deps)
    register_claude_cli_commands(app, deps)
    register_agents_command(app, deps)
    register_mode_command(app, deps)
    register_git_commands(app, deps)
    register_worktree_commands(app, deps)
    register_cancel_commands(app, deps)

    return deps
