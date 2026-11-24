---
title: Architecture
description: Independent components, instance topologies, native platform boundaries and lifecycles.
---

## Component model

ServerFS grants an MCP client controlled access to selected local directories, called **workdirs**. A persistent HTTP deployment is assembled from an MCP Core, zero or more ingress Connectors, and an optional host Agent Bridge. Local stdio is a separate transport choice; it does not register a persistent HTTP instance.

```text
MCP client ── direct HTTP ───────────────┐
           ├─ OpenAI Connector ─────────┤
           └─ Cloudflare Connector ─────┤
                                        ▼
                                   HTTP MCP Core ── scoped workdirs
                                        │
                           authenticated local IPC
                                        │
                                  optional Bridge ── host Agents / Repo Ops
```

These are logical paths, not a requirement to deploy all components or a guarantee that every combination has the same platform-specific command surface.

| Component | Responsibility | Ownership |
| --- | --- | --- |
| MCP Core | MCP sessions, HTTP caller admission, workdir policy, file access and optional tool exposure | Its own configuration and runtime |
| Connector | Forward MCP traffic to a selected HTTP Core through an external Tunnel | Its own provider credentials, identity and process |
| Agent Bridge | Execute authorized Agent tasks and deterministic Repo Ops on the host; coordinate writer leases | Its own private runtime, policy, IPC and task state |

Direct HTTP is a Core exposure option, not a Connector. A Tunnel is never mandatory for a local or privately reachable Core. Public ingress requires an independent caller-admission boundary; a Tunnel credential is not an MCP caller credential. The named OpenAI HTTP Connector uses a static Core bearer header, not an OAuth-only Core.

## Identities and Bridge modes

A persistent Core is identified by `instance_id`. An ingress is selected by its provider, target Core and, where supported, `connector_id`. The Bridge owner has a distinct state and `bridge_id`. IDs are stable identifiers; an ID is neither a credential nor proof of authorization. The native identifier grammar is 1–32 ASCII lowercase letters, digits and internal hyphens, with an alphanumeric first and last character. The legacy `default` identity retains its existing resources; new named deployments do not implicitly migrate it.

| Bridge mode | Meaning |
| --- | --- |
| `disabled` | No Bridge reference. File-only operation, with Agent and Repo Ops disabled. Binary transfer may still be enabled independently. |
| `owned` | A dedicated Bridge for the deployment. macOS/Windows use the Core ID as the owner ID; Linux uses a distinct Bridge ID. |
| `shared` | A Core references a separately managed Bridge on the same host; only its owner controls grants and lifecycle. |

Every signed attachment needs an explicit grant for the Core identity and permitted workdir/method scope. A common UID, SID, socket or directory is not authorization. Revoked or stale grants, replay and unauthorized task/result access must fail closed. Independent Bridge owners must not claim overlapping writable roots; same-user component isolation is not a sandbox between mutually untrusted users.

## Deployment topologies

| Topology | Why choose it |
| --- | --- |
| A — several Connectors → one Core → one Bridge | Multiple ingress paths to the same Core policy and session owner. |
| B — multiple Cores → one shared Bridge | Distinct Core policies and caller admission with a common, explicitly authorized host execution boundary. |
| C — independent Core/Bridge pairs | Separate task state, runtime and lifecycle; use disjoint writable roots. |
| D — one or more file-only Cores | No Agent Bridge, Agent runtime or provider credentials required. |

A topology describes the product model. Follow the platform runbook for its supported provisioning path, limits and acceptance evidence; do not infer support for the Cartesian product of every feature.

## Platform implementations

| Platform | Core / Connector lifecycle | Bridge lifecycle and IPC |
| --- | --- | --- |
| Linux | Named Compose project; direct HTTP publication, internal-only or optional OpenAI/Cloudflare services | Optional host user systemd unit, Unix socket, peer credentials and signed grants |
| macOS | Signed app-associated SMAppService / LaunchAgents; same-provider Connector IDs predeclared when staging file-only/owned instances | Private Unix socket and policy checks for owned; signed v2 grants for shared |
| Windows | Current-user Task Scheduler, component identities and Job containment | Separate owner/worker environments, Named Pipe, SID, protected ACLs and signed grants |

Current implementation limits:

- **Linux:** one service per Tunnel provider per Compose project. The v0.25.0 signed overlay binds one Agent/Repo Ops-enabled workdir in slot 01; the v0.26.0 candidate supports 1–16 independently granted Agent and/or Repo Ops workdirs across slots 01–16. Additional disjoint file-only slots remain possible.
- **macOS:** a Shared Core binds one Agent workdir in v0.25.0 or 1–16 owner-bound Agent workdirs in the v0.26.0 candidate, with Repo Ops disabled and no Connector staging interface. File-only and owned instances support predeclared additional Connector IDs.
- **Windows:** named Connector IDs and signed owned/shared attachments. The initial shared helper accepts up to 16 Cores; fresh runtime/configuration helpers are create-only, not live upgrades or credential rotation.

See [Getting Started](../getting-started/), [macOS](../macos-native/) and [Windows](../windows-native/) for precise procedures.

The public MCP contract and security outcomes are common. Private IPC encoding, state layout and service implementations need not be identical across OSes. No central service manager is required.

## Lifecycle and failure boundaries

Independence means operating a Connector does not implicitly restart Core, Bridge or another Connector. Stage required dependencies, verify readiness, then explicitly start the desired components. A Core/Bridge stop can be refused while dependent components remain active; use the supported retirement order and check active tasks/writer leases first. A shared Core must never start, stop, upgrade, uninstall or reconfigure its Bridge owner. A Bridge outage must not grant authority; file-only capabilities can remain available, while Bridge-dependent requests fail safely. An ingress restart does not promise replay of an old SSE session.

## Optional capabilities

- **Binary transfer:** opt-in bounded whole-file transfer; optional file ingress uses a separately isolated HTTPS fetch path. See [Binary Transfer](../binary-transfer/).
- **Repo Ops:** four bounded repository tools, fixed Git semantics and integrity-pinned named actions. Requires an authorized Bridge but never starts an Agent task. See [Repo Ops](../repo-ops/).
- **Agent delegation:** native Codex, Claude, Qoder or explicitly configured ACP profiles; provider-native authentication and permissions remain in effect. See [Agent Bridge](../agent-bridge/) and [ACP](../acp/).
- **Jev:** optional advisory-only decisions, never execution or approval authority. See [Jev Advisors](../jev-advisors/).

See [Configuration](../configuration/), [Security](../security/) and [HTTP authentication](../api-key/) for policy details. Release-level source/build gates and real native/public acceptance are recorded separately in the [release checklist](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.25.0-release-checklist-2026-11-21.md).
