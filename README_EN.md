# ServerFS MCP

**English** · [简体中文](README.md)

[![Release](https://img.shields.io/github/v/release/NTLx/ServerFS_MCP)](https://github.com/NTLx/ServerFS_MCP/releases/latest)

- [Website](https://ntlx.github.io/ServerFS_MCP/)
- [Documentation](https://ntlx.github.io/ServerFS_MCP/docs/)
- [Releases](https://github.com/NTLx/ServerFS_MCP/releases/tag/v0.26.0)

**Current stable release: [v0.26.0](https://github.com/NTLx/ServerFS_MCP/releases/tag/v0.26.0).**

v0.26.0 restores multi-workdir Agent/Repo Ops on named Linux Cores, extends macOS Shared Cores to multiple Agent workdirs, supports explicit signed v2 on newly provisioned macOS Owned Cores, and adds per-runtime/ACP proxy configuration for named Windows Bridge owners. Signed v2 adds paginated task discovery scoped to an authorized alias; shared Bridge approvals retain private audit identity before public redaction and cannot remotely approve recognized outside-workdir writes. See the [release notes](docs/releases/v0.26.0.md) and [release checklist](docs/v0.26.0-release-checklist.md).

ServerFS MCP is a cross-platform, self-hosted MCP filesystem service. It exposes explicitly configured local directories (workdirs) to AI clients, with read-only defaults and revision-guarded writes. Optional binary transfer, deterministic repository operations (Repo Ops) and host Agent delegation extend its capabilities. There is no shell or general command-execution interface.

## Compose only what you need

```text
MCP client ── direct HTTP / optional Tunnel Connector ── MCP Core ── workdirs
                                                           │
                                                    optional Agent Bridge
                                                           │
                                                    host Agents / Repo Ops
```

- **MCP Core** owns MCP sessions, HTTP caller admission, workdir policy and file operations. Local native stdio does not require a persistent HTTP service.
- **Connector** is an optional OpenAI or Cloudflare Tunnel ingress with its own ingress credentials and lifecycle. Direct HTTP is not a Connector.
- **Agent Bridge** is an optional host execution boundary. Agent delegation and Repo Ops require separate per-workdir authorization; installing a Bridge does not enable them automatically.

Persistent deployments can contain multiple named Cores (`instance_id`), their ingress components (distinguished by `connector_id` where supported) and independent Bridge owners (`bridge_id`). Each Core has a Bridge mode:

| Mode | Why choose it |
| --- | --- |
| disabled | File capabilities only, without a Bridge or provider credentials |
| owned | Dedicated Bridge with isolated task state and runtime |
| shared | Explicit attachment to an existing independently managed Bridge on the same host |

A Shared Core does not own the Bridge lifecycle or gain extra authority from a shared user or IPC endpoint. Independent Bridges must not claim overlapping writable roots. Independent component lifecycles still have dependencies: start and retire components according to platform readiness, active-task and stop-safety checks.

## Choose a platform

| Platform | Deployment | Guide |
| --- | --- | --- |
| Linux | Docker Compose for named Cores and optional Connectors; direct HTTP or internal networking; optional host user systemd Bridge | [Linux deployment](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/linux/README.md) |
| macOS | Apple Silicon, native arm64, macOS 27, APFS; signed SMAppService / LaunchAgents and authenticated Unix IPC | [macOS deployment](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/macos/README.md) |
| Windows | Windows 11 x64, local NTFS, matching Native Wheel; current-user Task Scheduler, Job Objects and Named Pipe, without WSL/VM | [Windows deployment](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/docs/deployment/windows/v0250-native-operations.md) |

The public MCP contract and security outcomes are shared, not necessarily the internal implementation. Follow the platform guides for actual component combinations, named Connector support, installation and upgrade limits. No central service manager is introduced.

## Capabilities and security

The filesystem surface has 11 tools. Binary transfer adds 2, Agent delegation adds 11, including `list_agent_tasks` (usable on supporting signed-v2 attachments), and Repo Ops adds 4. Capabilities are independently enabled; the full registered surface has 28 tools. Actual operations depend on attachment protocol, grants and runtime support. Repo Ops does not launch an Agent. Agent delegation uses host-native Codex, Claude, Qoder or explicit ACP profiles; Jev is advisory only.

Workdir containment, deny/reserved-path policy, revision checks and write authorization do not expand with ingress choice. Configure exact HTTP Host/Origin admission; public ingress requires caller authentication. Tunnel credentials are separate from HTTP caller credentials. Bridge attachment requires explicit per-Core authorization rather than trusting a shared UID/SID.

## Development checkout

The v0.27.0 platform split is in development; it is not a release or a migration instruction.
The owning source and build/test directory is [Linux](linux/), [macOS](macos/) or
[Windows](windows/). Common public contracts and distribution tooling remain at repository
level. Follow [AGENTS.md](AGENTS.md) and the [maintainer workflow](docs/maintainers/workflow.md)
for candidate development and validation.

## Get started

Begin with a minimal file-only instance; verify MCP file access, then add Connectors, Bridge and other capabilities only as required.

- [Architecture and deployment choices](https://ntlx.github.io/ServerFS_MCP/docs/architecture/) · [Getting Started](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/)
- [HTTP API Key](docs/api-key.md) · [OAuth](docs/oauth.md) · [Cloudflare Connector](docs/cloudflare-tunnel.md)
- [Agent Bridge](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/agent_bridge/README.md) · [Agent Skills](skills/README.md) (seven Skills and a skills-only Plugin)
- [Repository documentation index](docs/README.md) · [Maintainer guide](docs/maintainers/README.md)

Use documentation, source and installation artifacts matching the target version; do not implicitly migrate existing instances. [Release notes](docs/releases/v0.26.0.md) and the [release checklist](docs/v0.26.0-release-checklist.md) record actual acceptance and remaining gates. Historical evidence remains in `docs/releases/`, `docs/diagnostics/` and architecture decision records.
