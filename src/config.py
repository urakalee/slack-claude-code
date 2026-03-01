import functools
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.config_storage import get_storage

# Global constant for Claude plans directory
PLANS_DIR = str(Path.home() / ".claude" / "plans")

# Model-to-backend mapping
CLAUDE_MODELS: set[str] = {
    "opus",
    "sonnet",
    "haiku",
    "claude-opus-4",
    "claude-opus-4-5-20250929",
    "claude-opus-4-6",
    "claude-sonnet-4",
    "claude-sonnet-4-5",
    "claude-haiku-4",
}

CODEX_MODELS: set[str] = {
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.1-codex-max",
    "gpt-5.2",
    "gpt-5.1-codex-mini",
    "gpt-5-codex",
    "gpt-5",
    "codex",
    "o3",
    "o4-mini",
}


EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh")


def parse_model_effort(model: str) -> tuple[str, Optional[str]]:
    """Parse effort suffix from a Codex model name.

    Parameters
    ----------
    model : str
        Model name, possibly with effort suffix (e.g., "gpt-5.3-codex-high").

    Returns
    -------
    tuple[str, Optional[str]]
        (base_model, effort_level) — effort_level is None if no suffix found.
    """
    model_lower = model.lower()
    # Check xhigh before high since "-high" is a suffix of "-xhigh"
    for suffix in ("-xhigh", "-medium", "-high", "-low"):
        if model_lower.endswith(suffix):
            return model[: -len(suffix)], suffix[1:]
    return model, None


def get_backend_for_model(model: Optional[str]) -> str:
    """
    Determine which backend to use based on the model name.

    Args:
        model: The model name (e.g., "opus", "gpt-5-codex")

    Returns:
        "claude" or "codex"
    """
    if model is None:
        return "claude"  # Default to Claude

    model_lower = model.lower()

    # Check exact matches first
    if model_lower in CLAUDE_MODELS:
        return "claude"
    if model_lower in CODEX_MODELS:
        return "codex"

    # Check prefixes for extended model names
    if model_lower.startswith("claude"):
        return "claude"
    if model_lower.startswith("gpt") or model_lower.startswith("codex") or (model_lower.startswith("o") and len(model_lower) > 1 and model_lower[1:2].isdigit()):
        return "codex"

    # Default to Claude for unknown models
    return "claude"


class ExecutionTimeouts(BaseModel):
    """Timeout configuration for command execution."""

    usage_check: int = 30
    max_questions_per_conversation: int = 10

    @field_validator("usage_check", "max_questions_per_conversation")
    @classmethod
    def validate_positive(cls, v: int, info) -> int:
        """Ensure timeout values are positive integers."""
        if v <= 0:
            raise ValueError(f"{info.field_name} must be a positive integer, got {v}")
        return v


class SlackTimeouts(BaseModel):
    """Timeout configuration for Slack message updates."""

    message_update_throttle: float = 2.0
    heartbeat_interval: float = 15.0
    heartbeat_threshold: float = 20.0

    @field_validator("message_update_throttle", "heartbeat_interval", "heartbeat_threshold")
    @classmethod
    def validate_positive_float(cls, v: float, info) -> float:
        """Ensure timeout values are positive."""
        if v <= 0:
            raise ValueError(f"{info.field_name} must be positive, got {v}")
        return v


class CacheTimeouts(BaseModel):
    """Cache duration configuration."""

    usage: int = 60

    @field_validator("usage")
    @classmethod
    def validate_positive(cls, v: int, info) -> int:
        """Ensure cache duration is positive."""
        if v <= 0:
            raise ValueError(f"{info.field_name} must be a positive integer, got {v}")
        return v


class StreamingConfig(BaseModel):
    """Configuration for streaming message updates."""

    max_accumulated_size: int = 500000
    max_tools_display: int = 10
    tool_thread_threshold: int = 500


class LimitsConfig(BaseModel):
    """Configuration for input/output limits."""

    max_prompt_length: int = 50000  # Maximum command input length
    max_action_value_size: int = 1024 * 1024  # Max JSON payload in actions (1MB)
    plan_file_max_age_seconds: int = 300  # Time window for plan file discovery


class DisplayConfig(BaseModel):
    """Configuration for tool activity display truncation."""

    truncate_path_length: int = 45
    truncate_cmd_length: int = 50
    truncate_pattern_length: int = 40
    truncate_url_length: int = 50
    truncate_text_length: int = 40


class TimeoutConfig(BaseModel):
    """Centralized timeout configuration."""

    execution: ExecutionTimeouts = Field(default_factory=ExecutionTimeouts)
    slack: SlackTimeouts = Field(default_factory=SlackTimeouts)
    cache: CacheTimeouts = Field(default_factory=CacheTimeouts)
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)
    display: DisplayConfig = Field(default_factory=DisplayConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)


class EncryptedSettingsSource:
    """Settings source that reads from encrypted storage."""

    def __init__(self, settings_cls: type[BaseSettings]):
        self.settings_cls = settings_cls

    def __call__(self) -> dict[str, Any]:
        """Load settings from encrypted storage."""
        storage = get_storage()
        return storage.get_all()


class Config(BaseSettings):
    """
    Application configuration loaded from multiple sources.

    Priority (highest to lowest):
    1. Encrypted storage (~/.slack-claude-code/config.enc)
    2. Environment variables
    3. .env file
    4. Default values
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Customize settings sources to add encrypted storage with highest priority."""
        return (
            init_settings,
            EncryptedSettingsSource(settings_cls),  # Encrypted storage (highest priority)
            env_settings,  # Environment variables
            dotenv_settings,  # .env file
            file_secret_settings,
        )

    # Slack configuration
    SLACK_BOT_TOKEN: str = ""
    SLACK_APP_TOKEN: str = ""
    SLACK_SIGNING_SECRET: str = ""

    # Database - defaults to ~/.slack-claude-code/
    DATABASE_PATH: str = Field(
        default_factory=lambda: str(Path.home() / ".slack-claude-code" / "slack_claude.db")
    )
    DEFAULT_WORKING_DIR: str = Field(default_factory=lambda: str(Path.cwd()))

    # Claude Code configuration
    CLAUDE_PERMISSION_MODE: str = "bypassPermissions"
    DEFAULT_MODEL: Optional[str] = None

    # Default permission mode constant (used as fallback when invalid mode specified)
    DEFAULT_BYPASS_MODE: str = "bypassPermissions"

    # Slack API limits
    SLACK_BLOCK_TEXT_LIMIT: int = 2900
    SLACK_FILE_THRESHOLD: int = 2000
    SLACK_MAX_BLOCKS_PER_MESSAGE: int = 50

    # Valid permission modes for Claude Code CLI
    VALID_PERMISSION_MODES: tuple[str, ...] = (
        "acceptEdits",
        "bypassPermissions",
        "default",
        "delegate",
        "dontAsk",
        "plan",
    )

    # Permissions - stored as comma-separated string, converted to list via property
    AUTO_APPROVE_TOOLS_STR: str = Field(default="", alias="AUTO_APPROVE_TOOLS")
    ALLOWED_TOOLS: Optional[str] = None

    # File upload configuration
    MAX_FILE_SIZE_MB: int = 10
    MAX_UPLOAD_STORAGE_MB: int = 100

    # GitHub repository for web viewer links
    GITHUB_REPO: str = ""

    # Command suffix configuration
    COMMAND_SUFFIX: str = Field(
        default="",
        description="Global suffix for all Slack commands (e.g., 'cc' -> /ls-cc, /cd-cc)",
    )

    # Codex configuration
    CODEX_SANDBOX_MODE: str = "workspace-write"
    CODEX_APPROVAL_MODE: str = "on-request"

    # Valid sandbox modes for Codex CLI
    VALID_SANDBOX_MODES: tuple[str, ...] = (
        "read-only",
        "workspace-write",
        "danger-full-access",
    )

    # Valid approval modes for Codex CLI
    VALID_APPROVAL_MODES: tuple[str, ...] = (
        "untrusted",
        "on-failure",
        "on-request",
        "never",
    )

    # PTY session configuration (for Codex)
    USE_PTY_SESSIONS: bool = True
    PTY_MAX_SESSIONS: int = 10
    PTY_IDLE_TIMEOUT_MINUTES: int = 30
    PTY_CLEANUP_INTERVAL_SECONDS: int = 60

    # Execution timeout overrides from environment
    USAGE_CHECK_TIMEOUT: int = 30
    MAX_QUESTIONS_PER_CONVERSATION: int = 10

    # Slack timeout overrides from environment
    MESSAGE_UPDATE_THROTTLE: float = 2.0

    # Cache timeout overrides from environment
    USAGE_CACHE_DURATION: int = 60

    # Streaming config overrides from environment
    MAX_ACCUMULATED_SIZE: int = 500000
    MAX_TOOLS_DISPLAY: int = 10
    TOOL_THREAD_THRESHOLD: int = 500

    # Display config overrides from environment
    TRUNCATE_PATH_LENGTH: int = 45
    TRUNCATE_CMD_LENGTH: int = 50
    TRUNCATE_PATTERN_LENGTH: int = 40
    TRUNCATE_URL_LENGTH: int = 50
    TRUNCATE_TEXT_LENGTH: int = 40

    @property
    def AUTO_APPROVE_TOOLS(self) -> list[str]:
        """Parse AUTO_APPROVE_TOOLS from comma-separated string."""
        if not self.AUTO_APPROVE_TOOLS_STR:
            return []
        return [t.strip() for t in self.AUTO_APPROVE_TOOLS_STR.split(",") if t.strip()]

    @functools.cached_property
    def timeouts(self) -> TimeoutConfig:
        """Build TimeoutConfig from environment variables."""
        return TimeoutConfig(
            execution=ExecutionTimeouts(
                usage_check=self.USAGE_CHECK_TIMEOUT,
                max_questions_per_conversation=self.MAX_QUESTIONS_PER_CONVERSATION,
            ),
            slack=SlackTimeouts(
                message_update_throttle=self.MESSAGE_UPDATE_THROTTLE,
            ),
            cache=CacheTimeouts(
                usage=self.USAGE_CACHE_DURATION,
            ),
            streaming=StreamingConfig(
                max_accumulated_size=self.MAX_ACCUMULATED_SIZE,
                max_tools_display=self.MAX_TOOLS_DISPLAY,
                tool_thread_threshold=self.TOOL_THREAD_THRESHOLD,
            ),
            display=DisplayConfig(
                truncate_path_length=self.TRUNCATE_PATH_LENGTH,
                truncate_cmd_length=self.TRUNCATE_CMD_LENGTH,
                truncate_pattern_length=self.TRUNCATE_PATTERN_LENGTH,
                truncate_url_length=self.TRUNCATE_URL_LENGTH,
                truncate_text_length=self.TRUNCATE_TEXT_LENGTH,
            ),
        )

    def validate_required(self) -> list[str]:
        """Validate required configuration."""
        errors = []
        if not self.SLACK_BOT_TOKEN:
            errors.append("SLACK_BOT_TOKEN is required")
        if not self.SLACK_APP_TOKEN:
            errors.append("SLACK_APP_TOKEN is required (for Socket Mode)")
        if not self.SLACK_SIGNING_SECRET:
            errors.append("SLACK_SIGNING_SECRET is required")
        return errors


config = Config()
