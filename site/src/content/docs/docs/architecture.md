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

The MCP container has no published ports and no Internet egress. The OpenAI tunnel container is the only component that bridges outward.

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
- structured Agent task RPC

Every optional capability is separately gated.
