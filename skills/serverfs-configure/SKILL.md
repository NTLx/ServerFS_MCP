---
name: serverfs-configure
description: Inspect and change an existing Core's workdir policy, limits, binary transfer, Repo Ops or Agent opt-in; preserve unrelated permissions and verify effective configuration.
---

# Configure ServerFS

## When to use

Change a selected Core's workdir policy.

## When NOT to use

Install/migrate, change HTTP credentials, manage Connectors or provision Bridge grants. Use the corresponding Skill.

## Preflight

Identify platform, installed version, Core ID, active config and requested policy. Confirm authority, current dependencies and active work. Stop if uncertain. Do not overwrite live config with a template.

## Safety / stop conditions

Do not widen permissions without authorization, reveal credentials, expose private state, bypass grants/leases, or operate another component. Stop on unsupported mode, config conflict, active unsafe work or failed validation.

## Procedure

1. Inspect relevant existing entries and inherited defaults; retain the original configuration privately.
2. Make the smallest guarded edit. Preserve unrelated read-only, hidden, deny and runtime policy.
3. Keep Repo Ops and Agent separate. Repo Ops needs authorized Bridge IPC, not Agent execution. A Shared Core cannot modify owner grants.
4. Validate the actual deployment selection before applying. Staged native configurations may be pinned; do not edit private manifests to bypass create-only or integrity guards. Coordinate required grant changes with the owner.
5. Apply only the authorized component change. Verify before expanding scope.

## Platform branches

| Platform | Read only the applicable source | Key constraint |
| --- | --- | --- |
| Linux | `.env.example`, `deployment/linux/README.md` | Slot overrides; v0.25.0 signed Bridge binds slot 01; v0.26.0 candidate supports 1–16 explicitly granted Agent/Repo Ops slots. |
| macOS | `serverfs.toml.example`, `deployment/macos/README.md` | Shared Core: one Agent workdir in v0.25.0, 1–16 owner-bound Agent workdirs in the v0.26.0 candidate; no Repo Ops; native staging is create-only. |
| Windows | `serverfs.toml.example`, `deployment/windows/v0250-native-operations.md` | Initial helper is create-only; shared grant policy must match owner. v0.26.0 candidate adds explicit named runtime proxy opt-in; use the matching runbook. |

Native TOML uses shared `[defaults]` for file/binary limits; it does not accept Linux slot overrides.

## Authoritative sources

Paths above are checkout-relative, not packaged files. Locate matching source and installed CLI/help; open relevant sections only. For unresolved policy semantics, read `site/src/content/docs/docs/configuration.md` and the single applicable capability topic. Avoid historical reports unless investigating a specific regression.

## Verification

Check actual effective policy, authenticated MCP/tools and requested operation; include relevant denied cases and unchanged unrelated components. Report evidence, not inferred acceptance.

## Success criteria

Requested policy verified; unrelated configuration and ownership unchanged.

## Failure handling

Stop at the first failed gate, preserve inputs/state, perform only authorized scoped rollback. Report failure, changes, remaining uncertainty and next bounded action.
