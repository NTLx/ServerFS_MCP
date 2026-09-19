# ServerFS Agent Bridge — v0.3 development

This directory contains the **host-side** Agent Bridge planned for ServerFS v0.3.

It is deliberately separate from the released `serverfs-mcp` package and is not wired
into the production Compose stack yet.

Phase A is frozen and provides the provider-neutral infrastructure:

- JSON-lines RPC over a Unix-domain socket
- SQLite task/event/request persistence
- explicit task-state transitions
- durable approval/question records
- per-workdir Agent policy
- cross-process `flock` write leases
- deterministic `FakeAdapter` integration tests

Phase B is currently implementing **Codex only**:

- official managed Codex App Server daemon reuse
- WebSocket-over-UDS App Server transport
- thread/turn start, continuation, steer and interrupt
- normalized Codex events
- command/file/permission approval brokerage
- `requestUserInput` brokerage through the provider-neutral question model
- **native Codex execution semantics**: the Bridge selects the starting workdir but does
  not override the user's Codex sandbox, approval policy, MCPs, skills/plugins, web
  features or shell environment

Claude, MCP Agent tools and production deployment remain out of scope for this phase.

Configuration is fail-closed: security fields use their JSON types exactly, workdir
paths must already be real directories, aliases and slots are validated, and unknown
runtime names are rejected. The development `fake` runtime remains test-only; Codex is
available only when the explicit `codex.enabled` setting and workdir allowlist both permit
it. If both peer credential fields are `null`, the UDS accepts only the bridge
process's own UID/GID; production configuration should set the expected ServerFS
identity explicitly.

## Development

Run from this directory:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The repository-level v0.2 tests must also remain green.

## Local fake-runtime smoke test

Copy and edit the example config. The fake runtime is development-only and must never be
enabled in production:

```bash
cp config.example.json /tmp/serverfs-agent-bridge.json
# Edit host_path to an existing test directory.

uv run serverfs-agent-bridge --config /tmp/serverfs-agent-bridge.json
```

The protocol is newline-delimited JSON over the configured Unix socket. Phase D will add
the ServerFS MCP client side; until then this socket is for tests/development only.

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
The socket parent is created only when missing; existing parents must already be owned by
the bridge user and not be group/world writable. Existing files at the socket path are
never removed. State and lease directories are likewise required to be private (`0700`),
and SQLite database/WAL/SHM files are kept at `0600`.

## Security

- The Bridge is intended to run as a dedicated non-root host user.
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
- Do not add generic shell/argv/environment RPC methods.
- Do not place provider credentials in the existing ServerFS MCP container.

The architecture contract is `../dev_plan_v0.3.md`.
