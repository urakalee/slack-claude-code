"""Agents command handler: /agents.

Provides interactive management of configurable subagents.
"""

import uuid

from slack_bolt.async_app import AsyncApp

from src.agents.executor import AgentExecutor
from src.agents.models import AgentExecutionStatus, AgentSource
from src.agents.registry import get_registry
from src.config import config
from src.utils.formatters.base import markdown_to_mrkdwn
from src.utils.formatting import SlackFormatter
from src.utils.streaming import StreamingMessageState, create_streaming_callback

from ..base import CommandContext, HandlerDependencies, get_command_name, slack_command


def register_agents_command(app: AsyncApp, deps: HandlerDependencies) -> None:
    """Register /agents command and related actions.

    Commands:
    - /agents - List all available agents
    - /agents run <name> <task> - Run a specific agent
    - /agents create - Show creation instructions
    - /agents info <name> - Show agent details

    Parameters
    ----------
    app : AsyncApp
        The Slack Bolt async app.
    deps : HandlerDependencies
        Shared handler dependencies.
    """

    @app.command(get_command_name("/agents"))
    @slack_command(require_text=False, usage_hint="Usage: /agents [list|run|create|info]")
    async def handle_agents(ctx: CommandContext, deps: HandlerDependencies = deps):
        """Handle /agents command."""
        session = await deps.db.get_or_create_session(
            ctx.channel_id, thread_ts=ctx.thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        registry = get_registry(session.working_directory)

        parts = ctx.text.split(maxsplit=2) if ctx.text else []
        subcommand = parts[0].lower() if parts else "list"

        if subcommand in ("list", ""):
            await _handle_list(ctx, registry)

        elif subcommand == "run":
            if len(parts) < 3:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    blocks=SlackFormatter.error_message(
                        "Usage: `/agents run <agent-name> <task description>`"
                    ),
                )
                return
            agent_name = parts[1]
            task = parts[2]
            await _handle_run(ctx, deps, registry, session, agent_name, task)

        elif subcommand == "create":
            await _handle_create(ctx)

        elif subcommand == "info":
            if len(parts) < 2:
                await ctx.client.chat_postMessage(
                    channel=ctx.channel_id,
                    blocks=SlackFormatter.error_message("Usage: `/agents info <agent-name>`"),
                )
                return
            agent_name = parts[1]
            await _handle_info(ctx, registry, agent_name)

        else:
            await ctx.client.chat_postMessage(
                channel=ctx.channel_id,
                blocks=SlackFormatter.error_message(
                    f"Unknown subcommand: `{subcommand}`\n\n"
                    "Valid commands: `list`, `run`, `create`, `info`"
                ),
            )

    # Action handler for run buttons
    @app.action("agent_run")
    async def handle_agent_run_action(ack, action, body, client, logger):
        """Handle agent run button click - open modal for task input."""
        await ack()

        agent_name = action["value"]
        channel_id = body["channel"]["id"]
        thread_ts = body.get("message", {}).get("thread_ts")

        await client.views_open(
            trigger_id=body["trigger_id"],
            view={
                "type": "modal",
                "callback_id": "agent_run_submit",
                "private_metadata": f"{agent_name}|{channel_id}|{thread_ts or ''}",
                "title": {"type": "plain_text", "text": f"Run {agent_name}"},
                "submit": {"type": "plain_text", "text": "Run"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {
                        "type": "input",
                        "block_id": "task_input",
                        "element": {
                            "type": "plain_text_input",
                            "action_id": "task",
                            "multiline": True,
                            "placeholder": {
                                "type": "plain_text",
                                "text": "Describe the task for this agent...",
                            },
                        },
                        "label": {"type": "plain_text", "text": "Task"},
                    }
                ],
            },
        )

    @app.view("agent_run_submit")
    async def handle_agent_run_submit(ack, body, client, view, logger):
        """Handle agent run modal submission."""
        await ack()

        metadata_parts = view["private_metadata"].split("|")
        agent_name = metadata_parts[0]
        channel_id = metadata_parts[1]
        thread_ts = metadata_parts[2] if len(metadata_parts) > 2 and metadata_parts[2] else None

        task = view["state"]["values"]["task_input"]["task"]["value"]

        session = await deps.db.get_or_create_session(
            channel_id, thread_ts=thread_ts, default_cwd=config.DEFAULT_WORKING_DIR
        )

        registry = get_registry(session.working_directory)
        agent = registry.get(agent_name)

        if not agent:
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                blocks=SlackFormatter.error_message(f"Agent not found: `{agent_name}`"),
            )
            return

        # Post initial message
        response = await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f":robot_face: Running agent `{agent_name}`...\n\n> {task[:200]}",
                    },
                },
            ],
        )

        # Execute agent with streaming
        await _run_agent_with_streaming(
            deps, client, logger, channel_id, thread_ts, response["ts"],
            agent, task, session
        )


async def _handle_list(ctx: CommandContext, registry) -> None:
    """List all available agents.

    Parameters
    ----------
    ctx : CommandContext
        Command context.
    registry : AgentRegistry
        Agent registry.
    """
    agents = registry.list_all()

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "Available Agents", "emoji": True},
        },
        {"type": "divider"},
    ]

    # Group by source
    for source, label in [
        (AgentSource.BUILTIN, ":gear: Built-in Agents"),
        (AgentSource.USER, ":bust_in_silhouette: User Agents (~/.claude/agents/)"),
        (AgentSource.PROJECT, ":file_folder: Project Agents (.claude/agents/)"),
    ]:
        source_agents = [a for a in agents if a.source == source]
        if source_agents:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{label}*"},
                }
            )

            for agent in source_agents:
                model_info = f" ({agent.model.value})" if agent.model.value != "inherit" else ""
                desc = agent.description[:100] + "..." if len(agent.description) > 100 else agent.description
                blocks.append(
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"`{agent.name}`{model_info}\n_{desc}_",
                        },
                        "accessory": {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "Run"},
                            "action_id": "agent_run",
                            "value": agent.name,
                        },
                    }
                )

            blocks.append({"type": "divider"})

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Use `/agents run <name> <task>` or click Run button | "
                    "`/agents info <name>` for details | "
                    "`/agents create` for instructions",
                }
            ],
        }
    )

    await ctx.client.chat_postMessage(channel=ctx.channel_id, blocks=blocks)


async def _handle_run(ctx, deps, registry, session, agent_name: str, task: str) -> None:
    """Run a specific agent.

    Parameters
    ----------
    ctx : CommandContext
        Command context.
    deps : HandlerDependencies
        Handler dependencies.
    registry : AgentRegistry
        Agent registry.
    session : Session
        Database session.
    agent_name : str
        Name of agent to run.
    task : str
        Task to execute.
    """
    agent = registry.get(agent_name)
    if not agent:
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=SlackFormatter.error_message(f"Agent not found: `{agent_name}`"),
        )
        return

    # Post initial message
    response = await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        thread_ts=ctx.thread_ts,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":robot_face: Running agent `{agent_name}`...\n\n> {task[:200]}",
                },
            },
        ],
    )

    await _run_agent_with_streaming(
        deps, ctx.client, ctx.logger, ctx.channel_id, ctx.thread_ts,
        response["ts"], agent, task, session
    )


async def _run_agent_with_streaming(
    deps, client, logger, channel_id, thread_ts, message_ts, agent, task, session
) -> None:
    """Run an agent with streaming output.

    Parameters
    ----------
    deps : HandlerDependencies
        Handler dependencies.
    client : WebClient
        Slack client.
    logger : Logger
        Logger instance.
    channel_id : str
        Slack channel ID.
    thread_ts : str, optional
        Thread timestamp.
    message_ts : str
        Message timestamp to update.
    agent : AgentConfig
        Agent to run.
    task : str
        Task to execute.
    session : Session
        Database session.
    """
    execution_id = str(uuid.uuid4())

    # Setup streaming
    streaming_state = StreamingMessageState(
        channel_id=channel_id,
        message_ts=message_ts,
        prompt=f"[{agent.name}] {task[:50]}...",
        client=client,
        logger=logger,
        track_tools=True,
        smart_concat=True,
    )
    streaming_state.start_heartbeat()
    on_chunk = create_streaming_callback(streaming_state)

    try:
        # Build prompt with agent's system prompt
        if agent.system_prompt:
            full_prompt = f"{agent.system_prompt}\n\n---\n\nTask:\n{task}"
        else:
            full_prompt = task

        # Resolve model
        model = None
        if agent.model.value != "inherit":
            model = agent.model.value
        elif session.model:
            model = session.model

        # Resolve permission mode
        permission_mode = None
        if agent.permission_mode.value != "inherit":
            permission_mode = agent.permission_mode.value
        elif session.permission_mode:
            permission_mode = session.permission_mode

        result = await deps.executor.execute(
            prompt=full_prompt,
            working_directory=session.working_directory,
            session_id=f"agent-{agent.name}-{channel_id}",
            execution_id=execution_id,
            on_chunk=on_chunk,
            permission_mode=permission_mode,
            model=model,
            db_session_id=session.id,
            channel_id=channel_id,
        )

        await streaming_state.stop_heartbeat()

        # Format final output - convert markdown to Slack mrkdwn (flattens paragraphs)
        output = result.output or result.error or "No output"
        output = markdown_to_mrkdwn(output)
        if len(output) > 2500:
            output = output[:2500] + "\n\n... (truncated)"

        status_emoji = ":heavy_check_mark:" if result.success else ":x:"

        # Build cost/duration context
        context_parts = []
        if result.duration_ms:
            context_parts.append(f"Duration: {result.duration_ms}ms")
        if result.cost_usd:
            context_parts.append(f"Cost: ${result.cost_usd:.4f}")

        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{status_emoji} Agent `{agent.name}` completed\n\n> {task[:100]}",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": output},
            },
        ]

        if context_parts:
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {"type": "mrkdwn", "text": " | ".join(context_parts)}
                    ],
                }
            )

        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=blocks,
        )

    except Exception as e:
        logger.error(f"Agent execution failed: {e}")
        await streaming_state.stop_heartbeat()
        await client.chat_update(
            channel=channel_id,
            ts=message_ts,
            blocks=SlackFormatter.error_message(f"Agent error: {e}"),
        )


async def _handle_create(ctx: CommandContext) -> None:
    """Show agent creation instructions.

    Parameters
    ----------
    ctx : CommandContext
        Command context.
    """
    await ctx.client.chat_postMessage(
        channel=ctx.channel_id,
        blocks=[
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Create a New Agent"},
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Create a custom agent by adding a markdown file:\n\n"
                    "*Project-level:* `.claude/agents/<name>.md`\n"
                    "*User-level:* `~/.claude/agents/<name>.md`\n\n"
                    "Project agents override user agents with the same name.",
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Example format:*\n```\n---\n"
                    "name: my-agent\n"
                    "description: What this agent does (used for auto-selection)\n"
                    "model: sonnet  # or opus, haiku, inherit\n"
                    "tools:\n"
                    "  - Read\n"
                    "  - Grep\n"
                    "  - Glob\n"
                    "disallowedTools:\n"
                    "  - Write\n"
                    "  - Edit\n"
                    "permissionMode: bypassPermissions\n"
                    "maxTurns: 30\n"
                    "---\n\n"
                    "You are a custom agent. Your role is to...\n\n"
                    "(This becomes the system prompt)\n"
                    "```",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "After creating the file, use `/agents` to see it listed",
                    }
                ],
            },
        ],
    )


async def _handle_info(ctx: CommandContext, registry, agent_name: str) -> None:
    """Show agent details.

    Parameters
    ----------
    ctx : CommandContext
        Command context.
    registry : AgentRegistry
        Agent registry.
    agent_name : str
        Name of agent to show.
    """
    agent = registry.get(agent_name)
    if not agent:
        await ctx.client.chat_postMessage(
            channel=ctx.channel_id,
            blocks=SlackFormatter.error_message(f"Agent not found: `{agent_name}`"),
        )
        return

    tools_str = ", ".join(agent.tools) if agent.tools else "all"
    disallowed_str = ", ".join(agent.disallowed_tools) if agent.disallowed_tools else "none"

    prompt_preview = agent.system_prompt[:500]
    if len(agent.system_prompt) > 500:
        prompt_preview += "..."

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Agent: {agent_name}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Source:* {agent.source.value}\n"
                + (f"*File:* `{agent.file_path}`\n" if agent.file_path else "")
                + f"*Description:* {agent.description}\n"
                f"*Model:* {agent.model.value}\n"
                f"*Permission Mode:* {agent.permission_mode.value}\n"
                f"*Max Turns:* {agent.max_turns}\n"
                f"*Tools:* {tools_str}\n"
                f"*Disallowed:* {disallowed_str}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*System Prompt:*\n```{prompt_preview}```",
            },
        },
    ]

    if not agent.is_builtin and agent.file_path:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Edit this agent by modifying: `{agent.file_path}`",
                    }
                ],
            }
        )

    await ctx.client.chat_postMessage(channel=ctx.channel_id, blocks=blocks)
