"""Unit tests for command response formatting."""

from src.utils.formatters import command as command_fmt


def test_split_blocks_by_limit_no_split() -> None:
    blocks = [{"type": "section"}, {"type": "divider"}]
    assert command_fmt._split_blocks_by_limit(blocks, 5) == [blocks]


def test_split_blocks_by_limit_splits_chunks() -> None:
    blocks = [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}, {"id": 5}]
    chunks = command_fmt._split_blocks_by_limit(blocks, 2)
    assert chunks == [[{"id": 1}, {"id": 2}], [{"id": 3}, {"id": 4}], [{"id": 5}]]


def test_command_response_with_output_and_footer() -> None:
    blocks = command_fmt.command_response(
        prompt="p" * 210,
        output="hello",
        command_id=42,
        duration_ms=1500,
        cost_usd=1.25,
    )

    assert blocks[0]["type"] == "context"
    assert blocks[0]["elements"][0]["text"].startswith("> p")
    assert blocks[0]["elements"][0]["text"].endswith("...")

    footer_context = blocks[-1]["elements"][0]["text"]
    assert ":stopwatch: 1.5s" in footer_context
    assert ":moneybag: $1.2500" in footer_context
    assert ":memo: History #42" in footer_context


def test_command_response_without_output_has_placeholder() -> None:
    blocks = command_fmt.command_response(
        prompt="run command",
        output="",
        command_id=None,
    )

    assert blocks[2]["type"] == "section"
    assert blocks[2]["text"]["text"] == "_No output_"


def test_command_response_with_file_truncates_preview_and_adds_notice(monkeypatch) -> None:
    captured_previews = []

    def fake_text_to_rich_text_blocks(text: str) -> list[dict]:
        captured_previews.append(text)
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    monkeypatch.setattr(command_fmt, "text_to_rich_text_blocks", fake_text_to_rich_text_blocks)

    output = ("A" * 250) + ". " + ("B" * 400)
    blocks, file_content, file_title = command_fmt.command_response_with_file(
        prompt="show output",
        output=output,
        command_id=7,
        duration_ms=1000,
        cost_usd=0.75,
    )

    assert file_content == output
    assert file_title == "claude_response_7.txt"
    assert captured_previews[0].endswith(".")
    assert len(captured_previews[0]) < len(output)
    assert any(block.get("text", {}).get("text") == "_... (continued in thread)_" for block in blocks)


def test_error_message_uses_sanitized_text(monkeypatch) -> None:
    monkeypatch.setattr(command_fmt, "sanitize_error", lambda _error: "sanitized")
    blocks = command_fmt.error_message("secret path")
    assert "sanitized" in blocks[0]["text"]["text"]
    assert blocks[0]["text"]["text"].startswith(":x: *Error*")


def test_should_attach_file_respects_threshold(monkeypatch) -> None:
    monkeypatch.setattr(command_fmt, "FILE_THRESHOLD", 10)
    assert command_fmt.should_attach_file("x" * 11) is True
    assert command_fmt.should_attach_file("x" * 10) is False


def test_command_response_with_tables_falls_back_to_regular_response(monkeypatch) -> None:
    sentinel = [{"type": "section", "text": {"type": "mrkdwn", "text": "regular"}}]

    monkeypatch.setattr(command_fmt, "extract_tables_from_text", lambda text: (text, []))

    called = {}

    def fake_command_response(prompt, output, command_id, duration_ms, cost_usd, is_error):
        called["args"] = (prompt, output, command_id, duration_ms, cost_usd, is_error)
        return sentinel

    monkeypatch.setattr(command_fmt, "command_response", fake_command_response)

    messages = command_fmt.command_response_with_tables(
        prompt="prompt",
        output="output",
        command_id=9,
        duration_ms=20,
        cost_usd=0.1,
        is_error=True,
    )

    assert messages == [sentinel]
    assert called["args"] == ("prompt", "output", 9, 20, 0.1, True)


def test_command_response_with_tables_splits_messages_and_adds_footer(monkeypatch) -> None:
    monkeypatch.setattr(command_fmt.config, "SLACK_MAX_BLOCKS_PER_MESSAGE", 2, raising=False)
    monkeypatch.setattr(
        command_fmt,
        "extract_tables_from_text",
        lambda _text: (
            "ignored",
            [{"type": "table", "rows": [[{"type": "raw_text", "text": "H"}]]}],
        ),
    )
    monkeypatch.setattr(
        command_fmt,
        "split_text_by_tables",
        lambda _text: [
            {"type": "text", "content": "   "},
            {"type": "text", "content": "alpha"},
            {"type": "table", "index": 0},
            {"type": "text", "content": "beta"},
        ],
    )
    monkeypatch.setattr(
        command_fmt,
        "text_to_rich_text_blocks",
        lambda text: [{"type": "rich_text", "elements": [{"type": "text", "text": text}]}],
    )

    messages = command_fmt.command_response_with_tables(
        prompt="P" * 250,
        output="unused",
        command_id=3,
        duration_ms=2500,
        cost_usd=0.25,
    )

    assert len(messages) == 4
    assert messages[0][0]["type"] == "context"
    assert messages[1][0]["type"] == "rich_text"
    assert messages[2][0]["type"] == "table"

    prompt_contexts = [
        block
        for message in messages
        for block in message
        if block["type"] == "context" and block["elements"][0]["text"].startswith("> ")
    ]
    assert len(prompt_contexts) == 1

    footer_text = messages[-1][-1]["elements"][0]["text"]
    assert ":stopwatch: 2.5s" in footer_text
    assert ":moneybag: $0.2500" in footer_text
    assert ":memo: History #3" in footer_text


def test_command_response_with_tables_returns_no_output_when_segments_empty(monkeypatch) -> None:
    monkeypatch.setattr(
        command_fmt,
        "extract_tables_from_text",
        lambda _text: ("ignored", [{"type": "table", "rows": []}]),
    )
    monkeypatch.setattr(
        command_fmt,
        "split_text_by_tables",
        lambda _text: [{"type": "text", "content": "   "}],
    )

    messages = command_fmt.command_response_with_tables(
        prompt="prompt",
        output="output",
        command_id=None,
    )

    assert messages == [[{"type": "section", "text": {"type": "mrkdwn", "text": "_No output_"}}]]
