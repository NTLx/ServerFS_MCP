---
name: serverfs-tunnel
description: Add, diagnose or operate one OpenAI or Cloudflare Connector for an identified HTTP Core without changing unrelated components.
---

# Operate a ServerFS Connector

## When to use

A selected OpenAI or Cloudflare ingress Connector needs staging, verification, repair or retirement.

## When NOT to use

Direct HTTP or stdio needs no Connector. Core auth/config changes, Bridge operation and provider migration require their own scope and authorization.

## Preflight

Identify exact Core ID, provider, Connector ID (if applicable), actual version, route, ownership, active sessions and current component state. Verify matching runbook and local authenticated MCP first. Obtain separate authorization for public ingress, provider-side route changes and interruption. Do not reuse another instance's Tunnel resource.

## Platform branches

Load only one applicable packaged reference:

| Platform | OpenAI | Cloudflare |
| --- | --- | --- |
| Linux | [Linux](references/linux.md) | [Cloudflare Linux](references/cloudflare-linux.md) |
| macOS | [macOS](references/macos.md) | [macOS](references/macos.md) |
| Windows | [Windows](references/windows.md) | [Cloudflare Windows](references/cloudflare-windows.md) |

Linux supplied overlays provide one service per provider per project. macOS named shared Core has no Connector staging path. Windows named configuration is create-only.

## Authoritative sources

Reference paths are relative to the matching checkout unless explicitly linked as packaged references. Read the relevant runbook section, actual client help and `site/src/content/docs/docs/tunnel-diagnostics.md` for failures; do not assume historical client flags still apply.

## Procedure

1. Confirm local Core endpoint, exact Host policy and caller admission. If a Core change is necessary, treat it as a separately authorized operation.
2. Prepare verified provider client/artifact and independent private credentials outside workdirs. OpenAI named HTTP requires static Core bearer admission; it injects the upstream header independently of its control-plane key. Cloudflare Tunnel token authenticates the Connector, not callers.
3. Stage and start only the selected Connector using the reference. Preserve all other deployment selections; never create/stop a Core or Bridge as a side effect.
4. Verify provider registration, route and fresh authenticated MCP independently. For OpenAI, distinguish external control-plane admission from local Core bearer denial. For Cloudflare test caller denial through the public route. Test response delivery and long-lived SSE separately when required.
5. Stop/restart or retire only the selected Connector if authorized. No old-session replay guarantee. Provider-side resource deletion is separate from local retirement.

## Safety / stop conditions

Stop on uncertain ownership, conflicting route, unsupported mode, active unsafe work, missing authorization or failed validation. Never log credential files or expanded env, expose health/admin endpoints, disable caller auth, modify unrelated DNS, or force removal of private state.

## Verification

Record exact component ID/version, upstream authority, local MCP acceptance, provider status, external authorized result and relevant denial checks. Confirm unrelated Core/Bridge/Connector identities; registration or a successful poll alone is not delivery proof.

## Success criteria

Intended route works under its actual admission contract, or retirement removes only the intended ingress; untested gates are explicit.

## Failure handling

Identify first failing boundary using bounded evidence. Stop rather than repeatedly restarting or widening admission. Retain private inputs and report changed/unchanged state, missing proof and next bounded action.
