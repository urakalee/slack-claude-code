"""Unit tests for queue processing handlers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.database.models import Session
from src.handlers.claude.queue import (
    _QUEUE_START_LOCKS,
    _process_queue,
    _queue_task_id,
    ensure_queue_processor,
    register_queue_commands,
)


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator


def _queue_item(item_id: int, prompt: str, working_directory_override: str | None = None):
    """Build a queue-item-like namespace for tests."""
    return SimpleNamespace(
        id=item_id,
        prompt=prompt,
        working_directory_override=working_directory_override,
        parallel_group_id=None,
        parallel_limit=None,
    )


@pytest.mark.asyncio
async def test_process_queue_marks_failed_when_initial_notification_fails():
    """Queue item should fail instead of staying running if initial Slack post fails."""
    item = _queue_item(42, "run analysis")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(side_effect=[Exception("slack unavailable"), {"ts": "999.001"}]),
        chat_update=AsyncMock(),
    )

    with patch("src.handlers.claude.queue.execute_for_session", new=AsyncMock()) as mock_execute:
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    assert deps.db.update_queue_item_status.await_count == 2
    assert deps.db.update_queue_item_status.await_args_list[0].args == (42, "running")
    assert deps.db.update_queue_item_status.await_args_list[1].args == (42, "failed")
    assert (
        deps.db.update_queue_item_status.await_args_list[1].kwargs["error_message"]
        == "slack unavailable"
    )
    mock_execute.assert_not_awaited()
    client.chat_update.assert_not_called()


@pytest.mark.asyncio
async def test_process_queue_skips_item_if_it_is_removed_before_claim():
    """Queue worker should skip execution when pending->running claim fails."""
    item = _queue_item(43, "run analysis")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(return_value=False),
            get_or_create_session=AsyncMock(),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(),
        chat_update=AsyncMock(),
    )

    with patch("src.handlers.claude.queue.execute_for_session", new=AsyncMock()) as mock_execute:
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    deps.db.update_queue_item_status.assert_awaited_once_with(43, "running")
    mock_execute.assert_not_awaited()
    client.chat_postMessage.assert_not_called()
    client.chat_update.assert_not_called()


@pytest.mark.asyncio
async def test_process_queue_completes_item_and_updates_message():
    """Successful queue item execution should complete and update Slack message."""
    item = _queue_item(7, "run tests")
    session = SimpleNamespace(id=1)
    route_result = SimpleNamespace(
        result=SimpleNamespace(success=True, output="done", error=None),
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(return_value=route_result),
    ):
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    assert deps.db.update_queue_item_status.await_count == 2
    assert deps.db.update_queue_item_status.await_args_list[0].args == (7, "running")
    assert deps.db.update_queue_item_status.await_args_list[1].args == (7, "completed")
    assert deps.db.update_queue_item_status.await_args_list[1].kwargs["output"] == "done"
    assert client.chat_postMessage.await_args.kwargs["text"] == "Processing queue item 1: run tests"
    client.chat_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_queue_completion_update_failure_keeps_completed_status():
    """Streaming finalization failures should not flip successful item to failed."""
    item = _queue_item(71, "run tests")
    session = SimpleNamespace(id=1)
    route_result = SimpleNamespace(
        result=SimpleNamespace(success=True, output="done", error=None),
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(side_effect=Exception("slack update failed")),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(return_value=route_result),
    ):
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    statuses = [call.args[1] for call in deps.db.update_queue_item_status.await_args_list]
    assert statuses == ["running", "completed"]
    assert client.chat_postMessage.await_count == 1


@pytest.mark.asyncio
async def test_process_queue_failure_notification_error_does_not_crash_worker():
    """Slack notification failures in exception path should be logged, not raised."""
    item = _queue_item(72, "run tests")
    session = SimpleNamespace(id=1)
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(side_effect=Exception("slack update failed")),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(side_effect=Exception("execution failed")),
    ):
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    statuses = [call.args[1] for call in deps.db.update_queue_item_status.await_args_list]
    assert statuses == ["running", "failed"]
    client.chat_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_queue_streams_updates_during_execution():
    """Queue item execution should stream intermediate output updates."""
    item = _queue_item(70, "run tests")
    session = SimpleNamespace(id=1)
    route_result = SimpleNamespace(
        result=SimpleNamespace(success=True, output="done", error=None),
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    async def fake_execute_for_session(**kwargs):
        await kwargs["on_chunk"](
            SimpleNamespace(type="assistant", content="partial output", tool_activities=[])
        )
        return route_result

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(side_effect=fake_execute_for_session),
    ) as mock_execute:
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    assert mock_execute.await_args.kwargs["on_chunk"] is not None
    assert client.chat_update.await_count >= 2


@pytest.mark.asyncio
async def test_process_queue_waits_for_active_codex_turn():
    """Queue processor should wait while active Codex turn is in progress for the same scope."""
    item = _queue_item(8, "follow up")
    session = SimpleNamespace(id=1)
    route_result = SimpleNamespace(result=SimpleNamespace(success=True, output="ok", error=None))
    codex_executor = SimpleNamespace(has_active_turn=AsyncMock(side_effect=[True, False, False]))
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
        ),
        codex_executor=codex_executor,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(return_value=route_result),
    ):
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock(), thread_ts="123.4")

    assert codex_executor.has_active_turn.await_count >= 2


@pytest.mark.asyncio
async def test_process_queue_recovers_from_transient_scope_error():
    """Scope-level errors should be logged and retried without crashing worker."""
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[Exception("db hiccup"), []]),
            update_queue_item_status=AsyncMock(),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(chat_postMessage=AsyncMock(), chat_update=AsyncMock())
    fake_logger = MagicMock()

    with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
        await _process_queue("C123", deps, client, fake_logger)

    assert deps.db.get_pending_queue_items.await_count == 2
    deps.db.update_queue_item_status.assert_not_called()
    client.chat_postMessage.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_queue_processor_startup_is_serialized():
    """Concurrent startup checks should create only one processor task per scope."""
    deps = SimpleNamespace()
    client = SimpleNamespace()
    created_state = {"running": False}

    async def fake_is_running(*args, **kwargs):
        return created_state["running"]

    create_calls = 0

    async def fake_create_task(coro, *args, **kwargs):
        nonlocal create_calls
        create_calls += 1
        # Simulate work that would expose races without per-scope locking.
        await asyncio.sleep(0.01)
        created_state["running"] = True
        coro.close()
        return AsyncMock()

    with patch(
        "src.handlers.claude.queue._is_queue_processor_running",
        new=AsyncMock(side_effect=fake_is_running),
    ):
        with patch(
            "src.handlers.claude.queue._create_queue_task",
            new=AsyncMock(side_effect=fake_create_task),
        ):
            await asyncio.gather(
                ensure_queue_processor("C123", "123.4", deps, client, MagicMock()),
                ensure_queue_processor("C123", "123.4", deps, client, MagicMock()),
            )

    assert create_calls == 1


@pytest.mark.asyncio
async def test_process_queue_cleans_scope_start_lock_on_exit():
    """Queue processor should clean up idle startup lock entries when exiting."""
    _QUEUE_START_LOCKS.clear()
    thread_ts = "123.4"
    task_id = _queue_task_id("C123", thread_ts)
    _QUEUE_START_LOCKS[task_id] = asyncio.Lock()

    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(return_value=[]),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(),
        chat_update=AsyncMock(),
    )

    await _process_queue("C123", deps, client, MagicMock(), thread_ts=thread_ts)

    assert task_id not in _QUEUE_START_LOCKS


@pytest.mark.asyncio
async def test_process_queue_cancelled_marks_running_item_cancelled():
    """Cancellation while running should transition current queue item to cancelled."""
    item = _queue_item(9, "long job")
    session = SimpleNamespace(id=1)
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item]]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "222.333"}),
        chat_update=AsyncMock(),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _process_queue("C123", deps, client, MagicMock())

    assert deps.db.update_queue_item_status.await_count == 2
    assert deps.db.update_queue_item_status.await_args_list[0].args == (9, "running")
    assert deps.db.update_queue_item_status.await_args_list[1].args == (9, "cancelled")


@pytest.mark.asyncio
async def test_register_queue_commands_exposes_current_queue_commands():
    """Queue command registration should expose /q, /qc, /qv, /qclear, and /qr."""
    app = _FakeApp()
    deps = SimpleNamespace(db=SimpleNamespace())

    register_queue_commands(app, deps)

    assert "/q" in app.handlers
    assert "/qc" in app.handlers
    assert "/qv" in app.handlers
    assert "/qclear" in app.handlers
    assert "/qr" in app.handlers


@pytest.mark.asyncio
async def test_qv_posts_queue_status():
    """`/qv` should render queue state."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(return_value=[]),
            get_running_queue_items=AsyncMock(return_value=[]),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/qv"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/qv",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.get_pending_queue_items.assert_awaited_once_with("C123", None)
    deps.db.get_running_queue_items.assert_awaited_once_with("C123", None)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Queue status"


@pytest.mark.asyncio
async def test_qc_view_subcommand_posts_queue_status():
    """`/qc view` should render queue state."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(return_value=[]),
            get_running_queue_items=AsyncMock(return_value=[]),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/qc"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "view",
            "command": "/qc",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.get_pending_queue_items.assert_awaited_once_with("C123", None)
    deps.db.get_running_queue_items.assert_awaited_once_with("C123", None)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Queue status"


@pytest.mark.asyncio
async def test_qr_without_id_removes_next_pending_item():
    """`/qr` should remove the next pending queue item."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(return_value=[SimpleNamespace(id=11)]),
            remove_queue_item=AsyncMock(return_value=True),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/qr"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/qr",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.remove_queue_item.assert_awaited_once_with(11, "C123", None)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Removed item #11 from queue"


@pytest.mark.asyncio
async def test_qc_remove_without_id_removes_next_pending_item():
    """`/qc remove` should remove the next pending queue item."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(return_value=[SimpleNamespace(id=12)]),
            remove_queue_item=AsyncMock(return_value=True),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/qc"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "remove",
            "command": "/qc",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.remove_queue_item.assert_awaited_once_with(12, "C123", None)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Removed item #12 from queue"


@pytest.mark.asyncio
async def test_qclear_clears_pending_items():
    """`/qclear` should clear pending queue items."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            clear_queue=AsyncMock(return_value=3),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/qclear"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/qclear",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.clear_queue.assert_awaited_once_with("C123", None)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Cleared 3 item(s) from queue"


@pytest.mark.asyncio
async def test_q_parses_structured_plan_and_queues_all_items():
    """`/q` should parse structured plan markers and enqueue expanded prompts atomically."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=Session(id=1, working_directory="/repo")),
            add_many_to_queue=AsyncMock(
                return_value=[
                    SimpleNamespace(id=1, position=1),
                    SimpleNamespace(id=2, position=2),
                    SimpleNamespace(id=3, position=3),
                ]
            ),
            get_running_queue_items=AsyncMock(return_value=[]),
        )
    )
    register_queue_commands(app, deps)

    handler = app.handlers["/q"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    with patch("src.handlers.claude.queue.contains_queue_plan_markers", return_value=True):
        with patch(
            "src.handlers.claude.queue.materialize_queue_plan_text",
            new=AsyncMock(
                return_value=[
                    SimpleNamespace(
                        prompt="first",
                        working_directory_override=None,
                        parallel_group_id=None,
                        parallel_limit=None,
                    ),
                    SimpleNamespace(
                        prompt="second",
                        working_directory_override="/repo-worktrees/feature",
                        parallel_group_id=None,
                        parallel_limit=None,
                    ),
                    SimpleNamespace(
                        prompt="third",
                        working_directory_override=None,
                        parallel_group_id=None,
                        parallel_limit=None,
                    ),
                ]
            ),
        ):
            with patch(
                "src.handlers.claude.queue.ensure_queue_processor", new=AsyncMock()
            ) as mock_ensure:
                await handler(
                    ack=AsyncMock(),
                    command={
                        "channel_id": "C123",
                        "user_id": "U123",
                        "text": "first\n***\nsecond",
                        "command": "/q",
                    },
                    client=client,
                    logger=MagicMock(),
                )

    deps.db.add_many_to_queue.assert_awaited_once_with(
        session_id=1,
        channel_id="C123",
        thread_ts=None,
        queue_entries=[
            ("first", None, None, None),
            ("second", "/repo-worktrees/feature", None, None),
            ("third", None, None, None),
        ],
    )
    assert "Added 3 item(s) to queue" in client.chat_postMessage.await_args.kwargs["text"]
    mock_ensure.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_queue_uses_worktree_override_and_non_persistent_session_ids():
    """Worktree-scoped queue items should run with cwd override and in-memory resume IDs."""
    item1 = _queue_item(201, "task one", "/repo-worktrees/feature-x")
    item2 = _queue_item(202, "task two", "/repo-worktrees/feature-x")
    session = Session(id=1, working_directory="/repo", model="gpt-5.3-codex")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item1], [item2], []]),
            update_queue_item_status=AsyncMock(),
            get_or_create_session=AsyncMock(return_value=session),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    call_sessions: list[Session] = []

    async def _fake_execute_for_session(**kwargs):
        call_sessions.append(kwargs["session"])
        if len(call_sessions) == 1:
            return SimpleNamespace(
                backend="codex",
                result=SimpleNamespace(
                    success=True,
                    output="done-one",
                    error=None,
                    session_id="codex-worktree-session-1",
                ),
            )
        return SimpleNamespace(
            backend="codex",
            result=SimpleNamespace(
                success=True,
                output="done-two",
                error=None,
                session_id="codex-worktree-session-2",
            ),
        )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(side_effect=_fake_execute_for_session),
    ) as mock_execute:
        with patch("src.handlers.claude.queue.asyncio.sleep", new=AsyncMock()):
            await _process_queue("C123", deps, client, MagicMock())

    assert mock_execute.await_count == 2
    first_kwargs = mock_execute.await_args_list[0].kwargs
    second_kwargs = mock_execute.await_args_list[1].kwargs
    assert first_kwargs["persist_session_ids"] is False
    assert second_kwargs["persist_session_ids"] is False
    assert call_sessions[0].working_directory == "/repo-worktrees/feature-x"
    assert call_sessions[0].codex_session_id is None
    assert call_sessions[1].working_directory == "/repo-worktrees/feature-x"
    assert call_sessions[1].codex_session_id == "codex-worktree-session-1"


@pytest.mark.asyncio
async def test_process_queue_parallel_group_honors_width_and_uses_isolated_scopes():
    """Parallel groups should respect width and use isolated execution scopes."""
    item1 = _queue_item(301, "task one")
    item1.parallel_group_id = "parallel-1"
    item1.parallel_limit = 2
    item2 = _queue_item(302, "task two")
    item2.parallel_group_id = "parallel-1"
    item2.parallel_limit = 2
    item3 = _queue_item(303, "task three")
    item3.parallel_group_id = "parallel-1"
    item3.parallel_limit = 2
    session = Session(
        id=1,
        working_directory="/repo",
        model="opus",
        claude_session_id="claude-main-session",
    )
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(side_effect=[[item1, item2, item3], []]),
            get_queue_group_items=AsyncMock(return_value=[item1, item2, item3]),
            update_queue_item_status=AsyncMock(return_value=True),
            get_or_create_session=AsyncMock(return_value=session),
            get_command_history=AsyncMock(return_value=([], 0)),
        ),
        codex_executor=None,
    )
    client = SimpleNamespace(
        chat_postMessage=AsyncMock(return_value={"ts": "123.456"}),
        chat_update=AsyncMock(),
    )

    active = 0
    max_active = 0
    call_sessions: list[Session] = []

    async def _fake_execute_for_session(**kwargs):
        nonlocal active, max_active
        call_sessions.append(kwargs["session"])
        active += 1
        max_active = max(max_active, active)
        if active == 2:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        active -= 1
        return SimpleNamespace(
            backend="claude",
            result=SimpleNamespace(success=True, output="done", error=None, session_id=None),
        )

    with patch(
        "src.handlers.claude.queue.execute_for_session",
        new=AsyncMock(side_effect=_fake_execute_for_session),
    ) as mock_execute:
        await _process_queue("C123", deps, client, MagicMock())

    assert max_active == 2
    assert mock_execute.await_count == 3
    for call in mock_execute.await_args_list:
        assert call.kwargs["persist_session_ids"] is False
        assert ":parallel:parallel-1:" in call.kwargs["session_scope_override"]
    assert all(session_arg.claude_session_id is None for session_arg in call_sessions)
