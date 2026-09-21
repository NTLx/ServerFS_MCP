---
title: ServerFS MCP
description: Secure, scoped Linux filesystem access for ChatGPT and AI agents.
---

ServerFS MCP exposes explicitly configured Linux directories as **controlled workdirs** through the Model Context Protocol.

It is **read-only by default**. Administrators can opt individual workdirs into narrow file mutations, bounded whole-file binary transfer, and an optional host-side Agent Bridge for Codex or Claude.

## Capability surfaces

| Surface | Tools | Enabled by |
| --- | ---: | --- |
| Filesystem | 11 | Base deployment |
| Filesystem + binary | 13 | Binary transfer enabled |
| Filesystem + Agent | 19 | Agent overlay enabled |
| Full capability | 21 | Binary + Agent enabled |

There is no shell, generic command executor, recursive delete, or unguarded overwrite.

## Start here

- [Getting Started](./getting-started/) — deploy the base server with the OpenAI Secure MCP Tunnel.
- [Configuration](./configuration/) — define workdirs and effective per-workdir policy.
- [Architecture](./architecture/) — understand the container, tunnel, and Agent Bridge boundaries.
- [Security Model](./security/) — review the defense-in-depth model.
- [Binary Transfer](./binary-transfer/) — enable bounded download/upload.
- [Agent Bridge](./agent-bridge/) — opt into structured Codex/Claude delegation.

For implementation detail and the complete operational reference, see the repository [README](https://github.com/NTLx/ServerFS_MCP#readme).
