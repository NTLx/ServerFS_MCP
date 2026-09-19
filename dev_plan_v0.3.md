# ServerFS MCP v0.3.0 Design Baseline — Agent Bridge

> Status: **design baseline for the next version, not shipped behavior**.
>
> Current released behavior remains v0.2.x as documented in `README.md`.
> This document freezes the intended v0.3 architecture before implementation.

## 1. Goal

ServerFS v0.3 adds an optional **Agent Bridge** capability so a remote MCP client such as ChatGPT can delegate a long-running task to an AI coding agent already available on the Linux server, initially:

- OpenAI Codex CLI / Codex App Server
- Anthropic Claude Code / Claude Agent SDK

The user experience should be:

```text
ChatGPT
  -> submit an agent task against a configured ServerFS workdir
  -> receive a task_id immediately
  -> poll progress later
  -> receive approval requests or questions in the ChatGPT conversation
  -> answer/approve/deny through dedicated MCP tools
  -> retrieve the final agent response
```

Tasks may take minutes or hours. A single MCP tool call MUST NOT remain open for the
whole agent run.

## 2. Non-goals

v0.3 does **not** turn ServerFS into a generic remote shell.

Do not add:

- `execute_command`
- `run_shell`
- arbitrary executable paths
- arbitrary argv from MCP callers
- arbitrary environment injection
- arbitrary working directories outside configured workdirs
- arbitrary host filesystem access
- generic process management
- permanent permission-rule editing
- automatic `bypassPermissions` / `danger-full-access`
- provider-specific Codex or Claude protocol objects in the public MCP schema

The new capability is specifically:

> delegate a task to an administrator-enabled AI-agent runtime under an explicit
> workdir and permission profile.

## 3. Architectural decision

### 3.1 Keep the existing ServerFS container security domain

The current `serverfs-mcp` container remains:

- no published port
- no Internet egress
- read-only container root filesystem
- non-root
- `cap_drop: ALL`
- `no-new-privileges`
- no Docker socket
- no generic shell tool

The MCP container MUST NOT directly spawn `codex` or `claude`.

### 3.2 Add a thin host-side Agent Bridge

Add a separate host-side component:

```text
ChatGPT
   |
OpenAI Secure MCP Tunnel
   |
serverfs-mcp container
   |
   | versioned JSON RPC over local Unix-domain socket
   v
serverfs-agent-bridge
   |                         |
   |                         |
CodexAdapter             ClaudeAdapter
   |                         |
official Codex           official Claude
App Server daemon        Agent SDK / session layer
```

The bridge is a **protocol gateway and permission broker**, not a replacement agent
daemon.

It owns:

- ServerFS task IDs and provider-neutral state
- mapping task IDs to native provider sessions/turns
- normalized events
- pending approval/question records
- workdir/profile authorization
- cross-process write leases
- persistence needed to reconnect/reconcile

It does NOT own:

- Codex's thread history
- Claude's session transcript
- provider model execution
- provider authentication
- provider auto-update lifecycle
- a second implementation of either provider's agent runtime

## 4. Provider strategy

### 4.1 Codex: use the official App Server daemon

Codex integration MUST target the official machine-readable App Server protocol and,
when configured, its official managed daemon.

Preferred topology:

```text
serverfs-agent-bridge
  -> Codex App Server JSON-RPC
  -> codex app-server daemon
```

The adapter must be isolated behind a `CodexAdapter` interface because the official
`codex-app-server-daemon` is currently documented as experimental and its lifecycle
contract may change.

The Bridge MUST NOT expose raw Codex JSON-RPC to MCP callers.

The Bridge SHOULD require the administrator to prepare Codex authentication and daemon
availability. It may optionally use the official `codex app-server daemon start`
lifecycle command when an explicit autostart setting is enabled, but MUST NOT silently
bootstrap/download/install Codex because an MCP task was submitted.

### 4.2 Claude: use the official Claude Agent SDK

Do not reverse-engineer the private implementation behind `claude agents`.

Use the official Python `claude-agent-sdk`, preferably `ClaudeSDKClient` for
interactive/bidirectional runs because it supports:

- persistent conversation context while connected
- streaming messages
- interrupts
- dynamic permission mode
- `can_use_tool`
- session resume through documented session IDs/options

The bridge should use the existing system Claude Code CLI/authentication when explicitly
configured, or the SDK-bundled CLI only when that deployment choice is intentional.

The public ServerFS model must not depend on undocumented Claude daemon socket formats.

## 5. Why ServerFS still needs a bridge process

Reusing the provider daemons does not remove the need for a ServerFS-specific bridge.
Something still has to:

- authenticate/authorize MCP-originated requests against ServerFS workdir policy
- translate one provider-neutral task API into two different provider APIs
- normalize approval requests and user questions
- keep provider-native IDs private
- preserve pending interaction state across individual MCP calls
- enforce a write lease shared with ServerFS mutation tools
- store task metadata/results/events for polling

The bridge must remain thin enough that provider lifecycle is still delegated to the
official runtime.

## 6. Transport and trust boundary

Use a Unix-domain socket only.

Recommended host path:

```text
/run/serverfs-agent-bridge/bridge.sock
```

or an administrator-configured equivalent.

The socket directory is bind-mounted read-only into `serverfs-mcp`, for example:

```text
host /run/serverfs-agent-bridge
  -> container /run/serverfs-agent-bridge
```

No TCP listener and no host-published port are introduced.

The bridge MUST:

- set restrictive socket permissions
- verify the connecting peer UID/GID with Linux peer credentials where available
- accept only the expected ServerFS identity/group
- use a versioned request protocol
- reject unknown methods/fields by default
- impose request-size limits

The UDS is an additional local security boundary; MCP authorization remains mandatory.

## 7. Workdir agent authorization

Agent execution is a separate capability from file mutation.

Existing:

```env
WORKDIR_01_READ_ONLY=true
```

does NOT imply anything about agent execution.

Add per-workdir configuration:

```env
WORKDIR_01_AGENT_MODE=disabled
WORKDIR_01_AGENT_RUNTIMES=
```

Allowed `AGENT_MODE` values:

```text
disabled
review
workspace-write
```

Default MUST be:

```text
disabled
```

so upgrading from v0.2 never grants agent execution.

Example:

```env
WORKDIR_02_ALIAS=ServerFS
WORKDIR_02_READ_ONLY=false
WORKDIR_02_AGENT_MODE=workspace-write
WORKDIR_02_AGENT_RUNTIMES=codex,claude
```

Rules:

- `disabled`: no agent task may be submitted.
- `review`: provider is constrained to a read-only ceiling.
- `workspace-write`: provider may modify this workdir under the interactive approval
  model below.
- `workspace-write` requires `WORKDIR_XX_READ_ONLY=false`; configuration mismatch is
  a startup error.
- runtime names are strict allowlist values; unknown names fail configuration.
- MCP callers cannot request a runtime not allowlisted on that workdir.

No v0.3 public profile provides unrestricted host access.

## 8. Permission ceiling vs provider permission prompts

The ServerFS profile is a **hard ceiling**. Provider-native approval is a second,
narrower layer.

A provider approval can never grant more than the ServerFS profile allows.

Examples:

- `review` + provider asks to write -> Bridge denies without prompting the user.
- `workspace-write` + provider asks for workdir-local write -> may proceed according
  to provider/Bridge policy.
- provider asks for filesystem access outside the configured workdir -> deny.
- provider asks for network access -> may be surfaced to ChatGPT if enabled by policy.
- provider asks for an unrestricted/bypass mode -> deny in v0.3.

ServerFS may restrict a provider more than its native configuration; it must never make
the provider less restrictive than the configured ServerFS ceiling.

## 9. Default permission behavior

Do not use `bypassPermissions`, `danger-full-access`, or equivalent by default.

Recommended initial mapping:

### review

Codex:
- read-only sandbox
- no write escalation beyond profile

Claude:
- `permission_mode="plan"` or a tested equivalent read-only configuration
- writes denied by Bridge policy

### workspace-write

Codex:
- `workspaceWrite` sandbox
- provider approval requests preserved and bridged

Claude:
- start with `permission_mode="default"`
- `can_use_tool` handles ask-path operations
- Bridge may auto-allow narrowly classified workdir-local safe edits
- destructive filesystem changes, shell escalation, network, or ambiguous operations
  remain interactive

Do not use Claude `acceptEdits` blindly as the ServerFS policy implementation:
officially it also auto-approves filesystem commands such as `rm`, `rmdir`, `mv`
and `cp` inside the working directory. The Bridge must keep its own ceiling/classifier.

## 10. Public MCP task model

A ServerFS `task_id` represents **one delegated objective / provider turn**, not an
entire conversation forever.

A task has one terminal result.

If the user wants a follow-up that keeps the same native provider conversation, submit a
new task with `continue_from_task_id`.

This prevents a task from changing from `succeeded` back to `running` and keeps task
state compatible with future MCP Tasks semantics.

Native provider IDs are internal implementation details.

Suggested internal mapping:

```text
ServerFS task_id
  -> runtime
  -> native_session_id / thread_id
  -> native_turn_id (if provider has one)
```

## 11. Task states

Freeze these provider-neutral states:

```text
queued
starting
running
waiting_for_approval
waiting_for_question
succeeded
failed
cancelled
interrupted
```

Terminal:

```text
succeeded
failed
cancelled
interrupted
```

Transitions:

```text
queued -> starting -> running
running -> waiting_for_approval -> running
running -> waiting_for_question -> running
running -> succeeded | failed | cancelled | interrupted
waiting_* -> cancelled
waiting_* -> interrupted
```

Never silently retry a side-effecting task after an ambiguous interruption.

## 12. Provider-neutral task record

Persist at least:

```text
task_id
runtime
workdir_alias
workdir_slot
relative_cwd
profile
status
created_at
started_at
updated_at
completed_at

continue_from_task_id (nullable)

native_session_id (private)
native_turn_id (private)

final_response (nullable)
error_code (nullable)
error_message (nullable)

pending_request_id (nullable)
```

Do not return native host paths or provider-native IDs to MCP callers unless a later
diagnostic design explicitly needs them.

## 13. Public MCP tools

Freeze the initial v0.3 surface to eight new tools.

### 13.1 list_agent_runtimes

Read-only.

Returns provider availability and normalized capabilities, for example:

```json
{
  "runtimes": [
    {
      "name": "codex",
      "available": true,
      "version": "…",
      "capabilities": {
        "persistent_session": true,
        "live_steer": true,
        "interactive_approval": true,
        "interactive_question": true
      }
    }
  ]
}
```

Must not start/install a provider merely to list it.

### 13.2 submit_agent_task

Inputs:

```text
runtime
workdir
path              # relative cwd, default ""
profile            # must not exceed configured workdir mode
prompt
continue_from_task_id?
```

Immediately returns:

```json
{
  "task_id": "agt_…",
  "status": "queued"
}
```

It MUST NOT wait for completion.

If `continue_from_task_id` is supplied:

- prior task must exist
- runtime must match
- workdir must match
- profile cannot become less restrictive without explicit policy
- Bridge reuses/resumes the native provider session but creates a NEW ServerFS task ID

### 13.3 get_agent_task

Returns normalized task state.

If waiting:

```json
{
  "task_id": "agt_…",
  "status": "waiting_for_approval",
  "pending_request": { ... }
}
```

If complete:

```json
{
  "task_id": "agt_…",
  "status": "succeeded",
  "final_response": "…"
}
```

### 13.4 read_agent_task_events

Cursor-paginated normalized events.

Do not return unbounded raw stdout/stderr.

Suggested event types:

```text
task.started
turn.started
agent.message
command.started
command.completed
file_change.started
file_change.completed
approval.requested
approval.resolved
question.requested
question.answered
task.completed
task.failed
task.interrupted
```

Do not persist token-by-token text deltas as individual durable events by default.

### 13.5 respond_agent_approval

Inputs:

```text
task_id
request_id
decision
granted_permission_ids?   # only for permission-set requests
```

Decisions:

```text
approve_once
approve_session
deny
cancel_task
```

Rules:

- only a currently pending approval may be answered
- stale/already-resolved request -> `REQUEST_ALREADY_RESOLVED`
- `approve_session` is session-scoped only
- never modify global Codex/Claude config files
- no permanent approval in v0.3
- no Codex exec-policy amendment exposure in v0.3
- requested permission subsets are bounded by the ServerFS workdir/profile ceiling

### 13.6 answer_agent_question

Inputs:

```text
task_id
request_id
answers[]
```

Normalized answer:

```text
question_id
selected_option_ids[]
text?
```

Support:

- single choice
- multiple choice
- free text
- mixed provider forms where representable

### 13.7 send_agent_message

Only valid while the current task is active.

Purpose: steer an in-flight task, not create a post-completion follow-up.

Examples:

- "Do not modify the database; change only the API layer."
- "Stop investigating the frontend and focus on the failing test."

If the task is terminal, return an error instructing the caller to use
`submit_agent_task(... continue_from_task_id=...)`.

### 13.8 cancel_agent_task

Interrupt/cancel an active task.

Repeated cancellation must have no additional environmental effect.

## 14. Tool annotations

Recommended conservative annotations:

```text
list_agent_runtimes:
  readOnly=true
  openWorld=false

get_agent_task:
  readOnly=true
  openWorld=false

read_agent_task_events:
  readOnly=true
  openWorld=false

submit_agent_task:
  readOnly=false
  destructive=true
  idempotent=false
  openWorld=true

respond_agent_approval:
  readOnly=false
  destructive=true
  idempotent=true only when request_id resolution is CAS-protected
  openWorld=true

answer_agent_question:
  readOnly=false
  destructive=false
  idempotent=true only when duplicate answer is detected/rejected
  openWorld=true

send_agent_message:
  readOnly=false
  destructive=true
  idempotent=false
  openWorld=true

cancel_agent_task:
  readOnly=false
  destructive=true
  idempotent=true
  openWorld=true
```

Annotations are Host hints, never authorization.

## 15. Pending interaction model

ChatGPT is the remote human-in-the-loop UI.

The Agent Bridge converts native provider requests into one of two public request kinds:

```text
approval
question
```

The MCP call that originally submitted the task is already finished.

ChatGPT later learns about the interaction by calling `get_agent_task` or
`read_agent_task_events`.

### Approval request

Normalized shape should include only the fields needed for an informed decision:

```text
request_id
kind
category
title
reason?
relative_cwd?
command_display?
file_changes[]
network_targets[]
requested_permissions[]
available_decisions[]
created_at
```

Categories:

```text
command
file_change
filesystem_permission
network_permission
mcp_tool
other
```

### Question request

```text
request_id
kind="question"
questions[]
created_at
expires_at?
```

Each question:

```text
question_id
prompt
options[]
multi_select
allow_free_text
```

## 16. Host-path confidentiality during approvals

The existing ServerFS contract that host workdir paths are not exposed remains in force.

Provider-native absolute paths must be normalized:

- paths under the selected workdir -> workdir-relative paths
- configured workdir root itself -> "."
- paths outside the selected workdir -> do not disclose the raw path; the operation is
  outside the v0.3 filesystem ceiling and is denied

Codex `cwd`, `grantRoot`, and permission path fields must not leak the host mapping.

Commands may be shown for approval, but the adapter must redact configured workdir root
prefixes from display strings.

## 17. Codex mapping

### Start a new task

```text
thread/start
  cwd = resolved host workdir + relative cwd
  sandbox = profile mapping
  serviceName = "serverfs-agent-bridge"

turn/start
  threadId
  user input = task prompt
```

Persist:

```text
thread.id -> native_session_id
turn.id   -> native_turn_id
```

### Continue a prior task

```text
thread/resume(native_session_id)
turn/start(new prompt)
```

A new ServerFS task ID is returned.

### Progress

Normalize:

- `item/started`
- `item/completed`
- completed agent messages
- command execution
- file change events
- errors
- `turn/completed`

### Steer

```text
send_agent_message
  -> turn/steer
```

### Cancel

```text
cancel_agent_task
  -> turn/interrupt
```

### Command approval

Native:

```text
item/commandExecution/requestApproval
```

Normalize to `approval(category="command")`.

Mapping:

```text
approve_once    -> accept
approve_session -> acceptForSession (only if advertised by availableDecisions)
deny            -> decline
cancel_task     -> cancel + task cancellation semantics
```

Do not expose `acceptWithExecpolicyAmendment` in v0.3.

### File-change approval

Native:

```text
item/fileChange/requestApproval
```

Same normalized decision model.

### Permission approval

Native:

```text
item/permissions/requestApproval
```

The adapter:

1. normalizes requested filesystem/network permissions
2. removes/denies anything outside the ServerFS profile ceiling
3. exposes the remaining requested permission IDs
4. maps `approve_once` to turn scope
5. maps `approve_session` to session scope
6. returns only the granted subset to Codex

### User input

Native:

```text
item/tool/requestUserInput
```

Normalize to `question`.

Track Codex `serverRequest/resolved`. If Codex clears the request before ChatGPT answers,
mark the ServerFS request stale and reject a late response.

### Codex daemon lifecycle

The adapter connects to the official control socket.

v0.3 should support explicit settings such as:

```env
SERVERFS_AGENT_CODEX_ENABLED=true
SERVERFS_AGENT_CODEX_AUTOSTART=false
```

Default autostart SHOULD be false.

When autostart is enabled, use only official lifecycle commands such as
`codex app-server daemon start`; do not implement process supervision or silent
bootstrap/install logic.

## 18. Claude mapping

Use `ClaudeSDKClient`, not a private `claude agents` daemon protocol.

### Start a new task

Create a client with:

- host workdir cwd
- provider permission mode derived from ServerFS profile
- `can_use_tool` callback
- explicit settings sources according to deployment policy

Persist the native Claude session ID once known.

### Continue a prior task

Use the documented session resume mechanism (for example `ClaudeAgentOptions.resume`
with the recorded session ID) and create a new ServerFS task ID.

Do not rely on `continue_conversation=True` as the only persistence mechanism; use the
explicit session ID.

### Progress

Normalize:

- assistant completed messages
- tool use/result
- task progress/notification messages where useful
- result/error messages

Do not mirror the entire raw provider transcript into the ServerFS SQLite database.

### Steer

While a `ClaudeSDKClient` for the active task is connected:

```text
send_agent_message
  -> client.query(...)
```

The implementation must test provider behavior for steering while a response is already
in progress. If the SDK cannot safely accept such input in the tested version, expose
`live_steer=false` from `list_agent_runtimes` and reject rather than emulate badly.

### Cancel

```text
cancel_agent_task
  -> ClaudeSDKClient.interrupt()
```

After interrupt, drain the provider's remaining buffered response before reusing the
client, matching the official SDK contract.

### Permission approval

Use `can_use_tool`.

The callback MUST first apply ServerFS profile policy.

If the request may be shown to the user:

1. create a durable pending approval record
2. transition task to `waiting_for_approval`
3. await an in-process future/event while the bridge remains alive
4. on `respond_agent_approval`, resolve the future with
   `PermissionResultAllow` or `PermissionResultDeny`
5. return task to `running`

Do not also place gated tools in Claude `allowed_tools`, because allow rules can approve
before `can_use_tool` is called.

### AskUserQuestion

When `tool_name == "AskUserQuestion"`:

1. normalize the questions
2. persist `waiting_for_question`
3. await `answer_agent_question`
4. return `PermissionResultAllow(updated_input=...)` containing the original question
   input plus normalized answers

Treat this as a user question, not an approval card.

## 19. Waiting for input and restart semantics

The public pending request is durable; the provider callback/request may not be.

This distinction is critical.

### Normal operation

The Bridge is a long-lived process. A Codex server request or Claude `can_use_tool`
callback may remain pending while ChatGPT is idle. The original MCP request is not held.

### Bridge restart while waiting

Do NOT claim transparent recovery unless proven by provider-specific integration tests.

Conservative v0.3 behavior:

- persist the ServerFS task and pending request before exposing it
- after Bridge restart, reconcile the native provider session
- if the exact native pending request can still be answered safely, restore waiting state
- otherwise mark the task `interrupted`
- never fabricate an approval response or silently re-run a side-effecting provider turn

A user can continue an interrupted provider conversation by submitting a NEW task with
`continue_from_task_id`.

## 20. Runtime recovery capability

`list_agent_runtimes` should publish tested recovery capability, not marketing claims.

Suggested values:

```text
in_flight_recovery:
  native
  session-resume
  none
```

Codex may reach `native` when daemon reconnect/reconciliation is proven.

Claude should initially be treated as `session-resume`: the session can be continued,
but an in-flight SDK subprocess/callback is not assumed to survive Bridge restart.

## 21. Cross-process write lease

v0.2's in-process mutation lock is insufficient once a host agent becomes an official
writer.

v0.3 adds a cross-process per-workdir write lease.

Recommended implementation:

```text
/run/serverfs-agent-locks/<slot>.lock
```

using Linux `flock`.

Rules:

- ServerFS `create/edit/delete/mkdir/rmdir` takes the workdir exclusive lease for the
  short mutation transaction.
- a `workspace-write` Agent task holds the exclusive lease for the entire active turn.
- a second workspace-write task for the same workdir is rejected/queued according to a
  fixed policy; v0.3 should prefer `WORKDIR_BUSY` over hidden queuing.
- read-only ServerFS tools never take the lease.
- review Agent tasks do not hold a long exclusive lease.
- do not put lock files inside user workdirs.

This intentionally serializes ServerFS mutations with a coding agent that may edit many
files over a long turn.

## 22. Bridge persistence

Use SQLite in a private host state directory.

Suggested:

```text
~/.local/state/serverfs-agent-bridge/
  state.sqlite3
  logs/
```

No Redis/PostgreSQL.

SQLite stores:

- task metadata
- provider mapping IDs
- normalized events
- pending requests
- final response/error
- retention timestamps

Provider transcript remains provider-owned.

State directory permissions: 0700.

Database/file creation must use restrictive permissions.

## 23. Retention and limits

Add explicit limits from day one.

Suggested defaults:

```text
SERVERFS_AGENT_MAX_PROMPT_BYTES=65536
SERVERFS_AGENT_MAX_FINAL_RESPONSE_BYTES=262144
SERVERFS_AGENT_MAX_EVENT_BYTES=65536
SERVERFS_AGENT_MAX_EVENTS_PER_TASK=10000
SERVERFS_AGENT_MAX_ACTIVE_TASKS=4
SERVERFS_AGENT_TASK_RETENTION_HOURS=168
SERVERFS_AGENT_MAX_RUN_SECONDS=21600
```

Values are implementation defaults to validate during development, not promises that may
be silently ignored.

If final output exceeds the MCP result limit, return a bounded result plus a retrieval
mechanism rather than a giant tool response.

## 24. Audit logging

ServerFS MCP audit should record:

```text
tool
task_id
runtime
workdir
relative cwd
profile
status transition
request kind
decision
duration
success/error_code
```

Do not log:

- full prompt by default
- full final agent response
- raw provider transcript
- secrets
- provider auth material
- host workdir paths
- raw environment
- sensitive command output

Bridge local diagnostic logging may be more detailed but still must not dump credentials.

## 25. Unified error codes

Add a provider-neutral layer, for example:

```text
AGENT_DISABLED
AGENT_RUNTIME_NOT_ALLOWED
AGENT_RUNTIME_UNAVAILABLE
AGENT_RUNTIME_NOT_READY
AGENT_PROFILE_NOT_ALLOWED
AGENT_TASK_NOT_FOUND
AGENT_TASK_NOT_ACTIVE
AGENT_TASK_TERMINAL
AGENT_TASK_LIMIT_REACHED
AGENT_TASK_TIMED_OUT
AGENT_PROVIDER_ERROR
AGENT_PROVIDER_DISCONNECTED
AGENT_SESSION_NOT_RESUMABLE

WORKDIR_BUSY

REQUEST_NOT_FOUND
REQUEST_ALREADY_RESOLVED
REQUEST_STALE
INVALID_APPROVAL_DECISION
INVALID_QUESTION_ANSWER
PERMISSION_OUTSIDE_POLICY
```

Provider-native errors can be stored internally and summarized, but MCP clients should
not need Codex/Claude-specific error parsing.

## 26. MCP Tasks extension compatibility

The 2026-07-28 MCP Tasks extension is conceptually aligned with this design:

- durable task handle
- polling
- cancellation
- in-progress input requests

However the official Python MCP SDK currently documents the Tasks extension as not yet
implemented.

Therefore v0.3 MUST NOT block on MCP Tasks.

Build the task backend/provider model independently and expose the eight ServerFS tools
above.

When the Python SDK and target OpenAI host support `io.modelcontextprotocol/tasks`
reliably, add an adapter that maps the same task backend to standard MCP Tasks. Do not
rewrite the Agent Bridge backend.

## 27. Bridge protocol

Use a small versioned RPC contract over UDS.

Suggested methods:

```text
runtime.list
task.submit
task.get
task.events
task.approval.respond
task.question.answer
task.message.send
task.cancel
```

Each request carries:

```text
protocol_version
request_id
method
params
```

Each response carries:

```text
request_id
ok
result | error
```

No unsolicited notifications are required for v0.3 because MCP uses polling. The Bridge
internally consumes provider event streams and persists normalized events.

Do not make the UDS protocol a public Internet API.

## 28. Implementation packaging

Keep the existing `serverfs-mcp` Python dependency set minimal.

Do not install Codex/Claude SDK/runtime dependencies into the MCP container just because
the repository also contains the Bridge.

Preferred repository layout:

```text
src/serverfs_mcp/                 # existing MCP package

agent_bridge/
  pyproject.toml                  # separate host-side Python application
  src/serverfs_agent_bridge/
    main.py
    protocol.py
    store.py
    policy.py
    leases.py
    adapters/
      base.py
      codex.py
      claude.py
```

Use `uv` for the Bridge environment.

Claude adapter depends on the official `claude-agent-sdk`.

Codex adapter should use Python stdlib JSON/async I/O against the official App Server
socket rather than importing private Codex implementation packages.

Do not restructure the released v0.2 package into a monorepo workspace until the
implementation proves that the extra complexity is useful.

## 29. Native adapter interface

Freeze a narrow internal interface before provider implementation.

Conceptually:

```text
probe() -> RuntimeInfo
start_task(...)
continue_task(...)
get_state(...)
send_message(...)
cancel(...)
respond_approval(...)
answer_question(...)
reconcile(...)
close()
```

Adapters emit provider-neutral events and pending requests into the Bridge core.

No MCP tool should directly import or call `CodexAdapter` / `ClaudeAdapter`; MCP talks
only to the Bridge RPC client.

## 30. Testing strategy

### Unit

- state machine transition matrix
- workdir/profile authorization
- prompt/result/event size limits
- path redaction
- approval normalization
- question normalization
- request CAS/resolution
- SQLite persistence/recovery
- write lease behavior
- provider error normalization

### Codex integration

Against a real official App Server daemon:

- probe without starting
- new thread + turn
- event stream
- final response
- thread resume
- continue task
- turn steer
- turn interrupt
- command approval
- file-change approval
- permission request
- user-input question
- stale resolved request
- daemon reconnect behavior

### Claude integration

Against a real Claude Agent SDK / authenticated Claude Code environment:

- new session
- final response
- explicit session ID capture
- resume by ID
- interrupt + buffer drain
- `can_use_tool` approval
- AskUserQuestion
- deny
- session-scoped approval behavior if supported safely
- Bridge restart -> conservative interrupted/session-resume behavior

### MCP E2E

Real path:

```text
ChatGPT
 -> OpenAI Secure MCP Tunnel
 -> ServerFS MCP
 -> UDS Bridge
 -> provider
 -> real workdir
```

Must test:

- submit returns immediately
- polling while task runs
- ChatGPT receives an approval request
- approve_once and deny
- ChatGPT receives a question with options/free text
- answer resumes the task
- final response
- cancel
- continue_from_task_id
- concurrent workdir mutation -> WORKDIR_BUSY
- provider unavailable
- workdir agent mode disabled
- review profile cannot write

## 31. Rollout order

Do not implement everything in one patch.

Phase A — Bridge core:
- RPC
- SQLite
- task state machine
- pending request model
- workdir mapping
- leases
- fake adapter tests

Phase B — Codex:
- App Server daemon adapter
- approvals/questions
- resume/steer/cancel
- real integration tests

Phase C — Claude:
- Claude Agent SDK adapter
- permissions/questions
- session resume/cancel
- real integration tests

Phase D — MCP:
- eight tools
- settings/models
- annotations
- audit
- integration with existing ServerFS workdir registry

Phase E — deployment:
- host service
- UDS permissions
- Compose socket/lock mounts
- documentation
- ChatGPT E2E

## 32. Release gate for v0.3

Do not release until all of the following are true:

```text
[ ] v0.2 filesystem behavior remains fully green
[ ] agent execution defaults disabled on every workdir
[ ] no generic shell MCP tool exists
[ ] serverfs-mcp container still has no Internet egress
[ ] MCP container does not contain provider credentials
[ ] Codex uses official App Server protocol
[ ] Claude uses official Agent SDK API
[ ] provider-native IDs stay behind the Bridge
[ ] long task submit returns immediately
[ ] task polling survives separate MCP calls
[ ] approval round-trip works through ChatGPT
[ ] question round-trip works through ChatGPT
[ ] outside-workdir filesystem escalation is denied
[ ] no permanent approval is exposed
[ ] no bypass/unrestricted public profile exists
[ ] cancel is tested
[ ] continuation by prior task is tested
[ ] workdir write lease is tested
[ ] Bridge restart semantics are tested and documented per provider
[ ] real ChatGPT -> Tunnel -> Agent E2E passes
```

## 33. Current known provider limitations

### Codex

The official managed App Server daemon is documented as experimental. Keep all daemon
details behind the adapter and version/protocol probes.

Do not assume a pending server request survives a Bridge connection loss until tested.

### Claude

The official Python Agent SDK is the supported programmable surface. The SDK can resume
sessions by explicit session ID and provides interactive permission callbacks, but the
Bridge must not assume an in-flight subprocess/callback survives Bridge restart.

Use explicit recorded session IDs for continuation rather than relying only on "continue
the most recent conversation" behavior.

## 34. Source references verified for this design

Verified on 2026-09-19:

- Codex App Server:
  https://developers.openai.com/zh-Hans/docs/app-server
- Codex managed App Server daemon:
  https://github.com/openai/codex/blob/main/codex-rs/app-server-daemon/README.md
- Claude Agent SDK Python reference:
  https://code.claude.com/docs/en/agent-sdk/python
- Claude Agent SDK permission model:
  https://code.claude.com/docs/en/agent-sdk/permissions
- Official Claude Agent SDK Python repository:
  https://github.com/anthropics/claude-agent-sdk-python
- MCP Tasks draft:
  https://tasks.extensions.modelcontextprotocol.io/specification/draft/tasks
- MCP Python SDK roadmap:
  https://github.com/modelcontextprotocol/python-sdk/blob/main/ROADMAP.md

## 35. Final v0.3 product boundary

ServerFS v0.3 should be accurately described as:

> A secure remote workdir MCP with optional, administrator-enabled delegation to
> supported local AI-agent runtimes. ServerFS exposes no generic shell; it brokers
> structured long-running tasks, human approvals, questions, cancellation, progress,
> and final responses through a separate local Agent Bridge.

The invariant is:

```text
MCP caller
   -> explicit workdir
   -> explicit agent enablement
   -> explicit provider/profile ceiling
   -> ServerFS Agent Bridge
   -> official provider runtime
   -> human-in-the-loop when native runtime asks
```

No provider feature may bypass the ServerFS workdir/profile ceiling merely because the
native provider supports a broader mode.
