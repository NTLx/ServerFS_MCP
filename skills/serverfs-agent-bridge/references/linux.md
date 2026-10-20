# Linux / Docker + host Bridge

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

The base `compose.yml` is intentionally Agent-unaware. Agent delegation requires `compose.agent.yml` and the current-user host Bridge.

Follow the current deployment guide in order:

1. merge Agent settings into the existing `.env`; never replace it;
2. define selected workdir `AGENT_MODE`/runtime allowlist;
3. measure the **real** container peer UID/GID using `deployment/agent-bridge/measure_peercred.py` and the documented real-image probe;
4. stop if the measured peer does not match the current user — do not solve this with root/privileged ownership;
5. confirm provider executable paths and minimal user-owned `provider.env` when needed;
6. install/update through:

```bash
deployment/agent-bridge/install.sh
```

7. verify:

```bash
python3 deployment/agent-bridge/verify_host.py
python3 deployment/agent-bridge/verify_host.py --require-runtimes
```

8. render/deploy using the Agent overlay:

```bash
docker compose --env-file .env -f compose.yml -f compose.agent.yml config --quiet
docker compose --env-file .env -f compose.yml -f compose.agent.yml up -d
```

If generic HTTP/OAuth/file ingress is already part of the deployment, preserve those existing overlays/profiles too. Never recreate an Agent-enabled MCP container with base Compose alone.

The Bridge service is `systemctl --user`; no system-level unit or `loginctl enable-linger` side effect is introduced by ServerFS. Host linger policy is external and must be reported rather than worked around with root.

Agent proxying is credentialless-only on the supported Linux path. Do not inject shared proxy credentials into provider children. Tunnel and Jev proxy decisions remain independent.
