"""Unit tests for git worktree command handlers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.database.models import Session
from src.git.models import Worktree
from src.git.service import GitError
from src.handlers.claude.worktree import (
    _handle_add,
    _handle_list,
    _handle_merge,
    _handle_prune,
    _handle_remove,
    _handle_switch,
    register_worktree_commands,
)


class _FakeApp:
    """Minimal Slack app stub for command registration tests."""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def command(self, name: str):
        def decorator(func):
            self.handlers[name] = func
            return func

        return decorator


def _ctx(channel_id: str = "C123", thread_ts: str = "123.456"):
    return SimpleNamespace(
        channel_id=channel_id,
        thread_ts=thread_ts,
        client=SimpleNamespace(chat_postMessage=AsyncMock()),
    )


def _deps_for_session(session: Session):
    return SimpleNamespace(
        db=SimpleNamespace(
            get_or_create_session=AsyncMock(return_value=session),
            update_session_cwd=AsyncMock(),
            clear_session_claude_id=AsyncMock(),
            clear_session_codex_id=AsyncMock(),
        )
    )


@pytest.mark.asyncio
async def test_registers_worktree_and_alias_commands():
    app = _FakeApp()
    deps = SimpleNamespace(db=SimpleNamespace(get_or_create_session=AsyncMock()))

    register_worktree_commands(app, deps)

    assert "/worktree" in app.handlers
    assert "/wt" in app.handlers


@pytest.mark.asyncio
async def test_command_shows_usage_when_subcommand_missing():
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(validate_git_repo=AsyncMock(return_value=True))
    app = _FakeApp()

    with patch("src.handlers.claude.worktree.GitService", return_value=git_service):
        register_worktree_commands(app, deps)

    handler = app.handlers["/worktree"]
    ack = AsyncMock()
    client = SimpleNamespace(chat_postMessage=AsyncMock())

    await handler(
        ack=ack,
        command={"channel_id": "C123", "user_id": "U123", "text": "", "command": "/worktree"},
        client=client,
        logger=MagicMock(),
    )

    ack.assert_awaited_once()
    assert client.chat_postMessage.await_args.kwargs["text"] == "Worktree usage"


@pytest.mark.asyncio
async def test_command_reports_not_git_repo():
    session = Session(working_directory="/not-a-repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(validate_git_repo=AsyncMock(return_value=False))
    app = _FakeApp()

    with patch("src.handlers.claude.worktree.GitService", return_value=git_service):
        register_worktree_commands(app, deps)

    handler = app.handlers["/worktree"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())

    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "list",
            "command": "/worktree",
        },
        client=client,
        logger=MagicMock(),
    )

    assert client.chat_postMessage.await_args.kwargs["text"] == "Not a git repository"


@pytest.mark.asyncio
async def test_command_rejects_extra_args_for_add():
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(validate_git_repo=AsyncMock(return_value=True))
    app = _FakeApp()

    with patch("src.handlers.claude.worktree.GitService", return_value=git_service):
        register_worktree_commands(app, deps)

    handler = app.handlers["/worktree"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())

    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "add feature-x extra",
            "command": "/worktree",
        },
        client=client,
        logger=MagicMock(),
    )

    assert client.chat_postMessage.await_args.kwargs["text"] == "Worktree usage"


@pytest.mark.asyncio
async def test_command_rejects_invalid_flags_for_list():
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(validate_git_repo=AsyncMock(return_value=True))
    app = _FakeApp()

    with patch("src.handlers.claude.worktree.GitService", return_value=git_service):
        register_worktree_commands(app, deps)

    handler = app.handlers["/worktree"]
    client = SimpleNamespace(chat_postMessage=AsyncMock())

    await handler(
        ack=AsyncMock(),
        command={
            "channel_id": "C123",
            "user_id": "U123",
            "text": "list --from main",
            "command": "/worktree",
        },
        client=client,
        logger=MagicMock(),
    )

    assert client.chat_postMessage.await_args.kwargs["text"] == "Worktree usage"


@pytest.mark.asyncio
async def test_handle_add_updates_session_by_default():
    ctx = _ctx()
    session = Session(working_directory="/repo", codex_session_id="codex-1")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(add_worktree=AsyncMock(return_value="/repo-worktrees/feature-x"))

    await _handle_add(ctx, deps, session, git_service, "feature-x", from_ref=None, stay=False)

    git_service.add_worktree.assert_awaited_once_with("/repo", "feature-x", from_ref=None)
    deps.db.update_session_cwd.assert_awaited_once_with(
        "C123", "123.456", "/repo-worktrees/feature-x"
    )
    deps.db.clear_session_claude_id.assert_awaited_once_with("C123", "123.456")
    deps.db.clear_session_codex_id.assert_awaited_once_with("C123", "123.456")


@pytest.mark.asyncio
async def test_handle_add_with_stay_does_not_change_session_cwd():
    ctx = _ctx()
    session = Session(working_directory="/repo", codex_session_id="codex-1")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(add_worktree=AsyncMock(return_value="/repo-worktrees/feature-x"))

    await _handle_add(ctx, deps, session, git_service, "feature-x", from_ref="main", stay=True)

    git_service.add_worktree.assert_awaited_once_with("/repo", "feature-x", from_ref="main")
    deps.db.update_session_cwd.assert_not_called()
    deps.db.clear_session_claude_id.assert_not_called()
    deps.db.clear_session_codex_id.assert_not_called()


@pytest.mark.asyncio
async def test_handle_list_includes_current_tags_and_action_buttons():
    ctx = _ctx()
    session = Session(working_directory="/tmp/project-worktrees/feature2/subdir")
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(
            return_value=[
                Worktree(path="/tmp/project", branch="main", is_main=True),
                Worktree(path="/tmp/project-worktrees/feature", branch="feature"),
                Worktree(path="/tmp/project-worktrees/feature2", branch="feature2"),
            ]
        )
    )

    await _handle_list(ctx, session, git_service)

    blocks = ctx.client.chat_postMessage.await_args.kwargs["blocks"]
    section_texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    all_text = "\n".join(section_texts)
    assert "`feature2` _(current)_" in all_text
    assert "`main` _(main)_" in all_text
    assert any(b["type"] == "actions" for b in blocks)


@pytest.mark.asyncio
async def test_handle_switch_supports_path_target():
    ctx = _ctx()
    session = Session(working_directory="/repo", codex_session_id=None)
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(
            return_value=[Worktree(path="/repo-worktrees/feature-x", branch="feature-x")]
        )
    )

    await _handle_switch(ctx, deps, session, git_service, "/repo-worktrees/feature-x")

    deps.db.update_session_cwd.assert_awaited_once_with(
        "C123", "123.456", "/repo-worktrees/feature-x"
    )


@pytest.mark.asyncio
async def test_handle_switch_reports_missing_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(return_value=[Worktree(path="/repo", branch="main", is_main=True)])
    )

    await _handle_switch(ctx, deps, session, git_service, "feature-x")

    deps.db.update_session_cwd.assert_not_called()
    assert ctx.client.chat_postMessage.await_args.kwargs["text"] == "Worktree not found: feature-x"


@pytest.mark.asyncio
async def test_handle_merge_success_removes_clean_source_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo-worktrees/target")
    deps = _deps_for_session(session)
    target_wt = Worktree(path="/repo-worktrees/target", branch="target")
    source_wt = Worktree(path="/repo-worktrees/feature-x", branch="feature-x")
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(return_value=[target_wt, source_wt]),
        get_status=AsyncMock(
            side_effect=[
                SimpleNamespace(is_clean=True),
                SimpleNamespace(is_clean=True),
            ]
        ),
        merge_branch=AsyncMock(return_value=(True, "merged")),
        remove_worktree=AsyncMock(return_value=True),
    )

    await _handle_merge(ctx, deps, session, git_service, "feature-x")

    git_service.merge_branch.assert_awaited_once_with("/repo-worktrees/target", "feature-x")
    git_service.remove_worktree.assert_awaited_once_with(
        "/repo-worktrees/target", "/repo-worktrees/feature-x"
    )


@pytest.mark.asyncio
async def test_handle_merge_keeps_dirty_source_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo-worktrees/target")
    deps = _deps_for_session(session)
    target_wt = Worktree(path="/repo-worktrees/target", branch="target")
    source_wt = Worktree(path="/repo-worktrees/feature-x", branch="feature-x")
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(return_value=[target_wt, source_wt]),
        get_status=AsyncMock(
            side_effect=[
                SimpleNamespace(is_clean=True),
                SimpleNamespace(is_clean=False),
            ]
        ),
        merge_branch=AsyncMock(return_value=(True, "merged")),
        remove_worktree=AsyncMock(),
    )

    await _handle_merge(ctx, deps, session, git_service, "feature-x")

    git_service.remove_worktree.assert_not_called()


@pytest.mark.asyncio
async def test_handle_merge_conflicts_do_not_remove_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo-worktrees/target")
    deps = _deps_for_session(session)
    target_wt = Worktree(path="/repo-worktrees/target", branch="target")
    source_wt = Worktree(path="/repo-worktrees/feature-x", branch="feature-x")
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(return_value=[target_wt, source_wt]),
        get_status=AsyncMock(return_value=SimpleNamespace(is_clean=True)),
        merge_branch=AsyncMock(return_value=(False, "conflict details")),
        remove_worktree=AsyncMock(),
    )

    await _handle_merge(ctx, deps, session, git_service, "feature-x")

    git_service.remove_worktree.assert_not_called()


@pytest.mark.asyncio
async def test_handle_merge_rejects_dirty_target_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo-worktrees/target")
    deps = _deps_for_session(session)
    target_wt = Worktree(path="/repo-worktrees/target", branch="target")
    source_wt = Worktree(path="/repo-worktrees/feature-x", branch="feature-x")
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(return_value=[target_wt, source_wt]),
        get_status=AsyncMock(return_value=SimpleNamespace(is_clean=False)),
        merge_branch=AsyncMock(),
        remove_worktree=AsyncMock(),
    )

    with pytest.raises(GitError, match="Target worktree"):
        await _handle_merge(ctx, deps, session, git_service, "feature-x")


@pytest.mark.asyncio
async def test_handle_remove_blocks_current_and_main_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(
            return_value=[Worktree(path="/repo", branch="main", is_main=True)]
        ),
        get_status=AsyncMock(return_value=SimpleNamespace(is_clean=True)),
        remove_worktree=AsyncMock(),
        delete_branch=AsyncMock(),
    )

    with pytest.raises(GitError, match="current session"):
        await _handle_remove(ctx, deps, session, git_service, "main")


@pytest.mark.asyncio
async def test_handle_remove_requires_force_for_dirty_worktree():
    ctx = _ctx()
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(
            return_value=[
                Worktree(path="/repo", branch="main", is_main=True),
                Worktree(path="/repo-worktrees/feature", branch="feature"),
            ]
        ),
        get_status=AsyncMock(return_value=SimpleNamespace(is_clean=False)),
        remove_worktree=AsyncMock(),
        delete_branch=AsyncMock(),
    )

    with pytest.raises(GitError, match="--force"):
        await _handle_remove(ctx, deps, session, git_service, "feature", force=False)


@pytest.mark.asyncio
async def test_handle_remove_force_and_delete_branch():
    ctx = _ctx()
    session = Session(working_directory="/repo")
    deps = _deps_for_session(session)
    git_service = SimpleNamespace(
        list_worktrees=AsyncMock(
            return_value=[
                Worktree(path="/repo", branch="main", is_main=True),
                Worktree(path="/repo-worktrees/feature", branch="feature"),
            ]
        ),
        get_status=AsyncMock(return_value=SimpleNamespace(is_clean=False)),
        remove_worktree=AsyncMock(return_value=True),
        delete_branch=AsyncMock(return_value=True),
    )

    await _handle_remove(
        ctx,
        deps,
        session,
        git_service,
        "feature",
        force=True,
        delete_branch=True,
    )

    git_service.remove_worktree.assert_awaited_once_with(
        "/repo", "/repo-worktrees/feature", force=True
    )
    git_service.delete_branch.assert_awaited_once_with("/repo", "feature")


@pytest.mark.asyncio
async def test_handle_prune_passes_dry_run_flag():
    ctx = _ctx()
    session = Session(working_directory="/repo")
    git_service = SimpleNamespace(prune_worktrees=AsyncMock(return_value="stale entry"))

    await _handle_prune(ctx, session, git_service, dry_run=True)

    git_service.prune_worktrees.assert_awaited_once_with("/repo", dry_run=True)
