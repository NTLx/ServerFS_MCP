# ServerFS v0.15.0 Development Plan

Status: implementation in progress.

## Implementation checkpoint — 2026-10-19

Completed and verified so far:

- repository synchronized with `origin/main` before v0.15 edits;
- exact MCP SDK pin updated to `mcp==2.3.0`; `uv.lock` changed only `mcp` and `mcp-types` from 2.2.0 to 2.3.0;
- static ServerFS MCP catalog now advertises `subscriptions=False`;
- HTTP host/port/path/Host allowlist/Origin allowlist are runtime-configurable while DNS-rebinding protection remains mandatory;
- existing OpenAI Tunnel defaults remain compatible, including non-default HTTP port/path propagation;
- opt-in `compose.http.yml` publishes generic MCP HTTP on loopback by default;
- base and generic Compose shapes render successfully; generic HTTP starts only `serverfs-mcp` unless the `openai-tunnel` profile is explicitly enabled, while the historical base Compose shape still starts the Tunnel by default;
- focused HTTP/config/security tests are green;
- modern MCP `2026-07-28` and legacy `2025-11-25` clients expose the same 11-tool base surface in-process and over a real transient TCP Streamable HTTP endpoint;
- real Qoder CLI interoperability is verified: Qoder `1.1.65` consumed the transient Streamable HTTP endpoint in a strict headless session, invoked only `mcp__serverfs__list_workdirs`, and received the expected empty workdir list with exit status 0;
- real Codex CLI interoperability is verified: Codex `0.163.0` consumed the transient Streamable HTTP endpoint with ServerFS MCP tool approval explicitly set to `approve`, invoked only `list_workdirs`, and received the expected empty workdir list with exit status 0;
- bounded root regression batches are green: HTTP/config/security `91 passed`; deployment/binary `129 passed, 8 skipped`; core filesystem `625 passed, 3 skipped`; Agent/shared lifecycle `365 passed, 167 skipped`; remaining platform/package/CLI paths `208 passed, 138 skipped`. These batches intentionally contain some overlap and are evidence by boundary rather than an additive suite total.

Still pending before v0.15.0 acceptance:

- native Windows/macOS live shared-core validation on the corresponding hosts (Linux platform-specific skips are not substitutes);
- repeatable real-host interoperability with Claude Code. Current transient Claude Code `2.1.302` probes are **Not verified**: its `mcp list` management subcommand does not surface session-only `--mcp-config` servers on this host, and the first prompt probe did not load the temporary tool. This is not evidence of a ServerFS protocol failure because official Python clients plus Qoder and Codex all consume the same transient endpoint successfully;
- ChatGPT/OpenAI Tunnel regression including optional file ingress/Agent surfaces;
- release-facing README/docs/site convergence and final release gates.

The full monolithic root `pytest` was intentionally not left running when it produced no incremental evidence within the short baseline window; validation is being executed in bounded domain batches instead. This is not recorded as a passing full-suite gate.

## Goal

Make ServerFS a **standards-first, vendor-neutral MCP server** that can be consumed directly by any compatible MCP host or harness through standard MCP transports, while preserving the existing OpenAI Secure MCP Tunnel as an optional ChatGPT ingress and preserving all existing filesystem / binary / Agent Bridge security invariants.

v0.15.0 is an ingress and interoperability release. It is **not** a rewrite of the filesystem core, Agent Bridge, platform-native backends, or tool contract.

The central architectural change is conceptual and operational:

```text
Before

ChatGPT / OpenAI
      |
OpenAI Secure MCP Tunnel
      |
ServerFS MCP

After

Claude Code ─┐
Codex ───────┤
Qoder ───────┤
MCP harness ─┼─ stdio / Streamable HTTP ─┐
other hosts ─┤                            |
ChatGPT ─────┤                            v
             └─ OpenAI Tunnel (optional) -> ServerFS MCP Core
```

The OpenAI Tunnel becomes one ingress adapter rather than the architectural center of ServerFS.

## Non-negotiable invariants

v0.15.0 must preserve the existing project security model:

- explicit configured workdirs only; host paths remain hidden from MCP callers;
- read-only by default and per-workdir opt-in mutation;
- no generic shell, arbitrary command execution, recursive delete, force overwrite, or unrestricted filesystem access;
- revision-guarded destructive mutations and existing Agent writer-lease semantics remain unchanged;
- binary transfer remains separately opt-in and bounded;
- ChatGPT/OpenAI file ingress remains separately opt-in and isolated from the main MCP process;
- Agent delegation remains separately opt-in and allowlisted per workdir/runtime;
- Linux ServerFS MCP does not gain general Internet egress merely to support generic MCP clients;
- DNS-rebinding Host protection remains enabled; generic MCP support must configure the allowlist, not disable the protection;
- public Internet exposure must terminate TLS and authentication at an explicitly configured ingress/gateway boundary; ServerFS must never publish an unauthenticated wildcard listener by default;
- Windows/macOS remain current-user scoped and non-elevated;
- existing OpenAI Tunnel deployments remain supported without requiring a flag day migration.

## Protocol and SDK baseline

v0.15.0 targets the MCP **2026-07-28** protocol revision while retaining the Python SDK's built-in compatibility with earlier MCP clients.

Dependency policy:

```text
mcp==2.3.0
```

The exact pin is intentional. ServerFS is infrastructure with a security-sensitive protocol boundary; SDK upgrades are reviewed, locked, tested, and released explicitly rather than floating within a major/minor range.

`mcp==2.3.0` remains an SDK version, not a protocol version.

The SDK currently does not provide the 2026-07-28 Tasks extension as a release-ready replacement for ServerFS Agent Bridge task RPC. v0.15.0 therefore keeps the existing explicit `task_id` Agent tool model unchanged.

## Supported MCP transports

ServerFS v0.15.0 formally supports exactly two standard transports:

1. **stdio** — local/native client integration;
2. **Streamable HTTP** — remote/client-server integration.

No new legacy SSE server surface is added. No WebSocket MCP transport is added.

Existing SDK compatibility with older clients is allowed to operate internally, but ServerFS documentation and acceptance target stdio + Streamable HTTP.

## Client / host compatibility target

The release target is host-neutral MCP semantics, with explicit live acceptance for:

| Host / harness | stdio | Streamable HTTP | v0.15 release target |
| --- | ---: | ---: | --- |
| MCP Python SDK Client / protocol harness | yes | yes | mandatory baseline |
| MCP Inspector / conformance tooling where practical | yes | yes | mandatory protocol gate |
| Claude Code | yes | yes | live interoperability |
| Codex | yes | yes | live interoperability |
| Qoder | yes | yes | live interoperability |
| ChatGPT | n/a for remote product path | yes via existing Tunnel; direct standard HTTP may also work | regression acceptance |

A client-specific ServerFS tool implementation is forbidden. All hosts consume the same `create_server()` tool/resource contract.

## Product layering

v0.15.0 makes the following layers explicit:

```text
Client / Host
  ChatGPT / Claude Code / Codex / Qoder / Inspector / other MCP hosts
        |
        v
MCP ingress
  - stdio
  - generic Streamable HTTP
  - OpenAI Secure MCP Tunnel adapter (optional)
        |
        v
ServerFS MCP Core
  - create_server()
  - filesystem tools
  - binary tools
  - resources
  - Agent Bridge tools
        |
        v
ServerFS services / platform backends
  - workdir registry and path policy
  - Linux / Windows / macOS filesystem backends
  - mutation/revision logic
  - binary implementation
  - Agent Bridge / leases
```

Host-specific extensions may be additive at the ingress/tool metadata edge, but the portable MCP baseline must remain independently usable.

## Vendor-extension policy

### OpenAI file parameters

`openai/fileParams` remains supported as an optional ChatGPT convenience extension. It is not treated as part of the portable ServerFS binary contract.

Portable binary upload remains:

```text
upload_binary_file(data_base64=...)
```

The OpenAI-specific `file` parameter remains additive. v0.15.0 must not introduce parallel `claude/*`, `codex/*`, or `qoder/*` file-parameter dialects.

### Agent Bridge

Provider-native Agent runtimes (Codex / Claude / Qoder) are implementation targets of the optional Agent Bridge, not MCP host-specific ServerFS implementations. An MCP caller may itself be Claude Code while delegating a bounded task to Codex, and vice versa; the authorization boundary remains the ServerFS workdir/runtime policy.

## HTTP transport configuration

The current hard-coded HTTP transport security assumption:

```text
allowed_hosts = ["serverfs-mcp:8000"]
```

must become explicit runtime configuration while preserving that value as the Linux/OpenAI-Tunnel compatibility default.

Planned environment-facing settings:

```text
SERVERFS_HTTP_HOST                  default: 0.0.0.0
SERVERFS_HTTP_PORT                  default: 8000
SERVERFS_HTTP_PATH                  default: /mcp
SERVERFS_HTTP_ALLOWED_HOSTS         default: serverfs-mcp:8000
SERVERFS_HTTP_ALLOWED_ORIGINS       default: empty
```

Rules:

- DNS-rebinding protection remains enabled and is not given a casual disable switch;
- allowed Host/Origin values are explicit comma-separated allowlists with whitespace removed and empty items ignored;
- the default preserves the existing OpenAI Tunnel Docker path exactly;
- a generic deployment must explicitly add its real reverse-proxy/public Host value;
- invalid host/port/path configuration fails closed at startup;
- `/mcp` remains the default endpoint;
- request-body sizing continues to account for the largest enabled binary base64 upload.

## HTTP publication and TLS boundary

The base Linux Compose deployment keeps `serverfs-mcp:8000` un-published by default.

v0.15.0 adds an explicit generic HTTP publication shape, preferably as a small Compose override rather than weakening the base deployment. The default publication binds loopback only:

```text
127.0.0.1:<configured-port> -> serverfs-mcp:8000
```

A public deployment is expected to be:

```text
MCP client
   |
HTTPS + authentication
   v
operator-managed reverse proxy / access gateway
   |
private/loopback HTTP
   v
ServerFS MCP
```

The reverse proxy must preserve or deliberately set a Host value present in `SERVERFS_HTTP_ALLOWED_HOSTS`.

ServerFS does not ship a universal TLS certificate manager or identity provider in v0.15.0.

## Authentication boundary

v0.15.0 deliberately does **not** couple the ServerFS core to Auth0, Keycloak, Entra, or any other identity provider, and it does not grant the main Linux MCP process general Internet egress for token introspection/JWKS discovery.

For the v0.15.0 single-trusted-operator model:

- stdio relies on the local OS/user process boundary;
- loopback/private generic HTTP relies on the explicitly controlled network boundary;
- Internet-facing generic HTTP requires an upstream TLS/auth gateway;
- the existing OpenAI Tunnel retains its current authenticated control-plane boundary for ChatGPT.

The Python MCP SDK's OAuth 2.1 Resource Server APIs remain a future-compatible extension point. A later release may add provider-neutral built-in verification if it can do so without weakening ServerFS's network isolation or creating provider-specific product coupling.

Multi-user identity-to-workdir authorization is explicitly out of scope for v0.15.0.

## MCP capability advertisement

ServerFS has a static tool/resource catalog for a process lifetime. v0.15.0 should instantiate `MCPServer(..., subscriptions=False)` so modern clients are not told that change-notification subscriptions are available when ServerFS never changes the catalog dynamically.

This is capability accuracy, not a feature removal for existing 2025-era clients.

No MCP Tasks extension is advertised or emulated.

## Phase 0 — Baseline and MCP SDK update

1. synchronize `main` with `origin/main` and require a clean worktree;
2. freeze this plan in `dev_plan_v0.15.md`;
3. update the exact dependency pin from `mcp==2.2.0` to `mcp==2.3.0`;
4. regenerate `uv.lock` through `uv`, never by hand;
5. verify only expected dependency deltas occurred;
6. run root lint/format/tests;
7. run focused MCP server tests;
8. confirm existing OpenAI Tunnel / Streamable HTTP behavior is unchanged before any transport refactor.

Success criterion: a pure SDK update is green before generic-ingress code begins.

## Phase A — MCP core portability cleanup

1. keep `create_server()` as the single host-neutral server factory;
2. instantiate the server with `subscriptions=False`;
3. remove host-specific wording from portable code comments/descriptions where it incorrectly implies ChatGPT is the caller;
4. keep `openai/fileParams` explicitly isolated as optional metadata and preserve `data_base64` as the portable binary path;
5. add tests that the normal filesystem/Agent tool surface is identical independent of HTTP ingress choice;
6. do not change public tool names or filesystem semantics.

Success criterion: the in-process/stdio MCP core contains no required ChatGPT/OpenAI dependency.

## Phase B — Configurable Streamable HTTP security and listener

1. add HTTP host/port/path/allowed-host/origin settings to `Settings`;
2. add strict parsing/validation tests;
3. replace the module-level OpenAI-specific `STREAMABLE_HTTP_TRANSPORT_SECURITY` constant with a function derived from effective settings;
4. keep DNS rebinding protection enabled;
5. wire configured host/port/path into `mcp.run("streamable-http", ...)`;
6. preserve current default behavior exactly for the OpenAI Tunnel container;
7. log only non-secret effective listener/security metadata;
8. add tests for default compatibility, generic real-host allowlisting, bad values, and empty Origin policy.

Success criterion: `serverfs-mcp:8000` continues to work unchanged, while a configured `mcp.example.com` Host can be accepted without disabling transport security.

## Phase C — Generic HTTP deployment shape

1. add a minimal Compose override for direct/generic MCP publication;
2. bind `127.0.0.1` by default rather than `0.0.0.0`;
3. allow an operator to change the publish address deliberately for a private interface such as Tailscale/LAN;
4. do not give `serverfs-mcp` general egress;
5. do not add Caddy/Nginx/Traefik as a mandatory project dependency;
6. document a small reference reverse-proxy contract: HTTPS termination, authentication, Host preservation, request-size compatibility, no cross-origin redirects;
7. keep the OpenAI Tunnel service path operational in parallel.

Success criterion: the same running ServerFS core can be reached through the existing OpenAI Tunnel and through an explicitly enabled standard Streamable HTTP endpoint.

## Phase D — Protocol and client interoperability gates

Add repeatable automated/local gates before real-host live acceptance:

1. use the MCP Python SDK client against Streamable HTTP with the modern 2026-07-28 path;
2. exercise a legacy/handshake-era compatibility path supported by the SDK;
3. verify `server/discover` / tool discovery and standard tool calls;
4. verify stdio where the platform deployment exposes it;
5. verify Host rejection for an unallowlisted Host;
6. verify configured Host acceptance;
7. verify binary body-size behavior remains correct;
8. verify OpenAI file metadata is additive rather than required;
9. exercise MCP Inspector/conformance tooling where practical without converting conformance-only optional extensions into ServerFS requirements.

Minimum portable tool flow:

```text
list_workdirs
list_directory
read_text_file
stat_file
```

Read-write acceptance additionally exercises revision-guarded create/edit/delete in a disposable workdir.

Agent-enabled acceptance additionally exercises runtime discovery and one narrow task without changing Agent Bridge semantics.

## Phase E — Real client live interoperability

Use disposable client configuration and a disposable ServerFS workdir where mutation is involved.

### Claude Code

- connect to the generic Streamable HTTP endpoint;
- discover ServerFS tools;
- read a file;
- perform one bounded mutation in a disposable read-write workdir;
- if Agent tools are enabled, submit/read one narrow task.

### Codex

Repeat the same host-neutral contract using Codex's MCP client support.

### Qoder

Repeat the same host-neutral contract using Qoder's MCP client support.

### ChatGPT regression

Through the existing OpenAI Secure MCP Tunnel:

- discover the same ServerFS surface;
- exercise read and guarded mutation;
- exercise binary upload/download where configured;
- exercise one Agent task where configured;
- confirm OpenAI `fileParams` ingress remains functional when enabled.

No client-specific ServerFS code change is accepted merely to make one host pass unless the change corrects a standards-level interoperability bug.

Success criterion: Claude Code, Codex, Qoder and ChatGPT consume the same ServerFS MCP core/tool semantics.

## Phase F — Cross-platform implementation and regression

Because `create_server()` is shared by Linux, Windows and macOS, v0.15.0 must not be released on Linux-only evidence. Platform-native work is executed only in the corresponding physical development environment; the VPS is not used to implement or live-validate Windows/macOS behavior.

### Platform workdir ownership and GitHub synchronization

The repository has three authoritative development workdirs for v0.15.0:

| Platform scope | Development environment | ServerFS workdir | Allowed v0.15 work |
| --- | --- | --- | --- |
| Linux + shared MCP core | VPS001 ServerFS | `ServerFS` | Linux Compose/deployment, protocol/core code, host-neutral tests/docs |
| Windows native | WorkPC ServerFS MCP | `serverfs-mcp` | Windows-native code/tests/lifecycle/live acceptance only |
| macOS native | MacBook ServerFS MCP | `serverfs` | macOS-native code/tests/lifecycle/live acceptance only |

Synchronization rules are mandatory:

1. GitHub `origin` is the **only** code synchronization channel between the three workdirs; never copy repository files directly between hosts.
2. Before changing a platform workdir, require a clean worktree, fetch `origin`, and fast-forward to the exact latest accepted `origin/main` checkpoint.
3. Shared/Linux changes are completed and validated on VPS first, committed, and pushed as a development checkpoint before Windows/macOS work starts.
4. Windows then pulls that checkpoint on WorkPC, performs only Windows-native adaptation/validation, commits and pushes its own checkpoint.
5. macOS then pulls the latest `origin/main` including the Windows checkpoint, performs only macOS-native adaptation/validation, commits and pushes its own checkpoint.
6. VPS finally fast-forwards to the combined latest `origin/main` before Linux regression, docs/site convergence and release closeout.
7. Do not develop conflicting shared-file changes concurrently on multiple hosts. If a native platform needs a shared-core change, make that need explicit, stop platform-local mutation, land the shared change from the designated shared/Linux workdir, push it, then fast-forward the native workdir and resume.
8. Platform-specific Agent runtimes are environment-bound: VPS Agent only operates the VPS workdir, WorkPC Agent only the WorkPC workdir, and MacBook Agent only the MacBook workdir.

This serial checkpoint model intentionally trades a small amount of parallelism for deterministic history and avoids cross-host merge drift.

### Linux

- Docker base deployment;
- generic HTTP override;
- OpenAI Tunnel regression;
- Agent Bridge regression.

### Windows

- existing native stdio/Tunnel lifecycle remains current-user/non-elevated;
- service install/start/stop/restart/status behavior unchanged;
- filesystem and Agent tests green;
- generic MCP core changes do not regress the v0.14 persistence model.

### macOS

- native stdio/Tunnel path remains current-user/non-elevated;
- SMAppService lifecycle remains unchanged;
- filesystem and Agent tests green.

Generic direct remote HTTP publication is a Linux deployment feature for v0.15.0 unless a native platform implementation is needed by a verified use case. Native Windows/macOS stdio support already satisfies the primary local MCP-client integration path and should not be complicated speculatively.

## Phase G — Documentation and product-positioning convergence

Update current-state documentation and site content so ServerFS is described as a generic MCP server first.

Required changes include:

- README headline and architecture diagram;
- Getting Started: generic MCP path first, OpenAI Tunnel as optional ChatGPT ingress;
- Configuration: generic HTTP settings and Host security;
- Architecture: separate MCP Core from ingress adapters;
- Security: public ingress/TLS/auth responsibility and no-public-bind default;
- Binary Transfer: portable base64 path vs OpenAI fileParams extension;
- Agent Bridge: caller is an MCP host/client, not implicitly ChatGPT;
- Windows/macOS docs: stdio as standard MCP integration path;
- English/Chinese site parity;
- `.env.example` and Compose examples;
- maintainer/release acceptance docs.

Historical plans/evidence remain historically accurate and are not rewritten merely to remove old ChatGPT terminology.

## Phase H — Release acceptance and closeout

Before tagging v0.15.0:

1. all root lint/format/test gates green;
2. Docker Compose configs render for base and generic HTTP shapes;
3. site build green;
4. stale current-state wording scan finds no claim that OpenAI Tunnel is mandatory;
5. MCP SDK exact lock is `2.3.0` and no unintended dependency drift is present;
6. protocol tests cover modern + legacy compatibility;
7. generic HTTP Host security rejection/acceptance is proven;
8. Claude Code live interoperability passes;
9. Codex live interoperability passes;
10. Qoder live interoperability passes;
11. ChatGPT/OpenAI Tunnel regression passes;
12. Linux Agent Bridge regression passes when enabled;
13. Windows and macOS shared-core regressions are green on their native environments;
14. release-facing README/docs/site state `v0.15.0` as released only in the final release commit so no post-tag documentation commit is required;
15. tag the verified immutable commit and publish the GitHub Release.

## Explicit non-goals

v0.15.0 does not:

- implement separate MCP servers for Claude, Codex, Qoder, or ChatGPT;
- add WebSocket transport;
- add a new legacy SSE deployment surface;
- replace Agent Bridge tools with MCP Tasks;
- implement multi-user RBAC or identity-to-workdir ACLs;
- bundle an OAuth authorization server or identity provider;
- add provider-specific JWT/OIDC verification to the filesystem core;
- give the Linux MCP core unrestricted Internet egress;
- remove OpenAI Secure MCP Tunnel;
- remove ChatGPT/OpenAI `fileParams` support;
- change filesystem mutation semantics;
- broaden deletion/write capabilities;
- refactor unrelated platform lifecycle code.

## Implementation discipline

Development proceeds phase-by-phase with the smallest reviewable changes.

- ChatGPT performs repository file inspection and edits directly through ServerFS whenever those tools are sufficient.
- Native Codex delegation is used only when an operation cannot be performed through structured ServerFS tools, such as Git synchronization, dependency lock regeneration, command/test execution, or a real CLI interoperability probe.
- Every delegated task must have one narrow objective, explicit mutation boundary, explicit stop condition, and evidence requirement.
- Long multi-purpose Agent tasks are forbidden.
- A failing approach is diagnosed from concrete output before a different fix is attempted.
- Development checkpoint commits and pushes are required when handing work from VPS → WorkPC → MacBook → VPS, because GitHub is the sole synchronization channel.
- Release tagging and GitHub Release publication remain forbidden until explicit release closeout; intermediate development pushes do not imply a release.

## Immediate implementation order

```text
Phase 0
  plan -> mcp 2.3.0 -> lock -> baseline tests
      |
Phase A
  capability accuracy / portable core
      |
Phase B
  HTTP config + transport security
      |
Phase C
  generic HTTP Compose publication
      |
Phase D
  automated protocol interoperability
      |
Phase E
  Linux/shared client interoperability + ChatGPT regression
      |
  commit + push Linux/shared checkpoint
      |
Phase F-Windows
  WorkPC fetch/fast-forward -> Windows native adaptation + acceptance
      |
  commit + push Windows checkpoint
      |
Phase F-macOS
  MacBook fetch/fast-forward -> macOS native adaptation + acceptance
      |
  commit + push macOS checkpoint
      |
VPS resync
  fast-forward combined main -> Linux final regression
      |
Phase G/H
  docs/site convergence -> release acceptance
```

The first implementation checkpoint is intentionally small: **SDK 2.3.0 + unchanged behavior + green tests**. No generic-ingress refactor begins until that checkpoint is proven.
