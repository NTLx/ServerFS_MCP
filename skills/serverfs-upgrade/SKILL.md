---
name: serverfs-upgrade
description: Plan and verify a scoped version change or explicit migration of a ServerFS component; distinguish Linux release selection from native create-only installation.
---

# Upgrade ServerFS

## When to use

Authorized version change with known owner, dependency compatibility and recovery plan.

## When NOT to use

Unclear owner, automatic migration of legacy installation, credential rotation or owner upgrade through Shared Core.

## Preflight

Record exact platform, IDs, current/target versions, installed artifact identities, active policy, grants, dependent components, tasks/leases and state schema. Verify trusted version-matched artifacts and whether the target is published. Identify supported upgrade mechanism and actual rollback limits before stopping anything.

## Platform branches

Read only the matching packaged reference: [Linux](references/linux.md), [macOS](references/macos.md), [Windows](references/windows.md). Linux Bridge has explicit release selection; named macOS staging and Windows install/config helpers are create-only, not in-place upgrade/rollback tools.

## Authoritative sources

For the released v0.26.0 layout, resolve repository paths against the [v0.26.0 tagged source](https://github.com/NTLx/ServerFS_MCP/tree/v0.26.0), not the evolving default branch. For other installed versions, resolve the corresponding tagged source; an unpublished candidate is not a stable deployment target.

Reference paths are checkout-relative except links to packaged references. Use the exact current and target release metadata, selected platform runbook and installed CLI help. Stop at a migration plan if compatibility, ownership or supported lifecycle is not established.

## Procedure

1. Preserve existing artifact/config/state and authorized recovery plan privately; do not copy active database state as if it were consistent.
2. Stage and verify target artifacts without replacing current releases. Determine documented compatibility; mixed Core/Bridge versions are not assumed.
3. If a supported scoped version switch exists, quiesce dependent operations, honor stop guards, and switch only the requested component. Otherwise plan explicit migration with fresh identity/state and verified cutover; do not force create-only helpers.
4. Establish fresh authenticated MCP and signed IPC/provider readiness as applicable. Verify denied cases and unaffected component identity/state.
5. Use documented rollback only if state/config remain compatible. Preserve failed attempt evidence.

## Safety / stop conditions

No implicit upgrade of shared owner, unrelated services or default installation. Stop on active unsafe tasks, unproven compatibility, failed stop guard or validation. No secret logs, ACL widening, grant bypass, forced state deletion or unreviewed database downgrade.

## Verification

Record actual source/version, component readiness, effective policy, authenticated operation and relevant negative case. Check external ingress/SSE separately if affected; old session replay is not guaranteed.

## Success criteria

Requested version/capability proven and recovery path retained; unrelated components unaffected. A proposed migration is not a completed upgrade.

## Failure handling

Stop, preserve original and failed artifacts/state, perform only authorized compatible rollback. Report exact gate, current service state, remaining uncertainty and next bounded action.
