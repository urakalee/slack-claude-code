import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.config import get_backend_for_model


@dataclass
class Session:
    id: Optional[int] = None
    channel_id: str = ""
    thread_ts: Optional[str] = None  # Thread timestamp for thread-based sessions
    working_directory: str = "~"
    claude_session_id: Optional[str] = None  # For Claude --resume flag
    permission_mode: Optional[str] = (
        None  # Per-session permission mode override (Claude)
    )
    model: Optional[str] = (
        None  # Model to use (e.g., "sonnet", "claude-opus-4-6[1m]", "gpt-5.3-codex")
    )
    added_dirs: list = field(default_factory=list)  # Directories added via /add-dir
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)
    # Codex-specific fields
    codex_session_id: Optional[str] = None  # For Codex resume
    sandbox_mode: str = (
        "workspace-write"  # read-only, workspace-write, danger-full-access
    )
    approval_mode: str = "on-request"  # untrusted, on-request, never

    @classmethod
    def from_row(cls, row: tuple) -> "Session":
        # Handle schema evolution with ALTER TABLE ADD COLUMN
        # Columns are appended at the end in order:
        # - model (pos 8)
        # - added_dirs (pos 9)
        # - codex_session_id (pos 10)
        # - sandbox_mode (pos 11)
        # - approval_mode (pos 12)
        # Original 8 columns: id, channel_id, thread_ts, working_directory,
        #                     claude_session_id, permission_mode, created_at, last_active
        model = None
        added_dirs = []
        codex_session_id = None
        sandbox_mode = "workspace-write"
        approval_mode = "on-request"

        if len(row) > 8:
            model = row[8]
        if len(row) > 9:
            added_dirs = json.loads(row[9]) if row[9] else []
        if len(row) > 10:
            codex_session_id = row[10]
        if len(row) > 11:
            sandbox_mode = row[11] or "workspace-write"
        if len(row) > 12:
            approval_mode = row[12] or "on-request"

        return cls(
            id=row[0],
            channel_id=row[1],
            thread_ts=row[2],
            working_directory=row[3],
            claude_session_id=row[4],
            permission_mode=row[5],
            model=model,
            added_dirs=added_dirs,
            created_at=datetime.fromisoformat(row[6]) if row[6] else datetime.now(),
            last_active=datetime.fromisoformat(row[7]) if row[7] else datetime.now(),
            codex_session_id=codex_session_id,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
        )

    def is_thread_session(self) -> bool:
        """Check if this is a thread-scoped session."""
        return self.thread_ts is not None

    def session_display_name(self) -> str:
        """Get human-readable session identifier."""
        if self.is_thread_session():
            return f"{self.channel_id} (Thread: {self.thread_ts})"
        return f"{self.channel_id} (Channel)"

    def get_backend(self) -> str:
        """Get the backend type based on the current model.

        Returns
        -------
        str
            "claude" or "codex"
        """
        return get_backend_for_model(self.model)


@dataclass
class CommandHistory:
    id: Optional[int] = None
    session_id: int = 0
    command: str = ""
    output: Optional[str] = None
    status: str = "pending"  # pending, running, completed, failed, cancelled
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "CommandHistory":
        return cls(
            id=row[0],
            session_id=row[1],
            command=row[2],
            output=row[3],
            status=row[4],
            error_message=row[5],
            created_at=datetime.fromisoformat(row[6]) if row[6] else datetime.now(),
            completed_at=datetime.fromisoformat(row[7]) if row[7] else None,
        )


@dataclass
class ParallelJob:
    id: Optional[int] = None
    session_id: int = 0
    channel_id: str = ""
    job_type: str = ""  # parallel_analysis, sequential_loop
    status: str = "pending"  # pending, running, completed, failed, cancelled
    config: dict = field(default_factory=dict)  # n_instances, commands, loop_count
    results: list = field(default_factory=list)  # outputs from each terminal
    aggregation_output: Optional[str] = None
    message_ts: Optional[str] = None  # Slack message timestamp for updates
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "ParallelJob":
        return cls(
            id=row[0],
            session_id=row[1],
            channel_id=row[2],
            job_type=row[3],
            status=row[4],
            config=json.loads(row[5]) if row[5] else {},
            results=json.loads(row[6]) if row[6] else [],
            aggregation_output=row[7],
            message_ts=row[8],
            created_at=datetime.fromisoformat(row[9]) if row[9] else datetime.now(),
            completed_at=datetime.fromisoformat(row[10]) if row[10] else None,
        )


@dataclass
class QueueItem:
    """Item in the FIFO command queue."""

    id: Optional[int] = None
    session_id: int = 0
    channel_id: str = ""
    thread_ts: Optional[str] = None
    prompt: str = ""
    status: str = "pending"  # pending, running, completed, failed, cancelled
    output: Optional[str] = None
    error_message: Optional[str] = None
    position: int = 0
    message_ts: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "QueueItem":
        # Handle schema evolution: queue_items.thread_ts was added after initial release.
        if len(row) >= 13:
            return cls(
                id=row[0],
                session_id=row[1],
                channel_id=row[2],
                thread_ts=row[3],
                prompt=row[4],
                status=row[5],
                output=row[6],
                error_message=row[7],
                position=row[8],
                message_ts=row[9],
                created_at=(
                    datetime.fromisoformat(row[10]) if row[10] else datetime.now()
                ),
                started_at=datetime.fromisoformat(row[11]) if row[11] else None,
                completed_at=datetime.fromisoformat(row[12]) if row[12] else None,
            )
        return cls(
            id=row[0],
            session_id=row[1],
            channel_id=row[2],
            thread_ts=None,
            prompt=row[3],
            status=row[4],
            output=row[5],
            error_message=row[6],
            position=row[7],
            message_ts=row[8],
            created_at=datetime.fromisoformat(row[9]) if row[9] else datetime.now(),
            started_at=datetime.fromisoformat(row[10]) if row[10] else None,
            completed_at=datetime.fromisoformat(row[11]) if row[11] else None,
        )


@dataclass
class UploadedFile:
    """File uploaded from Slack and stored locally."""

    id: Optional[int] = None
    session_id: int = 0
    slack_file_id: str = ""
    filename: str = ""
    mimetype: str = ""
    size: int = 0
    local_path: str = ""
    uploaded_at: datetime = field(default_factory=datetime.now)
    last_referenced: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: tuple) -> "UploadedFile":
        return cls(
            id=row[0],
            session_id=row[1],
            slack_file_id=row[2],
            filename=row[3],
            mimetype=row[4],
            size=row[5],
            local_path=row[6],
            uploaded_at=datetime.fromisoformat(row[7]) if row[7] else datetime.now(),
            last_referenced=datetime.fromisoformat(row[8]) if row[8] else None,
        )


@dataclass
class GitCheckpoint:
    """Git checkpoint for version control."""

    id: Optional[int] = None
    session_id: int = 0
    channel_id: str = ""
    name: str = ""
    stash_ref: str = ""
    stash_message: Optional[str] = None
    description: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    is_auto: bool = False

    @classmethod
    def from_row(cls, row: tuple) -> "GitCheckpoint":
        return cls(
            id=row[0],
            session_id=row[1],
            channel_id=row[2],
            name=row[3],
            stash_ref=row[4],
            stash_message=row[5],
            description=row[6],
            created_at=datetime.fromisoformat(row[7]) if row[7] else datetime.now(),
            is_auto=bool(row[8]),
        )


@dataclass
class NotificationSettings:
    """Per-channel notification settings."""

    id: Optional[int] = None
    channel_id: str = ""
    notify_on_completion: bool = True  # Default enabled
    notify_on_permission: bool = True  # Default enabled
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)

    @classmethod
    def from_row(cls, row: tuple) -> "NotificationSettings":
        return cls(
            id=row[0],
            channel_id=row[1],
            notify_on_completion=bool(row[2]),
            notify_on_permission=bool(row[3]),
            created_at=datetime.fromisoformat(row[4]) if row[4] else datetime.now(),
            updated_at=datetime.fromisoformat(row[5]) if row[5] else datetime.now(),
        )

    @classmethod
    def default(cls, channel_id: str) -> "NotificationSettings":
        """Return default settings for a channel (all notifications enabled)."""
        return cls(channel_id=channel_id)
