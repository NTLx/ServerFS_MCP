# ServerFS v0.20.0 Development Plan

Status: **In progress.** Phases B and C were implemented and validated in baseline commit `840aae50`. Phase D local acceptance is complete: its initial direct MCP-over-HTTP smoke failure was traced to missing disabled-slot placeholders in `/workdirs/02..16`, fixed by image defaults, and the post-fix single direct MCP call returned the configured read-only `workspace`. Focused tests, root Ruff, full root pytest, scratch image build, version lock checks and `git diff --check` passed. Registry client interoperability is **Not verified**. Phases E and F remain outstanding.

Planning date: 2026-10-21.

## 0. Decision summary

v0.20.0 is **Protocol & Distribution Maturity**. It hardens the existing MCP contract across dated protocol revisions, produces verifiable wire evidence, and makes the existing Linux OCI distribution registry-ready. It does not widen host authority or add a new runtime/execution surface.

Development started from the v0.19.0 repository baseline: Python MCP SDK `mcp==2.3.0`; separate root ServerFS and `agent_bridge` environments; stdio and Streamable HTTP; optional API Key and OAuth HTTP authentication; a Linux container/Tunnel deployment; native Windows wheels/lifecycle; and narrowly supported native arm64 macOS 27 lifecycle. The development version is now `0.20.0`. These are distinct surfaces and must retain distinct evidence.

Frozen decisions:

- Production Streamable HTTP uses one stateful SDK app (`stateless_http=False`): preserve the legacy `2025-11-25` session lifecycle, while serving modern `2026-07-28` requests on that same app using per-request `_meta`/headers without a session dependency. Do not design a second endpoint.
- Stable Conformance release `v0.1.16` does not support `--requirements`; v0.20 selects official source commit `232a9014457eaf4c59559916e3d616d2f7f28f05`, which introduced the option, as its reproducible suite source. CI builds from that pinned source; never use floating `main`/`latest`.
- Capture actual Streamable HTTP requests/responses at the socket boundary for both protocol families. SDK-only unit tests are insufficient wire evidence.
- Keep stdio protocol-clean and separately tested. HTTP API Key and OAuth gates apply only to HTTP ingress; they must not become stdio authentication or alter stdio framing.
- Prepare `server.json`, OCI identity metadata, schema/version checks and CI validation for the MCP Registry preview. Registry publication is optional and must not become the only way to obtain or run ServerFS.
- Existing filesystem, Agent Bridge, ACP v1, authentication, deployment and platform authority boundaries remain fixed. No new tool that executes arbitrary host commands, no widened workdir roots, and no general Internet egress.
- **MCP Tasks extension and ACP v2 are explicitly out of scope.** ServerFS task tools remain the existing Agent Bridge contract; they are not MCP Tasks.

Official upstream references recorded as planning evidence are listed in §15. Their schemas, CLIs, and revisions must be rechecked at implementation time.

## 1. Goal and completion outcomes

Deliver a release whose protocol claims can be independently reproduced and whose container artifact can be described and validated for registry submission, while preserving supported clients and deployment modes.

Completion requires:

1. the formal Conformance gate is integrated from a confirmed immutable upstream revision, then both requirement sets are reported separately with no untriaged applicable required failures;
2. captured HTTP exchanges prove headers, body, session/lifecycle behavior, authentication and errors for modern and legacy clients;
3. stdio, API Key HTTP and OAuth HTTP behavior remain explicitly bounded and have independent deterministic coverage;
4. the platform capability matrix is backed by matching tests and native acceptance, not inferred from a shared source tree;
5. `server.json`, OCI annotation, package/schema versions and CI validation agree with the actual v0.20 release artifact;
6. current docs, comments and fixtures no longer make false current-state protocol or distribution claims, while historical records remain truthful;
7. release-facing README and both localized site trees describe only measured support and keep English/Chinese current-state meaning aligned.

## 2. Non-goals

v0.20.0 does not:

- add MCP Tasks, Task lifecycle RPC methods, or map existing Agent Bridge tasks to the Tasks extension;
- implement ACP v2 or change the v0.19 ACP v1 contract;
- add WebSocket, legacy SSE transport, a new public endpoint, or a new authentication provider;
- make Registry availability, credentials, APIs, or preview status a runtime or installation dependency;
- introduce automatic package installation, registry-driven executable launch, or trusted execution based on registry metadata;
- add host shell/argv/env execution, widen filesystem access, weaken path/race containment, or modify Agent authorization/provider permission policy;
- claim OAuth behavior is identical to API Key behavior or claim either authenticates stdio;
- broaden current macOS support beyond Apple M-series, native arm64, macOS 27, or current Windows lifecycle beyond non-elevated current-user Task Scheduler semantics;
- mechanically rewrite historical plans, acceptance reports, release notes, or fixtures whose old behavior is intentionally part of historical evidence.

## 3. Baseline and repository boundaries

Use the v0.19.0 source of truth before implementation. Relevant existing surfaces include:

| Boundary | Current repository location / evidence |
|---|---|
| Root product and MCP tools | `src/serverfs_mcp/`, `tests/`, root `pyproject.toml` and `uv.lock` |
| MCP transports and auth | `src/serverfs_mcp/main.py`, `http_auth.py`, `oauth_resource.py`, `compose.http.yml`, `compose.oauth.yml`, `tests/test_api_key_e2e.py`, OAuth tests |
| Agent Bridge and ACP v1 | `agent_bridge/src/serverfs_agent_bridge/`, `agent_bridge/tests/`, `agent_bridge/pyproject.toml`, `agent_bridge/uv.lock` |
| Linux OCI build | `Dockerfile`, `compose*.yml`, `.github/workflows/container.yml` |
| Native deployments | `deployment/windows/`, `native/macos/`, platform CI workflows, root CLI/config modules |
| Release website | `README.md`, `site/src/content/docs/docs/`, `site/src/content/docs/zh-cn/docs/`, `.github/workflows/pages.yml` |
| Maintainer gates | `docs/maintainers/invariants.md`, `docs/maintainers/workflow.md` |

Before editing, inspect actual MCP SDK transport construction and version negotiation, all protocol-related tests and fixtures, workflow triggers, current OCI image labels, release asset jobs, and version sources. Do not treat this plan's references to files as proof that their contents have not changed.

## 4. Protocol contract and conformance

### 4.1 Requirement-set baseline

The stable official Conformance release `v0.1.16` does not support `--requirements`. v0.20 uses official immutable source commit `232a9014457eaf4c59559916e3d616d2f7f28f05`, which introduced the option, and builds the CLI from that checkout in CI. Do not use floating `main`/`latest`. Run complete `--requirements 2025-11-25` and `--requirements 2026-07-28` suites separately and preserve each report and exit code. Full-suite exit codes are report evidence, not hard gates: the sets include undeclared capabilities and fixed-fixture/Host assumptions. Never use `--expected-failures` to manufacture a passing score.

The hard gate is the reviewed subset of scenarios applicable to ServerFS's declared capabilities and security boundaries, complemented by this repository's real-wire regressions. Use the pinned CLI command exit code as the scored-failure threshold and require `checks.json` to establish that a scenario was measured. Any newly observed applicable required failure blocks release/CI. A change in scenario applicability requires human review and updated versioned evidence; applicability must not be silently reclassified to clear a failure.

The 2025-11-25 set exercises the initialize-negotiated, session-aware Streamable HTTP family. The 2026-07-28 set exercises modern requests with per-request `_meta`/headers and no session dependency. A real loopback probe confirmed both on one stateful SDK app (`stateless_http=False`); retain that single production app and legacy session lifecycle. Do not create a second endpoint or silently reinterpret protocol markers.

**Conformance source decision:** stable `v0.1.16` lacks `--requirements`; CI builds the official source at immutable commit `232a9014457eaf4c59559916e3d616d2f7f28f05`, which provides the option. Verify roles, requirement-set availability, output format, endpoint setup and OAuth applicability against that pinned source. Preserve both full reports on each run.

### 4.2 Two-path architecture and negotiation

Keep modern and legacy wire behavior explicit within the single production Streamable HTTP SDK app (`stateless_http=False`), as confirmed by the real loopback probe. Reuse tool implementations and schemas where the wire contract is identical. Do not add a second endpoint; do not add unverified SDK calls.

Required behavior:

- valid modern requests on the stateful app receive per-request `_meta`/header handling and have no dependency on `Mcp-Session-Id`;
- valid legacy requests retain initialization, negotiated protocol version, session behavior and required subsequent-request headers;
- mismatched or missing headers/body protocol markers are rejected according to the relevant spec instead of silently routed to the other implementation;
- errors and JSON-RPC IDs remain bound to the originating exchange; no cross-request/session leakage;
- tool listing/calling and resource/prompt capabilities are advertised only when actually implemented;
- preserve all existing Host/Origin and DNS-rebinding defenses; do not weaken them to make the conformance harness connect.

### 4.3 Real Streamable HTTP wire evidence

Add deterministic tests using a real loopback TCP socket and raw HTTP client (or the official harness where it provides equally inspectable traffic), not only Starlette in-process transports. Capture sanitized request/response evidence for each revision covering:

- initialize/negotiation and the next legacy request, versus a standalone modern request;
- method, content type, protocol-version, session, and modern routing headers as specified by the pinned revision;
- matching and mismatching metadata/header/body cases;
- successful `tools/list` and representative read-only and guarded mutation calls, with schemas/results unchanged across transports;
- JSON-RPC errors, HTTP status/content type, notification handling and session expiry/rejection where applicable;
- authenticated and unauthenticated HTTP behavior without recording credentials or sensitive payloads.

The modern header set and exact validation rules are **probe items** against the final official 2026-07-28 specification and SDK implementation before coding. Do not guess optional headers or invent routing metadata. Test evidence stores redacted/minimal fixtures; never commit live tokens, filesystem contents, user paths, OAuth codes or private keys.

### 4.4 Conformance roles and boundaries

Run server conformance against the deployed Streamable HTTP server. Run client conformance only if ServerFS actually implements the client role under test; do not claim a client role because the project has an ACP client. OAuth Resource Server scenarios are evaluated only for the optional OAuth HTTP deployment and only where the official requirement set defines them for the MCP server role. The suite's authorization-server scenarios are not ServerFS MCP conformance requirements; the separate `serverfs-auth` identity broker remains its existing optional product boundary.

Report API Key, OAuth and unauthenticated loopback/publication modes separately. Conformance must not turn a test harness bypass into a production bypass.

## 5. Transport and authentication boundary matrix

| Surface | Protocol coverage | Authentication boundary | Required invariant |
|---|---|---|---|
| stdio | MCP framing and negotiated protocol revisions supported by the actual SDK; separate test leg | No HTTP API Key/OAuth middleware; local process ownership/configuration remains the boundary | stdout contains protocol frames only; diagnostics to stderr/logging |
| Streamable HTTP, API Key | modern and legacy wire paths where supported | Existing single configured key on HTTP requests only | constant-time/current implementation semantics preserved; no credential logging; transport Host/Origin protections remain active |
| Streamable HTTP, OAuth | modern and legacy wire paths where supported | Existing OAuth Resource Server validates bearer tokens; optional identity broker remains separate | issuer/audience/scope and identity policy unchanged; no downgrade to API Key or unauthenticated access |
| Streamable HTTP, no built-in auth | only explicit loopback or explicitly gated trusted gateway topology | Network/gateway is operator boundary; never claim public auth | listener remains unpublished by base Compose; public exposure requires TLS and explicit upstream auth boundary |

Do not imply that the 2026 protocol revision supersedes authentication or that API Key equals OAuth. Do not run HTTP middleware for stdio, add auth headers to stdio, or let conformance mode bypass either auth scheme.

## 6. Registry-preview-ready distribution

### 6.1 Distribution posture

Prepare the existing Linux OCI image for the official MCP Registry's preview workflow. The Registry is metadata/discovery; GHCR remains the image host and GitHub Releases remain the established Windows wheel/Plugin distribution. Existing direct Docker/Compose, Tunnel, native Windows and supported macOS routes remain documented and usable without Registry access.

No registry login/publish action is required for ordinary PR CI. Registry publication, if later authorized and technically accepted, is an explicit release operation and cannot be silently added as a required release dependency.

### 6.2 `server.json` and OCI identity

Add a root `server.json` only after probing the current official schema and package requirements. It must describe only real package transports and the actual OCI artifact. Use an immutable repository identity namespace controlled by the maintainer; do not invent or claim ownership of a namespace. The planned OCI package identifier is the GHCR image with a stable `0.20.0` tag (or immutable digest where schema/registry behavior permits). It must not point clients at `edge`/`latest` for reproducible installation.

The image metadata must include the exact label/annotation:

```text
io.modelcontextprotocol.server.name=<same name as server.json>
```

The manifest's server name, OCI annotation, `version`, package version/tag, repository URL, transport declaration, project version and release tag must agree. Keep the container's existing architecture claim to measured `linux/amd64` and `linux/arm64`; do not register native Windows/macOS as OCI packages unless their packaging is actually represented and supported by the current schema.

Registry schema URL, accepted OCI `registryType`, identifier/tag grammar, allowed metadata and official preview validation endpoint are **implementation-time probes**. Pin or vendor the exact published schema used for deterministic local CI if licensing and schema policy permit; otherwise fetch by a pinned immutable revision with cache/checksum. CI must not depend on a mutable `main` schema or Registry uptime. Validate `server.json` against that schema, enforce cross-file version/name/image-label consistency, and inspect built image config/labels. If the current Registry preview rejects a claim, fix the manifest or record a blocker; never weaken the validator to green.

### 6.3 CI validation without Registry dependence

CI validation must run offline after dependencies are installed and fail on:

- schema-invalid or unknown/unintended package fields;
- mismatch among project version, manifest, package reference and tag template;
- absent/wrong OCI name annotation;
- mutable-only package reference or inaccurate architecture/transport claims;
- secrets, credentials or host-specific paths in distribution metadata.

An optional non-blocking/maintainer-invoked preview API probe may supplement local validation. It cannot be the only check and must not publish or require credentials on pull requests.

## 7. Cross-platform capability matrix

This is the v0.20 acceptance target, not a claim that all cells are already implemented. Each claim must be evidenced on the named platform.

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| MCP stdio | Required: real process pipes, clean stdout, both protocol fixtures supported by selected SDK | Required: native arm64 process pipes and clean stdout | Required: native Windows pipes/process invocation and clean stdout |
| MCP Streamable HTTP | Required: real TCP conformance target, production Host/Origin/DNS defenses, API Key and OAuth configured modes | Required if current native HTTP command is supported; verify both protocol paths without widening macOS scope | Required if current native HTTP command is supported; verify both protocol paths without WSL/Docker substitution |
| Linux OCI registry-ready metadata | Required: build and inspect `amd64`/`arm64` image labels and manifest | N/A | N/A |
| Auth boundaries | API Key/OAuth HTTP gates tested independently; stdio unauthenticated as local process boundary | Native config/runtime tests confirm no HTTP auth leakage into stdio | Native config/runtime tests confirm no HTTP auth leakage into stdio |
| Existing filesystem/Agent Bridge | Root and Bridge suites; ACP remains v1 | Native arm64 macOS 27 root and Bridge suites; ACP remains v1 | Native Windows root and Bridge suites; ACP remains v1 |
| Persistent service acceptance | Existing container/Tunnel path; no new privileged service | Existing app-associated `SMAppService`/LaunchAgent boundary | Existing current-user, non-elevated Task Scheduler 2.0 boundary |

If current transport support is not present on a native platform, the plan must preserve the existing supported command/deployment boundary and explicitly mark that matrix cell unsupported, not add a new native service as part of protocol maturity. The platform requirement is to verify actual claims and avoid regression, not force identical deployment shapes.

## 8. Stale docs, comments and fixture hygiene

After behavior is determined, inventory protocol/distribution wording in README, `docs/`, maintained site pages in both locales, source comments/docstrings, CI workflow comments and protocol fixtures. Search for assumptions such as “only 2025 protocol”, mandatory stateful HTTP sessions, deprecated modern headers, old SDK versions, beta registry status described as stable, unverified Registry publication, stale image labels, or stale release tag values.

Classify each hit before editing:

1. current-state error: update English/Chinese current-state docs together and relevant comments/fixtures;
2. historical evidence: keep it accurate as-of the recorded version and label historical status if it reads as current;
3. test oracle: update only when implementation/spec evidence proves the old expectation obsolete, retaining regression coverage for compatibility where required.

Do not delete legacy fixtures merely because the modern path is added. Retain an explicit corpus for both protocol revisions. Run a residual managed-doc scan excluding `.git`, `.workbuddy`, `node_modules`, generated/build outputs and historical evidence where appropriate; review every hit rather than bulk replacing version strings.

## 9. Security and compatibility constraints

- Preserve the root filesystem path containment, deny/reserved-path, atomic mutation, revision and binary-ingress invariants in `docs/maintainers/invariants.md`.
- Preserve narrow MCP tools and stable schemas/output schemas; a protocol adapter may change framing/metadata only as required by official spec evidence.
- Preserve HTTP Host/Origin allowlists and DNS-rebinding protections. Do not expose MCP port in base Compose or broaden wildcard hosts.
- Keep main Linux MCP process without general Internet egress. Conformance/Registry tooling belongs in CI or maintainer environment, not the runtime container.
- Keep stdio stdout protocol-clean, logs sanitized, and fixtures free of secrets/content/private paths.
- Keep API Key/OAuth optional and HTTP-scoped; authentication failures remain fail-closed. Do not leak bearer tokens, API keys, OAuth codes or principal data to logs/reports.
- Preserve client compatibility with v0.19 behavior; do not remove the legacy protocol until a separately approved deprecation policy and measured client evidence exist.
- Do not alter native runtime/provider permissions, ACP configuration or ACP v1 capability mapping.
- Pin action/tool/schema inputs according to repository policy; do not add an unpinned third-party conformance service or remote validator as a required supply-chain dependency.
- Version and registry metadata must not claim features unsupported by actual artifacts.

## 10. Phased implementation sequence

### Phase A — Reconnaissance and probes

Inspect v0.19 transport wiring, SDK negotiation behavior, CI jobs, release workflows, Docker labels, package metadata and localized docs. Probe final specs and official suite requirement files/CLI; probe Registry schema, OCI package declaration and preview validation. Produce a short decision record inside the eventual PR/implementation notes before changing code. **Gate:** no guessed API/header/schema fields remain.

### Phase B — Protocol seam and deterministic wire fixtures

Define routing/compatibility behavior for both dated revisions using the pinned SDK's actual supported API. Add raw loopback Streamable HTTP test harness and sanitized request/response fixtures. Ensure shared tool logic and authorization remain downstream of protocol framing. **Gate:** modern and legacy fixtures prove distinct correct lifecycles; mixed/malformed requests fail closed.

### Phase C — Official conformance (pinned source integrated)

Stable release `v0.1.16` lacks `--requirements`; v0.20 uses official source commit `232a9014457eaf4c59559916e3d616d2f7f28f05`, builds it in CI, and runs both requirement sets against the production-shaped target. Preserve a report and exit code for each full set, even when one lacks `checks.json`, then assess report integrity after both attempts. Full-suite exit codes are report evidence; hard-gate reviewed applicable scenarios using the pinned CLI exit code and require `checks.json`, alongside repository real-wire regressions. Any new applicable required failure blocks; applicability changes require human review and versioned evidence. Never use floating `main`/`latest` or `--expected-failures`.

### Phase D — Registry-ready metadata and CI

Implemented and locally accepted: added schema-validated `server.json`, OCI name label, version/name consistency check, vendored schema and offline CI validation. A temporary-filesystem reproduction confirmed the initial smoke's cause: with slot01 aliased and slots02..16 absent, registry construction raises `WorkdirError`; sentinel-only directories for those slots construct the expected read-only `workspace` registry. The image now creates `/workdirs/01..16` with only `.serverfs-disabled`, owned by UID/GID 10001 and without write permission. Real workspace bind mounts cover the image defaults. `server.json` no longer declares the unused `WORKDIR_01_PATH`; the read-only bind, alias, `READ_ONLY`, and HTTP Host/Origin settings remain. A scratch image built successfully; one named container stayed running and one direct root MCP client call returned alias `workspace`, access `read-only`; the container was then stopped and removed. Focused tests: 92 passed. Root Ruff check/format passed; full root pytest: 1691 passed, 317 skipped. Version lock checks and `git diff --check` passed. **Not verified:** Registry client OCI + Streamable HTTP installation/interoperability. Multi-architecture publish manifest verification remains in the existing publish workflow.

### Phase E — Cross-platform regression and stale-state cleanup

Run root and Bridge gates on their existing Linux/macOS/Windows workflows; verify stdio on each native family and HTTP on each currently supported native server. Retain platform lifecycle assertions. Update only false current docs/comments/fixtures; align English and Simplified Chinese. **Gate:** capability matrix has direct evidence or explicit unsupported cells, and residual scan hits are classified.

### Phase F — Release readiness

Run all applicable CI gates, build and inspect artifacts, review release README/site copy and rollback instructions. Produce a release acceptance report with conformance summaries, raw evidence locations, Registry schema revision, image digest/labels, platform runs and known gaps. **Gate:** every completion criterion in §16 is met before v0.20.0 is called complete.

## 11. Deterministic test matrix

Tests must avoid external account state and mutable network dependencies. Mock or fixture protocol peers for deterministic coverage; the official suite is separately pinned and run against an ephemeral local server.

| Test group | Deterministic cases | Expected evidence |
|---|---|---|
| Modern HTTP wire | no session handshake; required modern request metadata/headers; method/name mismatch; response and error framing | raw loopback captures; assertions against pinned 2026-07-28 spec |
| Legacy HTTP wire | initialize, negotiated protocol version, session ID propagation, missing/invalid session, version header | raw loopback captures; assertions against pinned 2025-11-25 spec |
| Cross-revision routing | each valid version selects only its own behavior; malformed/unknown version; no silent downgrade | table-driven route/error tests |
| MCP surface parity | tools/list schemas/output schemas and representative calls across both revisions | normalized results equal where behavior is intended to match |
| stdio | launch actual CLI subprocess, initialize/call, stdout framing only, stderr diagnostics | Linux/macOS/Windows native subprocess tests |
| API Key | missing/wrong/correct key over real HTTP; key never accepted through stdio or query unless existing contract explicitly supports it | existing semantics preserved; sanitized captured output |
| OAuth | invalid/expired/audience/scope cases and successful configured test principal; modern/legacy HTTP dispatch | existing resource-server tests plus real loopback integration; no live IdP dependency |
| Registry manifest | schema-valid manifest, invalid field, version/name mismatch, wrong/missing image annotation, mutable tag policy | offline validator unit/CLI tests |
| OCI artifact | labels present and correct; both published Linux architectures; immutable digest metadata | image config/manifest inspection output |
| Compatibility/security | Host/Origin denied/allowed, DNS rebinding protections wired, listener unpublished, clean logs, no secrets | existing regression suites plus focused assertions |
| Stale-state scan | current English/Chinese docs alignment, historical hits classified, no stale current protocol claim | saved command/output or CI summary |

Exact modern header requirements, conformance scenario applicability and Registry schema fields remain probe-driven; tests must encode the verified final sources, not this plan's provisional prose.

## 12. CI and release gates

### Pull request CI

- Root: `uv sync --frozen`, Ruff lint/format, root pytest, relevant Compose render gates, scratch-tag container build.
- Bridge: `agent_bridge` frozen sync, Ruff lint/format, Bridge pytest.
- MCP conformance: CI builds official source commit `232a9014457eaf4c59559916e3d616d2f7f28f05`, runs both sets independently and retains each report/exit code. Hard-gate reviewed applicable scenarios using the pinned CLI exit code and require `checks.json`; also run repository real-wire regressions. Preserve the measured scenario results and `SKIPPED`/`SHOULD` distinctions in `docs/v0.20.0-phase-a-probes-2026-10-21.md`; do not describe SHOULD warnings as mandatory behavior passing. Construct the no-workdir target directly, use exact test-only Host/Origin values, and do not relax production defaults. Never use `--expected-failures`. Run OAuth as a separate configuration where applicable.
- Registry metadata: offline schema and consistency validation; inspect built OCI config/labels. Do not call authenticated publish APIs.
- Native workflow jobs: preserve existing macOS 27 arm64 assertions and Windows native artifact/runtime gates; add wire/stdio checks only to actual supported native transports.
- Site workflow: `npm ci` + `npm run build` for site changes; source English/Chinese current-state alignment review and stale scan for release-state changes.
- `git diff --check`; verify no generated artifacts/secrets were added.

### Release tag gate

- Root project version, Bridge/native version gates, `server.json`, OCI labels/tag templates and release tag agree exactly.
- Linux image builds for existing `amd64` and `arm64` targets; verify manifest and digest before publication evidence is accepted.
- Windows wheel release and existing Plugin asset workflow remain intact and version-aligned; no Registry publication replaces GitHub Release assets.
- Official conformance report artifacts are retained for both requirement sets and applicable auth configurations.
- Release README/site copy is reviewed against the capability matrix; site build is green.
- No tag is created/moved and no release is published until the maintainer explicitly performs the repository release operation under the existing workflow.

## 13. Platform acceptance boundaries

### Linux

Use the current Linux OCI deployment. Acceptance must run the modern and legacy HTTP wire/conformance cases on the actual container port through loopback or the documented generic HTTP override, with production Host/Origin controls enabled. Exercise API Key and OAuth as separate configurations where supported. Retain complete reports from the pinned official requirement sets as evidence; hard-gate only reviewed applicable scenarios/checks plus repository real-wire regressions. For the DNS scenario, use a test-only allowlist that includes the suite positive Host authority; do not relax production defaults. Confirm base Compose still publishes no MCP port, HTTP exposure remains opt-in, and Tunnel topology is unaffected. Build the release image under a scratch tag, inspect labels and `amd64`/`arm64` manifest, and verify no conformance tooling entered the image/runtime dependency set. Existing root and Bridge full suites remain mandatory.

### macOS

Acceptance is on Apple M-series, native arm64, macOS 27 only. Run root and Bridge suites with native interpreter assertions, build/inspect the wheel's bundled Service Manager sources under existing gates, and launch stdio as a real native process to verify framing and clean stdout. If native Streamable HTTP is currently supported, run both protocol wire paths and applicable auth tests without widening the supported OS range. Verify existing app-associated `SMAppService` lifecycle is unchanged; no hosted CI mutation of Login Items. Do not claim Intel, Rosetta, macOS 26/28 or a `darwin-amd64` asset.

### Windows

Acceptance runs on native Windows, not WSL/Docker as a substitute. Run root and Bridge Windows suites, release wheel isolation/artifact gates, actual stdio subprocess framing, and both HTTP wire paths only where the current native command supports them. Verify current-user, non-elevated Task Scheduler 2.0 service behavior remains unchanged and no hosted test installs/mutates a persistent scheduled task. Preserve the documented Windows revision-token limitations and current-user configuration/secret boundaries.

### Linux-only distribution claim

Registry-ready OCI metadata covers the existing Linux image only. It does not imply that Windows/macOS wheels are installable through the OCI package entry or that the Registry is required to discover/download them.

## 14. README, website and release documentation

Update `README.md` and current product docs to state:

- supported MCP protocol revisions and which transport modes are measured;
- modern and legacy Streamable HTTP compatibility, without claiming unsupported features;
- clear distinction among stdio, API Key HTTP and OAuth HTTP boundaries;
- the Linux OCI registry-ready metadata/discovery route as optional preview-era distribution, alongside direct GHCR/Compose, Tunnel, Windows wheel and supported macOS installation paths;
- exact supported platform limits and the fact that registry metadata does not auto-install or authorize an executable.

Keep English root site and Simplified Chinese `/zh-cn/` pages semantically aligned. Update configuration/security/architecture/getting-started pages only where current-state claims change; preserve shared `HomePage.astro`, project base, Starlight/i18n structure and self-hosted font behavior. Update release notes/checklists only if they are current release surfaces. Historical v0.19 and earlier records stay historical.

Run `cd site && npm ci && npm run build`, root `git diff --check`, and the managed-doc stale scan. Static build validates routes/syntax only; if CSS/layout changes are introduced, rendered geometry needs browser evidence. Prefer no visual layout change in this release.

## 15. Planning evidence: official upstream references

URLs below are research inputs, not implicit dependencies. Recheck at Phase A because the Registry preview and conformance suite can change.

- MCP specification 2025-11-25: `https://modelcontextprotocol.io/specification/2025-11-25`
- MCP specification draft/current revision documentation: `https://modelcontextprotocol.io/specification/draft`
- MCP 2026-07-28 release announcement and migration context: `https://blog.modelcontextprotocol.io/posts/2026-07-28-release-candidate/`
- Official MCP Conformance suite and frozen `--requirements` contract: `https://github.com/modelcontextprotocol/conformance`
- Official Registry repository and preview implementation/docs: `https://github.com/modelcontextprotocol/registry`
- Generic `server.json` format reference: `https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/generic-server-json.md`
- Official Registry-specific `server.json` constraints: `https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/server-json/official-registry-requirements.md`
- OCI package type and `io.modelcontextprotocol.server.name` annotation: `https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/package-types.mdx`
- Official Registry API / validation endpoint documentation: `https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md`
- MCP Tasks extension, cited only to freeze it out of scope: `https://tasks.extensions.modelcontextprotocol.io/specification/draft/tasks`
- ACP v1 repository and current protocol baseline: `https://github.com/agentclientprotocol/agent-client-protocol`
- ACP v2 draft announcement, cited only to freeze it out of scope: `https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/announcements/acp-v2-draft.mdx`

At implementation time, use immutable release/spec tags where available. `main`, `draft`, blog and preview URLs can move; pin the exact evidence revision in the implementation record and do not treat an announcement as normative wire detail.

## 16. Rollback strategy

Protocol changes must remain additive and independently routable. If modern-path implementation or conformance fails, ship no partial modern claim: retain the v0.19 legacy behavior, revert the new route/adapter as one unit, and leave the prior endpoint/auth configuration operational. Do not downgrade malformed modern requests silently.

Registry metadata is additive. If preview validation fails or Registry service is unavailable, remove/disable only the optional publication step or correct metadata; direct GHCR/Compose and established platform channels remain. Do not remove an image tag or alter an immutable release to roll back a metadata error.

For a release regression, publish a new patch release through the existing immutable-tag workflow. Do not move/recreate a stable tag. Preserve previous container tags and Windows/macOS artifacts as the operator rollback target. Deployment rollback follows existing platform docs: pin prior image/wheel, preserve config and service registration, and re-establish Tunnel/session lifecycle as documented. No data migration is planned; if implementation discovers persisted-state changes, release is blocked until a separately reviewed forward/backward-compatible migration and rollback plan exists.

## 17. Explicit completion standard

v0.20.0 is complete only when all are true:

1. No implementation detail depends on an unverified API/header/schema; all Phase A probes are resolved and recorded.
2. CI builds the official immutable Conformance source commit `232a9014457eaf4c59559916e3d616d2f7f28f05` because stable `v0.1.16` lacks `--requirements`; every run retains both reports/exit codes and passes the reviewed applicable hard-gate scenarios plus repository real-wire regressions. Any newly applicable required failure blocks, applicability changes receive human review plus versioned evidence, and no expected-failures baseline manufactures a passing score.
3. Real loopback wire evidence demonstrates both HTTP lifecycles and deterministic malformed/mismatch behavior; stdio has native subprocess evidence and remains protocol-clean.
4. API Key and OAuth HTTP boundaries are tested independently; neither changes stdio behavior or bypasses production transport protections.
5. Linux/macOS/Windows acceptance is complete within the exact boundaries in §13, or any unsupported cell remains explicitly unsupported in release copy.
6. `server.json` validates against the pinned official schema; all names/versions/package references agree; built OCI artifacts contain the matching annotation and measured architecture manifest; CI validation is offline and release-gating.
7. Registry preview is not a runtime/install/release prerequisite; direct existing distribution channels remain documented and usable.
8. English and Simplified Chinese current-state copy is aligned; stale search hits are classified; historical evidence is preserved; site build and relevant repository gates are green.
9. Security, compatibility and rollback reviews find no widening of authority or weakening of existing invariants.
10. Release acceptance evidence is assembled before release tagging/publication, which remains a separate explicit maintainer action.

If any item fails, the release status remains incomplete; do not turn a planned claim into a present-tense support statement.
