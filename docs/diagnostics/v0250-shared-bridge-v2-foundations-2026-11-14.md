# v0.25.0 macOS — Shared Bridge v2 Foundations (2026-11-14)

> Historical superseded experiment. The later [native-first ADR](../architecture/v0.25.0-native-platform-boundaries.md) supersedes the original requirement for cross-OS private-wire parity, not the requirement for same-host explicit per-Core authorization and fail-closed isolation. The proposed envelope, nonce/store and continuation semantics below are not the current signed-grant contract; consult [maintainer invariants](../maintainers/invariants.md) and the [release checklist](../v0.25.0-release-checklist-2026-11-21.md). No operational authority or current runtime acceptance follows from these tests.

**Status: SUPERSEDED SOURCE-ONLY EXPERIMENT / NOT OPERATIONAL.** The earlier Mac-only v2 wire is incompatible with the verified Windows core_grants.py contract. See docs/architecture/v0.25.0-bridge-v2-macos-windows-reconciliation.md. No shared Bridge runtime mode was enabled.

## Objective and baseline

Shared Bridge topology B requires two independently configured Cores to authenticate as distinct owner-granted clients of one Agent Bridge. Existing v1 RPC checks a measured local process UID/GID or Windows SID, not a per-Core identity, and is insufficient for this topology.

The implementation is deliberately separate from production, pending review of a common Windows/macOS/Linux protocol. Windows ServerFS was unreachable in this session; no Windows parity claims are made.

## Changed artifacts in the macOS ServerFS checkout

New independent security primitives:
- agent_bridge/src/serverfs_agent_bridge/shared_v2_crypto.py
- agent_bridge/src/serverfs_agent_bridge/shared_v2_replay.py
- agent_bridge/src/serverfs_agent_bridge/shared_v2_grants.py

New deterministic and security-negative tests:
- agent_bridge/tests/test_shared_v2_crypto.py
- agent_bridge/tests/test_shared_v2_replay.py
- agent_bridge/tests/test_shared_v2_grants.py

Design:
- docs/architecture/v0.25.0-shared-bridge-v2-protocol.md

No existing BridgeProtocolServer, TaskStore, production configuration, macOS SMAppService registrations, Agent tasks, or provider environments were modified. No Git commit/push/tag or release operation was performed.

## Implemented, not integrated

1. **Envelope and MAC:** strict version-2 bounded JSON envelope; case-sensitive, canonical unpadded Base64URL; reject unknown/duplicate nested JSON keys, malformed UTF-8, nonfinite values, invalid IDs and time windows; 32-byte HMAC-SHA256 key, 16-byte nonce, domain-separated length-prefixed MAC input, constant-time MAC comparison. Deterministic fixed independent HMAC test vector is in the ADR and test.
2. **Persistent replay ledger:** standalone SQLite schema initializer, atomic (grant_id, nonce) unique insertion under BEGIN IMMEDIATE, durable COMMIT before success, fail-closed database errors/duplicate nonce, bounded expiry pruning, and tests for reopened database and concurrent claims. The helper does not make any Bridge request dispatch decision.
3. **Grant capability check:** trusted owner CoreGrant identity and revision match, validity interval, revocation, exact method/workdir allowlists, and trusted task scope (Core ID plus the task's original workdir alias) for reads/events/cancellation/approvals/messages, including continuation checks. Even same-Core tasks become inaccessible when their workdir is removed from the current grant. Owner-global diagnostics remain denied. It returns an immutable metadata context, not a runtime capability.

## Executed validation

- Crypto-only suite: **43 passed**; Ruff lint/format and git diff checks exit 0 after narrow formatting.
- Crypto + replay suite: **59 passed**; full Bridge suite exit 0; Ruff lint/format and git diff checks exit 0.
- Crypto + replay + grants suite after task-workdir hardening: **90 passed**; full Agent Bridge pytest **557 passed, 134 skipped, exit 0**; targeted Ruff check/format exit 0 and git diff check exit 0.
- A separate Claude read-only security review was cancelled before producing findings. Do not treat it as passed security review.
- No service jobs were registered/started/stopped by these tests; neither real credentials nor networking were used.

## Outstanding mandatory gates

- **Cross-platform protocol reconciliation:** Windows connector was not reachable during this task. The sample HMAC vector and wire contract remain PROPOSED, not frozen multi-platform compatibility.
- **Trusted owner registry and secret provisioning:** verified private key owner/permission handling, grant lifecycle/revocation/rotation and monotonic policy versions are not implemented.
- **Replay integration:** nonce ledger table must be integrated with the Bridge-owner-secured TaskStore connection lifecycle and checked/committed before any RPC dispatch. Existing v1 frame maximum is smaller than the proposed v2 wire maximum.
- **Task ownership migration:** each v2-created task/event/approval/continuation/response must carry its authenticated Core identity; unowned/legacy task records must not be exposed to shared clients. The pure helper's task_owner callback is not connected to a durable owner index.
- **Protocol integration:** v2 client signing and the Bridge-side v2 verified dispatcher must be added only once the owner-policy and task-ownership gates are complete. A shared Bridge must reject all v1 requests, never downgrade.
- **Operational acceptance:** two independent Cores with disjoint rights, one shared Bridge, cross-Core negative tests, nonce replay across restart, revoke/rotate, lifecycle, Windows/macOS/Linux and public MCP end-to-end. None was performed here.

**Release conclusion:** macOS Owned/file-only multi-Connector native lifecycle was previously accepted with synthetic no-network Connectors; Shared Bridge remains unsupported. v0.25.0 is **not release-ready**.
