<p align="center">
  <img src="assets/repo_logo.png" alt="Slack Claude Code Bot" width="1000">
</p>

<p align="center">
  <a href="https://pypi.org/project/slack-claude-code/"><img src="https://img.shields.io/pypi/v/slack-claude-code" alt="PyPI version"></a>
  <a href="https://pypi.org/project/slack-claude-code/"><img src="https://img.shields.io/pypi/pyversions/slack-claude-code" alt="Python versions"></a>
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License"></a>
  <a href="https://github.com/djkelleher/slack-claude-code/actions/workflows/tests.yml"><img src="https://github.com/djkelleher/slack-claude-code/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
</p>

**Claude Code, but in Slack.** Access Claude Code remotely from any device, or use it full-time for a better UI experience.

## Why Slack?

| Feature | Terminal | Slack |
|---------|----------|-------|
| **Code blocks** | Plain text | Syntax-highlighted with copy button |
| **Long output** | Scrolls off screen | "View Details" modal |
| **Permissions** | Y/n prompts | Approve/Deny buttons |
| **Parallel work** | Multiple terminals | Threads = isolated sessions |
| **File sharing** | `cat` or copy-paste | Drag & drop with preview |
| **Notifications** | Watch the terminal | Alerts when tasks complete |
| **Streaming** | Live terminal output | Watch responses as they generate |
| **Smart context** | Manual file inclusion | Frequently-used files auto-included |

## Installation

### Prerequisites
- Python 3.10+
- [Claude Code CLI](https://github.com/anthropics/claude-code) installed and authenticated

### 1. Install the `ccslack` executable
```bash
pipx install slack-claude-code
```

### 2. Create Slack App
Go to https://api.slack.com/apps → "Create New App" → "From scratch"

**Socket Mode**: Enable and create an app-level token with `connections:write` scope (save the `xapp-` token)

**Bot Token Scopes** (OAuth & Permissions):
- `chat:write`, `commands`, `channels:history`, `app_mentions:read`, `files:read`, `files:write`

**Event Subscriptions**: Enable and add `message.channels`, `app_mention`

**App Icon**: In "Basic Information" → "Display Information", upload `assets/claude_logo.png` from this repo as the app icon

**Slash Commands**: Add the commands from the tables below (or the subset that you plan to use)

If you want worktree workflows, register both `/worktree` and `/wt` (alias) in Slack.

#### Configuration
Customize Claude's behavior for your workflow.

| Command | Description | Example |
|---------|-------------|---------|
| `/model` | Show or change AI model | `/model sonnet` |
| `/mode` | View or set session mode (Claude and Codex) | `/mode`, `/mode plan`, `/mode bypass`, `/mode approval never`, `/mode sandbox workspace-write` |
| `/permissions` | View current permission mode (use `/mode` to change) | `/permissions` |
| `/notifications` | Configure notifications | `/notifications` |

#### Codex Controls
Use these when your session model is a Codex model.

| Command | Description | Example |
|---------|-------------|---------|
| `/usage` | Show Codex session state and modes | `/usage` |

Codex transport uses `codex app-server` JSON-RPC for all modes.
See [Codex app-server JSON-RPC integration notes](CODEX_APP_SERVER_JSON_RPC.md) for
the exact request/notification methods this app handles.

Codex session command support:
- `/review` starts a Codex review when the active session backend is Codex
- `/review status [thread_id|current]` inspects latest review/thread lifecycle status
- `/mcp` shows Codex MCP server status when the active session backend is Codex

`/mode` in Codex sessions:
- `/mode bypass` -> `approval=never`
- `/mode ask`, `/mode default`, and `/mode plan` -> `approval=on-request`
- `/mode plan` also adds a plan-first, no-execution instruction to Codex prompts
- `/mode accept` and `/mode delegate` remain unsupported for Codex
- `/mode approval <untrusted|on-request|never>` sets explicit approval policy
- `/mode sandbox <read-only|workspace-write|danger-full-access>` sets sandbox policy

Supported Codex models:
- `gpt-5.3-codex`
- `gpt-5.3-codex-spark`
- `gpt-5.2-codex`
- `gpt-5.1-codex-max`
- `gpt-5.2`
- `gpt-5.1-codex-mini`

Optional Codex effort suffixes:
- `-low`
- `-medium`
- `-high`
- `-xhigh` (alias: `-extra-high`)

Breaking change:
- Legacy Codex transport flags `CODEX_NATIVE_PLAN_MODE_ENABLED` and `CODEX_USE_DANGEROUS_BYPASS` were removed.

#### Session Management
Each Slack thread maintains an isolated Claude session with its own context.

| Command | Description | Example |
|---------|-------------|---------|
| `/clear` | Reset conversation | `/clear` |
| `/compact` | Compact context | `/compact` |
| `/cost` | Show session cost | `/cost` |
| `/usage` | Show usage/cost (Claude) or session status (Codex) | `/usage` |

#### Navigation
Control the working directory and additional directories for Claude's file operations.

| Command | Description | Example |
|---------|-------------|---------|
| `/ls` | List directory contents | `/ls`, `/ls src/` |
| `/cd` | Change working directory | `/cd /home/user/project`, `cd subfolder`, `cd ..` |
| `/pwd` | Print working directory | `/pwd` |
| `/add-dir` | Add directory to context | `/add-dir /home/user/other-project` |
| `/remove-dir` | Remove directory from context | `/remove-dir /home/user/other-project` |
| `/list-dirs` | List all directories in context | `/list-dirs` |

#### Agents
Configurable subagents for specialized tasks. Matches terminal Claude Code's agent system.

| Command | Description | Example |
|---------|-------------|---------|
| `/agents` | List all available agents | `/agents` |
| `/agents run` | Run a specific agent | `/agents run explore find all API endpoints` |
| `/agents info` | Show agent configuration | `/agents info plan` |
| `/agents create` | Show how to create custom agents | `/agents create` |

**Built-in agents:**
- `explore` - Read-only codebase exploration (fast, uses Haiku)
- `plan` - Create detailed implementation plans
- `bash` - Execute shell commands, git, npm, etc.
- `general` - Full capabilities for implementation

**Custom agents:** Create `.claude/agents/<name>.md` files with YAML frontmatter to define project-specific agents.

#### Command Queue
Queue commands for sequential execution while preserving Claude's session context across items.

| Command | Description | Example |
|---------|-------------|---------|
| `/q` | Add command to queue | `/q analyze the API endpoints` |
| `/qv` | View queue status | `/qv` |
| `/qc` | Clear pending queue | `/qc` |
| `/qr` | Remove specific item | `/qr 5` |

Queue scope follows session scope:
- Channel messages use a channel-level queue
- Thread messages use an isolated queue per thread

#### Jobs & Control
Monitor and control long-running operations with real-time progress updates.

| Command | Description | Example |
|---------|-------------|---------|
| `/st` | Show active job status | `/st` |
| `/cc` | Cancel jobs | `/cc` or `/cc abc123` |
| `/esc` | Send interrupt (Ctrl+C) | `/esc` |

#### Git
Full git workflow without leaving Slack. Includes branch name and commit message validation.

| Command | Description | Example |
|---------|-------------|---------|
| `/status` | Show branch and changes | `/status` |
| `/diff` | Show uncommitted changes | `/diff --staged` |
| `/commit` | Commit staged changes | `/commit fix: resolve race condition` |
| `/branch` | Manage branches | `/branch create feature/auth` |
| `/worktree` | Manage worktrees (`add`, `list`, `switch`, `merge`) | `/worktree add feature/auth` |
| `/wt` | Alias for `/worktree` | `/wt list` |


### 3. Configure

Use the built-in config CLI to securely store your Slack credentials:

```bash
ccslack-config set SLACK_BOT_TOKEN=xoxb-...
ccslack-config set SLACK_APP_TOKEN=xapp-...
ccslack-config set SLACK_SIGNING_SECRET=...
```

**Config CLI Commands:**
| Command | Description |
|---------|-------------|
| `ccslack-config set KEY=VALUE` | Store a configuration value |
| `ccslack-config get KEY` | Retrieve a configuration value |
| `ccslack-config list` | List all stored configuration |
| `ccslack-config delete KEY` | Remove a configuration value |
| `ccslack-config path` | Show config file locations |

Configuration is encrypted and stored in `~/.slack-claude-code/config.enc`. Sensitive values (tokens, secrets) are masked when displayed.

**Alternative:** You can also use environment variables or a `.env` file. Config values take precedence over environment variables.

**Where to find these values:**
- `SLACK_BOT_TOKEN`: Your App → OAuth & Permissions → Bot User OAuth Token
- `SLACK_APP_TOKEN`: Your App → Basic Information → App-Level Tokens → (token you created with `connections:write`)
- `SLACK_SIGNING_SECRET`: Your App → Basic Information → App Credentials → Signing Secret

### 4. Start the Slack bot
You can now run `ccslack` in your terminal. The working directory where you start the executable will be the default working directory for your Claude Code session(s). If you have a .env file in this directory, it will automatically be loaded.   

## Usage

Type messages in any channel where the bot is installed. The main channel is a single Claude Code session. If you click `reply` to any message and start a thread, this will be a new Claude Code session.

## Architecture

```
src/
├── app.py                 # Main entry point
├── config.py              # Configuration
├── database/              # SQLite persistence (models, migrations, repository)
├── claude/                # Claude CLI integration (streaming)
├── handlers/              # Slack command handlers
├── agents/                # Configurable subagent system (explore, plan, bash, general)
├── approval/              # Permission & plan approval handling
├── git/                   # Git operations (status, diff, commit, branch, worktree)
├── hooks/                 # Event hook system
├── question/              # AskUserQuestion tool support
├── tasks/                 # Background task management
└── utils/                 # Formatters, helpers, validators
```

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Configuration errors on startup | Check `.env` has all required tokens |
| Commands not appearing | Verify slash commands in Slack app settings |

## License

MIT

- - - 

Congratulations, you can now use Claude Code from anywhere 🎉💪
