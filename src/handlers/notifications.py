"""Notifications command handler: /notifications."""

from slack_bolt.async_app import AsyncApp

from ..utils.formatting import SlackFormatter
from .base import CommandContext, HandlerDependencies, get_command_name, slack_command


def register_notifications_command(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register notifications command handler.

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command(get_command_name("/notifications"))
    @slack_command(
        require_text=False,
        usage_hint="Usage: /notifications [on|off|completion on|off|permission on|off]",
    )
    async def handle_notifications(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /notifications command - view or configure channel notifications."""
        text = ctx.text.strip().lower() if ctx.text else ""
        parts = text.split() if text else []

        # Get current settings
        settings = await deps.db.get_notification_settings(ctx.channel_id)

        # No argument: show current settings
        if not parts:
            completion_status = "✅ on" if settings.notify_on_completion else "❌ off"
            permission_status = "✅ on" if settings.notify_on_permission else "❌ off"

            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Notification settings: completion={completion_status}, permission={permission_status}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": (
                                "*🔔 Notification Settings*\n\n"
                                f"• Completion alerts: {completion_status}\n"
                                f"• Permission alerts: {permission_status}\n\n"
                                "_Channel notifications trigger Slack sounds and unread badges._"
                            ),
                        },
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": (
                                    "Commands: `/notifications on` • `/notifications off` • "
                                    "`/notifications completion on|off` • `/notifications permission on|off`"
                                ),
                            }
                        ],
                    },
                ],
            )
            return

        subcommand = parts[0]
        value = parts[1] if len(parts) > 1 else ""

        if subcommand == "on":
            # Enable all notifications
            await deps.db.update_notification_settings(
                ctx.channel_id,
                notify_on_completion=True,
                notify_on_permission=True,
            )
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text="Notifications enabled",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "🔔 *Notifications enabled*\n\nYou'll receive channel alerts when Claude finishes or needs permission.",
                        },
                    }
                ],
            )

        elif subcommand == "off":
            # Disable all notifications
            await deps.db.update_notification_settings(
                ctx.channel_id,
                notify_on_completion=False,
                notify_on_permission=False,
            )
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text="Notifications disabled",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "🔕 *Notifications disabled*\n\nYou won't receive channel-level alerts.",
                        },
                    }
                ],
            )

        elif subcommand == "completion":
            if value not in ("on", "off"):
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text="Usage: /notifications completion on|off",
                    blocks=SlackFormatter.error_message(
                        "Usage: `/notifications completion on` or `/notifications completion off`"
                    ),
                )
                return

            enabled = value == "on"
            await deps.db.update_notification_settings(
                ctx.channel_id,
                notify_on_completion=enabled,
                notify_on_permission=settings.notify_on_permission,
            )
            status = "✅ enabled" if enabled else "❌ disabled"
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Completion notifications {status}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"Completion notifications {status}.",
                        },
                    }
                ],
            )

        elif subcommand == "permission":
            if value not in ("on", "off"):
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    text="Usage: /notifications permission on|off",
                    blocks=SlackFormatter.error_message(
                        "Usage: `/notifications permission on` or `/notifications permission off`"
                    ),
                )
                return

            enabled = value == "on"
            await deps.db.update_notification_settings(
                ctx.channel_id,
                notify_on_completion=settings.notify_on_completion,
                notify_on_permission=enabled,
            )
            status = "✅ enabled" if enabled else "❌ disabled"
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Permission notifications {status}",
                blocks=[
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"Permission notifications {status}.",
                        },
                    }
                ],
            )

        else:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                text=f"Unknown subcommand: {subcommand}",
                blocks=SlackFormatter.error_message(
                    f"Unknown subcommand: `{subcommand}`\n\n"
                    "Valid commands:\n"
                    "• `/notifications` - Show settings\n"
                    "• `/notifications on|off` - Enable/disable all\n"
                    "• `/notifications completion on|off`\n"
                    "• `/notifications permission on|off`"
                ),
            )
