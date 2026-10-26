---
name: serverfs-auth
description: Choose or change remote HTTP API Key/OAuth authentication; rotate secrets or diagnose admission, issuer, and resource failures.
---

# Configure ServerFS HTTP authentication

Treat authentication as a remote Streamable HTTP ingress decision. Do not apply ServerFS HTTP authentication to native Windows/macOS stdio deployments.

## When to use

Use this skill when the user explicitly wants to:

- add/remove/rotate the ServerFS static HTTP API Key;
- enable/configure/disable built-in OAuth 2.1;
- choose the appropriate ServerFS-owned remote HTTP authentication mode;
- diagnose authentication startup, admission or resource/issuer problems.

## When NOT to use

Do not use this skill for:

- OpenAI Tunnel control-plane credentials: `serverfs-tunnel`;
- workdir read/write authorization: `serverfs-configure`;
- Agent runtime/provider authorization: `serverfs-agent-bridge`;
- Windows/macOS native stdio, which does not use ServerFS HTTP API Key/OAuth.

Do not silently enable an auth mode as part of installation or upgrade without explicit operator intent.

## Preflight

First locate the target ServerFS checkout or deployed installation and establish its version, active configuration and lifecycle owner. In imported/standalone mode, if no authoritative checkout/deployment source is accessible, stop at bounded guidance; do not mutate or infer target commands/settings.

Paths below are relative to the located **ServerFS checkout root**, never to this skill or the Plugin ZIP. Resolve repository paths only after locating that checkout. For an installed deployment without a checkout, use its version-matched deployment documentation, installed CLI help and active configuration; obtain matching sources before a step that needs missing repository material.

1. Confirm the target is Linux remote Streamable HTTP and identify the exact public/private ingress shape.
2. Read the existing `.env` without printing secrets and determine the effective ServerFS-owned auth mode:
   - no built-in auth;
   - static API Key;
   - OAuth 2.1.
3. Confirm API Key and OAuth are not simultaneously enabled. That combination must fail closed; do not invent precedence.
4. Identify whether TLS/auth is already provided by an external gateway. Do not add ServerFS-owned auth unless that is the requested design.
5. For OAuth, establish the canonical public MCP resource URL, authorization-server public/issuer URL, provider(s), stable principal allowlist and state directory before mutation.
6. Keep Tunnel keys, provider secrets and introspection secrets separate by purpose.

## Authoritative sources

- `site/src/content/docs/docs/api-key.md`
- `site/src/content/docs/docs/oauth.md`
- `site/src/content/docs/docs/configuration.md`
- `site/src/content/docs/docs/getting-started.md`
- `.env.example`
- `compose.http.yml`
- `compose.oauth.yml`
- current authentication parser/tests in the target checkout when exact validation behavior matters.

Do not copy provider IDs/secrets or current `.env` values into tracked documentation or skill files.

## Procedure

1. **Choose the mode from explicit user intent.** If the user only needs a private/gateway/Tunnel-protected endpoint, no built-in auth may be correct. Use API Key for one shared-secret identity; use OAuth when federated identity/browser login and independently managed user admission are required. Record the requested mode and ingress/TLS boundary before editing.
2. **Edit existing `.env` surgically.** Never replace it with `.env.example`. Review the local diff for only requested auth keys without emitting their values.
3. **Preserve mutual exclusion.** API Key non-empty + OAuth enabled is invalid and must remain a startup error. Confirm effective configuration selects exactly one requested mode, or neither when explicitly intended.
4. **Preserve transport security.** Public bearer/OAuth deployments require HTTPS at an explicit ingress boundary; ServerFS does not become a DNS/certificate manager. Establish the HTTPS termination and exact Host/Origin policy before public access.
5. **Render the exact Compose shape before restart.** Generic HTTP uses `compose.http.yml`; OAuth additionally requires `compose.oauth.yml`; preserve any already-enabled Host Bridge overlay (`compose.agent.yml`) and other profiles. On v0.23 Linux that overlay may exist for Agent delegation, Repo Ops, or both; authentication work must not change either capability's authorization. Proceed only after config --quiet succeeds for the deployment being restarted.
6. **Restart only the required services/deployment.** Provider secret rotation may only require `serverfs-auth`; introspection-secret rotation must be coordinated across the resource/auth services. Confirm affected services are healthy and both sides of any coordinated secret rotation agree.
7. **Validate admission and rejection behavior without logging secrets.** Test the intended client path and at least one expected rejection where safe. Record redacted admission/rejection outcomes; unavailable live provider acceptance remains Not verified.

## Platform branches

Load only the matching branch after identifying the deployment and requested mode:

- Linux / generic HTTP: no built-in auth: read [no-built-in-auth branch](references/no-built-in-auth.md) before platform/mode-specific changes.
- Linux / static API Key: read [api-key branch](references/api-key.md) before platform/mode-specific changes.
- Linux / OAuth 2.1: read [oauth branch](references/oauth.md) before platform/mode-specific changes.
- Windows/macOS native: read [native-stdio branch](references/native-stdio.md) before platform/mode-specific changes.

## Safety / stop conditions

Stop when:

- API Key and OAuth are both configured and the operator intent is not clear enough to choose which one to disable;
- public HTTP is being exposed without an explicit HTTPS boundary;
- OAuth canonical resource/issuer/public URLs are unknown;
- provider stable principal cannot be established and someone proposes using a display/email identity as a shortcut;
- a secret would be printed, committed, put in `serverfs.toml`, or placed inside an exposed workdir;
- OAuth state would be deleted/reinitialized without explicit destructive authorization;
- the task would broaden Host/Origin/resource/audience validation merely to make a client work;
- native stdio is being misidentified as the HTTP deployment.

Do not weaken OAuth audience/resource validation to work around external client/Tunnel behavior.

## Verification

### API Key

Prove:

- exact Compose config renders;
- unauthenticated/wrong credential is rejected;
- the intended Bearer credential reaches MCP successfully;
- OAuth discovery is not incorrectly presented as the API Key mechanism;
- ServerFS/upstream logs used in the test do not reveal the key.

Prefer header-based testing; use query-key compatibility only when the target client requires it.

### OAuth

Prove:

- MCP and auth services start healthy;
- protected-resource/auth metadata uses the canonical external URLs;
- unauthenticated access is rejected as expected;
- at least one enabled provider login/admission path is validated when real provider credentials/browser acceptance are available;
- an unallowlisted identity is denied;
- issued ServerFS credentials are resource-bound and accepted by the MCP resource server;
- state persists across the intended service restart.

If live external provider acceptance is unavailable, report it as `Not verified` rather than inferring it from deterministic tests.

## Success criteria

The requested HTTP auth mode is the only active ServerFS-owned mode, secrets remain private, the exact deployment shape validates, expected admission/rejection behavior is demonstrated to the available level, and workdir/Repo Ops/Agent authorization remains unchanged.

## Failure handling

On startup failure, check the current parser error first; do not toggle unrelated settings. For OAuth, diagnose provider enablement/credentials, exact public/issuer/resource URLs, state permissions, introspection secret and stable allowlist in that order.

For a resource mismatch or Tunnel-related OAuth limitation, correct the client/canonical URL or consult current documented upstream behavior. Never solve it by accepting wildcard resources or undocumented gateway identities.
