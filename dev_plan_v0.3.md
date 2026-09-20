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
- `review` remains a provider-neutral Phase A profile for adapters that can honestly
  enforce read-only execution.
- `workspace-write` means the delegated agent may mutate files, so the Bridge holds the
  cross-process workdir lease for the active turn.
- `workspace-write` requires `WORKDIR_XX_READ_ONLY=false`; configuration mismatch is
  a startup error.
- runtime names are strict allowlist values; unknown names fail configuration.
- MCP callers cannot request a runtime not allowlisted on that workdir.
- **Codex Phase B native mode accepts only `workspace-write`**. This is a scheduling
  and lease declaration, not a Codex sandbox override.

## 8. Provider-native execution policy

For Codex Phase B, ServerFS deliberately does **not** redefine the user's Codex execution
environment.

The selected ServerFS workdir determines:

- which administrator-approved workdir may be used to start a Codex task;
- the initial `cwd` passed to Codex;
- which workdir lease ServerFS holds while that Codex turn is active.

It is **not** a Codex filesystem/network sandbox boundary.

ServerFS does not override, disable or synthesize Codex:

- sandbox mode;
- approval policy;
- MCP servers;
- skills/plugins/apps;
- web/network features;
- shell environment policy;
- user/project Codex configuration.

Codex therefore behaves as closely as practical to the same server user's direct Codex
usage. Its existing configuration, project trust, authentication and provider-level
permissions remain authoritative.

If Codex itself emits an approval or user-input request, the Bridge relays that native
interaction to ChatGPT and returns the user's decision. ServerFS does not silently deny
the request merely because it refers to network access or a path outside the selected
workdir.

This is an intentional product boundary:

> ServerFS controls **whether and where delegation starts**. Codex controls **what its
> configured runtime is allowed to do after delegation starts**.

The workdir lease remains necessary because native Codex may modify files even when
ServerFS itself is not performing a mutation.

## 9. Codex native-mode behavior

Phase B should preserve the familiar server-side Codex environment:

- connect to the user's existing official managed App Server daemon;
- pass the selected `cwd` and user prompt;
- omit per-thread/per-turn sandbox overrides;
- omit per-thread/per-turn approval-policy overrides;
- omit config overrides that disable MCPs, skills, plugins, web search or shell features;
- do not rewrite Codex config files;
- do not copy provider credentials into ServerFS;
- preserve Codex-native approval/question behavior through the Bridge.

`codex.autostart` defaults to false. This is preferred for native-mode predictability
because the already-running official daemon uses the environment inherited when that
daemon was started. If an administrator explicitly enables autostart, the Bridge may call
only the official idempotent daemon start lifecycle command and must document that the
daemon then inherits the Bridge service environment.

Claude Phase C may make a separate provider-specific choice; do not force Codex's native
mode semantics onto Claude before its official SDK behavior is evaluated.

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
- provider adapters may apply provider-specific limits only when that provider's phase explicitly defines them; Codex Phase B native mode does not impose an additional ServerFS permission ceiling

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
- paths outside the selected workdir -> display `<outside-workdir>` rather than the raw
  host path

This redaction is an MCP information-disclosure rule only. It does not prevent native
Codex from requesting or using such a path if Codex's own configuration and the user's
approval allow it.

Codex `cwd`, `grantRoot`, and permission path fields must not leak the host mapping.

Commands may be shown for approval, but the adapter must redact configured workdir root
prefixes from display strings.

## 17. Codex mapping

### Start a new task

```text
thread/start
  cwd = resolved host workdir + relative cwd
  serviceName = "serverfs-agent-bridge"
  # no sandbox / approval / config override

turn/start
  threadId
  cwd
  user input = task prompt
  # no sandbox / approval / config override
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

1. preserves the native Codex permission request as provider data;
2. exposes its top-level permission categories as selectable permission IDs;
3. sends the request to ChatGPT without imposing an additional ServerFS permission
   ceiling;
4. maps `approve_once` to turn scope;
5. maps `approve_session` to session scope;
6. returns either the user's selected permission categories or the full native request
   when the user approves without narrowing the set.

### User input

Native:

```text
item/tool/requestUserInput
```

Normalize to `question`.

Track Codex `serverRequest/resolved`. If Codex clears the request before ChatGPT answers,
mark the ServerFS request stale and reject a late response.

### Codex daemon lifecycle

The adapter connects to the official control socket using WebSocket-over-UDS, matching
Codex's current remote app-server client implementation. On connection it performs the
normal JSON-RPC `initialize` request followed by the `initialized` notification before
issuing thread/turn requests.

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

Use the official Python `ClaudeSDKClient`, not a private `claude agents` daemon
protocol.

Phase C follows the same product principle already frozen for Codex: **preserve the
server user's native Claude Code environment instead of synthesizing a second ServerFS
permission/sandbox policy**.

The Claude Agent SDK intentionally isolates SDK applications from filesystem settings
and the Claude Code system prompt by default. That default is useful for generic SDK
applications, but it is the wrong behavior for ServerFS native mode. The adapter MUST
explicitly restore normal Claude Code context:

- `cli_path` points to the administrator-selected, already-installed system `claude`
  executable;
- `setting_sources=["user", "project", "local"]`;
- `system_prompt={"type": "preset", "preset": "claude_code"}`;
- no ServerFS-supplied `permission_mode`;
- no ServerFS `allowed_tools` / `disallowed_tools`;
- no ServerFS replacement MCP list;
- no ServerFS sandbox/environment override.

Existing Claude authentication, settings, CLAUDE.md files, skills, MCP servers, hooks and
permission rules remain authoritative.

As with Codex native mode, Phase C only exposes Claude on a
`workspace-write` Agent workdir. This is a **writer-lease accounting rule**, not a
request to force Claude into any particular permission mode. Native Claude may modify
files, so ServerFS must hold the selected workdir's exclusive lease for the active task.

### Start a new task

Create `ClaudeSDKClient(ClaudeAgentOptions(...))` with:

- the selected host workdir as `cwd`;
- the existing system Claude CLI via `cli_path`;
- native setting sources and Claude Code system-prompt preset as described above;
- a `can_use_tool` callback for native permission decisions that reach Claude's
  `ask` path.

Do not force a tool into the callback merely to make ServerFS display an approval.
Tools already allowed/denied by the user's native Claude settings should retain that
behavior.

Persist the native Claude session ID from the result.

### Continue a prior task

Use `ClaudeAgentOptions.resume` with the recorded native session ID and create a new
ServerFS task ID.

Do not use "continue the most recent conversation" as the persistence primitive; explicit
session IDs are required.

### Progress

Normalize useful provider events such as:

- assistant text;
- Bash/tool starts;
- file-edit tool starts;
- result/error information.

Do not mirror the entire raw provider transcript into the ServerFS SQLite database.

### Steer

`ClaudeSDKClient` is bidirectional, but Phase C MUST NOT advertise live steer merely
because `client.query(...)` exists. First prove against the installed SDK/CLI that a
query submitted during an in-flight response has the intended steer semantics rather
than queuing a second turn.

Until that proof exists:

```text
live_steer = false
send_agent_message -> explicit provider error
```

### Cancel

```text
cancel_agent_task
  -> ClaudeSDKClient.interrupt()
```

User cancellation remains authoritative in the Bridge even if the provider interrupt
itself fails.

### Permission approval

Use `can_use_tool` only for native Claude permission decisions that reach the
`ask` path.

For a request that reaches the callback:

1. normalize it into a durable ServerFS approval;
2. transition the task to `waiting_for_approval`;
3. await ChatGPT/user resolution;
4. `approve_once` returns `PermissionResultAllow(updated_input=...)`;
5. `deny` returns `PermissionResultDeny`;
6. `cancel_task` denies with `interrupt=True`;
7. expose `approve_session` only if Claude itself supplies a
   `PermissionUpdate(destination="session")` suggestion, and echo only those
   session-scoped suggestions back via `updated_permissions`.

Never turn a Claude suggestion targeting `userSettings`, `projectSettings` or
`localSettings` into a persistent approval. ServerFS must not silently modify the
user's Claude permission configuration.

Do not set `allowed_tools` merely to avoid callbacks: provider rules that already allow
a tool are intentionally outside the remote approval path.

### AskUserQuestion

When `tool_name == "AskUserQuestion"`:

1. normalize the Claude questions into the provider-neutral question model;
2. persist `waiting_for_question`;
3. await `answer_agent_question`;
4. return `PermissionResultAllow(updated_input=...)` containing the original input plus
   Claude's expected `answers` mapping.

Treat this as a user question, not an approval card.

A historical Agent SDK / Claude Code bug has allowed `AskUserQuestion` to resolve with
empty answers before an asynchronous `can_use_tool` callback completes. Therefore
**a real installed-CLI AskUserQuestion round-trip is a mandatory Phase C release gate**.
Mock coverage alone is insufficient.

### Human-wait idle timeout

Waiting for ChatGPT/user input is not provider idleness. If an optional provider event
idle timeout is configured, the adapter must keep the same pending SDK receive operation
alive while a permission/question callback is waiting for user input.

### Recovery

Initial Claude recovery capability is `session-resume`. Do not claim transparent
in-flight recovery of a live SDK subprocess/callback after Bridge restart.

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

For Codex native mode, the lease covers only the selected ServerFS workdir slot. If the
user's native Codex configuration permits the agent to access or modify paths outside
that workdir, those external paths are not serialized by ServerFS. Administrators who
need a stronger filesystem/network boundary should configure that boundary in Codex
itself rather than expecting Agent Bridge to synthesize a second sandbox.

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

Codex adapter should connect to the official managed App Server control socket using the
transport Codex itself currently uses for remote Unix-socket clients: **WebSocket frames
over the Unix-domain socket**, with the WebSocket handshake URI `ws://localhost/rpc` and
JSON-RPC messages inside text frames. Do not treat the daemon control socket as raw JSONL.
The official Python Codex SDK currently launches its own stdio app-server and does not
expose a supported attach-to-existing-daemon transport, so Phase B uses a small
WebSocket-over-UDS client behind `CodexAdapter`. Keep that transport isolated because the
managed daemon remains experimental and may change upstream.

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
- Codex native mode rejects the provider-neutral `review` profile rather than pretending to enforce read-only behavior

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
[ ] ServerFS injects no Codex sandbox/approval/config override in native mode
[ ] Codex-native approval/question requests are faithfully bridged when App Server emits them
[ ] ServerFS exposes no separate bypass/unrestricted Agent profile of its own
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

Phase B intentionally supports the core Codex human-interaction requests needed for the
initial delegation surface: command approval, file-change approval, permission approval
and `item/tool/requestUserInput`. The user's existing Codex MCP servers remain enabled in
native mode, but MCP-originated `mcpServer/elicitation/request` is not yet translated into
ServerFS's provider-neutral interaction model. The Bridge must reject unsupported server
requests promptly rather than leave the Codex turn hanging. Add MCP elicitation later as
a separate interaction-surface extension instead of prematurely generalizing the Phase B
question model to arbitrary MCP form/URL schemas.

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
   -> provider-specific delegation mode
   -> ServerFS Agent Bridge
   -> official provider runtime
   -> human-in-the-loop when native runtime asks
```

ServerFS always controls whether delegation is enabled and which configured workdir is
selected. Execution restrictions after delegation are provider-specific: Codex Phase B
intentionally preserves the server user's native Codex policy, while later providers may
define stricter adapter-level ceilings when their official integration model requires it.
