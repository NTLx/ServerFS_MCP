---
title: Static API Key
description: Protect remote Streamable HTTP with one optional shared API Key.
---

ServerFS v0.17.0 adds **optional static API Key authentication for remote Streamable HTTP**. It is designed for a single operator or a small set of trusted clients that need a simpler built-in boundary than OAuth 2.1.

The API Key proves possession of one shared instance secret. Existing ServerFS workdir policy still decides what the authenticated caller may do.

## Configure one key

Set a high-entropy secret in `.env`:

```text
SERVERFS_HTTP_API_KEY=<long-random-secret>
```

Empty or unset means disabled.

Static API Key authentication and built-in OAuth are mutually exclusive. ServerFS fails at startup if `SERVERFS_HTTP_API_KEY` is non-empty while `SERVERFS_OAUTH_ENABLED=true`.

Native Windows/macOS stdio does not use this HTTP credential.

## Client authentication

Preferred form:

```http
Authorization: Bearer <key>
```

Compatibility form for MCP products that accept a complete one-line URL:

```text
https://mcp.example.com/mcp?key=<key>
```

ServerFS accepts exactly one credential source per request. It rejects duplicate Authorization headers, duplicate `key` query parameters, empty credentials and requests that provide both header and query credentials.

API Key failures are handled at the HTTP boundary and do not expose OAuth discovery or trigger an OAuth login flow.

## Query-string warning

The `?key=` form is convenient but easier to leak operationally. Reverse proxies, WAFs, browser history and monitoring systems may persist the full URL.

ServerFS disables its own default Uvicorn access log while API Key mode is active, so the supported ServerFS runner does not print the secret-bearing query URL. It cannot sanitize logs produced by infrastructure before the request reaches ServerFS.

Prefer `Authorization: Bearer` whenever the client supports custom headers. If you must use `?key=`, configure upstream access logging so the query string is not stored.

## HTTPS and deployment boundary

A static API Key is a bearer credential. Public deployment still requires HTTPS.

ServerFS does not manage domains, DNS or TLS certificates. The recommended public shape remains:

```text
MCP client
   |
   | HTTPS
   v
reverse proxy / gateway
   |
   | loopback/private HTTP
   v
ServerFS
```

`compose.http.yml` publishes the MCP endpoint on loopback by default.

## Existing authorization still applies

The key does not bypass:

- configured workdirs;
- read-only defaults;
- hidden/credential-path denial;
- revision-guarded mutations;
- binary-transfer limits;
- Agent Bridge enablement and runtime allowlists;
- platform-specific filesystem containment.

## Rotation

v0.17.0 intentionally supports one active key rather than a credential database.

To rotate it:

1. generate a new high-entropy secret;
2. replace `SERVERFS_HTTP_API_KEY` in `.env`;
3. recreate/restart ServerFS;
4. update clients.

The previous key stops working when the new process starts.

## Choose the right boundary

| Mode | Configuration | Identity model |
| --- | --- | --- |
| no built-in auth | API Key empty, OAuth off | private network, OpenAI Tunnel or external gateway |
| static API Key | `SERVERFS_HTTP_API_KEY=<secret>` | one shared-secret holder |
| OAuth 2.1 | `SERVERFS_OAUTH_ENABLED=true` | federated identities and ServerFS resource tokens |

Use [OAuth 2.1](../oauth/) when you need user identity, browser login or independently revocable credentials.
