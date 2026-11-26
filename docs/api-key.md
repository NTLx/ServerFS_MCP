# Static API Key authentication

Use one shared secret per HTTP Core when the callers are a single operator or a small set of trusted clients. For individually identified users and revocable credentials, use [OAuth](oauth.md). Authentication admits a caller; the Core's workdir policy still controls every operation.

## 1. Configure the selected Core

| Deployment | Where to configure |
| --- | --- |
| Linux | Selected Compose project's private env: `SERVERFS_HTTP_API_KEY` |
| macOS named HTTP | Private HTTP env supplied to the selected instance's staging command |
| Windows named HTTP | Initial private configuration helper generates the key; the manual path accepts a protected auth file |
| Local stdio | No HTTP key required |

Follow the [Linux](../deployment/linux/README.md), [macOS](../deployment/macos/README.md) or [Windows](deployment/windows/v0250-native-operations.md) runbook. Generate a unique high-entropy ASCII secret locally (for example, a random hex string). Keep it outside Git and all exposed workdirs. Set OAuth disabled; a nonempty API key and enabled OAuth are rejected together. Native HTTP requires one authentication mode; Linux defaults to neither, so explicitly configure admission before exposure.

The Core's Host/Origin policy is independent of the key. For public use, configure HTTPS at the selected Connector or an operator-managed reverse proxy, and admit the exact Host authority. Tunnel credentials authenticate Connectors, not MCP callers.

The named OpenAI HTTP path additionally stores a private header file containing exactly `Bearer <Core API key>` without a trailing newline (Windows initial helper derives this from the Core configuration). Keep its content synchronized with that Core; it is not the control-plane API key. See the platform runbook for provisioning and limits.

## 2. Configure the client

Preferred: supply the MCP URL and bearer header through private client configuration.

```http
Authorization: Bearer <key>
```

For a client that only accepts a one-line URL, use the compatibility query form:

```text
https://mcp.example.com/mcp?key=<key>
```

URL-encode the value if necessary. Use only one credential source per request. Missing, incorrect, empty, duplicate or mixed header/query credentials are rejected with HTTP 401 before MCP dispatch; this is not an OAuth challenge. Do not place actual keys in commands, screenshots, logs or chat. Query strings can leak through infrastructure logs and browser history. ServerFS disables its own default Uvicorn access log in API Key mode; configure upstream logging separately.

## 3. Verify

Use a trusted MCP client, not only a TCP health probe.

- Confirm the intended URL and Host; verify no-key and wrong-key requests fail.
- With the correct bearer, initialize a new MCP session, list tools and read a safe workdir path. Verify denied writes or other relevant restrictions.
- If using the URL form, test it separately; reject simultaneous header and query credentials.
- For public ingress, test from the intended remote client. The named OpenAI Connector injects a Core bearer header, so test Core key denial at the Core separately from provider-side caller admission. Confirm unrelated Core/Connector/Bridge identities remain unchanged.

The middleware uses constant-time comparison and has no per-user identity or multi-key grace set. Workdir, revision, binary and Bridge authorization remain unchanged.

## 4. Rotate or recover

Plan a maintenance change for the exact Core and account for active sessions/tasks. Generate a replacement key privately; account for all clients and any OpenAI Connector's derived bearer file. For Linux, retain the full Compose selection and update the private env, then recreate only the selected Core with `up -d --no-deps --no-build --force-recreate serverfs-mcp`. Recreate any affected OpenAI Connector separately using its full selection after updating the private bearer file. Restart alone does not reload container environment; do not restart unrelated Bridge/Connector services. The old key ceases to authenticate against the new process. Verify both old-key denial and new-key access.

Named native provisioning is create-only and may pin private auth snapshots and executable/configuration identities. No general online key-rotation command is provided. Use an explicit platform-appropriate migration/reprovision plan, preserving existing state and rollback capability; never edit protected manifests or grants to force a change. Stop if the safe path cannot be established.

If an external gateway is the only admission boundary on Linux, explicitly assess its enforcement, bypass paths and Core reachability. Never rely on a Tunnel registration or an unguessable URL as caller authorization. Native HTTP still requires API Key or OAuth.
