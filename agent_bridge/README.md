# ServerFS Agent Bridge

The optional host-side Bridge owns Agent execution, task state, writer coordination and deterministic Repo Ops. Core and Connector lifecycles are separate. Choose `disabled` for file-only service, `owned` for a dedicated Bridge, or `shared` for attachment to an explicitly authorized owner. Named shared attachments have per-Core grants and method/alias scope; macOS owned uses private native IPC and manifest/policy checks instead. Shared Cores cannot manage the owner. Keep owner state and credentials private.

## Deployment

- [Linux named user systemd Bridge](../deployment/linux/README.md)
- [macOS native SMAppService](../deployment/macos/README.md)
- [Windows native Task Scheduler and Named Pipe](../deployment/windows/v0250-native-operations.md)

Repo Ops and Agent delegation are independently gated per workdir. Repo Ops never submits a provider task. Native Codex, Claude and Qoder plus fixed operator-configured ACP profiles are supported as documented; ACP needs workspace-write and must not infer unsupported capabilities. Provider authorization is provider-owned. Jev is advisory only. See the [Agent Bridge guide](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/) for task lifecycle, model discovery, recovery, diagnostics and limits.

## Development

Run from this directory:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The repository-level tests must also remain green.

## Two-process MCP E2E harness

The two-process end-to-end gate lives in `../tests/e2e/` and is run from the repository
root with the **root** environment, which starts this package as a separate host
process:

```bash
uv sync --frozen                        # in agent_bridge/
uv run python tests/e2e/run_e2e.py      # from the repository root
```

It launches the Bridge over a real Unix socket and drives the published MCP surface
against it, covering runtime listing, submit/poll/events, approval and question
round-trips, steering, cancellation and the shared cross-process writer lease. The harness uses the public runtime ID `codex`, so it supplies the
deterministic `FakeAdapter` under the name `codex` on the Bridge side; the production
adapter keeps `name == "fake"`; it is not a public production runtime.

This is a standalone harness, not a pytest suite: its files are not named
`test_*.py`, and normal `uv run pytest` does not run it. Install both environments
before running the explicit harness command.

## Local fake-runtime smoke test

The example JSON is a POSIX-only, standalone protocol-v1 fixture with `fake` enabled. It is not a production configuration, a signed attachment, or a Windows Named Pipe example. Run it only as the current unprivileged user in a fresh private scratch directory, without exposing the socket or any real workdir:

```bash
fixture=$(mktemp -d)
mkdir "$fixture/workdir"
cp config.example.json "$fixture/config.json"
```

Edit the copied JSON: point `socket_path`, `state_dir`, `lock_dir` and `workdirs[0].host_path` to distinct locations under `$fixture` (the workdir already exists); keep real providers disabled. Then run `uv run serverfs-agent-bridge --config "$fixture/config.json"` in the foreground. Stop only this test process before removing its scratch directory. Null peer UID/GID defaults to the current POSIX UID/GID; it does not establish a named Core grant. Use the platform runbooks for production deployment and authorization.

## Historical evidence

Previous provider smoke recipes, phase records and exact acceptance statements remain available in the [integration baseline](https://github.com/NTLx/ServerFS_MCP/blob/fffc0adb8341edb30ac7b03e72a017ca95b8c83d/agent_bridge/README.md) and repository diagnostics. Re-run applicable gates against the actual owner/artifacts before claiming current acceptance.
