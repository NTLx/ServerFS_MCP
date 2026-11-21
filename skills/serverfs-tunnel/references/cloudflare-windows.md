# Windows Cloudflare Connector

Use `deployment/windows/v0250-native-operations.md`, `docs/windows-cloudflare-tunnel.md` and primary Skill gates. Select exact Core and Connector ID. Install provider runtime once per Core; further IDs share its environment, not credentials/tasks.

Prepare verified executable with trusted SHA256 and private protected-DACL token input. Use `configure-v25-connector.py` with the actual runbook arguments. Helper copies inputs and creates manifests, not registration/start. It is create-only; no overwrite/retry of existing private state. Audit duplicate Tunnel identities; `manual_cloudflare_peers_unchecked` requires checking legacy/manual provider routes independently before activation.

Configure external origin to actual Core loopback port/Host. Use `serverfs_mcp.windows_v25.cli cloudflare --instance <id> --connector-id <id>` with scoped install/start/status/stop. Verify local Core, provider registration, DNS/TLS and public MCP caller admission/denial independently. Registration is not public readiness. No legacy group restart, administrator ownership or unrelated Core/Bridge change.
