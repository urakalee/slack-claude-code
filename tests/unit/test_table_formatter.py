"""Unit tests for markdown table formatting utilities."""

from src.utils.formatters.table import (
    _is_table_separator,
    _make_cell,
    _split_row,
    _strip_inline_markdown,
    extract_tables_from_text,
    parse_markdown_table,
    split_text_by_tables,
)


def test_is_table_separator_accepts_alignment_markers() -> None:
    assert _is_table_separator("| --- | :---: | ---: |") is True


def test_is_table_separator_rejects_invalid_lines() -> None:
    assert _is_table_separator("no pipes here") is False
    assert _is_table_separator("| -- | --- |") is False


def test_split_row_handles_escaped_pipes_backticks_and_code_spans() -> None:
    line = r"| a\|b | `x|y` | plain \`tick\` | slash\\pipe |"
    assert _split_row(line) == ["a|b", "`x|y`", "plain `tick`", "slash\\pipe"]


def test_strip_inline_markdown_removes_supported_inline_syntax() -> None:
    text = "**bold** __strong__ *italic* _em_ ~~strike~~ [label](https://example.com) `code`"
    assert _strip_inline_markdown(text) == "bold strong italic em strike label code"


def test_make_cell_uses_single_space_for_empty_content() -> None:
    assert _make_cell("") == {"type": "raw_text", "text": " "}


def test_parse_markdown_table_rejects_invalid_or_incomplete_input() -> None:
    assert parse_markdown_table("| only one line |") is None
    assert parse_markdown_table("| --- | --- |\n| data | row |") is None


def test_parse_markdown_table_normalizes_row_width() -> None:
    table_text = "\n".join(
        [
            "| H1 | H2 |",
            "| --- | --- |",
            "| a |",
            "| b | c | d |",
        ]
    )

    blocks = parse_markdown_table(table_text)

    assert blocks is not None
    assert len(blocks) == 1

    rows = blocks[0]["rows"]
    assert len(rows) == 3
    assert [cell["text"] for cell in rows[0]] == ["H1", "H2", " "]
    assert [cell["text"] for cell in rows[1]] == ["a", " ", " "]
    assert [cell["text"] for cell in rows[2]] == ["b", "c", "d"]


def test_parse_markdown_table_skips_extra_separator_rows_in_body() -> None:
    table_text = "\n".join(
        [
            "| H1 | H2 |",
            "| --- | --- |",
            "| a | b |",
            "| --- | --- |",
            "| c | d |",
        ]
    )

    blocks = parse_markdown_table(table_text)

    assert blocks is not None
    rows = blocks[0]["rows"]
    assert len(rows) == 3
    assert [cell["text"] for cell in rows[1]] == ["a", "b"]
    assert [cell["text"] for cell in rows[2]] == ["c", "d"]


def test_parse_markdown_table_chunks_large_column_and_row_counts() -> None:
    header = [f"c{i}" for i in range(21)]
    separator = ["---"] * 21
    data_rows = [[f"r{r}c{c}" for c in range(21)] for r in range(101)]

    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in data_rows)

    blocks = parse_markdown_table("\n".join(lines))

    assert blocks is not None
    assert len(blocks) == 4

    assert len(blocks[0]["rows"]) == 100
    assert len(blocks[1]["rows"]) == 3
    assert len(blocks[2]["rows"]) == 100
    assert len(blocks[3]["rows"]) == 3

    assert len(blocks[0]["rows"][0]) == 20
    assert len(blocks[2]["rows"][0]) == 1


def test_extract_tables_from_text_extracts_valid_tables_and_restores_code_blocks() -> None:
    text = "\n".join(
        [
            "before",
            "```",
            "| not | a table |",
            "| --- | --- |",
            "```",
            "| H1 | H2 |",
            "| --- | --- |",
            "| a | b |",
            "after",
        ]
    )

    transformed, table_blocks = extract_tables_from_text(text)

    assert len(table_blocks) == 1
    assert table_blocks[0]["type"] == "table"
    assert "```\n| not | a table |\n| --- | --- |\n```" in transformed
    assert "\x00TABLEBLOCK0\x00" in transformed


def test_extract_tables_from_text_keeps_header_only_table_text_when_parse_fails() -> None:
    text = "\n".join(
        [
            "before",
            "| H1 | H2 |",
            "| --- | --- |",
            "after",
        ]
    )

    transformed, table_blocks = extract_tables_from_text(text)

    assert table_blocks == []
    assert transformed == text


def test_split_text_by_tables_returns_text_and_table_segments() -> None:
    text = " intro \x00TABLEBLOCK0\x00 middle \x00TABLEBLOCK2\x00 end "

    segments = split_text_by_tables(text)

    assert segments == [
        {"type": "text", "content": "intro"},
        {"type": "table", "index": 0},
        {"type": "text", "content": "middle"},
        {"type": "table", "index": 2},
        {"type": "text", "content": "end"},
    ]


def test_split_text_by_tables_handles_only_table_placeholders() -> None:
    assert split_text_by_tables("\x00TABLEBLOCK1\x00") == [{"type": "table", "index": 1}]
