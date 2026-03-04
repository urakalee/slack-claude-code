"""Unit tests for handler base infrastructure."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.handlers.base import CommandContext, HandlerDependencies, slack_command


class TestCommandContext:
    """Tests for CommandContext dataclass."""

    def test_from_command_basic(self):
        """from_command extracts fields correctly."""
        command = {
            "channel_id": "C123ABC",
            "user_id": "U456DEF",
            "text": "  hello world  ",
            "command": "/mycommand",
        }
        client = MagicMock()
        logger = MagicMock()

        ctx = CommandContext.from_command(command, client, logger)

        assert ctx.channel_id == "C123ABC"
        assert ctx.user_id == "U456DEF"
        assert ctx.text == "hello world"  # Stripped
        assert ctx.command_name == "/mycommand"
        assert ctx.client is client
        assert ctx.logger is logger

    def test_from_command_empty_text(self):
        """from_command handles missing text."""
        command = {
            "channel_id": "C123ABC",
            "user_id": "U456DEF",
            # No text field
        }
        client = MagicMock()
        logger = MagicMock()

        ctx = CommandContext.from_command(command, client, logger)

        assert ctx.text == ""

    def test_from_command_missing_command(self):
        """from_command handles missing command field."""
        command = {
            "channel_id": "C123ABC",
            "user_id": "U456DEF",
            "text": "test",
            # No command field
        }
        client = MagicMock()
        logger = MagicMock()

        ctx = CommandContext.from_command(command, client, logger)

        assert ctx.command_name == ""

    def test_from_command_normalizes_blank_thread_ts(self):
        """Blank thread timestamp should normalize to None."""
        command = {
            "channel_id": "C123ABC",
            "user_id": "U456DEF",
            "text": "test",
            "thread_ts": "   ",
        }
        client = MagicMock()
        logger = MagicMock()

        ctx = CommandContext.from_command(command, client, logger)
        assert ctx.thread_ts is None


class TestHandlerDependencies:
    """Tests for HandlerDependencies dataclass."""

    def test_basic_dependencies(self):
        """HandlerDependencies stores db and executor."""
        db = MagicMock()
        executor = MagicMock()

        deps = HandlerDependencies(db=db, executor=executor)

        assert deps.db is db
        assert deps.executor is executor


class TestSlackCommandDecorator:
    """Tests for slack_command decorator."""

    @pytest.mark.asyncio
    async def test_decorator_calls_ack(self):
        """Decorator calls ack() automatically."""
        ack = AsyncMock()
        client = AsyncMock()
        logger = MagicMock()
        command = {
            "channel_id": "C123",
            "user_id": "U123",
            "text": "test",
            "command": "/test",
        }

        @slack_command()
        async def handler(ctx):
            pass

        await handler(ack=ack, command=command, client=client, logger=logger)

        ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_decorator_creates_context(self):
        """Decorator creates CommandContext and passes to handler."""
        ack = AsyncMock()
        client = AsyncMock()
        logger = MagicMock()
        command = {
            "channel_id": "C123",
            "user_id": "U123",
            "text": "hello",
            "command": "/test",
        }
        received_ctx = None

        @slack_command()
        async def handler(ctx):
            nonlocal received_ctx
            received_ctx = ctx

        await handler(ack=ack, command=command, client=client, logger=logger)

        assert received_ctx is not None
        assert received_ctx.channel_id == "C123"
        assert received_ctx.user_id == "U123"
        assert received_ctx.text == "hello"

    @pytest.mark.asyncio
    async def test_require_text_validation_fails(self):
        """require_text=True rejects empty text."""
        ack = AsyncMock()
        client = AsyncMock()
        logger = MagicMock()
        command = {
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/test",
        }
        handler_called = False

        @slack_command(require_text=True, usage_hint="Usage: /test <arg>")
        async def handler(ctx):
            nonlocal handler_called
            handler_called = True

        await handler(ack=ack, command=command, client=client, logger=logger)

        # Handler should not be called
        assert handler_called is False

        # Error message should be sent
        client.chat_postMessage.assert_called_once()
        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C123"
        # blocks should contain error message

    @pytest.mark.asyncio
    async def test_require_text_validation_preserves_thread_scope(self):
        """Validation errors should reply in-thread when thread_ts is present."""
        ack = AsyncMock()
        client = AsyncMock()
        logger = MagicMock()
        command = {
            "channel_id": "C123",
            "user_id": "U123",
            "text": "",
            "command": "/test",
            "thread_ts": "123.456",
        }

        @slack_command(require_text=True, usage_hint="Usage: /test <arg>")
        async def handler(ctx):
            raise AssertionError("handler should not run")

        await handler(ack=ack, command=command, client=client, logger=logger)

        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["thread_ts"] == "123.456"

    @pytest.mark.asyncio
    async def test_require_text_validation_passes(self):
        """require_text=True allows non-empty text."""
        ack = AsyncMock()
        client = AsyncMock()
        logger = MagicMock()
        command = {
            "channel_id": "C123",
            "user_id": "U123",
            "text": "valid input",
            "command": "/test",
        }
        handler_called = False

        @slack_command(require_text=True)
        async def handler(ctx):
            nonlocal handler_called
            handler_called = True

        await handler(ack=ack, command=command, client=client, logger=logger)

        assert handler_called is True

    @pytest.mark.asyncio
    async def test_exception_handling(self):
        """Decorator catches exceptions and posts error message."""
        ack = AsyncMock()
        client = AsyncMock()
        logger = MagicMock()
        command = {
            "channel_id": "C123",
            "user_id": "U123",
            "text": "test",
            "command": "/test",
        }

        @slack_command()
        async def handler(ctx):
            raise ValueError("Something went wrong")

        await handler(ack=ack, command=command, client=client, logger=logger)

        # Error should be logged
        logger.error.assert_called_once()
        assert "Something went wrong" in str(logger.error.call_args)

        # Error message should be sent to channel
        client.chat_postMessage.assert_called_once()
        call_kwargs = client.chat_postMessage.call_args.kwargs
        assert call_kwargs["channel"] == "C123"

    @pytest.mark.asyncio
    async def test_passes_extra_kwargs(self):
        """Decorator passes through extra kwargs to handler."""
        ack = AsyncMock()
        client = AsyncMock()
        logger = MagicMock()
        command = {
            "channel_id": "C123",
            "user_id": "U123",
            "text": "test",
            "command": "/test",
        }
        received_kwargs = {}

        @slack_command()
        async def handler(ctx, deps=None, extra=None):
            received_kwargs["deps"] = deps
            received_kwargs["extra"] = extra

        mock_deps = MagicMock()
        await handler(
            ack=ack,
            command=command,
            client=client,
            logger=logger,
            deps=mock_deps,
            extra="value",
        )

        assert received_kwargs["deps"] is mock_deps
        assert received_kwargs["extra"] == "value"
