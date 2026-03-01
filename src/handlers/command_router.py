"""Backend-aware command execution helpers."""

from dataclasses import dataclass
from typing import Any, Optional

from src.approval.handler import PermissionManager
from src.approval.plan_manager import PlanApprovalManager
from src.codex.approval_bridge import (
    approval_payload_from_decision,
    format_approval_request_for_slack,
)
from src.codex.capabilities import apply_codex_mode_to_prompt, is_likely_plan_content
from src.config import config
from src.database.models import Session
from src.question.manager import QuestionManager
from src.utils.execution_scope import build_session_scope


@dataclass
class CommandRouteResult:
    """Execution result annotated with the selected backend."""

    backend: str
    result: Any


def resolve_backend_for_session(session: Session) -> str:
    """Resolve backend for a session based on selected model."""
    return session.get_backend()


def _normalize_codex_question_input(tool_name: str, tool_input: dict) -> dict:
    """Normalize Codex question tool input into AskUserQuestion-compatible shape."""
    normalized_input = tool_input if isinstance(tool_input, dict) else {}
    if normalized_input.get("questions"):
        return normalized_input

    if normalized_input.get("question"):
        return {
            "questions": [
                {
                    "question": normalized_input.get("question", ""),
                    "header": normalized_input.get("header", "Question"),
                    "options": normalized_input.get("options", []),
                    "multiSelect": normalized_input.get("multiSelect", False),
                }
            ]
        }

    if (tool_name or "").strip().lower() == "request_user_input":
        return {
            "questions": [
                {
                    "question": "Please provide additional input.",
                    "header": "Input Needed",
                    "options": [],
                    "multiSelect": False,
                }
            ]
        }

    return {"questions": []}


async def execute_for_session(
    deps: Any,
    session: Session,
    prompt: str,
    channel_id: str,
    thread_ts: Optional[str],
    execution_id: str,
    on_chunk: Any = None,
    slack_client: Any = None,
    user_id: Optional[str] = None,
    logger: Any = None,
) -> CommandRouteResult:
    """Execute a prompt with the correct backend and persist resumed session IDs."""
    backend = resolve_backend_for_session(session)

    if backend == "codex":
        if not deps.codex_executor:
            raise RuntimeError("Codex executor is not configured")

        session_scope = build_session_scope(channel_id, thread_ts)

        pending_question = None
        accumulated_context = ""
        question_count = 0
        max_questions = config.timeouts.execution.max_questions_per_conversation

        async def wrapped_on_chunk(msg: Any) -> None:
            nonlocal accumulated_context
            if on_chunk:
                await on_chunk(msg)

            if msg.type == "assistant" and msg.content:
                accumulated_context += msg.content

        async def on_user_input_request(tool_use_id: str, tool_input: dict) -> dict | None:
            nonlocal pending_question, question_count
            if slack_client is None:
                if pending_question and pending_question.tool_use_id == tool_use_id:
                    await QuestionManager.cancel(pending_question.question_id)
                    pending_question = None
                return None

            if question_count >= max_questions:
                if logger:
                    logger.warning(
                        f"Reached maximum Codex question limit ({max_questions}) "
                        f"for session {session.id}"
                    )
                return None

            if not pending_question or pending_question.tool_use_id != tool_use_id:
                pending_question = await QuestionManager.create_pending_question(
                    session_id=str(session.id),
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    tool_use_id=tool_use_id,
                    tool_input=_normalize_codex_question_input("request_user_input", tool_input),
                )

            await QuestionManager.post_question_to_slack(
                pending_question,
                slack_client,
                deps.db,
                context_text=accumulated_context,
            )
            question_count += 1
            answers = await QuestionManager.wait_for_answer(pending_question.question_id)
            if not answers:
                pending_question = None
                return None

            response_payload = QuestionManager.format_answer_for_codex_request(pending_question)
            pending_question = None
            return response_payload

        async def on_approval_request(method: str, approval_input: dict) -> dict | None:
            if slack_client is None:
                return None

            tool_name, tool_input = format_approval_request_for_slack(method, approval_input)
            approved = await PermissionManager.request_approval(
                session_id=str(session.id),
                channel_id=channel_id,
                tool_name=tool_name,
                tool_input=tool_input,
                user_id=user_id,
                thread_ts=thread_ts,
                slack_client=slack_client,
                db=deps.db,
                auto_approve_tools=config.AUTO_APPROVE_TOOLS,
            )
            return approval_payload_from_decision(method, approved)

        execution_prompt = apply_codex_mode_to_prompt(prompt, session.permission_mode)
        result = await deps.codex_executor.execute(
            prompt=execution_prompt,
            working_directory=session.working_directory,
            session_id=session_scope,
            resume_session_id=session.codex_session_id,
            execution_id=execution_id,
            on_chunk=wrapped_on_chunk,
            on_user_input_request=on_user_input_request,
            on_approval_request=on_approval_request,
            permission_mode=session.permission_mode,
            sandbox_mode=session.sandbox_mode or config.CODEX_SANDBOX_MODE,
            approval_mode=session.approval_mode or config.CODEX_APPROVAL_MODE,
            db_session_id=session.id,
            model=session.model,
            channel_id=channel_id,
            thread_ts=thread_ts,
        )

        if result.session_id:
            await deps.db.update_session_codex_id(channel_id, thread_ts, result.session_id)

        if question_count >= max_questions:
            result.output = (
                (result.output or accumulated_context)
                + f"\n\n_Reached maximum question limit ({max_questions})._"
            ).strip()
            result.success = False
        if pending_question:
            await QuestionManager.cancel(pending_question.question_id)
            pending_question = None

        if (
            session.permission_mode == "plan"
            and result.success
            and is_likely_plan_content(result.output)
            and slack_client is not None
        ):
            if logger:
                logger.info("Codex plan response ready, requesting user approval")
            approved = await PlanApprovalManager.request_approval(
                session_id=str(session.id),
                channel_id=channel_id,
                plan_content=result.output,
                claude_session_id=result.session_id or "",
                prompt=prompt,
                user_id=user_id,
                thread_ts=thread_ts,
                slack_client=slack_client,
                plan_file_path=None,
            )

            if approved:
                await deps.db.update_session_mode(channel_id, thread_ts, config.DEFAULT_BYPASS_MODE)
                session.permission_mode = config.DEFAULT_BYPASS_MODE

                result = await deps.codex_executor.execute(
                    prompt="Plan approved. Please proceed with the implementation.",
                    working_directory=session.working_directory,
                    session_id=session_scope,
                    resume_session_id=result.session_id,
                    execution_id=execution_id,
                    on_chunk=wrapped_on_chunk,
                    on_user_input_request=on_user_input_request,
                    on_approval_request=on_approval_request,
                    permission_mode=session.permission_mode,
                    sandbox_mode=session.sandbox_mode or config.CODEX_SANDBOX_MODE,
                    approval_mode=session.approval_mode or config.CODEX_APPROVAL_MODE,
                    db_session_id=session.id,
                    model=session.model,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                )
                if result.session_id:
                    await deps.db.update_session_codex_id(channel_id, thread_ts, result.session_id)
            else:
                result.success = False
                result.output = (
                    result.output
                    + "\n\n_Plan not approved. Staying in plan mode until you provide feedback._"
                ).strip()

        return CommandRouteResult(backend=backend, result=result)

    result = await deps.executor.execute(
        prompt=prompt,
        working_directory=session.working_directory,
        session_id=build_session_scope(channel_id, thread_ts),
        resume_session_id=session.claude_session_id,
        execution_id=execution_id,
        on_chunk=on_chunk,
        permission_mode=session.permission_mode,
        db_session_id=session.id,
        model=session.model,
        channel_id=channel_id,
        thread_ts=thread_ts,
    )

    if result.session_id:
        await deps.db.update_session_claude_id(channel_id, thread_ts, result.session_id)

    return CommandRouteResult(backend=backend, result=result)
