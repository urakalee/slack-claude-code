"""Unit tests for structured queue-plan parsing and materialization."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.tasks.queue_plan import (
    QueuePlanError,
    contains_queue_plan_markers,
    materialize_queue_plan_prompts,
    materialize_queue_plan_text,
    parse_queue_plan_text,
)


def test_contains_queue_plan_markers_detects_known_markers() -> None:
    assert contains_queue_plan_markers("first\n***\nsecond") is True
    assert contains_queue_plan_markers("***loop-2***\nrun\n***loop-2-end***") is True
    assert (
        contains_queue_plan_markers("***branch-feature/x***\nrun\n***branch-feature/x-end***")
        is True
    )


def test_contains_queue_plan_markers_ignores_plain_text() -> None:
    assert contains_queue_plan_markers("normal prompt\n***bold***\ncontinue") is False


def test_contains_queue_plan_markers_treats_invalid_markers_as_structured() -> None:
    assert contains_queue_plan_markers("***loop-0***") is True


def test_parse_queue_plan_separator_expands_prompts() -> None:
    prompts = parse_queue_plan_text("first task\n***\nsecond task")
    assert [item.prompt for item in prompts] == ["first task", "second task"]
    assert all(item.branch_name is None for item in prompts)


def test_parse_queue_plan_branch_section_scopes_prompts() -> None:
    prompts = parse_queue_plan_text(
        "***branch-feature/auth***\ninside worktree\n***\nagain\n***branch-feature/auth-end***\noutside"
    )
    assert [item.prompt for item in prompts] == ["inside worktree", "again", "outside"]
    assert [item.branch_name for item in prompts] == ["feature/auth", "feature/auth", None]


def test_parse_queue_plan_loop_expands_prompts() -> None:
    prompts = parse_queue_plan_text("***loop-3***\nrun once\n***loop-3-end***")
    assert [item.prompt for item in prompts] == ["run once", "run once", "run once"]

def test_parse_queue_plan_allows_nested_loop_and_branch() -> None:
    prompts = parse_queue_plan_text(
        "***loop-2***\n"
        "outside\n"
        "***branch-feature/a***\n"
        "inside\n"
        "***branch-feature/a-end***\n"
        "***loop-2-end***"
    )
    assert [item.prompt for item in prompts] == ["outside", "inside", "outside", "inside"]
    assert [item.branch_name for item in prompts] == [None, "feature/a", None, "feature/a"]


def test_parse_queue_plan_allows_branch_marker_shorthand_close_inside_loop() -> None:
    prompts = parse_queue_plan_text(
        "***loop-2***\n"
        "***branch-f1***\n"
        "t1\n"
        "***\n"
        "t2\n"
        "***branch-f1***\n"
        "***branch-f2***\n"
        "t3\n"
        "***\n"
        "t4\n"
        "***branch-f2***\n"
        "***loop-2-end***"
    )
    assert [item.prompt for item in prompts] == [
        "t1",
        "t2",
        "t3",
        "t4",
        "t1",
        "t2",
        "t3",
        "t4",
    ]
    assert [item.branch_name for item in prompts] == [
        "f1",
        "f1",
        "f2",
        "f2",
        "f1",
        "f1",
        "f2",
        "f2",
    ]


def test_parse_queue_plan_allows_unclosed_loop_block_at_eof() -> None:
    prompts = parse_queue_plan_text("***loop-2***\nrun")
    assert [item.prompt for item in prompts] == ["run", "run"]
    assert [item.branch_name for item in prompts] == [None, None]


def test_parse_queue_plan_allows_unclosed_branch_block_at_eof() -> None:
    prompts = parse_queue_plan_text("***branch-feature/a***\ninside")
    assert [item.prompt for item in prompts] == ["inside"]
    assert [item.branch_name for item in prompts] == ["feature/a"]


def test_parse_queue_plan_rejects_mismatched_block_end() -> None:
    with pytest.raises(QueuePlanError, match="does not match open"):
        parse_queue_plan_text("***branch-feature/a***\nrun\n***branch-feature/b-end***")


def test_parse_queue_plan_reports_open_branch_when_loop_end_hits_branch_scope() -> None:
    with pytest.raises(QueuePlanError, match="currently inside branch `f2`"):
        parse_queue_plan_text("***loop-2***\n" "***branch-f2***\n" "run\n" "***loop-2-end***")


def test_parse_queue_plan_rejects_non_positive_loop_count() -> None:
    with pytest.raises(QueuePlanError, match="must be >= 1"):
        parse_queue_plan_text("***loop-0***\nrun\n***loop-0-end***")


def test_parse_queue_plan_rejects_unknown_marker() -> None:
    with pytest.raises(QueuePlanError, match="Unknown queue-plan marker"):
        parse_queue_plan_text("first\n***\n***not-a-marker***\nsecond")

def test_parse_queue_plan_enforces_expansion_cap() -> None:
    with pytest.raises(QueuePlanError, match="more than 3 items"):
        parse_queue_plan_text("***loop-4***\nrun\n***loop-4-end***", max_expanded_items=3)


@pytest.mark.asyncio
async def test_materialize_queue_plan_without_branch_does_not_touch_git() -> None:
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(),
        list_worktrees=AsyncMock(),
        add_worktree=AsyncMock(),
    )
    materialized = await materialize_queue_plan_text(
        text="first\n***\nsecond",
        working_directory="/repo",
        git_service=git_service,
    )

    assert [item.prompt for item in materialized] == ["first", "second"]
    assert all(item.working_directory_override is None for item in materialized)
    git_service.validate_git_repo.assert_not_called()
    git_service.list_worktrees.assert_not_called()
    git_service.add_worktree.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_queue_plan_resolves_existing_worktree() -> None:
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        list_worktrees=AsyncMock(
            return_value=[
                SimpleNamespace(branch="feature/auth", path="/repo-worktrees/feature/auth")
            ]
        ),
        add_worktree=AsyncMock(),
    )
    materialized = await materialize_queue_plan_text(
        text="***branch-feature/auth***\nrun\n***branch-feature/auth-end***",
        working_directory="/repo",
        git_service=git_service,
    )

    assert materialized[0].working_directory_override == "/repo-worktrees/feature/auth"
    git_service.add_worktree.assert_not_called()


@pytest.mark.asyncio
async def test_materialize_queue_plan_creates_missing_worktree() -> None:
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        list_worktrees=AsyncMock(return_value=[]),
        add_worktree=AsyncMock(return_value="/repo-worktrees/feature/new"),
    )
    materialized = await materialize_queue_plan_text(
        text="***branch-feature/new***\nrun\n***branch-feature/new-end***",
        working_directory="/repo",
        git_service=git_service,
    )

    assert materialized[0].working_directory_override == "/repo-worktrees/feature/new"
    git_service.add_worktree.assert_awaited_once_with("/repo", "feature/new", from_ref=None)


@pytest.mark.asyncio
async def test_materialize_queue_plan_rejects_branch_sections_outside_git_repo() -> None:
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=False),
        list_worktrees=AsyncMock(),
        add_worktree=AsyncMock(),
    )
    with pytest.raises(QueuePlanError, match="not a git repository"):
        await materialize_queue_plan_text(
            text="***branch-feature/new***\nrun\n***branch-feature/new-end***",
            working_directory="/repo",
            git_service=git_service,
        )


@pytest.mark.asyncio
async def test_materialize_queue_plan_prompts_applies_branch_path_mapping() -> None:
    prompts = parse_queue_plan_text(
        "***branch-feature/a***\nfirst\n***branch-feature/a-end***\nsecond"
    )
    git_service = SimpleNamespace(
        validate_git_repo=AsyncMock(return_value=True),
        list_worktrees=AsyncMock(return_value=[]),
        add_worktree=AsyncMock(return_value="/repo-worktrees/feature/a"),
    )

    materialized = await materialize_queue_plan_prompts(
        expanded=prompts,
        working_directory="/repo",
        git_service=git_service,
    )

    assert [item.working_directory_override for item in materialized] == [
        "/repo-worktrees/feature/a",
        None,
    ]
