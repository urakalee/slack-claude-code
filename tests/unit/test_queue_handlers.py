"""Unit tests for queue processing handlers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.handlers.claude.queue import _process_queue


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
        chat_postMessage=AsyncMock(
            side_effect=[Exception("slack unavailable"), {"ts": "999.001"}]
        ),
        chat_update=AsyncMock(),
    )

    with patch(
        "src.handlers.claude.queue.execute_for_session", new=AsyncMock()
    ) as mock_execute:
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
    assert (
        deps.db.update_queue_item_status.await_args_list[1].kwargs["output"] == "done"
    )
    client.chat_update.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_queue_waits_for_active_codex_turn():
    """Queue processor should wait while active Codex turn is in progress for the same scope."""
    item = SimpleNamespace(id=8, prompt="follow up")
    session = SimpleNamespace(id=1)
    route_result = SimpleNamespace(
        result=SimpleNamespace(success=True, output="ok", error=None)
    )
    codex_executor = SimpleNamespace(
        has_active_turn=AsyncMock(side_effect=[True, False])
    )
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
