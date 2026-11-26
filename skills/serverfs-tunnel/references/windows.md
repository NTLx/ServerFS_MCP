# Windows OpenAI Connector

Use `docs/deployment/windows/v0250-native-operations.md` and primary Skill gates. For Cloudflare use [Cloudflare Windows](cloudflare-windows.md).

Install the requested provider runtime once per Core using verified installed controller and offline wheels. Additional same-provider named IDs share its environment but have independent manifests/credentials/tasks.

Prepare verified tunnel-client.exe and matching bundled cloudflared.exe with independent trusted SHA256 pins, Tunnel ID and private control-plane key file. Initial `configure-v25-connector.py` derives the upstream bearer header from actual Core admission; static bearer required, not OAuth-only. Use the runbook's exact helper arguments; no separate caller-supplied header in this helper. Private DACL and create-only checks apply. No rerun/overwrite to replace an existing manifest.

Use `serverfs_mcp.windows_v25.cli openai --instance <id> --connector-id <id>` with documented install/start/status/stop actions. Registration requires explicit authorization and startup the intended ready Core. Verify provider admission, remote MCP, local Core denial and response delivery separately. No Core/Bridge restart or default-owner/group operation.
