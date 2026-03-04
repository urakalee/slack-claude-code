"""Unit tests for AskUserQuestion Slack UI rendering."""

from src.question.manager import PendingQuestion, Question, QuestionOption
from src.question.slack_ui import build_question_blocks


def test_single_select_question_renders_all_options_across_multiple_action_blocks():
    """Single-select options should not be dropped when more than five are provided."""
    options = [QuestionOption(label=f"Option {i}") for i in range(1, 8)]
    pending = PendingQuestion(
        question_id="q123",
        session_id="s1",
        channel_id="C123",
        thread_ts=None,
        tool_use_id="tool1",
        questions=[
            Question(
                id="q1",
                question="Pick one",
                header="Choice",
                options=options,
                multi_select=False,
            )
        ],
    )

    blocks = build_question_blocks(pending)
    action_blocks = [
        block
        for block in blocks
        if block.get("type") == "actions"
        and str(block.get("block_id", "")).startswith("question_actions_q123_0_")
    ]
    assert len(action_blocks) == 2

    select_labels = []
    has_other_button = False
    for block in action_blocks:
        for button in block.get("elements", []):
            action_id = button.get("action_id", "")
            label = button.get("text", {}).get("text")
            if str(action_id).startswith("question_select_0_"):
                select_labels.append(label)
            if action_id == "question_custom_0":
                has_other_button = True

    assert select_labels == [f"Option {i}" for i in range(1, 8)]
    assert has_other_button is True


def test_multi_select_question_shows_overflow_warning_when_options_exceed_limit():
    """Multi-select blocks should show a warning when Slack option limits truncate choices."""
    options = [QuestionOption(label=f"Option {i}") for i in range(1, 13)]
    pending = PendingQuestion(
        question_id="q456",
        session_id="s1",
        channel_id="C123",
        thread_ts=None,
        tool_use_id="tool1",
        questions=[
            Question(
                id="q1",
                question="Select all that apply",
                header="Choice",
                options=options,
                multi_select=True,
            )
        ],
    )

    blocks = build_question_blocks(pending)
    checkbox_block = next(
        block
        for block in blocks
        if block.get("type") == "section"
        and str(block.get("block_id", "")).startswith("question_checkbox_q456_0")
    )
    checkbox_options = checkbox_block["accessory"]["options"]
    assert len(checkbox_options) == 10

    overflow_context = next(
        block
        for block in blocks
        if block.get("type") == "context"
        and "additional option(s) omitted" in block.get("elements", [{}])[0].get("text", "")
    )
    assert "Showing first 10 options" in overflow_context["elements"][0]["text"]
