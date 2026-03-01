"""Unit tests for Codex capability mappings."""

from src.codex.capabilities import (
    apply_codex_mode_to_prompt,
    codex_mode_alias_for_approval,
    get_codex_hint_for_claude_command,
    is_likely_plan_content,
    is_claude_only_slash_command,
    normalize_codex_approval_mode,
    resolve_codex_compat_mode,
)


class TestCodexModeMappings:
    """Tests for `/mode` compatibility behavior in Codex sessions."""

    def test_bypass_maps_to_never(self):
        """bypass alias maps to approval=never."""
        resolved = resolve_codex_compat_mode("bypass")
        assert resolved.approval_mode == "never"
        assert resolved.error is None

    def test_ask_and_default_map_to_on_request(self):
        """ask/default aliases map to approval=on-request."""
        ask = resolve_codex_compat_mode("ask")
        default = resolve_codex_compat_mode("default")

        assert ask.approval_mode == "on-request"
        assert default.approval_mode == "on-request"

    def test_plan_maps_to_on_request(self):
        """plan alias remains valid and maps to approval=on-request."""
        resolved = resolve_codex_compat_mode("plan")
        assert resolved.approval_mode == "on-request"
        assert resolved.error is None

    def test_unknown_mode_lists_codex_supported_modes(self):
        """Unknown mode errors list only Codex-supported compatibility aliases."""
        resolved = resolve_codex_compat_mode("random")
        assert resolved.approval_mode is None
        assert resolved.error is not None
        assert "`bypass`" in resolved.error
        assert "`ask`" in resolved.error
        assert "`default`" in resolved.error
        assert "`plan`" in resolved.error
        assert "`accept`" not in resolved.error
        assert "`delegate`" not in resolved.error

    def test_approval_mode_normalization(self):
        """Unknown approvals are normalized to supported values."""
        assert normalize_codex_approval_mode("invalid-mode") == "on-request"
        assert normalize_codex_approval_mode("never") == "never"

    def test_mode_alias_derivation(self):
        """Best-effort compatibility alias derives from approval mode."""
        assert codex_mode_alias_for_approval("never") == "bypass"
        assert codex_mode_alias_for_approval("on-request") == "ask"

    def test_plan_mode_prompt_enrichment(self):
        """Plan mode adds plan-only instruction to the prompt."""
        prompt = apply_codex_mode_to_prompt("Implement feature X", "plan")
        assert "Provide a concrete implementation plan first" in prompt
        assert "Do not execute commands or edit files yet" in prompt

    def test_non_plan_prompt_unchanged(self):
        """Non-plan mode leaves the prompt untouched."""
        prompt = apply_codex_mode_to_prompt("Implement feature X", "default")
        assert prompt == "Implement feature X"

    def test_plan_content_detector_rejects_clarification_text(self):
        """Short clarification messages should not trigger plan approval."""
        text = (
            "Ready to help. Share the change you want, and I will provide a concrete "
            "implementation plan first, then wait for your confirmation."
        )
        assert is_likely_plan_content(text) is False

    def test_plan_content_detector_accepts_structured_plan(self):
        """Structured plan output should trigger plan approval."""
        text = """# Implementation Plan
1. Add request validation in the command handler.
2. Update persistence logic to store the new flag.
3. Add tests for success and error paths.

## Acceptance Criteria
- Validation rejects empty payloads.
- Existing behavior stays unchanged for valid inputs.

## Test Plan
- Run unit tests for command routing and persistence.
"""
        assert is_likely_plan_content(text) is True


class TestCodexCommandHints:
    """Tests for Claude-only command hints in Codex sessions."""

    def test_claude_only_command_detection(self):
        """Known Claude-only slash commands are detected."""
        assert is_claude_only_slash_command("/cost") is True
        assert is_claude_only_slash_command("/approval") is False

    def test_hint_generation(self):
        """A Codex hint is returned for Claude-only commands."""
        hint = get_codex_hint_for_claude_command("/cost")
        assert "/usage" in hint

    def test_claude_config_hint_points_to_usage(self):
        """Codex hint for /claude-config should include /usage."""
        hint = get_codex_hint_for_claude_command("/claude-config")
        assert "/usage" in hint

    def test_stats_hint_points_to_usage(self):
        """Codex hint for /stats should include /usage."""
        hint = get_codex_hint_for_claude_command("/stats")
        assert "/usage" in hint
