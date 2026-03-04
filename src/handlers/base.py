"""Base infrastructure for Slack command handlers."""

import traceback
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger as LoguruLogger
from slack_sdk.web.async_client import AsyncWebClient

from src.config import config
from src.utils.formatters.command import error_message


@dataclass
class CommandContext:
    """Unified context for command execution.

    Extracts common fields from Slack command dict and provides
    typed access to the Slack client and logger.
    """

    channel_id: str
    user_id: str
    text: str
    command_name: str
    client: AsyncWebClient
    logger: LoguruLogger
    thread_ts: str | None = None  # Thread timestamp for thread-based sessions

    @classmethod
    def from_command(
        cls, command: dict, client: AsyncWebClient, logger: LoguruLogger
    ) -> "CommandContext":
        """Create context from Slack command dict.

        Parameters
        ----------
        command : dict
            The command payload from Slack.
        client : Any
            The Slack WebClient for API calls.
        logger : Any
            Logger instance for this request.

        Returns
        -------
        CommandContext
            Populated context object.

        Note
        ----
        Most slash command payloads do not include ``thread_ts``. When Slack
        provides it, handlers can scope operations to that thread session.
        """
        return cls(
            channel_id=command["channel_id"],
            user_id=command["user_id"],
            text=command.get("text", "").strip(),
            command_name=command.get("command", ""),
            client=client,
            logger=logger,
            thread_ts=(command.get("thread_ts") or "").strip() or None,
        )


@dataclass
class HandlerDependencies:
    """Container for handler dependencies.

    Provides access to shared instances across all handlers.
    """

    db: Any  # DatabaseRepository
    executor: Any  # Claude SubprocessExecutor
    codex_executor: Any = None  # Codex SubprocessExecutor


def slack_command(
    require_text: bool = False,
    usage_hint: str = "",
    max_length: int | None = None,
) -> Callable:
    """Decorator for Slack command handlers.

    Handles common boilerplate:
    - Automatic ack() call
    - CommandContext creation
    - Optional text validation
    - Input length validation
    - Exception handling with error message formatting

    Parameters
    ----------
    require_text : bool
        If True, validates that command text is not empty.
    usage_hint : str
        Usage hint shown when text validation fails.
    max_length : int
        Maximum allowed length for input text.

    Returns
    -------
    Callable
        Decorated handler function.

    Examples
    --------
    >>> @app.command("/mycommand")
    ... @slack_command(require_text=True, usage_hint="Usage: /mycommand <arg>")
    ... async def handle_mycommand(ctx: CommandContext, deps: HandlerDependencies):
    ...     await ctx.client.chat_postMessage(
    ...         channel=ctx.channel_id,
    ...         text=f"You said: {ctx.text}",
    ...     )
    """

    def decorator(func: Callable) -> Callable:
        async def wrapper(ack, command, client, logger, **kwargs):
            await ack()

            ctx = CommandContext.from_command(command, client, logger)

            if require_text and not ctx.text:
                await client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    blocks=error_message(f"Please provide input. {usage_hint}"),
                )
                return

            # Validate input length to prevent resource exhaustion
            effective_max_length = (
                max_length if max_length is not None else config.timeouts.limits.max_prompt_length
            )
            if len(ctx.text) > effective_max_length:
                await client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    blocks=error_message(
                        f"Input too long ({len(ctx.text):,} chars). "
                        f"Maximum is {effective_max_length:,} characters."
                    ),
                )
                return

            try:
                await func(ctx, **kwargs)
            except Exception as e:
                logger.error(f"Error in {ctx.command_name}: {e}\n{traceback.format_exc()}")
                await client.chat_postMessage(
                    channel=ctx.channel_id,
                    thread_ts=ctx.thread_ts,
                    blocks=error_message(str(e)),
                )

        return wrapper

    return decorator
