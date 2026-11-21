---
title: OAuth 2.1
description: Admit individually identified MCP callers using a separate authorization service.
---

OAuth is an alternative to [Static API Key](../api-key/). It establishes who may enter an HTTP Core; workdir and Bridge policy still decide what they may do. Local stdio needs neither.

## Choose the deployment

The packaged authorization service, `serverfs-auth`, runs with Linux Docker Compose. It owns identity-provider credentials, OAuth state and explicit admission. The Core validates ServerFS-issued resource-bound tokens through private introspection.

Native HTTP Cores can use an independently configured auth service; native installers do not deploy that service. Windows initial configuration is bearer-only, so use its manual path for OAuth. The named OpenAI HTTP Connector requires static Core bearer admission and is not the OAuth path.

## Set up admission

Follow the [OAuth operator guide](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/oauth.md) for exact env, state preparation, scoped Compose commands and native configuration.

1. Select the exact Core and canonical public HTTPS MCP resource URL.
2. Prepare a separate HTTPS authorization origin and private auth-state directory. The issuer must equal the auth public URL. Keep the introspection endpoint on a trusted private path.
3. Select the provider, register its callback and configure the actual credentials and stable-principal allowlist.
4. Start the auth service, verify readiness, then start/activate the Core. Retain existing authorized component choices; OAuth does not require a Tunnel or Bridge.

| Provider | Allowlisted principal |
| --- | --- |
| GitHub | `github:<numeric-id>` |
| DingTalk | `dingtalk:<unionId>` |
| Feishu/Lark | `feishu:<open_id>` |
| WeChat Open Platform Website Application | `wechat:<unionid>` |

Admission is default-deny; use exact stable subjects, not display names or email. Complete real provider login before public use. Prefer CIMD or explicit public-client pre-registration; DCR is compatibility-only. Authorization Code uses PKCE S256.

## Verify and maintain

Check public TLS, OAuth discovery, unauthenticated challenge, allowed and denied identity login, authenticated MCP and wrong-resource/revoked-token denial. Health alone does not establish acceptance. Never broaden the resource or bypass introspection to make a client work.

Removing a principal blocks new admission but does not revoke existing access/refresh pairs. The authenticated client can revoke its pair at `/revoke`. A provider secret change does not revoke ServerFS tokens. Coordinate introspection-secret rotation across both sides; preserve the whole private state directory for backup/restore. Existing tokens may survive or reappear in restored state. See the operator guide for recovery, proxy configuration and historical OpenAI compatibility limits.
