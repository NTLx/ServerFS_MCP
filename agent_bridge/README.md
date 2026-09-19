# ServerFS Agent Bridge — Phase A

This directory contains the **host-side** Agent Bridge planned for ServerFS v0.3.

It is deliberately separate from the released `serverfs-mcp` package and is not wired
into the production Compose stack yet.

Phase A implements only provider-neutral infrastructure:

- JSON-lines RPC over a Unix-domain socket
- SQLite task/event/request persistence
- explicit task-state transitions
- durable approval/question records
- per-workdir Agent policy
- cross-process `flock` write leases
- deterministic `FakeAdapter` integration tests

It does **not** currently invoke Codex or Claude.

Configuration is fail-closed: security fields use their JSON types exactly, workdir
paths must already be real directories, aliases and slots are validated, and unknown
runtime names are rejected. The Phase A package implements only the development `fake`
runtime. If both peer credential fields are `null`, the UDS accepts only the bridge
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
The socket parent is created only when missing; existing parents must already be owned by
the bridge user and not be group/world writable. Existing files at the socket path are
never removed. State and lease directories are likewise required to be private (`0700`),
and SQLite database/WAL/SHM files are kept at `0600`.

## Security

- The Bridge is intended to run as a dedicated non-root host user.
- The socket is local-only; there is no TCP listener.
- `workspace-write` must be explicitly enabled per workdir.
- A workspace-write task holds an exclusive workdir lease for its active turn.
- Provider authentication and provider runtime lifecycle are not implemented in Phase A.
- Do not add generic shell/argv/environment RPC methods.
- Do not place provider credentials in the existing ServerFS MCP container.

The architecture contract is `../dev_plan_v0.3.md`.
