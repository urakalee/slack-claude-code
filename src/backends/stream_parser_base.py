"""Shared base parser state and iteration utilities for backend stream parsers."""

from typing import Iterator, Optional

from loguru import logger

from src.backends.stream_parsing_common import parse_json_line_with_buffer
from src.utils.stream_models import BaseToolActivity, StreamMessage


class BaseStreamParser:
    """Shared parser state and utilities for backend stream parsers."""

    def __init__(self) -> None:
        self.buffer = ""
        self.session_id: Optional[str] = None
        self.accumulated_content = ""
        self.accumulated_detailed = ""
        # Track pending tool uses to link with results
        self.pending_tools: dict[str, BaseToolActivity] = {}

    def _append_assistant_content(self, content: str) -> None:
        """Append assistant text to accumulated output buffers."""
        if not content:
            return
        self.accumulated_content += content
        self.accumulated_detailed += content

    def _parse_json_line(
        self,
        line: str,
        *,
        max_buffer_size: int,
    ) -> tuple[object | None, StreamMessage | None]:
        """Parse a JSON line and emit overflow errors consistently."""
        data, self.buffer, overflow_error = parse_json_line_with_buffer(
            line=line,
            buffer=self.buffer,
            max_buffer_size=max_buffer_size,
        )
        if overflow_error:
            logger.error(
                f"{overflow_error} This may indicate a malformed JSON stream or extremely large output. Resetting buffer."
            )
            return None, StreamMessage(
                type="error",
                content=(
                    "Stream buffer overflow: "
                    f"JSON chunk exceeded {max_buffer_size // 1024}KB limit"
                ),
                raw={},
            )
        return data, None

    def parse_stream(self, stream: Iterator[str]) -> Iterator[StreamMessage]:
        """Parse a stream of lines."""
        for line in stream:
            msg = self.parse_line(line)
            if msg:
                yield msg

    def reset(self) -> None:
        """Reset parser state."""
        self.buffer = ""
        self.session_id = None
        self.accumulated_content = ""
        self.accumulated_detailed = ""
        self.pending_tools.clear()
