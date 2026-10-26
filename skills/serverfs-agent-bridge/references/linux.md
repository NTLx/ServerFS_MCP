# Linux / Docker + host Bridge

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

The base `compose.yml` is intentionally host-Bridge-unaware. Agent delegation and the v0.23 Linux Repo Ops transport require `compose.agent.yml` plus the current-user host Bridge; their per-workdir authorization remains independent.

Follow the current deployment guide in order:

1. merge only the required Host Bridge / Agent settings into the existing `.env`; never replace it;
2. define selected workdir capability policy: Agent `AGENT_MODE`/runtime allowlist when Agent delegation is requested, and Repo Ops policy through `serverfs-configure` when deterministic repository operations are requested; do not enable one merely because the other uses the same transport;
3. measure the **real** container peer UID/GID using `deployment/agent-bridge/measure_peercred.py` and the documented real-image probe;
4. stop if the measured peer does not match the current user — do not solve this with root/privileged ownership;
5. confirm provider executable paths and minimal user-owned `provider.env` only when Agent runtimes are enabled; Repo Ops-only deployments do not need provider runtime setup;
6. install/update through:

```bash
deployment/agent-bridge/install.sh
```

7. verify the user-scoped Bridge deployment:

```bash
uv run --frozen --no-sync python deployment/agent-bridge/verify_host.py
```

When Agent runtimes are enabled and runtime readiness is required, additionally run:

```bash
uv run --frozen --no-sync python deployment/agent-bridge/verify_host.py --require-runtimes
```

For Repo Ops-only deployments, do not require provider runtimes; verify the public Repo Ops surface after the Bridge transport is live.

8. render/deploy using the Host Bridge overlay:

```bash
docker compose --env-file .env -f compose.yml -f compose.agent.yml config --quiet
docker compose --env-file .env -f compose.yml -f compose.agent.yml up -d
```

If generic HTTP/OAuth/file ingress is already part of the deployment, preserve those existing overlays/profiles too. Never recreate a Host-Bridge-enabled MCP container with base Compose alone, whether the Bridge is used for Agent delegation, Repo Ops, or both.

The Bridge service is `systemctl --user`; no system-level unit or `loginctl enable-linger` side effect is introduced by ServerFS. Host linger policy is external and must be reported rather than worked around with root.

Agent proxying is credentialless-only on the supported Linux path. Do not inject shared proxy credentials into provider children. Tunnel and Jev proxy decisions remain independent.

The v0.23.0 stable release reuses this authenticated host Bridge transport for independently gated Linux Deterministic Repo Ops. Do not route Repo Ops work through Agent submission when the deterministic surface is available; see the [Repo Ops guide](https://ntlx.github.io/ServerFS_MCP/docs/repo-ops/).

ACP v1 profiles are included in released v0.19.0. See the [ACP Guide](https://ntlx.github.io/ServerFS_MCP/docs/acp/) for the full workflow. Profiles are operator-owned absolute executable paths with fixed argv, configured through `SERVERFS_ACP_PROFILES_JSON`; never accept task-level launch metadata. Configure `acp-*` only for explicitly authorized `workspace-write` workdirs. Runtime listing/probe must not launch the Agent. ACP task children use POSIX process-group stop semantics; retain the recovery guard and writer lease if stopping the group cannot be proven. OpenCode 2.1.2 acceptance is recorded across Linux, macOS, and Windows; do not generalize that evidence to all ACP Agents or versions.
