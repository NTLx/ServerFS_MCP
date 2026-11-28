---
name: serverfs-install
description: Install a fresh ServerFS Core or explicitly new named instance; select the native or Docker path and verify the minimum requested capability.
---

# Install ServerFS

## When to use

New local stdio or persistent HTTP Core; optional separately authorized attachments.

## When NOT to use

Existing owner, migration, upgrade or uncertain installation. Use diagnose or upgrade first.

## Preflight

Identify platform, architecture, storage, actual version, target topology and authorization. Inventory existing IDs, workdir roots, occupied ports, private state and active work. Choose disjoint writable roots and private state. For persistent HTTP choose exact instance ID, Host admission and caller authentication; Connector credentials are not caller auth.

## Procedure

1. Resolve the applicable version-matched source and trusted artifacts. Verify integrity against trusted release evidence; do not install an unpublished candidate as stable.
2. Select local stdio (native, no persistent registration) or persistent HTTP (named Core). Linux uses Docker Compose for Core, native host Bridge if requested.
3. Prepare new private config with explicitly allowed workdirs, read-only by default. Start with file-only *policy* when that is the requested mode; if owned/shared is requested, choose its mode-specific initial procedure before creating any immutable config.
4. Follow only the selected platform runbook. Verify Core independently, then the authorized Bridge attachment and/or Connector where applicable. Native registration and external ingress require explicit approval.
5. Never expand the installation just to demonstrate optional capabilities. For Repo Ops-only, do not start an Agent provider.

## Platform branches

| Platform | Read this runbook only | Gate |
| --- | --- | --- |
| Linux | `deployment/linux/README.md` | Docker, Compose v2.24+, explicit project/env/overlays; file-only uses bridge-disabled; host Bridge is a user systemd service. |
| macOS | `deployment/macos/README.md` | Native arm64 Apple Silicon, macOS 27.x, APFS; signed SMAppService for persistent jobs. Shared: existing owner, 1–16 owner-bound Agent workdirs; no Shared Repo Ops or named Connector staging. New Owned instances may explicitly select signed v2. |
| Windows | `docs/deployment/windows/v0250-native-operations.md` | Windows 11 x64, NTFS; verified installed wheels, offline dependency cache, current-user Scheduler. Create-only helpers; no WSL, VM or administrator ownership. |

## Authoritative sources

For the released v0.26.0 layout, resolve repository paths against the [v0.26.0 tagged source](https://github.com/NTLx/ServerFS_MCP/tree/v0.26.0), not the evolving default branch. For other installed versions, resolve the corresponding tagged source; an unpublished candidate is not a stable deployment target.

Paths refer to the matching checkout, not files packaged in this Skill. Use the selected runbook section and `.env.example` (Linux) or `serverfs.toml.example` (native). Consult installed CLI help when uncertain. Without matching source or verified ownership, give non-mutating guidance only.

## Safety / stop conditions

Stop on conflicting identities, unsupported version/platform, unsafe active work, absent authorization or failed validation. Never overwrite a default owner, expose credentials/private state, bypass grants/ACLs or clear locks. A Shared Core cannot provision or manage its owner; same UID/SID is not authorization.

## Verification

Check exact version, readiness and effective workdir policy. Verify MCP initialize/tools/list and safe read; for HTTP test caller denial, and for read-only test denied mutation in a disposable approved target. Verify optional attachment/remote MCP independently only if requested; registration is not live readiness. Check unrelated components remain unchanged.

## Success criteria

Requested mode and capabilities verified, no unintended exposure or new component, existing owners unaffected.

## Failure handling

Stop at first failing gate. Preserve partial state and inputs; native create-only attempt markers may prohibit retry. Use only documented authorized recovery, not forced deletion/re-ACL. Report failed check, changes, uncertainty and next bounded action.
