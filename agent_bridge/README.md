# ServerFS Agent Bridge — v0.7.1

This directory contains the **host-side** Agent Bridge shipped with ServerFS v0.7.1. The
provider-neutral execution/approval contract originated in v0.3 and remains compatible;
v0.6.0 added the optional Jev advisory suite, v0.7.0 added runtime reliability,
recovery evidence, immutable execution manifests and bounded large-result retrieval, and
v0.7.1 adds a targeted Codex reconciliation hotfix without changing that public surface.

The Bridge remains a separate host process from the `serverfs-mcp` package. Production
Agent delegation is opt-in: `compose.agent.yml` wires the MCP container to the host Bridge,
while the base `compose.yml` intentionally preserves the 11-tool filesystem-only surface.

> The Jev-backed Preflight, Runtime Router, and Approval Advisor introduced in v0.6.0
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
may be cleared only when the persisted task is already failed, the recorded control-socket
connection failure occurred before provider execution began, and both native session and
turn IDs are absent. Other no-ID failures remain ambiguous and continue to fail closed.

Agent delegation should remain objective-level and capability-bounded. A submitted task
should carry one authorized objective, the minimum context needed for it, an explicit
mutation boundary/stop condition, and the evidence required for verification. Follow-up
steering should stay within that objective; distinct work belongs in a new task. This is a
least-authority and clarity rule, not an instruction-obfuscation layer: the Bridge must
never encode, disguise, split or rewrite prompts in order to evade provider safety checks.

When the opt-in Jev advisor is configured on `main`, one advisory call evaluates those properties before
the writer lease is acquired and also produces a Runtime Router recommendation among
`direct_serverfs_tool`, `codex`, `claude`, and `human_review`. Successful quality results are
persisted as `task.preflight`; the derived router object is persisted as `task.routing_advice`.
Both are deliberately fail-open: an unavailable Jev evaluation is reported as
`{"status": "unavailable"}` and the authorized task still runs on the explicitly requested
runtime. When a provider actually asks for approval, the same Jev client may issue one
additional approval-specific request and attach its advisory result to the existing pending
approval payload plus an `approval.advice` event. No additional request is made for ordinary
turns or question prompts. None of the advisors can approve/deny permissions, change
runtime/workdir/profile, mutate files, or rewrite the prompt. The current experiment pins
`jev-1.13.0` for reproducible evaluation. The public operator-facing overview is the [Jev Advisors guide](https://ntlx.github.io/ServerFS_MCP/docs/jev-advisors/).

Configuration is fail-closed: security fields use their JSON types exactly, workdir
paths must already be real directories, aliases and slots are validated, and unknown
runtime names are rejected. The development `fake` runtime remains test-only; Codex and
Claude are available only when their explicit provider enable setting and workdir
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
public runtime allowlist is only `codex`/`claude`, so the harness supplies the
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
the thin ServerFS MCP client and nine provider-neutral Agent tools. The accepted Phase E
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

## Security

- The Bridge runs non-root. Phase E production deployment uses the same normal login user whose native Codex/Claude environment it delegates to; it does not create a dedicated system account.
- The socket is local-only; there is no TCP listener.
- `workspace-write` must be explicitly enabled per workdir for Codex native mode.
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

The architecture contract is `../dev_plan_v0.3.md`.
