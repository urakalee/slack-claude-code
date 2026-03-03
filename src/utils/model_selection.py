"""Shared model normalization and validation helpers for Slack handlers."""

from typing import Optional

from src.config import (
    CODEX_MODELS,
    EFFORT_LEVELS,
    get_backend_for_model,
    is_supported_codex_model,
    looks_like_codex_model,
    parse_model_effort,
)

CLAUDE_MODEL_DISPLAY: dict[str | None, str] = {
    None: "Default (recommended)",
    "default": "Default (recommended)",
    "opus": "Default (recommended)",
    "claude-opus-4-6": "Default (recommended)",
    "claude-opus-4-6[1m]": "Opus (1M context)",
    "sonnet": "Sonnet",
    "claude-sonnet-4-6": "Sonnet",
    "claude-sonnet-4-6[1m]": "Sonnet (1M context)",
    "haiku": "Haiku",
    "claude-haiku-4-5": "Haiku",
}

_CLAUDE_MODEL_ALIASES: dict[str, str | None] = {
    "default": None,
    "default (recommended)": None,
    "recommended": None,
    "opus": None,
    "opus-4.6": None,
    "claude-opus-4-6": None,
    "opus-1m": "claude-opus-4-6[1m]",
    "opus (1m context)": "claude-opus-4-6[1m]",
    "claude-opus-4-6[1m]": "claude-opus-4-6[1m]",
    "sonnet": "sonnet",
    "sonnet-4.6": "sonnet",
    "claude-sonnet-4-6": "sonnet",
    "sonnet-1m": "claude-sonnet-4-6[1m]",
    "sonnet (1m context)": "claude-sonnet-4-6[1m]",
    "claude-sonnet-4-6[1m]": "claude-sonnet-4-6[1m]",
    "haiku": "haiku",
    "haiku-4.5": "haiku",
    "claude-haiku-4-5": "haiku",
}

_CODEX_MODEL_ALIASES: dict[str, str] = {
    "codex": "gpt-5.3-codex",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.3-codex-spark": "gpt-5.3-codex-spark",
    "gpt-5.2-codex": "gpt-5.2-codex",
    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
    "gpt-5.2": "gpt-5.2",
    "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
}

_CLAUDE_DEFAULT_ALIASES: set[str] = {"default", "opus", "claude-opus-4-6"}


def normalize_model_name(model_name: str) -> Optional[str]:
    """Normalize model input into stored model identifier.

    Parameters
    ----------
    model_name : str
        User-supplied model alias or model identifier.

    Returns
    -------
    Optional[str]
        Canonical model ID for storage, or None for default model selection.
    """
    normalized = (model_name or "").strip().lower()
    if not normalized:
        return None

    base_name, effort = parse_model_effort(normalized)
    if base_name in _CLAUDE_MODEL_ALIASES:
        resolved_base = _CLAUDE_MODEL_ALIASES[base_name]
    else:
        resolved_base = _CODEX_MODEL_ALIASES.get(base_name, base_name)

    if resolved_base is None:
        return None
    if effort and looks_like_codex_model(resolved_base):
        return f"{resolved_base}-{effort}"
    return resolved_base


def normalize_current_model(model: Optional[str]) -> Optional[str]:
    """Normalize persisted current model aliases for UI display."""
    if model is None:
        return None
    lowered = model.strip().lower()
    if lowered in _CLAUDE_DEFAULT_ALIASES:
        return None
    return lowered


def model_display_name(model: Optional[str]) -> str:
    """Return human-readable display name for a model identifier."""
    normalized = normalize_current_model(model)
    return CLAUDE_MODEL_DISPLAY.get(normalized, normalized or "Default (recommended)")


def codex_model_validation_error(model: Optional[str]) -> Optional[str]:
    """Return validation error text for unsupported Codex model IDs."""
    if not model:
        return None
    if not looks_like_codex_model(model):
        return None
    if is_supported_codex_model(model):
        return None

    supported = "\n".join(f"• `{entry}`" for entry in sorted(CODEX_MODELS))
    effort_levels = ", ".join(f"`{level}`" for level in EFFORT_LEVELS)
    return (
        f"Unsupported Codex model: `{model}`\n\n"
        f"Supported Codex models:\n{supported}\n\n"
        f"Optional effort suffixes: {effort_levels}, `extra-high`"
    )


def backend_label_for_model(model: Optional[str]) -> str:
    """Return user-facing backend label for the selected model."""
    backend = get_backend_for_model(model)
    return "Claude Code" if backend == "claude" else "OpenAI Codex"
