"""Codex app-server executor using subprocess JSON-RPC over stdio."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from loguru import logger

from src.backends.process_registry import ProcessRegistry
from src.backends.process_termination import terminate_processes
from src.codex.approval_bridge import default_approval_payload
from src.codex.capabilities import normalize_codex_approval_mode
from src.config import config, parse_model_effort
from src.utils.execution_scope import build_session_scope
from src.utils.process_utils import terminate_process_safely

from .streaming import StreamMessage, StreamParser

if TYPE_CHECKING:
    from src.database.repository import DatabaseRepository

# Backwards-compatible alias retained for existing tests/integration points.
_terminate_process_safely = terminate_process_safely


@dataclass
class ExecutionResult:
    """Result of a Codex execution."""

    success: bool
    output: str
    detailed_output: str = ""  # Full output with tool use details
    session_id: Optional[str] = None
    error: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    was_cancelled: bool = False


@dataclass
class TurnControlResult:
    """Result for steer/interrupt control requests sent to active turns."""

    success: bool
    message: str = ""
    error: Optional[str] = None
    turn_id: Optional[str] = None


@dataclass
class _ControlRequest:
    """Internal request queued to an active turn execution loop."""

    kind: str  # "steer" | "interrupt"
    future: asyncio.Future[TurnControlResult]
    text: Optional[str] = None


@dataclass
class _ActiveTurnState:
    """Live app-server turn metadata for steer/interrupt routing."""

    scope: str
    track_id: str
    thread_id: str
    turn_id: str
    control_queue: asyncio.Queue[_ControlRequest]
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    started_at: float = field(default_factory=time.monotonic)


class SubprocessExecutor:
    """Execute Codex via `codex app-server` JSON-RPC over stdio."""

    def __init__(
        self,
        db: Optional["DatabaseRepository"] = None,
    ) -> None:
        self._registry = ProcessRegistry()
        # Backwards-compatible aliases retained for tests/integration points.
        self._active_processes = self._registry.active_processes
        self._process_channels = self._registry.process_channels
        self._process_scopes = self._registry.process_scopes
        self._execution_track_ids = self._registry.execution_track_ids
        self._active_turns_by_scope: dict[str, _ActiveTurnState] = {}
        self._active_turns_by_track: dict[str, _ActiveTurnState] = {}
        self._metrics: dict[str, int] = {
            "turn_start_registered": 0,
            "turn_state_cleared": 0,
            "steer_requests": 0,
            "steer_successes": 0,
            "steer_failures": 0,
            "steer_timeouts": 0,
            "interrupt_requests": 0,
            "interrupt_successes": 0,
            "interrupt_failures": 0,
            "interrupt_timeouts": 0,
            "queue_fallback_attempts": 0,
            "queue_fallback_successes": 0,
            "queue_fallback_failures": 0,
        }
        self._lock: asyncio.Lock = asyncio.Lock()
        self.db = db

    async def _increment_metric(self, metric_name: str, count: int = 1) -> None:
        """Increment a named runtime metric counter."""
        async with self._lock:
            if metric_name not in self._metrics:
                self._metrics[metric_name] = 0
            self._metrics[metric_name] += count

    async def record_queue_fallback(self, success: bool) -> None:
        """Record steer-failure queue fallback outcome from app-level routing."""
        await self._increment_metric("queue_fallback_attempts")
        if success:
            await self._increment_metric("queue_fallback_successes")
            return
        await self._increment_metric("queue_fallback_failures")

    async def get_metrics_snapshot(self) -> dict[str, Any]:
        """Return a snapshot of runtime Codex integration metrics."""
        async with self._lock:
            counters = dict(self._metrics)
            active_turns = sum(
                1 for state in self._active_turns_by_scope.values() if not state.done_event.is_set()
            )

        def safe_rate(success_key: str, total_key: str) -> float:
            total = counters.get(total_key, 0)
            if total <= 0:
                return 0.0
            return counters.get(success_key, 0) / total

        counters["active_turns"] = active_turns
        counters["steer_success_rate"] = safe_rate("steer_successes", "steer_requests")
        counters["interrupt_success_rate"] = safe_rate("interrupt_successes", "interrupt_requests")
        counters["queue_fallback_success_rate"] = safe_rate(
            "queue_fallback_successes", "queue_fallback_attempts"
        )
        return counters

    async def reset_metrics(self) -> None:
        """Reset runtime Codex integration metrics counters."""
        async with self._lock:
            for key in list(self._metrics.keys()):
                self._metrics[key] = 0

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        on_user_input_request: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]] = None,
        on_approval_request: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]] = None,
        permission_mode: Optional[str] = None,
        sandbox_mode: Optional[str] = None,
        approval_mode: Optional[str] = None,
        db_session_id: Optional[int] = None,
        model: Optional[str] = None,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        _recursion_depth: int = 0,
    ) -> ExecutionResult:
        """Execute a prompt via Codex app-server.

        Args:
            prompt: The prompt to send to Codex.
            working_directory: Directory to run Codex in.
            session_id: Identifier for this execution (for tracking).
            resume_session_id: Codex thread ID to resume (from previous execution).
            execution_id: Unique ID for this execution (for cancellation).
            on_chunk: Async callback for each streamed message.
            on_user_input_request: Callback for request_user_input prompts.
            on_approval_request: Callback for command/file/skill approval prompts.
            permission_mode: Slack compatibility mode (e.g., "plan", "default").
            sandbox_mode: Sandbox mode (read-only, workspace-write, danger-full-access).
            approval_mode: Approval mode (untrusted, on-request, never).
            db_session_id: Database session ID for tracking.
            model: Model to use (e.g., "gpt-5.3-codex").
            channel_id: Slack channel ID (for process tracking).
            _recursion_depth: Internal retry depth for resume recovery.

        Returns:
            ExecutionResult with command output.
        """
        log_prefix = f"[S:{db_session_id}] " if db_session_id else ""
        effective_prompt = self._build_effective_prompt(prompt, log_prefix)

        max_recursion_depth = 3
        if _recursion_depth >= max_recursion_depth:
            logger.error(
                f"{log_prefix}Max recursion depth ({max_recursion_depth}) reached, aborting"
            )
            return ExecutionResult(
                success=False,
                output="",
                error=f"Max retry depth ({max_recursion_depth}) exceeded",
            )

        return await self._execute_via_app_server(
            prompt=prompt,
            effective_prompt=effective_prompt,
            working_directory=working_directory,
            session_id=session_id,
            resume_session_id=resume_session_id,
            execution_id=execution_id,
            on_chunk=on_chunk,
            on_user_input_request=on_user_input_request,
            on_approval_request=on_approval_request,
            permission_mode=permission_mode,
            sandbox_mode=sandbox_mode,
            approval_mode=approval_mode,
            db_session_id=db_session_id,
            model=model,
            channel_id=channel_id,
            thread_ts=thread_ts,
            _recursion_depth=_recursion_depth,
        )

    async def _execute_via_app_server(
        self,
        prompt: str,
        effective_prompt: str,
        working_directory: str,
        session_id: Optional[str],
        resume_session_id: Optional[str],
        execution_id: Optional[str],
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]],
        on_user_input_request: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]],
        on_approval_request: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]],
        permission_mode: Optional[str],
        sandbox_mode: Optional[str],
        approval_mode: Optional[str],
        db_session_id: Optional[int],
        model: Optional[str],
        channel_id: Optional[str],
        thread_ts: Optional[str],
        _recursion_depth: int,
    ) -> ExecutionResult:
        """Execute using Codex app-server JSON-RPC flow."""
        log_prefix = f"[S:{db_session_id}] " if db_session_id else ""
        logger.info(f"{log_prefix}Executing via `codex app-server` JSON-RPC flow")

        approval = self._resolve_approval_mode(approval_mode, log_prefix)
        sandbox = self._resolve_sandbox_mode(sandbox_mode, log_prefix)

        cmd = ["codex", "app-server", "--listen", "stdio://"]
        limit = 200 * 1024 * 1024
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_directory,
                limit=limit,
            )
        except Exception as e:
            return ExecutionResult(
                success=False,
                output="",
                error=f"Failed to start codex app-server: {e}",
            )

        track_id = ProcessRegistry.build_track_id(
            execution_id=execution_id,
            session_id=session_id,
            channel_id=channel_id,
        )
        session_scope = build_session_scope(channel_id or "", thread_ts)
        await self._registry.register(
            track_id=track_id,
            process=process,
            channel_id=channel_id,
            session_scope=session_scope,
            execution_id=execution_id,
        )

        parser = StreamParser()
        accumulated_output = ""
        accumulated_detailed = ""
        result_session_id = resume_session_id
        cost_usd = None
        duration_ms = None
        error_msg = None
        started_at = time.monotonic()
        next_request_id = 1
        response_cache: dict[str, dict] = {}
        pending_control_responses: dict[str, _ControlRequest] = {}
        control_queue: asyncio.Queue[_ControlRequest] = asyncio.Queue()
        active_turn_state: Optional[_ActiveTurnState] = None
        current_turn_id: Optional[str] = None
        assistant_delta_item_ids: set[str] = set()

        if model:
            base_model, effort = parse_model_effort(model)
        else:
            base_model, effort = None, None

        async def send_rpc(payload: dict) -> None:
            if process.stdin is None:
                raise RuntimeError("app-server stdin is unavailable")
            process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await process.stdin.drain()

        async def send_request(method: str, params: dict) -> int:
            nonlocal next_request_id
            request_id = next_request_id
            next_request_id += 1
            await send_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            return request_id

        async def handle_stream_message(msg: StreamMessage) -> bool:
            nonlocal accumulated_output, accumulated_detailed, result_session_id
            nonlocal cost_usd, duration_ms, error_msg

            if msg.type == "assistant" and msg.content:
                preview = msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
                logger.debug(f"{log_prefix}Codex: {preview}")
            elif msg.type == "tool_call":
                for tool in msg.tool_activities:
                    logger.info(f"{log_prefix}Tool: {tool.name} {tool.input_summary}")
            elif msg.type == "tool_result":
                for tool in msg.tool_activities:
                    status = "ERROR" if tool.is_error else "OK"
                    logger.info(
                        f"{log_prefix}Tool result [{tool.id[:8] if tool.id else '?'}]: {status}"
                    )
            elif msg.type == "init":
                logger.info(f"{log_prefix}Session initialized: {msg.session_id}")
            elif msg.type == "error":
                logger.error(f"{log_prefix}Error: {msg.content}")
            elif msg.type == "result":
                duration_display = msg.duration_ms if msg.duration_ms is not None else "?"
                logger.info(f"{log_prefix}Codex Finished - completed in {duration_display}ms")

            if msg.session_id:
                result_session_id = msg.session_id

            if msg.type == "assistant" and msg.content:
                # App-server emits tiny deltas; preserve raw chunk boundaries.
                accumulated_output += msg.content

            if msg.type == "result":
                cost_usd = msg.cost_usd
                duration_ms = msg.duration_ms
                if msg.content and not accumulated_output:
                    accumulated_output = msg.content
                if msg.detailed_content:
                    accumulated_detailed = msg.detailed_content
                if msg.raw and msg.raw.get("is_error"):
                    errors = msg.raw.get("errors", [])
                    if errors:
                        error_msg = "; ".join(str(e) for e in errors)

            if msg.type == "error":
                error_msg = msg.content

            if on_chunk:
                await on_chunk(msg)

            return msg.is_final

        async def emit_assistant_delta(delta: str) -> bool:
            if not delta:
                return False
            msg = parser.parse_line(
                json.dumps(
                    {
                        "type": "assistant",
                        "content": delta,
                    }
                )
            )
            if msg:
                return await handle_stream_message(msg)
            return False

        def _suffix_prefix_overlap(existing: str, incoming: str) -> int:
            """Return max overlap where existing suffix equals incoming prefix."""
            max_check = min(len(existing), len(incoming))
            for size in range(max_check, 0, -1):
                if existing[-size:] == incoming[:size]:
                    return size
            return 0

        def _extract_item_id(params: dict) -> Optional[str]:
            """Extract item identifier from app-server delta params when available."""
            for key in ("itemId", "item_id", "id"):
                value = params.get(key)
                if value is not None and str(value).strip():
                    return str(value)
            item = params.get("item")
            if isinstance(item, dict):
                for key in ("itemId", "item_id", "id"):
                    value = item.get(key)
                    if value is not None and str(value).strip():
                        return str(value)
            return None

        def _extract_app_server_error_text(params: dict) -> str:
            """Best-effort extraction for v1/v2 app-server error notification payloads."""

            def _append_unique(parts: list[str], value: str) -> None:
                text = value.strip()
                if text and text not in parts:
                    parts.append(text)

            parts: list[str] = []
            message = params.get("message")
            if isinstance(message, str):
                _append_unique(parts, message)

            error_obj = params.get("error")
            if isinstance(error_obj, dict):
                nested_message = error_obj.get("message")
                if isinstance(nested_message, str):
                    _append_unique(parts, nested_message)

                details = error_obj.get("additionalDetails")
                if isinstance(details, str):
                    _append_unique(parts, details)

                codex_error_info = error_obj.get("codexErrorInfo")
                if isinstance(codex_error_info, str):
                    _append_unique(parts, f"codexErrorInfo={codex_error_info}")
                elif codex_error_info is not None:
                    _append_unique(
                        parts,
                        f"codexErrorInfo={json.dumps(codex_error_info, default=str)}",
                    )
            elif isinstance(error_obj, str):
                _append_unique(parts, error_obj)

            if not parts:
                return "Codex app-server error"
            return " | ".join(parts)

        async def handle_notification(method: str, params: dict) -> bool:
            nonlocal result_session_id, current_turn_id

            if method == "thread/started":
                thread = params.get("thread", {})
                thread_id = thread.get("id")
                if thread_id:
                    result_session_id = str(thread_id)
                    msg = parser.parse_line(
                        json.dumps({"type": "thread.started", "thread_id": str(thread_id)})
                    )
                    if msg:
                        return await handle_stream_message(msg)
                return False

            if method == "turn/started":
                turn = params.get("turn", {})
                turn_id = turn.get("id")
                if turn_id:
                    current_turn_id = str(turn_id)
                    if active_turn_state:
                        active_turn_state.turn_id = current_turn_id
                msg = parser.parse_line(json.dumps({"type": "turn.started"}))
                if msg:
                    await handle_stream_message(msg)
                return False

            if method in {"item/agentMessage/delta", "item/plan/delta"}:
                item_id = _extract_item_id(params)
                if item_id:
                    assistant_delta_item_ids.add(item_id)
                return await emit_assistant_delta(str(params.get("delta", "")))

            if method in {
                "item/reasoning/textDelta",
                "item/reasoning/summaryTextDelta",
                "item/reasoning/summaryPartAdded",
            }:
                # Reasoning deltas frequently contain internal scratchpad text.
                # Keep these out of user-facing assistant output.
                return False

            if method == "item/started":
                item = params.get("item", {})
                synthetic = {"type": "item.started", "item": item}
                msg = parser.parse_line(json.dumps(synthetic))
                if msg:
                    return await handle_stream_message(msg)
                return False

            if method == "item/completed":
                item = params.get("item", {})
                item_type = str(item.get("type", ""))
                item_identifier = item.get("id")
                item_id = str(item_identifier) if item_identifier is not None else ""
                if item_id:
                    delta_seen_for_item = item_id in assistant_delta_item_ids
                    assistant_delta_item_ids.discard(item_id)
                else:
                    delta_seen_for_item = False
                if item_type in {"agent_message", "agentMessage"}:
                    item_text_raw = item.get("text")
                    item_text = str(item_text_raw) if item_text_raw is not None else ""
                    # item/completed includes full text already streamed as deltas.
                    if item_text and accumulated_output.endswith(item_text):
                        logger.debug(
                            f"{log_prefix}Skipping duplicate completed assistant item "
                            f"{item_id or '<unknown>'}"
                        )
                        return False
                    if delta_seen_for_item and item_text:
                        overlap = _suffix_prefix_overlap(accumulated_output, item_text)
                        missing_tail = item_text[overlap:]
                        if missing_tail:
                            logger.debug(
                                f"{log_prefix}Repairing assistant item "
                                f"{item_id or '<unknown>'} by appending missing tail "
                                f"({len(missing_tail)} chars)"
                            )
                            return await emit_assistant_delta(missing_tail)
                        logger.debug(
                            f"{log_prefix}Skipping duplicate completed assistant item "
                            f"{item_id or '<unknown>'}"
                        )
                        return False
                synthetic = {"type": "item.completed", "item": item}
                msg = parser.parse_line(json.dumps(synthetic))
                if msg:
                    return await handle_stream_message(msg)
                return False

            if method == "turn/plan/updated":
                delta = params.get("text") or ""
                if delta:
                    return await emit_assistant_delta(str(delta))
                return False

            if method == "turn/diff/updated":
                # Diff updates are verbose raw patch data; rely on final assistant
                # message summaries instead of streaming this directly to Slack.
                return False

            if method == "turn/completed":
                turn = params.get("turn", {})
                status = str(turn.get("status", "")).lower()
                current_turn_id = None
                assistant_delta_item_ids.clear()
                if status in {"failed", "interrupted"}:
                    turn_error = turn.get("error", {})
                    error_text = (
                        turn_error.get("message", "Codex turn failed")
                        if isinstance(turn_error, dict)
                        else str(turn_error or "Codex turn failed")
                    )
                    msg = parser.parse_line(
                        json.dumps({"type": "turn.failed", "error": {"message": error_text}})
                    )
                else:
                    msg = parser.parse_line(
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "duration_ms": int((time.monotonic() - started_at) * 1000),
                            }
                        )
                    )
                if msg:
                    return await handle_stream_message(msg)
                return True

            if method == "error":
                error_text = _extract_app_server_error_text(params)
                if params.get("willRetry") is True:
                    logger.warning(
                        f"{log_prefix}Transient app-server error (willRetry=true): {error_text}"
                    )
                    return False
                msg = parser.parse_line(
                    json.dumps(
                        {
                            "type": "error",
                            "error": {"message": error_text},
                        }
                    )
                )
                if msg:
                    return await handle_stream_message(msg)
                return True

            return False

        async def handle_server_request(request: dict) -> None:
            method = request.get("method", "")
            request_id = request.get("id")
            params = request.get("params", {})

            if request_id is None:
                return

            if method == "item/tool/requestUserInput":
                item_id = str(params.get("itemId", f"request_{request_id}"))
                questions = params.get("questions", [])

                request_msg = parser.parse_line(
                    json.dumps(
                        {
                            "type": "request_user_input",
                            "call_id": item_id,
                            "questions": questions,
                        }
                    )
                )
                if request_msg:
                    await handle_stream_message(request_msg)

                response_payload = None
                if on_user_input_request:
                    response_payload = await on_user_input_request(
                        item_id,
                        {"questions": questions},
                    )
                if not isinstance(response_payload, dict):
                    response_payload = self._empty_user_input_response(questions)

                await send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": response_payload,
                    }
                )

                tool_result_msg = parser.parse_line(
                    json.dumps(
                        {
                            "type": "tool_result",
                            "tool_use_id": item_id,
                            "content": "User input received",
                            "is_error": False,
                        }
                    )
                )
                if tool_result_msg:
                    await handle_stream_message(tool_result_msg)
                return

            if method == "item/tool/call":
                tool_name = str(params.get("tool") or "unknown")
                call_id = str(params.get("callId") or f"request_{request_id}")
                response_payload = self._dynamic_tool_call_not_supported_response(tool_name)
                await send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": response_payload,
                    }
                )

                tool_result_msg = parser.parse_line(
                    json.dumps(
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": f"Dynamic tool call `{tool_name}` is not supported.",
                            "is_error": True,
                        }
                    )
                )
                if tool_result_msg:
                    await handle_stream_message(tool_result_msg)
                return

            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                response_payload = None
                if on_approval_request:
                    response_payload = await on_approval_request(method, params)

                if not self._is_valid_approval_response(method, response_payload):
                    response_payload = default_approval_payload(method, approval)

                await send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": response_payload,
                    }
                )
                return

            await send_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Unsupported app-server request method: {method}",
                    },
                }
            )

        async def handle_control_request(request: _ControlRequest) -> None:
            """Queue steer/interrupt RPC calls for the active turn."""
            if request.kind == "steer":
                await self._increment_metric("steer_requests")
                logger.info(
                    f"{log_prefix}event=turn_steer_requested scope={session_scope} "
                    f"thread_id={result_session_id or 'unknown'} turn_id={current_turn_id or 'unknown'}"
                )
                if not result_session_id or not current_turn_id:
                    await self._increment_metric("steer_failures")
                    if not request.future.done():
                        request.future.set_result(
                            TurnControlResult(
                                success=False,
                                error="No active turn available for steer",
                                turn_id=current_turn_id,
                            )
                        )
                    logger.warning(
                        f"{log_prefix}event=turn_steer_result success=false scope={session_scope} "
                        "reason=no_active_turn"
                    )
                    return
                steer_input = request.text or ""
                request_id = await send_request(
                    "turn/steer",
                    {
                        "threadId": result_session_id,
                        "expectedTurnId": current_turn_id,
                        "input": [{"type": "text", "text": steer_input}],
                    },
                )
                pending_control_responses[str(request_id)] = request
                return

            if request.kind == "interrupt":
                await self._increment_metric("interrupt_requests")
                logger.info(
                    f"{log_prefix}event=turn_interrupt_requested scope={session_scope} "
                    f"thread_id={result_session_id or 'unknown'} turn_id={current_turn_id or 'unknown'}"
                )
                if not result_session_id or not current_turn_id:
                    await self._increment_metric("interrupt_failures")
                    if not request.future.done():
                        request.future.set_result(
                            TurnControlResult(
                                success=False,
                                error="No active turn available for interrupt",
                                turn_id=current_turn_id,
                            )
                        )
                    logger.warning(
                        f"{log_prefix}event=turn_interrupt_result success=false scope={session_scope} "
                        "reason=no_active_turn"
                    )
                    return
                request_id = await send_request(
                    "turn/interrupt",
                    {
                        "threadId": result_session_id,
                        "turnId": current_turn_id,
                    },
                )
                pending_control_responses[str(request_id)] = request
                return

            if not request.future.done():
                request.future.set_result(
                    TurnControlResult(
                        success=False, error=f"Unsupported control kind: {request.kind}"
                    )
                )

        async def process_rpc_message(rpc: dict) -> bool:
            response_id = rpc.get("id")
            if response_id is not None and ("result" in rpc or "error" in rpc):
                cache_key = str(response_id)
                control_request = pending_control_responses.pop(cache_key, None)
                if control_request:
                    if not control_request.future.done():
                        if rpc.get("error"):
                            await self._increment_metric(f"{control_request.kind}_failures")
                            logger.warning(
                                f"{log_prefix}event=turn_{control_request.kind}_result success=false "
                                f"scope={session_scope} turn_id={current_turn_id or 'unknown'} "
                                f"error={rpc.get('error')}"
                            )
                            control_request.future.set_result(
                                TurnControlResult(
                                    success=False,
                                    error=str(rpc.get("error")),
                                    turn_id=current_turn_id,
                                )
                            )
                        else:
                            result_payload = rpc.get("result", {})
                            turn_id = result_payload.get("turnId") or current_turn_id
                            if turn_id:
                                if active_turn_state:
                                    active_turn_state.turn_id = str(turn_id)
                                await self._increment_metric(f"{control_request.kind}_successes")
                                logger.info(
                                    f"{log_prefix}event=turn_{control_request.kind}_result success=true "
                                    f"scope={session_scope} turn_id={turn_id}"
                                )
                                control_request.future.set_result(
                                    TurnControlResult(
                                        success=True,
                                        message=f"{control_request.kind} accepted",
                                        turn_id=str(turn_id),
                                    )
                                )
                            else:
                                await self._increment_metric(f"{control_request.kind}_successes")
                                logger.info(
                                    f"{log_prefix}event=turn_{control_request.kind}_result success=true "
                                    f"scope={session_scope} turn_id={current_turn_id or 'unknown'}"
                                )
                                control_request.future.set_result(
                                    TurnControlResult(
                                        success=True,
                                        message=f"{control_request.kind} accepted",
                                        turn_id=current_turn_id,
                                    )
                                )
                    return False
                response_cache[cache_key] = rpc
                return False

            method = rpc.get("method")
            if not method:
                return False

            if response_id is not None and "params" in rpc:
                await handle_server_request(rpc)
                return False

            if method.startswith("codex/event/"):
                return False

            return await handle_notification(method, rpc.get("params", {}))

        async def read_rpc_line() -> dict:
            if process.stdout is None:
                raise RuntimeError("app-server stdout is unavailable")
            line = await process.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed the stream unexpectedly")
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                return {}
            try:
                return json.loads(line_str)
            except json.JSONDecodeError as e:
                logger.warning(f"{log_prefix}Failed to parse app-server JSON line: {e}")
                return {}

        async def await_response(request_id: int) -> dict:
            while True:
                cache_key = str(request_id)
                if cache_key in response_cache:
                    return response_cache.pop(cache_key)
                rpc = await read_rpc_line()
                if not rpc:
                    continue
                await process_rpc_message(rpc)

        try:
            init_req_id = await send_request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "slack-claude-code",
                        "version": "1.0",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            )
            init_resp = await await_response(init_req_id)
            if init_resp.get("error"):
                raise RuntimeError(f"initialize failed: {init_resp['error']}")

            thread_params: dict[str, Any] = {
                "cwd": working_directory,
                "approvalPolicy": approval,
                "sandbox": sandbox,
            }
            if base_model:
                thread_params["model"] = base_model

            thread_method = "thread/start"
            if resume_session_id:
                thread_method = "thread/resume"
                thread_params["threadId"] = resume_session_id
                logger.info(f"{log_prefix}Resuming session via app-server: {resume_session_id}")

            thread_req_id = await send_request(thread_method, thread_params)
            thread_resp = await await_response(thread_req_id)

            if (
                thread_resp.get("error")
                and resume_session_id
                and self._is_missing_thread_error(thread_resp["error"])
            ):
                logger.info(
                    f"{log_prefix}Session {resume_session_id} not found, "
                    "retrying with a new thread"
                )
                await terminate_process_safely(process, timeout=2.0)
                return await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=None,
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    on_user_input_request=on_user_input_request,
                    on_approval_request=on_approval_request,
                    permission_mode=permission_mode,
                    sandbox_mode=sandbox,
                    approval_mode=approval,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    _recursion_depth=_recursion_depth + 1,
                )

            if thread_resp.get("error"):
                raise RuntimeError(f"{thread_method} failed: {thread_resp['error']}")

            thread = (thread_resp.get("result") or {}).get("thread", {})
            result_session_id = str(thread.get("id") or result_session_id or "")

            turn_params: dict[str, Any] = {
                "threadId": result_session_id,
                "input": [{"type": "text", "text": effective_prompt}],
            }
            if effort:
                turn_params["effort"] = effort
            mode = (permission_mode or "").strip().lower()
            collaboration_model = base_model
            if not collaboration_model:
                response_model = (thread_resp.get("result") or {}).get("model")
                if isinstance(response_model, str) and response_model.strip():
                    collaboration_model = response_model
            collaboration_settings: dict[str, Any] | None = None
            if collaboration_model:
                collaboration_settings = {
                    "model": collaboration_model,
                    "reasoning_effort": effort,
                    "developer_instructions": None,
                }
            if mode == "plan":
                if collaboration_settings:
                    turn_params["collaborationMode"] = {
                        "mode": "plan",
                        "settings": collaboration_settings,
                    }
            elif mode:
                # Explicitly set default mode so resumed plan threads exit plan mode.
                if collaboration_settings:
                    turn_params["collaborationMode"] = {
                        "mode": "default",
                        "settings": collaboration_settings,
                    }

            turn_req_id = await send_request("turn/start", turn_params)
            turn_resp = await await_response(turn_req_id)
            if turn_resp.get("error"):
                raise RuntimeError(f"turn/start failed: {turn_resp['error']}")

            turn_obj = (turn_resp.get("result") or {}).get("turn", {})
            current_turn_id = str(turn_obj.get("id") or "") or None
            if not current_turn_id:
                logger.warning(f"{log_prefix}turn/start did not return a turn id")

            if current_turn_id:
                active_turn_state = _ActiveTurnState(
                    scope=session_scope,
                    track_id=track_id,
                    thread_id=result_session_id,
                    turn_id=current_turn_id,
                    control_queue=control_queue,
                )
                async with self._lock:
                    self._active_turns_by_scope[session_scope] = active_turn_state
                    self._active_turns_by_track[track_id] = active_turn_state
                await self._increment_metric("turn_start_registered")
                logger.info(
                    f"{log_prefix}event=turn_start_registered scope={session_scope} "
                    f"thread_id={result_session_id} turn_id={current_turn_id} track_id={track_id}"
                )

            is_final = False
            while not is_final:
                rpc_task = asyncio.create_task(read_rpc_line())
                control_task = asyncio.create_task(control_queue.get())
                done, pending = await asyncio.wait(
                    {rpc_task, control_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for pending_task in pending:
                    pending_task.cancel()
                if rpc_task in done:
                    rpc = rpc_task.result()
                    if rpc:
                        is_final = await process_rpc_message(rpc)
                if control_task in done:
                    control_request = control_task.result()
                    await handle_control_request(control_request)

            await terminate_process_safely(process, timeout=2.0)

            stderr = await process.stderr.read() if process.stderr else b""
            if stderr:
                stderr_str = stderr.decode("utf-8", errors="replace").strip()
                if stderr_str:
                    logger.warning(f"{log_prefix}codex app-server stderr: {stderr_str}")

            success = not error_msg
            return ExecutionResult(
                success=success,
                output=accumulated_output,
                detailed_output=accumulated_detailed,
                session_id=result_session_id,
                error=error_msg,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
            )

        except asyncio.CancelledError:
            for request in pending_control_responses.values():
                if not request.future.done():
                    request.future.set_result(
                        TurnControlResult(
                            success=False,
                            error="Execution cancelled",
                            turn_id=current_turn_id,
                        )
                    )
            await terminate_process_safely(process)
            return ExecutionResult(
                success=False,
                output=accumulated_output,
                detailed_output=accumulated_detailed,
                session_id=result_session_id,
                error="Cancelled",
                was_cancelled=True,
            )
        except Exception as e:
            logger.error(f"{log_prefix}Error during app-server execution: {e}")
            for request in pending_control_responses.values():
                if not request.future.done():
                    request.future.set_result(
                        TurnControlResult(
                            success=False,
                            error=f"Execution failed: {e}",
                            turn_id=current_turn_id,
                        )
                    )
            await terminate_process_safely(process)
            return ExecutionResult(
                success=False,
                output=accumulated_output,
                detailed_output=accumulated_detailed,
                session_id=result_session_id,
                error=str(e),
            )
        finally:
            for request in pending_control_responses.values():
                if not request.future.done():
                    request.future.set_result(
                        TurnControlResult(
                            success=False,
                            error="Execution ended before control request completed",
                            turn_id=current_turn_id,
                        )
                    )
            await self._registry.unregister(track_id=track_id, execution_id=execution_id)
            async with self._lock:
                if self._active_turns_by_track.get(track_id):
                    self._active_turns_by_track.pop(track_id, None)
                scope_state = self._active_turns_by_scope.get(session_scope)
                if scope_state and scope_state.track_id == track_id:
                    self._active_turns_by_scope.pop(session_scope, None)
            if active_turn_state:
                active_turn_state.done_event.set()
                await self._increment_metric("turn_state_cleared")
                logger.info(
                    f"{log_prefix}event=turn_state_cleared scope={session_scope} "
                    f"thread_id={active_turn_state.thread_id} "
                    f"turn_id={active_turn_state.turn_id} track_id={track_id}"
                )

    @staticmethod
    def _resolve_sandbox_mode(mode: Optional[str], log_prefix: str) -> str:
        """Return validated sandbox mode."""
        resolved = mode or config.CODEX_SANDBOX_MODE
        if resolved not in config.VALID_SANDBOX_MODES:
            logger.warning(
                f"{log_prefix}Invalid sandbox mode: {resolved}, "
                f"using {config.CODEX_SANDBOX_MODE}"
            )
            return config.CODEX_SANDBOX_MODE
        logger.info(f"{log_prefix}Using sandbox mode: {resolved}")
        return resolved

    @staticmethod
    def _resolve_approval_mode(mode: Optional[str], log_prefix: str) -> str:
        """Return validated approval mode."""
        normalized = normalize_codex_approval_mode(mode or config.CODEX_APPROVAL_MODE)
        if normalized not in config.VALID_APPROVAL_MODES:
            logger.warning(
                f"{log_prefix}Invalid approval mode: {normalized}, "
                f"using {config.CODEX_APPROVAL_MODE}"
            )
            return normalize_codex_approval_mode(config.CODEX_APPROVAL_MODE)
        logger.info(f"{log_prefix}Using approval mode: {normalized}")
        return normalized

    @staticmethod
    def _is_missing_thread_error(error: Any) -> bool:
        """Return True when an app-server resume error indicates thread/session missing."""
        error_text = str(error).lower()
        markers = (
            "thread not found",
            "session not found",
            "no conversation found",
            "unknown thread",
        )
        return any(marker in error_text for marker in markers)

    @staticmethod
    def _is_valid_approval_response(method: str, payload: Any) -> bool:
        """Validate approval payload shape for known request methods."""
        if not isinstance(payload, dict):
            return False
        decision = payload.get("decision")
        if decision is None:
            return False

        normalized_method = (method or "").strip()

        if normalized_method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            simple_decisions = {"accept", "acceptForSession", "decline", "cancel"}
            if isinstance(decision, str):
                return decision in simple_decisions
            if isinstance(decision, dict):
                # Allow advanced app-server decision payloads.
                return bool(decision)
            return False

        return False

    @staticmethod
    def _dynamic_tool_call_not_supported_response(tool_name: str) -> dict:
        """Return a schema-valid failure payload for unsupported dynamic tool calls."""
        return {
            "success": False,
            "contentItems": [
                {
                    "type": "inputText",
                    "text": (
                        f"Dynamic tool call `{tool_name}` is not supported in this Slack "
                        "integration."
                    ),
                }
            ],
        }

    @staticmethod
    def _empty_user_input_response(questions: list[dict]) -> dict:
        """Return a schema-compatible empty answer payload for request_user_input."""
        answers: dict[str, dict[str, list[str]]] = {}
        for i, question in enumerate(questions):
            question_id = str(question.get("id", f"q_{i + 1}"))
            answers[question_id] = {"answers": []}
        return {"answers": answers}

    def _build_effective_prompt(self, prompt: str, log_prefix: str) -> str:
        """Apply Codex default instructions preamble, if configured."""
        if not config.CODEX_PREPEND_DEFAULT_INSTRUCTIONS:
            return prompt

        preamble_path = Path(config.CODEX_DEFAULT_INSTRUCTIONS_FILE).expanduser()
        try:
            preamble = preamble_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.debug(f"{log_prefix}No default Codex instructions file at {preamble_path}")
            return prompt
        except Exception as e:
            logger.warning(
                f"{log_prefix}Failed reading Codex instructions file {preamble_path}: {e}"
            )
            return prompt

        if not preamble:
            return prompt
        if prompt:
            return f"{preamble}\n\n{prompt}"
        return preamble

    async def has_active_turn(self, session_scope: str) -> bool:
        """Return True when a turn is currently active for the session scope."""
        async with self._lock:
            active = self._active_turns_by_scope.get(session_scope)
            return bool(active and not active.done_event.is_set())

    async def get_active_turn(self, session_scope: str) -> Optional[dict]:
        """Return metadata for the active turn in the scope, if any."""
        async with self._lock:
            active = self._active_turns_by_scope.get(session_scope)
            if not active or active.done_event.is_set():
                return None
            return {
                "scope": active.scope,
                "track_id": active.track_id,
                "thread_id": active.thread_id,
                "turn_id": active.turn_id,
                "started_at": active.started_at,
            }

    async def _enqueue_control(
        self,
        session_scope: str,
        kind: str,
        text: Optional[str] = None,
        timeout: float = 5.0,
    ) -> TurnControlResult:
        """Send a steer/interrupt request to the active turn loop."""
        async with self._lock:
            active = self._active_turns_by_scope.get(session_scope)
            if not active or active.done_event.is_set():
                logger.debug(
                    f"event=turn_{kind}_enqueue_skipped scope={session_scope} reason=no_active_turn"
                )
                return TurnControlResult(success=False, error="No active turn", turn_id=None)
            loop = asyncio.get_running_loop()
            future: asyncio.Future[TurnControlResult] = loop.create_future()
            request = _ControlRequest(kind=kind, text=text, future=future)
            await active.control_queue.put(request)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            await self._increment_metric(f"{kind}_timeouts")
            await self._increment_metric(f"{kind}_failures")
            return TurnControlResult(
                success=False,
                error=f"{kind} request timed out",
                turn_id=active.turn_id,
            )

    async def _wait_for_turn_settle(self, session_scope: str, timeout: float) -> bool:
        """Wait for an active turn in scope to finish after an interrupt request."""
        async with self._lock:
            active = self._active_turns_by_scope.get(session_scope)
            if not active or active.done_event.is_set():
                return True
            done_event = active.done_event
        try:
            await asyncio.wait_for(done_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def steer_active_turn(
        self,
        session_scope: str,
        text: str,
        timeout: float = 5.0,
    ) -> TurnControlResult:
        """Send `turn/steer` for the currently active turn in the scope."""
        return await self._enqueue_control(session_scope, kind="steer", text=text, timeout=timeout)

    async def interrupt_active_turn(
        self,
        session_scope: str,
        timeout: float = 5.0,
    ) -> TurnControlResult:
        """Send `turn/interrupt` for the currently active turn in the scope."""
        return await self._enqueue_control(session_scope, kind="interrupt", timeout=timeout)

    async def _rpc_call(
        self,
        method: str,
        params: dict,
        working_directory: str = "~",
    ) -> dict:
        """Execute a single app-server RPC method call and return its result payload."""
        cmd = ["codex", "app-server", "--listen", "stdio://"]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_directory,
            limit=50 * 1024 * 1024,
        )
        next_request_id = 1
        response_cache: dict[str, dict] = {}

        async def send_rpc(payload: dict) -> None:
            if process.stdin is None:
                raise RuntimeError("app-server stdin is unavailable")
            process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await process.stdin.drain()

        async def send_request(request_method: str, request_params: dict) -> int:
            nonlocal next_request_id
            request_id = next_request_id
            next_request_id += 1
            await send_rpc(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": request_method,
                    "params": request_params,
                }
            )
            return request_id

        async def read_rpc_line() -> dict:
            if process.stdout is None:
                raise RuntimeError("app-server stdout is unavailable")
            line = await process.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server closed the stream unexpectedly")
            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                return {}
            try:
                return json.loads(line_str)
            except json.JSONDecodeError:
                return {}

        async def process_rpc_message(rpc: dict) -> None:
            response_id = rpc.get("id")
            if response_id is not None and ("result" in rpc or "error" in rpc):
                response_cache[str(response_id)] = rpc
                return
            method_name = rpc.get("method")
            if not method_name:
                return
            if response_id is not None and "params" in rpc:
                # This executor path is metadata/introspection only; reject server requests.
                await send_rpc(
                    {
                        "jsonrpc": "2.0",
                        "id": response_id,
                        "error": {
                            "code": -32601,
                            "message": f"Unsupported app-server request method: {method_name}",
                        },
                    }
                )

        async def await_response(request_id: int) -> dict:
            cache_key = str(request_id)
            while True:
                if cache_key in response_cache:
                    return response_cache.pop(cache_key)
                rpc = await read_rpc_line()
                if not rpc:
                    continue
                await process_rpc_message(rpc)

        try:
            init_id = await send_request(
                "initialize",
                {
                    "clientInfo": {"name": "slack-claude-code", "version": "1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            init_response = await await_response(init_id)
            if init_response.get("error"):
                raise RuntimeError(f"initialize failed: {init_response['error']}")

            request_id = await send_request(method, params)
            response = await await_response(request_id)
            if response.get("error"):
                raise RuntimeError(str(response["error"]))
            return response.get("result", {})
        finally:
            await terminate_process_safely(process, timeout=2.0)

    async def thread_list(
        self,
        working_directory: str,
        limit: int = 20,
        archived: Optional[bool] = False,
    ) -> dict:
        """List persisted Codex threads."""
        return await self._rpc_call(
            "thread/list",
            {"limit": limit, "archived": archived},
            working_directory=working_directory,
        )

    async def thread_read(
        self,
        thread_id: str,
        working_directory: str,
        include_turns: bool = True,
    ) -> dict:
        """Read a specific thread."""
        return await self._rpc_call(
            "thread/read",
            {"threadId": thread_id, "includeTurns": include_turns},
            working_directory=working_directory,
        )

    async def thread_archive(self, thread_id: str, working_directory: str) -> dict:
        """Archive a thread."""
        return await self._rpc_call(
            "thread/archive",
            {"threadId": thread_id},
            working_directory=working_directory,
        )

    async def thread_unarchive(self, thread_id: str, working_directory: str) -> dict:
        """Unarchive a thread."""
        return await self._rpc_call(
            "thread/unarchive",
            {"threadId": thread_id},
            working_directory=working_directory,
        )

    async def thread_fork(self, thread_id: str, working_directory: str) -> dict:
        """Fork a thread."""
        return await self._rpc_call(
            "thread/fork",
            {"threadId": thread_id},
            working_directory=working_directory,
        )

    async def thread_rollback(self, thread_id: str, num_turns: int, working_directory: str) -> dict:
        """Rollback a thread by dropping the most recent turns."""
        return await self._rpc_call(
            "thread/rollback",
            {"threadId": thread_id, "numTurns": max(1, num_turns)},
            working_directory=working_directory,
        )

    async def thread_compact_start(self, thread_id: str, working_directory: str) -> dict:
        """Start context compaction for a thread."""
        return await self._rpc_call(
            "thread/compact/start",
            {"threadId": thread_id},
            working_directory=working_directory,
        )

    async def review_start(self, thread_id: str, target: dict, working_directory: str) -> dict:
        """Start a Codex review for the current session thread."""
        return await self._rpc_call(
            "review/start",
            {"threadId": thread_id, "target": target},
            working_directory=working_directory,
        )

    async def model_list(self, working_directory: str) -> dict:
        """Return available models from app-server."""
        return await self._rpc_call("model/list", {}, working_directory=working_directory)

    async def account_read(self, working_directory: str) -> dict:
        """Return account metadata."""
        return await self._rpc_call("account/read", {}, working_directory=working_directory)

    async def config_read(self, working_directory: str) -> dict:
        """Return resolved config from app-server."""
        return await self._rpc_call("config/read", {}, working_directory=working_directory)

    async def config_requirements_read(self, working_directory: str) -> dict:
        """Return runtime config requirements from app-server."""
        return await self._rpc_call(
            "configRequirements/read", {}, working_directory=working_directory
        )

    async def experimental_feature_list(self, working_directory: str) -> dict:
        """Return server experimental feature list."""
        return await self._rpc_call(
            "experimentalFeature/list", {}, working_directory=working_directory
        )

    async def mcp_server_status_list(self, working_directory: str) -> dict:
        """Return MCP server status from app-server."""
        return await self._rpc_call("mcpServerStatus/list", {}, working_directory=working_directory)

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an active execution."""
        tracked = await self._registry.pop_for_execution(execution_id)
        if tracked is None:
            return False

        scope = tracked.session_scope
        if scope:
            await self.interrupt_active_turn(scope, timeout=1.0)
            await self._wait_for_turn_settle(scope, timeout=1.5)

        async with self._lock:
            active_turn = self._active_turns_by_track.pop(tracked.track_id, None)
            if active_turn:
                scope_state = self._active_turns_by_scope.get(active_turn.scope)
                if scope_state and scope_state.track_id == tracked.track_id:
                    self._active_turns_by_scope.pop(active_turn.scope, None)
                active_turn.done_event.set()

        await terminate_process_safely(tracked.process)
        return True

    async def cancel_by_scope(self, session_scope: str) -> int:
        """Cancel active executions for a channel/thread session scope."""
        initial_count = await self._registry.count_for_scope(session_scope)
        await self.interrupt_active_turn(session_scope, timeout=1.0)
        await self._wait_for_turn_settle(session_scope, timeout=1.5)

        tracked = await self._registry.pop_for_scope(session_scope)
        async with self._lock:
            for entry in tracked:
                active_turn = self._active_turns_by_track.pop(entry.track_id, None)
                if active_turn:
                    active_turn.done_event.set()
                    scope_state = self._active_turns_by_scope.get(active_turn.scope)
                    if scope_state and scope_state.track_id == entry.track_id:
                        self._active_turns_by_scope.pop(active_turn.scope, None)

        await terminate_processes(entry.process for entry in tracked)
        return max(len(tracked), initial_count)

    async def cancel_by_channel(self, channel_id: str) -> int:
        """Cancel all active executions for a specific channel.

        Args:
            channel_id: The Slack channel ID to cancel executions for.

        Returns:
            Number of processes cancelled.
        """
        initial_count = await self._registry.count_for_channel(channel_id)
        channel_scopes = await self._registry.scopes_for_channel(channel_id)
        for scope in channel_scopes:
            await self.interrupt_active_turn(scope, timeout=1.0)
            await self._wait_for_turn_settle(scope, timeout=1.5)

        tracked = await self._registry.pop_for_channel(channel_id)
        async with self._lock:
            for entry in tracked:
                active_turn = self._active_turns_by_track.pop(entry.track_id, None)
                if active_turn:
                    active_turn.done_event.set()
                    scope_state = self._active_turns_by_scope.get(active_turn.scope)
                    if scope_state and scope_state.track_id == entry.track_id:
                        self._active_turns_by_scope.pop(active_turn.scope, None)

        await terminate_processes(entry.process for entry in tracked)
        return max(len(tracked), initial_count)

    async def cancel_all(self) -> int:
        """Cancel all active executions."""
        initial_count = len(self._active_processes)
        active_scopes = list(self._active_turns_by_scope.keys())
        for scope in active_scopes:
            await self.interrupt_active_turn(scope, timeout=1.0)
            await self._wait_for_turn_settle(scope, timeout=1.5)

        tracked = await self._registry.pop_all()
        async with self._lock:
            for active_turn in self._active_turns_by_track.values():
                active_turn.done_event.set()
            self._active_turns_by_scope.clear()
            self._active_turns_by_track.clear()
        await terminate_processes(entry.process for entry in tracked)
        return max(len(tracked), initial_count)

    async def shutdown(self) -> None:
        """Shutdown and cancel all active executions."""
        await self.cancel_all()
