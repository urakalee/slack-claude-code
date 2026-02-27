"""Unit tests for database models."""

import json
from datetime import datetime

import pytest

from src.config import is_supported_codex_model, parse_model_effort
from src.database.models import (
    CommandHistory,
    GitCheckpoint,
    NotificationSettings,
    ParallelJob,
    QueueItem,
    Session,
    UploadedFile,
)


class TestSession:
    """Tests for Session model."""

    def test_from_row_new_schema(self):
        """from_row handles new schema with model column."""
        row = (
            1,  # id
            "C123ABC",  # channel_id
            "1234567890.123456",  # thread_ts
            "/home/user",  # working_directory
            "session-abc123",  # claude_session_id
            "plan",  # permission_mode
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T11:00:00",  # last_active
            "opus",  # model (at position 8)
        )

        session = Session.from_row(row)

        assert session.id == 1
        assert session.channel_id == "C123ABC"
        assert session.thread_ts == "1234567890.123456"
        assert session.working_directory == "/home/user"
        assert session.claude_session_id == "session-abc123"
        assert session.permission_mode == "plan"
        assert session.model == "opus"
        assert session.created_at == datetime.fromisoformat("2024-01-15T10:30:00")
        assert session.last_active == datetime.fromisoformat("2024-01-15T11:00:00")

    def test_from_row_full_schema_with_codex(self):
        """from_row handles full schema with Codex fields."""
        row = (
            1,  # id
            "C123ABC",  # channel_id
            "1234567890.123456",  # thread_ts
            "/home/user",  # working_directory
            "session-abc123",  # claude_session_id
            "plan",  # permission_mode
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T11:00:00",  # last_active
            "gpt-5.3-codex",  # model
            "[]",  # added_dirs (JSON)
            "codex-session-456",  # codex_session_id
            "danger-full-access",  # sandbox_mode
            "never",  # approval_mode
        )

        session = Session.from_row(row)

        assert session.id == 1
        assert session.model == "gpt-5.3-codex"
        assert session.codex_session_id == "codex-session-456"
        assert session.sandbox_mode == "danger-full-access"
        assert session.approval_mode == "never"

    def test_get_backend_claude(self):
        """get_backend returns 'claude' for Claude models."""
        session = Session(channel_id="C123", model="opus")
        assert session.get_backend() == "claude"

        session = Session(channel_id="C123", model="default")
        assert session.get_backend() == "claude"

        session = Session(channel_id="C123", model="claude-opus-4-6[1m]")
        assert session.get_backend() == "claude"

        session = Session(channel_id="C123", model="claude-sonnet-4-6[1m]")
        assert session.get_backend() == "claude"

        session = Session(channel_id="C123", model="claude-haiku-4-5")
        assert session.get_backend() == "claude"

    def test_get_backend_codex(self):
        """get_backend returns 'codex' for Codex models."""
        session = Session(channel_id="C123", model="gpt-5.3-codex")
        assert session.get_backend() == "codex"

        session = Session(channel_id="C123", model="gpt-5.3-codex-spark")
        assert session.get_backend() == "codex"

        session = Session(channel_id="C123", model="gpt-5.3-codex")
        assert session.get_backend() == "codex"

        session = Session(channel_id="C123", model="gpt-5.1-codex-max")
        assert session.get_backend() == "codex"

        session = Session(channel_id="C123", model="gpt-5.2")
        assert session.get_backend() == "codex"

        session = Session(channel_id="C123", model="gpt-5.3-codex-high")
        assert session.get_backend() == "codex"

        session = Session(channel_id="C123", model="gpt-5.3-codex-extra-high")
        assert session.get_backend() == "codex"

    def test_get_backend_unknown_gpt_defaults_to_claude(self):
        """Unsupported gpt-* model IDs should not route to codex."""
        session = Session(channel_id="C123", model="gpt-5")
        assert session.get_backend() == "claude"

    def test_get_backend_default(self):
        """get_backend returns 'claude' for None model."""
        session = Session(channel_id="C123", model=None)
        assert session.get_backend() == "claude"

    def test_from_row_old_schema(self):
        """from_row handles old schema without model column."""
        row = (
            1,  # id
            "C123ABC",  # channel_id
            None,  # thread_ts
            "~",  # working_directory
            None,  # claude_session_id
            None,  # permission_mode
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T11:00:00",  # last_active
        )

        session = Session.from_row(row)

        assert session.id == 1
        assert session.channel_id == "C123ABC"
        assert session.thread_ts is None
        assert session.model is None

    def test_from_row_handles_null_dates(self):
        """from_row handles null date values."""
        row = (1, "C123", None, "~", None, None, None, None, None)

        session = Session.from_row(row)

        assert session.id == 1
        assert isinstance(session.created_at, datetime)
        assert isinstance(session.last_active, datetime)

    def test_is_thread_session_true(self):
        """is_thread_session returns True for thread sessions."""
        session = Session(channel_id="C123", thread_ts="1234567890.123456")
        assert session.is_thread_session() is True

    def test_is_thread_session_false(self):
        """is_thread_session returns False for channel sessions."""
        session = Session(channel_id="C123", thread_ts=None)
        assert session.is_thread_session() is False

    def test_session_display_name_thread(self):
        """session_display_name formats thread sessions correctly."""
        session = Session(channel_id="C123ABC", thread_ts="1234567890.123456")
        assert session.session_display_name() == "C123ABC (Thread: 1234567890.123456)"

    def test_session_display_name_channel(self):
        """session_display_name formats channel sessions correctly."""
        session = Session(channel_id="C123ABC", thread_ts=None)
        assert session.session_display_name() == "C123ABC (Channel)"


class TestParseModelEffort:
    """Tests for parse_model_effort utility."""

    def test_no_effort_suffix(self):
        """Returns None effort when no suffix present."""
        assert parse_model_effort("gpt-5.3-codex") == ("gpt-5.3-codex", None)
        assert parse_model_effort("opus") == ("opus", None)

    def test_effort_suffixes(self):
        """Parses all effort level suffixes correctly."""
        assert parse_model_effort("gpt-5.3-codex-low") == ("gpt-5.3-codex", "low")
        assert parse_model_effort("gpt-5.3-codex-medium") == ("gpt-5.3-codex", "medium")
        assert parse_model_effort("gpt-5.3-codex-high") == ("gpt-5.3-codex", "high")
        assert parse_model_effort("gpt-5.3-codex-xhigh") == ("gpt-5.3-codex", "xhigh")

    def test_max_model_with_effort(self):
        """Handles models ending in -max correctly (not confused with effort)."""
        assert parse_model_effort("gpt-5.1-codex-max") == ("gpt-5.1-codex-max", None)
        assert parse_model_effort("gpt-5.1-codex-max-high") == (
            "gpt-5.1-codex-max",
            "high",
        )
        assert parse_model_effort("gpt-5.1-codex-max-xhigh") == (
            "gpt-5.1-codex-max",
            "xhigh",
        )

    def test_mini_model_with_effort(self):
        """Handles models ending in -mini correctly."""
        assert parse_model_effort("gpt-5.1-codex-mini") == ("gpt-5.1-codex-mini", None)
        assert parse_model_effort("gpt-5.1-codex-mini-low") == (
            "gpt-5.1-codex-mini",
            "low",
        )

    def test_case_insensitive(self):
        """Parsing is case-insensitive."""
        assert parse_model_effort("GPT-5.3-CODEX-HIGH") == ("GPT-5.3-CODEX", "high")
        assert parse_model_effort("GPT-5.3-CODEX-EXTRA-HIGH") == (
            "GPT-5.3-CODEX",
            "xhigh",
        )


class TestSupportedCodexModels:
    """Tests for supported codex model validation."""

    def test_supported_models_without_effort(self):
        """Base codex models are accepted."""
        assert is_supported_codex_model("gpt-5.3-codex")
        assert is_supported_codex_model("gpt-5.3-codex-spark")

    def test_supported_models_with_effort(self):
        """Supported codex models accept effort suffixes."""
        assert is_supported_codex_model("gpt-5.3-codex-high")
        assert is_supported_codex_model("gpt-5.3-codex-extra-high")
        assert is_supported_codex_model("gpt-5.2-codex-xhigh")

    def test_unsupported_models_are_rejected(self):
        """Unsupported codex-like model IDs are rejected."""
        assert not is_supported_codex_model("gpt-5")
        assert not is_supported_codex_model("gpt-5.3-codex-unknown")


class TestCommandHistory:
    """Tests for CommandHistory model."""

    def test_from_row_complete(self):
        """from_row parses complete row correctly."""
        row = (
            42,  # id
            1,  # session_id
            "analyze code",  # command
            "Analysis complete",  # output
            "completed",  # status
            None,  # error_message
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T10:35:00",  # completed_at
        )

        cmd = CommandHistory.from_row(row)

        assert cmd.id == 42
        assert cmd.session_id == 1
        assert cmd.command == "analyze code"
        assert cmd.output == "Analysis complete"
        assert cmd.status == "completed"
        assert cmd.error_message is None
        assert cmd.completed_at == datetime.fromisoformat("2024-01-15T10:35:00")

    def test_from_row_failed(self):
        """from_row parses failed command with error."""
        row = (
            1,
            1,
            "bad command",
            None,
            "failed",
            "Something went wrong",
            "2024-01-15T10:30:00",
            "2024-01-15T10:31:00",
        )

        cmd = CommandHistory.from_row(row)

        assert cmd.status == "failed"
        assert cmd.error_message == "Something went wrong"

    def test_default_values(self):
        """CommandHistory has correct defaults."""
        cmd = CommandHistory()

        assert cmd.id is None
        assert cmd.session_id == 0
        assert cmd.command == ""
        assert cmd.output is None
        assert cmd.status == "pending"
        assert cmd.error_message is None
        assert cmd.completed_at is None


class TestParallelJob:
    """Tests for ParallelJob model."""

    def test_from_row_complete(self):
        """from_row parses complete job correctly."""
        config_json = json.dumps({"n_instances": 3, "commands": ["cmd1", "cmd2"]})
        results_json = json.dumps([{"output": "result1"}, {"output": "result2"}])

        row = (
            1,  # id
            5,  # session_id
            "C123",  # channel_id
            "parallel_analysis",  # job_type
            "completed",  # status
            config_json,  # config
            results_json,  # results
            "aggregated output",  # aggregation_output
            "1234567890.123456",  # message_ts
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T10:35:00",  # completed_at
        )

        job = ParallelJob.from_row(row)

        assert job.id == 1
        assert job.session_id == 5
        assert job.channel_id == "C123"
        assert job.job_type == "parallel_analysis"
        assert job.status == "completed"
        assert job.config == {"n_instances": 3, "commands": ["cmd1", "cmd2"]}
        assert job.results == [{"output": "result1"}, {"output": "result2"}]
        assert job.aggregation_output == "aggregated output"
        assert job.message_ts == "1234567890.123456"

    def test_from_row_handles_null_json(self):
        """from_row handles null JSON fields."""
        row = (1, 1, "C123", "test", "pending", None, None, None, None, None, None)

        job = ParallelJob.from_row(row)

        assert job.config == {}
        assert job.results == []


class TestQueueItem:
    """Tests for QueueItem model."""

    def test_from_row_complete(self):
        """from_row parses complete queue item correctly."""
        row = (
            10,  # id
            1,  # session_id
            "C123",  # channel_id
            "123.456",  # thread_ts
            "analyze this code",  # prompt
            "running",  # status
            "partial output",  # output
            None,  # error_message
            5,  # position
            "1234567890.123456",  # message_ts
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T10:31:00",  # started_at
            None,  # completed_at
        )

        item = QueueItem.from_row(row)

        assert item.id == 10
        assert item.thread_ts == "123.456"
        assert item.prompt == "analyze this code"
        assert item.status == "running"
        assert item.position == 5
        assert item.started_at == datetime.fromisoformat("2024-01-15T10:31:00")
        assert item.completed_at is None

    def test_default_values(self):
        """QueueItem has correct defaults."""
        item = QueueItem()

        assert item.id is None
        assert item.status == "pending"
        assert item.position == 0
        assert item.output is None


class TestUploadedFile:
    """Tests for UploadedFile model."""

    def test_from_row_complete(self):
        """from_row parses complete uploaded file correctly."""
        row = (
            1,  # id
            5,  # session_id
            "F123ABC",  # slack_file_id
            "report.pdf",  # filename
            "application/pdf",  # mimetype
            102400,  # size
            "/tmp/uploads/report.pdf",  # local_path
            "2024-01-15T10:30:00",  # uploaded_at
            "2024-01-15T11:00:00",  # last_referenced
        )

        file = UploadedFile.from_row(row)

        assert file.id == 1
        assert file.slack_file_id == "F123ABC"
        assert file.filename == "report.pdf"
        assert file.mimetype == "application/pdf"
        assert file.size == 102400
        assert file.local_path == "/tmp/uploads/report.pdf"


class TestGitCheckpoint:
    """Tests for GitCheckpoint model."""

    def test_from_row_complete(self):
        """from_row parses complete checkpoint correctly."""
        row = (
            1,  # id
            5,  # session_id
            "C123",  # channel_id
            "before-refactor",  # name
            "stash@{0}",  # stash_ref
            "checkpoint: before-refactor",  # stash_message
            "Saving state before major refactor",  # description
            "2024-01-15T10:30:00",  # created_at
            0,  # is_auto (False)
        )

        checkpoint = GitCheckpoint.from_row(row)

        assert checkpoint.id == 1
        assert checkpoint.name == "before-refactor"
        assert checkpoint.stash_ref == "stash@{0}"
        assert checkpoint.description == "Saving state before major refactor"
        assert checkpoint.is_auto is False

    def test_from_row_auto_checkpoint(self):
        """from_row handles auto checkpoints correctly."""
        row = (
            1,
            1,
            "C123",
            "auto-save",
            "stash@{1}",
            None,
            None,
            "2024-01-15T10:30:00",
            1,
        )

        checkpoint = GitCheckpoint.from_row(row)

        assert checkpoint.is_auto is True


class TestNotificationSettings:
    """Tests for NotificationSettings model."""

    def test_from_row_complete(self):
        """from_row parses complete settings correctly."""
        row = (
            1,  # id
            "C123ABC",  # channel_id
            1,  # notify_on_completion (True)
            0,  # notify_on_permission (False)
            "2024-01-15T10:30:00",  # created_at
            "2024-01-15T11:00:00",  # updated_at
        )

        settings = NotificationSettings.from_row(row)

        assert settings.id == 1
        assert settings.channel_id == "C123ABC"
        assert settings.notify_on_completion is True
        assert settings.notify_on_permission is False

    def test_default_factory(self):
        """default creates settings with all notifications enabled."""
        settings = NotificationSettings.default("C123ABC")

        assert settings.channel_id == "C123ABC"
        assert settings.notify_on_completion is True
        assert settings.notify_on_permission is True
        assert settings.id is None

    def test_default_values(self):
        """NotificationSettings has correct defaults."""
        settings = NotificationSettings()

        assert settings.notify_on_completion is True
        assert settings.notify_on_permission is True
