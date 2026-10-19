---
title: ServerFS MCP
description: A secure, standards-first MCP filesystem server for Linux, Windows and macOS.
---

ServerFS MCP exposes explicitly configured directories — Linux containers, native Windows since v0.10.0, native macOS since v0.13.0 — as **controlled workdirs** through the Model Context Protocol.

Current stable release: **v0.15.0**. ServerFS is now standards-first: compatible MCP hosts can use native stdio or standard Streamable HTTP, while the OpenAI Secure MCP Tunnel remains an optional ChatGPT ingress. The same filesystem, binary and Agent capability contracts remain shared across Linux, Windows and macOS.

It is **read-only by default**. Administrators can opt individual workdirs into narrow file mutations, bounded whole-file binary transfer, and an isolated, separately gated ChatGPT file-parameter ingress path. A host-side Agent Bridge remains optional; Codex, Claude and Qoder keep provider-neutral model discovery and request-scoped overrides, with advisory-only TypeSafe Jev support. Windows stays native: MCP stdio over a prebuilt Rust/NTFS kernel wheel, with no Docker, WSL or MSVC; v0.14.0 also removes terminal/session lifetime from the normal Windows lifecycle.

## Capability surfaces

| Surface | Tools | Enabled by |
| --- | ---: | --- |
| Filesystem | 11 | Base deployment |
| Filesystem + binary | 13 | Binary transfer enabled |
| Filesystem + Agent | 21 | Agent overlay enabled |
| Full capability | 23 | Binary + Agent enabled |

There is no shell, generic command executor, recursive delete, or unguarded overwrite.

## Start here

- [Getting Started](./getting-started/) — start with standard MCP connectivity; add the OpenAI Tunnel only for the ChatGPT Tunnel path.
- [Configuration](./configuration/) — define workdirs and effective per-workdir policy.
- [Architecture](./architecture/) — understand the MCP core, transport/ingress adapters, deployment and Agent Bridge boundaries.
- [Security Model](./security/) — review the defense-in-depth model.
- [OAuth 2.1](./oauth/) — protect Linux remote Streamable HTTP with optional OAuth, federated identity and exact stable-principal admission.
- [Binary Transfer](./binary-transfer/) — enable bounded download/upload, including optional ChatGPT file-parameter ingress.
- [Agent Bridge](./agent-bridge/) — opt into structured Codex/Claude/Qoder delegation.
- [Windows Native](./windows-native/) — native Windows 11 x64 + local NTFS deployment, including current-user persistent service lifecycle, health checks, Tunnel bootstrap, and Agent delegation.
- [macOS Native](./macos-native/) — native Apple M-series + macOS 27 deployment, including the Darwin filesystem boundary, launchd Agent Bridge, and platform gate.
- [Jev Advisors](./jev-advisors/) — optional experimental task preflight, five-way runtime routing, pre-submit model advice, and approval advice; v0.12 adds independent explicit Jev proxy routing.

This documentation site is the authoritative user and operator reference. The repository README is intentionally a concise project entry point.
