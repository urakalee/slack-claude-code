"""Codex config and requirements command handlers."""

from __future__ import annotations

import json
from typing import Any

from slack_bolt.async_app import AsyncApp

from src.config import config
from src.utils.formatters.command import error_message

from ..base import CommandContext, HandlerDependencies, slack_command

_SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "token",
    "secret",
    "key",
    "password",
    "credential",
    "auth",
)
_MAX_RAW_KEYS = 40
_MAX_VALUE_CHARS = 200


def _is_sensitive_key(key: str) -> bool:
    """Return True if key name indicates sensitive data."""
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def _sanitize_value(value: Any) -> Any:
    """Redact sensitive-ish values and truncate long content for Slack display."""
    if isinstance(value, str):
        return (
            value[:_MAX_VALUE_CHARS] + "...(truncated)"
            if len(value) > _MAX_VALUE_CHARS
            else value
        )
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, nested in value.items():
            if _is_sensitive_key(str(key)):
                sanitized[str(key)] = "***REDACTED***"
            else:
                sanitized[str(key)] = _sanitize_value(nested)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    return value


def _extract_requirements(payload: dict) -> list[dict]:
    """Extract requirements list from varying app-server payload shapes."""
    candidates = [
        payload.get("requirements"),
        payload.get("data"),
        payload.get("items"),
    ]
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _extract_items(payload: dict, keys: tuple[str, ...]) -> list[dict]:
    """Extract list payload from common app-server response shapes."""
    for key in keys:
        candidate = payload.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    return []


def _missing_requirements(requirements: list[dict]) -> list[dict]:
    """Return required requirements that are not satisfied."""
    missing: list[dict] = []
    for requirement in requirements:
        required = bool(requirement.get("required", True))
        satisfied = bool(
            requirement.get(
                "satisfied",
                requirement.get("isSatisfied", requirement.get("met", False)),
            )
        )
        if required and not satisfied:
            missing.append(requirement)
    return missing


async def _post_config_summary(
    ctx: CommandContext,
    deps: HandlerDependencies,
    working_directory: str,
    fallback_model: str,
    fallback_sandbox: str,
    fallback_approval: str,
) -> None:
    """Post compact config summary and requirement health."""
    config_data = await deps.codex_executor.config_read(working_directory)
    requirements_data = await deps.codex_executor.config_requirements_read(
        working_directory
    )

    resolved = config_data.get("config")
    if not isinstance(resolved, dict):
        resolved = config_data
    requirements = _extract_requirements(requirements_data)
    missing = _missing_requirements(requirements)

    model_value = resolved.get("model", fallback_model)
    sandbox_value = resolved.get("sandbox", fallback_sandbox)
    approval_value = resolved.get(
        "approvalPolicy",
        resolved.get("approval", fallback_approval),
    )
    source_value = resolved.get("source", resolved.get("configSource", "unknown"))
    health_value = "ok" if not missing else f"missing {len(missing)}"

    text = (
        "*Codex Config Summary*\n"
        f"• cwd: `{working_directory}`\n"
        f"• model: `{model_value}`\n"
        f"• sandbox: `{sandbox_value}`\n"
        f"• approval: `{approval_value}`\n"
        f"• source: `{source_value}`\n"
        f"• requirement health: `{health_value}`"
    )
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        thread_ts=ctx.thread_ts,
        text="Codex config summary",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


async def _post_config_requirements(
    ctx: CommandContext,
    deps: HandlerDependencies,
    working_directory: str,
) -> None:
    """Post unsatisfied config requirements with remediation hints when present."""
    requirements_data = await deps.codex_executor.config_requirements_read(
        working_directory
    )
    requirements = _extract_requirements(requirements_data)
    missing = _missing_requirements(requirements)
    if not missing:
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            thread_ts=ctx.thread_ts,
            text="Codex config requirements",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": ":white_check_mark: No missing required Codex config requirements.",
                    },
                }
            ],
        )
        return

    lines = []
    for requirement in missing[:20]:
        name = requirement.get("name", requirement.get("id", "unknown"))
        severity = requirement.get("severity", "required")
        reason = requirement.get(
            "reason", requirement.get("message", "missing requirement")
        )
        remediation = requirement.get("remediation", requirement.get("fix", ""))
        line = f"• *{name}* (`{severity}`)\n{reason}"
        if remediation:
            line += f"\nremediation: {remediation}"
        lines.append(line)
    if len(missing) > 20:
        lines.append(f"...and {len(missing) - 20} more.")

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        thread_ts=ctx.thread_ts,
        text="Codex config requirements",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Missing Codex Config Requirements*\n"
                    + "\n\n".join(lines),
                },
            }
        ],
    )


async def _post_config_raw(
    ctx: CommandContext,
    deps: HandlerDependencies,
    working_directory: str,
) -> None:
    """Post sanitized raw config JSON snippet."""
    config_data = await deps.codex_executor.config_read(working_directory)
    resolved = config_data.get("config")
    if not isinstance(resolved, dict):
        resolved = (
            config_data if isinstance(config_data, dict) else {"value": config_data}
        )

    sanitized: dict[str, Any] = {}
    keys = list(resolved.keys())
    for key in keys[:_MAX_RAW_KEYS]:
        if _is_sensitive_key(str(key)):
            sanitized[str(key)] = "***REDACTED***"
        else:
            sanitized[str(key)] = _sanitize_value(resolved[key])
    truncated = len(keys) > _MAX_RAW_KEYS

    snippet = json.dumps(sanitized, indent=2, sort_keys=True)
    text = f"*Codex Config (sanitized)*\n```json\n{snippet}\n```"
    if truncated:
        text += f"\n_Showing first {_MAX_RAW_KEYS} keys out of {len(keys)}._"

    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        thread_ts=ctx.thread_ts,
        text="Codex raw config",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


async def _post_model_list(
    ctx: CommandContext, deps: HandlerDependencies, working_directory: str, limit: int
) -> None:
    """Post detailed model inventory."""
    model_data = await deps.codex_executor.model_list(working_directory)
    models = _extract_items(model_data, ("data", "models", "items"))
    if not models:
        text = "No models returned by app-server."
    else:
        lines = []
        for model in models[:limit]:
            model_id = model.get("id", model.get("name", "unknown"))
            provider = model.get("provider", "default")
            default_effort = model.get("defaultEffort", model.get("effort", "n/a"))
            lines.append(
                f"• `{model_id}`\nprovider: `{provider}` • default effort: `{default_effort}`"
            )
        text = "*Codex Models*\n" + "\n\n".join(lines)
        if len(models) > limit:
            text += f"\n\n_Showing {limit} of {len(models)} models._"
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        thread_ts=ctx.thread_ts,
        text="Codex models",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


async def _post_account_details(
    ctx: CommandContext, deps: HandlerDependencies, working_directory: str
) -> None:
    """Post detailed account metadata."""
    account_data = await deps.codex_executor.account_read(working_directory)
    account = account_data.get("account")
    if not isinstance(account, dict):
        text = "No account metadata returned."
    else:
        lines = []
        for key in sorted(account.keys()):
            value = account[key]
            if _is_sensitive_key(str(key)):
                formatted = "***REDACTED***"
            else:
                formatted = _sanitize_value(value)
            lines.append(f"• *{key}:* `{formatted}`")
        text = "*Codex Account Details*\n" + "\n".join(lines)
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        thread_ts=ctx.thread_ts,
        text="Codex account details",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


async def _post_feature_list(
    ctx: CommandContext, deps: HandlerDependencies, working_directory: str, limit: int
) -> None:
    """Post experimental feature listing."""
    feature_data = await deps.codex_executor.experimental_feature_list(
        working_directory
    )
    features = _extract_items(feature_data, ("data", "features", "items"))
    if not features:
        text = "No experimental features reported."
    else:
        lines = []
        for feature in features[:limit]:
            name = feature.get("name", feature.get("id", "unknown"))
            status = feature.get("status", "unknown")
            description = feature.get("description", "")
            line = f"• `{name}` status=`{status}`"
            if description:
                line += f"\n{description}"
            lines.append(line)
        text = "*Codex Experimental Features*\n" + "\n\n".join(lines)
        if len(features) > limit:
            text += f"\n\n_Showing {limit} of {len(features)} features._"
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        thread_ts=ctx.thread_ts,
        text="Codex experimental features",
        blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
    )


def register_codex_config_commands(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register `/codex-config` command handlers."""

    @app.command("/codex-config")
    @slack_command(require_text=False)
    async def handle_codex_config(
        ctx: CommandContext, deps: HandlerDependencies = deps
    ) -> None:
        """Show Codex configuration diagnostics from app-server."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, ctx.thread_ts, config.DEFAULT_WORKING_DIR
        )
        if session.get_backend() != "codex":
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="/codex-config is only available for Codex sessions.",
                blocks=error_message(
                    "`/codex-config` is only available in Codex sessions."
                ),
            )
            return
        if not deps.codex_executor:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text="Codex executor is not configured.",
                blocks=error_message("Codex executor is not configured."),
            )
            return

        tokens = ctx.text.split() if ctx.text else []
        subcommand = tokens[0].lower() if tokens else "summary"
        raw_limit = tokens[1] if len(tokens) > 1 else "20"
        try:
            list_limit = max(1, min(int(raw_limit), 100))
        except ValueError:
            list_limit = 20

        try:
            if subcommand in {"summary", "show"}:
                await _post_config_summary(
                    ctx=ctx,
                    deps=deps,
                    working_directory=session.working_directory,
                    fallback_model=session.model or "(default)",
                    fallback_sandbox=session.sandbox_mode or config.CODEX_SANDBOX_MODE,
                    fallback_approval=session.approval_mode
                    or config.CODEX_APPROVAL_MODE,
                )
                return
            if subcommand == "requirements":
                await _post_config_requirements(
                    ctx=ctx,
                    deps=deps,
                    working_directory=session.working_directory,
                )
                return
            if subcommand == "raw":
                await _post_config_raw(
                    ctx=ctx,
                    deps=deps,
                    working_directory=session.working_directory,
                )
                return
            if subcommand == "models":
                await _post_model_list(
                    ctx=ctx,
                    deps=deps,
                    working_directory=session.working_directory,
                    limit=list_limit,
                )
                return
            if subcommand == "account":
                await _post_account_details(
                    ctx=ctx,
                    deps=deps,
                    working_directory=session.working_directory,
                )
                return
            if subcommand == "features":
                await _post_feature_list(
                    ctx=ctx,
                    deps=deps,
                    working_directory=session.working_directory,
                    limit=list_limit,
                )
                return
            raise RuntimeError(
                "Usage: /codex-config [summary|requirements|raw|models [limit]|account|features [limit]]"
            )
        except Exception as e:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                thread_ts=ctx.thread_ts,
                text=f"/codex-config failed: {e}",
                blocks=error_message(str(e)),
            )
