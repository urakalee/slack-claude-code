"""Unit tests for backend-aware command routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import config
from src.database.models import Session
from src.handlers.command_router import execute_for_session, resolve_backend_for_session


class TestCommandRouter:
    """Tests for route selection and execution."""

    def test_resolve_backend_for_session(self):
        """Backend resolution follows selected model."""
        assert resolve_backend_for_session(Session(model="opus")) == "claude"
        assert resolve_backend_for_session(Session(model="gpt-5.3-codex")) == "codex"

    @pytest.mark.asyncio
    async def test_execute_for_session_claude(self):
        """Claude sessions call Claude executor and persist Claude session ID."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_or_create_session=AsyncMock(return_value=Session(codex_session_id=None)),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock(), thread_fork=AsyncMock()),
        )
        deps.executor.execute.return_value = SimpleNamespace(session_id="claude-new", success=True)

        session = Session(
            id=7,
            model="opus",
            working_directory="/tmp",
            claude_session_id="claude-old",
        )

        routed = await execute_for_session(
            deps=deps,
            session=session,
            prompt="hello",
            channel_id="C123",
            thread_ts=None,
            execution_id="exec-1",
        )

        assert routed.backend == "claude"
        deps.executor.execute.assert_awaited_once()
        deps.codex_executor.execute.assert_not_called()
        deps.db.update_session_claude_id.assert_awaited_once_with("C123", None, "claude-new")
        deps.db.update_session_codex_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_for_session_codex(self):
        """Codex sessions call Codex executor and persist Codex session ID."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_or_create_session=AsyncMock(return_value=Session(codex_session_id=None)),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock(), thread_fork=AsyncMock()),
        )
        deps.codex_executor.execute.return_value = SimpleNamespace(
            session_id="codex-new",
            success=True,
            output="",
        )

        session = Session(
            id=9,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        routed = await execute_for_session(
            deps=deps,
            session=session,
            prompt="hello",
            channel_id="C123",
            thread_ts="123.4",
            execution_id="exec-2",
        )

        assert routed.backend == "codex"
        deps.codex_executor.execute.assert_awaited_once()
        deps.executor.execute.assert_not_called()
        deps.db.update_session_codex_id.assert_awaited_once_with("C123", "123.4", "codex-new")
        deps.db.update_session_claude_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_for_session_codex_without_executor(self):
        """Codex routing fails fast when no Codex executor is configured."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(), update_session_codex_id=AsyncMock()
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=None,
        )
        session = Session(id=1, model="gpt-5.3-codex", working_directory="/tmp")

        with pytest.raises(RuntimeError, match="Codex executor is not configured"):
            await execute_for_session(
                deps=deps,
                session=session,
                prompt="hello",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-3",
            )

    @pytest.mark.asyncio
    async def test_execute_for_session_codex_plan_mode_passes_permission_mode(self):
        """Codex plan mode keeps original prompt and forwards permission mode."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(), update_session_codex_id=AsyncMock()
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )
        deps.codex_executor.execute.return_value = SimpleNamespace(
            session_id="codex-new",
            success=True,
            output="",
        )

        session = Session(
            id=11,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            permission_mode="plan",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        await execute_for_session(
            deps=deps,
            session=session,
            prompt="Implement feature",
            channel_id="C123",
            thread_ts=None,
            execution_id="exec-4",
        )

        kwargs = deps.codex_executor.execute.await_args.kwargs
        assert kwargs["prompt"] == "Implement feature"
        assert kwargs["permission_mode"] == "plan"

    @pytest.mark.asyncio
    async def test_codex_plan_mode_skips_approval_for_non_plan_output(self):
        """Plan mode should not request approval for generic clarification text."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )
        deps.codex_executor.execute.return_value = SimpleNamespace(
            session_id="codex-new",
            success=True,
            output=(
                "Ready to help. Share the change you want, and I will provide a concrete "
                "implementation plan first, then wait for your confirmation."
            ),
        )

        session = Session(
            id=12,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            permission_mode="plan",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        with patch(
            "src.handlers.command_router.PlanApprovalManager.request_approval",
            new=AsyncMock(return_value=True),
        ) as mock_request_approval:
            await execute_for_session(
                deps=deps,
                session=session,
                prompt="hi",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-5",
                slack_client=SimpleNamespace(),
                user_id="U123",
            )

        mock_request_approval.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_codex_plan_mode_namespaces_tool_activity_ids_per_turn(self):
        """Post-approval execution tool activity IDs should not collide with plan turn IDs."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        call_index = 0

        async def _fake_codex_execute(**kwargs):
            nonlocal call_index
            call_index += 1
            on_chunk = kwargs["on_chunk"]

            await on_chunk(
                SimpleNamespace(
                    type="tool_call",
                    content="",
                    tool_activities=[SimpleNamespace(id="item_1")],
                )
            )

            if call_index == 1:
                await on_chunk(
                    SimpleNamespace(
                        type="assistant",
                        content=(
                            "# Implementation Plan\n"
                            "1. Add streaming tool ID namespacing for multi-turn Codex flows.\n"
                            "2. Preserve tool activity visibility after plan approval.\n"
                            "3. Add regression tests for turn separation.\n\n"
                            "## Test Plan\n"
                            "- Run command router unit tests.\n"
                        ),
                        tool_activities=[],
                    )
                )
                return SimpleNamespace(
                    session_id="codex-new",
                    success=True,
                    output=(
                        "# Implementation Plan\n"
                        "1. Add streaming tool ID namespacing for multi-turn Codex flows.\n"
                        "2. Preserve tool activity visibility after plan approval.\n"
                        "3. Add regression tests for turn separation.\n\n"
                        "## Test Plan\n"
                        "- Run command router unit tests.\n"
                    ),
                )

            await on_chunk(
                SimpleNamespace(
                    type="assistant",
                    content="Implementation complete.",
                    tool_activities=[],
                )
            )
            return SimpleNamespace(
                session_id="codex-new",
                success=True,
                output="Implementation complete.",
            )

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)

        session = Session(
            id=13,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            permission_mode="plan",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        on_chunk = AsyncMock()
        with patch(
            "src.handlers.command_router.PlanApprovalManager.request_approval",
            new=AsyncMock(return_value=True),
        ):
            await execute_for_session(
                deps=deps,
                session=session,
                prompt="Ship this",
                channel_id="C123",
                thread_ts=None,
                execution_id="exec-6",
                on_chunk=on_chunk,
                slack_client=SimpleNamespace(),
                user_id="U123",
            )

        assert deps.codex_executor.execute.await_count == 2
        tool_ids: list[str] = []
        for call in on_chunk.await_args_list:
            msg = call.args[0]
            for tool in msg.tool_activities:
                tool_ids.append(tool.id)

        assert "turn1:item_1" in tool_ids
        assert "turn2:item_1" in tool_ids

    @pytest.mark.asyncio
    async def test_execute_for_session_codex_thread_forks_inherited_channel_thread(self):
        """Thread-scoped Codex sessions should fork inherited channel thread IDs."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id="codex-shared")
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(
                    return_value=SimpleNamespace(session_id="codex-forked", success=True, output="")
                ),
                thread_fork=AsyncMock(return_value={"thread": {"id": "codex-forked"}}),
            ),
        )

        session = Session(
            id=14,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-shared",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )

        await execute_for_session(
            deps=deps,
            session=session,
            prompt="hello",
            channel_id="C123",
            thread_ts="123.4",
            execution_id="exec-7",
        )

        deps.codex_executor.thread_fork.assert_awaited_once_with(
            thread_id="codex-shared",
            working_directory="/tmp",
        )
        assert deps.codex_executor.execute.await_args.kwargs["resume_session_id"] == "codex-forked"
        assert deps.db.update_session_codex_id.await_args_list[0].args == (
            "C123",
            "123.4",
            "codex-forked",
        )
        assert session.codex_session_id == "codex-forked"

    @pytest.mark.asyncio
    async def test_execute_for_session_codex_thread_fork_failure_uses_inherited_thread(self):
        """Fork failures should not block execution for thread-scoped Codex sessions."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                get_or_create_session=AsyncMock(
                    return_value=Session(codex_session_id="codex-shared")
                ),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(
                execute=AsyncMock(
                    return_value=SimpleNamespace(session_id="codex-shared", success=True, output="")
                ),
                thread_fork=AsyncMock(side_effect=RuntimeError("fork unavailable")),
            ),
        )

        session = Session(
            id=15,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-shared",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        logger = SimpleNamespace(warning=MagicMock(), info=MagicMock())

        await execute_for_session(
            deps=deps,
            session=session,
            prompt="hello",
            channel_id="C123",
            thread_ts="123.4",
            execution_id="exec-8",
            logger=logger,
        )

        assert deps.codex_executor.execute.await_args.kwargs["resume_session_id"] == "codex-shared"

    @pytest.mark.asyncio
    async def test_codex_question_limit_does_not_fail_on_exact_limit(self):
        """Hitting exactly the question limit should not force a failed result."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        async def _fake_codex_execute(**kwargs):
            payload = await kwargs["on_user_input_request"](
                "item_1",
                {
                    "questions": [
                        {
                            "id": "q_1",
                            "question": "Proceed?",
                            "header": "Confirm",
                            "options": [{"label": "Yes", "description": "Continue"}],
                        }
                    ]
                },
            )
            assert payload == {"answers": {"q_1": {"answers": ["Yes"]}}}
            return SimpleNamespace(
                session_id="codex-new", success=True, output="Implementation complete."
            )

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)
        session = Session(
            id=16,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        pending_question = SimpleNamespace(question_id="pq1", tool_use_id="item_1")

        with patch.object(config.timeouts.execution, "max_questions_per_conversation", 1):
            with patch(
                "src.handlers.command_router.QuestionManager.create_pending_question",
                new=AsyncMock(return_value=pending_question),
            ):
                with patch(
                    "src.handlers.command_router.QuestionManager.post_question_to_slack",
                    new=AsyncMock(),
                ):
                    with patch(
                        "src.handlers.command_router.QuestionManager.wait_for_answer",
                        new=AsyncMock(return_value={0: ["Yes"]}),
                    ):
                        with patch(
                            "src.handlers.command_router.QuestionManager.format_answer_for_codex_request",
                            return_value={"answers": {"q_1": {"answers": ["Yes"]}}},
                        ):
                            routed = await execute_for_session(
                                deps=deps,
                                session=session,
                                prompt="hello",
                                channel_id="C123",
                                thread_ts=None,
                                execution_id="exec-9",
                                slack_client=SimpleNamespace(),
                                user_id="U123",
                            )

        assert routed.result.success is True
        assert routed.result.output == "Implementation complete."

    @pytest.mark.asyncio
    async def test_codex_question_limit_still_fails_when_extra_question_requested(self):
        """A question request beyond the limit should still mark the result as failed."""
        deps = SimpleNamespace(
            db=SimpleNamespace(
                update_session_claude_id=AsyncMock(),
                update_session_codex_id=AsyncMock(),
                update_session_mode=AsyncMock(),
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
        )

        async def _fake_codex_execute(**kwargs):
            first_payload = await kwargs["on_user_input_request"](
                "item_1",
                {
                    "questions": [
                        {
                            "id": "q_1",
                            "question": "Proceed?",
                            "header": "Confirm",
                            "options": [{"label": "Yes", "description": "Continue"}],
                        }
                    ]
                },
            )
            assert first_payload == {"answers": {"q_1": {"answers": ["Yes"]}}}
            second_payload = await kwargs["on_user_input_request"](
                "item_2",
                {
                    "questions": [
                        {
                            "id": "q_2",
                            "question": "Need another input?",
                            "header": "Confirm",
                            "options": [{"label": "No", "description": "Skip"}],
                        }
                    ]
                },
            )
            assert second_payload is None
            return SimpleNamespace(
                session_id="codex-new", success=True, output="Should not be final success."
            )

        deps.codex_executor.execute = AsyncMock(side_effect=_fake_codex_execute)
        session = Session(
            id=17,
            model="gpt-5.3-codex",
            working_directory="/tmp",
            codex_session_id="codex-old",
            sandbox_mode="workspace-write",
            approval_mode="on-request",
        )
        pending_question = SimpleNamespace(question_id="pq1", tool_use_id="item_1")

        with patch.object(config.timeouts.execution, "max_questions_per_conversation", 1):
            with patch(
                "src.handlers.command_router.QuestionManager.create_pending_question",
                new=AsyncMock(return_value=pending_question),
            ):
                with patch(
                    "src.handlers.command_router.QuestionManager.post_question_to_slack",
                    new=AsyncMock(),
                ):
                    with patch(
                        "src.handlers.command_router.QuestionManager.wait_for_answer",
                        new=AsyncMock(return_value={0: ["Yes"]}),
                    ):
                        with patch(
                            "src.handlers.command_router.QuestionManager.format_answer_for_codex_request",
                            return_value={"answers": {"q_1": {"answers": ["Yes"]}}},
                        ):
                            routed = await execute_for_session(
                                deps=deps,
                                session=session,
                                prompt="hello",
                                channel_id="C123",
                                thread_ts=None,
                                execution_id="exec-10",
                                slack_client=SimpleNamespace(),
                                user_id="U123",
                            )

        assert routed.result.success is False
        assert "Reached maximum question limit (1)." in routed.result.output
