# Linux Cloudflare Connector

Use `deployment/linux/README.md`, `docs/cloudflare-tunnel.md` and primary Skill gates. Verify operator-controlled Tunnel/route and exact hostname; origin is actual service DNS/port (normally `http://serverfs-mcp:8000`), not host loopback.

Choose verified `CLOUDFLARED_IMAGE`. Set absolute `CLOUDFLARE_TUNNEL_TOKEN_FILE` to a private 0600 file readable by actual configured `CLOUDFLARED_UID/GID`. Preserve existing project/env/exposure/auth/Bridge selection; append `compose.cloudflare.yml` and `deployment/linux/compose.cloudflare-profile.yml` with profile `cloudflare`. If OpenAI is also enabled retain its overlay last. One provider service per project. Tunnel-only exposure uses internal-only overlay; avoid accidental host port publication.

After local Core acceptance, validate `config --quiet`, pull/select intended image and start only `cloudflared` with `up -d --no-deps --no-build cloudflared`. Verify registration, external DNS/TLS, exact forwarded Host, remote authenticated MCP and missing/wrong caller credential denial. Token is not caller auth. Scoped stop/start only and fresh session verification; no unrelated DNS or service changes.
