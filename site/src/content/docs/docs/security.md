---
title: Security Model
description: Defense in depth across MCP, Docker, filesystem, transport, and Agent boundaries.
---

ServerFS is designed around **narrow capabilities and independent enforcement layers**.

## Core properties

- Read-only by default.
- Per-workdir mutation opt-in.
- No shell or generic command execution.
- No generic `write_file`.
- No recursive delete or force mode.
- Revision-guarded destructive operations.
- FD-based path traversal with symlink rejection.
- Built-in credential deny rules.
- Read/write/search/binary size limits.
- Structured audit logging without file contents.
- Read-only container root filesystem.
- Non-root container user, dropped capabilities, and no-new-privileges.
- Internal-only MCP network with no published ports.

## Transport hardening

v0.4.0 enables MCP Streamable HTTP DNS-rebinding protection.

The transport accepts only the fixed internal authority `serverfs-mcp:8000`, rejects unexpected or missing Host values, and rejects non-empty unapproved Origin values before MCP dispatch.

## Agent boundary

Agent support is opt-in and lives behind a host-side Unix socket. The MCP container receives structured Agent capabilities, not shell access to the host.

For the full threat model and implementation details, see the repository [Security Model](https://github.com/NTLx/ServerFS_MCP#security-model).
