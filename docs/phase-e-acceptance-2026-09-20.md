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
