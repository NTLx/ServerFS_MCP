---
title: ACP Guide
description: Configure and operate generic ACP v1 profiles with ServerFS v0.19.0.
---

# ACP Guide

ServerFS v0.19.0 supports operator-configured generic Agent Client Protocol (ACP) v1 profiles through the optional host-side Agent Bridge. ACP is a standards adapter, not another native provider integration. The established native runtimes remain Codex, Claude Code, and Qoder; choose those when their native integrations are needed. ACP profiles are exposed as runtime IDs `acp-<id>`.

## Prerequisites and authentication

Install and verify the Agent Bridge for the deployment platform, enable it explicitly, and ensure the selected ACP-compatible executable is installed for the same user that owns the Bridge. ACP does not install Agents from a registry. Sign in and manage credentials using the Agent/provider's own supported CLI or UI. Keep provider state, credentials, and shell-only environment in provider-owned private locations; do not put secrets in ServerFS profile configuration, workdirs, task events, or logs.

## Configure a profile

Use an absolute executable path and fixed arguments. Task callers cannot change command, arguments, or environment. On Linux/Docker, set `SERVERFS_ACP_PROFILES_JSON` in the root `.env` (activated through the explicit Agent Compose overlay):

```dotenv
SERVERFS_ACP_PROFILES_JSON=[{"id":"opencode","command":"/absolute/path/to/opencode","args":["acp"]}]
```

On Windows and macOS, configure the Bridge's native `serverfs.toml`:

```toml
[[agent.acp_profiles]]
id = "opencode"
command = "/absolute/path/to/opencode"
args = ["acp"]
use_proxy = false
```

Use the platform's actual absolute path. On Unix systems, locate it with `command -v opencode` and resolve that result to an absolute path; on Windows use the actual absolute executable path. `opencode acp --help` has no model argument, so do not put a model in argv. OpenCode manages its selected model itself. ServerFS ACP does not support model discovery or request-scoped model override.

For workdir `project`, enable writes and explicitly authorize that profile:

```toml
[[workdirs]]
alias = "project"
path = "/srv/projects/project"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["acp-opencode"]
```

The same effective policy is required when workdir settings come from environment variables: `read_only=false`, `agent_mode=workspace-write`, and an allowlist containing the exact `acp-<id>` runtime. ACP never authorizes itself or widens workdir policy.

## OpenCode 2.1.2 example and evidence

The tested profile uses `args = ["acp"]`; the model `opencode/glm-5.3-flash-free` is selected and managed by OpenCode, not passed to ServerFS. On the acceptance Linux host the executable was `/home/lx/.opencode/bin/opencode`; this is only that host's recorded example. Use `command -v opencode` or the platform's actual absolute path in a deployment.

OpenCode 2.1.2 with `opencode/glm-5.3-flash-free` is recorded as passing real end-to-end acceptance on Linux, macOS, and Windows, including prompts, persisted-session continuation, and active-tool cancellation/cleanup. This evidence applies to that tested OpenCode/version/model combination and does not guarantee compatibility for every ACP Agent or version.

## Validate Bridge and runtime

Run the platform's current Bridge verification procedure (Linux source checkouts commonly use `uv run --frozen --no-sync python deployment/agent-bridge/verify_host.py --require-runtimes`; Windows/macOS use their native deployment verification and `serverfs doctor --config serverfs.toml`). Confirm the Bridge is healthy, the effective workdir policy allows `acp-opencode`, and runtime discovery/listing reports the configured runtime available. Listing/probing does not launch an Agent to discover capabilities. Then use a disposable authorized workdir for a small task and inspect its terminal result and events.

## Task operation semantics

Use the structured MCP Agent tools: `list_agent_runtimes` shows the configured normalized runtimes and capabilities; `submit_agent_task` starts an asynchronous task; `get_agent_task` reports normalized task state/result or a pending interaction; `read_agent_task_events` reads normalized events with a cursor; `read_agent_task_result` retrieves a large spooled result in chunks. Reusing an idempotency key with the same semantic submission returns the retained task rather than starting another turn.

When the runtime reports continuation support, continue a prior terminal ServerFS task by submitting a new task with `continue_from_task_id=<prior task id>`. The Bridge reuses the persisted ACP session associated with that task; callers do not pass a raw ACP session ID. Session resumability does not prove an earlier process has stopped. `cancel_agent_task` records terminal cancellation first and requests a bounded best-effort provider/process stop. The Bridge retains the writer lease/recovery guard until cleanup and stop are established; uncertain stop remains fail-closed. Client disconnect or stopping polls does not cancel a task. After completion, cancel, or failure, verify terminal status and cleanup/lease release before starting another writer in that workdir.

## Security boundary and limitations

ServerFS authorizes the workdir and runtime, holds the shared writer lease for workspace-write work, and fixes the executable/argv/environment at operator configuration. ACP/provider sandbox and approval policy remain provider-owned; the ServerFS writer lease is not a sandbox. ACP does not provide ServerFS model discovery/override, elicitation, ACP client filesystem, or terminal capabilities. Other capabilities are reported conservatively from measured behavior. Never expose a generic shell, arbitrary argv, or environment passthrough through ACP tasks, and do not automatically install or trust registry entries.

## Troubleshooting

- **Runtime missing/unavailable:** verify the profile ID, executable absolute path, executable permissions, Bridge user's PATH-independent access, and Bridge configuration reload/restart.
- **ACP startup/protocol error:** run the configured executable with the fixed `acp` argument as the Bridge user; check the Agent's own diagnostics and ACP support/version.
- **Task rejected:** inspect effective workdir policy; confirm `read_only=false`, `agent_mode=workspace-write`, and exact `acp-<id>` allowlist entry.
- **Authentication/model failure:** sign in or select the model using the provider's own tooling. ServerFS cannot discover or override ACP models.
- **Cancellation leaves workdir busy:** do not delete locks or retry a writer. Check Bridge/provider process state and use the documented recovery procedure; the guard remains until stop is proven.
- **Linux container cannot reach Bridge:** use the deployment guide to verify measured peer UID/GID, socket mounts, Agent overlay, and host Bridge health.

See [Agent Bridge](../agent-bridge/) and the platform deployment guides for lifecycle and IPC setup.
