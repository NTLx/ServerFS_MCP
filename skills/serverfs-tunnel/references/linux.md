# Linux / Docker Compose

Use this branch after the source-resolution and preflight gates in `SKILL.md`. Repository paths and command examples refer to the located checkout/deployment; confirm syntax and settings against its current CLI, templates and guide before execution.

The supported Tunnel deployment uses the base Compose path with Tunnel settings already present in the existing `.env`:

```bash
docker compose pull
docker compose up -d
docker compose ps
docker compose logs --tail=200 openai-tunnel
```

If Agent delegation or file ingress is already enabled, preserve the deployment's existing overlay/profile when recreating the stack.

Use the running Tunnel's own layered health evidence:

```bash
docker compose ps openai-tunnel
docker compose exec openai-tunnel /usr/bin/tunnel-client health --port 8080 --json
docker compose exec openai-tunnel \
  /usr/bin/tunnel-client health --port 8080 --require-control-plane-poll --json
```

When deeper evidence is needed, query the loopback-only component endpoints from inside the container as documented in Tunnel Diagnostics. Do not publish the health/admin port just for troubleshooting.

`SERVERFS_OPENAI_TUNNEL_USE_PROXY` controls Tunnel egress only. It does not configure Docker image pulls, Agent provider traffic, Jev or OAuth-provider traffic.
