"""Unit tests for backend-aware command routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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
                update_session_claude_id=AsyncMock(), update_session_codex_id=AsyncMock()
            ),
            executor=SimpleNamespace(execute=AsyncMock()),
            codex_executor=SimpleNamespace(execute=AsyncMock()),
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
    async def test_execute_for_session_codex_plan_mode_enriches_prompt(self):
        """Codex plan mode appends plan-only guidance to the prompt."""
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
        called_prompt = kwargs["prompt"]
        assert "Provide a concrete implementation plan first" in called_prompt
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
