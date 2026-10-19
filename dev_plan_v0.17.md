# ServerFS v0.17.0 Development Plan

Status: frozen for implementation — 2026-10-19.

## Goal

Add an **optional static API Key authentication mode for remote Streamable HTTP** so a ServerFS operator can protect a standard MCP endpoint with one high-entropy shared secret without deploying the v0.16 OAuth authorization service.

The feature is intentionally small:

```text
MCP client
   |
   |  Authorization: Bearer <key>        (recommended)
   |  or /mcp?key=<key>                  (compatibility/convenience)
   v
ServerFS HTTP API Key boundary
   v
MCP Streamable HTTP
   v
existing ServerFS MCP Core
   v
existing workdir / mutation / binary / Agent policies
```

v0.17.0 does **not** replace or weaken OAuth 2.1. It adds a separate ingress authentication choice for single-operator and small trusted-client deployments.

---

## Release definition

ServerFS v0.17.0 introduces static API Key authentication for remote Streamable HTTP.

The operator configures one secret:

```dotenv
SERVERFS_HTTP_API_KEY=
```

Semantics:

- empty/unset: API Key authentication is disabled;
- non-empty: API Key authentication is enabled for Streamable HTTP;
- `SERVERFS_HTTP_API_KEY` and `SERVERFS_OAUTH_ENABLED=true` are mutually exclusive and must fail closed at startup;
- native stdio is unchanged and does not require the HTTP API Key;
- the OpenAI Secure MCP Tunnel control-plane key remains a separate credential and is not reused;
- workdir authorization remains the existing ServerFS policy model; the API Key proves only possession of the instance-level shared secret.

Supported client credential forms:

1. **Recommended**: `Authorization: Bearer <key>`;
2. **Compatibility/convenience**: exact query parameter `?key=<key>`, including DingTalk-style one-line MCP URLs.

The query form is explicitly documented as less desirable because URLs can be captured by reverse-proxy, browser, observability or infrastructure logs. ServerFS itself must not emit the query secret in startup, access, error or audit logs.

---

## Product positioning

ServerFS will expose three remote HTTP security choices:

| Mode | Built-in identity | Operator complexity | Intended use |
| --- | --- | --- | --- |
| no built-in auth | none | lowest | private network, OpenAI Tunnel, or external auth gateway |
| static API Key | one shared-secret holder identity | low | single user / small trusted client set |
| OAuth 2.1 | federated user identity and token lifecycle | higher | public or multi-user identity-aware deployments |

The static API Key mode is a ServerFS HTTP ingress extension. It is **not** advertised as MCP-standard OAuth authorization and must not emit OAuth discovery or Protected Resource Metadata merely because the API Key is enabled.

---

## Non-goals

v0.17.0 does not add:

- multiple API Keys;
- per-key users, labels, scopes or RBAC;
- API Key persistence/database/state service;
- API Key expiry, refresh or automatic rotation;
- OAuth + API Key OR/AND combinations;
- an API Key management endpoint;
- a generic rate limiter;
- domain registration, DNS, certificate issuance or reverse-proxy automation;
- a replacement for `serverfs-auth`;
- stdio authentication;
- changes to filesystem containment, workdir policy, binary transfer, file ingress or Agent authorization semantics.

If future use cases require multiple independently revocable identities, OAuth or a later explicit credential-management design should be used instead of growing v0.17 into an ad-hoc identity database.

---

## Security model

### Credential semantics

`SERVERFS_HTTP_API_KEY` is a bearer secret. Possession grants access to the same ServerFS MCP surface that an otherwise admitted remote caller would receive, subject to all existing ServerFS policies.

It does not bypass:

- configured workdirs;
- read-only state;
- deny/reserved-path rules;
- binary-transfer gates and size limits;
- revision-guarded mutation semantics;
- Agent Bridge enablement, per-workdir Agent policy and runtime allowlists;
- writer leases or platform-specific containment.

### Secret handling

The implementation must:

- never log the configured key;
- never log a received candidate key;
- never include either value in exceptions, startup events, doctor output or MCP errors;
- compare secrets with `hmac.compare_digest` (or equivalent constant-time comparison for equal-type byte/string values);
- reject malformed or ambiguous credentials before dispatching the MCP request;
- keep `.env` / operator-owned secret handling under the repository's existing secret rules.

### Query parameter risk

The `?key=` form exists for interoperability with MCP ecosystems that distribute a complete one-line URL. It is not the preferred transport for bearer credentials.

ServerFS must ensure its own HTTP serving path does not emit the raw query string when API Key mode is enabled. Reverse proxies and upstream infrastructure remain operator-controlled and may still record the URL; documentation must warn operators to sanitize/disable query logging or prefer the Authorization header.

### TLS boundary

A public API-Key-protected endpoint still requires HTTPS. ServerFS does not become a certificate manager in v0.17. Operators continue to terminate TLS at their chosen reverse proxy/gateway or otherwise provide a secure transport.

---

## Authentication mode contract

The effective Streamable HTTP auth mode is exactly one of:

```text
none
api_key
oauth
```

Resolution:

```text
SERVERFS_OAUTH_ENABLED=false + SERVERFS_HTTP_API_KEY empty     -> none
SERVERFS_OAUTH_ENABLED=false + SERVERFS_HTTP_API_KEY non-empty -> api_key
SERVERFS_OAUTH_ENABLED=true  + SERVERFS_HTTP_API_KEY empty     -> oauth
SERVERFS_OAUTH_ENABLED=true  + SERVERFS_HTTP_API_KEY non-empty -> startup error
```

The startup log may emit only the non-secret effective mode, for example:

```text
http_auth_mode=api_key
```

It must not log key length, prefix, digest or any other stable key-derived identifier unless a later explicit audit requirement justifies it.

---

## HTTP credential contract

### Authorization header

Accepted form:

```http
Authorization: Bearer <configured-key>
```

Rules:

- scheme matching follows normal case-insensitive HTTP auth-scheme handling;
- exactly one usable Bearer credential is accepted;
- missing, empty, malformed or incorrect credentials fail before MCP dispatch;
- the response must not echo credential material.

### Query parameter

Accepted form:

```text
/mcp?key=<configured-key>
```

Rules:

- the parameter name is fixed as `key`;
- exactly one `key` value is allowed;
- empty `key` is rejected;
- duplicate `key` parameters are rejected;
- percent-decoding follows the server framework's normal query decoding once; no bespoke double-decoding is introduced.

### Header/query ambiguity

If both an Authorization Bearer credential and a `key` query parameter are present, reject the request rather than choosing one. This keeps credential precedence unambiguous and avoids accidental mixed-client configuration.

### Failure response

API Key authentication failures should use a minimal HTTP-level response and must not masquerade as OAuth discovery. The exact status/body/header contract will be frozen in tests before release; the default design target is a generic `401 Unauthorized` with no secret-derived detail.

Malformed ambiguous credential syntax may use `400 Bad Request` only where the distinction materially improves protocol clarity; otherwise prefer a uniform 401 to minimize information disclosure.

---

## Architecture

### Shared MCP core remains unchanged

`create_server()` remains the host-neutral source of the MCP tool/resource surface.

OAuth remains owned by the MCP SDK `AuthSettings` / `TokenVerifier` integration exactly as in v0.16.

Static API Key authentication lives outside the OAuth verifier path:

```text
create_server()
   |
   +-- OAuth mode -> existing SDK auth/token-verifier integration
   |
   +-- API Key mode -> no OAuth AuthSettings / no TokenVerifier
                         |
                         v
                 streamable_http_app()
                         |
                         v
                 small ServerFS ASGI middleware
                         |
                         v
                      Uvicorn
```

Do not implement the static key as a fake OAuth token verifier because that would incorrectly expose OAuth Resource Server discovery/challenge semantics.

### HTTP serving boundary

The installed `mcp==2.3.0` SDK exposes `streamable_http_app()`. v0.17 may use that supported ASGI surface to wrap only the HTTP ingress that needs static API Key handling, rather than forking or patching the SDK.

The narrowest acceptable implementation is preferred:

- preserve the existing `mcp.run("streamable-http", ...)` path when no ServerFS-owned HTTP wrapper is necessary, unless one shared runner materially reduces duplication without changing behavior;
- API Key mode may build the SDK Streamable HTTP Starlette app, wrap it in a pure ASGI middleware, and serve it with Uvicorn;
- do not use middleware that buffers or rewrites MCP streaming responses unnecessarily;
- preserve current request-body limits, transport-security settings, session behavior and Host/Origin protection.

### Access logging

Because URL query credentials can leak through ordinary access logs, API Key mode must use an HTTP serving configuration that does not emit the raw query string.

The simplest acceptable v0.17 implementation is to disable Uvicorn's default access log for the API-Key-protected app and retain ServerFS's structured non-secret application logs. Do not invent a custom access-log subsystem unless needed by tests or existing operational contracts.

If the implementation instead supplies a sanitized access logger, tests must prove that raw query strings and secret values cannot appear.

---

## Configuration surface

Add to shared `Settings`:

```text
http_api_key: str = ""
```

Environment input:

```dotenv
SERVERFS_HTTP_API_KEY=
```

Validation:

- strip only operator-surrounding whitespace consistently with existing secret settings;
- an empty result means disabled;
- do not enforce an arbitrary minimum length that would break existing integrations, but documentation must recommend a cryptographically random high-entropy secret;
- reject OAuth + API Key simultaneous enablement;
- keep the raw secret out of repr/logging paths where practical.

`.env.example` must document the feature adjacent to generic HTTP and OAuth settings, while keeping the variable empty by default.

No new Compose service is required. Existing `compose.http.yml` remains the generic publication shape.

---

## Platform scope and repository synchronization

The three authoritative development workdirs remain independent and synchronize only through GitHub:

| Platform scope | Environment | Workdir | v0.17 responsibility |
| --- | --- | --- | --- |
| Linux + shared core | VPS001 ServerFS | `ServerFS` | implementation, HTTP E2E, Compose, docs/site, release closeout |
| Windows native | WorkPC ServerFS MCP | `serverfs-mcp` | pull shared checkpoint, shared-Python/collection/native regression, Windows CI evidence |
| macOS native | MacBook ServerFS MCP | `serverfs` | pull latest checkpoint, shared-Python/native regression, macOS CI evidence |

Rules:

1. GitHub `origin` is the only repository synchronization channel between hosts.
2. Do not copy repository files directly between Linux/Windows/macOS.
3. Shared/core changes are implemented on VPS first and pushed as an accepted checkpoint.
4. Windows fast-forwards to that checkpoint and performs Windows-local verification; any genuinely required Windows-only fix is committed/pushed from WorkPC.
5. macOS fast-forwards to the latest accepted checkpoint and performs macOS-local verification; any genuinely required macOS-only fix is committed/pushed from MacBook.
6. VPS fast-forwards to the combined `origin/main` before final regression/docs/version/release work.
7. Do not mutate a platform's unrelated existing local files. The known Mac `.workbuddy/memory/2026-10-10.md` modification must remain byte-preserved unless the maintainer explicitly asks otherwise.
8. Platform-specific Agent runtimes may operate only their own host/workdir.

---

## Phase 0 — Freeze plan and baseline

1. confirm Linux/Windows/macOS are at the accepted v0.16.0 release commit;
2. write and freeze `dev_plan_v0.17.md`;
3. commit/push the plan as a plan-only checkpoint;
4. require Linux/shared worktree cleanliness after the checkpoint;
5. run a focused pre-change baseline covering config/main/current HTTP/OAuth tests and the generic HTTP Compose render.

Success criterion: v0.17 has an auditable frozen design and a green v0.16 baseline before product code changes.

---

## Phase A — Configuration and auth-mode state

1. add `SERVERFS_HTTP_API_KEY` parsing to shared `Settings`;
2. derive a small explicit effective HTTP auth mode (`none` / `api_key` / `oauth`) without exposing the secret;
3. reject OAuth + API Key simultaneous configuration at startup;
4. preserve all v0.16 defaults when the key is empty;
5. expose only non-secret auth mode in startup logging;
6. add strict config tests for disabled, enabled, whitespace, conflict and existing OAuth behavior.

Success criterion: configuration alone cannot create an ambiguous authentication state.

---

## Phase B — Static API Key ASGI boundary

1. add a small shared module (expected shape: `src/serverfs_mcp/http_auth.py`) for static-key HTTP authentication;
2. implement a pure ASGI middleware or equally narrow ASGI wrapper;
3. accept either one Bearer header or one `key` query parameter;
4. reject duplicate query keys and header+query ambiguity;
5. compare with `hmac.compare_digest`;
6. return a generic unauthorized response without OAuth metadata;
7. never attach the secret to request state, logs or exceptions beyond the minimum local comparison lifetime;
8. add unit tests including missing, correct, incorrect, duplicate, malformed and ambiguity cases.

Success criterion: unauthenticated/incorrect requests never reach the wrapped MCP app; correct credentials do.

---

## Phase C — Streamable HTTP runner integration and log safety

1. preserve current MCP `create_server()` behavior and OAuth integration;
2. in API Key mode, obtain the SDK's supported `streamable_http_app()` using the same path, body-size and transport-security settings currently passed to `mcp.run()`;
3. wrap only that HTTP app with the API Key boundary;
4. serve via Uvicorn with default raw-query access logging disabled (or an equivalently proven sanitized access-log path);
5. preserve `SERVERFS_HTTP_HOST`, port, path, Host allowlist, Origin allowlist and DNS-rebinding protection;
6. prove OAuth mode still uses its existing discovery/challenge behavior;
7. prove unauthenticated mode still behaves as v0.16.

Success criterion: API Key mode changes authentication only, not MCP transport semantics or existing security settings.

---

## Phase D — HTTP E2E and secret-leak regression

Build a real loopback Streamable HTTP acceptance harness using the production app/runner boundary where practical.

Cover:

- no credential -> rejected;
- wrong Bearer -> rejected;
- correct Bearer -> initialize / tools discovery succeeds;
- correct `?key=` -> initialize / tools discovery succeeds;
- duplicate `key` -> rejected;
- Bearer + query together -> rejected;
- OAuth metadata is not exposed merely because API Key mode is enabled;
- normal MCP tool call succeeds after authentication;
- request body limits remain effective;
- Host/Origin rejection remains effective;
- captured ServerFS/Uvicorn logs contain neither configured key nor received wrong-key candidates nor raw secret-bearing query URL.

Include a distinct high-entropy-looking sentinel secret in tests and assert it is absent from captured output.

Success criterion: both supported credential forms work end-to-end and the query-secret logging hazard is covered by regression tests.

---

## Phase E — Deployment and Compose regression

1. keep base `compose.yml` unchanged unless required to pass the new environment variable into `serverfs-mcp`;
2. ensure `SERVERFS_HTTP_API_KEY` can reach the MCP container from `.env` through the established Compose environment boundary;
3. keep `compose.http.yml` loopback publication unchanged;
4. keep `compose.oauth.yml` behavior unchanged and prove API Key/OAuth conflict fails at application startup rather than silently choosing one;
5. verify base, generic HTTP, OAuth, OAuth+Agent and OAuth+file-ingress Compose renders remain valid;
6. do not add a new API-Key sidecar/service/network/database.

Success criterion: existing deployment shapes remain stable and API Key requires only one new secret setting.

---

## Phase F — Documentation and operator contract

Update current-state docs without rewriting historical plans.

Minimum surfaces:

- `.env.example`;
- `README.md`;
- generic HTTP / OAuth operator documentation as appropriate;
- security/configuration/architecture docs where authentication modes are enumerated;
- English site docs;
- Simplified Chinese site docs.

Document:

- three effective HTTP auth modes;
- one-key limitation;
- recommended `Authorization: Bearer` usage;
- DingTalk-style one-line `?key=` URL example with placeholder only;
- explicit warning that URL secrets may be logged by reverse proxies/infrastructure;
- HTTPS requirement for public deployment;
- OAuth/API Key mutual exclusion;
- key rotation = change `.env` + restart/recreate the relevant ServerFS process/container;
- no per-user identity/scopes in API Key mode;
- no domain/TLS automation supplied by ServerFS.

Do not place a real secret in tracked examples, tests, screenshots or generated site output.

Success criterion: a new operator can choose none/API Key/OAuth without reading implementation code.

---

## Phase G — Linux/shared validation checkpoint

Before platform hand-off run all applicable shared gates:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose --quiet config
docker compose -f compose.yml -f compose.http.yml --quiet config
docker compose -f compose.yml -f compose.oauth.yml --quiet config
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
cd site && npm ci && npm run build
```

Also run focused API Key/OAuth/security tests separately so failures are easy to attribute.

Commit and push the accepted Linux/shared implementation checkpoint only after the relevant gates are green.

Success criterion: `origin/main` contains a green shared implementation suitable for native platform regression.

---

## Phase H — Windows regression on WorkPC

On the Windows development environment only:

1. require a clean repository before fast-forward;
2. fetch and fast-forward to the accepted shared checkpoint;
3. run collection/import gates proving new shared HTTP modules are portable on Windows;
4. run config/main/API-Key/OAuth/security tests that are platform-neutral;
5. run the established Windows native source/wheel regression required by current CI/workflow;
6. confirm native `serverfs serve` remains stdio and does not unexpectedly require `SERVERFS_HTTP_API_KEY`;
7. do not stop or weaken the real production native Tunnel/Agent service merely to satisfy a test that should be isolated;
8. if a genuine Windows-specific portability defect exists, fix only that defect on WorkPC, validate, commit and push it.

Success criterion: shared API-Key code imports/tests on Windows and the existing native lifecycle remains unchanged.

---

## Phase I — macOS regression on MacBook

On the macOS development environment only:

1. fetch and fast-forward to the latest accepted `origin/main` while byte-preserving the known unrelated `.workbuddy/memory/2026-10-10.md` modification;
2. run shared config/main/API-Key/OAuth/security tests;
3. run the established macOS native focused regression and package/build gates relevant to shared-module changes;
4. confirm native `serverfs serve` remains stdio and does not unexpectedly require the HTTP API Key;
5. do not mutate Login Items/ServiceManagement state unless a product defect specifically requires live lifecycle acceptance;
6. if a genuine macOS-specific portability defect exists, fix only that defect on MacBook, validate, commit and push it.

Success criterion: shared API-Key code remains portable on the supported macOS native environment with no stdio/lifecycle regression.

---

## Phase J — Combined Linux regression and CI convergence

1. fast-forward VPS to the latest `origin/main` containing all accepted platform checkpoints;
2. rerun full Linux/shared root validation;
3. rerun API Key + OAuth focused security matrix;
4. rerun all supported Compose renders and site build;
5. inspect GitHub CI for Container, Windows Native, macOS Native and any other release-blocking workflows;
6. fix only demonstrated regressions at their owning platform boundary;
7. do not tag while any required CI is red/queued indefinitely without diagnosis.

Success criterion: one combined commit lineage is green across Linux, hosted Windows and hosted macOS gates.

---

## Phase K — Version and release-facing convergence

After implementation/platform evidence is accepted:

1. update root Python package version to `0.17.0`;
2. update Agent Bridge/native package version surfaces only where repository conventions require the release version to stay synchronized, without implying functional Agent changes;
3. regenerate lockfiles through supported tooling rather than manual edits;
4. update README/docs/site/current-stable references to `v0.17.0` **before tagging**, following the repository's one-release-commit convention;
5. update `.env.example` image example to the v0.17.0 exact pin;
6. update `AGENTS.md` current stable baseline to v0.17.0;
7. update `dev_plan_v0.17.md` status/checkpoint evidence to describe the completed implementation while keeping historical facts intact;
8. scan managed docs/site for stale present-tense v0.16 current-stable/release-candidate wording, excluding historical evidence and generated/vendor trees.

Success criterion: the release commit already describes v0.17.0 as released/current so no post-tag documentation commit is necessary.

---

## Phase L — Final release-candidate gates

On the exact intended release commit run:

- `git diff --check`;
- root `uv lock --check` / dependency consistency gates;
- full Ruff lint/format;
- full root pytest;
- focused API Key/OAuth/security regression;
- base + generic HTTP + OAuth + OAuth/Agent/file-ingress Compose validation as applicable;
- production site build;
- Windows Native CI green;
- macOS Native CI green;
- Container CI green;
- any release workflow checks required by repository policy.

No release tag may be created from a commit different from the one whose required CI/evidence was accepted.

Success criterion: immutable release candidate commit is green and fully documented.

---

## Phase M — Tag and GitHub Release

1. create immutable annotated/lightweight tag according to existing repository convention: `v0.17.0`;
2. push the tag;
3. verify tag-driven stable image workflow starts/completes as expected where observable;
4. publish the GitHub Release from the same tag/commit using supported `gh`/GitHub API tooling;
5. summarize user-visible changes, security positioning, configuration and upgrade compatibility;
6. state that existing deployments remain unchanged unless `SERVERFS_HTTP_API_KEY` is explicitly configured;
7. do not move/recreate the tag after publication.

Success criterion: GitHub shows a `v0.17.0` Release backed by the exact accepted release commit and the repository remains clean/current afterward.

---

## Test matrix

Minimum new API Key cases:

| Case | Expected result |
| --- | --- |
| key unset, OAuth off | v0.16 unauthenticated HTTP behavior |
| correct Bearer | accepted |
| wrong Bearer | 401, no secret echo |
| empty Bearer | rejected |
| malformed Authorization | rejected |
| correct `?key=` | accepted |
| wrong `?key=` | rejected |
| empty `?key=` | rejected |
| duplicate `?key=` | rejected |
| Bearer + query | rejected as ambiguous |
| API Key + OAuth configured | startup failure |
| API Key mode HTTP logs | no configured/candidate secret; no raw secret-bearing query URL |
| stdio with API key env present | unchanged/no HTTP auth requirement |
| OAuth mode | v0.16 discovery/token behavior preserved |
| Host/Origin policy | preserved |
| binary request body sizing | preserved |

---

## Acceptance / release blockers

v0.17.0 must not release if any of the following remains true:

- a query API Key can appear in ServerFS/Uvicorn logs under the supported API-Key runner;
- OAuth and API Key can be simultaneously active without a startup error;
- API Key mode exposes OAuth discovery/challenge semantics;
- stdio begins requiring or interpreting the HTTP API Key;
- Host/Origin/DNS-rebinding protection is weakened;
- correct authenticated Streamable HTTP cannot complete real MCP initialize/tool discovery;
- Windows/macOS shared-code regression is unverified;
- required Container/Windows/macOS CI is red;
- release-facing docs still describe v0.16.0 as current stable on the release commit;
- the release tag would point at a commit different from the accepted CI/evidence commit.

---

## Upgrade compatibility

For existing v0.16.0 operators who do nothing:

```text
SERVERFS_HTTP_API_KEY is absent/empty
```

there is no intended authentication behavior change.

- existing OpenAI Tunnel deployments remain unchanged;
- generic HTTP deployments relying on private networking or an external gateway remain unchanged;
- OAuth deployments remain unchanged;
- native Windows/macOS stdio remains unchanged;
- no new sidecar/database/state directory is required.

API Key authentication is activated only by explicit secret configuration.
