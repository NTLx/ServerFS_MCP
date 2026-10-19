# Static API Key authentication

ServerFS v0.17.0 adds an optional static API Key for remote Streamable HTTP deployments that need a simpler built-in authentication boundary than OAuth 2.1.

The feature is intentionally instance-level and minimal: one ServerFS instance accepts one shared bearer secret. Existing workdir authorization remains unchanged.

## When to use it

Static API Key authentication is a good fit for:

- one operator connecting a small number of trusted MCP clients;
- private/self-hosted deployments that still want an application-level secret;
- MCP hosts that accept either a Bearer header or a complete one-line URL such as DingTalk-style `?key=` configuration.

Prefer OAuth 2.1 when you need independently identified users, browser login, revocation per user/client, refresh tokens, or federated identity admission.

## Configuration

Set one high-entropy secret in `.env`:

```dotenv
SERVERFS_HTTP_API_KEY=<long-random-secret>
```

Empty or unset means disabled.

API Key authentication and the built-in OAuth Resource Server are mutually exclusive. ServerFS fails at startup if both are enabled:

```dotenv
SERVERFS_HTTP_API_KEY=<secret>
SERVERFS_OAUTH_ENABLED=true
```

Native Windows/macOS stdio does not use this HTTP credential.

## Client authentication

### Recommended: Authorization header

Send:

```http
Authorization: Bearer <key>
```

This is the preferred form because ordinary HTTP infrastructure is less likely to persist an Authorization header than a credential embedded in a URL.

### Compatibility: query parameter

ServerFS also accepts exactly one `key` query parameter:

```text
https://mcp.example.com/mcp?key=<key>
```

This is useful for MCP products that distribute or accept a complete connection URL.

The query form has an important operational risk: reverse proxies, WAFs, browser history, monitoring systems or other infrastructure may record the full URL. ServerFS disables its own default Uvicorn access log while API Key mode is active, but it cannot control logs produced before the request reaches ServerFS.

If you use `?key=`, configure upstream infrastructure so the query string is not persisted. Prefer the Authorization header whenever the client supports custom headers.

## Credential rules

ServerFS accepts one credential source per request:

- one `Authorization: Bearer ...` header; or
- one `?key=...` query parameter.

It rejects:

- missing credentials;
- incorrect credentials;
- empty Bearer/query values;
- duplicate Authorization headers;
- duplicate `key` query parameters;
- requests that provide both a Bearer header and `?key=`.

Credential failures are rejected at the HTTP boundary before MCP dispatch. API Key mode does not publish OAuth Protected Resource Metadata or turn failures into an OAuth login flow.

## Security properties

The API Key proves only that the caller possesses the shared instance secret. It does not create a ServerFS user identity and does not replace existing authorization controls.

After authentication, all existing ServerFS policy remains in force, including:

- configured workdir boundaries;
- read-only defaults;
- hidden/credential-path denial;
- revision-guarded mutations;
- binary-transfer gates and limits;
- Agent Bridge enablement and runtime allowlists;
- platform-specific filesystem containment.

ServerFS compares the candidate secret with a constant-time comparison and does not intentionally emit configured or received API Key values in its startup/authentication responses.

## HTTPS and reverse proxy boundary

A static API Key is a bearer credential. Public deployments must protect it in transit with HTTPS.

ServerFS does not manage domains, DNS or certificates. The normal public shape remains:

```text
MCP client
    |
    | HTTPS
    v
operator-managed reverse proxy / gateway
    |
    | loopback/private HTTP
    v
ServerFS
```

`compose.http.yml` publishes the ServerFS HTTP endpoint on loopback by default. Keep it there unless you deliberately bind a trusted private interface.

## Rotation

v0.17.0 intentionally has no credential database or multi-key grace set.

To rotate the key:

1. generate a new high-entropy secret;
2. replace `SERVERFS_HTTP_API_KEY` in the operator-owned `.env`;
3. recreate/restart the ServerFS process/container;
4. update clients to the new credential.

The old key stops working after the new process starts.

## Relationship to OAuth and external gateways

The three remote HTTP security shapes are:

| Mode | ServerFS setting | Identity model |
| --- | --- | --- |
| no built-in auth | API Key empty, OAuth off | rely on private network, OpenAI Tunnel or external gateway |
| static API Key | `SERVERFS_HTTP_API_KEY=<secret>` | one shared-secret holder |
| OAuth 2.1 | `SERVERFS_OAUTH_ENABLED=true` | federated identities and ServerFS-issued resource tokens |

If your deployment already uses Cloudflare Access, Tailscale policy, an enterprise API gateway or another trusted authentication layer, you may continue using that boundary instead of enabling ServerFS API Key or OAuth.

## Example

With the generic HTTP Compose shape:

```bash
docker compose \
  -f compose.yml \
  -f compose.http.yml \
  up -d serverfs-mcp
```

recommended client configuration is conceptually:

```text
URL: https://mcp.example.com/mcp
Authorization: Bearer <key>
```

or, for a client that only accepts a complete URL:

```text
https://mcp.example.com/mcp?key=<key>
```

Never commit the real key to Git, documentation, screenshots or client configuration templates.
