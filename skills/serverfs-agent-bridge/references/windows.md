# Windows native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Agent Bridge is a separate Python distribution/environment from the ServerFS connector environment. Preserve the frozen process boundary selected by `SERVERFS_BRIDGE_PYTHON`; never install both distributions into one environment to simplify deployment. The v0.23 Deterministic Repo Ops implementation is Linux-only; this Windows branch does not provide native Repo Ops transport or policy and must not infer support from the shared Bridge concept.

Agent policy lives in `serverfs.toml`; secret/proxy/runtime values use the documented private env boundary. Run:

```powershell
& $serverfs doctor --config serverfs.toml
& $serverfs service status
```

The current-user Task Scheduler lifecycle owns persistent operation. Do not use SCM/LocalSystem/Admin services.

For provider/runtime failures, compare direct provider CLIs with ServerFS runtime discovery. ServerFS must not rewrite provider authentication or sandbox policy.

ACP v1 profiles are included in released v0.19.0. See the [ACP Guide](https://ntlx.github.io/ServerFS_MCP/docs/acp/) for the full workflow. Profiles are operator-owned absolute executable paths with fixed argv, configured through `[[agent.acp_profiles]]`; task callers cannot override command/argv/environment. Require explicit `workspace-write` workdir authorization. Runtime listing/probe must not launch the Agent. ACP children inherit the supervisor's existing Job Object; stop the task process tree at task scope and do not create a second Job. Retain the recovery guard and writer lease if stop cannot be proven. OpenCode 2.1.2 acceptance is recorded across Linux, macOS, and Windows; do not generalize that evidence to all ACP Agents or versions.
