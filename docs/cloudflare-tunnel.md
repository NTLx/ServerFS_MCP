# Cloudflare Connector

Use Cloudflare Tunnel when a Core needs a public HTTPS route without publishing its own HTTP port. It is optional; direct HTTP or an OpenAI Connector can be selected independently. Cloudflare authenticates the Connector to its provider, not the MCP caller.

## 1. Choose the target and origin

Identify the exact Core, its local endpoint and its deployment mode. A distinct Connector must have an explicitly assigned Tunnel resource and hostname. Do not reassign an existing route or reuse a production token for testing.

| Platform | Origin service address (example) | Component selection |
| --- | --- | --- |
| Linux | `http://serverfs-mcp:8000` | One Cloudflare service per Compose project |
| macOS | `http://127.0.0.1:18781` | File-only or owned Core; additional IDs declared at Core staging |
| Windows | `http://127.0.0.1:18781` | Exact Core ID and Connector ID |

Adjust the origin scheme/port to the actual Core; the public MCP path is normally `/mcp`. The address is relative to the Connector's own network namespace. Do not use a host loopback address inside a Linux Connector container to reach Core.

Configure a remotely managed Cloudflare Tunnel and its public hostname through the provider's management interface. Point its origin to the selected Core. Preserve the intended Host authority (or explicitly configure a known Host override) and admit exactly that value in Core policy. Confirm the public hostname, TLS and path. Ensure caller [API Key](api-key.md) or [OAuth](oauth.md) admission works before public exposure; OAuth also needs a reachable authorization origin and private introspection. Validate the actual combination.

## 2. Prepare private credentials

Use the verified official cloudflared artifact for the platform, pinned to the intended release or image digest. Keep its Tunnel token in an external, owner-private file outside the checkout and every exposed workdir. Linux uses a regular mode-0600 token file readable by the configured nonroot CLOUDFLARED_UID/GID; native Windows uses the protected DACL and native macOS uses owner-private inputs. Never paste the token into argv, env output, a prompt, screenshot or logs. The token is not the Core API Key.

## 3. Attach the Connector

Choose only the matching procedure:

- [Linux](../deployment/linux/README.md#3-add-or-operate-a-connector): retain the full named project, env and exposure/Bridge overlays. Add `compose.cloudflare.yml` and `deployment/linux/compose.cloudflare-profile.yml`, profile `cloudflare`. If also selecting named OpenAI HTTP, append that overlay last. Check Core readiness first, then start only `cloudflared` with `up -d --no-deps --no-build cloudflared`.
- [macOS](../deployment/macos/README.md#6-optional-add-a-connector): stage the selected Connector, then start it explicitly. With owned Core use the owned-connector selector. Additional Connector IDs must be predeclared in the signed App. Shared Core has no named Connector staging interface.
- [Windows](windows-cloudflare-tunnel.md): install the matching Connector runtime, configure the fresh private manifest and register/start only the selected task.

The Linux internal-only overlay avoids publishing Core's host port; use direct HTTP publication only when the deployment intentionally needs it. These commands create or operate local Connector components; they do not create provider resources, DNS or caller credentials.

## 4. Verify the actual route

First verify authenticated local Core MCP; a TCP listener alone is insufficient. Then check the selected Connector's status and recent bounded logs, provider registration and correct hostname. From an authorized remote client, verify HTTPS, MCP initialize, tools/list and a safe workdir read. Test missing/wrong caller credentials and relevant denied operations. Where needed, test long-lived SSE separately.

For a Connector-only restart, record unrelated component identities, stop/start the exact Connector and establish a fresh authorized MCP session. Confirm unrelated Core, Bridge and other Connector identities remain unchanged. Existing SSE streams can end; no guarantee of same-session replay.

## 5. Rotate or retire

A staged native Connector uses pinned/private inputs; do not overwrite its state to force credential rotation. Determine the supported platform-specific create/retire procedure and coordinate provider-side credential changes. Linux may recreate only the selected Connector with its intended full Compose selection after replacing the external private token. Verify remote registration and caller admission before considering rotation complete.

To retire, stop the selected Connector first. Remove its local resources only under explicit authorization and the platform's eligibility checks. Revoke/delete provider credentials or remove the public DNS route only after independently verifying ownership and scope. Never stop Core or Bridge merely to remove a Connector. See [Tunnel diagnostics](https://ntlx.github.io/ServerFS_MCP/docs/tunnel-diagnostics/) and [release checklist](v0.25.0-release-checklist-2026-11-21.md) for troubleshooting and measured acceptance.
