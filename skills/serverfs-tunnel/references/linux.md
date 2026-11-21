# Linux OpenAI Connector

Use the matching `deployment/linux/README.md` and primary Skill gates. For Cloudflare use [Cloudflare Linux](cloudflare-linux.md).

Retain the exact project, private env and complete exposure/auth/file-ingress/Bridge selection. Append `deployment/linux/compose.openai-http.yml` and profile `openai-http`; if both providers are enabled, OpenAI overlay is last. One service per provider per project.

Use verified `OPENAI_TUNNEL_IMAGE` supporting HTTP upstream, file keys and extra/discovery headers. Set `CONTROL_PLANE_TUNNEL_ID`, distinct private `OPENAI_CONTROL_PLANE_KEY_FILE` and `SERVERFS_CORE_BEARER_FILE` (exact single-line `Bearer <Core API key>`, no trailing newline). Admit actual `serverfs-mcp:<HTTP port>` Host. OAuth-only Core is unsupported. Connector egress and image pulls are distinct.

Validate with `config --quiet`, pull/select intended image and use the complete Compose array for `up -d --no-deps --no-build openai-tunnel`. Do not print expanded config or run project-wide up/down. Check local Core and selected service, then `exec openai-tunnel /usr/bin/tunnel-client health --port 8080 --require-control-plane-poll --json` for the named overlay; verify actual installed client supports the flags.

A successful poll is not proof of response delivery. Verify public MCP from intended client, provider admission, and missing/wrong key denial at local Core separately. Scoped stop/start only; verify fresh sessions and unchanged other components.
