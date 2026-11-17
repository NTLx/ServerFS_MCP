# v0.25.0 macOS — Shared Bridge v2 Owner Grant and Task Scope Foundations

**Date:** 2026-11-14
**Acceptance:** SUPERSEDED SOURCE-ONLY EXPERIMENT / not a live Shared Bridge acceptance. The experimental Mac owner/task-scope protocol is not wire-compatible with Windows v2; see docs/architecture/v0.25.0-bridge-v2-macos-windows-reconciliation.md.

## Owner Grant registry (not wired to live RPC)

Modules:
- agent_bridge/src/serverfs_agent_bridge/shared_v2_owner_store.py
- agent_bridge/tests/test_shared_v2_owner_store.py

Implemented explicit owner-only SQLite initialization, strict Bridge/Core/Grant identities,
32-byte owner-provisioned private key reads, no raw secret bytes in SQLite, fixed key
filenames and existing private-state ownership/symlink checks. Grant registration and
cross-Core same-Bridge alias collision checks occur in one BEGIN IMMEDIATE transaction.
Revocation uses an expected revision CAS and increments that revision. Concurrent
collision and private-file negative tests are included.

## Opt-in TaskStore ownership sidecar

Files:
- agent_bridge/src/serverfs_agent_bridge/shared_v2_task_scope.py
- agent_bridge/tests/test_shared_v2_task_scope.py
- agent_bridge/tests/test_shared_v2_security_chain.py (synthetic composition)
- agent_bridge/src/serverfs_agent_bridge/store.py (narrow optional integration)

TaskStore.initialize_shared_v2_task_scope() is an explicit optional migration, not
called by normal v1 startup. TaskStore.create_task() accepts optional v2_context=None.
When provided, TaskStore writes the task record and Core/workdir owner scope within
one SQLite transaction, including rollback on any missing schema/context failure.
Legacy v1 rows remain unowned, even after migration, and no owner is exposed
through the pre-existing public TaskRecord.to_dict() API. Workdir mismatch,
unknown task ownership and FK cascade cases have regression tests.

The v2 task sidecar also stores a Core-scoped idempotency key under a UNIQUE
(bridge_id, core_id, idempotency_key) index, with explicit upgrade for older
experimental sidecar schemas. Different Cores may reuse the same key; a
same-Core collision rolls back task creation. The old v1 global idempotency
lookup and unique index remain unchanged and do not see the v2 keys.

## Executed verification

- Owner registry initial scoped suite: 112 passed
- Task scope + legacy store focused suite: 80 passed, 2 skipped
- New concurrent alias collision focused suite: 43 passed, 2 skipped
- Scoped idempotency regression: 89 passed, 2 skipped; full Bridge suite 602 passed / 134 skipped
- Synthetic two-Core manual security-chain suite: 140 passed; full Bridge suite exit 0 (740 collected, skips not separately reported)
- Ruff lint/format on affected files: exit 0
- Full Agent Bridge pytest at each stage: exit 0
- Git diff --check: exit 0

These test subsets overlap and should not be added together. The final
full-suite output contained 740 collected cases but did not separately report
the exact number skipped. All database operations were performed on isolated test databases; the
live Bridge and its database were not modified.

No service registration, Agent task submission, real credentials, live owner
key provisioning, v2 RPC activation, public Tunnel tests or Git commit/push/tag
were performed.

## Unmet integration and safety gates

1. Trusted dispatcher: measured OS peer, Bridge owner key/grant, HMAC,
   persistent nonce acceptance, current method/workdir/task authorization
   and a verified dispatch sequence still need integration.
2. The source-only ClientContext is not a credential. A future v2 caller
   must never construct it from untrusted client input.
3. The Core-scoped v2 idempotency index exists in source, but the live
   BridgeService submit/retry path is not yet wired to its authenticated
   lookup. A global v1 idempotency key still applies to legacy requests.
4. All task reads/events/results/approval/cancellation/continuation methods,
   Repo Ops, diagnostics and runtime metadata need authoritative per-Core
   and per-workdir checks. Legacy tasks remain unowned and inaccessible to v2.
5. Owner Grant admin provisioning, rotation, task-revocation semantics and
   per-workdir write/runtime/action ceilings are not implemented; nor is
   cross-Bridge overlapping writer-root protection.
6. The v2 wire exceeds the v1 1 MiB frame ceiling. Windows/Linux still need
   independent verification of the proposed wire/HMAC contract and semantics.
7. There has been NO live shared-mode Bridge/Core acceptance or public MCP E2E.

**Release conclusion:** validated source-only building blocks, not a working
Shared Bridge v2 deployment. v0.25.0 is not release-ready.
