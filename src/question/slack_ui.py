"""Slack UI components for AskUserQuestion tool.

Builds interactive Block Kit elements for displaying questions
and capturing user responses.
"""

import json
from typing import TYPE_CHECKING

from src.utils.formatters.base import text_to_rich_text_blocks

if TYPE_CHECKING:
    from .manager import PendingQuestion, Question


def build_question_blocks(pending: "PendingQuestion", context_text: str = "") -> list[dict]:
    """Build Slack blocks for displaying question(s).

    Args:
        pending: The pending question to display
        context_text: Optional context text from Claude explaining why they're asking

    Returns:
        List of Slack Block Kit blocks
    """
    blocks = []

    # Header
    blocks.append(
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":question: Assistant has a question",
                "emoji": True,
            },
        }
    )

    blocks.append({"type": "divider"})

    # Add context text if provided (rich_text for full-width display)
    if context_text and context_text.strip():
        blocks.extend(text_to_rich_text_blocks(context_text.strip()))
        blocks.append({"type": "divider"})

    # Build blocks for each question
    for i, question in enumerate(pending.questions):
        # Question text (rich_text for full-width display)
        question_text = f"**{question.header}**\n{question.question}"
        blocks.extend(text_to_rich_text_blocks(question_text))

        # Build action buttons for options
        if question.multi_select:
            # For multi-select, use checkboxes (returns list of blocks)
            checkbox_blocks = _build_checkbox_block(pending.question_id, i, question)
            blocks.extend(checkbox_blocks)
        else:
            # For single-select, use button blocks (includes "Other" button).
            blocks.extend(_build_button_blocks(pending.question_id, i, question))

        # Add option descriptions if any (rich_text for full-width display)
        descriptions = []
        for opt in question.options:
            if opt.description:
                descriptions.append(f"- **{opt.label}**: {opt.description}")

        if descriptions:
            descriptions_text = "\n".join(descriptions)
            blocks.extend(text_to_rich_text_blocks(descriptions_text))

        # Add spacing between questions
        if i < len(pending.questions) - 1:
            blocks.append({"type": "divider"})

    # Always add a single confirm button at the bottom.
    # For single-question single-select, individual button clicks auto-resolve
    # (no confirm needed). For multi-question or multi-select, users must click
    # confirm after making all selections.
    needs_confirm = len(pending.questions) > 1 or any(q.multi_select for q in pending.questions)
    if needs_confirm:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"question_submit_{pending.question_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Confirm",
                            "emoji": True,
                        },
                        "style": "primary",
                        "action_id": "question_confirm_submit",
                        "value": pending.question_id,
                    }
                ],
            }
        )

    return blocks


def _build_button_blocks(
    question_id: str,
    question_index: int,
    question: "Question",
) -> list[dict]:
    """Build a button block for single-select question.

    Args:
        question_id: The question ID
        question_index: Index of this question
        question: The question object

    Returns:
        List of Slack actions blocks with buttons
    """
    buttons = []
    for opt in question.options:
        # Value encodes question_id, question_index, and selected label
        value = json.dumps(
            {
                "q": question_id,
                "i": question_index,
                "l": opt.label,
            }
        )

        buttons.append(
            {
                "type": "button",
                "text": {
                    "type": "plain_text",
                    "text": opt.label[:75],  # Slack button text limit
                    "emoji": True,
                },
                "action_id": f"question_select_{question_index}_{len(buttons)}",
                "value": value,
            }
        )

    # Add "Other" button for custom answer.
    other_value = json.dumps({"q": question_id, "i": question_index})
    buttons.append(
        {
            "type": "button",
            "text": {
                "type": "plain_text",
                "text": "Other...",
                "emoji": True,
            },
            "action_id": f"question_custom_{question_index}",
            "value": other_value,
        }
    )

    # Slack limit: 5 elements per actions block.
    button_blocks: list[dict] = []
    max_buttons_per_block = 5
    for idx in range(0, len(buttons), max_buttons_per_block):
        button_blocks.append(
            {
                "type": "actions",
                "block_id": f"question_actions_{question_id}_{question_index}_{idx // 5}",
                "elements": buttons[idx : idx + max_buttons_per_block],
            }
        )
    return button_blocks


def _build_checkbox_block(
    question_id: str,
    question_index: int,
    question: "Question",
) -> list[dict]:
    """Build checkbox blocks for multi-select question.

    Args:
        question_id: The question ID
        question_index: Index of this question
        question: The question object

    Returns:
        List of Slack blocks: section with checkboxes, plus "Other" button
    """
    options = []
    max_options = 10
    for opt in question.options:
        option_dict = {
            "text": {
                "type": "mrkdwn",
                "text": f"*{opt.label}*",
            },
            "value": opt.label,
        }
        # Only add description if it's a non-empty string
        if opt.description:
            option_dict["description"] = {
                "type": "mrkdwn",
                "text": opt.description[:75],
            }
        options.append(option_dict)

    blocks = [
        {
            "type": "section",
            "block_id": f"question_checkbox_{question_id}_{question_index}",
            "text": {
                "type": "mrkdwn",
                "text": "_Select all that apply:_",
            },
            "accessory": {
                "type": "checkboxes",
                "action_id": f"question_multiselect_{question_index}",
                "options": options[:max_options],  # Slack limit: 10 options
            },
        },
        # Add "Other" button for custom answer
        {
            "type": "actions",
            "block_id": f"question_other_{question_id}_{question_index}",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": "Other...",
                        "emoji": True,
                    },
                    "action_id": f"question_custom_{question_index}",
                    "value": json.dumps({"q": question_id, "i": question_index}),
                }
            ],
        },
    ]

    hidden_option_count = len(options) - max_options
    if hidden_option_count > 0:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"_Showing first {max_options} options due to Slack limits. "
                            f"{hidden_option_count} additional option(s) omitted; "
                            "use `Other...` if needed._"
                        ),
                    }
                ],
            }
        )

    return blocks


def build_question_result_blocks(
    pending: "PendingQuestion",
    user_id: str,
) -> list[dict]:
    """Build blocks showing the answered question.

    Args:
        pending: The answered pending question
        user_id: User who answered

    Returns:
        List of Slack Block Kit blocks
    """
    blocks = []

    # Header showing answered
    blocks.append(
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": ":heavy_check_mark: Question answered",
                "emoji": True,
            },
        }
    )

    blocks.append({"type": "divider"})

    # Show each question and answer (rich_text for full-width display)
    for i, question in enumerate(pending.questions):
        selected = pending.answers.get(i, ["(no answer)"])
        answer_text = ", ".join(selected)

        result_text = f"**{question.header}**\n*{question.question}*\n\n**Answer:** {answer_text}"
        blocks.extend(text_to_rich_text_blocks(result_text))

    return blocks


def build_custom_answer_modal(
    question_id: str,
    question_index: int,
    question_header: str = "Your Answer",
) -> dict:
    """Build a modal for custom answer input.

    Args:
        question_id: The question ID
        question_index: Index of the specific question being answered
        question_header: Header/label for the question (for display)

    Returns:
        Slack modal view
    """
    # Store both question_id and question_index in private_metadata
    private_metadata = json.dumps({"q": question_id, "i": question_index})

    return {
        "type": "modal",
        "callback_id": "question_custom_submit",
        "private_metadata": private_metadata,
        "title": {
            "type": "plain_text",
            "text": "Custom Answer",
        },
        "submit": {
            "type": "plain_text",
            "text": "Submit",
        },
        "close": {
            "type": "plain_text",
            "text": "Cancel",
        },
        "blocks": [
            {
                "type": "input",
                "block_id": "custom_answer_block",
                "element": {
                    "type": "plain_text_input",
                    "action_id": "custom_answer_input",
                    "multiline": True,
                    "placeholder": {
                        "type": "plain_text",
                        "text": "Type your answer here...",
                    },
                },
                "label": {
                    "type": "plain_text",
                    "text": question_header[:24],  # Slack label limit
                },
            },
        ],
    }
