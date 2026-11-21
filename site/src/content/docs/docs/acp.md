---
title: ACP Guide
description: Configure and operate generic ACP v1 profiles with ServerFS.
---

# ACP Guide

ServerFS supports operator-configured generic Agent Client Protocol (ACP) v1 profiles through the optional host-side Agent Bridge. ACP is a standards adapter, not another native provider integration. The established native runtimes remain Codex, Claude Code, and Qoder; choose those when their native integrations are needed. ACP profiles are exposed as runtime IDs `acp-<id>`.

## Prerequisites and authentication

Install and verify the Agent Bridge for the deployment platform, enable it explicitly, and ensure the selected ACP-compatible executable is installed for the same user that owns the Bridge. ACP does not install Agents from a registry. Sign in and manage credentials using the Agent/provider's own supported CLI or UI. Keep provider state, credentials, and shell-only environment in provider-owned private locations; do not put secrets in ServerFS profile configuration, workdirs, task events, or logs.

## Configure a profile

Use an absolute executable path and fixed arguments. Task callers cannot change command, arguments or environment. For a named Linux owner, add this entry to the private Bridge JSON `acp_profiles` array (not the Core environment):

```json
{"id":"opencode","command":"/absolute/path/to/opencode","args":["acp"],"use_proxy":false}
```

The legacy singleton Linux renderer alone uses `SERVERFS_ACP_PROFILES_JSON`. For native macOS and Windows, use the matching owner policy in `serverfs.toml` (follow the platform runbook for provisioning):

```toml
[[agent.acp_profiles]]
id = "opencode"
command = "/absolute/path/to/opencode"
args = ["acp"]
use_proxy = false
```

Use the actual absolute path to a regular executable accessible to the Bridge user. On Unix, `command -v opencode` can help locate it; on Windows use the actual executable path. OpenCode manages its own model; do not pass a model override via ServerFS. Profiles support at most 16 entries, unique lowercase IDs (1–28 characters, starting with a letter; letters/digits/underscore/hyphen; reserved runtime names excluded), and at most 32 fixed arguments of up to 4096 characters each. Encoded profile policy is bounded to 65536 bytes. Initialization and shutdown timeouts default to 5 seconds each, with positive values up to 60 seconds. Proxy use defaults off and Agent proxies must be credentialless.

For workdir `project`, enable writes and explicitly authorize that profile:

```toml
[[workdirs]]
alias = "project"
path = "/absolute/path/to/project"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["acp-opencode"]
```

For named Linux, match this policy in the owner JSON and Core slot 01 (`WORKDIR_01_READ_ONLY=false`, `WORKDIR_01_AGENT_MODE=workspace-write`, `WORKDIR_01_AGENT_RUNTIMES=acp-opencode`). The grant must authorize the required runtime/task methods and alias. Use actual platform paths and the matching native attachment policy on macOS/Windows. ACP never authorizes itself or widens workdir policy.

## Compatibility evidence

The [v0.19.0 historical acceptance record](https://github.com/NTLx/ServerFS_MCP/blob/main/dev_plan_v0.19.md) describes OpenCode 2.1.2 with `opencode/glm-5.3-flash-free` on Linux, macOS and Windows. The tested argv uses `acp`; OpenCode selects the model. This is historical evidence, not v0.25.0 signed-attachment acceptance or a guarantee for other Agent versions.

## Validate Bridge and runtime

Use the named [Linux](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md), [macOS](../macos-native/) or [Windows](../windows-native/) runbook to verify the owner, attachment and effective workdir policy. `list_agent_runtimes` checks executable availability and reports cached capabilities for the matching profile; it does not launch an Agent or prove authentication. A successful authorized task negotiates the protocol and records measured capabilities. Test on a disposable authorized workdir, inspect the terminal result/events and confirm cleanup. The legacy singleton verifier is not evidence of named attachment readiness.

## Task operation semantics

Use the structured MCP Agent tools: `list_agent_runtimes` shows the configured normalized runtimes and capabilities; `submit_agent_task` starts an asynchronous task; `get_agent_task` reports normalized task state/result or a pending interaction; `read_agent_task_events` reads normalized events with a cursor; `read_agent_task_result` retrieves a large spooled result in chunks. Follow the [Agent Bridge task workflow](../agent-bridge/) for polling and result handling.

Where the attachment supports it, a runtime advertising persistent sessions may continue a retained terminal task for the same runtime and workdir using `continue_from_task_id`. The adapter uses stable ACP `session/load`, not raw caller-supplied session IDs. However, v0.25.0 signed v2 Core grants (Linux named, macOS shared and Windows grant-enabled) reject both `continue_from_task_id` and `idempotency_key`, even when the adapter reports resumable sessions. Omit these fields on those paths; do not blindly retry an uncertain submission. V1-compatible paths can use idempotent submission and continuation subject to their policy and measured capabilities. Session resumability does not prove an earlier process has stopped. `cancel_agent_task` records terminal cancellation first and requests a bounded best-effort provider/process stop. The Bridge retains the writer lease/recovery guard until cleanup and stop are established; uncertain stop remains fail-closed. Client disconnect or stopping polls does not cancel a task. After completion, cancel, or failure, verify terminal status and cleanup/lease release before starting another writer in that workdir.

## Security boundary and limitations

ServerFS authorizes the workdir and runtime, holds the shared writer lease for workspace-write work, and fixes the executable/argv/environment at operator configuration. ACP/provider sandbox and approval policy remain provider-owned; the ServerFS writer lease is not a sandbox. ACP does not provide ServerFS model discovery/override, elicitation, ACP client filesystem, or terminal capabilities. Live steering is also unsupported. Interactive approval and persistent session support are reported conservatively from successful negotiation and cached profile-matched evidence. Never expose a generic shell, arbitrary argv, or environment passthrough through ACP tasks, and do not automatically install or trust registry entries.

## Troubleshooting

- **Runtime missing/unavailable:** verify the profile ID, executable absolute path, executable permissions, Bridge user's PATH-independent access, and the exact owner configuration. Changes require the platform's supported lifecycle/provisioning procedure.
- **ACP startup/protocol error:** check the configured executable, fixed argv and the Agent's own diagnostics as the Bridge user; use an isolated test environment rather than launching another writer in the live workdir.
- **Task rejected:** inspect effective workdir policy; confirm `read_only=false`, `agent_mode=workspace-write`, and exact `acp-<id>` allowlist entry.
- **Authentication/model failure:** sign in or select the model using the provider's own tooling. ServerFS cannot discover or override ACP models.
- **Cancellation leaves workdir busy:** do not delete locks or retry a writer. Check Bridge/provider process state and use the documented recovery procedure; the guard remains until stop is proven.
- **Linux container cannot reach Bridge:** use the deployment guide to verify measured peer UID/GID, socket mounts, signed attachment overlay, grant and host Bridge health.

See [Agent Bridge](../agent-bridge/) and the platform deployment guides for lifecycle and IPC setup.
