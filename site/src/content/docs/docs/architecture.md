---
title: Architecture
description: The ServerFS data path and trust boundaries.
---

## Base path

```text
Linux filesystem
   │
   ▼
Docker bind mounts
   │  read-only by default
   ▼
ServerFS MCP
   │  internal network only
   ▼
OpenAI Secure MCP Tunnel
   │  outbound-only
   ▼
ChatGPT
```

The MCP container has no published ports and no Internet egress. The OpenAI tunnel has a separate outbound network for control-plane traffic.

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

## Optional Agent path

```text
ServerFS MCP container
   │
   │ read-only Unix socket mount
   ▼
Host Agent Bridge
   │
   ├── Codex
   └── Claude Code
```

The Bridge runs as a user-scoped host service and owns its runtime socket, locks, installed releases, configuration, and state outside the repository checkout.

## Capability boundaries

ServerFS exposes narrow operations rather than a generic execution primitive:

- filesystem read tools
- guarded file mutations
- bounded whole-file binary transfer
- optional isolated ChatGPT file ingress
- structured Agent task RPC

Every optional capability is separately gated.
