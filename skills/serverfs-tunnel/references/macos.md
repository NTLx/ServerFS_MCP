# macOS Connectors

Use `deployment/macos/README.md` and primary Skill gates. Named file-only/owned instances only; shared Core has no Connector staging.

Base Connector per provider needs no additional ID. Additional same-provider IDs must be predeclared at Core staging with corresponding `--cloudflare-connector-id` / `--openai-connector-id`; signed jobs cannot be added in place. Use exact `--connector-id` thereafter and `--owned-connectors` before subcommand for owned pair.

Target actual Core loopback port/Host. Use verified executable and independent private credential file. Cloudflare uses `cloudflare-stage --cloudflared ... --token-file ...`; OpenAI uses `openai-stage --tunnel-client ... --tunnel-id ... --control-plane-key-file ... --core-bearer-file ...` with exact single-line `Bearer <Core API key>`, no trailing newline, static API-key Core. Follow actual CLI flags and Login Items approval.

Activate/stop only selected component with named CLI, not legacy service commands. Verify provider registration, public route/admission and fresh MCP. Local job status is not public evidence; OpenAI upstream bearer denial is checked locally. Never force undeclared jobs or unstage retained state.
