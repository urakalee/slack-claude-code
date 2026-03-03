"""Claude Code executor using subprocess with stream-json output."""

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from loguru import logger

from src.backends.process_executor_base import ProcessExecutorBase
from src.utils.process_utils import terminate_process_safely
from src.utils.stream_models import concat_with_spacing

from ..config import config
from .streaming import StreamMessage, StreamParser

# Timeout for reading a single line from Claude process stdout
# If no output is received for this duration, assume the process is hung
# Set to 30 minutes to allow for long-running operations like writing large files
READLINE_TIMEOUT_SECONDS = 1800
# Grace period to wait for plan writes after plan completion before terminating.
# High value to accommodate longer plan-generation workflows.
PLAN_WRITE_GRACE_SECONDS = 600.0

if TYPE_CHECKING:
    from ..database.repository import DatabaseRepository

# UUID pattern for validating session IDs
UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


@dataclass
class ExecutionResult:
    """Result of a Claude CLI execution."""

    success: bool
    output: str
    detailed_output: str = ""  # Full output with tool use details
    session_id: Optional[str] = None
    error: Optional[str] = None
    cost_usd: Optional[float] = None
    duration_ms: Optional[int] = None
    was_cancelled: bool = False
    has_pending_question: bool = False  # True if terminated due to AskUserQuestion
    has_pending_plan_approval: bool = False  # True if terminated due to ExitPlanMode
    plan_subagent_result: Optional[str] = None  # Plan content from Plan subagent output
    plan_write_timeout: bool = False  # True if we timed out waiting for plan write


@dataclass
class ExecutionState:
    """Per-execution state to avoid race conditions between concurrent executions."""

    # Track ExitPlanMode for retry logic and early termination
    exit_plan_mode_tool_id: Optional[str] = None
    exit_plan_mode_error_detected: bool = False
    exit_plan_mode_detected: bool = False  # For early termination to show approval UI
    # Track AskUserQuestion for early termination
    ask_user_question_detected: bool = False
    # Track Task tools in plan mode for plan approval
    # When in plan mode, we track any Task tool (not just Plan subagents) to capture
    # the plan content and prevent race conditions with ExitPlanMode
    plan_subagent_tool_id: Optional[str] = None
    plan_subagent_is_plan_type: bool = False  # True if subagent_type="Plan"
    plan_subagent_completed: bool = False
    plan_subagent_completed_at: Optional[float] = None
    plan_subagent_result: Optional[str] = None  # Store Task output for plan content
    # Track pending Write tools in plan mode so we don't terminate before plan file exists
    pending_write_tools: dict[str, str] = field(default_factory=dict)  # tool_id -> path
    exit_plan_mode_detected_at: Optional[float] = None
    plan_write_timeout: bool = False
    plan_write_completed: bool = False
    plan_write_path: Optional[str] = None
    plan_write_wait_logged: bool = False


class SubprocessExecutor(ProcessExecutorBase):
    """Execute Claude Code via subprocess with stream-json output.

    Uses `claude -p --output-format stream-json` for reliable non-interactive execution.
    Supports session resume via --resume flag.
    """

    def __init__(
        self,
        db: Optional["DatabaseRepository"] = None,
    ) -> None:
        super().__init__()
        self.db = db
        # Per-execution state to avoid race conditions between concurrent executions
        self._execution_states: dict[str, ExecutionState] = {}
        self._states_lock: asyncio.Lock = asyncio.Lock()

    async def _get_current_permission_mode(
        self, db_session_id: Optional[int], fallback_mode: Optional[str]
    ) -> str:
        """Get the current permission mode from the database.

        This allows detecting mode changes made via /mode command during execution.
        For example, if user switches to plan mode mid-execution, we can pick it up.

        Args:
            db_session_id: Database session ID to check
            fallback_mode: Mode to return if DB lookup fails

        Returns:
            Current permission mode from DB, or fallback_mode if not available
        """
        if not self.db or not db_session_id:
            return fallback_mode or config.CLAUDE_PERMISSION_MODE

        session = await self.db.get_session_by_id(db_session_id)
        if session and session.permission_mode:
            return session.permission_mode

        return fallback_mode or config.CLAUDE_PERMISSION_MODE

    async def execute(
        self,
        prompt: str,
        working_directory: str = "~",
        session_id: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        on_chunk: Optional[Callable[[StreamMessage], Awaitable[None]]] = None,
        permission_mode: Optional[str] = None,
        db_session_id: Optional[int] = None,
        model: Optional[str] = None,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        _recursion_depth: int = 0,
        _is_retry_after_exit_plan_error: bool = False,
    ) -> ExecutionResult:
        """Execute a prompt via Claude Code subprocess.

        Args:
            prompt: The prompt to send to Claude
            working_directory: Directory to run Claude in
            session_id: Identifier for this execution (for tracking)
            resume_session_id: Claude session ID to resume (from previous execution)
            execution_id: Unique ID for this execution (for cancellation)
            on_chunk: Async callback for each streamed message
            permission_mode: Permission mode to use (overrides config default)
            db_session_id: Database session ID for smart context tracking (optional)
            model: Model to use (e.g., "sonnet", "haiku", "claude-opus-4-6[1m]")
            channel_id: Slack channel ID for channel-specific cancellation
            _recursion_depth: Internal parameter to track retry depth (max 3)

        Returns:
            ExecutionResult with the command output
        """
        # Create log prefix for this session
        log_prefix = f"[S:{db_session_id}] " if db_session_id else ""

        # Prevent infinite recursion (max 3 retries)
        MAX_RECURSION_DEPTH = 3
        if _recursion_depth >= MAX_RECURSION_DEPTH:
            logger.error(
                f"{log_prefix}Max recursion depth ({MAX_RECURSION_DEPTH}) reached, aborting"
            )
            return ExecutionResult(
                success=False,
                output="",
                error=f"Max retry depth ({MAX_RECURSION_DEPTH}) exceeded",
            )

        # Create per-execution state to avoid race conditions between concurrent executions
        # Each execution gets its own state object keyed by execution_id
        tracking = self.create_tracking_context(
            execution_id=execution_id,
            session_id=session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )
        state = ExecutionState()
        async with self._states_lock:
            self._execution_states[tracking.track_id] = state

        # Build command
        cmd = [
            "claude",
            "-p",
            "--verbose",  # Required for stream-json
            "--output-format",
            "stream-json",
        ]

        # Add model flag if specified (explicit model > config default)
        effective_model = model or config.DEFAULT_MODEL
        if effective_model:
            cmd.extend(["--model", effective_model])
            logger.info(f"{log_prefix}Using --model {effective_model}")

        # Determine permission mode: explicit > config default
        mode = permission_mode or config.CLAUDE_PERMISSION_MODE
        if mode in config.VALID_PERMISSION_MODES:
            cmd.extend(["--permission-mode", mode])
            logger.info(f"{log_prefix}Using --permission-mode {mode}")
        else:
            logger.warning(
                f"{log_prefix}Invalid permission mode: {mode}, using {config.DEFAULT_BYPASS_MODE}"
            )
            cmd.extend(["--permission-mode", config.DEFAULT_BYPASS_MODE])

        # Add allowed tools restriction if configured
        if config.ALLOWED_TOOLS:
            cmd.extend(["--allowed-tools", config.ALLOWED_TOOLS])
            logger.info(f"{log_prefix}Using --allowed-tools {config.ALLOWED_TOOLS}")

        # Add resume flag if we have a valid Claude session ID (must be UUID format)
        if resume_session_id and UUID_PATTERN.match(resume_session_id):
            cmd.extend(["--resume", resume_session_id])
            logger.info(f"{log_prefix}Resuming session {resume_session_id}")
        elif resume_session_id:
            logger.warning(f"{log_prefix}Invalid session ID format (not UUID): {resume_session_id}")

        # Add the prompt
        cmd.append(prompt)

        # Log full command with all flags, but truncate prompt for readability
        cmd_without_prompt = " ".join(cmd[:-1])
        prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
        logger.info(f"{log_prefix}Executing: {cmd_without_prompt} '{prompt_preview}'")

        # Start subprocess with increased line limit (default is 64KB)
        # Large files can produce JSON lines exceeding this limit
        limit = 200 * 1024 * 1024  # 200MB limit for large file reads
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=working_directory,
                limit=limit,
            )
        except Exception as e:
            logger.error(f"{log_prefix}Failed to start Claude process: {e}")
            return ExecutionResult(
                success=False,
                output="",
                error=f"Failed to start Claude: {e}",
            )

        # Track process for cancellation (track_id already defined above)
        await self.register_process(
            context=tracking,
            process=process,
            channel_id=channel_id,
            execution_id=execution_id,
        )

        parser = StreamParser()
        accumulated_output = ""
        accumulated_detailed = ""
        result_session_id = None
        cost_usd = None
        duration_ms = None
        error_msg = None

        try:
            # Read stdout line by line with timeout protection
            while True:
                read_timeout = READLINE_TIMEOUT_SECONDS
                if (
                    state.exit_plan_mode_detected
                    and not state.plan_subagent_completed
                    and not state.pending_write_tools
                ):
                    if state.exit_plan_mode_detected_at is None:
                        state.exit_plan_mode_detected_at = time.monotonic()
                    remaining = PLAN_WRITE_GRACE_SECONDS - (
                        time.monotonic() - state.exit_plan_mode_detected_at
                    )
                    if remaining <= 0:
                        logger.warning(
                            f"{log_prefix}ExitPlanMode detected but no plan artifacts appeared; "
                            "terminating after grace period"
                        )
                        state.plan_write_timeout = True
                        await terminate_process_safely(process)
                        break
                    read_timeout = min(read_timeout, remaining)

                try:
                    line = await asyncio.wait_for(
                        process.stdout.readline(),
                        timeout=read_timeout,
                    )
                except asyncio.TimeoutError:
                    if (
                        state.exit_plan_mode_detected
                        and not state.plan_subagent_completed
                        and not state.pending_write_tools
                    ):
                        logger.warning(
                            f"{log_prefix}No plan artifact within grace period; terminating"
                        )
                        state.plan_write_timeout = True
                        await terminate_process_safely(process)
                        break

                    logger.error(
                        f"{log_prefix}Readline timeout after {READLINE_TIMEOUT_SECONDS}s - "
                        "Claude process may be hung or lost connection"
                    )
                    await terminate_process_safely(process)
                    return ExecutionResult(
                        success=False,
                        output=accumulated_output,
                        detailed_output=accumulated_detailed,
                        session_id=result_session_id,
                        error=(
                            f"Claude process timed out (no output for {READLINE_TIMEOUT_SECONDS}s). "
                            "The process may have hung or lost connection to the API."
                        ),
                    )

                if not line:
                    break

                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                # Parse the JSON message
                msg = parser.parse_line(line_str)
                if not msg:
                    continue

                # Log human-readable summaries (not full JSON)
                if msg.type == "assistant":
                    # Log text content
                    if msg.content:
                        preview = (
                            msg.content[:100] + "..." if len(msg.content) > 100 else msg.content
                        )
                        logger.debug(f"{log_prefix}Claude: {preview}")
                    # Log tool use and track file context
                    if msg.raw:
                        message = msg.raw.get("message", {})
                        if not isinstance(message, dict):
                            logger.debug(
                                f"{log_prefix}Unexpected assistant message type: {type(message)}"
                            )
                            message = {}
                        content_blocks = message.get("content", [])
                        if not isinstance(content_blocks, list):
                            logger.debug(
                                f"{log_prefix}Unexpected assistant content type: {type(content_blocks)}"
                            )
                            content_blocks = []
                        for block in content_blocks:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "tool_use":
                                tool_name = block.get("name", "unknown")
                                tool_input = block.get("input", {})
                                # Log tool use summary and track file operations
                                if tool_name in ("Read", "Edit", "Write"):
                                    file_path = tool_input.get("file_path", "")
                                    logger.info(f"{log_prefix}Tool: {tool_name} {file_path}")
                                    if tool_name in ("Write", "Edit") and file_path:
                                        # Track .md writes when we know plan mode is active
                                        # (either from ExitPlanMode detection or DB mode)
                                        in_plan_mode = state.exit_plan_mode_detected
                                        if not in_plan_mode:
                                            current_mode = await self._get_current_permission_mode(
                                                db_session_id, permission_mode
                                            )
                                            in_plan_mode = current_mode == "plan"
                                        if in_plan_mode and file_path.endswith(".md"):
                                            tool_id = block.get("id")
                                            if tool_id and tool_id not in state.pending_write_tools:
                                                state.pending_write_tools[tool_id] = file_path
                                                logger.info(
                                                    f"{log_prefix}Tracking pending plan write: {file_path}"
                                                )
                                elif tool_name == "Bash":
                                    command = tool_input.get("command", "")[:50]
                                    logger.info(f"{log_prefix}Tool: Bash '{command}...'")
                                elif tool_name == "AskUserQuestion":
                                    questions = tool_input.get("questions", [])
                                    if questions:
                                        first_q = questions[0].get("question", "?")[:80]
                                        logger.info(
                                            f"{log_prefix}Tool: AskUserQuestion - '{first_q}...' ({len(questions)} question(s))"
                                        )
                                    else:
                                        logger.info(f"{log_prefix}Tool: AskUserQuestion")
                                    # Mark for early termination to handle question in Slack
                                    state.ask_user_question_detected = True
                                    logger.info(
                                        f"{log_prefix}AskUserQuestion detected - will terminate for Slack handling"
                                    )
                                elif tool_name == "ExitPlanMode":
                                    state.exit_plan_mode_tool_id = block.get("id")
                                    # Always set exit_plan_mode_detected when Claude calls
                                    # ExitPlanMode. Claude calling this tool is definitive
                                    # evidence that plan mode is active, regardless of what
                                    # the DB says. The DB mode can be stale after a
                                    # question-answer cycle resumes the session.
                                    state.exit_plan_mode_detected = True
                                    if state.exit_plan_mode_detected_at is None:
                                        state.exit_plan_mode_detected_at = time.monotonic()
                                    logger.info(
                                        f"{log_prefix}Tool: ExitPlanMode - will terminate for Slack approval"
                                    )
                                elif tool_name == "Task":
                                    subagent_type = tool_input.get("subagent_type", "")
                                    desc = tool_input.get("description", "")[:50]
                                    # Always track Plan subagents (definitive plan mode
                                    # evidence). For other Task tools, check DB mode to
                                    # avoid false positives from non-plan exploration tasks.
                                    should_track = subagent_type == "Plan"
                                    if not should_track:
                                        current_mode = await self._get_current_permission_mode(
                                            db_session_id, permission_mode
                                        )
                                        should_track = current_mode == "plan"
                                    if should_track:
                                        state.plan_subagent_tool_id = block.get("id")
                                        state.plan_subagent_is_plan_type = subagent_type == "Plan"
                                        state.plan_subagent_completed = False
                                        state.plan_subagent_completed_at = None
                                        logger.info(
                                            f"{log_prefix}Tool: Task (subagent_type={subagent_type or 'default'}) "
                                            f"'{desc}...' - tracking for plan approval"
                                        )
                                    else:
                                        logger.info(f"{log_prefix}Tool: Task '{desc}...'")
                                else:
                                    logger.info(f"{log_prefix}Tool: {tool_name}")
                elif msg.type == "user" and msg.raw:
                    # Log tool results summary
                    message = msg.raw.get("message", {})
                    if not isinstance(message, dict):
                        logger.debug(f"{log_prefix}Unexpected user message type: {type(message)}")
                        message = {}
                    content_blocks = message.get("content", [])
                    if not isinstance(content_blocks, list):
                        logger.debug(
                            f"{log_prefix}Unexpected user content type: {type(content_blocks)}"
                        )
                        content_blocks = []
                    for block in content_blocks:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_result":
                            tool_use_id = block.get("tool_use_id", "")
                            tool_use_id_short = tool_use_id[:8]
                            is_error = block.get("is_error", False)
                            status = "ERROR" if is_error else "OK"
                            logger.info(f"{log_prefix}Tool result [{tool_use_id_short}]: {status}")

                            # Detect ExitPlanMode ERROR for immediate retry
                            if (
                                is_error
                                and state.exit_plan_mode_tool_id
                                and tool_use_id == state.exit_plan_mode_tool_id
                                and state.exit_plan_mode_detected
                                and not _is_retry_after_exit_plan_error
                            ):
                                logger.warning(
                                    f"{log_prefix}ExitPlanMode failed - will retry with bypass mode"
                                )
                                state.exit_plan_mode_error_detected = True

                            # Detect Task tool completion in plan mode
                            if (
                                not is_error
                                and state.plan_subagent_tool_id
                                and tool_use_id == state.plan_subagent_tool_id
                            ):
                                logger.info(
                                    f"{log_prefix}Task tool completed in plan mode - capturing result"
                                )
                                state.plan_subagent_completed = True
                                state.plan_subagent_completed_at = time.monotonic()
                                # Only capture result content if this is a Plan subagent
                                # Non-Plan subagents (Explore, general-purpose) may return
                                # file listings or other data that shouldn't be used as plan content
                                if state.plan_subagent_is_plan_type:
                                    result_content = block.get("content", [])
                                    if isinstance(result_content, list):
                                        for content_block in result_content:
                                            if not isinstance(content_block, dict):
                                                continue
                                            if content_block.get("type") == "text":
                                                state.plan_subagent_result = content_block.get(
                                                    "text", ""
                                                )
                                                logger.debug(
                                                    f"{log_prefix}Captured Plan subagent result: "
                                                    f"{len(state.plan_subagent_result)} chars"
                                                )
                                                break
                                    elif isinstance(result_content, str):
                                        state.plan_subagent_result = result_content
                                else:
                                    logger.debug(
                                        f"{log_prefix}Non-Plan Task completed, not capturing as plan content"
                                    )
                            # Track Write tool completion to avoid early termination
                            if tool_use_id in state.pending_write_tools:
                                file_path = state.pending_write_tools.pop(tool_use_id)
                                status = "ERROR" if is_error else "OK"
                                logger.info(
                                    f"{log_prefix}Write tool completed ({status}) for {file_path}"
                                )
                                if not is_error:
                                    state.plan_write_completed = True
                                    state.plan_write_path = file_path
                elif msg.type == "init":
                    logger.info(f"{log_prefix}Session initialized: {msg.session_id}")
                elif msg.type == "error":
                    logger.error(f"{log_prefix}Error: {msg.content}")
                elif msg.type == "result":
                    if msg.cost_usd:
                        logger.info(
                            f"{log_prefix}Claude Finished - completed in {msg.duration_ms}ms, cost ${msg.cost_usd:.4f}"
                        )
                    else:
                        logger.info(
                            f"{log_prefix}Claude Finished - completed in {msg.duration_ms}ms"
                        )

                # Track session ID
                if msg.session_id:
                    result_session_id = msg.session_id

                # Accumulate content
                if msg.type == "assistant" and msg.content:
                    accumulated_output = concat_with_spacing(accumulated_output, msg.content)
                elif msg.type == "result" and msg.content and not accumulated_output:
                    # Some CLI commands only populate the result field.
                    accumulated_output = msg.content

                # Track result metadata
                if msg.type == "result":
                    cost_usd = msg.cost_usd
                    duration_ms = msg.duration_ms
                    if msg.session_id:
                        result_session_id = msg.session_id
                    # Get final accumulated detailed output
                    if msg.detailed_content:
                        accumulated_detailed = msg.detailed_content
                    # Check for errors in result message (e.g., session not found)
                    if msg.raw and msg.raw.get("is_error"):
                        errors = msg.raw.get("errors", [])
                        if errors:
                            error_msg = "; ".join(errors)
                            logger.warning(f"{log_prefix}Result contains errors: {error_msg}")

                # Track errors from error-type messages
                if msg.type == "error":
                    error_msg = msg.content

                # Call chunk callback
                if on_chunk:
                    await on_chunk(msg)

                # If ExitPlanMode error detected, terminate early and retry
                if state.exit_plan_mode_error_detected:
                    logger.info(f"{log_prefix}Terminating execution to retry without plan mode")
                    await terminate_process_safely(process)
                    break  # Exit the message processing loop

                # If AskUserQuestion detected, terminate early to handle in Slack
                # This must happen before Claude CLI returns the error to Claude
                if state.ask_user_question_detected:
                    logger.info(
                        f"{log_prefix}Terminating execution to handle AskUserQuestion in Slack"
                    )
                    await terminate_process_safely(process)
                    break  # Exit the message processing loop

                # If ExitPlanMode detected in plan mode, terminate early to show approval UI
                # The CLI would otherwise block waiting for interactive approval
                # BUT: If we have a pending Plan subagent, wait for it to complete first
                # so we can capture its result (the plan content)
                if state.exit_plan_mode_detected:
                    # Check if Plan subagent is still running (started but not completed)
                    plan_subagent_pending = (
                        state.plan_subagent_tool_id and not state.plan_subagent_completed
                    )
                    write_pending = bool(state.pending_write_tools)
                    if plan_subagent_pending or write_pending:
                        if write_pending:
                            elapsed = 0.0
                            if state.exit_plan_mode_detected_at is not None:
                                elapsed = time.monotonic() - state.exit_plan_mode_detected_at
                            if elapsed > PLAN_WRITE_GRACE_SECONDS:
                                logger.warning(
                                    f"{log_prefix}ExitPlanMode detected but Write tools still pending "
                                    f"after {elapsed:.1f}s; terminating anyway"
                                )
                                await terminate_process_safely(process)
                                break
                            logger.info(
                                f"{log_prefix}ExitPlanMode detected but Write tool(s) still pending - "
                                "waiting for writes to complete"
                            )
                        else:
                            logger.info(
                                f"{log_prefix}ExitPlanMode detected but Plan subagent still running - "
                                "waiting for subagent to complete"
                            )
                        # Don't terminate yet - continue processing to get subagent result / writes
                    else:
                        logger.info(
                            f"{log_prefix}Terminating execution to handle plan approval in Slack"
                        )
                        await terminate_process_safely(process)
                        break  # Exit the message processing loop

                # If Plan subagent (specifically subagent_type=Plan) completed in plan mode,
                # terminate early to show approval UI. This handles the case where Claude
                # uses Task(subagent_type=Plan) instead of ExitPlanMode.
                # Note: We only auto-terminate for Plan subagents, not general Task tools.
                # General Task tools might be followed by more work before ExitPlanMode.
                if state.plan_subagent_completed and state.plan_subagent_is_plan_type:
                    if state.pending_write_tools:
                        elapsed = 0.0
                        if state.plan_subagent_completed_at is not None:
                            elapsed = time.monotonic() - state.plan_subagent_completed_at
                        if elapsed > PLAN_WRITE_GRACE_SECONDS:
                            logger.warning(
                                f"{log_prefix}Plan subagent completed but Write tools still pending "
                                f"after {elapsed:.1f}s; terminating anyway"
                            )
                            await terminate_process_safely(process)
                            break
                        logger.info(
                            f"{log_prefix}Plan subagent completed but Write tool(s) still pending - "
                            "waiting for writes to complete"
                        )
                    else:
                        if state.plan_write_completed:
                            logger.info(
                                f"{log_prefix}Plan write completed ({state.plan_write_path}); "
                                "terminating for Plan subagent approval"
                            )
                            await terminate_process_safely(process)
                            break  # Exit the message processing loop
                        elapsed = 0.0
                        if state.plan_subagent_completed_at is not None:
                            elapsed = time.monotonic() - state.plan_subagent_completed_at
                        if elapsed > PLAN_WRITE_GRACE_SECONDS:
                            logger.warning(
                                f"{log_prefix}Plan subagent completed but no plan write detected "
                                f"after {elapsed:.1f}s; terminating anyway"
                            )
                            await terminate_process_safely(process)
                            break  # Exit the message processing loop
                        if not state.plan_write_wait_logged:
                            logger.info(
                                f"{log_prefix}Plan subagent completed - waiting up to "
                                f"{PLAN_WRITE_GRACE_SECONDS:.0f}s for plan write to start/finish"
                            )
                            state.plan_write_wait_logged = True

                if msg.is_final:
                    break

            # Wait for process to complete
            await process.wait()

            # Check stderr for errors
            stderr = await process.stderr.read()
            if stderr:
                stderr_str = stderr.decode("utf-8", errors="replace").strip()
                if stderr_str:
                    logger.warning(f"{log_prefix}Claude stderr: {stderr_str}")
                    # Only treat stderr as error if process failed
                    if process.returncode != 0 and not error_msg:
                        error_msg = stderr_str

            success = process.returncode == 0 and not error_msg

            # Check if session not found - retry without resume
            if (
                not success
                and resume_session_id
                and "No conversation found with session ID" in (error_msg or "")
            ):
                logger.info(
                    f"{log_prefix}Session {resume_session_id} not found, retrying without resume (depth={_recursion_depth + 1})"
                )
                return await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=None,  # Don't resume
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    permission_mode=permission_mode,
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    _recursion_depth=_recursion_depth + 1,
                )

            # Check if ExitPlanMode error detected - retry without plan mode
            if state.exit_plan_mode_error_detected and not _is_retry_after_exit_plan_error:
                logger.info(
                    f"{log_prefix}Retrying execution with bypass mode after ExitPlanMode error (depth={_recursion_depth + 1})"
                )

                return await self.execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=session_id,
                    resume_session_id=resume_session_id,  # Keep the session
                    execution_id=execution_id,
                    on_chunk=on_chunk,
                    permission_mode=config.DEFAULT_BYPASS_MODE,  # Switch to bypass mode
                    db_session_id=db_session_id,
                    model=model,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    _recursion_depth=_recursion_depth + 1,
                    _is_retry_after_exit_plan_error=True,  # Prevent infinite retry
                )

            # Plan approval is triggered by ExitPlanMode or Plan subagent completion
            # Note: plan_subagent_completed alone is not enough - we also check plan_subagent_is_plan_type
            # to avoid triggering approval for non-Plan Task tools (Explore, general-purpose, etc.)
            has_plan_approval = state.exit_plan_mode_detected or (
                state.plan_subagent_completed and state.plan_subagent_is_plan_type
            )

            return ExecutionResult(
                success=success,
                output=accumulated_output,
                detailed_output=accumulated_detailed,
                session_id=result_session_id,
                error=error_msg,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                has_pending_question=state.ask_user_question_detected,
                has_pending_plan_approval=has_plan_approval,
                plan_subagent_result=state.plan_subagent_result,
                plan_write_timeout=state.plan_write_timeout,
            )

        except asyncio.CancelledError:
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
            logger.error(f"{log_prefix}Error during execution: {e}")
            await terminate_process_safely(process)
            return ExecutionResult(
                success=False,
                output=accumulated_output,
                detailed_output=accumulated_detailed,
                session_id=result_session_id,
                error=str(e),
            )
        finally:
            await self.unregister_process(
                context=tracking,
                execution_id=execution_id,
            )
            async with self._states_lock:
                self._execution_states.pop(tracking.track_id, None)
