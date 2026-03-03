"""Shared base parser state and iteration utilities for backend stream parsers."""

from typing import Iterator, Optional

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
