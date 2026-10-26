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
- v0.23 development Repo Ops remains non-Agent, per-workdir opt-in, and exposes no caller-controlled shell/argv/environment/remote/ref/force surface.

## File-ingress boundary

v0.5.0 keeps Internet egress out of the ServerFS MCP container. When explicitly enabled, `serverfs-file-ingress` receives a dedicated egress network and a separate internal network shared only with the MCP service. The tunnel is not attached to that ingress network.

The MCP-to-sidecar endpoint is fixed to the internal Compose service and the MCP client does not follow redirects. The sidecar accepts HTTPS on port 443 only. Host authorization is either an exact administrator-configured hostname or the explicit constrained OpenAI Azure Blob family measured from real ChatGPT fileParams (`oaisdmntpr` storage-account prefix, lowercase ASCII letters/digits only, Azure account-length limit, exact `.blob.core.windows.net` suffix). Generic host wildcards are rejected. After host authorization, the sidecar rejects any DNS answer that is not globally routable, pins the connection to the validated address while verifying TLS for the original hostname, revalidates every upstream redirect, and enforces independent byte and timeout ceilings. It is not a generic URL proxy.

## Transport hardening

v0.4.0 introduced MCP Streamable HTTP DNS-rebinding protection; v0.5.0 retains that transport boundary unchanged.

The transport accepts only the fixed internal authority `serverfs-mcp:8000`, rejects unexpected or missing Host values, and rejects non-empty unapproved Origin values before MCP dispatch.

## Static API Key boundary

v0.17 can protect remote Streamable HTTP with one optional shared API Key. The key is checked before MCP dispatch, compared with a constant-time comparison, and does not create a user identity or alter workdir authorization. Header authentication (`Authorization: Bearer <key>`) is preferred. The compatibility `?key=` form is accepted for one-line MCP URLs, but ServerFS disables its own default Uvicorn access log in this mode because query credentials are more likely to leak through URL logs. Upstream proxies remain the operator's responsibility. Static API Key and built-in OAuth are mutually exclusive. See [Static API Key](../api-key/).

## OAuth boundary

v0.16 can protect remote Streamable HTTP with an isolated `serverfs-auth` authorization server. OAuth is default-off. The auth service alone receives provider credentials and provider/CIMD Internet egress; `serverfs-mcp` receives neither and validates only ServerFS-issued opaque resource-bound tokens through a private introspection link.

Admission is default-deny and uses exact stable provider subjects. Provider-native tokens are never accepted at MCP. Authorization codes/provider state are single-use, refresh tokens rotate, raw bearer values are not persisted, and resource/audience validation remains strict. Existing workdir policy stays authoritative after identity admission; ServerFS does not duplicate workdir permissions into OAuth scopes.

The OpenAI Tunnel OAuth path keeps the same validation. A known upstream resource-rewrite issue is documented rather than bypassed with wildcard audiences. See [OAuth 2.1](../oauth/) for deployment, revocation, backup and provider details.

## Deterministic Repo Ops boundary (v0.23 development)

Repo Ops is an independently gated Linux host capability, not an Agent execution mode. It reuses the authenticated host Bridge transport and shared writer lease, but its `repo.*` RPC path never creates an Agent task or invokes a provider adapter.

Git operations have fixed semantics. Callers cannot select executable, argv, environment, cwd, remote, ref, force, hooks, signing, askpass, repository SSH overrides, or custom remote helpers. Commit accepts explicit policy-valid paths only and refuses pre-existing staged state; push derives the current branch/upstream internally and supplies one exact refspec. Child environments are allowlisted so Bridge/provider secrets are not inherited wholesale.

Named actions are administrator-owned fixed argv profiles. Every action requires a non-empty SHA-256 integrity map over repository-controlled execution inputs, and the Bridge verifies those pins immediately before process creation. All named actions acquire the exclusive writer lease even when marked `mutates=false`, preventing an integrity-check/execute race with another ServerFS or Agent writer. Timeout, output overflow, and cancellation terminate the Linux subprocess group before the lease is released.

See [Deterministic Repo Ops](../repo-ops/) for the complete tool, policy, and acceptance contract.

## Agent boundary

Agent support is opt-in and lives behind a host-side Unix socket. The MCP container receives structured Agent capabilities, not shell access to the host.

In v0.12, Linux Agent egress is explicit and independent from Tunnel/Jev routing. `SERVERFS_AGENT_USE_PROXY=true` may use only a credentialless shared HTTP proxy; authenticated proxy fields cause rendering/startup to fail closed so provider credentials are never embedded in standard proxy environment variables. Proxied Codex is isolated in a Bridge-owned standalone app-server rather than changing the user's shared managed daemon. Native provider approvals remain provider-controlled and require an explicit response through the existing public Agent tool.

## Jev advisor boundary

Jev is optional and runs only from the host Agent Bridge. It is not part of the MCP authorization path, runtime allowlist, writer lease, or native provider approval authority. A missing/failed Jev call removes advisory context only; it never grants additional capability.

The TypeSafe API key is rendered only into the user-owned Bridge config and is never passed to the MCP container. In v0.12, `SERVERFS_JEV_USE_PROXY` supplies the shared proxy through Jev's explicit HTTP client rather than mutating process-wide proxy variables. Approval Advisor sends a minimized, redacted approval state and applies an additional secret/token/password sanitizer before the external request. Identical approval advice may be cached only within the same task and never resolves the approval automatically.

See [Jev Advisors](../jev-advisors/) for the complete advisory contract.

For adjacent trust-boundary detail, see [Architecture](../architecture/), [Configuration](../configuration/), [Deterministic Repo Ops](../repo-ops/), [Static API Key](../api-key/) and [OAuth 2.1](../oauth/). Version-specific implementation evidence remains in the repository's phase acceptance records rather than the project README.
