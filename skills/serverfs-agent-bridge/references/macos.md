# macOS native

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

Agent workdir/runtime policy lives in `serverfs.toml`. Private derived Bridge state stays outside exposed workdirs. Use supported commands:

```bash
serverfs agent-bridge configure --config serverfs.toml --env-file .env
serverfs agent-bridge restart --config serverfs.toml --env-file .env
serverfs service status
serverfs doctor --config serverfs.toml
```

Persistent Bridge ownership is through the app-associated current-user ServiceManagement lifecycle. Do not resurrect the retired `serverfs agent-bridge install` path or hand-install legacy LaunchAgent plists.

Respect Login Items/TCC user decisions; do not bypass them.

ACP v1 profiles are included in released v0.19.0. See the [ACP Guide](https://ntlx.github.io/ServerFS_MCP/docs/acp/) for the full workflow. Profiles are operator-owned absolute executable paths with fixed argv, configured through `[[agent.acp_profiles]]`; task callers cannot override command/argv/environment. Require explicit `workspace-write` workdir authorization. Runtime listing/probe must not launch the Agent. ACP task children use POSIX process-group stop semantics; retain the recovery guard and writer lease if stopping the group cannot be proven. OpenCode 2.1.2 acceptance is recorded across Linux, macOS, and Windows; do not generalize that evidence to all ACP Agents or versions.
