"""Unit tests for Codex-specific `/review` command behavior."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database.models import Session
from src.handlers.claude.claude_cli import register_claude_cli_commands


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
            update_command_status=AsyncMock(),
            add_command=AsyncMock(),
        ),
        executor=SimpleNamespace(execute=AsyncMock()),
        codex_executor=codex_executor,
    )


@pytest.mark.asyncio
async def test_review_status_uses_thread_read_for_codex_session():
    app = _FakeApp()
    session = Session(
        model="gpt-5.3-codex", working_directory="/repo", codex_session_id="thread-1"
    )
    codex_executor = SimpleNamespace(
        thread_read=AsyncMock(
            return_value={
                "thread": {
                    "id": "thread-1",
                    "name": "review-thread",
                    "status": "running",
                    "turns": [
                        {"id": "turn-1", "status": "running", "createdAt": "now"}
                    ],
                }
            }
        ),
        review_start=AsyncMock(),
    )
    deps = _deps(session, codex_executor)
    register_claude_cli_commands(app, deps)

    handler = app.handlers["/review"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "status",
            "command": "/review",
        },
        client=client,
        logger=MagicMock(),
    )

    codex_executor.thread_read.assert_awaited_once_with(
        thread_id="thread-1",
        working_directory="/repo",
        include_turns=True,
    )
    codex_executor.review_start.assert_not_called()
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex review status"


@pytest.mark.asyncio
async def test_review_start_includes_status_followup_hint():
    app = _FakeApp()
    session = Session(
        model="gpt-5.3-codex", working_directory="/repo", codex_session_id="thread-1"
    )
    codex_executor = SimpleNamespace(
        thread_read=AsyncMock(),
        review_start=AsyncMock(
            return_value={"reviewThreadId": "review-2", "turn": {"id": "turn-9"}}
        ),
    )
    deps = _deps(session, codex_executor)
    register_claude_cli_commands(app, deps)

    handler = app.handlers["/review"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/review",
        },
        client=client,
        logger=MagicMock(),
    )

    codex_executor.review_start.assert_awaited_once()
    kwargs = client.chat_postMessage.await_args.kwargs
    text = kwargs["blocks"][0]["text"]["text"]
    assert "/review status review-2" in text
