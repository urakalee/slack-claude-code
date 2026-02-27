"""Unit tests for Codex session command handlers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database.models import Session
from src.handlers.codex.session_management import register_codex_session_commands


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator


def _deps(session: Session, codex_executor) -> SimpleNamespace:
    return SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            get_sessions_by_channel=AsyncMock(return_value=[]),
            delete_inactive_sessions=AsyncMock(return_value=0),
            clear_session_codex_id=AsyncMock(),
        ),
        codex_executor=codex_executor,
    )


@pytest.mark.asyncio
async def test_registers_codex_metrics_command():
    app = _FakeApp()
    deps = _deps(Session(model="gpt-5.3-codex"), codex_executor=SimpleNamespace())

    register_codex_session_commands(app, deps)

    assert "/codex-metrics" in app.handlers


@pytest.mark.asyncio
async def test_codex_metrics_summary_renders_snapshot():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    codex_executor = SimpleNamespace(
        get_metrics_snapshot=AsyncMock(
            return_value={
                "active_turns": 1,
                "turn_start_registered": 4,
                "turn_state_cleared": 3,
                "steer_successes": 5,
                "steer_requests": 10,
                "steer_success_rate": 0.5,
                "steer_failures": 5,
                "steer_timeouts": 1,
                "interrupt_successes": 2,
                "interrupt_requests": 4,
                "interrupt_success_rate": 0.5,
                "interrupt_failures": 2,
                "interrupt_timeouts": 1,
                "queue_fallback_successes": 2,
                "queue_fallback_attempts": 4,
                "queue_fallback_success_rate": 0.5,
                "queue_fallback_failures": 2,
            }
        ),
        reset_metrics=AsyncMock(),
    )
    deps = _deps(session, codex_executor)
    register_codex_session_commands(app, deps)

    handler = app.handlers["/codex-metrics"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/codex-metrics",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex metrics"
    block_text = kwargs["blocks"][0]["text"]["text"]
    assert "steer:" in block_text
    assert "queue fallback:" in block_text


@pytest.mark.asyncio
async def test_codex_metrics_reset():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    codex_executor = SimpleNamespace(
        get_metrics_snapshot=AsyncMock(return_value={}),
        reset_metrics=AsyncMock(),
    )
    deps = _deps(session, codex_executor)
    register_codex_session_commands(app, deps)

    handler = app.handlers["/codex-metrics"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "reset",
            "command": "/codex-metrics",
        },
        client=client,
        logger=MagicMock(),
    )

    codex_executor.reset_metrics.assert_awaited_once()
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex metrics reset"


@pytest.mark.asyncio
async def test_codex_metrics_rejects_non_codex_session():
    app = _FakeApp()
    session = Session(model="opus", working_directory="/repo")
    deps = _deps(session, codex_executor=SimpleNamespace())
    register_codex_session_commands(app, deps)

    handler = app.handlers["/codex-metrics"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/codex-metrics",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "/codex-metrics is only available for Codex sessions."
