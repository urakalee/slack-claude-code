"""Unit tests for `/mode` command handler."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database.models import Session
from src.handlers.claude.mode import _get_codex_display_mode, register_mode_command


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator


def _deps(session: Session) -> SimpleNamespace:
    return SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            update_session_mode=AsyncMock(),
            update_session_approval_mode=AsyncMock(),
            update_session_sandbox_mode=AsyncMock(),
            update_session_model=AsyncMock(),
        )
    )


@pytest.mark.asyncio
async def test_codex_rejects_claude_only_accept_mode_without_mutating_session():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    deps = _deps(session)
    register_mode_command(app, deps)

    handler = app.handlers["/mode"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "accept",
            "command": "/mode",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Invalid Codex mode: accept"
    deps.db.update_session_mode.assert_not_awaited()
    deps.db.update_session_approval_mode.assert_not_awaited()
    deps.db.update_session_sandbox_mode.assert_not_awaited()
    deps.db.update_session_model.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_compat_mode_updates_mode_and_approval_only():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    deps = _deps(session)
    register_mode_command(app, deps)

    handler = app.handlers["/mode"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "bypass",
            "command": "/mode",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.db.update_session_mode.assert_awaited_once()
    deps.db.update_session_approval_mode.assert_awaited_once_with("C123", None, "never")
    deps.db.update_session_sandbox_mode.assert_not_awaited()
    deps.db.update_session_model.assert_not_awaited()


def test_codex_display_mode_prefers_default_alias_for_on_request():
    assert (
        _get_codex_display_mode(permission_mode="default", approval_mode="on-request")
        == "default"
    )


def test_codex_display_mode_remains_bypass_for_never():
    assert (
        _get_codex_display_mode(permission_mode="default", approval_mode="never")
        == "bypass"
    )
