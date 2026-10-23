---
title: Architecture
description: The ServerFS data path and trust boundaries.
---

## MCP core and ingress paths

```text
MCP host
   │
   ├── stdio (native Windows/macOS)
   ├── Streamable HTTP (Linux; loopback/private by default)
   └── OpenAI Secure MCP Tunnel (optional ChatGPT ingress)
               │
               ▼
          ServerFS MCP Core
               │  scoped policy
               ▼
       explicitly configured workdirs
```

The MCP core and tool contract are host-neutral. Generic Linux HTTP publication is opt-in and binds loopback by default; public exposure requires explicit Host policy plus a TLS/authentication ingress boundary. The OpenAI Tunnel remains an optional authenticated outbound-only adapter with a separate control-plane network. The main MCP container still has no general Internet egress.

## Optional ChatGPT file-ingress path

```text
ChatGPT file parameter
   │
   │ temporary HTTPS URL
   ▼
ServerFS MCP
   │ dedicated internal network only
   ▼
serverfs-file-ingress
   │ policy-checked HTTPS/443 egress only
   ▼
Temporary file host
```

The sidecar is opt-in. It has no workdir mounts, OpenAI/tunnel credentials, or published port. The main MCP container never receives Internet egress; it sends only the temporary URL and byte ceiling to the sidecar. Host authorization is either an exact administrator-configured hostname or the separately enabled constrained OpenAI Azure Blob account family measured from real ChatGPT file parameters. Generic wildcards are rejected. The sidecar then validates globally routable DNS answers, pins the connection to a validated IP while verifying TLS for the original hostname, revalidates every redirect, and enforces timeout and byte ceilings before returning raw bytes.

## Optional static API Key path

```text
MCP client
   │ Authorization: Bearer <key>
   │ or /mcp?key=<key>
   ▼
ServerFS static API Key ASGI boundary
   ▼
Streamable HTTP MCP app
   ▼
existing ServerFS workdir policy
```

The v0.17 API Key path is a small HTTP ingress wrapper around the SDK Streamable HTTP app. It creates no authorization service, database, user identity, scope model or provider egress. API Key and built-in OAuth are mutually exclusive. Query-key compatibility is supported, but the ServerFS runner disables its own default Uvicorn access log in this mode so a secret-bearing URL is not printed by ServerFS itself. See [Static API Key](../api-key/).

## Optional OAuth path

```text
MCP client
   │ OAuth bearer
   ▼
ServerFS MCP Resource Server
   │ private introspection only
   ▼
serverfs-auth
   │
   ├── GitHub
   ├── DingTalk
   ├── Feishu / Lark
   └── WeChat Open Platform
```

`serverfs-auth` is a separately deployed, non-root service with its own writable SQLite state and OAuth-only egress network. Provider credentials never enter `serverfs-mcp`; the MCP container does not join the OAuth egress network. The Resource Server accepts only ServerFS-issued opaque tokens bound to the configured MCP resource. Admission is exact and default-deny, then the existing workdir policy applies unchanged.

OAuth is Linux remote-HTTP deployment state, not part of native Windows/macOS stdio. See [OAuth 2.1](../oauth/) for the public TLS/reverse-proxy shape, provider callbacks, client registration and Tunnel compatibility boundary.

## Optional Agent path

```text
ServerFS MCP container
   │
   │ read-only Unix socket mount
   ▼
Host Agent Bridge
   │
   ├── Codex
   ├── Claude Code
   ├── Qoder
   └── ACP v1 profiles (operator-configured)
```

The Bridge runs as a user-scoped host service and owns its runtime socket, locks, installed releases, configuration, and state outside the repository checkout.

### Optional Jev advisory path

```text
Agent task / model-advice request / provider approval
   │
   ▼
Host Agent Bridge
   │  minimized structured state
   ▼
TypeSafe Jev API
   │  probabilities / confidence / Noul
   ▼
Advisory result only
```

Jev runs from the host Bridge, not the MCP container. Task Preflight and Runtime Router share one task-submission request; Model Advisor can run before submission only after native model discovery; Approval Advisor runs only after a provider creates an approval request. These outputs never become authorization, runtime-selection, or model-selection authority.

## Native persistent lifecycle

Native deployments keep persistence outside the MCP protocol surface and within the current user's OS lifecycle:

```text
Windows v0.14.0+
Task Scheduler 2.0 (InteractiveToken · LeastPrivilege)
   │  logon trigger + 1-minute time-trigger heartbeat
   ▼
windowless watchdog
   │  Job Object containment + bounded restart
   ▼
managed runner
   │  bounded Tunnel-generation restart
   ▼
tunnel-client -> supervisor -> ServerFS / Agent Bridge

macOS v0.13.2
SMAppService + per-user LaunchAgents
   ▼
persistent Tunnel / Agent Bridge
```

The Windows v0.14 lifecycle is deliberately **not** an SCM/LocalSystem service: install, runtime, maintenance and removal stay current-user and non-elevated. A fresh task is registered disabled; `service start` enables it, while `service stop` disables it before ending the current action so the heartbeat cannot undo an intentional stop. `IgnoreNew` plus the native ownership mutex preserve exactly one managed Windows Tunnel lifecycle.

## Capability boundaries

ServerFS exposes narrow operations rather than a generic execution primitive:

- filesystem read tools
- guarded file mutations
- bounded whole-file binary transfer
- optional isolated ChatGPT file ingress
- optional one-secret static API Key boundary for Linux remote HTTP
- optional OAuth 2.1 Resource Server + isolated identity broker for Linux remote HTTP
- structured Agent task RPC, model discovery and request-scoped model override
- optional host-side Jev advisory decisions

In the current **v0.22.0** stable release, the supported MCP surfaces remain 11 / 13 / 21 / 23 tools. Every optional capability is separately gated; v0.12's independent Tunnel/Agent/Jev proxy switches, v0.13's native macOS deployment and v0.14's Windows persistent lifecycle do not change the public tool counts. Jev remains an internal Agent Bridge advisor rather than an MCP capability surface.
