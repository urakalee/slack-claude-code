"""Unit tests for `/codex-thread` command handlers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database.models import Session
from src.handlers.codex.thread_management import register_codex_thread_commands


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
            update_session_codex_id=AsyncMock(),
        ),
        codex_executor=codex_executor,
    )


@pytest.mark.asyncio
async def test_registers_codex_thread_command():
    app = _FakeApp()
    deps = _deps(Session(model="gpt-5.3-codex"), codex_executor=SimpleNamespace())

    register_codex_thread_commands(app, deps)

    assert "/codex-thread" in app.handlers


@pytest.mark.asyncio
async def test_codex_thread_list_parses_limit_and_archived():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    codex_executor = SimpleNamespace(
        thread_list=AsyncMock(
            return_value={
                "data": [
                    {
                        "id": "t1",
                        "name": "one",
                        "status": "ready",
                        "updatedAt": "now",
                        "turnCount": 2,
                    }
                ]
            }
        ),
        thread_read=AsyncMock(),
    )
    deps = _deps(session, codex_executor)
    register_codex_thread_commands(app, deps)

    handler = app.handlers["/codex-thread"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "list 5 archived",
            "command": "/codex-thread",
        },
        client=client,
        logger=MagicMock(),
    )

    codex_executor.thread_list.assert_awaited_once_with("/repo", limit=5, archived=True)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex thread list"
    assert "Recent threads" in kwargs["blocks"][0]["text"]["text"]


@pytest.mark.asyncio
async def test_codex_thread_read_supports_turn_limit():
    app = _FakeApp()
    session = Session(
        model="gpt-5.3-codex",
        working_directory="/repo",
        codex_session_id="thread-current",
    )
    codex_executor = SimpleNamespace(
        thread_list=AsyncMock(return_value={"data": []}),
        thread_read=AsyncMock(
            return_value={
                "thread": {
                    "id": "thread-current",
                    "name": "main",
                    "status": "ready",
                    "preview": "preview",
                    "turns": [
                        {"id": "turn1", "status": "done", "createdAt": "1"},
                        {"id": "turn2", "status": "done", "createdAt": "2"},
                        {"id": "turn3", "status": "done", "createdAt": "3"},
                    ],
                }
            }
        ),
    )
    deps = _deps(session, codex_executor)
    register_codex_thread_commands(app, deps)

    handler = app.handlers["/codex-thread"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "read current 2",
            "command": "/codex-thread",
        },
        client=client,
        logger=MagicMock(),
    )

    codex_executor.thread_read.assert_awaited_once_with("thread-current", "/repo", True)
    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Thread thread-current"
    block_text = kwargs["blocks"][0]["text"]["text"]
    assert "Recent Turns (last 2)" in block_text
    assert "turn2" in block_text
    assert "turn3" in block_text
