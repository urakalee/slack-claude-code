"""Unit tests for app-level helpers."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app import (
    _event_dedupe_key,
    _is_duplicate_event,
    _queue_structured_plan_message,
    _route_codex_message_to_active_turn_or_queue,
    _strip_leading_slack_mention,
    configure_logging,
    slack_api_with_retry,
)


class TestSlackApiRetry:
    """Tests for Slack API retry helper."""

    @pytest.mark.asyncio
    async def test_slack_api_with_retry_propagates_cancellation_immediately(self):
        """CancelledError should never be retried."""
        call_count = 0

        async def failing_call():
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await slack_api_with_retry(failing_call, max_retries=3, base_delay=0)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_slack_api_with_retry_rejects_non_positive_retry_count(self):
        """Retry helper should validate that at least one attempt is allowed."""

        async def successful_call():
            return "ok"

        with pytest.raises(ValueError, match="at least 1"):
            await slack_api_with_retry(successful_call, max_retries=0)


class TestConfigureLogging:
    """Tests for logger sink configuration."""

    def test_configure_logging_writes_log_file_to_database_directory(self, tmp_path):
        """Log file should live next to the configured database with 3-day retention."""
        db_path = tmp_path / "data" / "slack_claude.db"
        expected_log_path = db_path.parent / "slack_claude.log"

        with patch("src.app.config.DATABASE_PATH", str(db_path)):
            with patch("src.app.logger.remove") as mock_remove:
                with patch("src.app.logger.add") as mock_add:
                    configure_logging()

        mock_remove.assert_called_once_with()
        assert mock_add.call_count == 2
        assert mock_add.call_args_list[0].args[0] is sys.stderr

        file_sink_call = mock_add.call_args_list[1]
        assert file_sink_call.args[0] == expected_log_path
        assert file_sink_call.kwargs["retention"] == "3 days"
        assert file_sink_call.kwargs["rotation"] == "00:00"

        assert expected_log_path.parent.exists()


class TestEventHelpers:
    """Tests for Slack message normalization and dedupe helpers."""

    def test_strip_leading_slack_mention(self):
        """Leading bot mention should be stripped while preserving prompt text."""
        assert _strip_leading_slack_mention("<@U123> run tests") == "run tests"
        assert _strip_leading_slack_mention("  <@U123>   run tests  ") == "run tests"
        assert _strip_leading_slack_mention("run tests") == "run tests"

    def test_event_dedupe_key_uses_channel_ts_and_user(self):
        """Dedupe key should be stable across message/app_mention payloads."""
        event = {"channel": "C123", "ts": "111.222", "user": "U999"}
        assert _event_dedupe_key(event) == "C123:111.222:U999"

    def test_duplicate_event_detection_with_ttl(self):
        """Duplicate events inside TTL should be ignored; later events should pass."""
        seen: dict[str, float] = {}
        event = {"channel": "C123", "ts": "111.222", "user": "U999"}

        assert _is_duplicate_event(event, seen, now_monotonic=100.0, ttl_seconds=30.0) is False
        assert _is_duplicate_event(event, seen, now_monotonic=105.0, ttl_seconds=30.0) is True
        assert _is_duplicate_event(event, seen, now_monotonic=131.0, ttl_seconds=30.0) is False


class TestCodexActiveTurnRouting:
    """Tests for active-turn steer and queue fallback behavior."""

    @pytest.mark.asyncio
    async def test_routes_to_active_turn_when_steer_succeeds(self):
        """Active Codex turn should consume follow-up message via steer."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            codex_executor=SimpleNamespace(
                has_active_turn=AsyncMock(return_value=True),
                steer_active_turn=AsyncMock(
                    return_value=SimpleNamespace(success=True, turn_id="turn-123", error=None)
                ),
                record_queue_fallback=AsyncMock(),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=10)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _route_codex_message_to_active_turn_or_queue(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts="123.456",
            prompt="follow up",
            logger=MagicMock(),
        )

        assert handled is True
        deps.db.add_to_queue.assert_not_called()
        deps.db.update_command_status.assert_any_await(
            10,
            "completed",
            output="Routed to active Codex turn via turn/steer. turn_id=turn-123",
        )

    @pytest.mark.asyncio
    async def test_queues_message_when_steer_fails(self):
        """Steer failure should auto-queue and start queue processor."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            codex_executor=SimpleNamespace(
                has_active_turn=AsyncMock(return_value=True),
                steer_active_turn=AsyncMock(
                    return_value=SimpleNamespace(success=False, turn_id=None, error="conflict")
                ),
                record_queue_fallback=AsyncMock(),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=11)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(return_value=SimpleNamespace(id=77)),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch("src.app.ensure_queue_processor", new=AsyncMock()) as mock_ensure_queue:
            handled = await _route_codex_message_to_active_turn_or_queue(
                client=client,
                deps=deps,
                session=session,
                channel_id="C123",
                thread_ts="123.456",
                prompt="follow up",
                logger=MagicMock(),
            )

        assert handled is True
        deps.db.add_to_queue.assert_awaited_once()
        deps.codex_executor.record_queue_fallback.assert_awaited_once_with(success=True)
        mock_ensure_queue.assert_awaited_once()
        deps.db.update_command_status.assert_any_await(
            11,
            "completed",
            output="Steer failed (conflict). Auto-queued item #77.",
        )

    @pytest.mark.asyncio
    async def test_reports_queue_failure_after_steer_failure(self):
        """If queue fallback fails, command status should be marked failed and user notified."""
        session = SimpleNamespace(id=1)
        deps = SimpleNamespace(
            codex_executor=SimpleNamespace(
                has_active_turn=AsyncMock(return_value=True),
                steer_active_turn=AsyncMock(
                    return_value=SimpleNamespace(success=False, turn_id=None, error="busy")
                ),
                record_queue_fallback=AsyncMock(),
            ),
            db=SimpleNamespace(
                add_command=AsyncMock(return_value=SimpleNamespace(id=12)),
                update_command_status=AsyncMock(),
                add_to_queue=AsyncMock(side_effect=RuntimeError("db insert failed")),
            ),
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _route_codex_message_to_active_turn_or_queue(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts="123.456",
            prompt="follow up",
            logger=MagicMock(),
        )

        assert handled is True
        deps.db.update_command_status.assert_any_await(
            12,
            "failed",
            output="Steer failed and queue fallback failed. steer_error=busy queue_error=db insert failed",
            error_message="db insert failed",
        )
        deps.codex_executor.record_queue_fallback.assert_awaited_once_with(success=False)
        assert client.chat_postMessage.await_count >= 1


class TestStructuredQueuePlanRouting:
    """Tests for structured queue-plan message routing."""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_queue_plan_markers(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(db=SimpleNamespace())
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _queue_structured_plan_message(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts=None,
            prompt="normal prompt text",
            logger=MagicMock(),
        )

        assert handled is False
        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_marker_is_reported_as_structured_plan_error(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(db=SimpleNamespace())
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        handled = await _queue_structured_plan_message(
            client=client,
            deps=deps,
            session=session,
            channel_id="C123",
            thread_ts=None,
            prompt="***loop-0***",
            logger=MagicMock(),
        )

        assert handled is True
        kwargs = client.chat_postMessage.await_args.kwargs
        assert "Invalid structured queue plan" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_queues_structured_plan_message_items(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(
            db=SimpleNamespace(
                add_many_to_queue=AsyncMock(
                    return_value=[
                        SimpleNamespace(id=1, position=1),
                        SimpleNamespace(id=2, position=2),
                    ]
                ),
                get_running_queue_item=AsyncMock(return_value=None),
            )
        )
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch("src.app.contains_queue_plan_markers", return_value=True):
            with patch(
                "src.app.materialize_queue_plan_text",
                new=AsyncMock(
                    return_value=[
                        SimpleNamespace(prompt="first", working_directory_override=None),
                        SimpleNamespace(
                            prompt="second",
                            working_directory_override="/repo-worktrees/feature-x",
                        ),
                    ]
                ),
            ):
                with patch("src.app.ensure_queue_processor", new=AsyncMock()) as mock_ensure:
                    handled = await _queue_structured_plan_message(
                        client=client,
                        deps=deps,
                        session=session,
                        channel_id="C123",
                        thread_ts="123.456",
                        prompt="first\n***\nsecond",
                        logger=MagicMock(),
                    )

        assert handled is True
        deps.db.add_many_to_queue.assert_awaited_once_with(
            session_id=1,
            channel_id="C123",
            thread_ts="123.456",
            queue_entries=[("first", None), ("second", "/repo-worktrees/feature-x")],
        )
        mock_ensure.assert_awaited_once()
        assert (
            "Queued 2 item(s) from structured plan."
            in client.chat_postMessage.await_args.kwargs["text"]
        )

    @pytest.mark.asyncio
    async def test_reports_invalid_structured_plan(self):
        session = SimpleNamespace(id=1, working_directory="/repo")
        deps = SimpleNamespace(db=SimpleNamespace())
        client = SimpleNamespace(chat_postMessage=AsyncMock())

        with patch("src.app.contains_queue_plan_markers", return_value=True):
            with patch(
                "src.app.materialize_queue_plan_text",
                new=AsyncMock(side_effect=ValueError("bad marker")),
            ):
                handled = await _queue_structured_plan_message(
                    client=client,
                    deps=deps,
                    session=session,
                    channel_id="C123",
                    thread_ts=None,
                    prompt="***loop-0***",
                    logger=MagicMock(),
                )

        assert handled is True
        kwargs = client.chat_postMessage.await_args.kwargs
        assert "Failed to process structured queue plan" in kwargs["text"]
