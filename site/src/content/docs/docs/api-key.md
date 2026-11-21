---
title: Static API Key
description: Protect an HTTP Core with one shared bearer credential.
---

Choose API Key for one operator or a small set of trusted clients. It admits holders of a shared secret, not individual users. Workdir authorization is unchanged. Use [OAuth](../oauth/) when you need federated identity or individually revocable credentials.

## Configure

Set a unique high-entropy secret per Core. Linux uses `SERVERFS_HTTP_API_KEY` in its private env; macOS named HTTP uses a private staging input; Windows initial configuration generates a protected key. Follow the selected platform runbook. API Key and OAuth are mutually exclusive. Native stdio needs neither.

Public access requires HTTPS and exact Host/Origin admission. Connector credentials are separate from caller credentials; the named OpenAI HTTP Connector also needs the Core's bearer header.

## Connect

Preferred header:

```http
Authorization: Bearer <key>
```

Compatibility for one-line URL clients:

```text
https://mcp.example.com/mcp?key=<key>
```

Send exactly one credential source. Missing, wrong, empty, duplicate or mixed credentials return HTTP 401, not an OAuth login flow. Prefer headers: a URL key can leak through reverse proxy logs, monitoring or browser history. Do not publish real secrets.

## Verify and maintain

Check no-key/wrong-key denial, then authenticated MCP initialize, expected tools, safe file access and relevant denied operations. Public ingress requires separate remote verification.

There is one active key, without a grace set. Linux rotation requires recreating the selected Core and updating its clients and any affected OpenAI bearer file/Connector. Named native staging is create-only; use a deliberate migration or reprovision plan, not an in-place private-manifest edit.

See the [API Key operator guide](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/api-key.md) for exact platform routing, credential handling and rotation.
