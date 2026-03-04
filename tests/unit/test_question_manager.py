"""Unit tests for question parsing and validation behavior."""

from src.question.manager import QuestionManager


def test_parse_ask_user_question_input_ignores_malformed_entries():
    """Malformed question/option payload entries should be ignored safely."""
    parsed = QuestionManager.parse_ask_user_question_input(
        {
            "questions": [
                "invalid-question-entry",
                {
                    "id": 123,
                    "question": "Pick one",
                    "header": "Choice",
                    "options": [
                        "invalid-option-entry",
                        {"label": "A", "description": 5},
                    ],
                    "multiSelect": "yes",
                },
                {
                    "question": "Second question",
                    "header": "Second",
                    "options": "not-a-list",
                },
            ]
        }
    )

    assert len(parsed) == 2

    first = parsed[0]
    assert first.id == "123"
    assert first.question == "Pick one"
    assert first.header == "Choice"
    assert first.multi_select is True
    assert len(first.options) == 1
    assert first.options[0].label == "A"
    assert first.options[0].description == "5"

    second = parsed[1]
    assert second.question == "Second question"
    assert second.options == []
    assert second.multi_select is False


def test_parse_ask_user_question_input_handles_non_list_questions():
    """Non-list questions payloads should return an empty question list."""
    parsed = QuestionManager.parse_ask_user_question_input({"questions": "invalid"})
    assert parsed == []
