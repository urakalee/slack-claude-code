"""Unit tests for database repository."""

import asyncio

import aiosqlite
import pytest
import pytest_asyncio

from src.database.migrations import init_database
from src.database.repository import DatabaseRepository


@pytest_asyncio.fixture
async def db_repo(tmp_path):
    """Create a test database repository."""
    db_path = str(tmp_path / "test.db")
    await init_database(db_path)
    return DatabaseRepository(db_path)


class TestSessionOperations:
    """Tests for session CRUD operations."""

    @pytest.mark.asyncio
    async def test_get_or_create_session_creates_new(self, db_repo):
        """get_or_create_session creates new session when none exists."""
        session = await db_repo.get_or_create_session("C123ABC", None, "/home/user")

        assert session.id is not None
        assert session.channel_id == "C123ABC"
        assert session.thread_ts is None
        assert session.working_directory == "/home/user"

    @pytest.mark.asyncio
    async def test_get_or_create_session_returns_existing(self, db_repo):
        """get_or_create_session returns existing session."""
        session1 = await db_repo.get_or_create_session("C123ABC", None)
        session2 = await db_repo.get_or_create_session("C123ABC", None)

        assert session1.id == session2.id

    @pytest.mark.asyncio
    async def test_get_or_create_session_channel_level_does_not_duplicate(self, db_repo):
        """Channel-level sessions should reuse one row even with thread_ts=None."""
        session1 = await db_repo.get_or_create_session("C123ABC", None)
        session2 = await db_repo.get_or_create_session("C123ABC", None)
        session3 = await db_repo.get_or_create_session("C123ABC", None)

        assert session1.id == session2.id == session3.id

        async with aiosqlite.connect(db_repo.db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM sessions WHERE channel_id = ? AND thread_ts IS NULL",
                ("C123ABC",),
            )
            count = (await cursor.fetchone())[0]

        assert count == 1

    @pytest.mark.asyncio
    async def test_get_or_create_session_prefers_populated_duplicate_row(self, db_repo):
        """If duplicate NULL-thread rows exist, choose the most populated one."""
        async with aiosqlite.connect(db_repo.db_path) as db:
            await db.execute(
                """INSERT INTO sessions (
                       channel_id, thread_ts, working_directory, model, permission_mode,
                       codex_session_id, last_active
                   ) VALUES (?, NULL, ?, NULL, NULL, NULL, ?)""",
                ("C123ABC", "/tmp/empty", "2026-01-01T00:00:00+00:00"),
            )
            await db.execute(
                """INSERT INTO sessions (
                       channel_id, thread_ts, working_directory, model, permission_mode,
                       codex_session_id, last_active
                   ) VALUES (?, NULL, ?, ?, ?, ?, ?)""",
                (
                    "C123ABC",
                    "/tmp/populated",
                    "gpt-5.3-codex",
                    "plan",
                    "codex-session-123",
                    "2026-01-02T00:00:00+00:00",
                ),
            )
            await db.commit()

        session = await db_repo.get_or_create_session("C123ABC", None)
        assert session.working_directory == "/tmp/populated"
        assert session.model == "gpt-5.3-codex"
        assert session.codex_session_id == "codex-session-123"

    @pytest.mark.asyncio
    async def test_get_or_create_session_prefers_model_row_on_population_tie(self, db_repo):
        """Model-bearing rows should win when duplicate rows are otherwise equally populated."""
        async with aiosqlite.connect(db_repo.db_path) as db:
            await db.execute(
                """INSERT INTO sessions (
                       channel_id, thread_ts, working_directory, model, permission_mode,
                       codex_session_id, last_active
                   ) VALUES (?, NULL, ?, ?, ?, NULL, ?)""",
                (
                    "C123ABC",
                    "/tmp/model",
                    "gpt-5.3-codex",
                    "default",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
            await db.execute(
                """INSERT INTO sessions (
                       channel_id, thread_ts, working_directory, model, permission_mode,
                       codex_session_id, last_active
                   ) VALUES (?, NULL, ?, NULL, ?, ?, ?)""",
                (
                    "C123ABC",
                    "/tmp/no-model",
                    "default",
                    "codex-session-999",
                    "2026-01-02T00:00:00+00:00",
                ),
            )
            await db.commit()

        session = await db_repo.get_or_create_session("C123ABC", None)
        assert session.working_directory == "/tmp/model"
        assert session.model == "gpt-5.3-codex"

    @pytest.mark.asyncio
    async def test_get_or_create_session_thread_isolation(self, db_repo):
        """Different threads get different sessions."""
        channel_session = await db_repo.get_or_create_session("C123ABC", None)
        thread_session = await db_repo.get_or_create_session("C123ABC", "1234567890.123456")

        assert channel_session.id != thread_session.id
        assert channel_session.thread_ts is None
        assert thread_session.thread_ts == "1234567890.123456"

    @pytest.mark.asyncio
    async def test_get_or_create_session_normalizes_empty_thread_ts(self, db_repo):
        """Empty thread_ts should map to the same channel-level session scope."""
        session_with_none = await db_repo.get_or_create_session("C123ABC", None)
        session_with_empty = await db_repo.get_or_create_session("C123ABC", "")

        assert session_with_none.id == session_with_empty.id
        assert session_with_empty.thread_ts is None

    @pytest.mark.asyncio
    async def test_get_or_create_thread_inherits_channel_session_context(self, db_repo):
        """New thread sessions inherit channel-level session context at creation time."""
        await db_repo.get_or_create_session("C123ABC", None, "/repo")
        await db_repo.update_session_model("C123ABC", None, "gpt-5.3-codex-high")
        await db_repo.update_session_mode("C123ABC", None, "plan")
        await db_repo.add_session_dir("C123ABC", None, "/repo/subdir")
        await db_repo.update_session_claude_id("C123ABC", None, "claude-session-xyz")
        await db_repo.update_session_codex_id("C123ABC", None, "codex-thread-123")
        await db_repo.update_session_sandbox_mode("C123ABC", None, "danger-full-access")
        await db_repo.update_session_approval_mode("C123ABC", None, "never")

        thread_session = await db_repo.get_or_create_session("C123ABC", "1234567890.123456")
        assert thread_session.working_directory == "/repo"
        assert thread_session.model == "gpt-5.3-codex-high"
        assert thread_session.permission_mode == "plan"
        assert thread_session.added_dirs == ["/repo/subdir"]
        assert thread_session.claude_session_id == "claude-session-xyz"
        assert thread_session.codex_session_id == "codex-thread-123"
        assert thread_session.sandbox_mode == "danger-full-access"
        assert thread_session.approval_mode == "never"

    @pytest.mark.asyncio
    async def test_update_session_cwd(self, db_repo):
        """update_session_cwd updates working directory."""
        session = await db_repo.get_or_create_session("C123ABC", None, "~")
        await db_repo.update_session_cwd("C123ABC", None, "/new/path")

        updated = await db_repo.get_or_create_session("C123ABC", None)
        assert updated.working_directory == "/new/path"

    @pytest.mark.asyncio
    async def test_update_session_claude_id(self, db_repo):
        """update_session_claude_id updates claude session id."""
        await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.update_session_claude_id("C123ABC", None, "claude-session-xyz")

        session = await db_repo.get_or_create_session("C123ABC", None)
        assert session.claude_session_id == "claude-session-xyz"

    @pytest.mark.asyncio
    async def test_clear_session_claude_id(self, db_repo):
        """clear_session_claude_id clears the session id."""
        await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.update_session_claude_id("C123ABC", None, "claude-session-xyz")
        await db_repo.clear_session_claude_id("C123ABC", None)

        session = await db_repo.get_or_create_session("C123ABC", None)
        assert session.claude_session_id is None

    @pytest.mark.asyncio
    async def test_update_session_mode(self, db_repo):
        """update_session_mode updates permission mode."""
        await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.update_session_mode("C123ABC", None, "plan")

        session = await db_repo.get_or_create_session("C123ABC", None)
        assert session.permission_mode == "plan"

    @pytest.mark.asyncio
    async def test_update_session_model(self, db_repo):
        """update_session_model updates model."""
        await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.update_session_model("C123ABC", None, "opus")

        session = await db_repo.get_or_create_session("C123ABC", None)
        assert session.model == "opus"

    @pytest.mark.asyncio
    async def test_update_session_model_creates_missing_session(self, db_repo):
        """update_session_model creates a session when one doesn't exist yet."""
        await db_repo.update_session_model("C123ABC", None, "gpt-5.3-codex")

        session = await db_repo.get_or_create_session("C123ABC", None)
        assert session.model == "gpt-5.3-codex"

    @pytest.mark.asyncio
    async def test_restore_channel_model_selections_returns_saved_models(self, db_repo):
        """restore_channel_model_selections returns persisted channel model selections."""
        await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.update_session_model("C123ABC", None, "gpt-5.3-codex-high")

        selections = await db_repo.restore_channel_model_selections()
        assert selections["C123ABC"] == "gpt-5.3-codex-high"

    @pytest.mark.asyncio
    async def test_add_session_dir_requires_existing_session(self, db_repo):
        """add_session_dir raises when target session doesn't exist."""
        with pytest.raises(RuntimeError, match="Session not found"):
            await db_repo.add_session_dir("CMISSING", None, "/tmp")

    @pytest.mark.asyncio
    async def test_remove_session_dir_requires_existing_session(self, db_repo):
        """remove_session_dir raises when target session doesn't exist."""
        with pytest.raises(RuntimeError, match="Session not found"):
            await db_repo.remove_session_dir("CMISSING", None, "/tmp")

    @pytest.mark.asyncio
    async def test_update_session_codex_id(self, db_repo):
        """update_session_codex_id persists and is readable via get_or_create_session."""
        await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.update_session_codex_id("C123ABC", None, "codex-session-xyz")

        session = await db_repo.get_or_create_session("C123ABC", None)
        assert session.codex_session_id == "codex-session-xyz"

    @pytest.mark.asyncio
    async def test_delete_session(self, db_repo):
        """delete_session removes a session."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        result = await db_repo.delete_session("C123ABC", None)

        assert result is True
        assert await db_repo.get_session_by_id(session.id) is None

    @pytest.mark.asyncio
    async def test_delete_session_nonexistent(self, db_repo):
        """delete_session returns False for nonexistent session."""
        result = await db_repo.delete_session("NONEXISTENT", None)
        assert result is False


class TestCommandHistoryOperations:
    """Tests for command history operations."""

    @pytest.mark.asyncio
    async def test_add_command(self, db_repo):
        """add_command creates command history entry."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        cmd = await db_repo.add_command(session.id, "analyze this code")

        assert cmd.id is not None
        assert cmd.session_id == session.id
        assert cmd.command == "analyze this code"
        assert cmd.status == "pending"

    @pytest.mark.asyncio
    async def test_update_command_status_running(self, db_repo):
        """update_command_status updates to running."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        cmd = await db_repo.add_command(session.id, "test")
        await db_repo.update_command_status(cmd.id, "running")

        updated = await db_repo.get_command_by_id(cmd.id)
        assert updated.status == "running"

    @pytest.mark.asyncio
    async def test_update_command_status_completed(self, db_repo):
        """update_command_status updates to completed with output."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        cmd = await db_repo.add_command(session.id, "test")
        await db_repo.update_command_status(cmd.id, "completed", output="Success!")

        updated = await db_repo.get_command_by_id(cmd.id)
        assert updated.status == "completed"
        assert updated.output == "Success!"
        assert updated.completed_at is not None

    @pytest.mark.asyncio
    async def test_update_command_status_failed(self, db_repo):
        """update_command_status updates to failed with error."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        cmd = await db_repo.add_command(session.id, "test")
        await db_repo.update_command_status(cmd.id, "failed", error_message="Something broke")

        updated = await db_repo.get_command_by_id(cmd.id)
        assert updated.status == "failed"
        assert updated.error_message == "Something broke"

    @pytest.mark.asyncio
    async def test_append_command_output(self, db_repo):
        """append_command_output appends to existing output."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        cmd = await db_repo.add_command(session.id, "test")

        await db_repo.append_command_output(cmd.id, "First chunk. ")
        await db_repo.append_command_output(cmd.id, "Second chunk.")

        updated = await db_repo.get_command_by_id(cmd.id)
        assert updated.output == "First chunk. Second chunk."

    @pytest.mark.asyncio
    async def test_get_command_history_pagination(self, db_repo):
        """get_command_history supports pagination."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        for i in range(15):
            await db_repo.add_command(session.id, f"command {i}")

        # Get first page
        history, total = await db_repo.get_command_history(session.id, limit=5, offset=0)
        assert len(history) == 5
        assert total == 15

        # Get second page
        history2, _ = await db_repo.get_command_history(session.id, limit=5, offset=5)
        assert len(history2) == 5

    @pytest.mark.asyncio
    async def test_get_command_by_id_not_found(self, db_repo):
        """get_command_by_id returns None for nonexistent."""
        result = await db_repo.get_command_by_id(99999)
        assert result is None


class TestQueueOperations:
    """Tests for queue operations."""

    @pytest.mark.asyncio
    async def test_add_to_queue(self, db_repo):
        """add_to_queue creates queue item."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        item = await db_repo.add_to_queue(session.id, "C123ABC", None, "analyze code")

        assert item.id is not None
        assert item.prompt == "analyze code"
        assert item.status == "pending"
        assert item.position == 1

    @pytest.mark.asyncio
    async def test_add_to_queue_with_working_directory_override(self, db_repo):
        """add_to_queue stores working_directory_override when provided."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        item = await db_repo.add_to_queue(
            session.id,
            "C123ABC",
            None,
            "run in worktree",
            working_directory_override="/repo-worktrees/feature-x",
        )

        assert item.working_directory_override == "/repo-worktrees/feature-x"

    @pytest.mark.asyncio
    async def test_add_to_queue_auto_position(self, db_repo):
        """add_to_queue auto-increments position."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        item1 = await db_repo.add_to_queue(session.id, "C123ABC", None, "first")
        item2 = await db_repo.add_to_queue(session.id, "C123ABC", None, "second")
        item3 = await db_repo.add_to_queue(session.id, "C123ABC", None, "third")

        assert item1.position == 1
        assert item2.position == 2
        assert item3.position == 3

    @pytest.mark.asyncio
    async def test_add_to_queue_concurrent_unique_positions(self, db_repo):
        """add_to_queue should assign unique positions under concurrent inserts."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        items = await asyncio.gather(
            *[db_repo.add_to_queue(session.id, "C123ABC", None, f"cmd-{i}") for i in range(20)]
        )

        positions = sorted(item.position for item in items)
        assert positions == list(range(1, 21))

        pending = await db_repo.get_pending_queue_items("C123ABC", None)
        assert [item.position for item in pending] == list(range(1, 21))

    @pytest.mark.asyncio
    async def test_add_many_to_queue_inserts_items_in_order(self, db_repo):
        """add_many_to_queue inserts multiple items atomically with increasing positions."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        items = await db_repo.add_many_to_queue(
            session_id=session.id,
            channel_id="C123ABC",
            thread_ts=None,
            queue_entries=[
                ("first", None, None, None),
                ("second", "/repo-worktrees/feature-y", None, None),
                ("third", None, None, None),
            ],
        )

        assert [item.prompt for item in items] == ["first", "second", "third"]
        assert [item.position for item in items] == [1, 2, 3]
        assert items[1].working_directory_override == "/repo-worktrees/feature-y"

    @pytest.mark.asyncio
    async def test_add_many_to_queue_preserves_parallel_metadata(self, db_repo):
        """add_many_to_queue stores parallel group metadata on queued items."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        items = await db_repo.add_many_to_queue(
            session_id=session.id,
            channel_id="C123ABC",
            thread_ts=None,
            queue_entries=[
                ("first", None, "parallel-1", 2),
                ("second", None, "parallel-1", 2),
            ],
        )

        assert [item.parallel_group_id for item in items] == ["parallel-1", "parallel-1"]
        assert [item.parallel_limit for item in items] == [2, 2]

    @pytest.mark.asyncio
    async def test_add_many_to_queue_respects_existing_positions(self, db_repo):
        """add_many_to_queue appends after existing queue positions."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.add_to_queue(session.id, "C123ABC", None, "existing")

        items = await db_repo.add_many_to_queue(
            session_id=session.id,
            channel_id="C123ABC",
            thread_ts=None,
            queue_entries=[("next-1", None, None, None), ("next-2", None, None, None)],
        )

        assert [item.position for item in items] == [2, 3]

    @pytest.mark.asyncio
    async def test_get_pending_queue_items(self, db_repo):
        """get_pending_queue_items returns pending items in order."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.add_to_queue(session.id, "C123ABC", None, "first")
        await db_repo.add_to_queue(session.id, "C123ABC", None, "second")

        pending = await db_repo.get_pending_queue_items("C123ABC", None)

        assert len(pending) == 2
        assert pending[0].prompt == "first"
        assert pending[1].prompt == "second"

    @pytest.mark.asyncio
    async def test_update_queue_item_status_running(self, db_repo):
        """update_queue_item_status sets started_at for running."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        item = await db_repo.add_to_queue(session.id, "C123ABC", None, "test")
        await db_repo.update_queue_item_status(item.id, "running")

        updated = await db_repo.get_queue_item(item.id)
        assert updated.status == "running"
        assert updated.started_at is not None

    @pytest.mark.asyncio
    async def test_update_queue_item_status_completed(self, db_repo):
        """update_queue_item_status sets completed_at for completed."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        item = await db_repo.add_to_queue(session.id, "C123ABC", None, "test")
        await db_repo.update_queue_item_status(item.id, "completed", output="Done!")

        updated = await db_repo.get_queue_item(item.id)
        assert updated.status == "completed"
        assert updated.output == "Done!"
        assert updated.completed_at is not None

    @pytest.mark.asyncio
    async def test_remove_queue_item(self, db_repo):
        """remove_queue_item removes pending item."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        item = await db_repo.add_to_queue(session.id, "C123ABC", None, "test")
        result = await db_repo.remove_queue_item(item.id)

        assert result is True
        assert await db_repo.get_queue_item(item.id) is None

    @pytest.mark.asyncio
    async def test_remove_queue_item_not_pending(self, db_repo):
        """remove_queue_item only removes pending items."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        item = await db_repo.add_to_queue(session.id, "C123ABC", None, "test")
        await db_repo.update_queue_item_status(item.id, "running")

        result = await db_repo.remove_queue_item(item.id)
        assert result is False  # Can't remove running item

    @pytest.mark.asyncio
    async def test_remove_queue_item_respects_scope(self, db_repo):
        """remove_queue_item scoped delete only affects matching channel/thread."""
        session = await db_repo.get_or_create_session("C123ABC", "123.456")
        item = await db_repo.add_to_queue(session.id, "C123ABC", "123.456", "test")

        wrong_scope = await db_repo.remove_queue_item(item.id, "C123ABC", None)
        right_scope = await db_repo.remove_queue_item(item.id, "C123ABC", "123.456")

        assert wrong_scope is False
        assert right_scope is True

    @pytest.mark.asyncio
    async def test_clear_queue(self, db_repo):
        """clear_queue removes all pending items."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.add_to_queue(session.id, "C123ABC", None, "first")
        await db_repo.add_to_queue(session.id, "C123ABC", None, "second")
        await db_repo.add_to_queue(session.id, "C123ABC", None, "third")

        count = await db_repo.clear_queue("C123ABC", None)

        assert count == 3
        pending = await db_repo.get_pending_queue_items("C123ABC", None)
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_get_running_queue_item(self, db_repo):
        """get_running_queue_item returns currently running item."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        item = await db_repo.add_to_queue(session.id, "C123ABC", None, "running task")
        await db_repo.update_queue_item_status(item.id, "running")

        running = await db_repo.get_running_queue_item("C123ABC", None)

        assert running is not None
        assert running.id == item.id

    @pytest.mark.asyncio
    async def test_get_running_queue_item_none(self, db_repo):
        """get_running_queue_item returns None when nothing running."""
        result = await db_repo.get_running_queue_item("C123ABC", None)
        assert result is None

    @pytest.mark.asyncio
    async def test_get_running_queue_items_returns_all_running(self, db_repo):
        """get_running_queue_items returns all running items for a scope."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        item1 = await db_repo.add_to_queue(session.id, "C123ABC", None, "first")
        item2 = await db_repo.add_to_queue(session.id, "C123ABC", None, "second")
        await db_repo.update_queue_item_status(item1.id, "running")
        await db_repo.update_queue_item_status(item2.id, "running")

        running = await db_repo.get_running_queue_items("C123ABC", None)

        assert [item.id for item in running] == [item1.id, item2.id]

    @pytest.mark.asyncio
    async def test_get_queue_group_items_filters_by_group_and_status(self, db_repo):
        """get_queue_group_items filters by parallel group id and status."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        items = await db_repo.add_many_to_queue(
            session_id=session.id,
            channel_id="C123ABC",
            thread_ts=None,
            queue_entries=[
                ("first", None, "parallel-1", 2),
                ("second", None, "parallel-1", 2),
                ("third", None, "parallel-2", 1),
            ],
        )
        await db_repo.update_queue_item_status(items[0].id, "running")

        grouped = await db_repo.get_queue_group_items(
            "C123ABC", None, "parallel-1", statuses=("pending", "running")
        )

        assert [item.prompt for item in grouped] == ["first", "second"]

    @pytest.mark.asyncio
    async def test_queue_scope_isolated_by_thread(self, db_repo):
        """Queue items in channel and thread scopes should not overlap."""
        channel_session = await db_repo.get_or_create_session("C123ABC", None)
        thread_session = await db_repo.get_or_create_session("C123ABC", "123.456")
        await db_repo.add_to_queue(channel_session.id, "C123ABC", None, "channel item")
        await db_repo.add_to_queue(thread_session.id, "C123ABC", "123.456", "thread item")

        channel_pending = await db_repo.get_pending_queue_items("C123ABC", None)
        thread_pending = await db_repo.get_pending_queue_items("C123ABC", "123.456")

        assert len(channel_pending) == 1
        assert channel_pending[0].prompt == "channel item"
        assert len(thread_pending) == 1
        assert thread_pending[0].prompt == "thread item"


class TestParallelJobOperations:
    """Tests for parallel job operations."""

    @pytest.mark.asyncio
    async def test_create_parallel_job(self, db_repo):
        """create_parallel_job creates a job."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        config = {"n_instances": 3, "commands": ["cmd1", "cmd2"]}
        job = await db_repo.create_parallel_job(session.id, "C123ABC", "parallel_analysis", config)

        assert job.id is not None
        assert job.job_type == "parallel_analysis"
        assert job.status == "pending"
        assert job.config == config

    @pytest.mark.asyncio
    async def test_update_parallel_job(self, db_repo):
        """update_parallel_job updates job fields."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        job = await db_repo.create_parallel_job(session.id, "C123ABC", "test", {})

        await db_repo.update_parallel_job(
            job.id,
            status="completed",
            results=[{"output": "result1"}],
            aggregation_output="Summary",
        )

        updated = await db_repo.get_parallel_job(job.id)
        assert updated.status == "completed"
        assert updated.results == [{"output": "result1"}]
        assert updated.aggregation_output == "Summary"
        assert updated.completed_at is not None

    @pytest.mark.asyncio
    async def test_get_active_jobs(self, db_repo):
        """get_active_jobs returns pending/running jobs."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        job1 = await db_repo.create_parallel_job(session.id, "C123ABC", "test1", {})
        job2 = await db_repo.create_parallel_job(session.id, "C123ABC", "test2", {})
        await db_repo.update_parallel_job(job2.id, status="running")

        active = await db_repo.get_active_jobs("C123ABC")

        assert len(active) == 2

    @pytest.mark.asyncio
    async def test_get_active_jobs_excludes_completed(self, db_repo):
        """get_active_jobs excludes completed jobs."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        job = await db_repo.create_parallel_job(session.id, "C123ABC", "test", {})
        await db_repo.update_parallel_job(job.id, status="completed")

        active = await db_repo.get_active_jobs("C123ABC")

        assert len(active) == 0

    @pytest.mark.asyncio
    async def test_cancel_job(self, db_repo):
        """cancel_job cancels active job."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        job = await db_repo.create_parallel_job(session.id, "C123ABC", "test", {})

        result = await db_repo.cancel_job(job.id)

        assert result is True
        updated = await db_repo.get_parallel_job(job.id)
        assert updated.status == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_job_already_completed(self, db_repo):
        """cancel_job returns False for completed jobs."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        job = await db_repo.create_parallel_job(session.id, "C123ABC", "test", {})
        await db_repo.update_parallel_job(job.id, status="completed")

        result = await db_repo.cancel_job(job.id)

        assert result is False


class TestNotificationSettings:
    """Tests for notification settings operations."""

    @pytest.mark.asyncio
    async def test_get_notification_settings_default(self, db_repo):
        """get_notification_settings returns defaults for new channel."""
        settings = await db_repo.get_notification_settings("C123ABC")

        assert settings.channel_id == "C123ABC"
        assert settings.notify_on_completion is True
        assert settings.notify_on_permission is True

    @pytest.mark.asyncio
    async def test_update_notification_settings(self, db_repo):
        """update_notification_settings creates/updates settings."""
        await db_repo.update_notification_settings("C123ABC", True, False)

        settings = await db_repo.get_notification_settings("C123ABC")

        assert settings.notify_on_completion is True
        assert settings.notify_on_permission is False

    @pytest.mark.asyncio
    async def test_update_notification_settings_upsert(self, db_repo):
        """update_notification_settings updates existing record."""
        await db_repo.update_notification_settings("C123ABC", True, True)
        await db_repo.update_notification_settings("C123ABC", False, False)

        settings = await db_repo.get_notification_settings("C123ABC")

        assert settings.notify_on_completion is False
        assert settings.notify_on_permission is False


class TestGitCheckpointOperations:
    """Tests for git checkpoint operations."""

    @pytest.mark.asyncio
    async def test_create_checkpoint(self, db_repo):
        """create_checkpoint creates a checkpoint."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        checkpoint = await db_repo.create_checkpoint(
            session.id,
            "C123ABC",
            "before-refactor",
            "stash@{0}",
            stash_message="checkpoint: before-refactor",
            description="Saving state",
        )

        assert checkpoint.id is not None
        assert checkpoint.name == "before-refactor"
        assert checkpoint.stash_ref == "stash@{0}"

    @pytest.mark.asyncio
    async def test_get_checkpoints(self, db_repo):
        """get_checkpoints returns checkpoints for channel."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.create_checkpoint(session.id, "C123ABC", "cp1", "stash@{0}")
        await db_repo.create_checkpoint(session.id, "C123ABC", "cp2", "stash@{1}")

        checkpoints = await db_repo.get_checkpoints("C123ABC")

        assert len(checkpoints) == 2

    @pytest.mark.asyncio
    async def test_get_checkpoints_excludes_auto(self, db_repo):
        """get_checkpoints excludes auto checkpoints by default."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.create_checkpoint(session.id, "C123ABC", "manual", "stash@{0}")
        await db_repo.create_checkpoint(session.id, "C123ABC", "auto", "stash@{1}", is_auto=True)

        checkpoints = await db_repo.get_checkpoints("C123ABC", include_auto=False)

        assert len(checkpoints) == 1
        assert checkpoints[0].name == "manual"

    @pytest.mark.asyncio
    async def test_get_checkpoint_by_name(self, db_repo):
        """get_checkpoint_by_name finds checkpoint by name."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.create_checkpoint(session.id, "C123ABC", "target", "stash@{0}")

        checkpoint = await db_repo.get_checkpoint_by_name("C123ABC", "target")

        assert checkpoint is not None
        assert checkpoint.name == "target"

    @pytest.mark.asyncio
    async def test_delete_checkpoint(self, db_repo):
        """delete_checkpoint removes a checkpoint."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        checkpoint = await db_repo.create_checkpoint(
            session.id, "C123ABC", "to-delete", "stash@{0}"
        )

        result = await db_repo.delete_checkpoint(checkpoint.id)

        assert result is True
        assert await db_repo.get_checkpoint_by_name("C123ABC", "to-delete") is None

    @pytest.mark.asyncio
    async def test_delete_auto_checkpoints(self, db_repo):
        """delete_auto_checkpoints removes only auto checkpoints."""
        session = await db_repo.get_or_create_session("C123ABC", None)
        await db_repo.create_checkpoint(session.id, "C123ABC", "manual", "stash@{0}")
        await db_repo.create_checkpoint(session.id, "C123ABC", "auto1", "stash@{1}", is_auto=True)
        await db_repo.create_checkpoint(session.id, "C123ABC", "auto2", "stash@{2}", is_auto=True)

        count = await db_repo.delete_auto_checkpoints("C123ABC")

        assert count == 2
        checkpoints = await db_repo.get_checkpoints("C123ABC", include_auto=True)
        assert len(checkpoints) == 1
        assert checkpoints[0].name == "manual"
