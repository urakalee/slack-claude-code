# Codex app-server JSON-RPC Integration

This project runs Codex through:

```bash
codex app-server --listen stdio://
```

Implementation: `src/codex/subprocess_executor.py`

## Client Requests Sent by This App

1. `initialize`
2. `thread/start` or `thread/resume`
3. `turn/start`
4. `turn/steer` (for new messages while a turn is active in the same scope)
5. `turn/interrupt` (during cancellation flows before process termination)

Additional metadata/lifecycle RPCs (invoked on-demand by commands/status):
- `thread/list`
- `thread/read`
- `thread/archive`
- `thread/unarchive`
- `thread/fork`
- `thread/rollback`
- `thread/compact/start`
- `review/start`
- `model/list`
- `account/read`
- `config/read`
- `configRequirements/read`
- `experimentalFeature/list`
- `mcpServerStatus/list`

Used by Slack command surfaces:
- `/codex-thread` -> thread lifecycle/read APIs
- `/codex-config` -> config/model/account/feature APIs
- `/review status` -> `thread/read` status inspection

`thread/start|resume` parameters sent:
- `cwd`
- `approvalPolicy` (`untrusted`, `on-request`, `never`)
- `sandbox` (`read-only`, `workspace-write`, `danger-full-access`)
- `model` (optional)

`turn/start` parameters sent:
- `threadId`
- `input` (text payload)
- `effort` (optional, parsed from model suffix like `-high`)

## Server Notifications Handled

- `thread/started`
- `turn/started`
- `item/agentMessage/delta`
- `item/plan/delta`
- `item/reasoning/textDelta`
- `item/reasoning/summaryTextDelta`
- `item/reasoning/summaryPartAdded`
- `item/started`
- `item/completed`
- `turn/plan/updated`
- `turn/diff/updated`
- `turn/completed`
- `error`

These notifications are normalized into parser events consumed by
`src/codex/streaming.py`.

Item lifecycle types currently normalized:
- `agentMessage`
- `commandExecution`
- `webSearch`
- `fuzzyFileSearch`
- `fileChange`
- `mcpToolCall`
- `reasoning`

## Server Requests Handled

- `item/tool/requestUserInput`
- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `skill/requestApproval`
- `execCommandApproval`
- `applyPatchApproval`

Unsupported request methods receive JSON-RPC `-32601`.

## Approval Decision Mapping

Bridge helpers: `src/codex/approval_bridge.py`

- `skill/requestApproval` -> `approve|decline`
- `execCommandApproval` / `applyPatchApproval` -> `approved|denied`
- command/file approval methods -> `accept|decline`

If no interactive decision is available, defaults are based on approval mode:
- `never` -> accept/approve
- `on-request` or `untrusted` -> decline

## User Input Mapping

For `item/tool/requestUserInput`, answers are returned as:

```json
{
  "answers": {
    "<question_id>": { "answers": ["..."] }
  }
}
```

Formatting helpers live in `src/question/manager.py`.

## Session Resume Behavior

If `thread/resume` fails with a "thread/session not found" style error, the
executor automatically retries with a fresh `thread/start`.

## Active-Turn Control Model

- Active turns are tracked by session scope (`channel_id` + `thread_ts`).
- If a new message arrives while a scoped Codex turn is active, the app attempts
  `turn/steer` by default.
- If steering is unavailable/fails, the message is queued in the same scope and
  processed after the active turn completes.
- Cancellation paths use `turn/interrupt` first, then terminate subprocesses as
  fallback if needed.

## Troubleshooting Notes

- If `turn/steer` is rejected, Slack will report queue fallback with the queue item id.
- Queue processors are scope-isolated, so channel-level and thread-level queues
  do not overlap.
- Interrupt-first cancellation can report an already-finished turn as successful
  cancellation if the process exits during the interrupt window.
