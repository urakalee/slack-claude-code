import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from ..config import config
from .models import (
    CommandHistory,
    GitCheckpoint,
    NotificationSettings,
    ParallelJob,
    QueueItem,
    Session,
    UploadedFile,
)

# Default timeout for database operations (seconds)
DB_TIMEOUT = 30.0


class DatabaseRepository:
    _SESSION_SCOPE_WHERE = """channel_id = ? AND (
                       (thread_ts = ? AND ? IS NOT NULL) OR
                       (thread_ts IS NULL AND ? IS NULL)
                   )"""
    _QUEUE_SCOPE_WHERE = """channel_id = ? AND (
                       (thread_ts = ? AND ? IS NOT NULL) OR
                       (thread_ts IS NULL AND ? IS NULL)
                   )"""
    _QUEUE_ITEM_SELECT = """id, session_id, channel_id, thread_ts, prompt, status, output,
                       error_message, position, message_ts, created_at, started_at, completed_at"""

    def __init__(self, db_path: str, timeout: float = DB_TIMEOUT):
        self.db_path = db_path
        self.timeout = timeout
        self._initialized = False

    @staticmethod
    def _session_scope_params(
        channel_id: str, thread_ts: Optional[str]
    ) -> tuple[Optional[str], ...]:
        """Return standard SQL parameters for channel/thread scoped session queries."""
        return (channel_id, thread_ts, thread_ts, thread_ts)

    @staticmethod
    def _queue_scope_params(
        channel_id: str, thread_ts: Optional[str]
    ) -> tuple[Optional[str], ...]:
        """Return standard SQL parameters for channel/thread scoped queue queries."""
        return (channel_id, thread_ts, thread_ts, thread_ts)

    def _get_connection(self) -> aiosqlite.Connection:
        return aiosqlite.connect(self.db_path, timeout=self.timeout)

    async def _ensure_wal_mode(self, db: aiosqlite.Connection) -> None:
        """Enable WAL mode for better concurrent access."""
        if not self._initialized:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=30000")  # 30 second timeout for busy
            self._initialized = True

    @asynccontextmanager
    async def _transact(self):
        """Provide a connection with automatic commit on success.

        Usage:
            async with self._transact() as db:
                await db.execute(...)
                # commit happens automatically on exit
        """
        async with self._get_connection() as db:
            await self._ensure_wal_mode(db)
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    # Session operations
    async def get_or_create_session(
        self, channel_id: str, thread_ts: Optional[str] = None, default_cwd: str = "~"
    ) -> Session:
        """Get existing session for channel/thread or create a new one.

        Important: SQLite UNIQUE constraints allow multiple NULL values, so
        channel-level sessions (`thread_ts IS NULL`) cannot rely on UPSERT
        conflict behavior. We therefore select first, then insert only when no
        matching row exists.

        Args:
            channel_id: Slack channel ID
            thread_ts: Slack thread timestamp (None for channel-level session)
            default_cwd: Default working directory for new sessions
        """
        async with self._transact() as db:
            now_iso = datetime.now(timezone.utc).isoformat()

            # Find best existing session for this channel/thread pair.
            # If duplicate NULL-thread rows exist, prefer the most populated one.
            cursor = await db.execute(
                f"""SELECT id, channel_id, thread_ts, working_directory,
                          claude_session_id, permission_mode, created_at, last_active,
                          model, added_dirs, codex_session_id, sandbox_mode, approval_mode
                   FROM sessions
                   WHERE {self._SESSION_SCOPE_WHERE}
                   ORDER BY
                       ((CASE WHEN model IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN codex_session_id IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN claude_session_id IS NOT NULL THEN 1 ELSE 0 END) +
                        (CASE WHEN permission_mode IS NOT NULL THEN 1 ELSE 0 END)) DESC,
                       last_active DESC,
                       id DESC
                   LIMIT 1""",
                self._session_scope_params(channel_id, thread_ts),
            )
            row = await cursor.fetchone()

            # Update existing session activity and return it.
            if row is not None:
                await db.execute(
                    "UPDATE sessions SET last_active = ? WHERE id = ?",
                    (now_iso, row[0]),
                )
                # Refresh to return DB-normalized values.
                cursor = await db.execute(
                    """SELECT id, channel_id, thread_ts, working_directory,
                              claude_session_id, permission_mode, created_at, last_active,
                              model, added_dirs, codex_session_id, sandbox_mode, approval_mode
                       FROM sessions
                       WHERE id = ?""",
                    (row[0],),
                )
                updated_row = await cursor.fetchone()
                if updated_row is None:
                    raise RuntimeError(
                        f"Failed to load updated session {row[0]} for channel {channel_id}"
                    )
                return Session.from_row(updated_row)

            # Create new session when none exists.
            cursor = await db.execute(
                """INSERT INTO sessions (channel_id, thread_ts, working_directory, model, last_active)
                   VALUES (?, ?, ?, ?, ?)""",
                (channel_id, thread_ts, default_cwd, config.DEFAULT_MODEL, now_iso),
            )
            session_id = cursor.lastrowid
            if session_id is None:
                raise RuntimeError(f"Failed to create session for channel {channel_id}")

            cursor = await db.execute(
                """SELECT id, channel_id, thread_ts, working_directory,
                          claude_session_id, permission_mode, created_at, last_active,
                          model, added_dirs, codex_session_id, sandbox_mode, approval_mode
                   FROM sessions
                   WHERE id = ?""",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise RuntimeError(
                    f"Failed to load created session {session_id} for channel {channel_id}"
                )
            return Session.from_row(row)

    async def update_session_cwd(
        self, channel_id: str, thread_ts: Optional[str], cwd: str
    ) -> None:
        """Update the working directory for a session."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET working_directory = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    cwd,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def update_session_claude_id(
        self, channel_id: str, thread_ts: Optional[str], claude_session_id: str
    ) -> None:
        """Update the Claude session ID for resume functionality."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET claude_session_id = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    claude_session_id,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def clear_session_claude_id(
        self, channel_id: str, thread_ts: Optional[str] = None
    ) -> None:
        """Clear the Claude session ID to start fresh (used by /clear command)."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET claude_session_id = NULL, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def update_session_mode(
        self, channel_id: str, thread_ts: Optional[str], permission_mode: str
    ) -> None:
        """Update the permission mode for a session."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET permission_mode = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    permission_mode,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def update_session_model(
        self, channel_id: str, thread_ts: Optional[str], model: Optional[str]
    ) -> None:
        """Update the model for a session."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET model = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    model,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def add_session_dir(
        self, channel_id: str, thread_ts: Optional[str], directory: str
    ) -> list:
        """Add a directory to the session's added_dirs list.

        Returns the updated list of directories.
        """
        async with self._transact() as db:
            # Get current directories
            cursor = await db.execute(
                f"""SELECT added_dirs FROM sessions
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                self._session_scope_params(channel_id, thread_ts),
            )
            row = await cursor.fetchone()
            current_dirs = json.loads(row[0]) if row and row[0] else []

            # Add directory if not already present
            if directory not in current_dirs:
                current_dirs.append(directory)

            # Update database
            cursor = await db.execute(
                f"""UPDATE sessions SET added_dirs = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    json.dumps(current_dirs),
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )
            if cursor.rowcount == 0:
                raise RuntimeError(
                    f"Session not found for channel {channel_id} thread {thread_ts}"
                )
            return current_dirs

    async def remove_session_dir(
        self, channel_id: str, thread_ts: Optional[str], directory: str
    ) -> list:
        """Remove a directory from the session's added_dirs list.

        Returns the updated list of directories.
        """
        async with self._transact() as db:
            # Get current directories
            cursor = await db.execute(
                f"""SELECT added_dirs FROM sessions
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                self._session_scope_params(channel_id, thread_ts),
            )
            row = await cursor.fetchone()
            current_dirs = json.loads(row[0]) if row and row[0] else []

            # Remove directory if present
            if directory in current_dirs:
                current_dirs.remove(directory)

            # Update database
            cursor = await db.execute(
                f"""UPDATE sessions SET added_dirs = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    json.dumps(current_dirs) if current_dirs else None,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )
            if cursor.rowcount == 0:
                raise RuntimeError(
                    f"Session not found for channel {channel_id} thread {thread_ts}"
                )
            return current_dirs

    async def clear_session_dirs(
        self, channel_id: str, thread_ts: Optional[str]
    ) -> None:
        """Clear all added directories from a session."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET added_dirs = NULL, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def get_session_dirs(self, channel_id: str, thread_ts: Optional[str]) -> list:
        """Get the list of added directories for a session."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""SELECT added_dirs FROM sessions
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                self._session_scope_params(channel_id, thread_ts),
            )
            row = await cursor.fetchone()
            return json.loads(row[0]) if row and row[0] else []

    async def get_session_by_id(self, session_id: int) -> Optional[Session]:
        """Get a session by its database ID."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """SELECT id, channel_id, thread_ts, working_directory,
                          claude_session_id, permission_mode, created_at, last_active,
                          model, added_dirs, codex_session_id, sandbox_mode, approval_mode
                   FROM sessions WHERE id = ?""",
                (session_id,),
            )
            row = await cursor.fetchone()
            return Session.from_row(row) if row else None

    async def delete_session(
        self, channel_id: str, thread_ts: Optional[str] = None
    ) -> bool:
        """Delete a specific session."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""DELETE FROM sessions
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                self._session_scope_params(channel_id, thread_ts),
            )
            await db.commit()
            return cursor.rowcount > 0

    # Command history operations
    async def add_command(self, session_id: int, command: str) -> CommandHistory:
        """Add a new command to history."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "INSERT INTO command_history (session_id, command, status) VALUES (?, ?, 'pending')",
                (session_id, command),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM command_history WHERE id = ?", (cursor.lastrowid,)
            )
            row = await cursor.fetchone()
            return CommandHistory.from_row(row)

    async def update_command_status(
        self,
        command_id: int,
        status: str,
        output: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update command status and optionally output."""
        async with self._get_connection() as db:
            if status in ("completed", "failed", "cancelled"):
                await db.execute(
                    """UPDATE command_history
                       SET status = ?, output = ?, error_message = ?, completed_at = ?
                       WHERE id = ?""",
                    (
                        status,
                        output,
                        error_message,
                        datetime.now(timezone.utc).isoformat(),
                        command_id,
                    ),
                )
            else:
                await db.execute(
                    "UPDATE command_history SET status = ? WHERE id = ?",
                    (status, command_id),
                )
            await db.commit()

    async def append_command_output(self, command_id: int, output_chunk: str) -> None:
        """Append output chunk to command (for streaming)."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT output FROM command_history WHERE id = ?", (command_id,)
            )
            row = await cursor.fetchone()
            current_output = row[0] or "" if row else ""

            await db.execute(
                "UPDATE command_history SET output = ? WHERE id = ?",
                (current_output + output_chunk, command_id),
            )
            await db.commit()

    async def get_command_history(
        self, session_id: int, limit: int = 10, offset: int = 0
    ) -> tuple[list[CommandHistory], int]:
        """Get paginated command history for a session."""
        async with self._get_connection() as db:
            # Get total count
            cursor = await db.execute(
                "SELECT COUNT(*) FROM command_history WHERE session_id = ?",
                (session_id,),
            )
            total = (await cursor.fetchone())[0]

            # Get paginated results
            cursor = await db.execute(
                """SELECT * FROM command_history
                   WHERE session_id = ?
                   ORDER BY created_at DESC
                   LIMIT ? OFFSET ?""",
                (session_id, limit, offset),
            )
            rows = await cursor.fetchall()
            return [CommandHistory.from_row(row) for row in rows], total

    async def get_command_by_id(self, command_id: int) -> Optional[CommandHistory]:
        """Get a specific command by ID."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM command_history WHERE id = ?", (command_id,)
            )
            row = await cursor.fetchone()
            return CommandHistory.from_row(row) if row else None

    # Parallel job operations
    async def create_parallel_job(
        self,
        session_id: int,
        channel_id: str,
        job_type: str,
        config: dict,
        message_ts: Optional[str] = None,
    ) -> ParallelJob:
        """Create a new parallel job."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO parallel_jobs
                   (session_id, channel_id, job_type, config, results, message_ts, status)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                (
                    session_id,
                    channel_id,
                    job_type,
                    json.dumps(config),
                    "[]",
                    message_ts,
                ),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM parallel_jobs WHERE id = ?", (cursor.lastrowid,)
            )
            row = await cursor.fetchone()
            return ParallelJob.from_row(row)

    async def update_parallel_job(
        self,
        job_id: int,
        status: Optional[str] = None,
        results: Optional[list] = None,
        aggregation_output: Optional[str] = None,
        message_ts: Optional[str] = None,
    ) -> None:
        """Update parallel job fields."""
        async with self._get_connection() as db:
            updates = []
            params = []

            if status:
                updates.append("status = ?")
                params.append(status)
                if status in ("completed", "failed", "cancelled"):
                    updates.append("completed_at = ?")
                    params.append(datetime.now(timezone.utc).isoformat())

            if results is not None:
                updates.append("results = ?")
                params.append(json.dumps(results))

            if aggregation_output is not None:
                updates.append("aggregation_output = ?")
                params.append(aggregation_output)

            if message_ts is not None:
                updates.append("message_ts = ?")
                params.append(message_ts)

            if updates:
                # Build SQL safely with placeholders
                sql = "UPDATE parallel_jobs SET " + ", ".join(updates) + " WHERE id = ?"
                params.append(job_id)
                await db.execute(sql, tuple(params))
                await db.commit()

    async def get_parallel_job(self, job_id: int) -> Optional[ParallelJob]:
        """Get a parallel job by ID."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM parallel_jobs WHERE id = ?", (job_id,)
            )
            row = await cursor.fetchone()
            return ParallelJob.from_row(row) if row else None

    async def get_active_jobs(
        self, channel_id: Optional[str] = None
    ) -> list[ParallelJob]:
        """Get all active (pending/running) jobs, optionally filtered by channel."""
        async with self._get_connection() as db:
            if channel_id:
                cursor = await db.execute(
                    """SELECT * FROM parallel_jobs
                       WHERE status IN ('pending', 'running') AND channel_id = ?
                       ORDER BY created_at DESC""",
                    (channel_id,),
                )
            else:
                cursor = await db.execute(
                    """SELECT * FROM parallel_jobs
                       WHERE status IN ('pending', 'running')
                       ORDER BY created_at DESC"""
                )
            rows = await cursor.fetchall()
            return [ParallelJob.from_row(row) for row in rows]

    async def cancel_job(self, job_id: int) -> bool:
        """Cancel a job if it's still active."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """UPDATE parallel_jobs
                   SET status = 'cancelled', completed_at = ?
                   WHERE id = ? AND status IN ('pending', 'running')""",
                (datetime.now(timezone.utc).isoformat(), job_id),
            )
            await db.commit()
            return cursor.rowcount > 0

    # Queue operations
    async def add_to_queue(
        self,
        session_id: int,
        channel_id: str,
        thread_ts: Optional[str],
        prompt: str,
    ) -> QueueItem:
        """Add a command to the FIFO queue."""
        async with self._get_connection() as db:
            # Get next position for this channel/thread scope
            cursor = await db.execute(
                """SELECT COALESCE(MAX(position), 0) + 1
                   FROM queue_items WHERE """
                + self._QUEUE_SCOPE_WHERE,
                self._queue_scope_params(channel_id, thread_ts),
            )
            position = (await cursor.fetchone())[0]

            cursor = await db.execute(
                """INSERT INTO queue_items
                   (session_id, channel_id, thread_ts, prompt, position, status)
                   VALUES (?, ?, ?, ?, ?, 'pending')""",
                (session_id, channel_id, thread_ts, prompt, position),
            )
            await db.commit()

            cursor = await db.execute(
                f"SELECT {self._QUEUE_ITEM_SELECT} FROM queue_items WHERE id = ?",
                (cursor.lastrowid,),
            )
            row = await cursor.fetchone()
            return QueueItem.from_row(row)

    async def get_pending_queue_items(
        self, channel_id: str, thread_ts: Optional[str]
    ) -> list[QueueItem]:
        """Get all pending queue items for a session scope, ordered by position."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"""SELECT {self._QUEUE_ITEM_SELECT} FROM queue_items WHERE """
                + self._QUEUE_SCOPE_WHERE
                + """ AND status = 'pending'
                   ORDER BY position ASC""",
                self._queue_scope_params(channel_id, thread_ts),
            )
            rows = await cursor.fetchall()
            return [QueueItem.from_row(row) for row in rows]

    async def get_queue_item(self, item_id: int) -> Optional[QueueItem]:
        """Get a queue item by ID."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"SELECT {self._QUEUE_ITEM_SELECT} FROM queue_items WHERE id = ?",
                (item_id,),
            )
            row = await cursor.fetchone()
            return QueueItem.from_row(row) if row else None

    async def update_queue_item_status(
        self,
        item_id: int,
        status: str,
        output: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Update queue item status."""
        async with self._get_connection() as db:
            if status == "running":
                await db.execute(
                    "UPDATE queue_items SET status = ?, started_at = ? WHERE id = ?",
                    (status, datetime.now(timezone.utc).isoformat(), item_id),
                )
            elif status in ("completed", "failed", "cancelled"):
                await db.execute(
                    """UPDATE queue_items
                       SET status = ?, output = ?, error_message = ?, completed_at = ?
                       WHERE id = ?""",
                    (
                        status,
                        output,
                        error_message,
                        datetime.now(timezone.utc).isoformat(),
                        item_id,
                    ),
                )
            else:
                await db.execute(
                    "UPDATE queue_items SET status = ? WHERE id = ?",
                    (status, item_id),
                )
            await db.commit()

    async def remove_queue_item(
        self,
        item_id: int,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
    ) -> bool:
        """Remove a queue item (only if pending), optionally constrained to scope."""
        async with self._get_connection() as db:
            if channel_id is None:
                cursor = await db.execute(
                    "DELETE FROM queue_items WHERE id = ? AND status = 'pending'",
                    (item_id,),
                )
            else:
                cursor = await db.execute(
                    "DELETE FROM queue_items WHERE id = ? AND status = 'pending' AND "
                    + self._QUEUE_SCOPE_WHERE,
                    (item_id, *self._queue_scope_params(channel_id, thread_ts)),
                )
            await db.commit()
            return cursor.rowcount > 0

    async def clear_queue(self, channel_id: str, thread_ts: Optional[str]) -> int:
        """Clear all pending queue items for a session scope."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM queue_items WHERE "
                + self._QUEUE_SCOPE_WHERE
                + " AND status = 'pending'",
                self._queue_scope_params(channel_id, thread_ts),
            )
            await db.commit()
            return cursor.rowcount

    async def get_running_queue_item(
        self, channel_id: str, thread_ts: Optional[str]
    ) -> Optional[QueueItem]:
        """Get the currently running queue item for a session scope."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                f"SELECT {self._QUEUE_ITEM_SELECT} FROM queue_items WHERE "
                + self._QUEUE_SCOPE_WHERE
                + " AND status = 'running'",
                self._queue_scope_params(channel_id, thread_ts),
            )
            row = await cursor.fetchone()
            return QueueItem.from_row(row) if row else None

    # Uploaded file operations
    async def add_uploaded_file(
        self,
        session_id: int,
        slack_file_id: str,
        filename: str,
        local_path: str,
        mimetype: str = "",
        size: int = 0,
    ) -> UploadedFile:
        """Track an uploaded file."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """INSERT OR REPLACE INTO uploaded_files
                   (session_id, slack_file_id, filename, local_path, mimetype, size)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, slack_file_id, filename, local_path, mimetype, size),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM uploaded_files WHERE id = ?", (cursor.lastrowid,)
            )
            row = await cursor.fetchone()
            return UploadedFile.from_row(row)

    async def get_session_uploaded_files(self, session_id: int) -> list[UploadedFile]:
        """Get all uploaded files for a session."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """SELECT * FROM uploaded_files
                   WHERE session_id = ?
                   ORDER BY uploaded_at DESC""",
                (session_id,),
            )
            rows = await cursor.fetchall()
            return [UploadedFile.from_row(row) for row in rows]

    # Git checkpoint operations
    async def create_checkpoint(
        self,
        session_id: int,
        channel_id: str,
        name: str,
        stash_ref: str,
        stash_message: Optional[str] = None,
        description: Optional[str] = None,
        is_auto: bool = False,
    ) -> GitCheckpoint:
        """Create a git checkpoint record."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """INSERT INTO git_checkpoints
                   (session_id, channel_id, name, stash_ref, stash_message, description, is_auto)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    channel_id,
                    name,
                    stash_ref,
                    stash_message,
                    description,
                    1 if is_auto else 0,
                ),
            )
            await db.commit()

            cursor = await db.execute(
                "SELECT * FROM git_checkpoints WHERE id = ?", (cursor.lastrowid,)
            )
            row = await cursor.fetchone()
            return GitCheckpoint.from_row(row)

    async def get_checkpoints(
        self, channel_id: str, include_auto: bool = False
    ) -> list[GitCheckpoint]:
        """Get checkpoints for a channel."""
        async with self._get_connection() as db:
            if include_auto:
                cursor = await db.execute(
                    """SELECT * FROM git_checkpoints
                       WHERE channel_id = ?
                       ORDER BY created_at DESC""",
                    (channel_id,),
                )
            else:
                cursor = await db.execute(
                    """SELECT * FROM git_checkpoints
                       WHERE channel_id = ? AND is_auto = 0
                       ORDER BY created_at DESC""",
                    (channel_id,),
                )
            rows = await cursor.fetchall()
            return [GitCheckpoint.from_row(row) for row in rows]

    async def get_checkpoint_by_name(
        self, channel_id: str, name: str
    ) -> Optional[GitCheckpoint]:
        """Get a specific checkpoint by name."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                """SELECT * FROM git_checkpoints
                   WHERE channel_id = ? AND name = ?
                   ORDER BY created_at DESC
                   LIMIT 1""",
                (channel_id, name),
            )
            row = await cursor.fetchone()
            return GitCheckpoint.from_row(row) if row else None

    async def delete_checkpoint(self, checkpoint_id: int) -> bool:
        """Delete a checkpoint."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM git_checkpoints WHERE id = ?", (checkpoint_id,)
            )
            await db.commit()
            return cursor.rowcount > 0

    async def delete_auto_checkpoints(self, channel_id: str) -> int:
        """Delete all auto checkpoints for a channel."""
        async with self._get_connection() as db:
            cursor = await db.execute(
                "DELETE FROM git_checkpoints WHERE channel_id = ? AND is_auto = 1",
                (channel_id,),
            )
            await db.commit()
            return cursor.rowcount

    # -------------------------------------------------------------------------
    # Notification Settings
    # -------------------------------------------------------------------------

    async def get_notification_settings(
        self, channel_id: str
    ) -> "NotificationSettings":
        """
        Get notification settings for a channel.

        Returns default settings (all enabled) if no record exists.
        """
        async with self._get_connection() as db:
            cursor = await db.execute(
                "SELECT * FROM notification_settings WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cursor.fetchone()
            if row:
                return NotificationSettings.from_row(row)
            # Return defaults (all notifications enabled)
            return NotificationSettings.default(channel_id)

    async def update_notification_settings(
        self,
        channel_id: str,
        notify_on_completion: bool,
        notify_on_permission: bool,
    ) -> "NotificationSettings":
        """
        Update notification settings for a channel (upsert).

        Creates the record if it doesn't exist.
        """
        async with self._transact() as db:
            # Try to update first
            cursor = await db.execute(
                """UPDATE notification_settings
                   SET notify_on_completion = ?,
                       notify_on_permission = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE channel_id = ?""",
                (notify_on_completion, notify_on_permission, channel_id),
            )

            if cursor.rowcount == 0:
                # Insert new record
                await db.execute(
                    """INSERT INTO notification_settings
                       (channel_id, notify_on_completion, notify_on_permission)
                       VALUES (?, ?, ?)""",
                    (channel_id, notify_on_completion, notify_on_permission),
                )

        # Return the updated settings
        return await self.get_notification_settings(channel_id)

    # -------------------------------------------------------------------------
    # Codex-specific Session Operations
    # -------------------------------------------------------------------------

    async def update_session_codex_id(
        self, channel_id: str, thread_ts: Optional[str], codex_session_id: str
    ) -> None:
        """Update the Codex session ID for resume functionality."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET codex_session_id = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    codex_session_id,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def clear_session_codex_id(
        self, channel_id: str, thread_ts: Optional[str] = None
    ) -> None:
        """Clear the Codex session ID to start fresh."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET codex_session_id = NULL, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def update_session_sandbox_mode(
        self, channel_id: str, thread_ts: Optional[str], sandbox_mode: str
    ) -> None:
        """Update the sandbox mode for a session (Codex)."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET sandbox_mode = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    sandbox_mode,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )

    async def update_session_approval_mode(
        self, channel_id: str, thread_ts: Optional[str], approval_mode: str
    ) -> None:
        """Update the approval mode for a session (Codex)."""
        async with self._transact() as db:
            await db.execute(
                f"""UPDATE sessions SET approval_mode = ?, last_active = ?
                   WHERE {self._SESSION_SCOPE_WHERE}""",
                (
                    approval_mode,
                    datetime.now(timezone.utc).isoformat(),
                    *self._session_scope_params(channel_id, thread_ts),
                ),
            )
