---
title: ServerFS MCP
description: Secure, scoped filesystem access on Linux and Windows for ChatGPT and AI agents.
---

ServerFS MCP exposes explicitly configured directories — Linux containers today, native Windows since v0.10.0 — as **controlled workdirs** through the Model Context Protocol.

Current stable release: **v0.11.0**.

It is **read-only by default**. Administrators can opt individual workdirs into narrow file mutations, bounded whole-file binary transfer, and an isolated, separately gated ChatGPT file-parameter ingress path. A host-side Agent Bridge remains optional; Codex, Claude and Qoder keep provider-neutral model discovery and request-scoped overrides, with advisory-only TypeSafe Jev support. v0.10.0 adds the native Windows deployment: an MCP stdio service over a prebuilt Rust/NTFS kernel wheel, with no Docker, WSL or MSVC.

## Capability surfaces

| Surface | Tools | Enabled by |
| --- | ---: | --- |
| Filesystem | 11 | Base deployment |
| Filesystem + binary | 13 | Binary transfer enabled |
| Filesystem + Agent | 21 | Agent overlay enabled |
| Full capability | 23 | Binary + Agent enabled |

There is no shell, generic command executor, recursive delete, or unguarded overwrite.

## Start here

- [Getting Started](./getting-started/) — deploy the base server with the OpenAI Secure MCP Tunnel.
- [Configuration](./configuration/) — define workdirs and effective per-workdir policy.
- [Architecture](./architecture/) — understand the container, tunnel, and Agent Bridge boundaries.
- [Security Model](./security/) — review the defense-in-depth model.
- [Binary Transfer](./binary-transfer/) — enable bounded download/upload, including optional ChatGPT file-parameter ingress.
- [Agent Bridge](./agent-bridge/) — opt into structured Codex/Claude/Qoder delegation.
- [Windows Native](./windows-native/) — the native Windows deployment: prebuilt wheels in two isolated environments, stdio transport, the pinned tunnel-client launcher chain, the fail-closed health report, and (v0.11) Agent delegation for Codex/Claude/Qoder.
- [Jev Advisors](./jev-advisors/) — optional experimental task preflight, five-way runtime routing, pre-submit model advice, and approval advice included in the v0.9.0 host Bridge.

For implementation detail and the complete operational reference, see the repository [README](https://github.com/NTLx/ServerFS_MCP#readme).
