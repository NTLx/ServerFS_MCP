---
title: Agent Bridge
description: Optional structured delegation to native Codex and Claude runtimes.
---

The Agent Bridge is an **optional host-side boundary**. It lets ServerFS expose structured Agent task tools without putting Codex or Claude inside the MCP container.

```text
ChatGPT
  │
  ▼
ServerFS MCP
  │  structured Agent RPC
  ▼
Unix socket
  │
  ▼
Host Agent Bridge
  ├── Codex
  └── Claude Code
```

## Why it is separate

The base `compose.yml` remains Agent-unaware. Agent deployments explicitly add `compose.agent.yml`.

This keeps:

- the filesystem MCP usable without native Agent runtimes
- the host socket and writer locks out of the base deployment
- the security boundary visible in deployment configuration
- existing filesystem-only deployments backward compatible

## Runtime behavior

ServerFS exposes eight structured Agent tools when Agent policy is enabled. They cover runtime discovery, task submission, status/events, approval/question handling, steering where supported, and cancellation.

They are **not** a shell, argv passthrough, or generic command executor.

Current deployments can expose:

- 19 tools: filesystem + Agent
- 21 tools: filesystem + binary + Agent
