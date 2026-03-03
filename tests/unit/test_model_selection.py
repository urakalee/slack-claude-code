"""Unit tests for shared model selection helpers."""

from src.utils.model_selection import (
    backend_label_for_model,
    codex_model_validation_error,
    model_display_name,
    normalize_current_model,
    normalize_model_name,
)


class TestNormalizeModelName:
    """Tests for model alias normalization."""

    def test_normalizes_claude_default_aliases(self):
        """Default aliases should normalize to None."""
        assert normalize_model_name("default") is None
        assert normalize_model_name("opus") is None
        assert normalize_model_name("recommended") is None

    def test_normalizes_claude_named_aliases(self):
        """Claude aliases should map to canonical Claude IDs."""
        assert normalize_model_name("sonnet") == "sonnet"
        assert normalize_model_name("sonnet-1m") == "claude-sonnet-4-6[1m]"
        assert normalize_model_name("haiku-4.5") == "haiku"

    def test_normalizes_codex_aliases_with_effort(self):
        """Codex aliases should map to canonical Codex IDs with effort suffixes."""
        assert normalize_model_name("codex") == "gpt-5.3-codex"
        assert normalize_model_name("codex-extra-high") == "gpt-5.3-codex-xhigh"
        assert normalize_model_name("gpt-5.1-codex-max-high") == "gpt-5.1-codex-max-high"

    def test_passthrough_for_unknown_non_empty_model(self):
        """Unknown model IDs should be preserved."""
        assert normalize_model_name("custom-model-id") == "custom-model-id"


class TestModelDisplayName:
    """Tests for model display helper."""

    def test_known_model_display_name(self):
        """Known model IDs should return friendly labels."""
        assert model_display_name("claude-opus-4-6[1m]") == "Opus (1M context)"
        assert model_display_name("haiku") == "Haiku"

    def test_unknown_model_display_name(self):
        """Unknown model IDs should display raw model name."""
        assert model_display_name("custom-model-id") == "custom-model-id"

    def test_default_model_display_name(self):
        """Default model should display recommended label."""
        assert model_display_name(None) == "Default (recommended)"


class TestCodexModelValidation:
    """Tests for Codex model validation helper."""

    def test_supported_codex_model_returns_no_error(self):
        """Supported Codex IDs should not return validation error."""
        assert codex_model_validation_error("gpt-5.3-codex-high") is None

    def test_invalid_codex_like_model_returns_error(self):
        """Unsupported Codex-like IDs should return formatted error text."""
        error = codex_model_validation_error("gpt-5")
        assert error is not None
        assert "Unsupported Codex model" in error
        assert "Supported Codex models" in error


class TestCurrentModelNormalization:
    """Tests for persisted-model normalization helper."""

    def test_normalize_current_model_defaults_to_none(self):
        """Persisted default aliases should normalize to None."""
        assert normalize_current_model("default") is None
        assert normalize_current_model("opus") is None
        assert normalize_current_model("claude-opus-4-6") is None

    def test_normalize_current_model_lowercases_values(self):
        """Persisted values should normalize to lower-case IDs."""
        assert normalize_current_model("GPT-5.3-CODEX-HIGH") == "gpt-5.3-codex-high"


class TestBackendLabelForModel:
    """Tests for backend label helper."""

    def test_backend_label_for_claude_and_codex(self):
        """Helper should return backend labels used in Slack copy."""
        assert backend_label_for_model(None) == "Claude Code"
        assert backend_label_for_model("gpt-5.3-codex") == "OpenAI Codex"
