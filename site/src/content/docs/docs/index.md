---
title: ServerFS MCP
description: Start with a file-only Core; choose optional ingress and host execution capabilities.
---

ServerFS MCP gives AI clients controlled access to explicitly configured local directories (workdirs). Read-only is the default; writes require explicit policy and revision checks. Optional capabilities add bounded binary transfer, deterministic Repo Ops and host Agent delegation without exposing a general shell.

**Current stable release: [v0.26.0](https://github.com/NTLx/ServerFS_MCP/releases/tag/v0.26.0).** Use version-matched artifacts and guides. This release restores multiple authorized workdirs, extends signed-v2 task discovery and approval auditing, and supports named Windows runtime proxy policy.

## Start here

1. Read [Architecture](./architecture/) to understand the independent Core, optional Connectors and optional Agent Bridge, including supported platform boundaries.
2. Follow [Getting Started](./getting-started/) for the smallest deployment on your host. Linux uses Docker Compose; [macOS](./macos-native/) and [Windows](./windows-native/) use their native deployment procedures.
3. Configure the exact workdirs, access policy and HTTP admission with [Configuration](./configuration/) and [Security](./security/). Verify the Core before adding dependencies.

## Add only the components you need

| Your requirement | Guide |
| --- | --- |
| Direct remote or local HTTP, no Tunnel | [Getting Started](./getting-started/) |
| Public Cloudflare ingress | [Cloudflare Connector](./cloudflare-tunnel/) |
| OpenAI ingress | [Getting Started](./getting-started/) and [Tunnel Diagnostics](./tunnel-diagnostics/) |
| HTTP caller authentication | [API Key](./api-key/) or [OAuth](./oauth/) |
| Bounded whole-file transfer | [Binary Transfer](./binary-transfer/) |
| Deterministic Git and named repository actions | [Repo Ops](./repo-ops/) |
| Native or ACP Agent tasks | [Agent Bridge](./agent-bridge/) and [ACP](./acp/) |
| Optional advisory assistance | [Jev Advisors](./jev-advisors/) |

Connectors do not own Core or Bridge lifecycle. Each signed Bridge attachment requires an explicit Core grant; shared Core does not manage the Bridge owner. Agent and Repo Ops are separate per-workdir opt-ins.

## Capability surfaces

The tool set is determined by the enabled capabilities; ingress does not add filesystem or Agent authority.

| Surface | Tools |
| --- | ---: |
| Filesystem | 11 |
| Filesystem + binary | 13 |
| Filesystem + Repo Ops | 15 |
| Filesystem + binary + Repo Ops | 17 |
| Filesystem + Agent | 22 |
| Filesystem + binary + Agent | 24 |
| Filesystem + Agent + Repo Ops | 26 |
| All optional surfaces | 28 |

The Agent surface includes `list_agent_tasks`; successful discovery requires a supporting signed-v2 attachment and authorized alias. Registered tools do not imply every operation is available on every platform or protocol.

## Operate and maintain

Use [Agent Skills](./agent-skills/) to guide version-matched installation, configuration, diagnosis, upgrade and component operations. See [Host Observability](./host-observability/) and [Restricted-network ARM64](./restricted-network-arm64/) for their specific deployment scenarios. Do not treat historical compatibility commands as the current named-instance procedure. Exact-release and live acceptance are tracked separately in the [release checklist](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.26.0-release-checklist.md).
