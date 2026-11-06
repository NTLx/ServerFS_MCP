# Linux / Docker Compose

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

For a source-managed deployment, inspect the checkout before pulling/resetting anything. Do not use destructive Git cleanup to hide operator/developer changes.

Update/pull the intended image/source according to the deployment, then recreate with the **same** overlays/profiles that were already in use. Examples must be adapted to the real deployment shape:

```bash
docker compose --env-file .env -f compose.yml -f compose.http.yml pull
docker compose --env-file .env -f compose.yml -f compose.http.yml up -d
```

If the deployment uses the Host Bridge for Agent delegation and/or v0.23 Repo Ops, retain `-f compose.agent.yml`. For OAuth retain `-f compose.oauth.yml`; for file ingress retain its profile. For a v0.24 Cloudflare Tunnel deployment, also retain `-f compose.cloudflare.yml` and the exact existing Compose project name / private `--env-file` (including `CLOUDFLARE_TUNNEL_TOKEN_FILE`, `CLOUDFLARED_UID/GID`, and independent HTTP API Key/OAuth admission). The Cloudflare-only overlay must never be added as a shortcut to the current production project without its own authentication and isolation review. Using only base `compose.yml` during such a recreation removes the Host Bridge socket/lock mounts even if `.env` still contains Repo Ops or Agent policy.

After recreation, if Repo Ops was enabled, verify that the effective workdir still reports `repo_ops=true`, that `inspect_repo(actions/status)` succeeds, and that no Agent task is used as a substitute. If Agent runtimes are enabled, perform the separate runtime readiness checks required by the current Bridge guide.

After recreation, verify service/image/version evidence and the relevant ingress. If a Tunnel is enabled, route to `serverfs-tunnel` for ingress-specific evidence: OpenAI control-plane poll for the OpenAI adapter; connected Cloudflare Connector plus external HTTPS/MCP session checks for Cloudflare. A running container alone is insufficient.
