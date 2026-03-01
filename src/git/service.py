"""Git operations service for version control integration."""

import asyncio
from pathlib import Path
from typing import Optional

from loguru import logger

from .models import Checkpoint, GitStatus, Worktree


class GitError(Exception):
    """Raised when git operation fails."""

    pass


class GitService:
    """Git operations service for version control integration."""

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def _validate_working_directory(self, working_directory: str) -> None:
        """Validate that working directory exists and is a directory."""
        path = Path(working_directory).expanduser().resolve()
        if not path.exists():
            raise GitError(f"Directory does not exist: {working_directory}")
        if not path.is_dir():
            raise GitError(f"Not a directory: {working_directory}")

    def _validate_branch_name(self, branch_name: str) -> None:
        """Validate branch name follows git naming rules."""
        if not branch_name or not branch_name.strip():
            raise GitError("Branch name cannot be empty")
        if len(branch_name) > 255:
            raise GitError("Branch name too long (max 255 characters)")
        # Check for invalid characters (git ref restrictions)
        invalid_chars = [" ", "~", "^", ":", "?", "*", "[", "\\", "..", "@{", "//"]
        for char in invalid_chars:
            if char in branch_name:
                raise GitError(f"Branch name contains invalid character: {char}")
        # Check for leading/trailing slashes or dots
        if branch_name.startswith("/") or branch_name.endswith("/"):
            raise GitError("Branch name cannot start or end with '/'")
        if branch_name.startswith(".") or branch_name.endswith("."):
            raise GitError("Branch name cannot start or end with '.'")
        if branch_name.endswith(".lock"):
            raise GitError("Branch name cannot end with '.lock'")

    async def _validate_branch_name_with_git(
        self, working_directory: str, branch_name: str
    ) -> None:
        """Validate branch name with git's native ref validator."""
        self._validate_branch_name(branch_name)
        _, stderr, returncode = await self._run_git_command(
            working_directory, "check-ref-format", "--branch", branch_name
        )
        if returncode != 0:
            raise GitError(f"Invalid branch name: {stderr or branch_name}")

    def _validate_commit_message(self, message: str) -> None:
        """Validate commit message is reasonable."""
        if not message or not message.strip():
            raise GitError("Commit message cannot be empty")
        if len(message) > 10000:
            raise GitError("Commit message too long (max 10000 characters)")

    async def _run_git_command(self, working_directory: str, *args: str) -> tuple[str, str, int]:
        """Run a git command and return (stdout, stderr, returncode)."""
        self._validate_working_directory(working_directory)
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=working_directory,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout
            )

            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

            returncode = process.returncode if process.returncode is not None else -1
            return stdout, stderr, returncode

        except asyncio.TimeoutError:
            if process:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            logger.error(f"Git command timed out: git {' '.join(args)}")
            raise GitError("Git command timed out")
        except Exception as e:
            if process and process.returncode is None:
                process.terminate()
                await process.wait()
            logger.error(f"Git command failed: {e}")
            raise GitError(f"Git command failed: {e}")

    async def validate_git_repo(self, working_directory: str) -> bool:
        """Check if directory is a git repository."""
        try:
            _, _, returncode = await self._run_git_command(
                working_directory, "rev-parse", "--git-dir"
            )
            return returncode == 0
        except Exception:
            return False

    async def get_status(self, working_directory: str) -> GitStatus:
        """Get git status."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        status = GitStatus()

        # Get branch info
        branch_out, _, branch_code = await self._run_git_command(
            working_directory, "branch", "--show-current"
        )
        if branch_code == 0 and branch_out:
            status.branch = branch_out

        # Get ahead/behind count
        try:
            ahead_behind, _, _ = await self._run_git_command(
                working_directory, "rev-list", "--left-right", "--count", "HEAD...@{u}"
            )
            if ahead_behind:
                parts = ahead_behind.split()
                if len(parts) == 2:
                    status.ahead = int(parts[0])
                    status.behind = int(parts[1])
        except Exception:
            pass  # No upstream branch or other issue

        # Get file status
        status_out, _, status_code = await self._run_git_command(
            working_directory, "status", "--short"
        )

        if status_code == 0:
            for line in status_out.split("\n"):
                if not line:
                    continue

                file_status = line[:2]
                filename = line[3:].strip()

                # Staged files (index column has status)
                if file_status[0] in ("M", "A", "D", "R", "C"):
                    status.staged.append(filename)
                # Modified files (working tree column has status)
                if file_status[1] in ("M", "D"):
                    status.modified.append(filename)
                # Untracked files
                if file_status == "??":
                    status.untracked.append(filename)

            status.is_clean = not status.has_changes()

        return status

    async def get_diff(
        self, working_directory: str, staged: bool = False, max_size: int = 1_000_000
    ) -> str:
        """Get git diff.

        Args:
            working_directory: Directory to run git diff in
            staged: If True, show staged changes only
            max_size: Maximum diff size in bytes (default 1MB)

        Returns:
            Diff output, truncated if exceeds max_size
        """
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        args = ["diff"]
        if staged:
            args.append("--staged")

        stdout, stderr, returncode = await self._run_git_command(working_directory, *args)

        if returncode != 0:
            raise GitError(f"Git diff failed: {stderr}")

        if not stdout:
            return "(no changes)"

        # Truncate if diff is too large
        if len(stdout) > max_size:
            return stdout[:max_size] + f"\n\n... (diff truncated, {len(stdout)} bytes total)"

        return stdout

    async def create_checkpoint(
        self, working_directory: str, name: str, description: Optional[str] = None
    ) -> Checkpoint:
        """Create checkpoint using git stash."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        # Build stash message
        stash_message = f"checkpoint: {name}"
        if description:
            stash_message += f" - {description}"

        # Create stash
        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "stash", "push", "-m", stash_message
        )

        if returncode != 0:
            raise GitError(f"Failed to create checkpoint: {stderr}")

        # Get stash ref (stash@{0} is the most recent)
        stash_ref = "stash@{0}"

        return Checkpoint(
            name=name, stash_ref=stash_ref, message=stash_message, description=description
        )

    async def restore_checkpoint(self, working_directory: str, stash_ref: str) -> bool:
        """Restore from checkpoint."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "stash", "apply", stash_ref
        )

        if returncode != 0:
            raise GitError(f"Failed to restore checkpoint: {stderr}")

        return True

    async def undo_changes(self, working_directory: str, files: Optional[list[str]] = None) -> bool:
        """Undo uncommitted changes."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        args = ["restore"]
        if files:
            args.extend(files)
        else:
            args.append(".")

        stdout, stderr, returncode = await self._run_git_command(working_directory, *args)

        if returncode != 0:
            raise GitError(f"Failed to undo changes: {stderr}")

        return True

    async def commit_changes(
        self,
        working_directory: str,
        message: str,
        files: Optional[list[str]] = None,
    ) -> str:
        """Commit changes."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        # Validate commit message
        self._validate_commit_message(message)

        # Stage files if specified
        if files:
            add_args = ["add"] + files
            _, stderr, returncode = await self._run_git_command(working_directory, *add_args)
            if returncode != 0:
                raise GitError(f"Failed to stage files: {stderr}")

        # Commit
        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "commit", "-m", message
        )

        if returncode != 0:
            raise GitError(f"Failed to commit: {stderr}")

        # Get commit hash
        hash_out, _, _ = await self._run_git_command(
            working_directory, "rev-parse", "--short", "HEAD"
        )

        return hash_out if hash_out else "unknown"

    async def create_branch(
        self, working_directory: str, branch_name: str, switch: bool = True
    ) -> bool:
        """Create branch."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        # Validate branch name
        await self._validate_branch_name_with_git(working_directory, branch_name)

        if switch:
            args = ["checkout", "-b", branch_name]
        else:
            args = ["branch", branch_name]

        stdout, stderr, returncode = await self._run_git_command(working_directory, *args)

        if returncode != 0:
            raise GitError(f"Failed to create branch: {stderr}")

        return True

    async def switch_branch(self, working_directory: str, branch_name: str) -> bool:
        """Switch branch."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        # Validate branch name
        await self._validate_branch_name_with_git(working_directory, branch_name)

        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "checkout", branch_name
        )

        if returncode != 0:
            raise GitError(f"Failed to switch branch: {stderr}")

        return True

    async def get_branches(self, working_directory: str) -> tuple[list[str], str]:
        """Get list of branches and current branch."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        stdout, stderr, returncode = await self._run_git_command(working_directory, "branch")

        if returncode != 0:
            raise GitError(f"Failed to get branches: {stderr}")

        branches = []
        current_branch = ""

        for line in stdout.split("\n"):
            line = line.strip()
            if not line:
                continue

            if line.startswith("*"):
                current_branch = line[2:].strip()
                branches.append(current_branch)
            else:
                branches.append(line)

        return branches, current_branch

    async def branch_exists(self, working_directory: str, branch_name: str) -> bool:
        """Return True when a local branch exists."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        self._validate_branch_name(branch_name)

        _, _, returncode = await self._run_git_command(
            working_directory, "show-ref", "--verify", f"refs/heads/{branch_name}"
        )
        return returncode == 0

    async def get_current_branch(self, working_directory: str) -> str:
        """Get current branch for a worktree directory."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "branch", "--show-current"
        )
        if returncode != 0:
            raise GitError(f"Failed to get current branch: {stderr}")
        return stdout.strip()

    # -------------------------------------------------------------------------
    # Worktree Operations
    # -------------------------------------------------------------------------

    async def get_main_worktree(self, working_directory: str) -> str:
        """Get the path of the main worktree (original clone).

        Resolves correctly even when called from within a secondary worktree.

        Parameters
        ----------
        working_directory : str
            Any directory within the git repo or a worktree.

        Returns
        -------
        str
            Absolute path to the main worktree root.
        """
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        # git worktree list --porcelain always lists the main worktree first
        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "worktree", "list", "--porcelain"
        )
        if returncode != 0:
            raise GitError(f"Failed to get worktree info: {stderr}")

        for line in stdout.split("\n"):
            if line.startswith("worktree "):
                return line[len("worktree ") :]

        # Fallback
        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "rev-parse", "--show-toplevel"
        )
        if returncode != 0:
            raise GitError(f"Failed to get repo root: {stderr}")
        return stdout

    async def list_worktrees(self, working_directory: str) -> list[Worktree]:
        """List all worktrees for the repository.

        Parameters
        ----------
        working_directory : str
            Any directory within the git repo or a worktree.

        Returns
        -------
        list[Worktree]
            List of Worktree objects.
        """
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "worktree", "list", "--porcelain"
        )
        if returncode != 0:
            raise GitError(f"Failed to list worktrees: {stderr}")

        worktrees: list[Worktree] = []
        current_wt: dict[str, str] = {}
        is_first = True

        def _append_current(wt_data: dict[str, str], is_main: bool) -> None:
            if not wt_data:
                return
            branch = wt_data.get("branch", "").replace("refs/heads/", "")
            is_detached = wt_data.get("detached", "0") == "1"
            if is_detached and not branch:
                branch = "(detached HEAD)"
            worktrees.append(
                Worktree(
                    path=wt_data.get("worktree", ""),
                    branch=branch,
                    commit=wt_data.get("HEAD", ""),
                    is_main=is_main,
                    is_detached=is_detached,
                    is_locked=wt_data.get("locked", "0") == "1",
                    lock_reason=wt_data.get("lock_reason") or None,
                    is_prunable=wt_data.get("prunable", "0") == "1",
                    prunable_reason=wt_data.get("prunable_reason") or None,
                )
            )

        for line in stdout.split("\n"):
            if not line.strip():
                if current_wt:
                    _append_current(current_wt, is_first)
                    is_first = False
                    current_wt = {}
                continue

            if line.startswith("worktree "):
                current_wt["worktree"] = line[len("worktree ") :]
            elif line.startswith("HEAD "):
                current_wt["HEAD"] = line[len("HEAD ") :]
            elif line.startswith("branch "):
                current_wt["branch"] = line[len("branch ") :]
            elif line == "detached":
                current_wt["detached"] = "1"
            elif line == "locked":
                current_wt["locked"] = "1"
            elif line.startswith("locked "):
                current_wt["locked"] = "1"
                current_wt["lock_reason"] = line[len("locked ") :]
            elif line == "prunable":
                current_wt["prunable"] = "1"
            elif line.startswith("prunable "):
                current_wt["prunable"] = "1"
                current_wt["prunable_reason"] = line[len("prunable ") :]

        # Handle last entry (no trailing blank line)
        if current_wt:
            _append_current(current_wt, is_first)

        return worktrees

    async def add_worktree(
        self, working_directory: str, branch_name: str, from_ref: Optional[str] = None
    ) -> str:
        """Create a new worktree with a new branch.

        The worktree is placed in a sibling `-worktrees/` directory. For example,
        if the main repo is at `/home/user/project`, the worktree goes to
        `/home/user/project-worktrees/<branch-name>`.

        Parameters
        ----------
        working_directory : str
            Current working directory (any dir in the repo).
        branch_name : str
            Name for the new branch and worktree directory.

        Returns
        -------
        str
            Absolute path to the new worktree directory.
        """
        await self._validate_branch_name_with_git(working_directory, branch_name)

        main_root = await self.get_main_worktree(working_directory)
        worktree_base = main_root + "-worktrees"
        worktree_path = str(Path(worktree_base) / branch_name)

        if Path(worktree_path).exists():
            raise GitError(f"Worktree directory already exists: {worktree_path}")

        # Create the worktrees base directory if needed
        Path(worktree_base).mkdir(parents=True, exist_ok=True)

        branch_exists = await self.branch_exists(working_directory, branch_name)
        args = ["worktree", "add"]
        if branch_exists:
            if from_ref:
                raise GitError(
                    f"Branch `{branch_name}` already exists; `--from` can only be used for new branches"
                )
            args.extend([worktree_path, branch_name])
        else:
            args.extend(["-b", branch_name, worktree_path])
            if from_ref:
                args.append(from_ref)

        stdout, stderr, returncode = await self._run_git_command(working_directory, *args)
        if returncode != 0:
            raise GitError(f"Failed to create worktree: {stderr}")

        return worktree_path

    async def remove_worktree(
        self, working_directory: str, worktree_path: str, force: bool = False
    ) -> bool:
        """Remove a worktree.

        Parameters
        ----------
        working_directory : str
            Any directory within the repo (NOT the worktree being removed).
        worktree_path : str
            Path to the worktree to remove.
        force : bool
            If True, force removal even with uncommitted changes.

        Returns
        -------
        bool
            True if successfully removed.
        """
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        args = ["worktree", "remove", worktree_path]
        if force:
            args.append("--force")

        stdout, stderr, returncode = await self._run_git_command(working_directory, *args)
        if returncode != 0:
            raise GitError(f"Failed to remove worktree: {stderr}")

        return True

    async def prune_worktrees(self, working_directory: str, dry_run: bool = False) -> str:
        """Prune stale worktree administrative files."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        args = ["worktree", "prune"]
        if dry_run:
            args.append("--dry-run")

        stdout, stderr, returncode = await self._run_git_command(working_directory, *args)
        if returncode != 0:
            raise GitError(f"Failed to prune worktrees: {stderr}")
        return stdout or "No stale worktrees found."

    async def delete_branch(
        self, working_directory: str, branch_name: str, force: bool = False
    ) -> bool:
        """Delete a local branch."""
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        await self._validate_branch_name_with_git(working_directory, branch_name)

        args = ["branch", "-D" if force else "-d", branch_name]
        _, stderr, returncode = await self._run_git_command(working_directory, *args)
        if returncode != 0:
            raise GitError(f"Failed to delete branch: {stderr}")
        return True

    async def merge_branch(self, working_directory: str, branch_name: str) -> tuple[bool, str]:
        """Merge a branch into the current branch.

        Parameters
        ----------
        working_directory : str
            The directory to merge into (checked out to the target branch).
        branch_name : str
            The branch to merge from.

        Returns
        -------
        tuple[bool, str]
            (success, message). success is False if there were conflicts.
        """
        if not await self.validate_git_repo(working_directory):
            raise GitError("Not a git repository")

        await self._validate_branch_name_with_git(working_directory, branch_name)

        stdout, stderr, returncode = await self._run_git_command(
            working_directory, "merge", branch_name
        )

        if returncode == 0:
            return True, stdout or "Merge completed successfully."

        # Check for merge conflicts
        if "CONFLICT" in stdout or "CONFLICT" in stderr:
            conf_out, _, _ = await self._run_git_command(
                working_directory, "diff", "--name-only", "--diff-filter=U"
            )
            conflict_files = [f for f in conf_out.split("\n") if f.strip()]
            conflict_msg = f"Merge conflicts in {len(conflict_files)} file(s):\n"
            conflict_msg += "\n".join(f"  - {f}" for f in conflict_files[:20])
            if len(conflict_files) > 20:
                conflict_msg += f"\n  ... and {len(conflict_files) - 20} more"
            return False, conflict_msg

        raise GitError(f"Merge failed: {stderr or stdout}")
