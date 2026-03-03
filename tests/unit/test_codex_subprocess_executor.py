"""Unit tests for Codex app-server subprocess executor."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from src.config import config
from src.codex.subprocess_executor import (
    SubprocessExecutor,
    TurnControlResult,
    _ActiveTurnState,
    _terminate_process_safely,
)


class _DummyStdout:
    """Simple async stdout stream for subprocess mocks."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = [line + "\n" for line in lines]

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0).encode("utf-8")


class _DummyStderr:
    """Simple async stderr stream for subprocess mocks."""

    async def read(self) -> bytes:
        return b""


class _DummyStdin:
    """Capture JSON-RPC writes sent to app-server stdin."""

    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data.decode("utf-8"))

    async def drain(self) -> None:
        return None


class _DummyProcess:
    """Simple subprocess mock compatible with asyncio interfaces."""

    def __init__(self, lines: list[str]) -> None:
        self.stdin = _DummyStdin()
        self.stdout = _DummyStdout(lines)
        self.stderr = _DummyStderr()
        self.returncode = None

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9


class _HangingProcess:
    """Subprocess mock that ignores terminate until kill is called."""

    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        if self.killed:
            self.returncode = -9
            return -9
        await asyncio.sleep(1)
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


def _json_line(payload: dict) -> str:
    return json.dumps(payload)


def _sent_messages(process: _DummyProcess) -> list[dict]:
    messages: list[dict] = []
    for chunk in process.stdin.writes:
        for line in chunk.splitlines():
            line = line.strip()
            if line:
                messages.append(json.loads(line))
    return messages


def _sent_requests(process: _DummyProcess) -> list[dict]:
    return [msg for msg in _sent_messages(process) if "method" in msg and "params" in msg]


def _sent_responses(process: _DummyProcess) -> list[dict]:
    return [msg for msg in _sent_messages(process) if "id" in msg and "result" in msg]


def _sent_errors(process: _DummyProcess) -> list[dict]:
    return [msg for msg in _sent_messages(process) if "id" in msg and "error" in msg]


class TestCodexSubprocessExecutor:
    """Tests for app-server execution behavior."""

    @pytest.mark.asyncio
    async def test_execute_uses_app_server_and_passes_thread_params(self, monkeypatch):
        """Executor should start app-server and send thread/start + turn/start with settings."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as mock_exec:
            result = await executor.execute(
                prompt="build feature",
                working_directory="/tmp/workspace",
                sandbox_mode="danger-full-access",
                approval_mode="never",
                model="gpt-5.3-codex-high",
            )

        args = mock_exec.await_args.args
        assert args[:4] == ("codex", "app-server", "--listen", "stdio://")
        assert result.success is True
        assert result.session_id == "thread-1"

        requests = _sent_requests(process)
        methods = [req["method"] for req in requests]
        assert methods == ["initialize", "thread/start", "turn/start"]

        thread_start = requests[1]
        assert thread_start["params"]["cwd"] == "/tmp/workspace"
        assert thread_start["params"]["approvalPolicy"] == "never"
        assert thread_start["params"]["sandbox"] == "danger-full-access"
        assert thread_start["params"]["model"] == "gpt-5.3-codex"

        turn_start = requests[2]
        assert turn_start["params"]["threadId"] == "thread-1"
        assert turn_start["params"]["effort"] == "high"
        assert turn_start["params"]["input"][0]["text"] == "build feature"
        assert "collaborationMode" not in turn_start["params"]

    @pytest.mark.asyncio
    async def test_execute_plan_mode_sets_turn_collaboration_mode(self, monkeypatch):
        """Plan mode should use native app-server collaborationMode on turn/start."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}, "model": "gpt-5.3-codex"},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="plan this change",
                working_directory="/tmp/workspace",
                model="gpt-5.3-codex-high",
                permission_mode="plan",
            )

        assert result.success is True
        turn_start = _sent_requests(process)[2]
        assert turn_start["params"]["collaborationMode"] == {
            "mode": "plan",
            "settings": {
                "model": "gpt-5.3-codex",
                "reasoning_effort": "high",
                "developer_instructions": None,
            },
        }

    @pytest.mark.asyncio
    async def test_execute_non_plan_mode_sets_default_collaboration_mode(self, monkeypatch):
        """Non-plan modes should explicitly set default collaboration mode."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}, "model": "gpt-5.3-codex"},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="implement this plan",
                working_directory="/tmp/workspace",
                model="gpt-5.3-codex-high",
                permission_mode="bypassPermissions",
            )

        assert result.success is True
        turn_start = _sent_requests(process)[2]
        assert turn_start["params"]["collaborationMode"] == {
            "mode": "default",
            "settings": {
                "model": "gpt-5.3-codex",
                "reasoning_effort": "high",
                "developer_instructions": None,
            },
        }

    @pytest.mark.asyncio
    async def test_error_notification_uses_structured_error_payload(self, monkeypatch):
        """Structured error notifications should surface the nested message and details."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "error",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "willRetry": False,
                            "error": {
                                "message": "Context window exceeded",
                                "additionalDetails": "Please compact the thread and retry",
                                "codexErrorInfo": "contextWindowExceeded",
                            },
                        },
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="analyze quant/options",
                working_directory="/tmp/workspace",
            )

        assert result.success is False
        assert "Context window exceeded" in (result.error or "")
        assert "Please compact the thread and retry" in (result.error or "")
        assert "codexErrorInfo=contextWindowExceeded" in (result.error or "")

    @pytest.mark.asyncio
    async def test_error_notification_with_retry_does_not_end_turn(self, monkeypatch):
        """Retryable error notifications should not terminate execution early."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "error",
                        "params": {
                            "threadId": "thread-1",
                            "turnId": "turn-1",
                            "willRetry": True,
                            "error": {"message": "Responses stream disconnected"},
                        },
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="retryable issue",
                working_directory="/tmp/workspace",
            )

        assert result.success is True
        assert result.error is None

    @pytest.mark.asyncio
    async def test_assistant_deltas_preserve_text_and_skip_completed_duplicate(self, monkeypatch):
        """Delta chunks should be concatenated verbatim and not replayed by item/completed."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": "item_1", "delta": "I"},
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": "item_1", "delta": "'m testing formatting."},
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "id": "item_1",
                                "type": "agentMessage",
                                "text": "I'm testing formatting.",
                            }
                        },
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="format",
                working_directory="/tmp/workspace",
            )

        assert result.success is True
        assert result.output == "I'm testing formatting."
        assert "\n\n" not in result.output

    @pytest.mark.asyncio
    async def test_assistant_completed_repairs_missing_delta_tail(self, monkeypatch):
        """Completed assistant items should backfill missing delta tail text."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": "item_1", "delta": "I'm testing forma"},
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "id": "item_1",
                                "type": "agentMessage",
                                "text": "I'm testing formatting.",
                            }
                        },
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="format",
                working_directory="/tmp/workspace",
            )

        assert result.success is True
        assert result.output == "I'm testing formatting."

    @pytest.mark.asyncio
    async def test_agent_message_completed_without_deltas_is_retained(self, monkeypatch):
        """item/completed assistant text should still be captured when no deltas were emitted."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "id": "item_2",
                                "type": "agent_message",
                                "text": "Final assistant message",
                            }
                        },
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="format",
                working_directory="/tmp/workspace",
            )

        assert result.success is True
        assert result.output == "Final assistant message"

    @pytest.mark.asyncio
    async def test_internal_reasoning_and_diff_deltas_are_not_exposed(self, monkeypatch):
        """Reasoning and turn diff notifications should not leak into user output."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/reasoning/summaryTextDelta",
                        "params": {"delta": "*Capturing line references*"},
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/diff/updated",
                        "params": {"diff": "diff --git a/x.py b/x.py\n+raw patch line"},
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/agentMessage/delta",
                        "params": {"itemId": "item_3", "delta": "Summary only."},
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "item/completed",
                        "params": {
                            "item": {
                                "id": "item_3",
                                "type": "agentMessage",
                                "text": "Summary only.",
                            }
                        },
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="summarize changes",
                working_directory="/tmp/workspace",
            )

        assert result.success is True
        assert result.output == "Summary only."
        assert "diff --git" not in result.output
        assert "Capturing line references" not in result.output

    @pytest.mark.asyncio
    async def test_user_input_request_uses_callback_response(self, monkeypatch):
        """request_user_input server requests should be answered by callback payload."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        question = {
            "id": "q_1",
            "question": "Proceed?",
            "header": "Confirm",
            "options": [{"label": "Yes", "description": "Continue"}],
        }
        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "item/tool/requestUserInput",
                        "params": {"itemId": "item_1", "questions": [question]},
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        callback_payload = {"answers": {"q_1": {"answers": ["Yes"]}}}
        on_user_input_request = AsyncMock(return_value=callback_payload)

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="continue",
                working_directory="/tmp/workspace",
                on_user_input_request=on_user_input_request,
            )

        assert result.success is True
        on_user_input_request.assert_awaited_once_with("item_1", {"questions": [question]})

        response_by_id = {msg["id"]: msg for msg in _sent_responses(process)}
        assert response_by_id[10]["result"] == callback_payload

    @pytest.mark.asyncio
    async def test_approval_request_uses_callback_response(self, monkeypatch):
        """Approval server requests should use callback-provided decision payload."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        approval_params = {
            "itemId": "item_2",
            "threadId": "thread-1",
            "turnId": "turn-1",
            "command": "ls -la",
        }
        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 20,
                        "method": "item/commandExecution/requestApproval",
                        "params": approval_params,
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        on_approval_request = AsyncMock(return_value={"decision": "decline"})

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="run command",
                working_directory="/tmp/workspace",
                on_approval_request=on_approval_request,
            )

        assert result.success is True
        on_approval_request.assert_awaited_once_with(
            "item/commandExecution/requestApproval", approval_params
        )

        response_by_id = {msg["id"]: msg for msg in _sent_responses(process)}
        assert response_by_id[20]["result"] == {"decision": "decline"}

    @pytest.mark.asyncio
    async def test_dynamic_tool_call_returns_structured_failure(self, monkeypatch):
        """Dynamic tool requests should return schema-valid failure payloads."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        params = {
            "threadId": "thread-1",
            "turnId": "turn-1",
            "callId": "call-1",
            "tool": "custom_tool",
            "arguments": {"value": "x"},
        }
        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 30,
                        "method": "item/tool/call",
                        "params": params,
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="run dynamic tool",
                working_directory="/tmp/workspace",
            )

        assert result.success is True
        response_by_id = {msg["id"]: msg for msg in _sent_responses(process)}
        assert response_by_id[30]["result"]["success"] is False
        assert response_by_id[30]["result"]["contentItems"][0]["type"] == "inputText"
        assert "not supported" in response_by_id[30]["result"]["contentItems"][0]["text"]

    @pytest.mark.asyncio
    async def test_legacy_approval_request_methods_are_rejected(self, monkeypatch):
        """Legacy non-v2 approval request methods should return method-not-found."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 31,
                        "method": "skill/requestApproval",
                        "params": {"skillName": "legacy"},
                    }
                ),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="legacy request",
                working_directory="/tmp/workspace",
            )

        assert result.success is True
        errors_by_id = {msg["id"]: msg for msg in _sent_errors(process)}
        assert errors_by_id[31]["error"]["code"] == -32601

    @pytest.mark.asyncio
    async def test_default_approval_decision_respects_mode_when_no_callback(self, monkeypatch):
        """Without callback, never-mode auto-accepts while on-request declines."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        def build_process() -> _DummyProcess:
            return _DummyProcess(
                [
                    _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                    _json_line(
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "result": {"thread": {"id": "thread-1"}},
                        }
                    ),
                    _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                    _json_line(
                        {
                            "jsonrpc": "2.0",
                            "id": 21,
                            "method": "item/fileChange/requestApproval",
                            "params": {
                                "itemId": "item_3",
                                "threadId": "thread-1",
                                "turnId": "turn-1",
                            },
                        }
                    ),
                    _json_line(
                        {
                            "jsonrpc": "2.0",
                            "method": "turn/completed",
                            "params": {"turn": {"status": "completed"}},
                        }
                    ),
                ]
            )

        process_never = build_process()
        process_on_request = build_process()

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[process_never, process_on_request]),
        ):
            result_never = await executor.execute(
                prompt="p1",
                working_directory="/tmp/workspace",
                approval_mode="never",
            )
            result_on_request = await executor.execute(
                prompt="p2",
                working_directory="/tmp/workspace",
                approval_mode="on-request",
            )

        assert result_never.success is True
        assert result_on_request.success is True

        response_never = {msg["id"]: msg for msg in _sent_responses(process_never)}[21]
        response_on_request = {msg["id"]: msg for msg in _sent_responses(process_on_request)}[21]
        assert response_never["result"] == {"decision": "accept"}
        assert response_on_request["result"] == {"decision": "decline"}

    @pytest.mark.asyncio
    async def test_resume_missing_thread_retries_with_new_thread(self, monkeypatch):
        """Missing resume thread should retry with thread/start."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process_resume_fail = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "error": {"message": "thread not found"},
                    }
                ),
            ]
        )
        process_fresh_start = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-2"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[process_resume_fail, process_fresh_start]),
        ) as mock_exec:
            result = await executor.execute(
                prompt="retry me",
                working_directory="/tmp/workspace",
                resume_session_id="old-thread",
            )

        assert mock_exec.await_count == 2
        assert result.success is True
        assert result.session_id == "thread-2"

        first_methods = [req["method"] for req in _sent_requests(process_resume_fail)]
        second_methods = [req["method"] for req in _sent_requests(process_fresh_start)]
        assert first_methods == ["initialize", "thread/resume"]
        assert second_methods == ["initialize", "thread/start", "turn/start"]

    @pytest.mark.asyncio
    async def test_exec_prepends_default_instructions_when_file_exists(self, monkeypatch, tmp_path):
        """Executor prepends default instructions from file before turn/start input."""
        instructions = tmp_path / "default_instructions.txt"
        instructions.write_text("ALWAYS BE CONCISE", encoding="utf-8")
        monkeypatch.setattr(config, "CODEX_DEFAULT_INSTRUCTIONS_FILE", str(instructions))
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", True)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )

        executor = SubprocessExecutor()
        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            result = await executor.execute(
                prompt="do a repo review",
                working_directory="/tmp/workspace",
            )

        assert result.success is True
        turn_start = _sent_requests(process)[2]
        assert turn_start["params"]["input"][0]["text"] == "ALWAYS BE CONCISE\n\ndo a repo review"

    @pytest.mark.asyncio
    async def test_cancel_resolves_channel_prefixed_track_id(self):
        """cancel() should find executions by execution_id even with channel-prefixed track ids."""
        process = _DummyProcess([])
        executor = SubprocessExecutor()
        executor._active_processes["C123_exec-1"] = process
        executor._process_channels["C123_exec-1"] = "C123"
        executor._execution_track_ids["exec-1"] = "C123_exec-1"

        cancelled = await executor.cancel("exec-1")

        assert cancelled is True
        assert process.returncode == 0
        assert "C123_exec-1" not in executor._active_processes
        assert "exec-1" not in executor._execution_track_ids

    @pytest.mark.asyncio
    async def test_terminate_process_safely_kills_unresponsive_process(self):
        """_terminate_process_safely should kill processes that ignore terminate."""
        process = _HangingProcess()

        await _terminate_process_safely(process, timeout=0.01)

        assert process.terminated is True
        assert process.killed is True

    @pytest.mark.asyncio
    async def test_active_turn_lifecycle_helpers(self):
        """Active turn helpers should reflect done_event lifecycle transitions."""
        executor = SubprocessExecutor()
        state = _ActiveTurnState(
            scope="scope-1",
            track_id="track-1",
            thread_id="thread-1",
            turn_id="turn-1",
            control_queue=asyncio.Queue(),
        )

        async with executor._lock:
            executor._active_turns_by_scope["scope-1"] = state
            executor._active_turns_by_track["track-1"] = state

        assert await executor.has_active_turn("scope-1") is True
        active = await executor.get_active_turn("scope-1")
        assert active is not None
        assert active["turn_id"] == "turn-1"

        state.done_event.set()
        assert await executor.has_active_turn("scope-1") is False
        assert await executor.get_active_turn("scope-1") is None

    @pytest.mark.asyncio
    async def test_steer_active_turn_success(self):
        """steer_active_turn should return callback result when control request is consumed."""
        executor = SubprocessExecutor()
        control_queue: asyncio.Queue = asyncio.Queue()
        state = _ActiveTurnState(
            scope="scope-2",
            track_id="track-2",
            thread_id="thread-2",
            turn_id="turn-2",
            control_queue=control_queue,
        )
        async with executor._lock:
            executor._active_turns_by_scope["scope-2"] = state
            executor._active_turns_by_track["track-2"] = state

        async def consume_once():
            request = await control_queue.get()
            request.future.set_result(
                TurnControlResult(success=True, message="ok", turn_id="turn-2b")
            )

        consumer_task = asyncio.create_task(consume_once())
        result = await executor.steer_active_turn("scope-2", "continue", timeout=0.5)
        await consumer_task

        assert result.success is True
        assert result.turn_id == "turn-2b"

    @pytest.mark.asyncio
    async def test_steer_active_turn_timeout(self):
        """steer_active_turn should time out when no active loop consumes control request."""
        executor = SubprocessExecutor()
        state = _ActiveTurnState(
            scope="scope-3",
            track_id="track-3",
            thread_id="thread-3",
            turn_id="turn-3",
            control_queue=asyncio.Queue(),
        )
        async with executor._lock:
            executor._active_turns_by_scope["scope-3"] = state
            executor._active_turns_by_track["track-3"] = state

        result = await executor.steer_active_turn("scope-3", "continue", timeout=0.01)
        assert result.success is False
        assert "timed out" in (result.error or "")

    @pytest.mark.asyncio
    async def test_interrupt_active_turn_success(self):
        """interrupt_active_turn should return callback result when consumed."""
        executor = SubprocessExecutor()
        control_queue: asyncio.Queue = asyncio.Queue()
        state = _ActiveTurnState(
            scope="scope-4",
            track_id="track-4",
            thread_id="thread-4",
            turn_id="turn-4",
            control_queue=control_queue,
        )
        async with executor._lock:
            executor._active_turns_by_scope["scope-4"] = state
            executor._active_turns_by_track["track-4"] = state

        async def consume_once():
            request = await control_queue.get()
            request.future.set_result(
                TurnControlResult(success=True, message="interrupt accepted", turn_id="turn-4")
            )

        consumer_task = asyncio.create_task(consume_once())
        result = await executor.interrupt_active_turn("scope-4", timeout=0.5)
        await consumer_task

        assert result.success is True
        assert result.turn_id == "turn-4"

    @pytest.mark.asyncio
    async def test_cancel_interrupts_before_terminate(self):
        """cancel() should request turn interrupt before terminating subprocess."""
        process = _DummyProcess([])
        executor = SubprocessExecutor()
        executor._active_processes["exec-1"] = process
        executor._process_scopes["exec-1"] = "scope-cancel"

        event_order: list[str] = []

        async def interrupt_side_effect(*args, **kwargs):
            event_order.append("interrupt")
            return TurnControlResult(success=True, turn_id="turn-cancel")

        async def settle_side_effect(*args, **kwargs):
            event_order.append("settle")
            return True

        async def terminate_side_effect(*args, **kwargs):
            event_order.append("terminate")
            return None

        with patch.object(
            executor,
            "interrupt_active_turn",
            new=AsyncMock(side_effect=interrupt_side_effect),
        ) as mock_interrupt:
            with patch.object(
                executor,
                "_wait_for_turn_settle",
                new=AsyncMock(side_effect=settle_side_effect),
            ) as mock_settle:
                with patch(
                    "src.codex.subprocess_executor.terminate_process_safely",
                    new=AsyncMock(side_effect=terminate_side_effect),
                ) as mock_terminate:
                    cancelled = await executor.cancel("exec-1")

        assert cancelled is True
        mock_interrupt.assert_awaited_once_with("scope-cancel", timeout=1.0)
        mock_settle.assert_awaited_once_with("scope-cancel", timeout=1.5)
        mock_terminate.assert_awaited_once()
        assert event_order == ["interrupt", "settle", "terminate"]

    @pytest.mark.asyncio
    async def test_dual_ready_rpc_and_control_does_not_timeout_control(self, monkeypatch):
        """When rpc/control complete together, control should not be dropped and time out."""
        monkeypatch.setattr(config, "CODEX_PREPEND_DEFAULT_INSTRUCTIONS", False)

        process = _DummyProcess(
            [
                _json_line({"jsonrpc": "2.0", "id": 1, "result": {}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {"thread": {"id": "thread-1"}},
                    }
                ),
                _json_line({"jsonrpc": "2.0", "id": 3, "result": {"turn": {"id": "turn-1"}}}),
                _json_line(
                    {
                        "jsonrpc": "2.0",
                        "method": "turn/completed",
                        "params": {"turn": {"status": "completed"}},
                    }
                ),
            ]
        )
        executor = SubprocessExecutor()
        real_wait = asyncio.wait

        async def wait_both(tasks, return_when=asyncio.FIRST_COMPLETED):
            task_list = list(tasks)
            for task in task_list:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
                except asyncio.TimeoutError:
                    pass
            done = {task for task in task_list if task.done()}
            if done:
                pending = set(task_list) - done
                return done, pending
            return await real_wait(tasks, return_when=return_when)

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ):
            with patch("src.codex.subprocess_executor.asyncio.wait", new=wait_both):
                exec_task = asyncio.create_task(
                    executor.execute(prompt="run", working_directory="/tmp/workspace")
                )
                while not await executor.has_active_turn(":channel"):
                    await asyncio.sleep(0)
                steer_result = await executor.steer_active_turn(
                    ":channel", "follow-up", timeout=0.5
                )
                result = await exec_task

        assert result.success is True
        assert steer_result.success is False
        assert steer_result.error != "steer request timed out"

    @pytest.mark.asyncio
    async def test_metrics_snapshot_and_reset(self):
        """Executor should expose and reset integration metrics."""
        executor = SubprocessExecutor()
        await executor._increment_metric("steer_requests", 2)
        await executor._increment_metric("steer_successes", 1)
        await executor.record_queue_fallback(success=True)
        await executor.record_queue_fallback(success=False)

        snapshot = await executor.get_metrics_snapshot()
        assert snapshot["steer_requests"] == 2
        assert snapshot["steer_successes"] == 1
        assert snapshot["queue_fallback_attempts"] == 2
        assert snapshot["queue_fallback_successes"] == 1
        assert snapshot["queue_fallback_failures"] == 1

        await executor.reset_metrics()
        reset_snapshot = await executor.get_metrics_snapshot()
        assert reset_snapshot["steer_requests"] == 0
        assert reset_snapshot["queue_fallback_attempts"] == 0
