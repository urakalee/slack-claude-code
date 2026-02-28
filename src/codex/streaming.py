"""Parser for Codex CLI stream-json output format."""

from typing import Iterator, Optional

from loguru import logger

from src.backends.stream_parsing_common import (
    create_tool_activity,
    create_tool_result,
    parse_json_line_with_buffer,
)
from src.utils.stream_models import BaseToolActivity, StreamMessage

# Maximum size for buffered incomplete JSON to prevent memory exhaustion
MAX_BUFFER_SIZE = 1024 * 1024  # 1MB

CODEX_TOOL_SUMMARY_RULES = {
    "read_file": {"type": "path", "keys": ["path", "file_path"]},
    "edit_file": {"type": "path", "keys": ["path", "file_path"]},
    "write_file": {"type": "path", "keys": ["path", "file_path"]},
    "shell": {"type": "cmd", "keys": ["command", "cmd"]},
    "run_command": {"type": "cmd", "keys": ["command", "cmd"]},
    "glob": {"type": "pattern", "keys": ["pattern"]},
    "find_files": {"type": "pattern", "keys": ["pattern"]},
    "grep": {"type": "pattern", "keys": ["pattern", "query"]},
    "search": {"type": "pattern", "keys": ["pattern", "query"]},
    "web_fetch": {"type": "url", "keys": ["url"]},
    "web_search": {"type": "text", "keys": ["query"]},
    "fuzzy_file_search": {"type": "pattern", "keys": ["query", "pattern"]},
    "file_change": {"type": "path", "keys": ["path"]},
    "mcp_tool_call": {"type": "text", "keys": ["server", "tool"]},
    "reasoning": {"type": "text", "keys": ["summary", "content"]},
    "request_user_input": {"type": "first_question", "keys": ["questions"]},
}


class ToolActivity(BaseToolActivity):
    """Codex-specific tool activity metadata."""

    SUMMARY_RULES = CODEX_TOOL_SUMMARY_RULES


class StreamParser:
    """Parser for normalized Codex app-server stream events.

    The executor maps app-server JSON-RPC notifications into line-delimited event
    payloads consumed here (for example: thread.started, item.started/completed,
    request_user_input, turn.completed, turn.failed).
    """

    def __init__(self):
        self.buffer = ""
        self.session_id: Optional[str] = None
        self.accumulated_content = ""
        self.accumulated_detailed = ""
        # Track pending tool uses to link with results
        self.pending_tools: dict[str, ToolActivity] = {}  # tool_use_id -> ToolActivity

    def _append_assistant_content(self, content: str) -> None:
        """Append assistant text to accumulated output buffers."""
        if not content:
            return
        self.accumulated_content += content
        self.accumulated_detailed += content

    def _create_tool_call(
        self,
        tool_id: str,
        tool_name: str,
        tool_input: object,
        raw_data: dict,
    ) -> StreamMessage:
        """Create a normalized tool-call StreamMessage."""
        tool_activity, detailed_addition, collision = create_tool_activity(
            tool_cls=ToolActivity,
            pending_tools=self.pending_tools,
            tool_id=tool_id,
            tool_name=tool_name,
            tool_input=tool_input,
        )

        if collision:
            logger.warning(
                f"Tool ID collision detected: {tool_id} already tracked. "
                "This may indicate duplicate tool invocations."
            )
        self.accumulated_detailed += detailed_addition

        return StreamMessage(
            type="tool_call",
            tool_activities=[tool_activity],
            session_id=self.session_id,
            raw=raw_data,
        )

    def _create_tool_result(
        self,
        tool_use_id: str,
        content: str,
        is_error: bool,
        raw_data: dict,
    ) -> StreamMessage:
        """Create a normalized tool-result StreamMessage."""
        tool_activities, detailed_addition = create_tool_result(
            tool_cls=ToolActivity,
            pending_tools=self.pending_tools,
            tool_use_id=tool_use_id,
            content=content,
            is_error=is_error,
        )
        self.accumulated_detailed += detailed_addition

        return StreamMessage(
            type="tool_result",
            detailed_content=detailed_addition,
            tool_activities=tool_activities,
            session_id=self.session_id,
            raw=raw_data,
        )

    def _create_result_message(self, raw_data: dict) -> StreamMessage:
        """Create a normalized final-result StreamMessage."""
        self.pending_tools.clear()
        return StreamMessage(
            type="result",
            content=self.accumulated_content,
            detailed_content=self.accumulated_detailed,
            session_id=raw_data.get("session_id", self.session_id),
            is_final=True,
            cost_usd=raw_data.get("cost_usd", raw_data.get("usage", {}).get("cost")),
            duration_ms=raw_data.get("duration_ms", raw_data.get("duration")),
            raw=raw_data,
        )

    def parse_line(self, line: str) -> Optional[StreamMessage]:
        """Parse a single line of stream-json output."""
        data, self.buffer, overflow_error = parse_json_line_with_buffer(
            line=line,
            buffer=self.buffer,
            max_buffer_size=MAX_BUFFER_SIZE,
        )
        if overflow_error:
            logger.error(
                f"{overflow_error} This may indicate a malformed JSON stream or extremely large output. Resetting buffer."
            )
            return StreamMessage(
                type="error",
                content=f"Stream buffer overflow: JSON chunk exceeded {MAX_BUFFER_SIZE // 1024}KB limit",
                raw={},
            )
        if data is None:
            return None

        # Determine event type - Codex uses different event structure
        event_type = data.get("type", data.get("event", "unknown"))

        if event_type == "thread.started":
            # New stream format: thread id acts as session id.
            self.session_id = data.get("thread_id", self.session_id)
            return StreamMessage(
                type="init",
                session_id=self.session_id,
                raw=data,
            )

        elif event_type == "turn.started":
            return StreamMessage(
                type="turn_started", session_id=self.session_id, raw=data
            )

        elif event_type == "item.started":
            # New stream format: item lifecycle events.
            item = data.get("item", {})
            item_type = item.get("type")
            if item_type in {"command_execution", "commandExecution"}:
                tool_id = str(item.get("id", "unknown"))
                command = item.get("command", "")
                return self._create_tool_call(
                    tool_id=tool_id,
                    tool_name="run_command",
                    tool_input={"command": command},
                    raw_data=data,
                )
            if item_type == "webSearch":
                tool_id = str(item.get("id", "unknown"))
                return self._create_tool_call(
                    tool_id=tool_id,
                    tool_name="web_search",
                    tool_input={"query": item.get("query", "")},
                    raw_data=data,
                )
            if item_type == "fuzzyFileSearch":
                tool_id = str(item.get("id", "unknown"))
                query = item.get("query") or item.get("pattern") or ""
                return self._create_tool_call(
                    tool_id=tool_id,
                    tool_name="fuzzy_file_search",
                    tool_input={"query": query},
                    raw_data=data,
                )
            if item_type == "fileChange":
                tool_id = str(item.get("id", "unknown"))
                first_change = (item.get("changes") or [{}])[0]
                return self._create_tool_call(
                    tool_id=tool_id,
                    tool_name="file_change",
                    tool_input={"path": first_change.get("path", "")},
                    raw_data=data,
                )
            if item_type == "mcpToolCall":
                tool_id = str(item.get("id", "unknown"))
                return self._create_tool_call(
                    tool_id=tool_id,
                    tool_name="mcp_tool_call",
                    tool_input={
                        "server": item.get("server", ""),
                        "tool": item.get("tool", ""),
                    },
                    raw_data=data,
                )
            if item_type == "reasoning":
                tool_id = str(item.get("id", "unknown"))
                return self._create_tool_call(
                    tool_id=tool_id,
                    tool_name="reasoning",
                    tool_input={"summary": "reasoning in progress"},
                    raw_data=data,
                )
            return StreamMessage(
                type="item_started", session_id=self.session_id, raw=data
            )

        elif event_type == "item.completed":
            # New stream format: completed items include assistant messages and command results.
            item = data.get("item", {})
            item_type = item.get("type")
            if item_type in {"agent_message", "agentMessage"}:
                content = item.get("text", "")
                self._append_assistant_content(content)
                return StreamMessage(
                    type="assistant",
                    content=content,
                    detailed_content=content,
                    session_id=self.session_id,
                    raw=data,
                )
            if item_type in {"command_execution", "commandExecution"}:
                tool_id = str(item.get("id", "unknown"))
                command_output = item.get(
                    "aggregated_output",
                    item.get("aggregatedOutput", item.get("output", "")),
                )
                exit_code = item.get("exit_code", item.get("exitCode"))
                status = str(item.get("status", "")).lower()
                item_error = item.get("error")
                is_error = (
                    exit_code not in (0, None)
                    or status in {"failed", "error", "cancelled"}
                    or bool(item_error)
                )
                if not command_output and item_error:
                    command_output = (
                        item_error.get("message", str(item_error))
                        if isinstance(item_error, dict)
                        else str(item_error)
                    )
                return self._create_tool_result(
                    tool_use_id=tool_id,
                    content=command_output,
                    is_error=is_error,
                    raw_data=data,
                )
            if item_type == "fileChange":
                tool_id = str(item.get("id", "unknown"))
                changes = item.get("changes", [])
                content = f"Applied {len(changes)} file change(s)."
                if changes:
                    first = changes[0]
                    content += f" First path: {first.get('path', 'unknown')}"
                status = str(item.get("status", "")).lower()
                return self._create_tool_result(
                    tool_use_id=tool_id,
                    content=content,
                    is_error=status in {"failed", "error", "declined"},
                    raw_data=data,
                )
            if item_type == "mcpToolCall":
                tool_id = str(item.get("id", "unknown"))
                status = str(item.get("status", "")).lower()
                content = f"MCP {item.get('server', '')}/{item.get('tool', '')}: {status or 'completed'}"
                if status == "failed" and item.get("error"):
                    content += f"\n{item.get('error')}"
                return self._create_tool_result(
                    tool_use_id=tool_id,
                    content=content,
                    is_error=status in {"failed", "error"},
                    raw_data=data,
                )
            if item_type == "webSearch":
                tool_id = str(item.get("id", "unknown"))
                action = item.get("action", {})
                content = f"Web search query: {item.get('query', '')}"
                if isinstance(action, dict) and action:
                    content += f"\nAction: {action.get('type', 'other')}"
                return self._create_tool_result(
                    tool_use_id=tool_id,
                    content=content,
                    is_error=False,
                    raw_data=data,
                )
            if item_type == "fuzzyFileSearch":
                tool_id = str(item.get("id", "unknown"))
                result_count = len(item.get("results", []))
                content = f"Fuzzy file search returned {result_count} result(s)."
                return self._create_tool_result(
                    tool_use_id=tool_id,
                    content=content,
                    is_error=False,
                    raw_data=data,
                )
            if item_type == "reasoning":
                tool_id = str(item.get("id", "unknown"))
                summary = item.get("summary", [])
                summary_text = (
                    "\n".join(summary) if isinstance(summary, list) else str(summary)
                )
                return self._create_tool_result(
                    tool_use_id=tool_id,
                    content=summary_text or "Reasoning complete.",
                    is_error=False,
                    raw_data=data,
                )
            return StreamMessage(
                type="item_completed", session_id=self.session_id, raw=data
            )

        elif event_type == "request_user_input":
            # App-server stream format: request for structured user input.
            call_id = str(data.get("call_id", data.get("id", "request_user_input")))
            questions = data.get("questions", [])
            return self._create_tool_call(
                tool_id=call_id,
                tool_name="request_user_input",
                tool_input={"questions": questions},
                raw_data=data,
            )

        elif event_type == "turn.completed":
            return self._create_result_message(data)

        elif event_type == "turn.failed":
            error_obj = data.get("error", {})
            error_msg = (
                error_obj.get("message")
                if isinstance(error_obj, dict)
                else str(error_obj)
            )
            return StreamMessage(
                type="error",
                content=str(error_msg or "Codex turn failed"),
                session_id=self.session_id,
                is_final=True,
                raw=data,
            )

        elif event_type == "assistant":
            # Synthetic assistant delta event emitted by executor.
            content = data.get("content", data.get("text", ""))
            self._append_assistant_content(str(content))

            return StreamMessage(
                type="assistant",
                content=str(content),
                detailed_content=str(content),
                session_id=self.session_id,
                raw=data,
            )

        elif event_type == "tool_result":
            # Synthetic tool_result event emitted by executor.
            tool_use_id = data.get("tool_use_id", data.get("id", "unknown"))
            content = data.get("content", data.get("output", data.get("result", "")))
            is_error = data.get("is_error", data.get("error", False))

            if isinstance(is_error, str):
                is_error = is_error.lower() == "true"

            # Get full content as string
            if isinstance(content, str):
                full_content = content
            elif isinstance(content, list):
                full_content = ""
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        full_content += item.get("text", "")
                    elif isinstance(item, str):
                        full_content += item
            else:
                full_content = str(content) if content else ""

            return self._create_tool_result(
                tool_use_id=tool_use_id,
                content=full_content,
                is_error=is_error,
                raw_data=data,
            )

        elif event_type == "error":
            error_msg = data.get("error", {})
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            return StreamMessage(
                type="error",
                content=str(error_msg),
                is_final=True,
                raw=data,
            )

        # Handle any other message type
        return StreamMessage(type=event_type, raw=data)

    def parse_stream(self, stream: Iterator[str]) -> Iterator[StreamMessage]:
        """Parse a stream of lines."""
        for line in stream:
            msg = self.parse_line(line)
            if msg:
                yield msg

    def reset(self):
        """Reset parser state."""
        self.buffer = ""
        self.session_id = None
        self.accumulated_content = ""
        self.accumulated_detailed = ""
        self.pending_tools.clear()
