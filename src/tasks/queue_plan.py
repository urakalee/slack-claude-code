"""Structured queue-plan parser for prompt/worktree/loop DSL."""

import re
from dataclasses import dataclass, field
from typing import Optional

from src.git.service import GitError, GitService

MAX_EXPANDED_QUEUE_PLAN_ITEMS = 500

_ANY_MARKER_RE = re.compile(r"^\*\*\*.+$")
_BRANCH_START_RE = re.compile(r"^\*\*\*branch-(.+)$")
_BRANCH_END_RE = re.compile(r"^\*\*\*branch-(.+)-end$")
_LOOP_START_RE = re.compile(r"^\*\*\*loop-(-?\d+)$")
_LOOP_END_RE = re.compile(r"^\*\*\*loop-(-?\d+)-end$")
_PARALLEL_START_RE = re.compile(r"^\*\*\*parallel(?:-(-?\d+))?$")
_PARALLEL_END_RE = re.compile(r"^\*\*\*parallel-end$")


class QueuePlanError(ValueError):
    """Raised when structured queue-plan parsing or materialization fails."""


@dataclass(frozen=True)
class QueuePlanPrompt:
    """Expanded queue prompt with optional branch context."""

    prompt: str
    branch_name: Optional[str] = None
    parallel_group_id: Optional[str] = None
    parallel_limit: Optional[int] = None


@dataclass(frozen=True)
class MaterializedQueuePlanPrompt:
    """Queue prompt ready for storage, with optional worktree override."""

    prompt: str
    working_directory_override: Optional[str] = None
    parallel_group_id: Optional[str] = None
    parallel_limit: Optional[int] = None


@dataclass
class _PromptNode:
    prompt: str


@dataclass
class _BranchNode:
    branch_name: str
    children: list["_Node"]


@dataclass
class _LoopNode:
    count: int
    children: list["_Node"]


@dataclass
class _ParallelNode:
    limit: Optional[int]
    children: list["_Node"]


_Node = _PromptNode | _BranchNode | _LoopNode | _ParallelNode


@dataclass
class _Frame:
    kind: str
    start_line: int
    branch_name: Optional[str] = None
    loop_count: Optional[int] = None
    parallel_limit: Optional[int] = None
    nodes: list[_Node] = field(default_factory=list)
    prompt_lines: list[str] = field(default_factory=list)


def contains_queue_plan_markers(text: str) -> bool:
    """Return True when text includes at least one line-level queue-plan marker."""
    for line in text.splitlines():
        stripped = line.strip()
        try:
            marker = _parse_marker(stripped, strict=False)
        except QueuePlanError:
            # Invalid markers should still route through structured-plan handling
            # so users get a clear validation error from the parser.
            return True
        if marker is not None:
            return True
        if stripped.startswith("***"):
            return True
    return False


def parse_queue_plan_text(
    text: str, max_expanded_items: int = MAX_EXPANDED_QUEUE_PLAN_ITEMS
) -> list[QueuePlanPrompt]:
    """Parse queue-plan DSL text into expanded prompt entries."""
    if max_expanded_items < 1:
        raise QueuePlanError("max_expanded_items must be at least 1")

    root = _parse_to_ast(text)
    expanded: list[QueuePlanPrompt] = []
    _expand_nodes(
        root,
        active_branch=None,
        active_parallel_group_id=None,
        active_parallel_limit=None,
        out=expanded,
        max_items=max_expanded_items,
        group_counter=[0],
    )

    if not expanded:
        raise QueuePlanError("No prompts found in structured queue plan.")
    return expanded


async def materialize_queue_plan_text(
    text: str,
    working_directory: str,
    git_service: Optional[GitService] = None,
    max_expanded_items: int = MAX_EXPANDED_QUEUE_PLAN_ITEMS,
) -> list[MaterializedQueuePlanPrompt]:
    """Parse + resolve queue-plan DSL into queue-ready prompt entries."""
    expanded = parse_queue_plan_text(text, max_expanded_items=max_expanded_items)
    return await materialize_queue_plan_prompts(
        expanded=expanded,
        working_directory=working_directory,
        git_service=git_service,
    )


async def materialize_queue_plan_prompts(
    expanded: list[QueuePlanPrompt],
    working_directory: str,
    git_service: Optional[GitService] = None,
) -> list[MaterializedQueuePlanPrompt]:
    """Resolve branch-scoped queue entries to concrete worktree paths."""
    branch_names = sorted({item.branch_name for item in expanded if item.branch_name})
    if not branch_names:
        return [
            MaterializedQueuePlanPrompt(
                prompt=item.prompt,
                parallel_group_id=item.parallel_group_id,
                parallel_limit=item.parallel_limit,
            )
            for item in expanded
        ]

    service = git_service or GitService()
    if not await service.validate_git_repo(working_directory):
        raise QueuePlanError(
            "Structured queue plan uses branch sections, but current working directory is not a "
            f"git repository: {working_directory}"
        )

    try:
        worktrees = await service.list_worktrees(working_directory)
    except GitError as e:
        raise QueuePlanError(f"Failed to list worktrees: {e}") from e

    worktree_paths_by_branch: dict[str, str] = {
        worktree.branch: worktree.path for worktree in worktrees if worktree.branch
    }

    for branch_name in branch_names:
        if branch_name in worktree_paths_by_branch:
            continue
        try:
            worktree_paths_by_branch[branch_name] = await service.add_worktree(
                working_directory,
                branch_name,
                from_ref=None,
            )
        except GitError as e:
            raise QueuePlanError(
                f"Failed to create or resolve worktree for branch `{branch_name}`: {e}"
            ) from e

    return [
        MaterializedQueuePlanPrompt(
            prompt=item.prompt,
            working_directory_override=(
                worktree_paths_by_branch[item.branch_name] if item.branch_name else None
            ),
            parallel_group_id=item.parallel_group_id,
            parallel_limit=item.parallel_limit,
        )
        for item in expanded
    ]


def _parse_to_ast(text: str) -> list[_Node]:
    stack: list[_Frame] = [_Frame(kind="root", start_line=0)]

    for line_number, line in enumerate(text.splitlines(), start=1):
        marker = _parse_marker(line.strip(), strict=True)
        current = stack[-1]

        if marker is None:
            current.prompt_lines.append(line)
            continue

        _flush_prompt(current)
        marker_type = marker[0]

        if marker_type == "separator":
            continue

        if marker_type == "branch_start":
            branch_name = marker[1]
            # Allow `***branch-x` to act as a shorthand close marker
            # when the matching branch block is currently open.
            if current.kind == "branch" and current.branch_name == branch_name:
                _close_frame(stack)
            else:
                stack.append(_Frame(kind="branch", start_line=line_number, branch_name=branch_name))
            continue

        if marker_type == "branch_end":
            branch_name = marker[1]
            if current.kind != "branch":
                detail = _unexpected_block_close_detail(current)
                raise QueuePlanError(
                    f"Line {line_number}: found branch end marker for `{branch_name}` "
                    f"without matching open branch block. {detail}"
                )
            if current.branch_name != branch_name:
                raise QueuePlanError(
                    f"Line {line_number}: branch end `{branch_name}` does not match open "
                    f"branch `{current.branch_name}` from line {current.start_line}."
                )
            _close_frame(stack)
            continue

        if marker_type == "loop_start":
            stack.append(_Frame(kind="loop", start_line=line_number, loop_count=marker[1]))
            continue

        if marker_type == "loop_end":
            loop_count = marker[1]
            if current.kind != "loop":
                detail = _unexpected_block_close_detail(current)
                raise QueuePlanError(
                    f"Line {line_number}: found loop end marker for `{loop_count}` "
                    f"without matching open loop block. {detail}"
                )
            if current.loop_count != loop_count:
                raise QueuePlanError(
                    f"Line {line_number}: loop end `{loop_count}` does not match open "
                    f"loop `{current.loop_count}` from line {current.start_line}."
                )
            _close_frame(stack)
            continue

        if marker_type == "parallel_start":
            if current.kind == "parallel":
                raise QueuePlanError(
                    f"Line {line_number}: nested parallel blocks are not supported."
                )
            stack.append(
                _Frame(kind="parallel", start_line=line_number, parallel_limit=marker[1])
            )
            continue

        if marker_type == "parallel_end":
            if current.kind != "parallel":
                detail = _unexpected_block_close_detail(current)
                raise QueuePlanError(
                    f"Line {line_number}: found parallel end marker without matching open "
                    f"parallel block. {detail}"
                )
            _close_frame(stack)
            continue

        raise QueuePlanError(f"Line {line_number}: unsupported queue-plan marker.")

    _flush_prompt(stack[-1])
    # End markers are optional. Any still-open blocks are treated as running to EOF.
    while len(stack) > 1:
        _close_frame(stack)

    return stack[0].nodes


def _close_frame(stack: list[_Frame]) -> None:
    """Close the current non-root frame and append it to its parent."""
    finished = stack.pop()
    if finished.kind == "branch":
        stack[-1].nodes.append(
            _BranchNode(branch_name=finished.branch_name or "", children=finished.nodes)
        )
        return
    if finished.kind == "loop":
        stack[-1].nodes.append(_LoopNode(count=finished.loop_count or 1, children=finished.nodes))
        return
    if finished.kind == "parallel":
        stack[-1].nodes.append(
            _ParallelNode(limit=finished.parallel_limit, children=finished.nodes)
        )
        return
    raise QueuePlanError("Unsupported queue-plan frame type.")


def _unexpected_block_close_detail(current: _Frame) -> str:
    """Describe what block is currently open and how to close it."""
    if current.kind == "root":
        return "No block is currently open."
    if current.kind == "branch":
        branch_name = current.branch_name or ""
        return (
            f"You are currently inside branch `{branch_name}` opened on line "
            f"{current.start_line}. Close it first with `***branch-{branch_name}` "
            f"or `***branch-{branch_name}-end`."
        )
    if current.kind == "loop":
        loop_count = current.loop_count or 1
        return (
            f"You are currently inside loop `{loop_count}` opened on line "
            f"{current.start_line}. Close it first with `***loop-{loop_count}-end`."
        )
    if current.kind == "parallel":
        return (
            f"You are currently inside parallel block opened on line {current.start_line}. "
            "Close it first with `***parallel-end`."
        )
    return "A different block is currently open."


def _expand_nodes(
    nodes: list[_Node],
    active_branch: Optional[str],
    active_parallel_group_id: Optional[str],
    active_parallel_limit: Optional[int],
    out: list[QueuePlanPrompt],
    max_items: int,
    group_counter: list[int],
) -> None:
    for node in nodes:
        if isinstance(node, _PromptNode):
            if len(out) >= max_items:
                raise QueuePlanError(
                    f"Structured queue plan expands to more than {max_items} items."
                )
            out.append(
                QueuePlanPrompt(
                    prompt=node.prompt,
                    branch_name=active_branch,
                    parallel_group_id=active_parallel_group_id,
                    parallel_limit=active_parallel_limit,
                )
            )
            continue

        if isinstance(node, _BranchNode):
            _expand_nodes(
                node.children,
                active_branch=node.branch_name,
                active_parallel_group_id=active_parallel_group_id,
                active_parallel_limit=active_parallel_limit,
                out=out,
                max_items=max_items,
                group_counter=group_counter,
            )
            continue

        if isinstance(node, _LoopNode):
            for _ in range(node.count):
                _expand_nodes(
                    node.children,
                    active_branch=active_branch,
                    active_parallel_group_id=active_parallel_group_id,
                    active_parallel_limit=active_parallel_limit,
                    out=out,
                    max_items=max_items,
                    group_counter=group_counter,
                )
            continue

        if isinstance(node, _ParallelNode):
            group_counter[0] += 1
            _expand_nodes(
                node.children,
                active_branch=active_branch,
                active_parallel_group_id=f"parallel-{group_counter[0]}",
                active_parallel_limit=node.limit,
                out=out,
                max_items=max_items,
                group_counter=group_counter,
            )
            continue

        raise QueuePlanError("Unsupported queue-plan node type.")


def _flush_prompt(frame: _Frame) -> None:
    if not frame.prompt_lines:
        return

    raw_text = "\n".join(frame.prompt_lines).strip("\n")
    frame.prompt_lines.clear()
    if raw_text.strip():
        frame.nodes.append(_PromptNode(prompt=raw_text))


def _parse_marker(line: str, strict: bool) -> tuple[str, str | int] | tuple[str] | None:
    if line == "***":
        return ("separator",)

    parallel_end = _PARALLEL_END_RE.match(line)
    if parallel_end:
        return ("parallel_end",)

    parallel_start = _PARALLEL_START_RE.match(line)
    if parallel_start:
        return _parse_parallel_marker_value(parallel_start.group(1), line)

    loop_end = _LOOP_END_RE.match(line)
    if loop_end:
        return _parse_loop_marker_value(loop_end.group(1), line, marker_type="loop_end")

    loop_start = _LOOP_START_RE.match(line)
    if loop_start:
        return _parse_loop_marker_value(loop_start.group(1), line, marker_type="loop_start")

    branch_end = _BRANCH_END_RE.match(line)
    if branch_end:
        return _parse_branch_marker_value(branch_end.group(1), marker_type="branch_end")

    branch_start = _BRANCH_START_RE.match(line)
    if branch_start:
        return _parse_branch_marker_value(branch_start.group(1), marker_type="branch_start")

    if strict and _ANY_MARKER_RE.match(line):
        raise QueuePlanError(f"Unknown queue-plan marker: `{line}`")
    return None


def _parse_loop_marker_value(count_text: str, line: str, marker_type: str) -> tuple[str, int]:
    """Parse and validate loop marker payload."""
    count = int(count_text)
    if count < 1:
        raise QueuePlanError(
            f"Invalid loop count `{count}` in marker `{line}`. Loop counts must be >= 1."
        )
    return marker_type, count


def _parse_branch_marker_value(branch_text: str, marker_type: str) -> tuple[str, str]:
    """Parse and validate branch marker payload."""
    branch_name = branch_text.strip()
    if not branch_name:
        if marker_type == "branch_end":
            raise QueuePlanError("Branch end marker must include a branch name.")
        raise QueuePlanError("Branch marker must include a branch name.")
    return marker_type, branch_name


def _parse_parallel_marker_value(
    limit_text: Optional[str], line: str
) -> tuple[str, Optional[int]]:
    """Parse and validate parallel marker payload."""
    if limit_text is None:
        return "parallel_start", None

    limit = int(limit_text)
    if limit < 1:
        raise QueuePlanError(
            f"Invalid parallel width `{limit}` in marker `{line}`. Width must be >= 1."
        )
    return "parallel_start", limit
