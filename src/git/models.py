"""Git data models."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GitStatus:
    """Git status information."""

    branch: str = "unknown"
    modified: list[str] = field(default_factory=list)
    staged: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    ahead: int = 0
    behind: int = 0
    is_clean: bool = False

    def has_changes(self) -> bool:
        """Check if there are any uncommitted changes."""
        return bool(self.modified or self.staged or self.untracked)

    def summary(self) -> str:
        """Get human-readable summary."""
        parts = [f"Branch: {self.branch}"]

        if self.ahead > 0:
            parts.append(f"{self.ahead} ahead")
        if self.behind > 0:
            parts.append(f"{self.behind} behind")

        if self.is_clean:
            parts.append("(clean)")
        else:
            changes = []
            if self.staged:
                changes.append(f"{len(self.staged)} staged")
            if self.modified:
                changes.append(f"{len(self.modified)} modified")
            if self.untracked:
                changes.append(f"{len(self.untracked)} untracked")
            if changes:
                parts.append(f"({', '.join(changes)})")

        return " | ".join(parts)


@dataclass
class Checkpoint:
    """Git checkpoint (stash) information."""

    name: str
    stash_ref: str
    message: Optional[str] = None
    description: Optional[str] = None
    is_auto: bool = False

    def display_name(self) -> str:
        """Get display name for UI."""
        if self.is_auto:
            return f"{self.name} (auto)"
        return self.name


@dataclass
class Worktree:
    """Git worktree information."""

    path: str
    branch: str
    commit: str = ""
    is_main: bool = False
    is_detached: bool = False
    is_locked: bool = False
    lock_reason: Optional[str] = None
    is_prunable: bool = False
    prunable_reason: Optional[str] = None
