---
title: ServerFS MCP
description: A secure, standards-first MCP filesystem server for Linux, Windows and macOS.
---

ServerFS MCP exposes explicitly configured directories — Linux containers, native Windows since v0.10.0, native macOS since v0.13.0 — as **controlled workdirs** through the Model Context Protocol.

Current stable release: **v0.22.1**. ServerFS is now standards-first: compatible MCP hosts can use native stdio or standard Streamable HTTP, while the OpenAI Secure MCP Tunnel remains an optional ChatGPT ingress. The same filesystem, binary and Agent capability contracts remain shared across Linux, Windows and macOS.

v0.22.0 adds Correlated Agent Diagnostics through the existing private Agent Bridge RPC: `serverfs diagnostics snapshot` for a bounded Bridge snapshot and `serverfs diagnostics task <task-id>` for one retained task. Diagnostic state is process-local and bounded; there is no new MCP tool, listener, persistent trace store, OpenTelemetry dependency, or telemetry egress. Phase F passed on Linux/VPS, supported macOS, and supported Windows; Phase G release-readiness acceptance is PASS. See [v0.22 Phase G preflight](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.22.0-phase-g-preflight-2026-10-22.md).

It is **read-only by default**. Administrators can opt individual workdirs into narrow file mutations, bounded whole-file binary transfer, and an isolated, separately gated ChatGPT file-parameter ingress path. The v0.23 development source additionally introduces independently gated Linux **Deterministic Repo Ops** for bounded Git inspection, integrity-pinned named actions, explicit-file commit, and current-upstream push without Agent delegation. A host-side Agent Bridge remains optional; Native Codex, Claude and Qoder plus configurable ACP v1 profiles provide Agent delegation; provider-neutral model discovery and request-scoped overrides remain runtime-dependent, with advisory-only TypeSafe Jev support. Windows stays native: MCP stdio over a prebuilt Rust/NTFS kernel wheel, with no Docker, WSL or MSVC; v0.14.0 also removes terminal/session lifetime from the normal Windows lifecycle.

## Capability surfaces

| Surface | Tools | Enabled by |
| --- | ---: | --- |
| Filesystem | 11 | Base deployment |
| Filesystem + binary | 13 | Binary transfer enabled |
| Filesystem + Repo Ops | 15 | v0.23 Linux development Repo Ops enabled |
| Filesystem + binary + Repo Ops | 17 | Binary + Repo Ops enabled |
| Filesystem + Agent | 21 | Agent overlay enabled |
| Filesystem + binary + Agent | 23 | Binary + Agent enabled |
| Filesystem + Agent + Repo Ops | 25 | Agent + Repo Ops enabled |
| Full v0.23 development surface | 27 | Binary + Agent + Repo Ops enabled |

The stable v0.22.1 release remains 11 / 13 / 21 / 23 tools. Repo Ops is development-only until public MCP E2E and release publication complete. There is no shell, generic command executor, recursive delete, or unguarded overwrite.

## Start here

- [Getting Started](./getting-started/) — start with standard MCP connectivity; add the OpenAI Tunnel only for the ChatGPT Tunnel path.
- [Configuration](./configuration/) — define workdirs and effective per-workdir policy.
- [Architecture](./architecture/) — understand the MCP core, transport/ingress adapters, deployment and Agent Bridge boundaries.
- [Security Model](./security/) — review the defense-in-depth model.
- [Static API Key](./api-key/) — protect Linux remote Streamable HTTP with one optional shared bearer secret.
- [OAuth 2.1](./oauth/) — protect Linux remote Streamable HTTP with optional OAuth, federated identity and exact stable-principal admission.
- [Binary Transfer](./binary-transfer/) — enable bounded download/upload, including optional ChatGPT file-parameter ingress.
- [Deterministic Repo Ops](./repo-ops/) — v0.23 Linux development capability for bounded Git/action/commit/push operations without Agent delegation.
- [Agent Bridge](./agent-bridge/) — opt into structured Codex/Claude/Qoder and configurable ACP profile delegation.
- [Windows Native](./windows-native/) — native Windows 11 x64 + local NTFS deployment, including current-user persistent service lifecycle, health checks, Tunnel bootstrap, and Agent delegation.
- [macOS Native](./macos-native/) — native Apple M-series + macOS 27 deployment, including the Darwin filesystem boundary, launchd Agent Bridge, and platform gate.
- [Jev Advisors](./jev-advisors/) — optional experimental task preflight, five-way runtime routing, pre-submit model advice, and approval advice; v0.12 adds independent explicit Jev proxy routing.

This documentation site is the authoritative user and operator reference. The repository README is intentionally a concise project entry point.

For complete ACP setup, validation, task lifecycle, and troubleshooting, see the [ACP Guide](./acp/).
