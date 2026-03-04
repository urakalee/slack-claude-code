"""Question manager for handling Claude's AskUserQuestion tool.

Since we run Claude in non-interactive mode, AskUserQuestion can't get direct
input. Instead, we:
1. Detect when Claude uses AskUserQuestion
2. Display the question(s) in Slack with interactive buttons/options
3. Store pending questions with async futures
4. When user responds, resolve the future and continue the conversation
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from slack_sdk.web.async_client import AsyncWebClient

from ..database.repository import DatabaseRepository
from ..utils.pending_manager import PendingManager


@dataclass
class QuestionOption:
    """A single option for a question."""

    label: str
    description: str = ""


@dataclass
class Question:
    """A single question from AskUserQuestion."""

    id: str
    question: str
    header: str
    options: list[QuestionOption]
    multi_select: bool = False


@dataclass
class PendingQuestion:
    """A pending user question from AskUserQuestion tool."""

    question_id: str
    session_id: str
    channel_id: str
    thread_ts: Optional[str]
    tool_use_id: str  # The tool_use_id from Claude
    questions: list[Question]  # Can have multiple questions
    message_ts: Optional[str] = None
    future: Optional[asyncio.Future] = field(default=None, repr=False)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Collected answers: question_index -> list of selected labels
    answers: dict[int, list[str]] = field(default_factory=dict)


class QuestionManager:
    """Manages pending questions from Claude's AskUserQuestion tool.

    Uses async futures to wait for user responses via Slack buttons.
    Thread-safe via asyncio.Lock for all _pending dictionary access.
    """

    _pending = PendingManager[PendingQuestion]()

    @classmethod
    def parse_ask_user_question_input(cls, tool_input: dict) -> list[Question]:
        """Parse the input from AskUserQuestion tool.

        The input format is:
        {
            "questions": [
                {
                    "question": "Which approach should we use?",
                    "header": "Approach",
                    "options": [
                        {"label": "Option A", "description": "Description A"},
                        {"label": "Option B", "description": "Description B"}
                    ],
                    "multiSelect": false
                }
            ]
        }
        """
        questions = []
        safe_input = tool_input if isinstance(tool_input, dict) else {}
        raw_questions = safe_input.get("questions", [])
        if not isinstance(raw_questions, list):
            return questions

        for q in raw_questions:
            if not isinstance(q, dict):
                continue
            options = []
            raw_options = q.get("options", [])
            if not isinstance(raw_options, list):
                raw_options = []
            for opt in raw_options:
                if not isinstance(opt, dict):
                    continue
                options.append(
                    QuestionOption(
                        label=str(opt.get("label", "")),
                        description=str(opt.get("description", "")),
                    )
                )

            questions.append(
                Question(
                    id=str(q.get("id", "")),
                    question=str(q.get("question", "")),
                    header=str(q.get("header", "")),
                    options=options,
                    multi_select=bool(q.get("multiSelect", False)),
                )
            )

        return questions

    @classmethod
    async def create_pending_question(
        cls,
        session_id: str,
        channel_id: str,
        thread_ts: Optional[str],
        tool_use_id: str,
        tool_input: dict,
    ) -> PendingQuestion:
        """Create a pending question from AskUserQuestion tool input.

        Args:
            session_id: Database session ID
            channel_id: Slack channel ID
            thread_ts: Thread timestamp (if in thread)
            tool_use_id: The tool_use_id from Claude's tool invocation
            tool_input: The parsed tool input from Claude

        Returns:
            PendingQuestion object with an async future
        """
        question_id = str(uuid.uuid4())[:8]
        questions = cls.parse_ask_user_question_input(tool_input)
        future = asyncio.get_running_loop().create_future()

        pending = PendingQuestion(
            question_id=question_id,
            session_id=session_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            tool_use_id=tool_use_id,
            questions=questions,
            future=future,
        )

        await cls._pending.add(question_id, pending)
        logger.info(f"Created pending question {question_id} with {len(questions)} question(s)")

        return pending

    @classmethod
    async def post_question_to_slack(
        cls,
        pending: PendingQuestion,
        slack_client: AsyncWebClient,
        db: Optional[DatabaseRepository] = None,
        context_text: str = "",
    ) -> None:
        """Post the question(s) to Slack with interactive buttons.

        Args:
            pending: The pending question to post
            slack_client: Slack client for posting
            db: Optional database for notification settings
            context_text: Optional context text from Claude explaining why they're asking
        """
        from .slack_ui import build_question_blocks

        blocks = build_question_blocks(pending, context_text)

        result = await slack_client.chat_postMessage(
            channel=pending.channel_id,
            thread_ts=pending.thread_ts,
            blocks=blocks,
            text="Assistant has a question for you",
        )

        pending.message_ts = result.get("ts")

        # Post channel notification if configured
        await cls._post_notification(slack_client, pending.channel_id, pending.thread_ts, db)

    @classmethod
    async def _post_notification(
        cls,
        slack_client: AsyncWebClient,
        channel_id: str,
        thread_ts: Optional[str],
        db: Optional[DatabaseRepository] = None,
    ) -> None:
        """Post a channel notification for a pending question."""
        try:
            # Check settings if db provided
            if db:
                settings = await db.get_notification_settings(channel_id)
                # Reuse permission notification setting for questions
                if not settings.notify_on_permission:
                    return

            # Build thread link
            if thread_ts:
                thread_link = (
                    f"https://slack.com/archives/{channel_id}/p{thread_ts.replace('.', '')}"
                )
                message = f":question: Assistant has a question • <{thread_link}|Answer in thread>"
            else:
                message = ":question: Assistant has a question"

            # Post to channel (NOT thread) - triggers sound + unread badge
            await slack_client.chat_postMessage(
                channel=channel_id,
                text=message,
            )
            logger.debug(f"Posted question notification to channel {channel_id}")

        except Exception as e:
            logger.warning(f"Failed to post question notification: {e}")

    @classmethod
    async def set_answer(
        cls,
        question_id: str,
        question_index: int,
        selected_labels: list[str],
    ) -> bool:
        """Set an answer for a specific question.

        Args:
            question_id: The question ID
            question_index: Index of the question being answered
            selected_labels: List of selected option labels

        Returns:
            True if answer was set, False if question not found
        """
        pending = await cls._pending.get(question_id)
        if not pending:
            logger.warning(f"Question {question_id} not found")
            return False
        pending.answers[question_index] = selected_labels
        logger.debug(f"Set answer for question {question_id}[{question_index}]: {selected_labels}")
        return True

    @classmethod
    async def is_complete(cls, question_id: str) -> bool:
        """Check if all questions have been answered.

        Args:
            question_id: The question ID

        Returns:
            True if all questions have answers
        """
        pending = await cls._pending.get(question_id)
        if not pending:
            return False
        return len(pending.answers) >= len(pending.questions)

    @classmethod
    async def resolve(
        cls,
        question_id: str,
    ) -> Optional[PendingQuestion]:
        """Resolve a pending question (mark as answered).

        Called when user has answered all questions.

        Args:
            question_id: The question ID to resolve

        Returns:
            The PendingQuestion if found and resolved, None otherwise
        """
        pending = await cls._pending.get(question_id)
        if not pending:
            logger.warning(f"Question {question_id} not found")
            return None
        try:
            pending.future.set_result(pending.answers)
        except asyncio.InvalidStateError:
            logger.warning(f"Question {question_id} already resolved")
            await cls._pending.pop(question_id)
            return None

        logger.info(f"Question {question_id} resolved with answers: {pending.answers}")
        return pending

    @classmethod
    async def wait_for_answer(
        cls,
        question_id: str,
    ) -> dict[int, list[str]] | None:
        """Wait for user to answer the question.

        Waits indefinitely until the user responds via Slack buttons.

        Args:
            question_id: The question ID

        Returns:
            Dict of answers (question_index -> selected labels), or None if cancelled
        """
        pending = await cls._pending.get(question_id)
        if not pending:
            return None

        try:
            # Wait indefinitely for user response (no timeout)
            answers = await pending.future
            return answers

        except asyncio.CancelledError:
            logger.info(f"Question {question_id} was cancelled")
            return None

        finally:
            await cls._pending.pop(question_id)

    @classmethod
    async def get_pending(cls, question_id: str) -> Optional[PendingQuestion]:
        """Get a pending question by ID."""
        return await cls._pending.get(question_id)

    @classmethod
    async def cancel(cls, question_id: str) -> bool:
        """Cancel a pending question.

        Args:
            question_id: The question ID to cancel

        Returns:
            True if question was found and cancelled
        """
        return await cls._pending.cancel(question_id)

    @classmethod
    async def cancel_for_session(cls, session_id: str) -> int:
        """Cancel all pending questions for a session.

        Args:
            session_id: The session ID

        Returns:
            Number of questions cancelled
        """
        return await cls._pending.cancel_for_session(session_id)

    @classmethod
    def format_answer_for_claude(cls, pending: PendingQuestion) -> str:
        """Format the user's answers into a text response for Claude.

        Args:
            pending: The pending question with answers

        Returns:
            Formatted text response to send as a follow-up message
        """
        response_parts = []

        for i, question in enumerate(pending.questions):
            selected = pending.answers.get(i, [])
            if len(pending.questions) > 1:
                # Multiple questions - include question reference
                response_parts.append(f"**{question.header}**: {', '.join(selected)}")
            else:
                # Single question - just the answer
                response_parts.append(", ".join(selected))

        return "\n".join(response_parts)

    @classmethod
    def format_answer_for_codex_request(cls, pending: PendingQuestion) -> dict:
        """Format answers for Codex app-server `item/tool/requestUserInput` response."""
        answers: dict[str, dict[str, list[str]]] = {}
        for i, question in enumerate(pending.questions):
            question_id = question.id or f"q_{i + 1}"
            answers[question_id] = {"answers": pending.answers.get(i, [])}
        return {"answers": answers}

    @classmethod
    async def count_pending(cls) -> int:
        """Get count of pending questions."""
        return await cls._pending.count()

    @classmethod
    async def cleanup_expired(cls, max_age_seconds: int = 3600) -> int:
        """Remove pending questions that have been waiting too long.

        This prevents memory leaks from abandoned questions.

        Args:
            max_age_seconds: Maximum age in seconds (default: 1 hour)

        Returns:
            Number of expired questions cleaned up
        """
        now = datetime.now(timezone.utc)
        expired = []

        pendings = await cls._pending.list()
        for pending in pendings:
            qid = pending.question_id
            # Calculate age
            age = now - pending.created_at
            if age.total_seconds() > max_age_seconds:
                expired.append(qid)
                logger.info(f"Cleaning up expired question {qid} (age: {age.total_seconds():.0f}s)")

        for qid in expired:
            await cls._pending.cancel(qid)

        return len(expired)
