# ServerFS Agent Bridge — v0.13.0 stable

This directory contains the **host-side** Agent Bridge included in the current stable ServerFS v0.13.0 release. The provider-neutral execution/approval contract originated in v0.3 and remains compatible. v0.6.0 added the optional Jev advisory suite; v0.7–v0.7.3 added runtime reliability, recovery evidence, immutable manifests, bounded large-result retrieval, retry-safe submission and bounded task/interaction lifetimes; v0.8.0 added Qoder; and v0.9.0 added provider-neutral model discovery, advisory-only Jev model advice and request-scoped model overrides. v0.11 brought native Windows Agent delegation, v0.12 added independent Linux Agent/Jev proxy controls, and v0.13.0 adds native macOS Agent delegation while keeping Codex, Claude and Qoder behind the same provider-neutral contract.

The Bridge remains a separate host process from the `serverfs-mcp` package. Production
Agent delegation is opt-in. On Linux, `compose.agent.yml` wires the MCP container to the host
Bridge while the base `compose.yml` intentionally preserves the 11-tool filesystem-only
surface. Windows and macOS use their native platform lifecycle/integration paths rather than
the Linux Compose/systemd deployment shape.

> The Jev-backed Preflight, Runtime Router, Model Advisor, and Approval Advisor
> remain optional and advisory-only. The v0.7 release line does not turn Jev into a runtime,
> authorization layer or safety authority; it preserves the explicit runtime/workdir/profile
> and provider approval contracts.

Phase A is frozen and provides the provider-neutral infrastructure:

- JSON-lines RPC over a Unix-domain socket
- SQLite task/event/request persistence
- explicit task-state transitions
- durable approval/question records
- per-workdir Agent policy
- cross-process `flock` write leases
- deterministic `FakeAdapter` integration tests

Phase B is frozen and provides **Codex native-mode delegation**:

- official managed Codex App Server daemon reuse
- WebSocket-over-UDS App Server transport
- thread/turn start, continuation, steer and interrupt
- normalized Codex events
- command/file/permission approval brokerage
- `requestUserInput` brokerage through the provider-neutral question model
- native Codex execution semantics: the Bridge selects the starting workdir but does not
  override the user's Codex sandbox, approval policy, MCPs, skills/plugins, web features
  or shell environment

Phase C is frozen and provides **Claude Code native-mode delegation**:

- official Python Claude Agent SDK / `ClaudeSDKClient`
- existing system-installed `claude` executable through `cli_path`
- explicit `user/project/local` setting sources and the Claude Code system-prompt preset
  so SDK execution matches the user's normal Claude Code environment
- explicit native session-ID continuation
- native `can_use_tool` approval brokerage
- `AskUserQuestion` brokerage through the provider-neutral question model
- `interrupt()` cancellation
- live steer disabled until real installed-SDK behavior proves the intended semantics

v0.8.0 added **Qoder native-mode delegation** under the same adapter contract:

- official Python Qoder Agent SDK / `QoderSDKClient`;
- existing system-installed `qodercli` through `cli_path` and the current user's native Qoder login;
- explicit `user/project/local` setting sources;
- native session-ID persistence and continuation through `resume`;
- native `can_use_tool` approval and `AskUserQuestion` brokerage;
- `interrupt()` cancellation;
- v0.9.0 may pass an explicit request-scoped model through `QoderAgentOptions.model`; omission still preserves Qoder's native default and ServerFS never writes a provider/default model configuration;
- live steering is deliberately disabled: the 2026-09-29 installed-SDK/CLI probe showed `priority="now"` ends the first `receive_response()` with `error_during_execution`, while the steered success arrives only from a second response iteration, which does not fit the current one-task/one-terminal-Result Bridge contract;
- restart recovery is declared only as `session-resume`: a persisted Qoder session does not prove that an old in-flight process can be reattached.

Phase D is complete and frozen. It provides the ServerFS MCP client/tool surface and the
shared writer-lease integration. Phase E is also complete and frozen: production
Compose/systemd wiring, runtime permissions and ChatGPT end-to-end deployment were
accepted for v0.3.0; see `../docs/phase-e-acceptance-2026-09-20.md`.

v0.7.0 added a narrow reliability layer over those frozen contracts:

- event envelope schema v1 with optional opaque `correlation_id` propagation;
- immutable manifest JSON plus SHA-256 for every newly submitted task;
- a default 24-hour task deadline, interaction expiry at the same deadline, and seven-day
  terminal retention;
- a persistent per-slot recovery guard layered on the existing writer `flock`, with
  provider-aware restart reconciliation and no blind task rerun;
- inline final responses through 256 KiB, private spool storage above 256 KiB through
  8 MiB, exact SHA-256 metadata, and bounded UTF-8 retrieval through
  `read_agent_task_result`;
- additive SQLite migration: old tasks remain readable but do not receive fabricated
  manifest, deadline or correlation evidence.

v0.7.1 keeps that surface frozen and fixes one Codex recovery edge case. A recovery guard
may be cleared when a persisted task is already failed and recorded evidence proves the
control-socket connection failed before provider execution began. v0.7.2 extends recovery
state hygiene without weakening that proof requirement: when lazy guard reconciliation
independently proves `provider_active=false`, any still non-terminal ServerFS task is first
marked `interrupted` with `AGENT_PROVIDER_INACTIVE`, pending interaction state becomes
stale through the normal terminal transition, and only then is the guard removed. Unknown
provider state remains fail-closed and preserves the guard.

v0.7.3 adds lifecycle reliability for short-lived MCP/ChatGPT callers while preserving asynchronous Agent execution. `task.submit` accepts an optional opaque `idempotency_key` distinct from `correlation_id`. A retained task with the same key and semantic submission fingerprint is returned on retry without a second provider turn, lease, guard or Jev preflight; conflicting reuse fails with `AGENT_IDEMPOTENCY_CONFLICT`. The default task timeout is now 2 hours and each approval/question receives its own 30-minute bound, both administrator-configurable. Interaction expiry interrupts the task with `AGENT_INTERACTION_TIMED_OUT`; late answers are stale. Explicit cancellation, including an approval decision of `cancel_task`, is persisted as terminal before the RPC returns. Provider interrupt is best-effort and internally bounded to 10 seconds; a stalled interrupt cannot indefinitely block Bridge-side cancellation. A live writer lease is still released only after background cleanup, while a persistent recovery guard remains whenever provider stop cannot be proven. Disconnecting or ceasing to poll an MCP connection does not itself cancel an otherwise healthy task.

v0.9.0 adds a narrow model-control layer without turning ServerFS into a model router. `runtime.models` normalizes Codex App Server `model/list` and Qoder Agent SDK `get_available_models()`; Claude returns an explicit `unsupported` discovery result until Claude Code exposes an equivalent stable native-account API. `task.submit` accepts optional `model`; omission sends no ServerFS override, while an explicit value is persisted as `requested_model`, included in the idempotency fingerprint and manifest schema v2, and forwarded to the provider-native start/resume path. The Bridge never silently falls back to another model and does not claim a cross-provider `effective_model`.

The four lifecycle policy values are `task_timeout_seconds`, `interaction_timeout_seconds`, `max_active_tasks`, and `retention_seconds` in the private Bridge JSON config. Production rendering derives them from `SERVERFS_AGENT_TASK_TIMEOUT_SECONDS`, `SERVERFS_AGENT_INTERACTION_TIMEOUT_SECONDS`, `SERVERFS_AGENT_MAX_ACTIVE_TASKS`, and `SERVERFS_AGENT_TASK_RETENTION_HOURS` in the repository-root `.env`.

Agent delegation should remain objective-level and capability-bounded. A submitted task
should carry one authorized objective, the minimum context needed for it, an explicit
mutation boundary/stop condition, and the evidence required for verification. Follow-up
steering should stay within that objective; distinct work belongs in a new task. This is a
least-authority and clarity rule, not an instruction-obfuscation layer: the Bridge must
never encode, disguise, split or rewrite prompts in order to evade provider safety checks.

When the opt-in Jev advisor is configured, task submission still evaluates those properties before
the writer lease is acquired and produces a Runtime Router recommendation among
`direct_serverfs_tool`, `codex`, `claude`, `qoder`, and `human_review`. v0.9.0 also lets pre-submit `runtime.models` advice mode send the concrete task plus normalized currently exposed model candidates to the same Jev client and return `model_advice`; it never applies the recommendation automatically. Successful quality results are
persisted as `task.preflight`; the derived router object is persisted as `task.routing_advice`.
Both are deliberately fail-open: an unavailable Jev evaluation is reported as
`{"status": "unavailable"}` and the authorized task still runs on the explicitly requested
runtime. When a provider actually asks for approval, the same Jev client may issue one
additional approval-specific request and attach its advisory result to the existing pending
approval payload plus an `approval.advice` event. No additional request is made for ordinary
turns or question prompts. None of the advisors can approve/deny permissions, change
runtime/workdir/profile, mutate files, rewrite the prompt, or populate/override the task's model. The current experiment pins
`jev-1.13.0` for reproducible evaluation. The public operator-facing overview is the [Jev Advisors guide](https://ntlx.github.io/ServerFS_MCP/docs/jev-advisors/).

Configuration is fail-closed: security fields use their JSON types exactly, workdir
paths must already be real directories, aliases and slots are validated, and unknown
runtime names are rejected. The development `fake` runtime remains test-only; Codex,
Claude and Qoder are available only when their explicit provider enable setting and workdir
allowlist both permit them. Native provider modes currently require
`agent_mode=workspace-write` so the Bridge holds the writer lease; this does not impose
a provider permission mode. If both peer credential fields are `null`, the UDS accepts
only the bridge
process's own UID/GID; production configuration should set the expected ServerFS
identity explicitly.

## Development

Run from this directory:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The repository-level v0.2 tests must also remain green.

## Two-process MCP E2E harness

The Phase D end-to-end gate lives in `../tests/e2e/` and is run from the repository
root with the **root** environment, which starts this package as a separate host
process:

```bash
uv sync --frozen                        # in agent_bridge/
uv run python tests/e2e/run_e2e.py      # from the repository root
```

It launches the Bridge over a real Unix socket and drives the published MCP surface
against it, covering runtime listing, submit/poll/events, approval and question
round-trips, steering, cancellation and the shared cross-process writer lease. The MCP
public runtime allowlist is `codex`/`claude`/`qoder`, so the harness supplies the
deterministic `FakeAdapter` under the name `codex` on the Bridge side; the production
adapter keeps `name == "fake"` and never enters that allowlist.

This is a harness, not a pytest suite. CI installs the root dependencies only, so the
harness is deliberately excluded from `uv run pytest` (its files are not named
`test_*.py`). Install both environments before running it.

## Local fake-runtime smoke test

Copy and edit the example config. The fake runtime is development-only and must never be
enabled in production:

```bash
cp config.example.json /tmp/serverfs-agent-bridge.json
# Edit host_path to an existing test directory.

uv run serverfs-agent-bridge --config /tmp/serverfs-agent-bridge.json
```

The protocol is newline-delimited JSON over the configured Unix socket. Phase D provides
the thin ServerFS MCP client and ten provider-neutral Agent tools. The accepted Phase E
production deployment exposes the Bridge socket and shared lock directory to the MCP
container through read-only bind mounts defined by `../compose.agent.yml`.

## Phase B Codex live smoke

The normal pytest suite uses a deterministic mock App Server. Before Phase B is frozen,
also run the real managed Codex daemon smoke test on the Linux host where Codex is already
installed and authenticated.

Prepare a development-only Bridge JSON config that:

- sets `codex.enabled=true`;
- points `codex.codex_home` at the existing Codex home;
- allowlists `codex` on a disposable/read-write ServerFS workdir;
- keeps production ServerFS/Tunnel configuration untouched.

Prefer starting the official daemon explicitly:

```bash
codex app-server daemon start
```

Then, from `agent_bridge/`:

```bash
uv sync --frozen
uv run python scripts/codex_live_smoke.py \
  --config /path/to/development-agent-bridge.json \
  --workdir ServerFS
```

The live smoke verifies daemon probing, a new thread, native thread continuation and a
real Codex turn inside a disposable subdirectory, then verifies a file write and removes
its artifacts. Phase B Codex native mode requires a writable/workspace-write workdir so
the Bridge can hold the exclusive workdir lease; it does not pretend to implement a
ServerFS-controlled read-only Codex mode.

`list_agent_runtimes` / `probe()` never autostarts Codex. If
`codex.autostart=true`, the official `codex app-server daemon start` lifecycle command
may be invoked only when an actual task needs Codex and the daemon is unavailable.
The socket parent is created when missing; an existing parent must already be owned by
the bridge user and not group/world writable, and its mode and group are then forced to
the private or shared runtime-asset mode below. Existing files at the socket path are
never removed. State remains private (`0700`) and SQLite database/WAL/SHM files stay
`0600`. Lease/socket runtime assets use private `0700/0600` mode by default; when an
explicit `allowed_peer_gid` is configured for Phase D/E container sharing, the Bridge
switches only those runtime assets to group-readable `0750` directories with `0640`
lock files and a `0660` socket. All 16 lock files are pre-created before the Bridge
serves requests; task execution only ever opens an existing lock file.

## Phase C Claude live smoke

The normal pytest suite must use a deterministic SDK test double, but Phase C is not
complete until the actual server-installed Claude Code CLI passes a disposable live
smoke.

Prepare a development-only Bridge config that:

- sets `claude.enabled=true`;
- sets `claude.claude_bin` to the existing system Claude executable or command name;
- allowlists `claude` on a disposable/read-write workdir;
- leaves the user's native Claude settings and authentication untouched.

Then, from `agent_bridge/`:

```bash
uv sync --frozen
uv run python scripts/claude_live_smoke.py \
  --config /path/to/development-agent-bridge.json \
  --workdir ServerFS \
  --timeout 300
```

The live smoke must prove a new session, explicit session continuation, a real
`AskUserQuestion` round-trip through `waiting_for_question`, a real file write and
cleanup. If the installed Claude/Agent SDK auto-resolves `AskUserQuestion` without
waiting for the Bridge, Phase C is blocked on that provider behavior; do not hide the
failure by changing the user's native permission configuration.

## v0.8 Qoder live smoke

The deterministic suite uses a Qoder SDK test double. The real smoke uses the installed
`qodercli` and the same login/configuration as the server user. Its explicit model argument is
a validation choice; production ServerFS also supports an optional request-scoped model
override and preserves Qoder's native default when that argument is omitted.

Prepare a development-only Bridge config that enables `qoder`, points `qoder.qoder_bin`
at the existing system `qodercli`, and allowlists `qoder` on a writable/workspace-write
workdir. Then run:

```bash
uv sync --frozen
uv run python scripts/qoder_live_smoke.py \
  --config /path/to/development-agent-bridge.json \
  --workdir ServerFS \
  --model Qwen3.8-Flash \
  --timeout 300
```

The smoke script currently refuses any model other than the explicitly approved
`Qwen3.8-Flash` test model. It verifies provider probing, a new native session, explicit
session continuation, a real `AskUserQuestion` round-trip, a real file write and cleanup.
This model pin is validation policy only and is not part of production Bridge config or MCP
schema.

## Security

- The Bridge runs non-root. Phase E production deployment uses the same normal login user whose native Codex/Claude/Qoder environment it delegates to; it does not create a dedicated system account.
- The socket is local-only; there is no TCP listener.
- `workspace-write` must be explicitly enabled per workdir for every production native runtime.
- A Codex task holds the exclusive workdir lease for its active turn.
- The selected ServerFS workdir is the Codex starting `cwd` and lease unit, not a Codex
  sandbox boundary.
- The lease does not serialize native Codex writes outside that selected workdir. Use
  Codex's own configuration when a stronger filesystem/network boundary is required.
- Codex keeps using its existing server-side configuration, authentication, MCP servers,
  skills/plugins, approval policy, sandbox defaults and shell environment policy.
- Codex authentication remains owned by the existing official Codex installation; the Bridge does not copy credentials.
- Optional Codex autostart may invoke only the official idempotent `codex app-server daemon start` lifecycle command.
- Existing Codex MCP servers remain enabled, but Phase B does not yet bridge MCP-originated `mcpServer/elicitation/request`; unsupported server requests fail promptly rather than hanging the turn.
- Claude uses the existing system CLI, authentication, settings, CLAUDE.md files, skills,
  MCP servers and native permission rules. The Bridge explicitly opts into
  `user/project/local` setting sources because the Agent SDK otherwise isolates them.
- Claude `approve_session` may echo only provider-supplied session-scoped permission
  suggestions; the Bridge must not persist user/project/local permission changes.
- Do not add generic shell/argv/environment RPC methods.
- Do not place provider credentials in the existing ServerFS MCP container.

- Qoder uses the existing system CLI/login and official Agent SDK. Production ServerFS may pass an explicit request-scoped model through the provider-native API; omission preserves Qoder's native default, and ServerFS does not write a provider/default model configuration.
- Qoder `approve_session` may echo only provider-supplied permission suggestions; the Bridge does not invent or persist permission rules.

The frozen provider-neutral architecture contract is `../dev_plan_v0.3.md`; the Qoder extension is specified in `../dev_plan_v0.8.md`, with real validation evidence in `../docs/qoder-runtime-validation-2026-09-29.md`.
