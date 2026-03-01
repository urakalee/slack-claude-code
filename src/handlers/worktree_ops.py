"""Shared worktree operation helpers for commands and interactive actions."""

from pathlib import Path
from typing import Optional

from src.database.models import Session
from src.git.models import Worktree
from src.git.service import GitService

from .base import HandlerDependencies


def path_is_within(path: Path, root: Path) -> bool:
    """Return True when path is root or a descendant of root."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def find_current_worktree(session_cwd: str, worktrees: list[Worktree]) -> Optional[Worktree]:
    """Return worktree containing the current session cwd."""
    session_path = Path(session_cwd).resolve()
    for worktree in worktrees:
        worktree_path = Path(worktree.path).resolve()
        if path_is_within(session_path, worktree_path):
            return worktree
    return None


def find_worktree_by_target(target: str, worktrees: list[Worktree]) -> Optional[Worktree]:
    """Find a worktree by branch first, then by absolute path."""
    for worktree in worktrees:
        if worktree.branch == target:
            return worktree

    target_path = Path(target).expanduser().resolve()
    for worktree in worktrees:
        if Path(worktree.path).resolve() == target_path:
            return worktree
    return None


async def switch_session_to_worktree(
    deps: HandlerDependencies,
    session: Session,
    channel_id: str,
    thread_ts: Optional[str],
    target_path: str,
) -> bool:
    """Set session cwd and clear backend session IDs when directory changed.

    Returns
    -------
    bool
        True when cwd changed, False when it was already the current path.
    """
    current_path = Path(session.working_directory).resolve()
    new_path = Path(target_path).resolve()
    changed = current_path != new_path

    await deps.db.update_session_cwd(channel_id, thread_ts, target_path)

    if changed:
        await deps.db.clear_session_claude_id(channel_id, thread_ts)
        if session.codex_session_id:
            await deps.db.clear_session_codex_id(channel_id, thread_ts)

    return changed


async def worktree_is_clean(git_service: GitService, worktree_path: str) -> bool:
    """Return True if a worktree has no staged/modified/untracked files."""
    status = await git_service.get_status(worktree_path)
    return status.is_clean
