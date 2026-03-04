"""Unit tests for consolidated `/git` command handler."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.database.models import Session
from src.git.models import GitStatus
from src.handlers.claude.git import register_git_commands


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator


def _deps(session: Session):
    return SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
        )
    )


@pytest.mark.asyncio
async def test_registers_git_command_only():
    app = _FakeApp()
    deps = _deps(Session(working_directory="/repo"))

    register_git_commands(app, deps)

    assert "/git" in app.handlers
    assert "/status" not in app.handlers
    assert "/diff" not in app.handlers
    assert "/commit" not in app.handlers
    assert "/branch" not in app.handlers


@pytest.mark.asyncio
async def test_git_status_dispatches_to_git_service():
    session = Session(working_directory="/repo")
    deps = _deps(session)
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        get_status=AsyncMock(return_value=GitStatus(branch="main")),
    )
    app = _FakeApp()

    with patch("src.handlers.claude.git.GitService", return_value=git_service):
        register_git_commands(app, deps)

    handler = app.handlers["/git"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={"channel_id": "C123", "user_id": "U123", "text": "status", "command": "/git"},
        client=client,
        logger=MagicMock(),
    )

    git_service.get_status.assert_awaited_once_with("/repo")
    assert client.chat_postMessage.await_args.kwargs["text"] == "Git status: main"


@pytest.mark.asyncio
async def test_git_diff_staged_passes_staged_flag():
    session = Session(working_directory="/repo")
    deps = _deps(session)
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        get_diff=AsyncMock(return_value="diff --git a/a.py b/a.py"),
    )
    app = _FakeApp()

    with patch("src.handlers.claude.git.GitService", return_value=git_service):
        register_git_commands(app, deps)

    handler = app.handlers["/git"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "diff --staged",
            "command": "/git",
        },
        client=client,
        logger=MagicMock(),
    )

    git_service.get_diff.assert_awaited_once_with("/repo", staged=True)
    assert client.chat_postMessage.await_args.kwargs["text"] == "Git diff: 24 chars"


@pytest.mark.asyncio
async def test_git_commit_requires_message():
    session = Session(working_directory="/repo")
    deps = _deps(session)
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        commit_changes=AsyncMock(),
    )
    app = _FakeApp()

    with patch("src.handlers.claude.git.GitService", return_value=git_service):
        register_git_commands(app, deps)

    handler = app.handlers["/git"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={"channel_id": "C123", "user_id": "U123", "text": "commit", "command": "/git"},
        client=client,
        logger=MagicMock(),
    )

    git_service.commit_changes.assert_not_awaited()
    assert client.chat_postMessage.await_args.kwargs["text"] == "Git usage"


@pytest.mark.asyncio
async def test_git_branch_create_dispatches():
    session = Session(working_directory="/repo")
    deps = _deps(session)
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        create_branch=AsyncMock(return_value=True),
    )
    app = _FakeApp()

    with patch("src.handlers.claude.git.GitService", return_value=git_service):
        register_git_commands(app, deps)

    handler = app.handlers["/git"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "branch create feature/auth",
            "command": "/git",
        },
        client=client,
        logger=MagicMock(),
    )

    git_service.create_branch.assert_awaited_once_with("/repo", "feature/auth", switch=True)
    assert (
        client.chat_postMessage.await_args.kwargs["text"]
        == "Created and switched to branch: feature/auth"
    )


@pytest.mark.asyncio
async def test_git_command_reports_not_git_repo():
    session = Session(working_directory="/repo")
    deps = _deps(session)
    git_service = SimpleNamespace(validate_git_repo=AsyncMock(return_value=False))
    app = _FakeApp()

    with patch("src.handlers.claude.git.GitService", return_value=git_service):
        register_git_commands(app, deps)

    handler = app.handlers["/git"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())
    await handler(
        ack=AsyncMock(),
        command={"channel_id": "C123", "user_id": "U123", "text": "status", "command": "/git"},
        client=client,
        logger=MagicMock(),
    )

    assert client.chat_postMessage.await_args.kwargs["text"] == "Not a git repository"
