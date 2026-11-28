# ServerFS Maintainer Guide

`/AGENTS.md` is the repository-wide entry point. This directory carries the detailed maintainer contracts that should not make the root instruction file grow without bound.

## Current release

v0.26.0 is the current release baseline. Read the [release notes](../releases/v0.26.0.md) for scope, the [release checklist](../v0.26.0-release-checklist.md) and [evidence ledger](../releases/v0.26.0-evidence.md) for actual acceptance, and the [versioned plan](../plans/dev_plan_v0.26.0.md) for design history. Core, optional Connector and optional Bridge are independently managed with explicit per-Core authorization and native lifecycle implementations. macOS owned defaults to v1 private IPC/policy checks and supports explicit v2 on new instances; shared uses signed v2 grants.

## Active development

For v0.27.0, the [accepted Platform Independence ADR](../architecture/v0.27.0-platform-independence.md) and [approved development plan](../plans/dev_plan_v0.27.0.md) are mandatory reading before changes. Gate A (behavior-preserving independent platform split) precedes parallel feature development. The approved target deltas supersede conflicting older design permissions only for v0.27.0; current release behavior and security invariants remain binding until their explicit, tested replacement. During migration update root-based commands, paths and executable gates with their implementation. A plan is not evidence of support.

## Read order

1. [`AGENTS.md`](../../AGENTS.md) — scope, current baseline and non-negotiable working rules.
2. [`invariants.md`](./invariants.md) — durable filesystem, mutation, protocol, Agent Bridge, platform and security contracts.
3. [`workflow.md`](./workflow.md) — change protocol, validation gates, website rules and release workflow.
4. The active ADR and versioned development plan above for target-version work.
5. The implementation, tests and release-facing docs for the subsystem being changed.

## Authority and source hierarchy

When sources appear to conflict, investigate the actual implementation and accepted evidence rather than guessing. For current behavior, prefer:

1. implementation + executable tests + current configuration/schema;
2. current `README.md`, release-facing docs and site content;
3. accepted subsystem/phase evidence, bound to its actual source/artifact SHA and scope;
4. historical [versioned plans](../plans/) records for the version they describe.

A later plan may explicitly extend an older invariant. A plan or green historical test cannot establish current artifact acceptance. Do not erase an older historical statement merely because a later release superseded it.

## Deployment and historical records

Use the [Linux](../../deployment/linux/README.md), [macOS](../../deployment/macos/README.md) and [Windows](../deployment/windows/v0250-native-operations.md) runbooks for current operations. For operational guidance load only the matching [Skill](../../skills/README.md) and runbook section. Past release landmarks and acceptance evidence are retained in [release notes](../releases/) and [diagnostics](../diagnostics/), not repeated as current behavior.

## Keep this index small

New long-lived implementation rules belong in `invariants.md`. New recurring operational or release procedures belong in `workflow.md`. Version-specific design narratives belong in `docs/plans/`; acceptance and diagnostics belong in `docs/diagnostics/` or the applicable release ledger. Add material to the root `AGENTS.md` only when an agent must see it before deciding what deeper document to open.
