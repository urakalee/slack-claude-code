"""Unit tests for queue processing handlers."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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


@pytest.mark.asyncio
async def test_process_queue_marks_failed_when_initial_notification_fails():
    """Queue item should fail instead of staying running if initial Slack post fails."""
    item = SimpleNamespace(id=42, prompt="run analysis")
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
    item = SimpleNamespace(id=43, prompt="run analysis")
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
    item = SimpleNamespace(id=7, prompt="run tests")
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
    client.chat_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_queue_completion_update_failure_keeps_completed_status():
    """Completion Slack update failures should not flip successful item to failed."""
    item = SimpleNamespace(id=71, prompt="run tests")
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
    assert client.chat_postMessage.await_count == 2


@pytest.mark.asyncio
async def test_process_queue_failure_notification_error_does_not_crash_worker():
    """Slack notification failures in exception path should be logged, not raised."""
    item = SimpleNamespace(id=72, prompt="run tests")
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
    item = SimpleNamespace(id=70, prompt="run tests")
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
    item = SimpleNamespace(id=8, prompt="follow up")
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
    item = SimpleNamespace(id=9, prompt="long job")
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
            get_running_queue_item=AsyncMock(return_value=None),
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
    deps.db.get_running_queue_item.assert_awaited_once_with("C123", None)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Queue status"


@pytest.mark.asyncio
async def test_qc_view_subcommand_posts_queue_status():
    """`/qc view` should render queue state."""
    app = _FakeApp()
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_pending_queue_items=AsyncMock(return_value=[]),
            get_running_queue_item=AsyncMock(return_value=None),
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
    deps.db.get_running_queue_item.assert_awaited_once_with("C123", None)
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
