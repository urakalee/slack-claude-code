"""Unit tests for `/usage` command behavior."""

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


@pytest.mark.asyncio
async def test_usage_codex_returns_codex_status():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo", codex_session_id="thread-1")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            update_session_claude_id=AsyncMock(),
            clear_session_claude_id=AsyncMock(),
            clear_session_codex_id=AsyncMock(),
            get_session_dirs=AsyncMock(return_value=[]),
            add_session_dir=AsyncMock(return_value=[]),
            remove_session_dir=AsyncMock(return_value=[]),
        ),
        executor=SimpleNamespace(execute=AsyncMock()),
        codex_executor=SimpleNamespace(
            get_active_turn=AsyncMock(return_value={"turn_id": "turn-123"}),
            model_list=AsyncMock(return_value={"data": [{"id": "gpt-5.3-codex"}]}),
            account_read=AsyncMock(return_value={"account": {"type": "chatgpt"}}),
            mcp_server_status_list=AsyncMock(return_value={"data": []}),
            experimental_feature_list=AsyncMock(return_value={"data": []}),
            cancel_by_scope=AsyncMock(return_value=0),
            cancel_by_channel=AsyncMock(return_value=0),
        ),
    )
    register_claude_cli_commands(app, deps)

    handler = app.handlers["/usage"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/usage",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Usage"
    assert "Codex Session Status" in kwargs["blocks"][0]["text"]["text"]
    deps.codex_executor.get_active_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_usage_claude_delegates_to_cost_command():
    app = _FakeApp()
    session = Session(model="sonnet", working_directory="/repo", claude_session_id="claude-1")
    deps = SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            update_session_claude_id=AsyncMock(),
            clear_session_claude_id=AsyncMock(),
            clear_session_codex_id=AsyncMock(),
            get_session_dirs=AsyncMock(return_value=[]),
            add_session_dir=AsyncMock(return_value=[]),
            remove_session_dir=AsyncMock(return_value=[]),
        ),
        executor=SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    session_id="claude-2",
                    output="Cost output",
                    error=None,
                    detailed_output=None,
                    duration_ms=100,
                    cost_usd=0.01,
                    success=True,
                )
            ),
            cancel_by_scope=AsyncMock(return_value=0),
            cancel_by_channel=AsyncMock(return_value=0),
        ),
        codex_executor=SimpleNamespace(
            cancel_by_scope=AsyncMock(return_value=0),
            cancel_by_channel=AsyncMock(return_value=0),
        ),
    )
    register_claude_cli_commands(app, deps)

    handler = app.handlers["/usage"]
    client = SimpleNamespace(chat_postMessage=AsyncMock(), chat_update=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/usage",
        },
        client=client,
        logger=MagicMock(),
    )

    deps.executor.execute.assert_awaited_once()
    assert deps.executor.execute.await_args.kwargs["prompt"] == "/cost"
