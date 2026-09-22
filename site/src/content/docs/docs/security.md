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
- Internal-only MCP networks with no published ports or Internet egress.
- Optional ChatGPT file ingress is isolated in a separate sidecar with no workdir mounts or OpenAI credentials.

## File-ingress boundary

v0.5 keeps Internet egress out of the ServerFS MCP container. When explicitly enabled, `serverfs-file-ingress` receives a dedicated egress network and a separate internal network shared only with the MCP service. The tunnel is not attached to that ingress network.

The MCP-to-sidecar endpoint is fixed to the internal Compose service and the MCP client does not follow redirects. The sidecar accepts HTTPS on port 443 only, requires an exact administrator-configured hostname allowlist, rejects any DNS answer that is not globally routable, pins the connection to the validated address while verifying TLS for the original hostname, revalidates every upstream redirect, and enforces independent byte and timeout ceilings. It is not a generic URL proxy.

## Transport hardening

v0.4.0 enables MCP Streamable HTTP DNS-rebinding protection.

The transport accepts only the fixed internal authority `serverfs-mcp:8000`, rejects unexpected or missing Host values, and rejects non-empty unapproved Origin values before MCP dispatch.

## Agent boundary

Agent support is opt-in and lives behind a host-side Unix socket. The MCP container receives structured Agent capabilities, not shell access to the host.

For the full threat model and implementation details, see the repository [Security Model](https://github.com/NTLx/ServerFS_MCP#security-model).
