# ServerFS Maintainer Guide

`/AGENTS.md` is the repository-wide entry point. This directory carries the detailed maintainer contracts that should not make the root instruction file grow without bound.

## Current release target

v0.25.0 is the stable architecture baseline; v0.26.0 is the unreleased development target. Read the [v0.26.0 plan](../../dev_plan_v0.26.0.md) for current scope and release gates. Read [release checklist](../v0.25.0-release-checklist-2026-11-21.md) and [release notes](../releases/v0.25.0.md) for evidence and remaining gates. Core, optional Connector and optional Bridge are independently managed, with explicit per-Core authorization and native lifecycle implementations; macOS owned IPC uses private policy checks rather than shared v2 grants.

## Read order

1. [`AGENTS.md`](../../AGENTS.md) — scope, current baseline and non-negotiable working rules.
2. [`invariants.md`](./invariants.md) — durable filesystem, mutation, protocol, Agent Bridge, platform and security contracts.
3. [`workflow.md`](./workflow.md) — change protocol, validation gates, website rules and release workflow.
4. The implementation, tests and release-facing docs for the subsystem being changed.

## Authority and source hierarchy

When sources appear to conflict, investigate the actual implementation and accepted evidence rather than guessing. For current behavior, prefer:

1. implementation + executable tests + current configuration/schema;
2. current `README.md`, release-facing docs and site content;
3. accepted subsystem/phase evidence, bound to its actual source/artifact SHA and scope;
4. historical `dev_plan_v*.md` records for the version they describe.

A later plan may explicitly extend an older invariant. A plan or green historical test cannot establish current artifact acceptance. Do not erase an older historical statement merely because a later release superseded it.

## Deployment and historical records

Use the [Linux](../../deployment/linux/README.md), [macOS](../../deployment/macos/README.md) and [Windows](../../deployment/windows/v0250-native-operations.md) runbooks for current operations. For operational guidance load only the matching [Skill](../../skills/README.md) and runbook section. Past release landmarks and acceptance evidence are retained in [release notes](../releases/) and [diagnostics](../diagnostics/), not repeated as current behavior.

## Keep this index small

New long-lived implementation rules belong in `invariants.md`. New recurring operational or release procedures belong in `workflow.md`. Version-specific design narratives belong in a versioned plan or acceptance document. Add material to the root `AGENTS.md` only when an agent must see it before deciding what deeper document to open.
