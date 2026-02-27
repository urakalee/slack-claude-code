"""Codex app-server executor using subprocess JSON-RPC over stdio."""

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from loguru import logger

from src.codex.approval_bridge import default_approval_payload
from src.codex.capabilities import normalize_codex_approval_mode
from src.config import config, parse_model_effort
from src.utils.execution_scope import build_session_scope
from src.utils.process_utils import terminate_process_safely
from src.utils.stream_models import concat_with_spacing

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
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._process_channels: dict[str, str] = {}  # track_id -> channel_id
        self._process_scopes: dict[str, str] = {}  # track_id -> session_scope
        self._execution_track_ids: dict[str, str] = {}  # execution_id -> track_id
        self._active_turns_by_scope: dict[str, _ActiveTurnState] = {}
        self._active_turns_by_track: dict[str, _ActiveTurnState] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self.db = db

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        on_user_input_request: Optional[
            Callable[[str, dict], Awaitable[Optional[dict]]]
        ] = None,
        on_approval_request: Optional[
            Callable[[str, dict], Awaitable[Optional[dict]]]
        ] = None,
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
        on_user_input_request: Optional[
            Callable[[str, dict], Awaitable[Optional[dict]]]
        ],
        on_approval_request: Optional[Callable[[str, dict], Awaitable[Optional[dict]]]],
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

        track_id = execution_id or session_id or "default"
        if channel_id:
            track_id = f"{channel_id}_{track_id}"
        session_scope = build_session_scope(channel_id or "", thread_ts)
        async with self._lock:
            self._active_processes[track_id] = process
            if channel_id:
                self._process_channels[track_id] = channel_id
            self._process_scopes[track_id] = session_scope
            if execution_id:
                self._execution_track_ids[execution_id] = track_id

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
                preview = (
                    msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
                )
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
                duration_display = (
                    msg.duration_ms if msg.duration_ms is not None else "?"
                )
                logger.info(
                    f"{log_prefix}Codex Finished - completed in {duration_display}ms"
                )

            if msg.session_id:
                result_session_id = msg.session_id

            if msg.type == "assistant" and msg.content:
                accumulated_output = concat_with_spacing(
                    accumulated_output, msg.content
                )

            if msg.type == "result":
                cost_usd = msg.cost_usd
                duration_ms = msg.duration_ms
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

        async def handle_notification(method: str, params: dict) -> bool:
            nonlocal result_session_id, current_turn_id

            if method == "thread/started":
                thread = params.get("thread", {})
                thread_id = thread.get("id")
                if thread_id:
                    result_session_id = str(thread_id)
                    msg = parser.parse_line(
                        json.dumps(
                            {"type": "thread.started", "thread_id": str(thread_id)}
                        )
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
                return await emit_assistant_delta(str(params.get("delta", "")))

            if method in {
                "item/reasoning/textDelta",
                "item/reasoning/summaryTextDelta",
                "item/reasoning/summaryPartAdded",
            }:
                # Keep deltas visible in streaming output for better live progress.
                return await emit_assistant_delta(str(params.get("delta", "")))

            if method == "item/started":
                item = params.get("item", {})
                synthetic = {"type": "item.started", "item": item}
                msg = parser.parse_line(json.dumps(synthetic))
                if msg:
                    return await handle_stream_message(msg)
                return False

            if method == "item/completed":
                item = params.get("item", {})
                synthetic = {"type": "item.completed", "item": item}
                msg = parser.parse_line(json.dumps(synthetic))
                if msg:
                    return await handle_stream_message(msg)
                return False

            if method in {"turn/plan/updated", "turn/diff/updated"}:
                delta = params.get("diff") or params.get("text") or ""
                if delta:
                    return await emit_assistant_delta(str(delta))
                return False

            if method == "turn/completed":
                turn = params.get("turn", {})
                status = str(turn.get("status", "")).lower()
                current_turn_id = None
                if status in {"failed", "interrupted"}:
                    turn_error = turn.get("error", {})
                    error_text = (
                        turn_error.get("message", "Codex turn failed")
                        if isinstance(turn_error, dict)
                        else str(turn_error or "Codex turn failed")
                    )
                    msg = parser.parse_line(
                        json.dumps(
                            {"type": "turn.failed", "error": {"message": error_text}}
                        )
                    )
                else:
                    msg = parser.parse_line(
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "duration_ms": int(
                                    (time.monotonic() - started_at) * 1000
                                ),
                            }
                        )
                    )
                if msg:
                    return await handle_stream_message(msg)
                return True

            if method == "error":
                msg = parser.parse_line(
                    json.dumps(
                        {
                            "type": "error",
                            "error": params.get("message", "Codex app-server error"),
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

            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
                "skill/requestApproval",
                "execCommandApproval",
                "applyPatchApproval",
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
                if not result_session_id or not current_turn_id:
                    if not request.future.done():
                        request.future.set_result(
                            TurnControlResult(
                                success=False,
                                error="No active turn available for steer",
                                turn_id=current_turn_id,
                            )
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
                if not result_session_id or not current_turn_id:
                    if not request.future.done():
                        request.future.set_result(
                            TurnControlResult(
                                success=False,
                                error="No active turn available for interrupt",
                                turn_id=current_turn_id,
                            )
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
                                control_request.future.set_result(
                                    TurnControlResult(
                                        success=True,
                                        message=f"{control_request.kind} accepted",
                                        turn_id=str(turn_id),
                                    )
                                )
                            else:
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
                logger.info(
                    f"{log_prefix}Resuming session via app-server: {resume_session_id}"
                )

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
            async with self._lock:
                self._active_processes.pop(track_id, None)
                self._process_channels.pop(track_id, None)
                self._process_scopes.pop(track_id, None)
                if (
                    execution_id
                    and self._execution_track_ids.get(execution_id) == track_id
                ):
                    self._execution_track_ids.pop(execution_id, None)
                if self._active_turns_by_track.get(track_id):
                    self._active_turns_by_track.pop(track_id, None)
                scope_state = self._active_turns_by_scope.get(session_scope)
                if scope_state and scope_state.track_id == track_id:
                    self._active_turns_by_scope.pop(session_scope, None)
            if active_turn_state:
                active_turn_state.done_event.set()

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

        if normalized_method == "skill/requestApproval":
            return decision in {"approve", "decline"}

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

        if normalized_method in {"execCommandApproval", "applyPatchApproval"}:
            return decision in {"approved", "denied"}

        return True

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
            logger.debug(
                f"{log_prefix}No default Codex instructions file at {preamble_path}"
            )
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
                return TurnControlResult(
                    success=False, error="No active turn", turn_id=None
                )
            loop = asyncio.get_running_loop()
            future: asyncio.Future[TurnControlResult] = loop.create_future()
            request = _ControlRequest(kind=kind, text=text, future=future)
            await active.control_queue.put(request)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
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
        return await self._enqueue_control(
            session_scope, kind="steer", text=text, timeout=timeout
        )

    async def interrupt_active_turn(
        self,
        session_scope: str,
        timeout: float = 5.0,
    ) -> TurnControlResult:
        """Send `turn/interrupt` for the currently active turn in the scope."""
        return await self._enqueue_control(
            session_scope, kind="interrupt", timeout=timeout
        )

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

    async def thread_rollback(
        self, thread_id: str, num_turns: int, working_directory: str
    ) -> dict:
        """Rollback a thread by dropping the most recent turns."""
        return await self._rpc_call(
            "thread/rollback",
            {"threadId": thread_id, "numTurns": max(1, num_turns)},
            working_directory=working_directory,
        )

    async def thread_compact_start(
        self, thread_id: str, working_directory: str
    ) -> dict:
        """Start context compaction for a thread."""
        return await self._rpc_call(
            "thread/compact/start",
            {"threadId": thread_id},
            working_directory=working_directory,
        )

    async def review_start(
        self, thread_id: str, target: dict, working_directory: str
    ) -> dict:
        """Start a Codex review for the current session thread."""
        return await self._rpc_call(
            "review/start",
            {"threadId": thread_id, "target": target},
            working_directory=working_directory,
        )

    async def model_list(self, working_directory: str) -> dict:
        """Return available models from app-server."""
        return await self._rpc_call(
            "model/list", {}, working_directory=working_directory
        )

    async def account_read(self, working_directory: str) -> dict:
        """Return account metadata."""
        return await self._rpc_call(
            "account/read", {}, working_directory=working_directory
        )

    async def config_read(self, working_directory: str) -> dict:
        """Return resolved config from app-server."""
        return await self._rpc_call(
            "config/read", {}, working_directory=working_directory
        )

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
        return await self._rpc_call(
            "mcpServerStatus/list", {}, working_directory=working_directory
        )

    async def cancel(self, execution_id: str) -> bool:
        """Cancel an active execution."""
        scope = None
        async with self._lock:
            process_key = None
            if execution_id in self._active_processes:
                process_key = execution_id
            else:
                mapped_track = self._execution_track_ids.get(execution_id)
                if mapped_track and mapped_track in self._active_processes:
                    process_key = mapped_track

            if process_key is None:
                return False
            scope = self._process_scopes.get(process_key)

        if scope:
            await self.interrupt_active_turn(scope, timeout=1.0)
            await self._wait_for_turn_settle(scope, timeout=1.5)

        async with self._lock:
            process_key = None
            if execution_id in self._active_processes:
                process_key = execution_id
            else:
                mapped_track = self._execution_track_ids.get(execution_id)
                if mapped_track and mapped_track in self._active_processes:
                    process_key = mapped_track
            if process_key is None:
                return True
            process = self._active_processes.pop(process_key)
            self._process_channels.pop(process_key, None)
            self._process_scopes.pop(process_key, None)
            active_turn = self._active_turns_by_track.pop(process_key, None)
            if active_turn:
                scope_state = self._active_turns_by_scope.get(active_turn.scope)
                if scope_state and scope_state.track_id == process_key:
                    self._active_turns_by_scope.pop(active_turn.scope, None)
                active_turn.done_event.set()

            mapped_track_id = self._execution_track_ids.get(execution_id)
            if mapped_track_id == process_key:
                self._execution_track_ids.pop(execution_id, None)

        await terminate_process_safely(process)
        return True

    async def cancel_by_scope(self, session_scope: str) -> int:
        """Cancel active executions for a channel/thread session scope."""
        async with self._lock:
            initial_count = sum(
                1 for scope in self._process_scopes.values() if scope == session_scope
            )
        await self.interrupt_active_turn(session_scope, timeout=1.0)
        await self._wait_for_turn_settle(session_scope, timeout=1.5)

        async with self._lock:
            track_ids_to_cancel = [
                track_id
                for track_id, scope in self._process_scopes.items()
                if scope == session_scope
            ]
            processes = []
            for track_id in track_ids_to_cancel:
                process = self._active_processes.pop(track_id, None)
                if process:
                    processes.append(process)
                self._process_channels.pop(track_id, None)
                self._process_scopes.pop(track_id, None)
                active_turn = self._active_turns_by_track.pop(track_id, None)
                if active_turn:
                    active_turn.done_event.set()
                    scope_state = self._active_turns_by_scope.get(active_turn.scope)
                    if scope_state and scope_state.track_id == track_id:
                        self._active_turns_by_scope.pop(active_turn.scope, None)
            for execution_id, track_id in list(self._execution_track_ids.items()):
                if track_id in track_ids_to_cancel:
                    self._execution_track_ids.pop(execution_id, None)

        if processes:
            await asyncio.gather(
                *(terminate_process_safely(process) for process in processes),
                return_exceptions=True,
            )
        return max(len(processes), initial_count)

    async def cancel_by_channel(self, channel_id: str) -> int:
        """Cancel all active executions for a specific channel.

        Args:
            channel_id: The Slack channel ID to cancel executions for.

        Returns:
            Number of processes cancelled.
        """
        async with self._lock:
            initial_count = sum(
                1 for ch_id in self._process_channels.values() if ch_id == channel_id
            )
        async with self._lock:
            channel_scopes = {
                scope
                for track_id, scope in self._process_scopes.items()
                if self._process_channels.get(track_id) == channel_id
            }
        for scope in channel_scopes:
            await self.interrupt_active_turn(scope, timeout=1.0)
            await self._wait_for_turn_settle(scope, timeout=1.5)

        async with self._lock:
            track_ids_to_cancel = [
                track_id
                for track_id, ch_id in self._process_channels.items()
                if ch_id == channel_id
            ]
            processes = []
            for track_id in track_ids_to_cancel:
                if track_id in self._active_processes:
                    processes.append(self._active_processes.pop(track_id))
                    self._process_channels.pop(track_id, None)
                    self._process_scopes.pop(track_id, None)
                    active_turn = self._active_turns_by_track.pop(track_id, None)
                    if active_turn:
                        active_turn.done_event.set()
                        scope_state = self._active_turns_by_scope.get(active_turn.scope)
                        if scope_state and scope_state.track_id == track_id:
                            self._active_turns_by_scope.pop(active_turn.scope, None)
            for execution_id, track_id in list(self._execution_track_ids.items()):
                if track_id in track_ids_to_cancel:
                    self._execution_track_ids.pop(execution_id, None)

        if processes:
            await asyncio.gather(
                *(terminate_process_safely(process) for process in processes),
                return_exceptions=True,
            )
        return max(len(processes), initial_count)

    async def cancel_all(self) -> int:
        """Cancel all active executions."""
        async with self._lock:
            initial_count = len(self._active_processes)
        async with self._lock:
            active_scopes = list(self._active_turns_by_scope.keys())
        for scope in active_scopes:
            await self.interrupt_active_turn(scope, timeout=1.0)
            await self._wait_for_turn_settle(scope, timeout=1.5)

        async with self._lock:
            processes = list(self._active_processes.values())
            self._active_processes.clear()
            self._process_channels.clear()
            self._process_scopes.clear()
            self._execution_track_ids.clear()
            for active_turn in self._active_turns_by_track.values():
                active_turn.done_event.set()
            self._active_turns_by_scope.clear()
            self._active_turns_by_track.clear()
        if processes:
            await asyncio.gather(
                *(terminate_process_safely(process) for process in processes),
                return_exceptions=True,
            )
        return max(len(processes), initial_count)

    async def shutdown(self) -> None:
        """Shutdown and cancel all active executions."""
        await self.cancel_all()
