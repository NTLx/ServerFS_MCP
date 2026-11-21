# OAuth 2.1 for remote MCP

Choose OAuth when a Core needs admission by individual federated identity rather than a shared API Key. OAuth is optional, defaults off and is mutually exclusive with static API Key. It does not replace workdir permissions or Bridge grants.

## 1. Choose the deployment

```text
MCP client ── HTTPS ── Core (resource server) ── workdir policy
    │                          │
    │ browser login            │ private token introspection
    ▼                          ▼
serverfs-auth ◄────────────────┘
    │
    └── upstream identity provider
```

The packaged authorization service (`serverfs-auth`) is deployed with Linux Docker Compose. It owns provider credentials, identity admission and OAuth state; Core receives only its resource-server settings and introspection secret. Native macOS/Windows HTTP Cores can be configured as resource servers using an independently managed auth service; the native installers do not provision that service. Local stdio has no HTTP admission.

Use direct HTTP behind an HTTPS reverse proxy, or an independently configured Cloudflare Connector for the MCP route and a reachable HTTPS auth origin. The named OpenAI HTTP Connector uses static Core bearer admission and is **not the OAuth deployment path**.

## 2. Prepare a fresh Linux Core and auth service

Follow the [Linux deployment runbook](../deployment/linux/README.md) for a version-matched image, unique project, nonroot UID/GID, private env and minimal read-only workdir. This example uses a new project with two loopback publications and separately managed TLS routes:

| Public URL | Private origin |
| --- | --- |
| `https://mcp.example.com/mcp` | `http://127.0.0.1:18080/mcp` |
| `https://auth.example.com` | `http://127.0.0.1:18082` |

Preserve the actual external Host and scheme semantics used by metadata and callbacks. Public resource/issuer URLs require HTTPS (plain HTTP only for explicit loopback tests). Configure the auth reverse proxy to expose public OAuth routes, **not** `/internal/introspect`; keep introspection on the private network. DNS, TLS and reverse proxy configuration are external responsibilities.

Add these settings to the selected private env, replacing example domains, paths, IDs and secrets. The Core listener is 8000 and auth listener is 8082; publications use separate unused host ports.

```dotenv
SERVERFS_HTTP_API_KEY=
SERVERFS_HTTP_HOST=0.0.0.0
SERVERFS_HTTP_PORT=8000
SERVERFS_HTTP_PATH=/mcp
SERVERFS_HTTP_PUBLISH_HOST=127.0.0.1
SERVERFS_HTTP_PUBLISH_PORT=18080
SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp:8000,127.0.0.1:18080,mcp.example.com
SERVERFS_HTTP_ALLOWED_ORIGINS=

SERVERFS_OAUTH_ENABLED=true
SERVERFS_OAUTH_RESOURCE_URL=https://mcp.example.com/mcp
SERVERFS_OAUTH_ISSUER_URL=https://auth.example.com
SERVERFS_AUTH_PUBLIC_URL=https://auth.example.com
SERVERFS_OAUTH_REQUIRED_SCOPES=serverfs:access
SERVERFS_OAUTH_INTROSPECTION_SECRET=<unique-high-entropy-secret>

SERVERFS_AUTH_PORT=8082
SERVERFS_AUTH_PUBLISH_HOST=127.0.0.1
SERVERFS_AUTH_PUBLISH_PORT=18082
SERVERFS_AUTH_STATE_HOST_DIR=/absolute/private/blue-auth-state
SERVERFS_AUTH_ALLOWED_PRINCIPALS=<provider:stable-subject>
```

Issuer must equal auth public URL. Resource must be the exact externally meaningful MCP URL. The Compose overlay sets the internal introspection URL from `SERVERFS_AUTH_PORT`; do not confuse it with the published port. Keep each deployment's state directory distinct and outside Git and exposed workdirs. For a new owner directory, if Core uses your login UID/GID, create it as that user:

```bash
install -d -m 700 -- /absolute/private/blue-auth-state
```

Only create a fresh directory; do not change ownership of existing foreign state. If the configured container UID/GID differs, have an administrator create the new directory with the exact ownership and mode 0700. Verify actual ownership and write permission; mode alone is insufficient.

## 3. Select an identity provider and admission policy

Create the corresponding application externally. Register the exact callback at the auth origin. Enable only the providers needed and supply both client ID and secret privately.

| Provider | Callback path | Stable principal | Requested identity scope |
| --- | --- | --- | --- |
| GitHub OAuth App | `/oauth/provider/github/callback` | `github:<numeric-id>` | Public identity; no repository scope |
| DingTalk Open Platform | `/oauth/provider/dingtalk/callback` | `dingtalk:<unionId>` | `openid` |
| Feishu/Lark | `/oauth/provider/feishu/callback` | `feishu:<open_id>` | `contact:user.base:readonly` |
| WeChat Open Platform Website Application QR login | `/oauth/provider/wechat/callback` | `wechat:<unionid>` | `snsapi_login` |

For example, register `https://auth.example.com/oauth/provider/github/callback` and configure:

```dotenv
SERVERFS_OAUTH_GITHUB_ENABLED=true
SERVERFS_OAUTH_GITHUB_CLIENT_ID=<client-id>
SERVERFS_OAUTH_GITHUB_CLIENT_SECRET=<client-secret>
```

For the other providers replace the prefix `SERVERFS_OAUTH_GITHUB_` with `SERVERFS_OAUTH_DINGTALK_`, `SERVERFS_OAUTH_FEISHU_` or `SERVERFS_OAUTH_WECHAT_` respectively; each has the same three suffixes. Feishu does not request offline_access; DingTalk consumes the authCode callback field; WeChat verifies user-info openid matches the token subject. WeChat here is not WeCom or Official Account webpage authorization. Upstream tokens remain ephemeral identity material and are not accepted at MCP.

Admission is an exact comma-separated list in `SERVERFS_AUTH_ALLOWED_PRINCIPALS`. Empty means no admitted principal, not allow all. Wildcards, duplicates and mutable identifiers such as display names, emails and phone numbers are not valid admission strategies. Determine stable subject from the actual provider. Real provider applications, private credentials and browser login are operator prerequisites; do not substitute mock results.

## 4. Select client registration

Clients use Authorization Code with PKCE S256 and ServerFS's default scope `serverfs:access`.

- CIMD (Client ID Metadata Documents): preferred for compatible clients; fetches are HTTPS, public-address checked, TLS verified and bounded/cached.
- Pre-registration: `SERVERFS_AUTH_CLIENTS_FILE` points to a read-only JSON array of public client metadata **inside** the auth service. Each client has a unique client_id, redirect_uris, token_endpoint_auth_method=none, authorization_code grant and code response type, no client secret. The base OAuth overlay does not mount an external client JSON file automatically; explicitly add a read-only bind mount to the auth service in an operator-managed Compose override, and include that override in every operation.
- DCR: compatibility-only, off by default. Set `SERVERFS_AUTH_DCR_ENABLED=true` only for a client that needs it.

## 5. Start and verify

In Bash from the matching checkout root, select the complete deployment. This example is a fresh direct-HTTP, file-only Core. Preserve any deliberately selected Bridge, file-ingress and other provider overlays in a real deployment; do not substitute this selection for a different existing instance.

```bash
oauth=(docker compose -p serverfs-blue --env-file /absolute/private/blue.env
  -f compose.yml -f compose.http.yml -f compose.oauth.yml
  -f deployment/linux/compose.publish-http.yml
  -f deployment/linux/compose.bridge-disabled.yml)

"${oauth[@]}" config --quiet
# Ensure the selected verified image is already available.
"${oauth[@]}" up -d --no-deps --no-build --wait serverfs-auth
"${oauth[@]}" up -d --no-deps --no-build serverfs-mcp
"${oauth[@]}" ps serverfs-auth serverfs-mcp
```

Stop on any failed command. Auth readiness precedes Core activation; the two targeted starts do not start OpenAI, Cloudflare or Bridge. Do not print expanded Compose configuration. To use Cloudflare, follow the [Cloudflare guide](cloudflare-tunnel.md) with the actual complete selection and separate auth origin.

Before public use:

1. Check local auth health (`/healthz`), Core listener, both public TLS origins and authorization server metadata (`/.well-known/oauth-authorization-server`).
2. Request MCP without a credential: expect an OAuth challenge and correct protected-resource metadata. Ensure the private introspection endpoint is not publicly routed.
3. Complete a real browser login for each enabled provider; check exact allowlisted identity and rejection of another identity.
4. Initialize MCP with the issued token; verify tools, safe workdir access and denied operations. Test wrong resource, expired/revoked token and failed introspection denial.
5. Confirm provider secrets are only in the auth service and unrelated components are unchanged. Record actual client/provider acceptance, not just container health.

## 6. Native HTTP resource server

Use the selected platform's authenticated HTTP provisioning path, not the Linux Compose command on a native host. In its supported protected auth input, leave `SERVERFS_HTTP_API_KEY` empty, set `SERVERFS_OAUTH_ENABLED=true`, and supply `SERVERFS_OAUTH_RESOURCE_URL`, `SERVERFS_OAUTH_ISSUER_URL`, `SERVERFS_OAUTH_REQUIRED_SCOPES`, `SERVERFS_OAUTH_INTROSPECTION_URL` and `SERVERFS_OAUTH_INTROSPECTION_SECRET` matching the independently managed auth service. Configure the exact native Host authority and a trusted private path to introspection (HTTPS where it crosses an untrusted network). Do not expose provider credentials to Core. The Windows initial helper is bearer-only; use its documented manual path for OAuth. For named macOS, use the supported private env keys. Neither native installer supplies an auth-service lifecycle manager, and the named OpenAI HTTP Connector requires static bearer admission. Verify the actual native/public combination before use.

## 7. Token lifecycle and operations

ServerFS issues opaque access and refresh tokens; SQLite stores digests, not raw token values. Private introspection validates active status, issuer, resource, client, subject, scope and expiry. Codes/provider state are single-use; refresh rotation deletes the old pair before issuing a new pair. The default access and refresh TTLs are 3600 and 2592000 seconds (30 days), configurable by the auth service.

| Operation | Scope |
| --- | --- |
| Remove allowlisted principal | Recreate auth with updated allowlist; prevents new identity admission, **does not invalidate issued token pairs** |
| Revoke current pair | Authenticated client calls `/revoke` with its access or refresh token; the matching pair is removed |
| Rotate provider secret | Recreate auth only; existing ServerFS tokens are not thereby revoked |
| Rotate introspection secret | Coordinated recreation of auth and all Cores using that secret; mismatch denies access |
| Backup / restore | Stop auth in maintenance, copy/restore whole private state directory with permissions; restart and verify |
| Emergency global invalidation | With explicit authorization, stop auth, retain incident backup, replace state with new empty private directory; restart and verify |

Refresh and introspection do not re-evaluate the current principal allowlist. A removed principal may still use an existing access token or renew a valid refresh pair. Revoke the current pair or deliberately invalidate state; waiting for expiry only helps if the credentials are not renewed before expiry. Do not mistake UI logout or an upstream provider secret change for ServerFS revocation.

Use the exact Compose selection; for env changes use scoped `up -d --no-deps --no-build --force-recreate <service>`, not restart alone. Coordinate shared/external resource servers before disruptive auth changes. Do not delete state casually: replacement revokes every token and loses stored dynamic registration/state. Restoring an old backup can restore previously valid token state; make that security consequence explicit.

Optional provider/CIMD proxy: `SERVERFS_AUTH_USE_PROXY=true` with `SERVERFS_PROXY_HOST/PORT` and optional credential fields, private env only. This switch does not enable Tunnel, Agent or Jev proxying. Core introspection bypasses process proxy settings.

## 8. Diagnose by boundary

| Symptom | Check |
| --- | --- |
| 401 before login | Expected OAuth challenge; check exact resource and discovery metadata |
| Auth startup failure | Enabled provider with ID/secret, issuer/public URL, introspection secret, state owner and path |
| Login denied | Exact stable subject and allowlist; don't authorize by display name |
| Resource mismatch | Canonical client URL and proxy semantics; never broaden audience |
| Provider or CIMD timeout | Auth service egress/proxy, not wider Core access |
| Introspection failure | Private route, endpoint, secret, issuer, resource, expiry; fail closed |

Existing stdio-based OpenAI Tunnel compatibility deployments may use `MCP_OAUTH_TRUSTED_ORIGINS` for an exact separate authorization origin. Historical testing recorded a possible OpenAI resource-rewrite issue (tunnel-client #78, 2026-10-19); current behavior requires separate live verification. Do not generalize that historical path to the named static-bearer HTTP Connector or disable audience checks. See the [release checklist](v0.25.0-release-checklist-2026-11-21.md) for measured acceptance.
