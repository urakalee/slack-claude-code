"""Unit tests for tool activity block formatting."""

import re

from src.claude.streaming import ToolActivity
from src.utils.formatters import tool_blocks


def _tool(
    name: str = "Read",
    input_dict: dict | None = None,
    input_summary: str = "`x`",
    result: str | None = None,
    full_result: str | None = None,
    is_error: bool = False,
    duration_ms: int | None = None,
    timestamp: float | None = None,
) -> ToolActivity:
    return ToolActivity(
        id=f"{name}-1",
        name=name,
        input=input_dict or {},
        input_summary=input_summary,
        result=result,
        full_result=full_result,
        is_error=is_error,
        duration_ms=duration_ms,
        timestamp=timestamp,
    )


def test_split_code_into_blocks_short_text_with_prefix() -> None:
    blocks = tool_blocks._split_code_into_blocks("echo hi", prefix="*Input:*")
    assert len(blocks) == 1
    assert blocks[0]["text"]["text"].startswith("*Input:*\n```")


def test_split_code_into_blocks_long_text_splits_across_multiple_blocks() -> None:
    text = "line1\nline2\nline3\nline4\nline5\nline6"
    blocks = tool_blocks._split_code_into_blocks(text, prefix="*Input:*", max_length=22)
    assert len(blocks) > 1
    assert blocks[0]["text"]["text"].startswith("*Input:*")
    assert all("```" in block["text"]["text"] for block in blocks)


def test_get_tool_icon_and_inline_formatting() -> None:
    assert tool_blocks.get_tool_icon("Read") == ""
    assert tool_blocks.get_tool_icon("Unknown") == ""

    inline = tool_blocks.format_tool_inline(_tool(name="Read", input_summary="`file.py`"))
    assert inline == "*Read* `file.py`"


def test_format_tool_status_variants() -> None:
    assert tool_blocks.format_tool_status(_tool(result=None, is_error=False)) == "..."
    assert tool_blocks.format_tool_status(_tool(result="ok", is_error=False, duration_ms=12)) == "OK (12ms)"
    assert tool_blocks.format_tool_status(_tool(result="bad", is_error=True)) == "ERROR"


def test_format_tool_timestamp_and_activity_line() -> None:
    tool_with_time = _tool(timestamp=1700000000, result="done")
    timestamp = tool_blocks.format_tool_timestamp(tool_with_time)
    assert re.match(r"^\d{2}:\d{2}:\d{2}$", timestamp)

    line = tool_blocks.format_tool_activity_line(tool_with_time)
    assert line.startswith("`")
    assert "*Read*" in line
    assert "OK" in line

    tool_no_time = _tool(timestamp=None, result="done")
    assert tool_blocks.format_tool_timestamp(tool_no_time) == ""
    assert not tool_blocks.format_tool_activity_line(tool_no_time).startswith("`")


def test_format_tool_activity_section_empty_and_truncated() -> None:
    assert tool_blocks.format_tool_activity_section([]) == []

    tools = [
        _tool(name="Read", input_summary="`a`"),
        _tool(name="Write", input_summary="`b`"),
        _tool(name="Bash", input_summary="`c`"),
    ]
    blocks = tool_blocks.format_tool_activity_section(tools, max_display=2)

    assert blocks[0]["type"] == "divider"
    text = blocks[1]["text"]["text"]
    assert "*Write*" in text
    assert "*Bash*" in text
    assert "*Read*" not in text
    assert "Showing 2 of 3 tools" in blocks[2]["elements"][0]["text"]


def test_format_tool_detail_blocks_uses_full_result_and_error_label() -> None:
    tool = _tool(
        name="Bash",
        input_dict={"command": "ls", "description": "list files"},
        result="short",
        full_result="full command output",
        duration_ms=34,
    )

    blocks = tool_blocks.format_tool_detail_blocks(tool)

    assert "*Bash* OK (34ms)" in blocks[0]["text"]["text"]
    assert any("*Input:*" in block["text"]["text"] for block in blocks if block["type"] == "section")
    assert any("*Result:*" in block["text"]["text"] for block in blocks if block["type"] == "section")
    assert any("full command output" in block["text"]["text"] for block in blocks if block["type"] == "section")

    error_tool = _tool(name="Read", is_error=True, result="boom")
    error_blocks = tool_blocks.format_tool_detail_blocks(error_tool)
    assert any("*Error:*" in block["text"]["text"] for block in error_blocks if block["type"] == "section")


def test_format_tool_detail_blocks_error_without_result_adds_divider_only() -> None:
    tool = _tool(name="Read", is_error=True, result=None, full_result=None)
    blocks = tool_blocks.format_tool_detail_blocks(tool)
    assert blocks[0]["type"] == "section"
    assert blocks[1]["type"] == "divider"


def test_format_tool_input_detail_specialized_branches() -> None:
    read = tool_blocks._format_tool_input_detail(
        "Read",
        {"file_path": "src/app.py", "offset": 10, "limit": 20},
    )
    assert "file_path: src/app.py" in read
    assert "offset: 10" in read
    assert "limit: 20" in read

    edit = tool_blocks._format_tool_input_detail(
        "Edit",
        {"file_path": "a.py", "old_string": "o" * 250, "new_string": "n" * 250},
    )
    assert "old_string:" in edit and "..." in edit
    assert "new_string:" in edit and "..." in edit

    write = tool_blocks._format_tool_input_detail(
        "Write",
        {"file_path": "a.py", "content": "c" * 320},
    )
    assert "content (320 chars):" in write
    assert write.endswith("...")

    bash = tool_blocks._format_tool_input_detail(
        "Bash",
        {"command": "pytest", "description": "run tests"},
    )
    assert "command: pytest" in bash
    assert "description: run tests" in bash

    glob = tool_blocks._format_tool_input_detail(
        "Glob",
        {"pattern": "*.py", "path": "src", "type": "files", "ignored": "x"},
    )
    assert "pattern: *.py" in glob
    assert "path: src" in glob
    assert "type: files" in glob
    assert "ignored" not in glob

    task = tool_blocks._format_tool_input_detail(
        "Task",
        {
            "description": "summarize",
            "prompt": "p" * 400,
            "subagent_type": "reviewer",
        },
    )
    assert "description: summarize" in task
    assert "prompt:" in task and "..." in task
    assert "subagent_type: reviewer" in task

    question = tool_blocks._format_tool_input_detail(
        "AskUserQuestion",
        {
            "questions": [
                {
                    "header": "Choice",
                    "question": "Pick one",
                    "options": [{"label": "A"}, {"label": "B"}],
                }
            ]
        },
    )
    assert "Choice: Pick one" in question
    assert "Options: A, B" in question


def test_format_tool_input_detail_generic_and_empty() -> None:
    assert tool_blocks._format_tool_input_detail("Read", {}) == ""

    generic = tool_blocks._format_tool_input_detail(
        "Other",
        {
            "short": "ok",
            "long": "x" * 150,
            "arr": [1, 2, 3],
            "obj": {"k": "v"},
        },
    )
    assert "short: ok" in generic
    assert "long:" in generic and "..." in generic
    assert "arr:" in generic
    assert "obj:" in generic
