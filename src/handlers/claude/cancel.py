"""Cancel command handlers: /cancel, /c."""

from slack_bolt.async_app import AsyncApp

from ..base import CommandContext, HandlerDependencies, get_command_name, slack_command


def register_cancel_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register cancel command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    async def _handle_cancel(ctx: CommandContext, deps: HandlerDependencies) -> None:
        """Cancel all active executions in the current channel."""
        cancelled_count = await deps.executor.cancel_by_channel(ctx.channel_id)

        if cancelled_count > 0:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":no_entry: Cancelled {cancelled_count} active execution(s).",
            )
        else:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=":information_source: No active executions to cancel in this channel.",
            )

    @app.command(get_command_name("/cancel"))
    @slack_command()
    async def handle_cancel(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /cancel command - cancel active executions in channel."""
        await _handle_cancel(ctx, deps)

    @app.command(get_command_name("/c"))
    @slack_command()
    async def handle_c(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /c command - alias for /cancel."""
        await _handle_cancel(ctx, deps)
