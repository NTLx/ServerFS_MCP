---
title: Cloudflare Connector
description: Attach an independent Cloudflare Tunnel to an existing HTTP Core.
---

Choose Cloudflare when a Core needs a public HTTPS route without a direct public port. It is optional and independent of Core and Bridge. The Tunnel token authenticates cloudflared, not the client; configure separate Core caller admission.

## Select the target

| Platform | Origin (example) | Guide |
| --- | --- | --- |
| Linux | `http://serverfs-mcp:8000` | [Compose](https://github.com/NTLx/ServerFS_MCP/blob/v0.26.0/deployment/linux/README.md) |
| macOS | `http://127.0.0.1:18781` | [Native](../macos-native/) — file-only or owned Core |
| Windows | `http://127.0.0.1:18781` | [Native](../windows-native/) — exact Core and Connector IDs |

Use the actual origin reachable by that Connector. Configure a distinct remotely managed Tunnel and public hostname externally; do not reuse unrelated routes or tokens.

## Deploy and check

1. Verify the selected Core, [API Key](../api-key/) or [OAuth](../oauth/) admission and exact Host allowlist.
2. Prepare a verified cloudflared executable/image and a private token file outside exposed workdirs.
3. Use the platform's scoped Connector procedure; start only the Connector after Core is ready.
4. Verify provider registration, TLS, authorized MCP initialize/tools/list, safe workdir access and denied caller admission.

Restart or retire only the selected Connector. Verify unrelated identities remain unchanged and establish a fresh session afterward. Public reachability and long-lived SSE need separate tests.

See the [Cloudflare operator guide](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/cloudflare-tunnel.md) for private credentials, route configuration and rotation/retirement; see [Tunnel Diagnostics](../tunnel-diagnostics/) for failures.
