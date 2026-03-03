"""Queue status formatting."""

from typing import Any

from .base import escape_markdown


def queue_status(pending: list, running: Any) -> list[dict]:
    """Format queue status for /qc view command."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":inbox_tray: Command Queue",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    if running:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":arrow_forward: *Running:* #{running.id}\n> {escape_markdown(running.prompt[:100])}{'...' if len(running.prompt) > 100 else ''}",
                },
            }
        )
        blocks.append({"type": "divider"})

    if not pending:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_Queue is empty_"},
            }
        )
    else:
        for item in pending[:10]:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*#{item.id}* (pos {item.position})\n> {escape_markdown(item.prompt[:100])}{'...' if len(item.prompt) > 100 else ''}",
                    },
                }
            )

        if len(pending) > 10:
            blocks.append(
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"_... and {len(pending) - 10} more_"}],
                }
            )

    return blocks


def queue_item_running(item: Any) -> list[dict]:
    """Format running queue item status."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":arrow_forward: *Processing Queue Item #{item.id}*\n> {escape_markdown(item.prompt[:200])}{'...' if len(item.prompt) > 200 else ''}",
            },
        },
    ]


def queue_item_complete(item: Any, result: Any) -> list[dict]:
    """Format completed queue item."""
    status = ":heavy_check_mark:" if result.success else ":x:"
    output = result.output or result.error or "No output"
    if len(output) > 2500:
        output = output[:2500] + "\n\n... (truncated)"

    return [
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"{status} Queue Item #{item.id}"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"> {escape_markdown(item.prompt[:100])}{'...' if len(item.prompt) > 100 else ''}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": output},
        },
    ]
