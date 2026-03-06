"""Queue status formatting."""

from typing import Any

from .base import escape_markdown


def queue_status(pending: list, running: Any) -> list[dict]:
    """Format queue status for /qv command."""
    running_items = running if isinstance(running, list) else ([running] if running else [])
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

    if running_items:
        running_lines = []
        for item in running_items[:10]:
            label = f"#{item.id}"
            if item.parallel_group_id:
                width = item.parallel_limit or "all"
                label += f" · parallel `{item.parallel_group_id}` (max {width})"
            running_lines.append(
                f":arrow_forward: *Running:* {label}\n> "
                f"{escape_markdown(item.prompt[:100])}{'...' if len(item.prompt) > 100 else ''}"
            )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n\n".join(running_lines)},
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
                    "text": (
                        f"*#{item.id}* (pos {item.position}"
                        + (
                            f", parallel max {item.parallel_limit or 'all'}"
                            if item.parallel_group_id
                            else ""
                        )
                        + ")\n> "
                        + f"{escape_markdown(item.prompt[:100])}"
                        + ("..." if len(item.prompt) > 100 else "")
                    ),
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


def queue_item_running(item: Any, sequence_number: str) -> list[dict]:
    """Format running queue item status."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":arrow_forward: *Processing queue item {sequence_number}:*\n> {escape_markdown(item.prompt[:200])}{'...' if len(item.prompt) > 200 else ''}",
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
