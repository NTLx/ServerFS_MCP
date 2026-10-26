---
title: OAuth 2.1
description: Protect remote Streamable HTTP with optional OAuth 2.1 and federated identity.
---

ServerFS v0.16.0 adds **optional OAuth 2.1 authentication for remote Streamable HTTP**. It is disabled by default and does not apply to native Windows/macOS stdio. In v0.17+, built-in OAuth is mutually exclusive with `SERVERFS_HTTP_API_KEY`; choose one ServerFS-owned HTTP authentication mode per instance.

OAuth answers **who may enter this ServerFS instance**. Existing workdir policy still decides **what the admitted caller may do**.

```text
GitHub / DingTalk / Feishu / WeChat
                │
                ▼
          serverfs-auth
     identity + exact allowlist
       ServerFS opaque tokens
                │ private introspection
                ▼
           serverfs-mcp
                │
                ▼
        existing workdir policy
```

`serverfs-auth` owns upstream identity-provider egress and credentials. `serverfs-mcp` receives neither provider credentials nor general Internet egress.

## Deployment boundary

OAuth is currently a Linux Docker Compose capability.

Add `compose.agent.yml` only when an authenticated host Bridge capability is needed: Agent delegation, the v0.23 Linux Repo Ops development transport, or both. Their per-workdir authorization remains independent of OAuth and of each other.

A typical public deployment uses separate HTTPS origins:

```text
https://mcp.example.com/mcp -> 127.0.0.1:8000/mcp
https://auth.example.com    -> 127.0.0.1:8082
```

Keep Docker publications loopback-only and terminate public TLS at a reverse proxy. The proxy must preserve the external host/scheme semantics used by OAuth metadata and callbacks.

Minimal `.env` settings:

```text
SERVERFS_OAUTH_ENABLED=true
SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp:8000,127.0.0.1:8000,localhost:8000,mcp.example.com
SERVERFS_OAUTH_RESOURCE_URL=https://mcp.example.com/mcp
SERVERFS_AUTH_PUBLIC_URL=https://auth.example.com
SERVERFS_OAUTH_ISSUER_URL=https://auth.example.com
SERVERFS_OAUTH_REQUIRED_SCOPES=serverfs:access
SERVERFS_OAUTH_INTROSPECTION_SECRET=<long-random-secret>
SERVERFS_AUTH_ALLOWED_PRINCIPALS=<provider:stable-subject,...>
```

`SERVERFS_OAUTH_ISSUER_URL` must exactly equal `SERVERFS_AUTH_PUBLIC_URL`. Public URLs must use HTTPS outside explicit loopback tests.

The Host allowlist above preserves the internal and loopback paths and adds the public MCP authority in this example. Replace `mcp.example.com` with the actual Host authority preserved by your proxy; adjust ports if customized. Keep Host validation enabled.

Before starting Compose, prepare the auth state directory for the configured `SERVERFS_UID:SERVERFS_GID`. With the default `10001:10001` and `SERVERFS_AUTH_STATE_HOST_DIR=./.serverfs-auth-state`, run from the checkout root:

```bash
sudo install -d -m 700 -o 10001 -g 10001 -- .serverfs-auth-state
```

Assigning ownership to `10001:10001` normally requires `sudo` (or an administrator); the container still runs as that non-root identity. If you customize `SERVERFS_UID`, `SERVERFS_GID` or `SERVERFS_AUTH_STATE_HOST_DIR` in `.env`, replace the numeric IDs and path above with those actual values. Compose reads `.env`; these values are not automatically exported into your shell.

For a current-user deployment, you may instead set `SERVERFS_UID` and `SERVERFS_GID` in `.env` to the outputs of `id -u` and `id -g`, provided that identity also suits the MCP workdir permissions. For a new directory owned by that user, no privilege is needed:

```bash
install -d -m 700 -- .serverfs-auth-state
```

Use the configured state path if customized. Mode `700` alone does not grant a different container UID write access; verify the directory owner matches the configured UID before starting. Keep the directory private; do not widen its permissions.

After configuring the provider credentials and admission policy below, start OAuth:

```bash
docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  up -d
```

## Default-deny admission

`SERVERFS_AUTH_ALLOWED_PRINCIPALS` accepts exact comma-separated stable identities only. Wildcards are rejected.

| Provider | Principal | Stable source |
| --- | --- | --- |
| GitHub | `github:<numeric-id>` | immutable numeric user `id` |
| DingTalk | `dingtalk:<unionId>` | Open Platform `unionId` |
| Feishu | `feishu:<open_id>` | app-scoped `open_id` |
| WeChat | `wechat:<unionid>` | Open Platform `unionid` |

Display names, email addresses, mobile numbers and nicknames are not authorization keys.

## Provider callbacks

Enable only the providers you need; each enabled provider requires a client ID and secret.

> **External live-acceptance prerequisite:** ServerFS cannot ship real GitHub, Feishu/Lark, DingTalk or WeChat Open Platform applications or operator credentials. v0.16.0 validates all four adapters with deterministic protocol/security tests; before production use, create the relevant provider application, register the exact callback below, keep real credentials outside Git, and complete a real browser login for each provider you enable.

### GitHub OAuth App

Callback:

```text
https://auth.example.com/oauth/provider/github/callback
```

```text
SERVERFS_OAUTH_GITHUB_ENABLED=true
SERVERFS_OAUTH_GITHUB_CLIENT_ID=<client-id>
SERVERFS_OAUTH_GITHUB_CLIENT_SECRET=<client-secret>
```

ServerFS deliberately requests no repository scope and authorizes by immutable numeric GitHub user ID.

### Feishu / Lark

Callback:

```text
https://auth.example.com/oauth/provider/feishu/callback
```

```text
SERVERFS_OAUTH_FEISHU_ENABLED=true
SERVERFS_OAUTH_FEISHU_CLIENT_ID=<app-id>
SERVERFS_OAUTH_FEISHU_CLIENT_SECRET=<app-secret>
```

The flow requests `contact:user.base:readonly`, omits `offline_access`, and authorizes by `open_id`.

### DingTalk

Callback:

```text
https://auth.example.com/oauth/provider/dingtalk/callback
```

```text
SERVERFS_OAUTH_DINGTALK_ENABLED=true
SERVERFS_OAUTH_DINGTALK_CLIENT_ID=<client-id>
SERVERFS_OAUTH_DINGTALK_CLIENT_SECRET=<client-secret>
```

The flow requests `openid`, consumes DingTalk's `authCode` callback field, and authorizes by `unionId`.

### WeChat Open Platform

This is **WeChat Open Platform Website Application QR login**, not WeCom/企业微信 and not Official Account webpage authorization.

Callback:

```text
https://auth.example.com/oauth/provider/wechat/callback
```

```text
SERVERFS_OAUTH_WECHAT_ENABLED=true
SERVERFS_OAUTH_WECHAT_CLIENT_ID=<app-id>
SERVERFS_OAUTH_WECHAT_CLIENT_SECRET=<app-secret>
```

The flow uses `snsapi_login`, verifies user-info `openid` against the token subject, and authorizes by Open Platform `unionid`.

Provider access/refresh tokens remain provider-local ephemeral material; ServerFS issues its own opaque resource-bound credentials after admission.

## Client registration

ServerFS supports three client-identification paths:

1. **Pre-registration** through the optional read-only `SERVERFS_AUTH_CLIENTS_FILE`.
2. **CIMD (Client ID Metadata Documents)** — preferred for standards-current clients; fetched with strict HTTPS, public-address and TLS checks.
3. **DCR (Dynamic Client Registration)** — compatibility-only and disabled by default. Enable only for clients that require it:

```text
SERVERFS_AUTH_DCR_ENABLED=true
```

Public clients use Authorization Code + PKCE S256. Prefer CIMD or explicit pre-registration over DCR when possible.

## Token and resource model

ServerFS issues opaque access/refresh tokens and stores only digests, not raw credential values.

- tokens are bound to `SERVERFS_OAUTH_RESOURCE_URL`;
- private introspection validates issuer, resource, client, subject, scopes and expiry;
- provider-native tokens cannot authenticate directly to MCP;
- authorization codes/provider state are single-use;
- refresh tokens rotate and replay fails closed;
- OAuth scope defaults to `serverfs:access`; workdir read/write/Agent policy remains separate.

## OpenAI Secure MCP Tunnel

OAuth-disabled Tunnel operation remains supported with the repository-tested `tunnel-client v0.0.17` baseline. This is the tested default, not a compatibility lock.

If `serverfs-auth` uses a separate origin, explicitly trust that exact origin:

```text
MCP_OAUTH_TRUSTED_ORIGINS=https://auth.example.com
```

### Known upstream limitation

As verified on 2026-10-19, OpenAI tunnel-client issue #78 remains open: ChatGPT may replace the canonical RFC 9728 `resource` with its hosted Tunnel resource during authorization.

ServerFS does **not** weaken audience/resource validation, accept wildcard resources, or hard-code undocumented OpenAI gateway hosts to work around this.

Therefore direct/generic OAuth and OAuth-disabled Tunnel are supported ServerFS paths; OAuth-through-Tunnel still requires live acceptance for the OpenAI behavior in use and may be blocked by the upstream resource rewrite.

## Proxy routing

Only `serverfs-auth` needs provider/CIMD egress:

```text
SERVERFS_AUTH_USE_PROXY=true
SERVERFS_PROXY_HOST=proxy.example.internal
SERVERFS_PROXY_PORT=7890
```

This is independent from Tunnel, Agent and Jev proxy switches.

## Backup and restore

`SERVERFS_AUTH_STATE_HOST_DIR` contains SQLite authorization state. Treat backups as security-sensitive even though raw tokens are not persisted.

For a consistent backup:

1. stop only `serverfs-auth` during a maintenance window;
2. copy the whole auth-state directory with permissions preserved;
3. start `serverfs-auth`;
4. verify `/healthz`.

Restore only while `serverfs-auth` is stopped; restore the state directory as a unit rather than mixing SQLite files from different snapshots.

## Revocation and secret rotation

- `/revoke` revokes ServerFS-issued credentials when the client has the credential.
- Removing a principal prevents future admission but does not itself revoke already issued access tokens.
- Provider secret rotation requires recreating/restarting `serverfs-auth` only.
- `SERVERFS_OAUTH_INTROSPECTION_SECRET` is shared only by `serverfs-mcp` and `serverfs-auth`; rotate it atomically across both services. A mismatch fails closed.
- Emergency instance-wide invalidation can replace the OAuth state directory while `serverfs-auth` is stopped. This intentionally invalidates all issued ServerFS credentials and loses dynamic registration/state, so it is a deliberate recovery action.

## Troubleshooting

**401 before login** — expected when OAuth is enabled without a valid ServerFS bearer. Verify the protected-resource metadata and canonical `SERVERFS_OAUTH_RESOURCE_URL`.

**Auth server will not start** — verify at least one provider is enabled with credentials, public/issuer URLs match, HTTPS is used outside loopback, the introspection secret is set, and the state directory is writable.

**Login succeeds but access is denied** — the stable provider subject is not in the exact allowlist. Do not substitute email/display name.

**Resource mismatch** — fix the canonical external URL/client request; do not broaden the audience. For Tunnel, also check the known resource-rewrite limitation above.

**Provider timeout** — enable `SERVERFS_AUTH_USE_PROXY` when required; do not give `serverfs-mcp` general Internet egress.

**Tunnel cannot discover a separate auth origin** — set `MCP_OAUTH_TRUSTED_ORIGINS` to the exact authorization-server origin, never a wildcard.

## Native platforms

Windows and macOS native stdio continue to require **no OAuth configuration**. v0.16 shared OAuth modules are import-safe on both platforms, but OAuth deployment itself remains the Linux remote-HTTP shape.

The repository's canonical operator contract is [`docs/oauth.md`](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/oauth.md). The versioned environment-variable surface remains [`.env.example`](https://github.com/NTLx/ServerFS_MCP/blob/main/.env.example).
