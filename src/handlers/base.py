"""Base infrastructure for Slack command handlers."""

import re
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger as LoguruLogger
from slack_sdk.web.async_client import AsyncWebClient

from src.config import config
from src.utils.formatting import SlackFormatter


def get_command_name(base_command: str) -> str:
    """
    Apply configured command suffix to a base command name.

    Generates the final Slack command name by appending the configured
    COMMAND_SUFFIX from settings. If no suffix is configured, returns
    the original command unchanged.

    Parameters
    ----------
    base_command : str
        The base command name (e.g., "/ls", "/cd", "/model").
        Must start with "/" and follow Slack command naming rules.

    Returns
    -------
    str
        The final command name with suffix applied (e.g., "/ls-cc").
        Returns the original command if COMMAND_SUFFIX is empty.

    Raises
    ------
    ValueError
        If the resulting command name violates Slack's command format rules:
        - Must match pattern: /[a-z0-9_-]{1,32}
        - Total length must not exceed 32 characters (including "/")
        - Only lowercase letters, numbers, underscore, and hyphen allowed

    Examples
    --------
    >>> # With COMMAND_SUFFIX = "cc"
    >>> get_command_name("/ls")
    '/ls-cc'
    >>> get_command_name("/model")
    '/model-cc'

    >>> # With COMMAND_SUFFIX = ""
    >>> get_command_name("/ls")
    '/ls'

    Notes
    -----
    Slack command format specification:
    https://api.slack.com/interactivity/slash-commands#creating_commands
    """
    suffix = config.COMMAND_SUFFIX.strip()

    # No suffix configured - return original command
    if not suffix:
        return base_command

    # Construct new command with suffix
    new_command = f"{base_command}-{suffix}"

    # Validate against Slack command format: /[a-z0-9_-]{1,32}
    if not re.match(r"^/[a-z0-9_-]{1,32}$", new_command):
        error_msg = (
            f"Invalid Slack command format: '{new_command}'. "
            f"Commands must match pattern /[a-z0-9_-]{{1,32}} "
            f"(lowercase letters, numbers, underscore, hyphen only; max 32 chars total)."
        )
        LoguruLogger.error(error_msg)
        raise ValueError(error_msg)

    return new_command


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
    def from_command(cls, command: dict, client: AsyncWebClient, logger: LoguruLogger) -> "CommandContext":
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
        Slash commands don't include thread_ts, so it will always be None for commands.
        Thread-based sessions are only available when handling message events.
        """
        return cls(
            channel_id=command["channel_id"],
            user_id=command["user_id"],
            text=command.get("text", "").strip(),
            command_name=command.get("command", ""),
            client=client,
            logger=logger,
            thread_ts=None,  # Commands always operate on channel-level sessions
        )


@dataclass
class HandlerDependencies:
    """Container for handler dependencies.

    Provides access to shared instances across all handlers.
    """

    db: Any  # DatabaseRepository
    executor: Any  # Claude SubprocessExecutor
    codex_executor: Any = None  # Codex SubprocessExecutor
    pty_executor: Any = None  # PTYExecutor for persistent Codex sessions


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
                    blocks=SlackFormatter.error_message(f"Please provide input. {usage_hint}"),
                )
                return

            # Validate input length to prevent resource exhaustion
            effective_max_length = max_length if max_length is not None else config.timeouts.limits.max_prompt_length
            if len(ctx.text) > effective_max_length:
                await client.chat_postMessage(
                    channel=ctx.channel_id,
                    blocks=SlackFormatter.error_message(
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
                    blocks=SlackFormatter.error_message(str(e)),
                )

        return wrapper

    return decorator
