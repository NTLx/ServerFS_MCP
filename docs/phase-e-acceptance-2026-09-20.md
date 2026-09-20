# ServerFS MCP v0.3.0 Phase E Acceptance

Date: 2026-09-20

This records the completed v0.3.0 acceptance of the optional, user-scoped Agent
Bridge deployment. Phases A–D froze the provider-neutral core, Codex adapter,
Claude adapter, and eight-tool Agent MCP surface. Phase E froze the production
deployment layer: the opt-in Compose overlay, user systemd lifecycle, measured
SO_PEERCRED identity, runtime permissions, configuration rendering, provider
environment handling, container-to-Bridge verification, and release/rollback
documentation.

## Acceptance results

- Host Gate: **PASS** — user-scoped install/render/lifecycle, permissions,
  provider discovery and Bridge RPC verification.
- Container Gate: **PASS** — explicit overlay, peer identity, read-only runtime
  mounts, container-to-Bridge RPC, credential isolation, and Internet isolation.
- ChatGPT E2E: **PASS** — real Tunnel → MCP → Bridge path with 19 tools.
- Codex E2E: **PASS** — native task, cancellation, live steer, and
  persistent-session continuation.
- Claude E2E: **PASS** — native task, `approve_once`, and `AskUserQuestion`.
- `WORKDIR_BUSY`: **PASS** — during Claude's `waiting_for_question` state, a
  normal ServerFS mutation against the same workdir was rejected; after the
  task ended, lease release was confirmed.
- Credential isolation: **PASS** — provider credentials remain host-side.
- Internet isolation: **PASS** — the MCP container has no Internet egress.

The accepted Agent surface is the eight provider-neutral tools documented in the
root README. It is not a shell, argv, or generic command executor. The existing
base Compose deployment remains the 11-tool filesystem surface.

## Rollback disposition

The live base 11-tool rollback → re-cutover drill is **WAIVED BY MAINTAINER on
2026-09-20**. This waiver covers only additional live disruption during release
closeout and is not a failure or blocker. Rollback implementation and recovery
tests remain covered and the emergency rollback documentation remains in place.

The production deployment was not redeployed during release closeout.

## Final closeout revalidation — 2026-09-21

A non-disruptive final revalidation confirmed that the accepted v0.3.0 state remains
intact. No production restart, recreate, daemon update or live rollback/re-cutover was
performed.

- Repository gate: **PASS** — `673 passed`, Ruff check/format clean, scratch-tag Docker
  build and Compose validation succeeded.
- Agent Bridge gate: **PASS** — `83 passed`, Ruff check/format clean.
- Two-process MCP E2E harness: **PASS** — `46/46` checks.
- Live deployment: **PASS** — one healthy `serverfs-mcp` instance created from
  `compose.yml` + `compose.agent.yml`; the sole Tunnel targets that instance and reports
  ready.
- Live ChatGPT surface: **PASS** — 19 tools are exposed: 11 filesystem tools plus eight
  provider-neutral Agent tools.
- Live HITL/lease behavior: **PASS** — Claude `AskUserQuestion`, `approve_once`,
  `WORKDIR_BUSY` while the Agent writer lease is held, and lease release after terminal
  task state were all reverified.
- Live Codex behavior: **PASS** — native session continuation, live steer and cancellation
  were reverified through the production MCP/Bridge path.
- Provider parity: **PASS** — Codex direct CLI, managed daemon and app-server all report
  `0.155.1`; Claude Code reports `2.1.278`.
- Credential isolation: **PASS** — provider credentials remain host-side and are not
  mounted or injected into the MCP container.
- Internet isolation: **PASS** — in addition to the internal-only Docker topology, an
  active container probe could not resolve a public hostname and TCP connects to
  `1.1.1.1:443` and `8.8.8.8:443` both failed with `Network unreachable`.
- User lifecycle: `loginctl` reports linger enabled. The delegated non-interactive shell
  used for this revalidation could not attach to the user systemd bus; the live Bridge,
  socket/RPC checks and previously accepted lifecycle/recovery tests remained healthy, so
  this observation does not reopen the Phase E lifecycle gate.

The v0.3.0 implementation, deployment and release gate therefore remain **COMPLETE/FROZEN**.
