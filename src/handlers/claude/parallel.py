"""Job status and cancellation command handlers: /st, /cc."""

from slack_bolt.async_app import AsyncApp

from src.utils.formatting import SlackFormatter

from ..base import CommandContext, HandlerDependencies, get_command_name, slack_command


def register_parallel_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register job status and cancellation command handlers.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command(get_command_name("/st"))
    @slack_command()
    async def handle_status(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /st command - show active jobs."""
        jobs = await deps.db.get_active_jobs(ctx.channel_id)

        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=SlackFormatter.job_status_list(jobs),
        )

    @app.command(get_command_name("/cc"))
    @slack_command()
    async def handle_cancel(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /cc [job_id] command - cancel jobs."""
        if ctx.text:
            # Cancel specific job
            try:
                job_id = int(ctx.text)
                cancelled = await deps.db.cancel_job(job_id)
                if cancelled:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        text=f":no_entry: Job #{job_id} cancelled.",
                    )
                else:
                    await ctx.client.chat_postMessage(
                        channel=ctx.channel_id,
                        text=f"Job #{job_id} not found or already completed.",
                    )
            except ValueError:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    blocks=SlackFormatter.error_message("Invalid job ID. Usage: /cc [job_id]"),
                )
        else:
            # Cancel all active jobs in channel
            jobs = await deps.db.get_active_jobs(ctx.channel_id)
            cancelled_count = 0
            for job in jobs:
                if await deps.db.cancel_job(job.id):
                    cancelled_count += 1

            # Also cancel any active executions
            executor_cancelled = await deps.executor.cancel_all()

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f":no_entry: Cancelled {cancelled_count} job(s) and "
                f"{executor_cancelled} active execution(s).",
            )
