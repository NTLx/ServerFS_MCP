# ServerFS Maintainer Guide

`/AGENTS.md` is the repository-wide entry point. This directory carries the detailed maintainer contracts that should not make the root instruction file grow without bound.

## Current release

- **v0.13.0 is released and is the current stable release.**
- Native deployment families: Linux, Windows and macOS.
- Native Agent runtimes: Codex, Claude and Qoder, subject to the platform/runtime gates documented for each deployment.
- macOS v0.13 support is intentionally narrow: Apple M-series, native arm64, macOS 27 Golden Gate; no Intel, Rosetta, macOS 26/28 or `darwin-amd64` tunnel asset is claimed.
- `dev_plan_v*.md`, acceptance reports and audits are historical records. Update a stale present-tense status when it becomes false, but do not mechanically rewrite historical evidence or milestone facts.

## Read order

1. [`/AGENTS.md`](../../AGENTS.md) — scope, current baseline and non-negotiable working rules.
2. [`invariants.md`](./invariants.md) — durable filesystem, mutation, protocol, Agent Bridge, platform and security contracts.
3. [`workflow.md`](./workflow.md) — change protocol, validation gates, website rules and release workflow.
4. The implementation, tests and release-facing docs for the subsystem being changed.

## Authority and source hierarchy

When sources appear to conflict, investigate the actual implementation and accepted evidence rather than guessing. For current behavior, prefer:

1. implementation + executable tests + current configuration/schema;
2. current `README.md`, release-facing docs and site content;
3. accepted subsystem/phase evidence;
4. historical `dev_plan_v*.md` records for the version they describe.

A later plan may explicitly extend an older invariant. Do not erase an older historical statement merely because a later release superseded it.

## Release landmarks

These landmarks are navigation aids, not substitutes for their plans:

- **v0.3** — provider-neutral Agent Bridge contract and production Linux deployment baseline.
- **v0.4** — hierarchical workdir policy, binary transfer and Streamable HTTP transport-security fix.
- **v0.5** — isolated ChatGPT/OpenAI file-parameter ingress.
- **v0.6** — optional advisory-only Jev suite.
- **v0.7–v0.7.3** — Agent reliability, evidence, recovery, bounded result spooling and lifecycle/idempotency work.
- **v0.8** — Qoder as the third native Agent runtime.
- **v0.9** — provider-native model discovery, optional Jev model advice and request-scoped model override.
- **v0.10** — native Windows filesystem deployment and binary transfer.
- **v0.11** — native Windows Agent delegation for Codex, Claude and Qoder.
- **v0.12** — independent Linux proxy controls for Tunnel, Agent runtimes and Jev, plus related restricted-network reliability work.
- **v0.13** — native macOS filesystem + Agent deployment, POSIX-generic Codex seam and Darwin acceptance.

## Keep this index small

New long-lived implementation rules belong in `invariants.md`. New recurring operational or release procedures belong in `workflow.md`. Version-specific design narratives belong in a versioned plan or acceptance document. Add material to the root `AGENTS.md` only when an agent must see it before deciding what deeper document to open.
