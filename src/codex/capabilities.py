"""Codex capability mappings and Slack compatibility helpers."""

import re
from dataclasses import dataclass
from typing import Optional

# Alias surface users expect from Claude-mode `/mode`.
COMPAT_MODE_ALIASES: tuple[str, ...] = (
    "bypass",
    "ask",
    "default",
    "plan",
    "accept",
    "delegate",
)

# `/mode` aliases that Codex actually supports.
SUPPORTED_COMPAT_MODE_ALIASES: tuple[str, ...] = (
    "bypass",
    "ask",
    "default",
    "plan",
)

# Claude slash commands that are not routed through Codex.
CLAUDE_ONLY_SLASH_COMMANDS: tuple[str, ...] = (
    "/compact",
    "/cost",
    "/claude-help",
    "/doctor",
    "/claude-config",
    "/context",
    "/init",
    "/memory",
    "/stats",
    "/todos",
)

_CLAUDE_TO_CODEX_HINTS: dict[str, str] = {
    "/compact": "Use `/clear` to reset the conversation in Slack mode.",
    "/cost": "Use `/usage` and per-response footer cost metadata.",
    "/claude-help": "Use `/usage`, `/mode`, and `/model`.",
    "/doctor": "Use local CLI diagnostics outside Slack.",
    "/claude-config": "Use `/usage`, `/mode approval ...`, and `/mode sandbox ...`.",
    "/context": "Use Slack thread history and `/usage`.",
    "/init": "Codex does not provide `/init` in this Slack integration.",
    "/memory": "Codex does not use CLAUDE.md memory files.",
    "/stats": "Use `/usage` for Codex session status in Slack integration.",
    "/todos": "Use normal prompts to manage TODO tracking.",
}

_COMPAT_TO_APPROVAL: dict[str, str] = {
    "bypass": "never",
    "ask": "on-request",
    "default": "on-request",
}

_UNSUPPORTED_COMPAT_MODE_MESSAGES: dict[str, str] = {
    "accept": ("`/mode accept` maps to Claude file-edit approvals and has no Codex equivalent."),
    "delegate": ("`/mode delegate` is Claude-specific and has no Codex equivalent."),
}


@dataclass(frozen=True)
class CodexModeResolution:
    """Resolved Codex settings for a compatibility mode alias."""

    approval_mode: Optional[str]
    error: Optional[str] = None


def normalize_codex_approval_mode(approval_mode: Optional[str]) -> str:
    """Normalize approval mode to a supported Codex value."""
    if not approval_mode:
        return "on-request"

    mode = approval_mode.strip().lower()
    if mode in {"untrusted", "on-request", "never"}:
        return mode
    return "on-request"


def codex_mode_alias_for_approval(approval_mode: Optional[str]) -> str:
    """Derive best-effort `/mode` compatibility alias from Codex approval mode."""
    mode = normalize_codex_approval_mode(approval_mode)
    if mode == "never":
        return "bypass"
    return "ask"


def resolve_codex_compat_mode(alias: str) -> CodexModeResolution:
    """Map `/mode` compatibility alias to Codex settings."""
    normalized = (alias or "").strip().lower()

    if normalized in _COMPAT_TO_APPROVAL:
        return CodexModeResolution(approval_mode=_COMPAT_TO_APPROVAL[normalized])

    if normalized == "plan":
        return CodexModeResolution(approval_mode="on-request")

    if normalized in _UNSUPPORTED_COMPAT_MODE_MESSAGES:
        return CodexModeResolution(
            approval_mode=None,
            error=_UNSUPPORTED_COMPAT_MODE_MESSAGES[normalized],
        )

    valid = ", ".join(f"`{name}`" for name in SUPPORTED_COMPAT_MODE_ALIASES)
    return CodexModeResolution(
        approval_mode=None,
        error=f"Unknown mode: `{normalized}`. Valid compatibility modes: {valid}.",
    )


def apply_codex_mode_to_prompt(prompt: str, permission_mode: Optional[str]) -> str:
    """Adjust Codex prompt behavior based on Slack session mode."""
    mode = (permission_mode or "").strip().lower()
    if mode != "plan":
        return prompt

    return (
        f"{prompt}\n\n"
        "[Plan mode: Provide a concrete implementation plan first. "
        "Do not execute commands or edit files yet. "
        "Wait for user confirmation before making changes.]"
    )


def is_likely_plan_content(text: Optional[str]) -> bool:
    """Heuristically detect whether assistant output is an actionable plan.

    Avoids opening approval UI for non-plan replies like greetings or clarifications.
    """
    if not text:
        return False

    normalized = text.strip()
    if len(normalized) < 100:
        return False

    lowered = normalized.lower()
    early_exit_markers = (
        "share the change you want",
        "i don't have an actual scoped change",
        "please send the specific task",
        "ready to help",
    )
    if any(marker in lowered for marker in early_exit_markers):
        return False

    has_heading = bool(
        re.search(
            r"(?im)^\s{0,3}(#{1,6}\s+\S+|implementation plan\s*:|plan\s*:)",
            normalized,
        )
    )

    numbered_steps = len(re.findall(r"(?im)^\s*(?:\d+\.\s+|\d+\)\s+)", normalized))
    bullet_steps = len(re.findall(r"(?im)^\s*[-*]\s+", normalized))

    section_keywords = (
        "implementation steps",
        "acceptance criteria",
        "risks",
        "test plan",
        "validation",
        "rollout",
        "timeline",
        "milestones",
    )
    keyword_hits = sum(1 for keyword in section_keywords if keyword in lowered)

    if numbered_steps >= 3:
        return True
    if numbered_steps >= 2 and (has_heading or keyword_hits >= 1):
        return True
    if has_heading and keyword_hits >= 2 and (numbered_steps >= 1 or bullet_steps >= 3):
        return True
    return False


def is_claude_only_slash_command(command: str) -> bool:
    """Return True if the command is Claude-specific and not routed for Codex."""
    return command in CLAUDE_ONLY_SLASH_COMMANDS


def get_codex_hint_for_claude_command(command: str) -> str:
    """Get Codex guidance for a Claude-only slash command."""
    return _CLAUDE_TO_CODEX_HINTS.get(
        command,
        "Use `/usage`, `/mode approval ...`, `/mode sandbox ...`, or direct prompts.",
    )
