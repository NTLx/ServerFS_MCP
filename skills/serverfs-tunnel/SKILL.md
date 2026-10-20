---
name: serverfs-tunnel
description: Connect ChatGPT through OpenAI Secure MCP Tunnel; manage client bootstrap, credentials, proxy, lifecycle, or layered health failures.
---

# Operate the OpenAI Secure MCP Tunnel

The Tunnel is an optional authenticated ChatGPT ingress adapter. Treat it as a separate subsystem with its own control-plane credentials, lifecycle and health evidence.

## When to use

Use this skill when the user explicitly wants to:

- connect ServerFS to ChatGPT through the OpenAI Secure MCP Tunnel;
- configure Tunnel control-plane credentials or Tunnel-specific proxy routing;
- bootstrap/update the official Tunnel client on native Windows/macOS;
- restart/re-establish the Tunnel after a ServerFS deployment change;
- diagnose Tunnel readiness, control-plane polling or response delivery.

## When NOT to use

Do not use Tunnel as the default path for generic MCP clients; Linux supports direct Streamable HTTP.

Do not use this skill for:

- static ServerFS HTTP API Key or OAuth: `serverfs-auth`;
- generic workdir configuration: `serverfs-configure`;
- Agent provider/Jev proxy routing: `serverfs-agent-bridge`;
- a broad unknown outage before confirming Tunnel is the failing boundary: `serverfs-diagnose`.

Tunnel control-plane credentials are not ServerFS HTTP credentials and must never be reused as API Keys/OAuth secrets.

## Preflight

First locate the target ServerFS checkout or deployed installation and establish its version, active configuration and lifecycle owner. In imported/standalone mode, if no authoritative checkout/deployment source is accessible, stop at bounded guidance; do not mutate or infer target commands/settings.

Paths below are relative to the located **ServerFS checkout root**, never to this skill or the Plugin ZIP. Resolve repository paths only after locating that checkout. For an installed deployment without a checkout, use its version-matched deployment documentation, installed CLI help and active configuration; obtain matching sources before a step that needs missing repository material.

1. Identify Linux Compose vs Windows/macOS native Tunnel deployment.
2. Confirm the user explicitly wants/uses OpenAI Tunnel; do not enable it merely because ChatGPT is involved if another MCP connection is intended.
3. Locate current Tunnel configuration without printing key contents.
4. Determine whether Tunnel is currently managed by the supported platform lifecycle or is a bounded manual diagnostic invocation.
5. Check for duplicate ownership. Multiple active Tunnel clients for the same Tunnel ID are unsupported; do not start a second one until ownership is understood.
6. Read the target release's tested/default Tunnel client information from current docs/CLI rather than hard-coding a version from this skill.

## Authoritative sources

- `site/src/content/docs/docs/getting-started.md`
- `site/src/content/docs/docs/tunnel-diagnostics.md`
- `site/src/content/docs/docs/windows-native.md`
- `site/src/content/docs/docs/macos-native.md`
- `deployment/windows/README.md`
- `deployment/macos/README.md`
- `.env.example`
- current `serverfs tunnel` / `serverfs bootstrap tunnel-client` CLI help and the running Tunnel health API.

## Procedure

1. **Identify ownership and mode.** Determine exactly which managed service/container owns the Tunnel and whether it is expected to be active. Proceed only when the intended owner is proven and no competing client for the same Tunnel ID is active.
2. **Configure only Tunnel settings.** Keep control-plane Tunnel ID/key and Tunnel proxy switch separate from ServerFS HTTP Auth, Agent and Jev settings. Review the non-secret key names/diff and confirm credential-file location is outside workdirs.
3. **Use supported bootstrap/lifecycle paths.** On native platforms, prefer ServerFS bootstrap/service integration; on Linux use the intended Compose Tunnel service. Confirm bootstrap integrity checks and managed status pass for the selected branch.
4. **Restart/re-establish only when needed.** If ServerFS recreation requires reconnecting the Tunnel, use the platform's managed lifecycle rather than starting an extra client manually. Confirm exactly one intended owner after the lifecycle operation.
5. **Validate through layered Tunnel evidence.** Process/container existence is only the first layer; prove readiness/control-plane/MCP/response delivery as needed. Record the highest proven health layer and any unverified layer required by the symptom.
6. **Bound diagnostics.** Use recent redacted logs and health components rather than dumping environment variables or unrestricted logs. Finish with redacted evidence tied to the failed health layer.

## Platform branches

Load only the matching branch after identifying the deployment and requested mode:

- Linux / Docker Compose: read [linux branch](references/linux.md) before platform/mode-specific changes.
- Windows native: read [windows branch](references/windows.md) before platform/mode-specific changes.
- macOS native: read [macos branch](references/macos.md) before platform/mode-specific changes.

## Safety / stop conditions

Stop when:

- Tunnel ID/key source is unknown or would need to be exposed in output;
- another active client appears to own the same Tunnel ID and its identity cannot be proven;
- enabling Tunnel was not explicitly requested;
- a proposed fix requires reusing the Tunnel key as ServerFS API Key/OAuth material;
- the fix would put key files inside a configured workdir;
- the supported current-user service owner would be replaced by root/Admin machinery;
- a non-default Tunnel binary cannot satisfy the current integrity/bootstrap checks;
- generic MCP/ServerFS health is failing independently of Tunnel and must be repaired first.

## Verification

Use an evidence ladder appropriate to the observed failure:

1. managed container/service/process exists;
2. Tunnel liveness/readiness is healthy;
3. at least one successful control-plane poll is observed when required;
4. MCP target observation is healthy/expected;
5. response delivery succeeds when ChatGPT commands are being processed;
6. local queue/dispatcher evidence shows no unexplained backlog when relevant.

For Linux, `/healthz` or container `Up` alone is insufficient. A component endpoint returning HTTP 200 may still report a degraded JSON state; inspect the payload.

For native managed deployments, also prove exactly one intended Tunnel owner and healthy ServerFS service state.

## Success criteria

The Tunnel task is complete when the intended current-user/Compose owner is active, the correct control-plane configuration is in use without credential exposure, health evidence reaches the layer required by the user's symptom, and no second Tunnel client or unrelated Auth/Agent policy was introduced.

## Failure handling

If control-plane polling fails, inspect Tunnel-specific proxy/auth/connectivity evidence; do not change ServerFS filesystem policy. If polling succeeds but ChatGPT receives no result, inspect response-delivery and local dispatcher/queue evidence before restarting anything.

If the Tunnel is healthy but ServerFS MCP is not observed, diagnose the ServerFS target separately. If OAuth-through-Tunnel is involved, consult the current OAuth documentation for known upstream resource behavior rather than weakening OAuth audience/resource validation.
