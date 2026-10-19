# ServerFS v0.16.0 Development Plan

Status: in progress — Phase 0 through Phase E complete.

Plan frozen: 2026-10-19.

## Implementation checkpoint — 2026-10-19

Completed and verified so far:

- Phase 0 repository baseline was clean and synchronized with `origin/main` before product changes;
- the frozen v0.16 plan was committed separately at `1d9ee4720b819c93f58f71686e3e462b01599250`;
- pre-change Linux baseline is green: `git diff --check`, Ruff lint/format, full root pytest (`1419 passed, 317 skipped`), base Compose rendering and generic HTTP Compose rendering;
- Phase A adds strict default-off MCP Resource Server OAuth settings without changing default v0.15 behavior;
- upstream provider credentials live in a separate `AuthServiceSettings` / `auth_config.py` boundary so they never need to enter the `serverfs-mcp` process;
- GitHub, DingTalk, Feishu and WeChat provider switches are independently opt-in and strict;
- `serverfs-auth` configuration is default-deny, validates public HTTPS URLs outside loopback, exact provider-qualified principals, token TTLs, state path and shared proxy reuse;
- `.env.example` now documents the optional OAuth configuration surface while keeping `SERVERFS_OAUTH_ENABLED=false` by default;
- Phase A focused validation is green: `git diff --check`, 89 config/main tests, Ruff lint and Ruff format checks;
- Phase B adds an OAuth-only MCP Resource Server boundary using the installed `mcp==2.3.0` `AuthSettings` / `TokenVerifier` APIs rather than reimplementing protocol middleware;
- `OAuthIntrospectionVerifier` performs bounded private opaque-token introspection with process proxy bypass, strict issuer/resource/subject/client/scope/expiry checks, and sanitized failure logging;
- OAuth-enabled Streamable HTTP publishes RFC 9728 Protected Resource Metadata and challenges unauthenticated clients while `validate_token_resource=True` enforces the configured MCP resource;
- OAuth-disabled `create_server()` still constructs no auth settings/verifier and preserves the v0.15 path;
- Phase B focused validation is green: 124 OAuth/config/main/security tests, Ruff lint/format and `git diff --check`;
- Phase C adds the isolated `serverfs-auth` Authorization Server core with a user-owned SQLite state store and a dedicated `serverfs-auth` console entry point;
- OAuth clients, pending login transactions, authorization codes and opaque access/refresh tokens persist across auth-service restarts; authorization codes and login transactions are single-use and refresh tokens rotate as one paired access/refresh credential family;
- raw authorization codes, access tokens and refresh tokens are never stored in SQLite: only SHA-256 digests are persisted, while state/DB permissions are tightened to 0700/0600 where the platform supports POSIX modes;
- access/refresh revocation invalidates the credential pair, internal introspection is authenticated and body-bounded, and the Resource Server receives only the minimal active-token claims it needs;
- Authorization Code + PKCE S256, refresh, RFC 9207 `iss`, metadata and token endpoints use the MCP SDK where its behavior matches the ServerFS contract; the public-client revocation endpoint is narrowly owned by ServerFS because the installed SDK revocation request model incorrectly requires a `client_secret` field even for `token_endpoint_auth_method=none`;
- issuer canonicalization is routed through the installed SDK `AuthSettings` model so Authorization Server Metadata and authorization-response `iss` use the same exact issuer string;
- real upstream identity providers are still unavailable in Phase C by design: production `/login` fails closed with 503, while deterministic identity completion exists only as an explicit in-process test injection and cannot be enabled from `.env`;
- Phase C focused validation is green: 62 auth-config/auth-server/resource-server tests, Ruff lint/format, `uv lock --check` and `git diff --check`;
- Phase D implements three explicit client-identification paths: operator pre-registration from an optional read-only JSON file, standards-current CIMD resolution, and DCR only when `SERVERFS_AUTH_DCR_ENABLED=true`;
- ServerFS advertises `client_id_metadata_document_supported=true` and `authorization_response_iss_parameter_supported=true`; DCR's registration endpoint is absent from metadata unless the compatibility switch is enabled;
- CIMD follows the current MCP/Tier-1-client-compatible strict URL subset: HTTPS, non-root path, no query/fragment/userinfo/dot segments, exact `client_id` document match and public PKCE clients only;
- the CIMD fetcher does not use proxies or redirects, resolves DNS before connecting, rejects the entire answer set if any address is non-global/special-use, and pins the TLS TCP connection to a previously checked IP while keeping certificate/SNI verification on the original hostname;
- successful CIMD metadata is held only in a bounded in-memory cache; fetch errors and invalid documents are never cached or persisted to SQLite;
- pre-registered and DCR clients are intentionally public PKCE clients in v0.16, avoiding persistent client-secret material; unsupported confidential/shared-secret client registrations fail closed;
- Phase D focused validation is green: 77 client-registration/auth tests, Ruff lint/format and `git diff --check`;
- Phase E adds one canonical `(provider, subject)` principal representation and a default-deny exact-match `AdmissionPolicy`; wildcard admission remains unsupported;
- routine admission audit events contain only the provider plus a stable 16-hex SHA-256-derived principal identifier, never the upstream subject itself;
- admission is now enforced after deterministic identity proof and before ServerFS authorization-code issuance, so an authenticated but unlisted identity receives `access_denied` and cannot obtain ServerFS credentials;
- the empty allowlist remains a valid fail-closed configuration rather than an implicit allow-all mode;
- Phase E focused validation is green: 56 policy/config/auth-server tests, Ruff lint/format and `git diff --check`.

Phase F1 is the next implementation target. No real upstream provider adapter has been added yet.

## Goal

Add **standards-compliant, opt-in OAuth 2.1 authentication for remote MCP access** while preserving ServerFS's existing least-privilege filesystem model, Linux Docker isolation, standards-first MCP architecture, native stdio behavior, and OpenAI Secure MCP Tunnel compatibility.

v0.16.0 adds OAuth as an **optional security capability**, not as a new mandatory deployment model.

The target architecture is:

```text
                                        Internet
                                           |
                    +----------------------+----------------------+
                    |                      |                      |
                  GitHub                DingTalk              Feishu / WeChat
                    ^                      ^                      ^
                    |                      |                      |
                    +-------------- serverfs-auth ----------------+
                                     OAuth 2.1 AS
                                  identity federation
                                  admission policy
                                  opaque token store
                                           |
                              internal introspection only
                                           |
                                           v
MCP client / OpenAI Tunnel  --->  serverfs-mcp Resource Server
                                    |             |
                                    |             +--> Agent Bridge (optional)
                                    v
                               ServerFS Core
                          workdirs / file policies
```

The identity and authorization layers are deliberately separated:

```text
upstream provider
  proves: "who are you?"
        |
        v
serverfs-auth admission policy
  decides: "may this identity enter this ServerFS instance?"
        |
        v
existing ServerFS workdir policy
  decides: "what may this admitted caller do?"
```

v0.16.0 does **not** replace the existing workdir authorization model with OAuth scopes or a new RBAC system.

---

## Release definition

ServerFS v0.16.0 introduces standards-compliant OAuth 2.1 authentication for remote MCP access, with ServerFS acting as an MCP Resource Server and an isolated `serverfs-auth` authorization broker federating GitHub, DingTalk, Feishu and WeChat identities.

The feature is **disabled by default** and explicitly enabled from `.env`.

When OAuth is disabled:

- `serverfs-mcp` behaves as v0.15.0;
- stdio behavior is unchanged;
- generic Streamable HTTP behavior is unchanged;
- the OpenAI Secure MCP Tunnel behavior is unchanged;
- no OAuth provider credentials are required;
- no OAuth state database is required;
- no OAuth provider network traffic occurs;
- no OAuth code path may implicitly deny existing authenticated network-boundary deployments.

When OAuth is enabled:

- Streamable HTTP becomes an OAuth-protected MCP Resource Server;
- unauthenticated MCP requests fail closed with standards-compliant OAuth discovery/challenge behavior;
- `serverfs-auth` becomes the Authorization Server / identity broker;
- only explicitly enabled identity providers are exposed;
- only explicitly admitted provider identities can receive ServerFS access tokens;
- `serverfs-mcp` still has no general Internet egress;
- provider traffic is isolated to `serverfs-auth`.

---

## Scope assumptions

### Transport scope

OAuth applies to **remote Streamable HTTP**.

It does not wrap native stdio. Local stdio keeps relying on the current OS/user process security boundary, consistent with the MCP authorization model.

### Provider scope

v0.16.0 targets exactly four upstream identity providers:

1. GitHub OAuth App;
2. DingTalk OAuth / unified login;
3. Feishu/Lark OAuth user identity flow;
4. WeChat Open Platform **Website Application WeChat Login** (`snsapi_login`).

`WeChat` in this release does **not** mean WeCom/企业微信 and does not mean Official Account/公众号 webpage authorization. Those are separate provider types and are out of scope.

### Authorization scope

v0.16.0 implements an **instance admission allowlist**, not per-user workdir RBAC.

A principal is represented internally as a stable tuple:

```text
(provider, subject)
```

Examples are conceptually:

```text
github:<stable-provider-user-id>
dingtalk:<stable-provider-user-id>
feishu:<stable-provider-user-id>
wechat:<stable-provider-user-id>
```

The exact stable provider field for DingTalk/Feishu/WeChat must be frozen only after checking the current official provider documentation and a real authorization response during the corresponding provider phase. Mutable display names, usernames, phone numbers and email addresses must never be authorization keys.

Default admission policy is **deny**.

---

## Non-negotiable invariants

v0.16.0 must preserve all v0.15.0 security invariants plus the following OAuth-specific invariants.

### Existing ServerFS invariants

- explicit configured workdirs only;
- host paths remain hidden from MCP callers;
- read-only by default;
- mutation remains per-workdir opt-in;
- no generic shell, arbitrary command execution, recursive delete, force overwrite or unrestricted filesystem access;
- revision-guarded destructive mutations remain unchanged;
- Agent writer-lease semantics remain unchanged;
- binary transfer remains separately opt-in and bounded;
- ChatGPT/OpenAI file ingress remains separately opt-in;
- Agent delegation remains separately opt-in and allowlisted by workdir/runtime;
- DNS-rebinding Host protection remains enabled;
- Windows/macOS remain current-user scoped and non-elevated;
- no flag-day migration is required from v0.15.0.

### OAuth invariants

- OAuth is disabled by default;
- enabling an upstream provider does not itself admit every account from that provider;
- provider access tokens are never accepted as ServerFS bearer tokens;
- provider access/refresh tokens are not stored as long-lived ServerFS credentials;
- ServerFS issues its own resource-bound access tokens;
- bearer tokens are rejected unless their resource/audience is valid for this ServerFS deployment;
- authorization codes are single-use;
- refresh tokens rotate and old refresh tokens cannot be replayed;
- OAuth state is single-use and CSRF-protected;
- MCP authorization code flow requires PKCE S256;
- redirect URIs use exact-match validation;
- authorization failures fail closed;
- auth backend failure never creates an unauthenticated fallback;
- `serverfs-mcp` does not gain general Internet egress;
- provider client secrets are available only to `serverfs-auth`;
- auth database/state is not mounted into `serverfs-mcp`;
- secrets, authorization codes, bearer tokens and provider tokens never appear in normal logs;
- HTTP OAuth endpoints require HTTPS at the public boundary; HTTP is permitted only for explicit loopback/local test fixtures;
- no provider-specific logic enters filesystem tools, Agent tools or workdir policy code.

---

## Protocol and dependency baseline

Start from the released v0.15.0 baseline:

```text
MCP protocol target: 2026-07-28
Python MCP SDK:       mcp==2.3.0
Python:               >=3.12
```

Do **not** upgrade the MCP SDK merely because OAuth work begins.

Before implementation, review whether a newer MCP SDK version materially fixes or adds authorization behavior required by this plan. Upgrade only if a concrete requirement or security fix justifies it; keep an exact pin and review the lockfile delta explicitly.

The installed `mcp==2.3.0` already provides the relevant Resource Server primitives:

- `AuthSettings`;
- `TokenVerifier`;
- `AccessToken` with `resource`, `subject` and claims;
- RFC 9728 Protected Resource Metadata routes;
- bearer-token middleware;
- OAuth authorization-server helper routes/provider interfaces.

However, v0.16.0 must not blindly expose SDK helper behavior as the product contract. ServerFS owns the security policy, storage, provider federation and compatibility tests.

---

## Product layering

v0.16.0 formalizes the following layers:

```text
MCP Host / Client
  ChatGPT / Claude Code / Codex / Qoder / Inspector / other MCP clients
        |
        v
MCP ingress
  - stdio (local, no OAuth)
  - Streamable HTTP (OAuth optional)
  - OpenAI Secure MCP Tunnel -> same Streamable HTTP Resource Server
        |
        v
ServerFS MCP Resource Server
  - OAuth challenge / protected-resource metadata when enabled
  - opaque-token verification through private introspection
  - filesystem tools/resources
  - Agent Bridge tools
        |
        +--------------------------+
        |                          |
        v                          v
ServerFS core                 serverfs-auth
workdir policy                Authorization Server
filesystem backends           provider federation
mutation/revision logic        admission policy
Agent Bridge / leases          OAuth token state
                                   |
                                   v
                         GitHub / DingTalk /
                         Feishu / WeChat
```

`serverfs-auth` belongs to the same repository, package and release as ServerFS. It is a separate process/service boundary, not a separate project.

Suggested package shape:

```text
src/serverfs_mcp/
  auth/
    __init__.py
    server.py
    config.py
    models.py
    store.py
    tokens.py
    policy.py
    client_registration.py
    introspection.py
    providers/
      __init__.py
      base.py
      github.py
      dingtalk.py
      feishu.py
      wechat.py
```

The final names may be simplified during implementation if fewer files are sufficient. Do not create abstraction layers before a second implementation actually needs them.

---

## Optional feature contract

### Master switch

Add one authoritative runtime switch:

```text
SERVERFS_OAUTH_ENABLED=false
```

Rules:

- default is `false`;
- parsing is strict; invalid values fail startup;
- `false` means `serverfs-mcp` is constructed exactly as an unauthenticated v0.15 Resource Server;
- OAuth-only settings must not be required when false;
- OAuth-specific network calls must not occur when false;
- OAuth-specific state directories must not be required when false;
- tests must prove the OAuth-disabled server has the same tool/resource catalog and normal HTTP semantics as v0.15.0.

### Provider switches

Each provider is separately opt-in:

```text
SERVERFS_OAUTH_GITHUB_ENABLED=false
SERVERFS_OAUTH_DINGTALK_ENABLED=false
SERVERFS_OAUTH_FEISHU_ENABLED=false
SERVERFS_OAUTH_WECHAT_ENABLED=false
```

Rules:

- a provider disabled here must not expose a login option or require credentials;
- an enabled provider with incomplete credentials fails `serverfs-auth` startup;
- OAuth enabled with zero usable providers fails closed unless a deliberately configured non-provider client/identity flow exists; v0.16.0 should normally treat this as configuration error;
- provider secrets must not be passed into `serverfs-mcp`.

### Deployment activation

OAuth must remain an **optional Linux Compose shape**.

Preferred implementation:

```text
compose.yml           existing base services
compose.http.yml      existing generic HTTP publication
compose.oauth.yml     OAuth-specific service/network/state wiring
```

`SERVERFS_OAUTH_ENABLED` remains the runtime master gate even when `compose.oauth.yml` is selected.

The deployment documentation must make the two conditions explicit:

```text
OAuth code enabled       -> SERVERFS_OAUTH_ENABLED=true
OAuth service deployed   -> compose.oauth.yml included
```

This deliberate two-gate design avoids silently starting an Internet-capable identity service in historical deployments merely because the repository was upgraded.

Do not make existing `docker compose up -d` deployments unexpectedly acquire a new public/auth service.

---

## Proposed `.env` configuration surface

The implementation should minimize knobs. Add only settings with a real deployment/security purpose.

### Resource Server

```text
SERVERFS_OAUTH_ENABLED=false
SERVERFS_OAUTH_RESOURCE_URL=
SERVERFS_OAUTH_ISSUER_URL=
SERVERFS_OAUTH_REQUIRED_SCOPES=serverfs:access
SERVERFS_OAUTH_INTROSPECTION_URL=http://serverfs-auth:8082/internal/introspect
SERVERFS_OAUTH_INTROSPECTION_SECRET=
SERVERFS_OAUTH_INTROSPECTION_TIMEOUT_SECONDS=2
```

Rules:

- `RESOURCE_URL` is the externally meaningful canonical MCP resource identifier, normally the public HTTPS MCP URL;
- `ISSUER_URL` is the externally meaningful `serverfs-auth` issuer URL;
- when OAuth is enabled, both public URLs must be HTTPS except explicit loopback test fixtures;
- introspection URL must be private/internal in production;
- introspection secret is required in production and shared only by the two ServerFS services;
- a failed/timeout introspection fails closed;
- scopes remain deliberately small in v0.16.0.

### Authorization Server

```text
SERVERFS_AUTH_HOST=0.0.0.0
SERVERFS_AUTH_PORT=8082
SERVERFS_AUTH_PUBLIC_URL=
SERVERFS_AUTH_STATE_DIR=/var/lib/serverfs-auth
SERVERFS_AUTH_ALLOWED_PRINCIPALS=
SERVERFS_AUTH_ACCESS_TOKEN_TTL_SECONDS=3600
SERVERFS_AUTH_REFRESH_TOKEN_TTL_SECONDS=2592000
SERVERFS_AUTH_DCR_ENABLED=false
SERVERFS_AUTH_USE_PROXY=false
```

Use the existing `SERVERFS_PROXY_HOST`, `SERVERFS_PROXY_PORT`, `SERVERFS_PROXY_USERNAME`, and `SERVERFS_PROXY_PASSWORD` when `SERVERFS_AUTH_USE_PROXY=true`; do not create a second incompatible proxy configuration vocabulary.

The default token lifetimes above are proposed implementation defaults, not protocol requirements. They may be adjusted during implementation if interoperability evidence justifies it, but the release must document the final values.

### Provider credentials

Use explicit provider-prefixed values, for example:

```text
SERVERFS_OAUTH_GITHUB_CLIENT_ID=
SERVERFS_OAUTH_GITHUB_CLIENT_SECRET=

SERVERFS_OAUTH_DINGTALK_CLIENT_ID=
SERVERFS_OAUTH_DINGTALK_CLIENT_SECRET=

SERVERFS_OAUTH_FEISHU_CLIENT_ID=
SERVERFS_OAUTH_FEISHU_CLIENT_SECRET=

SERVERFS_OAUTH_WECHAT_CLIENT_ID=
SERVERFS_OAUTH_WECHAT_CLIENT_SECRET=
```

Exact provider naming (`client_id` vs app key/app secret/app id) should follow the provider's current official terminology where practical, but the product-facing naming should remain predictable.

Provider callback URLs should be derived from `SERVERFS_AUTH_PUBLIC_URL`; avoid separately configurable callback URLs unless a verified provider requires them.

Suggested routes:

```text
/oauth/provider/github/callback
/oauth/provider/dingtalk/callback
/oauth/provider/feishu/callback
/oauth/provider/wechat/callback
```

Separate callback paths reduce provider mix-up risk.

---

## OAuth server model

### ServerFS is not an upstream-token passthrough

The following is forbidden:

```text
GitHub access token ----------------------> ServerFS bearer token
DingTalk user access token ---------------> ServerFS bearer token
Feishu user_access_token -----------------> ServerFS bearer token
WeChat access_token ----------------------> ServerFS bearer token
```

Correct model:

```text
upstream OAuth authorization code
        |
        v
serverfs-auth obtains short-lived provider token
        |
        v
provider identity endpoint
        |
        v
(provider, stable subject)
        |
        v
ServerFS admission policy
        |
        v
ServerFS authorization code
        |
        v
ServerFS access / refresh token
```

Provider tokens exist only for the shortest practical time required to read identity. They are not persisted as ServerFS session credentials and are not returned to MCP clients.

### ServerFS token format

Use **opaque random tokens** for v0.16.0 rather than JWT access tokens.

Reasons:

- immediate revocation;
- immediate admission-policy effect;
- no signing-key/JWKS lifecycle;
- no duplicated signing secret in the MCP process;
- simpler audit surface;
- no demonstrated ServerFS scale requirement that justifies stateless JWT validation.

Token material must have cryptographically strong entropy. Persist only a one-way digest of access tokens, refresh tokens and authorization codes where lookup semantics permit it.

Do not log raw tokens.

### Refresh tokens

- rotate on every successful refresh;
- reject replay of an already-used refresh token;
- revoke the associated token family/session on detected replay where practical;
- do not issue upstream-provider refresh tokens to the MCP client;
- ServerFS refresh tokens represent ServerFS authorization only.

---

## Resource binding

MCP bearer tokens must be bound to a ServerFS resource/audience.

The canonical resource is configured by:

```text
SERVERFS_OAUTH_RESOURCE_URL
```

For direct generic HTTP, a token for another MCP server must always fail.

Example mandatory negative test:

```text
token.resource = https://mcp-a.example/mcp
request target = https://mcp-b.example/mcp
result         = reject
```

### SDK resource validation

Prefer SDK-native `validate_token_resource=True` when the deployment has exactly one canonical resource identifier and the token verifier can return it exactly.

If a verified interoperability path requires more than one exact externally valid resource identifier, do **not** disable audience validation generically. Instead:

1. make the accepted resource set explicit and finite;
2. perform exact validation inside the ServerFS `TokenVerifier`;
3. set SDK `validate_token_resource=False` only because the custom verifier already enforced an equivalent/stronger policy;
4. cover every accepted alias with positive and negative tests;
5. never accept wildcard resource origins.

Do not add resource aliases speculatively.

---

## OpenAI Secure MCP Tunnel compatibility

Current OpenAI Tunnel documentation supports OAuth-protected MCP:

- inbound `Authorization` is forwarded to the MCP origin;
- RFC 9728 protected-resource discovery is forwarded;
- OAuth discovery is routed through the Tunnel/Harpoon machinery;
- a separate Authorization Server origin can be explicitly trusted.

The current ServerFS base Compose pin is `ghcr.io/openai/tunnel-client:v0.0.16`.

When ServerFS OAuth uses a separate auth origin, the Tunnel may require:

```text
MCP_OAUTH_TRUSTED_ORIGINS=https://auth.example.com
```

The exact current tunnel-client configuration must be validated against the pinned version before modifying Compose.

### Known upstream risk

As of plan freeze, OpenAI `tunnel-client` issue #78 remains open: ChatGPT may replace a canonical PRMD `resource` with a hosted tunnel resource during the OAuth authorization flow in some deployments.

Therefore OAuth-through-Tunnel is a dedicated acceptance gate, not an assumed property.

Policy:

- first test the exact pinned v0.0.16 behavior;
- do not weaken ServerFS resource/audience checks to make a broken external flow pass;
- do not hard-code undocumented OpenAI internal gateway hostnames;
- do not add broad resource wildcards;
- if a newer official Tunnel release fixes a reproducible blocker, evaluate and pin that release through the existing controlled Tunnel-version process;
- if the upstream issue remains unresolved, v0.16.0 may release with direct/generic OAuth complete while documenting OAuth-over-Tunnel as a known upstream limitation, provided OAuth-disabled Tunnel behavior remains fully green.

OAuth-disabled OpenAI Tunnel regression remains mandatory regardless.

---

## Authorization Server endpoints

`serverfs-auth` should expose the minimum standards surface needed by supported MCP clients.

Expected public endpoints include:

```text
/.well-known/oauth-authorization-server
/authorize
/token
/revoke
```

Protected Resource Metadata remains owned by the MCP Resource Server, normally at the RFC 9728 path derived from the MCP resource URL.

If `/register` is exposed, it is only the DCR compatibility path and only when explicitly enabled.

Provider login/callback routes live under a distinct namespace such as `/oauth/provider/...` and are not MCP protocol endpoints.

A minimal server-rendered provider-selection page is sufficient. Do not introduce React/Vue or a frontend build system for four buttons and error messages.

---

## MCP client registration strategy

MCP 2026-07-28 prefers modern client identification/registration and treats legacy DCR as compatibility behavior.

v0.16.0 targets three modes:

1. **pre-registered clients** — always supported when configured;
2. **Client ID Metadata Documents (CIMD)** — preferred dynamic/modern path;
3. **Dynamic Client Registration (DCR)** — compatibility fallback, opt-in.

### CIMD

When `client_id` is an HTTPS metadata-document URL:

- fetch only over HTTPS except explicit loopback test fixtures;
- the document URL and returned `client_id` must match exactly as required by the applicable MCP/OAuth profile;
- redirect URIs must exact-match metadata;
- bound response size;
- bound connect/read timeouts;
- reject credentials embedded in URL;
- reject redirects that leave the validated origin unless explicitly required by the governing specification and safely revalidated;
- protect against SSRF after DNS resolution;
- reject loopback, link-local, private, multicast and otherwise non-public destinations by default for remote CIMD fetches;
- re-check resolved addresses on redirects;
- cache successful metadata for a bounded duration;
- never treat fetched content as ServerFS configuration or instructions.

CIMD support is an outbound-fetch attack surface and requires dedicated SSRF tests.

### DCR

Proposed default:

```text
SERVERFS_AUTH_DCR_ENABLED=false
```

Enable only for clients that still require it.

When enabled:

- enforce exact redirect URI rules;
- limit metadata size/counts;
- avoid unbounded client creation;
- apply reasonable client-registration rate limits;
- persist only necessary client metadata;
- never silently elevate DCR clients to confidential clients.

Interoperability testing may justify changing the final default before release, but such a change requires explicit rationale in this plan/checkpoint.

---

## Admission policy

### Minimal v0.16 policy

Use a strict allowlist keyed by `(provider, subject)`.

Initial environment representation:

```text
SERVERFS_AUTH_ALLOWED_PRINCIPALS=github:12345678,feishu:...,dingtalk:...,wechat:...
```

Parser requirements:

- exact provider names;
- no duplicate principals;
- no wildcard provider;
- no wildcard subject;
- malformed entries fail startup;
- empty allowlist means nobody is admitted;
- display name/email/phone values are never accepted as substitutes for provider subject IDs.

The environment representation is intentionally simple for a single-operator/small-team deployment. A separate policy file or database-managed administration UI is out of scope unless implementation proves the environment form unworkable.

### Authorization semantics

Successful upstream login alone is insufficient.

Flow:

```text
provider login succeeded
        |
        v
stable principal resolved
        |
        +-- not allowlisted --> access denied; no ServerFS auth code/token
        |
        +-- allowlisted -----> continue ServerFS OAuth authorization
```

### Scopes

v0.16.0 uses one application scope by default:

```text
serverfs:access
```

Do not duplicate the workdir policy into OAuth scopes such as `serverfs:read`, `serverfs:write`, `serverfs:agent`, etc. in this release.

Future multi-user authorization may add scopes/claims deliberately, but that is not a prerequisite for secure login.

---

## Provider adapter contract

Provider-specific logic must terminate at a narrow interface.

Conceptual result:

```text
ProviderIdentity {
    provider: "github" | "dingtalk" | "feishu" | "wechat"
    subject: stable opaque string
    display_name: optional presentation-only string
}
```

The authorization policy consumes only `provider` and `subject`.

A provider adapter is responsible for:

1. building the upstream authorization URL;
2. validating callback `state` and provider binding;
3. exchanging the provider authorization code;
4. using PKCE upstream when the provider supports/recommends it;
5. retrieving the minimal identity record;
6. mapping the documented stable identifier to `subject`;
7. discarding provider token material;
8. returning a normalized identity.

Adapters must not return provider API clients into the rest of ServerFS.

### GitHub

Implementation target:

- GitHub OAuth App web authorization flow;
- `state` required;
- PKCE S256 where supported by the current GitHub OAuth App flow;
- minimal identity scope;
- stable immutable numeric user ID as the authorization subject, not username/email;
- no repository scope simply for login.

GitHub is implemented first because it is the cleanest reference provider for the broker abstraction.

### Feishu/Lark

- use the current official user authorization/token flow at implementation time;
- do not reuse obsolete endpoint examples from historical SDKs/blogs;
- resolve one stable user identity field documented for the selected app type;
- request only identity scopes needed for login;
- keep the adapter isolated because Feishu auth API generations have changed over time.

### DingTalk

- use the current official unified OAuth/login capability;
- validate the selected stable identity field against current official docs/live response;
- request only identity scopes;
- keep DingTalk token-exchange peculiarities inside the provider adapter.

### WeChat

- target WeChat Open Platform Website Application login;
- use the `snsapi_login` website QR-login flow;
- `state` is mandatory;
- select the stable subject field from current official docs/live response for the configured Open Platform application;
- do not conflate WeChat Open Platform, Official Account and WeCom identities.

Provider endpoint constants and field mappings are frozen only during each provider implementation phase after current official documentation is rechecked.

---

## Auth state storage

Use SQLite for v0.16.0.

Rationale:

- single-node ServerFS deployment;
- no demonstrated need for Redis/PostgreSQL;
- transactional single-use code/refresh semantics;
- simple backup and migration story;
- minimal operational dependency surface.

Persist under an OAuth-specific state directory mounted only into `serverfs-auth`.

Suggested state:

```text
OAuth client registrations / cached CIMD metadata
login transactions
provider-state correlation
ServerFS authorization codes
ServerFS access token digests
ServerFS refresh token digests
principal/session linkage
revocations / refresh-token-family state
```

Provider access tokens and provider refresh tokens are not durable state for the ServerFS login-only use case.

### Storage security

- SQLite file created with user-only permissions;
- directory writable only by the non-root `serverfs-auth` container user;
- no state mount in `serverfs-mcp`;
- schema migration is explicit and repeatable;
- migrations run before serving traffic and fail closed;
- tests cover database restart persistence, single-use code semantics and refresh rotation.

---

## Private introspection boundary

`serverfs-mcp` validates opaque ServerFS access tokens through `serverfs-auth` over a dedicated internal Docker network.

Suggested internal call:

```text
POST /internal/introspect
Authorization: Bearer <SERVERFS_OAUTH_INTROSPECTION_SECRET>
```

The exact response should be minimal, for example conceptually:

```json
{
  "active": true,
  "client_id": "...",
  "subject": "github:12345678",
  "scopes": ["serverfs:access"],
  "resource": "https://mcp.example.com/mcp",
  "expires_at": 1790000000
}
```

Do not expose provider profile data.

Network/config rules:

- use a new internal-only network such as `serverfs_auth_internal`;
- only `serverfs-mcp` and `serverfs-auth` join it;
- introspection port is not published;
- auth provider secrets are not present in the MCP container;
- introspection secret is never logged;
- low bounded timeout;
- no retry storm; at most a tightly bounded retry for an unambiguous connection failure if evidence justifies it;
- auth service unavailable => fail closed.

The token verifier should convert the introspection response into MCP SDK `AccessToken` data while preserving subject/resource/scopes.

---

## Linux Docker architecture

Linux remains Docker/Compose-first.

### Networks

Target shape:

```text
serverfs-mcp:
  - serverfs_internal
  - serverfs_file_ingress
  - serverfs_auth_internal   # only when OAuth overlay is used

serverfs-auth:
  - serverfs_auth_internal
  - oauth_egress

openai-tunnel:
  - serverfs_internal
  - tunnel_egress

serverfs-file-ingress:
  - serverfs_file_ingress
  - file_ingress_egress
```

Security properties:

- `serverfs-mcp` still has no generic egress network;
- `serverfs-auth` has the Internet egress required for upstream OAuth/CIMD;
- file ingress and OAuth egress remain separate;
- Tunnel and auth internal traffic are separated;
- all containers remain non-root;
- `read_only: true` remains where compatible;
- `cap_drop: [ALL]` and `no-new-privileges` remain;
- auth writable state is a narrow dedicated mount, not a writable root filesystem.

### Publication

`serverfs-auth` public port binds loopback by default in the OAuth overlay, analogous to generic MCP HTTP publication.

Expected production shape:

```text
browser / MCP client
       |
       v
operator TLS reverse proxy
   |              |
   |              +--> 127.0.0.1:<auth-publish-port> -> serverfs-auth
   |
   +-----------------> 127.0.0.1:<mcp-publish-port>  -> serverfs-mcp
```

ServerFS does not become a certificate-management product.

The reverse proxy must preserve the public URL semantics used in OAuth metadata and callbacks.

---

## Logging and observability

Add structured events that are useful without leaking authentication material.

Examples:

```text
oauth_enabled
auth_server_started
provider_login_started
provider_login_succeeded
provider_login_denied
serverfs_authorization_code_issued
serverfs_token_issued
serverfs_token_refreshed
serverfs_token_revoked
token_introspection_failed
cimd_fetch_rejected
```

Safe identifiers:

- provider name;
- hashed/truncated correlation ID;
- reason code;
- HTTP status category;
- client ID only where it is already public/non-secret and safe.

Never log:

- access tokens;
- refresh tokens;
- provider tokens;
- authorization codes;
- client secrets;
- introspection secret;
- OAuth state/PKCE verifier;
- complete upstream responses containing identity/private data.

Startup logs should report only configuration posture, for example:

```text
oauth_enabled=true
provider_count=2
dcr_enabled=false
admission_principal_count=3
```

Do not log the principal allowlist contents.

---

## Security negative-test matrix

The following are release blockers when applicable.

### Resource Server

```text
OAuth disabled + historical valid MCP request          -> succeeds unchanged
OAuth enabled + no Authorization                       -> 401 + OAuth discovery challenge
OAuth enabled + malformed bearer                       -> 401
expired ServerFS token                                 -> 401
revoked ServerFS token                                 -> 401
provider-native access token                           -> 401
ServerFS token for wrong resource                      -> 401
ServerFS token missing required scope                  -> 403/standards-correct denial
introspection secret wrong                             -> internal request denied
introspection backend unavailable                      -> fail closed
```

### Authorization code / token flow

```text
wrong OAuth state                                      -> deny
replayed OAuth state                                   -> deny
wrong PKCE verifier                                    -> deny
PKCE method other than supported policy                -> deny
replayed authorization code                            -> deny
expired authorization code                             -> deny
redirect_uri not exact registered value                -> deny
client mismatch during code exchange                   -> deny
refresh token replay                                   -> deny
expired/revoked refresh token                          -> deny
resource changed during code/token flow                -> deny
scope escalation on refresh                            -> deny
```

### Admission

```text
valid GitHub login, principal not allowlisted          -> no ServerFS code/token
valid DingTalk login, principal not allowlisted        -> no ServerFS code/token
valid Feishu login, principal not allowlisted          -> no ServerFS code/token
valid WeChat login, principal not allowlisted          -> no ServerFS code/token
empty allowlist                                        -> nobody admitted
mutable username/email matches someone else            -> irrelevant; no authorization effect
```

### Provider mix-up

```text
GitHub callback delivered to DingTalk callback         -> deny
provider A state used at provider B callback           -> deny
callback without matching pending login transaction    -> deny
```

### CIMD / SSRF

```text
http:// remote CIMD URL                                -> deny
URL with credentials                                   -> deny
127.0.0.1 / ::1                                        -> deny except explicit test path
RFC1918 / ULA                                           -> deny
link-local / metadata IP                               -> deny
DNS name resolving to denied address                   -> deny
redirect from public host to private IP                -> deny
oversized document                                     -> deny
invalid content type/JSON                              -> deny
client_id/document mismatch                            -> deny
unregistered redirect_uri                              -> deny
```

### Logging

Automated tests should assert representative secrets/tokens do not appear in captured logs on success or failure paths.

---

## Phase 0 — Baseline, threat model and contract freeze

1. synchronize VPS `ServerFS` with `origin/main` and require a clean worktree before implementation;
2. confirm v0.15.0 release baseline and current exact dependency/tunnel pins;
3. commit this `dev_plan_v0.16.md` before product code work;
4. run current Linux root lint/tests and Compose rendering to establish baseline evidence;
5. document the OAuth threat model:
   - bearer-token theft;
   - CSRF/state confusion;
   - authorization-code replay;
   - refresh-token replay;
   - provider mix-up;
   - redirect URI attacks;
   - token audience/resource confusion;
   - OAuth confused-deputy/token passthrough;
   - CIMD SSRF/DNS rebinding;
   - provider token leakage;
   - admission bypass;
6. recheck MCP 2026-07-28 authorization and client-registration requirements against current official specification;
7. inspect the exact installed MCP SDK APIs rather than coding from newer `main` branch examples;
8. verify pinned OpenAI Tunnel v0.0.16 OAuth behavior/documentation and record any blocker before Tunnel-specific implementation.

Success criterion: architecture/security contracts are frozen before adding endpoints.

---

## Phase A — OAuth configuration model and disabled-path compatibility

Implement strict configuration parsing first, with no network/auth behavior yet.

1. add `SERVERFS_OAUTH_ENABLED=false`;
2. add OAuth Resource Server settings;
3. add `serverfs-auth` settings;
4. add four provider enable switches and credential settings;
5. reuse the existing ServerFS proxy configuration through `SERVERFS_AUTH_USE_PROXY`;
6. validate required setting dependencies only when OAuth/provider is enabled;
7. reject malformed URLs, invalid TTLs, empty required secrets and invalid allowlist entries;
8. add sanitized startup posture logging;
9. prove OAuth-disabled `Settings` defaults preserve v0.15 behavior;
10. update `.env.example` only after names are covered by tests.

Success criterion: adding the new code/configuration does not alter a default v0.15 deployment.

Suggested checkpoint: `v0.16 Phase A: optional OAuth configuration`.

---

## Phase B — Resource Server authentication boundary

Implement the MCP-side OAuth Resource Server without provider logic.

1. create a narrow `TokenVerifier` implementation;
2. add internal introspection client with strict timeout and sanitized errors;
3. conditionally construct `MCPServer(..., auth=..., token_verifier=...)` only when OAuth is enabled;
4. expose RFC 9728 Protected Resource Metadata through SDK-supported behavior;
5. configure required scope `serverfs:access`;
6. enforce exact resource/audience policy;
7. ensure OAuth-disabled server does not instantiate auth verifier/network client;
8. add 401/WWW-Authenticate/discovery tests;
9. add wrong-resource, wrong-scope, expired/revoked-token tests using a fake introspector;
10. verify tools/resources remain identical after successful authorization.

No provider or database code belongs in this phase.

Success criterion: a fake valid ServerFS token can authorize MCP; every invalid-token class fails closed.

---

## Phase C — `serverfs-auth` minimal Authorization Server and persistence

Build the ServerFS Authorization Server independently of upstream providers.

1. add the `serverfs-auth` CLI entry point;
2. implement SQLite initialization/migrations;
3. implement authorization-code state;
4. implement opaque access tokens;
5. implement rotating refresh tokens;
6. implement revocation;
7. implement internal introspection;
8. add PKCE S256 enforcement;
9. exact-match redirect URI validation;
10. bind codes/tokens to client, subject, scope and resource;
11. implement `/authorize`, `/token`, `/revoke` and AS metadata using SDK helpers where their behavior matches the frozen contract;
12. keep provider selection behind an internal abstraction so Phase C can use a deterministic fake identity provider in tests;
13. verify restart persistence and token revocation.

Success criterion: complete ServerFS OAuth code -> token -> MCP introspection flow works locally with a fake identity provider and no Internet dependency.

---

## Phase D — Client registration: pre-registration, CIMD and optional DCR

1. define minimal persisted client model;
2. support explicit pre-registered clients for deterministic tests/operations;
3. implement CIMD resolution and metadata validation;
4. implement SSRF-safe HTTP fetcher for CIMD;
5. add bounded cache;
6. implement DCR only behind `SERVERFS_AUTH_DCR_ENABLED`;
7. test public clients + PKCE;
8. test confidential-client authentication only where required/supported;
9. run MCP SDK/Inspector client-registration compatibility tests.

Success criterion: at least one standards-current client can register/identify without DCR, while DCR remains a tested compatibility path.

Do not enable DCR by default merely to make one test easy.

---

## Phase E — Admission policy

1. implement strict `SERVERFS_AUTH_ALLOWED_PRINCIPALS` parsing;
2. normalize principal representation once;
3. apply admission after upstream identity proof and before ServerFS authorization-code issuance;
4. default deny;
5. no wildcard syntax;
6. add audit events without logging subject contents if avoidable; use a stable hash/correlation representation for routine logs;
7. add positive/negative policy tests.

Success criterion: a correctly authenticated but unlisted real/fake provider user cannot obtain ServerFS credentials.

---

## Phase F1 — GitHub provider

Use GitHub as the reference adapter.

1. recheck current official GitHub OAuth App documentation;
2. create provider-specific authorization/callback flow;
3. require `state`;
4. use PKCE S256 if supported by current GitHub flow;
5. request only identity scope required for user identification;
6. retrieve current user identity;
7. map immutable numeric GitHub user ID to subject;
8. discard GitHub token material;
9. test denied and allowlisted users;
10. perform live browser login acceptance against a non-production test OAuth App before release.

Success criterion: GitHub login produces only a ServerFS identity/token and gives ServerFS no repository permission by default.

---

## Phase F2 — Feishu provider

1. recheck current official Feishu/Lark docs and selected app type;
2. freeze current authorization/token/identity endpoints in the adapter;
3. request minimal identity permissions;
4. choose and document stable subject field;
5. implement callback/state/provider binding;
6. discard provider token material;
7. unit test response/error mapping;
8. perform one real live login acceptance.

Success criterion: a Feishu identity maps deterministically to one stable ServerFS principal and admission is enforced.

---

## Phase F3 — DingTalk provider

1. recheck current official DingTalk unified authorization documentation;
2. freeze current endpoint/field contract in the adapter;
3. request minimal identity permissions;
4. choose and document stable subject field;
5. implement callback/state/provider binding;
6. discard provider token material;
7. test provider-specific error/token shapes;
8. perform one real live login acceptance.

Success criterion: DingTalk peculiarities remain isolated to the adapter; no DingTalk concepts leak into ServerFS OAuth core.

---

## Phase F4 — WeChat provider

1. verify current WeChat Open Platform Website Application login documentation/account requirements;
2. implement website QR authorization using the current documented endpoints;
3. require `state`;
4. use `snsapi_login` for website login;
5. choose/document the stable identity field for this Open Platform application model;
6. explicitly reject assumptions from WeCom or Official Account flows;
7. discard provider token material;
8. perform one live website QR-login acceptance if an eligible Open Platform application is available.

If a live WeChat application/account prerequisite prevents end-to-end acceptance, do not fabricate a pass. Complete deterministic contract tests, document the unmet external prerequisite, and decide release readiness explicitly.

---

## Phase G — Linux Compose isolation

Add `compose.oauth.yml` and the minimum base wiring required by it.

1. add `serverfs-auth` service from the same release image/package;
2. run non-root;
3. `read_only: true` root filesystem where compatible;
4. dedicated writable SQLite state mount only;
5. `cap_drop: ALL`;
6. `no-new-privileges:true`;
7. dedicated `serverfs_auth_internal` network;
8. dedicated `oauth_egress` network;
9. `serverfs-mcp` joins auth internal network only under OAuth overlay;
10. do not attach `serverfs-mcp` to `oauth_egress`;
11. loopback-only public auth publication by default;
12. add healthcheck that does not require/emit secrets;
13. render base, generic HTTP, OAuth, OAuth+Agent, OAuth+file-ingress and relevant Tunnel combinations;
14. prove base Compose without OAuth overlay remains unchanged.

Success criterion: packet/network topology preserves the rule "identity service has egress; MCP core does not".

---

## Phase H — Direct Streamable HTTP live E2E

Use the real Linux Docker shape behind a local/test HTTPS reverse proxy where practical.

Mandatory flow:

1. request protected MCP without token;
2. discover Protected Resource Metadata;
3. discover Authorization Server metadata;
4. complete client registration/identification path;
5. complete provider login;
6. exchange code with PKCE;
7. call `initialize`;
8. `tools/list`;
9. call `list_workdirs`;
10. exercise one read and one revision-guarded mutation in a disposable workdir;
11. refresh token;
12. prove old refresh token replay fails;
13. revoke token;
14. prove MCP request fails after revocation;
15. prove wrong resource token fails.

Repeat the protocol-level flow with MCP Python SDK and at least the clients that v0.15 explicitly supports where they support OAuth-protected remote MCP:

- Claude Code;
- Codex;
- Qoder;
- MCP Inspector/conformance tooling where practical.

Do not create client-specific ServerFS behavior to make one client pass.

---

## Phase I — OpenAI Secure MCP Tunnel OAuth acceptance

First preserve historical behavior:

### I1 — OAuth disabled

Run the existing ChatGPT/OpenAI Tunnel acceptance with:

```text
SERVERFS_OAUTH_ENABLED=false
```

Require the v0.15 tool/file/binary/Agent behavior to remain green.

### I2 — OAuth enabled

Against the exact supported/pinned tunnel-client:

1. verify OAuth discovery reaches the MCP server;
2. configure the separate auth origin in Tunnel trust settings if required;
3. verify ChatGPT receives the correct OAuth challenge/metadata;
4. complete provider login;
5. inspect the effective `resource` parameter without logging credentials;
6. verify issued ServerFS token is accepted only for the intended resource;
7. call `list_workdirs` through ChatGPT;
8. test token expiry/revocation where practical.

If OpenAI issue #78 or an equivalent hosted-resource rewrite blocks the flow:

- record evidence;
- check current official Tunnel release/fix status;
- update the exact Tunnel pin only through the existing controlled version policy if a verified fix exists;
- never add wildcard audience acceptance;
- never hard-code undocumented OpenAI internal gateway hosts merely as a workaround.

Success criterion for the ServerFS-owned portion: OAuth-disabled Tunnel remains green and OAuth-enabled behavior is either green or has a precisely documented upstream blocker that does not weaken direct OAuth security.

---

## Phase J — Full security regression

Run the complete negative-test matrix plus:

- HTTP Host/Origin rebinding protections;
- workdir path traversal/symlink protections;
- binary transfer bounds;
- revision conflicts;
- Agent writer leases;
- file-ingress isolation;
- startup configuration failure cases;
- auth DB permission/state corruption behavior;
- provider timeouts/HTTP failures;
- proxy behavior if enabled;
- sensitive-log redaction;
- restart/recovery behavior;
- concurrent authorization-code exchange race;
- concurrent refresh-token replay race.

Add fuzz/property-style tests only where they materially improve parser/security confidence; do not add a framework merely for appearance.

---

## Phase K — Cross-platform synchronization and native regression

Shared/core code is developed first on VPS Linux and synchronized only through the remote GitHub repository.

### Ownership matrix

| Platform | Environment | v0.16 responsibility |
| --- | --- | --- |
| Linux/shared | VPS001 ServerFS / `ServerFS` | OAuth shared code, `serverfs-auth`, Docker Compose, remote HTTP, protocol tests, docs |
| Windows native | WorkPC ServerFS MCP | Windows-native regression after shared checkpoint; no Linux Docker work |
| macOS native | MacBook ServerFS MCP | macOS-native regression after shared/Windows checkpoint; no Linux Docker work |

### Sequence

1. VPS implements shared/Linux work;
2. VPS commits and pushes a clean checkpoint;
3. WorkPC fast-forwards from GitHub and runs Windows-native stdio/filesystem/Agent/lifecycle regression;
4. Windows-specific production changes are made only if a real regression requires them;
5. WorkPC commits/pushes any justified Windows change/evidence;
6. MacBook fast-forwards from GitHub and runs macOS-native stdio/filesystem/Agent/lifecycle regression;
7. macOS-specific production changes are made only if a real regression requires them;
8. MacBook commits/pushes any justified macOS change/evidence;
9. VPS fast-forwards the combined result and performs final Linux/release closeout.

Do not invoke a platform's Agent from another platform's workdir. Each ServerFS plugin/Agent remains bound to its own machine and workdir.

### Native platform scope

OAuth itself is not added to native stdio merely for symmetry.

Windows/macOS v0.16 acceptance proves:

- OAuth additions did not make stdio require OAuth configuration;
- no provider dependency is imported on the disabled/local path;
- current-user/non-admin lifecycle remains unchanged;
- shared package imports work without Linux-only Docker assumptions;
- Agent Bridge behavior remains green.

---

## Phase L — Documentation and site

Update release-facing docs only after implementation behavior is stable.

Required topics:

- OAuth is optional and default-off;
- stdio does not require OAuth;
- generic remote HTTP security recommendation;
- Linux Docker OAuth deployment;
- public TLS/reverse-proxy requirement;
- `serverfs-auth` architecture;
- four provider setup guides;
- stable principal allowlist configuration;
- provider callback URLs;
- token/resource model;
- CIMD vs DCR compatibility;
- OpenAI Tunnel OAuth setup and any verified limitation;
- proxy configuration;
- backup/restore of auth SQLite state;
- revoking access;
- rotating provider secrets and introspection secret;
- troubleshooting discovery/401/resource mismatch;
- Windows/macOS native stdio remains OAuth-free.

Update at minimum:

- `README.md`;
- `.env.example`;
- relevant `docs/` deployment/security pages;
- English/Chinese site content;
- architecture diagrams/text;
- release/version surfaces only at final release stage.

Avoid duplicating long provider instructions in many places; maintain one canonical operator guide and link/index it appropriately.

---

## Phase M — Release gates

Before tagging v0.16.0:

1. worktree clean except intentional release changes;
2. `git diff --check` clean;
3. exact package version `0.16.0`;
4. dependency lock checks pass;
5. Ruff lint/format clean;
6. complete root pytest suite passes;
7. focused OAuth/security tests pass;
8. base Compose renders;
9. generic HTTP Compose renders;
10. OAuth Compose renders;
11. OAuth + Agent overlay renders;
12. OAuth + file-ingress shape renders where supported;
13. OpenAI Tunnel historical shape renders and OAuth-disabled live regression passes;
14. direct OAuth live E2E passes;
15. all four providers have either live acceptance evidence or an explicitly documented external prerequisite/blocker reviewed before release;
16. Windows native regression is green;
17. macOS native regression is green;
18. site build passes;
19. documentation describes v0.16.0 as released before the final release commit is tagged, preserving the project's one-release-commit convention;
20. immutable tag and GitHub Release point at the same final release commit.

---

## Explicit non-goals

v0.16.0 does not:

- make OAuth mandatory;
- add OAuth to local stdio;
- create per-user/per-workdir RBAC;
- replace workdir policy with OAuth scopes;
- accept provider-native tokens at `/mcp`;
- persist provider refresh tokens for future provider API calls;
- grant GitHub repository access merely for login;
- integrate WeCom/企业微信;
- integrate WeChat Official Account webpage authorization;
- add Google/Microsoft/Apple login;
- introduce Auth0/Keycloak/Casdoor/ZITADEL as mandatory dependencies;
- introduce Redis/PostgreSQL;
- introduce JWT/JWKS unless opaque-token introspection proves inadequate;
- introduce a SPA/frontend framework;
- give `serverfs-mcp` general Internet egress;
- disable DNS-rebinding protections;
- weaken resource/audience validation for Tunnel compatibility;
- refactor unrelated filesystem/Agent/platform-native code;
- implement speculative Windows/macOS remote-HTTP deployment features.

---

## Implementation discipline

### Changes stay surgical

OAuth code should be additive around the HTTP ingress/auth boundary. Existing filesystem and Agent code should change only where authenticated principal context is genuinely required.

### Delegate only atomic tasks

VPS AI Agent delegation is optional and should remain rare. If used, delegate only narrow objectives with explicit file/test boundaries. Do not hand the complete v0.16 implementation to an Agent.

Tasks requiring Windows/macOS local runtime validation belong only to the WorkPC/MacBook environments after Git synchronization.

### Evidence before workaround

If a client/provider flow fails twice:

1. stop repeating it;
2. capture sanitized protocol evidence;
3. compare against current official specification/docs;
4. identify whether the defect is ServerFS, provider, MCP SDK, client or Tunnel;
5. fix the owning boundary rather than weakening validation globally.

### Secrets

Never commit real OAuth credentials.

`.env.example` contains names and safe placeholders only. Local `.env` remains ignored/private.

---

## Suggested implementation/checkpoint order

```text
plan
  |
  v
Phase 0 threat model + baseline
  |
  v
Phase A config/default-off compatibility
  |
  v
Phase B Resource Server verifier
  |
  v
Phase C serverfs-auth + SQLite + opaque tokens
  |
  v
Phase D CIMD / pre-registration / optional DCR
  |
  v
Phase E admission policy
  |
  +--> F1 GitHub
  +--> F2 Feishu
  +--> F3 DingTalk
  +--> F4 WeChat
  |
  v
Phase G Linux Compose isolation
  |
  v
Phase H direct OAuth live E2E
  |
  v
Phase I OpenAI Tunnel regression/OAuth acceptance
  |
  v
Phase J full security regression
  |
  v
VPS checkpoint -> GitHub
  |
  v
WorkPC native regression -> GitHub
  |
  v
MacBook native regression -> GitHub
  |
  v
VPS final closeout
  |
  v
Docs/site/release gates -> v0.16.0 tag + Release
```

Provider phases may share tested broker primitives, but each provider must retain independent live acceptance and error handling.

---

## Definition of done

v0.16.0 is complete only when all of the following are true:

- OAuth is default-off and controlled by `.env`;
- v0.15-compatible operation is demonstrably unchanged when OAuth is off;
- remote Streamable HTTP can be OAuth-protected when enabled;
- ServerFS publishes standards-correct OAuth protected-resource discovery/challenges;
- ServerFS issues its own opaque resource-bound tokens;
- provider tokens cannot authenticate directly to MCP;
- admission is default-deny and based on stable provider subjects;
- GitHub, Feishu, DingTalk and WeChat adapters exist with current documented contracts;
- provider secrets/tokens are isolated from the MCP core;
- `serverfs-mcp` retains no general Internet egress;
- `serverfs-auth` runs as a separately isolated non-root Linux Docker service;
- CIMD is safely supported and DCR remains deliberate compatibility behavior;
- token/code/refresh replay and wrong-resource cases are rejected;
- direct generic OAuth live E2E is green;
- OpenAI Tunnel OAuth-disabled regression is green;
- OAuth-through-Tunnel is either green or a verified upstream limitation is explicitly recorded without security weakening;
- Windows and macOS native stdio regressions are green in their own environments;
- docs/site/configuration are synchronized with actual behavior;
- final CI/release gates are green.

---

## Research references frozen for planning

These references inform the plan but do not replace implementation-time verification of current provider documentation:

- MCP Authorization (2026-07-28): `https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization`
- MCP Client Registration (2026-07-28): `https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/client-registration`
- MCP Python SDK installed baseline: local `.venv/lib/python3.12/site-packages/mcp/server/auth/`
- GitHub OAuth Apps authorization: `https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps`
- GitHub OAuth App best practices: `https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/best-practices-for-creating-an-oauth-app`
- OpenAI Tunnel OAuth behavior: `https://github.com/openai/tunnel-client/blob/master/docs/onboarding.md`
- OpenAI Tunnel configuration/OAuth trusted origins: `https://github.com/openai/tunnel-client/blob/master/docs/configuration.md`
- OpenAI Tunnel OAuth resource issue #78: `https://github.com/openai/tunnel-client/issues/78`

DingTalk, Feishu and WeChat endpoint/field URLs are intentionally not frozen in this plan. Their adapters must re-read the then-current official platform documentation immediately before implementation so the code does not institutionalize stale endpoint generations.
