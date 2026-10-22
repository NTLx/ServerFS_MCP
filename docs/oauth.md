# OAuth 2.1 for remote MCP

ServerFS v0.16 adds optional OAuth 2.1 authentication for **remote Streamable HTTP** deployments. OAuth is disabled by default and is not used by native Windows/macOS stdio. In v0.17+, built-in OAuth is mutually exclusive with `SERVERFS_HTTP_API_KEY`; choose one ServerFS-owned HTTP authentication mode per instance.

This is the canonical operator guide for the v0.16 OAuth deployment model. The website pages summarize this contract; when they disagree, this file and the versioned `.env.example` are authoritative.

## Scope

OAuth protects the MCP resource boundary. It does not replace ServerFS workdir policy.

```text
upstream identity provider
        |
        v
serverfs-auth                 Internet egress only here
  identity federation
  exact principal allowlist
  OAuth 2.1 authorization server
  opaque ServerFS tokens
        |
        | private introspection
        v
serverfs-mcp                  no provider credentials / no OAuth egress
        |
        v
existing workdir policy
```

The supported upstream identity providers are:

- GitHub OAuth App
- DingTalk Open Platform login
- Feishu/Lark user identity OAuth
- WeChat Open Platform Website Application login

Provider tokens are used only inside `serverfs-auth` to prove identity. They are not stored as ServerFS bearer credentials and are never accepted directly at `/mcp`.

## Deployment boundaries

OAuth is currently a **Linux Docker Compose** capability:

- base deployment: `compose.yml`
- generic Streamable HTTP: `compose.http.yml`
- OAuth: `compose.oauth.yml`
- optional Agent Bridge: `compose.agent.yml`

Windows and macOS native stdio remain OAuth-free. Do not add provider credentials to native stdio merely for configuration symmetry.

Public OAuth deployments require HTTPS at the public boundary. Plain HTTP is accepted only for explicit loopback/local test fixtures.

## Minimal production shape

A typical deployment uses two public HTTPS origins:

```text
https://mcp.example.com/mcp   -> 127.0.0.1:8000/mcp
https://auth.example.com      -> 127.0.0.1:8082
```

The reverse proxy must preserve the external host/scheme semantics used in OAuth metadata and callbacks. Keep the Docker publications loopback-only and publish TLS through the reverse proxy rather than binding the containers directly to a public interface.

Set at least:

```env
SERVERFS_OAUTH_ENABLED=true

SERVERFS_OAUTH_RESOURCE_URL=https://mcp.example.com/mcp
SERVERFS_AUTH_PUBLIC_URL=https://auth.example.com
SERVERFS_OAUTH_ISSUER_URL=https://auth.example.com
SERVERFS_OAUTH_REQUIRED_SCOPES=serverfs:access

SERVERFS_OAUTH_INTROSPECTION_SECRET=<long-random-shared-secret>
SERVERFS_AUTH_ALLOWED_PRINCIPALS=<provider:stable-subject,...>
```

`SERVERFS_OAUTH_ISSUER_URL` must exactly equal `SERVERFS_AUTH_PUBLIC_URL`.

For the MCP Host/Origin boundary, configure only the public authorities you actually use, for example:

```env
SERVERFS_HTTP_ALLOWED_HOSTS=mcp.example.com
```

Do not disable DNS-rebinding protection to make a proxy work.

### Prepare persistent auth state

`serverfs-auth` is non-root and has one writable persistent mount. The host directory must already exist and be writable by the configured `SERVERFS_UID:GID`.

A simple current-user deployment can use:

```bash
mkdir -p .serverfs-auth-state
chmod 700 .serverfs-auth-state
```

and set `SERVERFS_UID` / `SERVERFS_GID` to the current user's `id -u` / `id -g` when that matches the deployment policy.

Start the OAuth shape with:

```bash
docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  up -d
```

Add `-f compose.agent.yml` only when Agent delegation is required.

Verify the rendered deployment before starting it:

```bash
docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  config --quiet
```

## Admission policy

ServerFS admission is default-deny. `SERVERFS_AUTH_ALLOWED_PRINCIPALS` is a comma-separated list of exact `provider:stable-subject` values. Wildcards are intentionally unsupported.

Stable subjects are:

| Provider | ServerFS principal | Source identity |
| --- | --- | --- |
| GitHub | `github:<numeric-id>` | immutable numeric GitHub user `id` |
| DingTalk | `dingtalk:<unionId>` | Open Platform account `unionId` |
| Feishu | `feishu:<open_id>` | app-scoped `open_id` |
| WeChat | `wechat:<unionid>` | Open Platform `unionid` |

Names, email addresses, mobile numbers, nicknames and tenant-local mutable identifiers are not authorization keys.

Removing a principal prevents future admission. It is not a substitute for revoking already issued credentials; see **Revocation and emergency invalidation** below.

## Provider setup

Enable providers independently. An enabled provider requires both a client ID and client secret.

> **External live-acceptance prerequisite:** the repository cannot manufacture real GitHub, Feishu/Lark, DingTalk or WeChat Open Platform applications or operator credentials. The v0.16.0 release validates all four adapters with deterministic protocol/security tests, but a production operator must still create the corresponding provider application, register the exact callback below, supply real credentials outside Git, and complete at least one real browser login for each provider they enable. Absence of those external applications/credentials is an explicit deployment prerequisite, not a reason to weaken ServerFS validation or embed test credentials in the project.

### GitHub

Create a GitHub OAuth App and register this exact callback URL:

```text
https://auth.example.com/oauth/provider/github/callback
```

Configure:

```env
SERVERFS_OAUTH_GITHUB_ENABLED=true
SERVERFS_OAUTH_GITHUB_CLIENT_ID=<client-id>
SERVERFS_OAUTH_GITHUB_CLIENT_SECRET=<client-secret>
```

ServerFS deliberately does not request repository scopes. The adapter needs only the authenticated public user identity and authorizes by immutable numeric user ID.

### Feishu / Lark

Create the corresponding application and register:

```text
https://auth.example.com/oauth/provider/feishu/callback
```

Configure:

```env
SERVERFS_OAUTH_FEISHU_ENABLED=true
SERVERFS_OAUTH_FEISHU_CLIENT_ID=<app-id>
SERVERFS_OAUTH_FEISHU_CLIENT_SECRET=<app-secret>
```

The flow requests only `contact:user.base:readonly`; it does not request `offline_access`. ServerFS authorizes by `open_id` and does not persist a Feishu refresh token.

### DingTalk

Create the Open Platform application/login capability and register:

```text
https://auth.example.com/oauth/provider/dingtalk/callback
```

Configure:

```env
SERVERFS_OAUTH_DINGTALK_ENABLED=true
SERVERFS_OAUTH_DINGTALK_CLIENT_ID=<client-id>
SERVERFS_OAUTH_DINGTALK_CLIENT_SECRET=<client-secret>
```

The flow requests only `openid`. The callback uses DingTalk's `authCode` field, and admission is keyed by `unionId`. Provider access/refresh tokens remain ephemeral.

### WeChat Open Platform

This integration is **WeChat Open Platform Website Application QR login**. It is not WeCom/企业微信 and not Official Account webpage authorization.

Register:

```text
https://auth.example.com/oauth/provider/wechat/callback
```

Configure:

```env
SERVERFS_OAUTH_WECHAT_ENABLED=true
SERVERFS_OAUTH_WECHAT_CLIENT_ID=<app-id>
SERVERFS_OAUTH_WECHAT_CLIENT_SECRET=<app-secret>
```

The flow uses `snsapi_login`; ServerFS verifies that user-info `openid` matches the token subject and authorizes by Open Platform `unionid`.

## Client registration: pre-registration, CIMD and DCR

ServerFS supports three client-identification paths:

1. **Operator pre-registration** — optional read-only client metadata supplied through `SERVERFS_AUTH_CLIENTS_FILE`.
2. **Client ID Metadata Documents (CIMD)** — preferred for standards-current clients. ServerFS performs strict HTTPS URL validation, DNS/public-address checks, TLS hostname verification and bounded in-memory caching.
3. **Dynamic Client Registration (DCR)** — compatibility-only and disabled by default. Enable only when a client genuinely requires it:

```env
SERVERFS_AUTH_DCR_ENABLED=true
```

Public clients use Authorization Code + PKCE S256. Do not enable DCR merely because it exists; prefer CIMD or explicit pre-registration where the client supports them.

## Token and resource model

ServerFS issues its **own opaque tokens**. Access and refresh token values are not stored raw in SQLite; the state store persists digests and associated claims.

Important properties:

- access tokens are bound to the configured `SERVERFS_OAUTH_RESOURCE_URL`;
- the Resource Server validates issuer, resource, client, subject, scope and expiry through private introspection;
- provider-native tokens cannot authenticate directly to MCP;
- authorization codes and provider login state are single-use;
- refresh tokens rotate on use and replay fails closed;
- the default required scope is `serverfs:access`; workdir read/write/Agent policy remains separate and is not encoded as OAuth scopes.

## OpenAI Secure MCP Tunnel

OAuth-disabled Tunnel operation remains supported and regression-tested with the repository-tested `tunnel-client v0.0.17` baseline. This is the tested default, not a compatibility lock.

When OAuth uses a different authorization-server origin from the MCP resource origin, configure the Tunnel's official trusted-origin setting in `.env`:

```env
MCP_OAUTH_TRUSTED_ORIGINS=https://auth.example.com
```

`compose.oauth.yml` passes this value only to `openai-tunnel`.

### Known upstream limitation

As verified on 2026-10-19, OpenAI tunnel-client issue #78 remains open: ChatGPT may replace the canonical RFC 9728 resource value with the hosted Tunnel resource during the authorization flow. ServerFS does **not** work around this by accepting wildcard audiences, disabling resource validation, or hard-coding undocumented OpenAI gateway hosts.

Therefore:

- direct/generic OAuth is a supported ServerFS path;
- OAuth-disabled OpenAI Tunnel is supported;
- OAuth-through-Tunnel requires live acceptance for the specific OpenAI behavior in use and may remain blocked by the upstream resource rewrite.

## Proxy routing

Provider/CIMD egress belongs only to `serverfs-auth`.

Enable it explicitly:

```env
SERVERFS_AUTH_USE_PROXY=true
SERVERFS_PROXY_HOST=proxy.example.internal
SERVERFS_PROXY_PORT=7890
SERVERFS_PROXY_USERNAME=
SERVERFS_PROXY_PASSWORD=
```

This switch is independent from `SERVERFS_OPENAI_TUNNEL_USE_PROXY`, `SERVERFS_AGENT_USE_PROXY` and `SERVERFS_JEV_USE_PROXY`. Enabling one does not implicitly enable another.

## Backup and restore

The OAuth state directory contains SQLite authorization state, including client state and digests for ServerFS-issued credentials. Protect backups as security-sensitive data even though raw access/refresh tokens are not stored.

For a consistent filesystem-level backup:

1. stop only `serverfs-auth` during a maintenance window;
2. copy the entire configured `SERVERFS_AUTH_STATE_HOST_DIR` with permissions preserved;
3. start `serverfs-auth` again;
4. verify `/healthz` through the private/local path.

Restore the complete state directory while `serverfs-auth` is stopped. Do not merge individual SQLite files from different snapshots.

## Revocation and emergency invalidation

OAuth clients can use the authorization server's `/revoke` endpoint to revoke ServerFS-issued credentials. Refresh rotation also invalidates stale/replayed credentials automatically.

Operational rules:

- remove a principal from `SERVERFS_AUTH_ALLOWED_PRINCIPALS` to prevent future admission;
- use `/revoke` when the affected ServerFS credential is available;
- changing provider credentials does not invalidate already issued ServerFS access tokens by itself;
- for emergency instance-wide invalidation, stop `serverfs-auth`, preserve an incident backup if required, replace its OAuth state with a new empty state directory, and restart. This intentionally invalidates all issued ServerFS credentials and loses dynamic client/state registrations, so use it only as a deliberate recovery action.

## Secret rotation

### Provider client secret

Update the provider's `*_CLIENT_SECRET`, then recreate/restart `serverfs-auth`. The MCP Resource Server does not receive provider secrets.

### Introspection secret

`SERVERFS_OAUTH_INTROSPECTION_SECRET` is shared only between `serverfs-mcp` and `serverfs-auth`. Rotate it atomically: update the value and recreate both services in the same maintenance change. A mismatch fails closed.

Never commit real provider or introspection secrets. `.env` remains private/ignored; `.env.example` contains names and safe placeholders only.

## Troubleshooting

### MCP returns 401 before login

This is expected when OAuth is enabled and no valid ServerFS bearer is present. Check the `WWW-Authenticate` / protected-resource metadata path and verify `SERVERFS_OAUTH_RESOURCE_URL` exactly matches the externally meaningful MCP URL.

### Authorization server cannot start

Check:

- `SERVERFS_OAUTH_ENABLED=true`;
- at least one provider is enabled;
- every enabled provider has client ID + client secret;
- `SERVERFS_AUTH_PUBLIC_URL` and `SERVERFS_OAUTH_ISSUER_URL` are identical;
- public URLs use HTTPS outside loopback;
- the introspection secret is non-empty;
- the auth state directory exists and is writable by `SERVERFS_UID:GID`.

### Login succeeds but access is denied

The authenticated upstream identity is not in the exact stable-subject allowlist. Determine the provider's stable subject and add the exact `provider:subject` value; do not authorize by display name/email as a shortcut.

### Resource mismatch

Do not broaden the audience. Confirm the MCP client requested the configured canonical resource and that the reverse proxy did not alter the external URL semantics. For OpenAI Tunnel, also check the known upstream resource-rewrite limitation above.

### Provider calls time out

If the host requires an HTTP proxy, enable `SERVERFS_AUTH_USE_PROXY` and the shared proxy endpoint. Do not give `serverfs-mcp` general Internet egress.

### Tunnel discovery cannot reach a separate auth origin

Set `MCP_OAUTH_TRUSTED_ORIGINS` to the exact public authorization-server origin. Do not use a wildcard.

## Verification checklist

Before public use, verify:

- `SERVERFS_OAUTH_ENABLED=false` still preserves the historical deployment when OAuth is not wanted;
- base/HTTP/OAuth Compose rendering succeeds;
- both public origins terminate valid TLS;
- unauthenticated MCP access returns an OAuth challenge when enabled;
- authorization-server metadata is reachable at `/.well-known/oauth-authorization-server`;
- `/healthz` is healthy;
- the intended provider callback URL exactly matches its application registration;
- admission is default-deny and uses stable provider subjects;
- an issued ServerFS token works only for the configured MCP resource;
- revocation/expiry behaves as expected;
- provider credentials appear only in `serverfs-auth`, not `serverfs-mcp`.

For implementation evidence and the v0.16 threat model, see `dev_plan_v0.16.md`.
