import json
from typing import Optional

from loguru import logger

from src.backends.stream_parser_base import BaseStreamParser
from src.backends.stream_parsing_common import (
    create_tool_activity,
    create_tool_result,
)
from src.utils.stream_models import (
    BaseToolActivity,
    StreamMessage,
    concat_with_spacing,
)

# Maximum size for buffered incomplete JSON to prevent memory exhaustion
# Increased to 1MB to handle large file reads and tool outputs
MAX_BUFFER_SIZE = 1024 * 1024  # 1MB

CLAUDE_TOOL_SUMMARY_RULES = {
    "Read": {"type": "path", "keys": ["file_path"]},
    "Edit": {"type": "path", "keys": ["file_path"]},
    "Write": {"type": "path", "keys": ["file_path"]},
    "Bash": {"type": "cmd", "keys": ["command"]},
    "Glob": {"type": "pattern", "keys": ["pattern"]},
    "Grep": {"type": "pattern", "keys": ["pattern"]},
    "Task": {"type": "text", "keys": ["description", "prompt"]},
    "WebFetch": {"type": "url", "keys": ["url"]},
    "WebSearch": {"type": "text", "keys": ["query"]},
    "LSP": {"type": "lsp", "op_key": "operation", "path_keys": ["filePath"]},
    "TodoWrite": {"type": "count", "keys": ["todos"], "suffix": " items"},
    "AskUserQuestion": {"type": "first_question", "keys": ["questions"]},
}

_concat_with_spacing = concat_with_spacing


class ToolActivity(BaseToolActivity):
    """Claude-specific tool activity metadata."""

    SUMMARY_RULES = CLAUDE_TOOL_SUMMARY_RULES


class StreamParser(BaseStreamParser):
    """Parser for Claude CLI stream-json output format."""

    def __init__(self) -> None:
        super().__init__()

    def parse_line(self, line: str) -> Optional[StreamMessage]:
        """Parse a single line of stream-json output."""
        data, overflow_message = self._parse_json_line(
            line,
            max_buffer_size=MAX_BUFFER_SIZE,
        )
        if overflow_message:
            return overflow_message
        if data is None:
            return None

        if not isinstance(data, dict):
            # Handle unexpected non-object JSON (e.g., a JSON string) as plain text output
            text = str(data)
            self.accumulated_content += text
            self.accumulated_detailed += text
            return StreamMessage(
                type="assistant",
                content=text,
                detailed_content=text,
                session_id=self.session_id,
                raw={},
            )

        msg_type = data.get("type", "unknown")

        def coerce_text(value: object) -> str:
            if value is None:
                return ""
            if isinstance(value, str):
                return value
            try:
                return json.dumps(value, indent=2, ensure_ascii=False)
            except TypeError:
                return str(value)

        if msg_type == "system":
            # System init message contains session_id
            self.session_id = data.get("session_id")
            return StreamMessage(
                type="init",
                session_id=self.session_id,
                raw=data,
            )

        elif msg_type == "assistant":
            # Assistant message with content
            message = data.get("message", {})
            if isinstance(message, str):
                text_content = message
                self.accumulated_content += text_content
                self.accumulated_detailed += text_content
                return StreamMessage(
                    type="assistant",
                    content=text_content,
                    detailed_content=text_content,
                    session_id=self.session_id,
                    raw=data,
                )
            if not isinstance(message, dict):
                message = {}
            content_blocks = message.get("content", [])
            if isinstance(content_blocks, str):
                text_content = content_blocks
                self.accumulated_content += text_content
                self.accumulated_detailed += text_content
                return StreamMessage(
                    type="assistant",
                    content=text_content,
                    detailed_content=text_content,
                    session_id=self.session_id,
                    raw=data,
                )
            if not isinstance(content_blocks, list):
                content_blocks = []

            text_content = ""
            detailed_content = ""
            tool_activities = []

            for block in content_blocks:
                if not isinstance(block, dict):
                    if isinstance(block, str):
                        text_content += block
                        detailed_content += block
                    continue
                if block.get("type") == "text":
                    text = block.get("text", "")
                    text_content += coerce_text(text)
                    detailed_content += coerce_text(text)
                elif block.get("type") == "tool_use":
                    # Create structured tool activity
                    tool_id = block.get("id", "")
                    tool_name = block.get("name", "unknown")
                    tool_input = block.get("input", {})

                    tool_activity, tool_detailed, collision = create_tool_activity(
                        tool_cls=ToolActivity,
                        pending_tools=self.pending_tools,
                        tool_id=tool_id,
                        tool_name=tool_name,
                        tool_input=tool_input,
                    )
                    tool_activities.append(tool_activity)

                    # Track for linking with results (detect collisions)
                    if collision:
                        logger.warning(
                            f"Tool ID collision detected: {tool_id} already tracked. "
                            "This may indicate duplicate tool invocations."
                        )
                    detailed_content += tool_detailed

            if text_content:
                self.accumulated_content = _concat_with_spacing(
                    self.accumulated_content, text_content
                )
            if detailed_content:
                self.accumulated_detailed += detailed_content

            return StreamMessage(
                type="assistant",
                content=text_content,
                detailed_content=detailed_content,
                tool_activities=tool_activities,
                session_id=self.session_id,
                raw=data,
            )

        elif msg_type == "user":
            # User message (tool results)
            message = data.get("message", {})
            if isinstance(message, str):
                detailed_addition = message
                if detailed_addition:
                    self.accumulated_detailed += detailed_addition
                return StreamMessage(
                    type="user",
                    detailed_content=detailed_addition,
                    session_id=self.session_id,
                    raw=data,
                )
            if not isinstance(message, dict):
                message = {}
            content_blocks = message.get("content", [])
            if isinstance(content_blocks, str):
                detailed_addition = content_blocks
                if detailed_addition:
                    self.accumulated_detailed += detailed_addition
                return StreamMessage(
                    type="user",
                    detailed_content=detailed_addition,
                    session_id=self.session_id,
                    raw=data,
                )
            if not isinstance(content_blocks, list):
                content_blocks = []

            detailed_addition = ""
            tool_activities = []

            for block in content_blocks:
                if not isinstance(block, dict):
                    if isinstance(block, str):
                        detailed_addition += block
                    continue
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "unknown")
                    content = block.get("content", "")
                    is_error = block.get("is_error", False)

                    # Get full content as string
                    if isinstance(content, str):
                        full_content = content
                    elif isinstance(content, list):
                        # Handle array of content blocks
                        full_content = ""
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                full_content += coerce_text(item.get("text", ""))
                            elif isinstance(item, str):
                                full_content += item
                    else:
                        full_content = coerce_text(content)
                    parsed_activities, parsed_detailed = create_tool_result(
                        tool_cls=ToolActivity,
                        pending_tools=self.pending_tools,
                        tool_use_id=tool_use_id,
                        content=full_content,
                        is_error=is_error,
                    )
                    tool_activities.extend(parsed_activities)
                    detailed_addition += parsed_detailed

            if detailed_addition:
                self.accumulated_detailed += detailed_addition

            return StreamMessage(
                type="user",
                detailed_content=detailed_addition,
                tool_activities=tool_activities,
                session_id=self.session_id,
                raw=data,
            )

        elif msg_type == "result":
            # Final result message
            # Clear pending tools to prevent memory accumulation across sessions
            self.pending_tools.clear()

            # Some commands (like /doctor, /cost, etc.) return output directly in the
            # "result" field without producing assistant messages. Capture this output.
            result_text = coerce_text(data.get("result", ""))
            if not result_text:
                result_message = data.get("message", "")
                result_text = coerce_text(result_message)
            final_content = self.accumulated_content
            if result_text:
                if final_content:
                    if result_text not in final_content:
                        final_content = f"{final_content}\n\n{result_text}"
                else:
                    final_content = result_text

            return StreamMessage(
                type="result",
                content=final_content,
                detailed_content=self.accumulated_detailed,
                session_id=data.get("session_id", self.session_id),
                is_final=True,
                cost_usd=data.get("cost_usd"),
                duration_ms=data.get("duration_ms"),
                raw=data,
            )

        elif msg_type == "error":
            return StreamMessage(
                type="error",
                content=data.get("error", {}).get("message", "Unknown error"),
                is_final=True,
                raw=data,
            )

        return StreamMessage(type=msg_type, raw=data)
