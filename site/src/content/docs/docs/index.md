---
title: ServerFS MCP
description: Secure, scoped Linux filesystem access for ChatGPT and AI agents.
---

ServerFS MCP exposes explicitly configured Linux directories as **controlled workdirs** through the Model Context Protocol.

Current stable release: **v0.7.2**.

It is **read-only by default**. Administrators can opt individual workdirs into narrow file mutations, bounded whole-file binary transfer, and an isolated, separately gated ChatGPT file-parameter ingress path. A host-side Agent Bridge for Codex or Claude remains optional, and can itself opt into advisory-only TypeSafe Jev support without changing the MCP tool surface.

## Capability surfaces

| Surface | Tools | Enabled by |
| --- | ---: | --- |
| Filesystem | 11 | Base deployment |
| Filesystem + binary | 13 | Binary transfer enabled |
| Filesystem + Agent | 20 | Agent overlay enabled |
| Full capability | 22 | Binary + Agent enabled |

There is no shell, generic command executor, recursive delete, or unguarded overwrite.

## Start here

- [Getting Started](./getting-started/) — deploy the base server with the OpenAI Secure MCP Tunnel.
- [Configuration](./configuration/) — define workdirs and effective per-workdir policy.
- [Architecture](./architecture/) — understand the container, tunnel, and Agent Bridge boundaries.
- [Security Model](./security/) — review the defense-in-depth model.
- [Binary Transfer](./binary-transfer/) — enable bounded download/upload, including optional ChatGPT file-parameter ingress.
- [Agent Bridge](./agent-bridge/) — opt into structured Codex/Claude delegation.
- [Jev Advisors](./jev-advisors/) — optional experimental task preflight, runtime routing advice, and approval advice included in the v0.7.2 host Bridge.

For implementation detail and the complete operational reference, see the repository [README](https://github.com/NTLx/ServerFS_MCP#readme).
