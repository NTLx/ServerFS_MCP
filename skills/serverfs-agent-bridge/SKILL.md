---
name: serverfs-agent-bridge
description: Provision or verify a host Bridge owner and authorized Core attachments; operate Agent runtimes and tasks, or enable deterministic Repo Ops without delegation.
---

# Operate Agent Bridge

## When to use

An explicitly authorized Bridge owner, attachment, grant, runtime, Repo Ops transport or Agent task operation.

## When NOT to use

File-only Core, HTTP caller auth or Connector lifecycle; provider account changes; replacing deterministic file/Repo Ops calls with Agent execution.

## Preflight

Identify platform/version, exact Core and Bridge IDs, disabled/owned/shared mode, workdir roots, effective runtime/Repo Ops policy, attachment protocol, active tasks and writer guards. Confirm owner authority separately from Core access. File-only requires no Bridge; same UID/SID is not authorization. Independent writable owners require disjoint roots.

## Platform branches

Load only the applicable packaged reference: [Linux](references/linux.md), [macOS](references/macos.md), [Windows](references/windows.md). Named Linux and shared macOS use signed v2 grants; macOS owned defaults to v1 private manifest/policy/IPC checks, with explicit signed v2 available for newly provisioned Owned Cores. Windows named grants use protected Named Pipe/ACL checks. A Shared Core cannot operate its owner.

## Authoritative sources

For the released v0.26.0 layout, resolve repository paths against the [v0.26.0 tagged source](https://github.com/NTLx/ServerFS_MCP/tree/v0.26.0), not the evolving default branch. For other installed versions, resolve the corresponding tagged source; an unpublished candidate is not a stable deployment target.

References resolve against the version-matched checkout. Read the selected runbook section and `agent_bridge/README.md`; for specific tasks consult `site/src/content/docs/docs/agent-bridge.md`, `repo-ops.md`, `acp.md` or `jev-advisors.md` in that docs directory. Use installed CLI/help and actual advertised tool schema. No matching source or authority: stop at non-mutating guidance.

## Procedure

1. Choose the least privileged capability: file tools first, Repo Ops for authorized deterministic Git/action/commit/push, Agent only for a genuine delegated objective. Repo Ops-only does not require an Agent provider. Configure matching Core and owner policy through supported provisioning; do not create, change or rotate grants implicitly.
2. Authorize an exact runtime on an explicitly writable workdir with workspace-write. Native provider login remains provider-owned. ACP profiles use operator-installed absolute executable and fixed argv; no registry installation or caller command/env override. Jev is optional advice only; preflight sends original prompt to external TypeSafe API. Enable only with data-transfer authorization.
3. Verify actual attachment and permitted alias/methods, then list workdirs and runtimes. Model discovery: Codex runtime catalog, Qoder account catalog, Claude/ACP unsupported. Only use model override, live steering and interaction tools when supported. Do not infer attachment capability from the runtime alone.
4. Submit one authorized objective with scope, stop conditions and verification evidence. Signed v2 grants reject both `continue_from_task_id` and `idempotency_key`. Omit them; never blindly retry uncertain submission. On compatible v1 paths, retained idempotent submissions and session continuation are conditional on actual policy/runtime. Correlation ID is not deduplication.
5. On supporting signed-v2 attachments, discover retained task IDs via `list_agent_tasks` for an exact authorized Agent alias; use active/terminal/all status and opaque pagination. Rediscovery is not native session continuation. Poll task state, read bounded cursor events and result chunks (up to 64 KiB), respond explicitly to genuine approval/question requests where supported. Public approval labels are redacted, not private resource identities; recognized outside-workdir writes cannot be approved remotely. No automatic approval or Jev-driven routing/model/permission changes. Verify outcome independently, not only terminal status.

## Safety / stop conditions

Stop on ambiguous ownership, unsupported mode, unsafe active work, grant mismatch or failed validation. No raw credentials, provider logs, exposed private state, arbitrary shell/argv, v1 fallback from a signed attachment or bypass of writer leases. Cancellation is persisted before bounded best-effort provider stop; disconnect is not cancellation. Unknown provider stop leaves recovery guard fail-closed. Never delete locks/guards or start another writer before proven cleanup.

## Verification

Check intended owner and attachment readiness, authorized capability and relevant denial (foreign task/grant/alias only where applicable). File MCP may work while Bridge is down. Generic local diagnostics are not signed named attachment proof. Use a disposable task only if authorized; verify provider stop/lease release after completion or cancellation. Confirm unrelated components unchanged.

## Success criteria

Requested attachment/policy/task outcome proven without broader authority or unrelated lifecycle changes.

## Failure handling

Preserve private state and bounded evidence, stop at failed gate; diagnose exact owner/attachment/runtime/lease before repair. Report observed error, known task state, changed/unchanged scope and missing proof. Do not force recovery or restart unrelated owners.
