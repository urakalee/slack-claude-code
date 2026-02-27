"""Unit tests for `/codex-config` command handlers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database.models import Session
from src.handlers.codex.config_management import register_codex_config_commands


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
        db=SimpleNamespace(get_or_create_session=AsyncMock(return_value=session)),
        codex_executor=codex_executor,
    )


@pytest.mark.asyncio
async def test_registers_codex_config_command():
    app = _FakeApp()
    deps = _deps(Session(model="gpt-5.3-codex"), codex_executor=SimpleNamespace())

    register_codex_config_commands(app, deps)

    assert "/codex-config" in app.handlers


@pytest.mark.asyncio
async def test_codex_config_summary_default_subcommand():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    codex_executor = SimpleNamespace(
        config_read=AsyncMock(
            return_value={
                "config": {
                    "model": "gpt-5.3-codex",
                    "sandbox": "workspace-write",
                    "approvalPolicy": "on-request",
                    "source": "user",
                }
            }
        ),
        config_requirements_read=AsyncMock(
            return_value={
                "requirements": [{"name": "auth", "required": True, "satisfied": True}]
            }
        ),
    )
    deps = _deps(session, codex_executor)
    register_codex_config_commands(app, deps)

    handler = app.handlers["/codex-config"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/codex-config",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex config summary"
    block_text = kwargs["blocks"][0]["text"]["text"]
    assert "requirement health: `ok`" in block_text
    assert "cwd: `/repo`" in block_text


@pytest.mark.asyncio
async def test_codex_config_requirements_lists_missing_items():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    codex_executor = SimpleNamespace(
        config_read=AsyncMock(return_value={"config": {}}),
        config_requirements_read=AsyncMock(
            return_value={
                "requirements": [
                    {
                        "name": "MCP Auth",
                        "required": True,
                        "satisfied": False,
                        "severity": "high",
                        "reason": "Missing token",
                        "remediation": "Run /mcp login",
                    }
                ]
            }
        ),
    )
    deps = _deps(session, codex_executor)
    register_codex_config_commands(app, deps)

    handler = app.handlers["/codex-config"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "requirements",
            "command": "/codex-config",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex config requirements"
    block_text = kwargs["blocks"][0]["text"]["text"]
    assert "MCP Auth" in block_text
    assert "Run /mcp login" in block_text


@pytest.mark.asyncio
async def test_codex_config_raw_redacts_and_truncates():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    payload = {
        "api_token": "shhh",
        "long_value": "x" * 250,
    }
    for i in range(45):
        payload[f"key_{i}"] = i
    codex_executor = SimpleNamespace(
        config_read=AsyncMock(return_value={"config": payload}),
        config_requirements_read=AsyncMock(return_value={"requirements": []}),
    )
    deps = _deps(session, codex_executor)
    register_codex_config_commands(app, deps)

    handler = app.handlers["/codex-config"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "raw",
            "command": "/codex-config",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex raw config"
    block_text = kwargs["blocks"][0]["text"]["text"]
    assert "***REDACTED***" in block_text
    assert "...(truncated)" in block_text
    assert "Showing first 40 keys out of" in block_text


@pytest.mark.asyncio
async def test_codex_config_rejects_non_codex_session():
    app = _FakeApp()
    session = Session(model="opus", working_directory="/repo")
    deps = _deps(session, codex_executor=SimpleNamespace())
    register_codex_config_commands(app, deps)

    handler = app.handlers["/codex-config"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/codex-config",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "/codex-config is only available for Codex sessions."


@pytest.mark.asyncio
async def test_codex_config_reports_missing_executor():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    deps = _deps(session, codex_executor=None)
    register_codex_config_commands(app, deps)

    handler = app.handlers["/codex-config"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/codex-config",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex executor is not configured."


@pytest.mark.asyncio
async def test_codex_config_models_lists_model_entries():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    codex_executor = SimpleNamespace(
        config_read=AsyncMock(return_value={"config": {}}),
        config_requirements_read=AsyncMock(return_value={"requirements": []}),
        model_list=AsyncMock(
            return_value={
                "data": [
                    {
                        "id": "gpt-5.3-codex",
                        "provider": "openai",
                        "defaultEffort": "medium",
                    }
                ]
            }
        ),
    )
    deps = _deps(session, codex_executor)
    register_codex_config_commands(app, deps)

    handler = app.handlers["/codex-config"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "models",
            "command": "/codex-config",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex models"
    assert "gpt-5.3-codex" in kwargs["blocks"][0]["text"]["text"]


@pytest.mark.asyncio
async def test_codex_config_account_redacts_sensitive_fields():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    codex_executor = SimpleNamespace(
        config_read=AsyncMock(return_value={"config": {}}),
        config_requirements_read=AsyncMock(return_value={"requirements": []}),
        account_read=AsyncMock(
            return_value={
                "account": {
                    "type": "chatgpt",
                    "email": "user@example.com",
                    "apiKey": "secret-key",
                }
            }
        ),
    )
    deps = _deps(session, codex_executor)
    register_codex_config_commands(app, deps)

    handler = app.handlers["/codex-config"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "account",
            "command": "/codex-config",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex account details"
    text = kwargs["blocks"][0]["text"]["text"]
    assert "user@example.com" in text
    assert "***REDACTED***" in text


@pytest.mark.asyncio
async def test_codex_config_features_lists_items():
    app = _FakeApp()
    session = Session(model="gpt-5.3-codex", working_directory="/repo")
    codex_executor = SimpleNamespace(
        config_read=AsyncMock(return_value={"config": {}}),
        config_requirements_read=AsyncMock(return_value={"requirements": []}),
        experimental_feature_list=AsyncMock(
            return_value={
                "data": [
                    {
                        "name": "fast_turn_merge",
                        "status": "enabled",
                        "description": "test",
                    }
                ]
            }
        ),
    )
    deps = _deps(session, codex_executor)
    register_codex_config_commands(app, deps)

    handler = app.handlers["/codex-config"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "features",
            "command": "/codex-config",
        },
        client=client,
        logger=MagicMock(),
    )

    kwargs = client.chat_postMessage.await_args.kwargs
    assert kwargs["text"] == "Codex experimental features"
    assert "fast_turn_merge" in kwargs["blocks"][0]["text"]["text"]
