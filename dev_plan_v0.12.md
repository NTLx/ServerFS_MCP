# ServerFS v0.12.0 Development Plan — Linux network reachability, real interactive approval, and live result spooling

Status: **RELEASED / FROZEN v0.12.0 — implementation, live acceptance, deterministic tests, docs/site, platform CI, tag and GitHub Release complete**
Target: **v0.12.0**
Primary platform: **Linux deployment**
Baseline analyzed: `main` = `origin/main` = `a89df78744798ec6563c4df8b48364be359d20ec` (v0.11.0 released).
Windows v0.11 behavior is a non-regression boundary for this release; v0.12 work is Linux-first unless a shared provider-neutral fix necessarily touches common Bridge code.

## 1. Objectives

v0.12.0 closes four concrete Linux gaps:

1. **Jev proxy support** — Jev requests can optionally use the existing shared ServerFS HTTP proxy configuration without setting process-wide proxy environment variables.
2. **Real interactive approval** — provider-native approval requests must actually be reachable through the existing `waiting_for_approval` / `respond_agent_approval` flow instead of depending on a user's persistent provider policy that may be `never` / `bypassPermissions`.
3. **Real Claude spool positive path** — a live Claude task must be able to complete through `result_storage="spool"` and exact `read_agent_task_result` retrieval; acceptance must not rely on Claude naturally producing more than the current 256 KiB inline limit.
4. **Linux proxy completeness** — OpenAI Tunnel, Agent runtimes, and Jev each receive an independent `.env` switch deciding whether that subsystem uses the same existing proxy endpoint.

The release is complete only when all four are proven with deterministic tests and Linux live acceptance.

## 2. Verified baseline facts

### 2.1 Repository / deployment

- v0.11.0 root and Agent Bridge packages are at `0.11.0`.
- Linux OpenAI Tunnel already receives `SERVERFS_PROXY_HOST/PORT/USERNAME/PASSWORD` and `deployment/tunnel/tunnel-launcher.sh` derives `CONTROL_PLANE_HTTP_PROXY` whenever `SERVERFS_PROXY_HOST` is non-empty.
- There is currently **no independent tunnel proxy enable switch**.
- Linux `deployment/agent-bridge/render_config.py` does not render the existing runtime `use_proxy` fields, and the user systemd unit starts the Bridge unsupervised with no runtime proxy bootstrap.
- `provider.env` can technically contain ambient `HTTPS_PROXY`, but that is not a feature-scoped contract and cannot provide independent Agent/Jev proxy control. v0.12 must not rely on ambient process proxy variables.

### 2.2 Jev

- `JevTaskPreflight.from_api_key()` constructs `typesafe_sdk.AsyncTypeSafeClient` with no proxy configuration.
- Installed Bridge environment: `typesafe-sdk==0.7.1`.
- The installed client accepts an explicit `http_client`; internally it otherwise creates `httpx2.AsyncClient`.
- Installed `httpx2==2.13.0` supports `AsyncClient(proxy=...)`.
- Therefore Jev can use a **per-client explicit proxy** without SDK upgrade, monkeypatching, or setting `HTTP_PROXY` / `HTTPS_PROXY` in the Bridge process.

### 2.3 Interactive approval

The provider-neutral state machine is already implemented:

- task state `waiting_for_approval`;
- persisted pending request;
- `respond_agent_approval` / `task.approval.respond`;
- adapter callback `context.request_approval()`;
- timeout, stale-request handling, cancellation and Jev Approval Advisor.

The missing part is provider-side policy activation:

- Codex adapter deliberately omits `approvalPolicy`; existing tests even assert it is absent.
- Installed Codex CLI `0.162.0` supports `approvalPolicy = "untrusted" | "on-request" | "never" | granular-object` on thread/turn parameters.
- Claude and Qoder adapters leave `permission_mode=None`, so persistent provider settings remain authoritative.
- Installed Claude SDK explicitly documents that `bypassPermissions` can bypass `can_use_tool`.

Thus the existing remote HITL plumbing is real, but provider configuration can prevent it from ever being exercised.

### 2.4 Result spool

Current actual limits are:

- inline final response: `<= 262144` bytes (256 KiB);
- spool: `> 262144` through `8 MiB`;
- result preview: `65536` bytes;
- `read_agent_task_result` chunk: max `65536` bytes;
- event payload: max `65536` bytes.

The often-mentioned 64 KiB value is **not** the current spool threshold; it is the preview/chunk/event limit.

A live Linux Claude `2.1.295` probe requested exactly 48,000 ASCII characters but completed with only **3,349 bytes**. This does not establish a provider hard limit, but it proves that acceptance cannot depend on prompt instruction alone to make Claude cross 256 KiB.

Additionally, Claude currently emits a complete `TextBlock` as an `agent.message` event. A sufficiently large block can hit the 64 KiB event limit before final-result spool logic runs. v0.12 must decouple event preview size from final-result retention.

## 3. Configuration contract

### 3.1 One shared proxy definition

Do **not** add another proxy hostname, port, username or password namespace.

The existing four fields remain the only operator-supplied proxy endpoint:

```dotenv
SERVERFS_PROXY_HOST=
SERVERFS_PROXY_PORT=
SERVERFS_PROXY_USERNAME=
SERVERFS_PROXY_PASSWORD=
```

v0.12 adds three independent booleans:

```dotenv
SERVERFS_OPENAI_TUNNEL_USE_PROXY=false
SERVERFS_AGENT_USE_PROXY=false
SERVERFS_JEV_USE_PROXY=false
```

Semantics:

- `false`: that subsystem must be direct even when shared proxy fields are populated;
- `true`: proxy configuration is required and validated; missing/invalid required fields fail closed before network activity;
- the three switches do not imply one another.

For upgrade compatibility only, the OpenAI Tunnel launcher may treat an **absent** `SERVERFS_OPENAI_TUNNEL_USE_PROXY` as the v0.11 legacy rule (`SERVERFS_PROXY_HOST` non-empty => enabled). `.env.example` and all v0.12 documentation must contain the explicit boolean so new/updated deployments stop relying on the compatibility fallback. Agent and Jev switches are new capabilities and default to `false` when absent.

### 3.2 Authentication boundary

The existing shared proxy may contain username/password.

- **OpenAI Tunnel:** authenticated proxy remains supported; credentials are scoped to the tunnel container as today.
- **Jev:** authenticated proxy is supported through the explicit in-memory `httpx2.AsyncClient(proxy=...)` client.
- **Agent runtimes:** v0.12 supports the shared proxy only when it is credentialless. If `SERVERFS_AGENT_USE_PROXY=true` and proxy username/password is configured, startup/config rendering must fail closed with a redacted explanation.

Reason: Claude/Qoder/Codex provider processes can expose their environment to agent-executed tool processes. Injecting proxy credentials would therefore expose those credentials to Agent code. Do not regress the v0.11 trust boundary. An authenticated enterprise upstream can still be used through an operator-managed **credentialless local broker**; implementing a ServerFS credential broker is out of scope for v0.12.

### 3.3 Configurable spool threshold

Add one deployment setting:

```dotenv
SERVERFS_AGENT_RESULT_SPOOL_THRESHOLD_BYTES=262144
```

- default remains `262144`, preserving v0.11 behavior;
- must be a positive integer and must not exceed the fixed maximum spooled result limit (`8 MiB`);
- `result_preview_bytes` becomes `min(65536, threshold)` so low test/acceptance thresholds remain internally valid;
- this is a storage threshold, **not** a provider output cap.

Production defaults do not change. Live acceptance may start the Bridge with a low threshold (for example 1024 or 2048 bytes) so a normal real Claude response can deterministically enter the spool path.

## 4. Phase A — Shared Linux proxy configuration

### A1. Strict shared proxy parser

Create one Python-side representation for the existing four shared fields:

- host;
- validated integer port;
- optional username/password;
- canonical HTTP proxy URL with percent-encoded userinfo;
- redacted `repr` / errors.

Reuse the same validation semantics already proven by `native_tunnel.proxy_url()` / `tunnel-launcher.sh`; do not invent a second meaning for the same `.env` fields.

`deployment/agent-bridge/render_config.py` should consume the three booleans plus the existing proxy fields and render only the Bridge material required by Agent/Jev. The generated config is private mode 0600 already and already contains the optional Jev API key; nevertheless errors/logs/tests must never echo proxy credentials.

### A2. Linux Bridge config

Extend Bridge configuration with:

- a private normalized shared proxy object (or canonical URL plus credential-presence metadata);
- `codex.use_proxy`, `claude.use_proxy`, `qoder.use_proxy` driven from `SERVERFS_AGENT_USE_PROXY`;
- `jev.use_proxy` driven from `SERVERFS_JEV_USE_PROXY`;
- result spool threshold in limits.

Rules:

- when Agent and Jev proxy switches are both false, no proxy object is required in the Bridge config;
- when either is true, shared proxy endpoint must validate;
- Agent enabled + authenticated shared proxy => refuse configuration;
- no standard proxy variable is added to the Bridge process environment.

### A3. Non-regression

Windows v0.11 private bootstrap remains authoritative for Windows runtime proxy material. Linux config wiring must not cause Windows to persist/use a second runtime proxy path. Shared constructors may accept the new normalized object, but Windows behavior and tests remain unchanged.

## 5. Phase B — OpenAI Tunnel explicit proxy switch

Update:

- `.env.example`;
- `compose.yml`;
- `deployment/tunnel/tunnel-launcher.sh`;
- `tests/test_tunnel_proxy_deployment.py`.

Required behavior:

1. explicit `false` => `CONTROL_PLANE_HTTP_PROXY` is absent even if proxy fields are populated;
2. explicit `true` => derive the exact current proxy URL and export only `CONTROL_PLANE_HTTP_PROXY` to the tunnel client;
3. explicit `true` + missing/invalid proxy => exit before tunnel-client starts;
4. invalid boolean => exit with redacted configuration error;
5. absent switch => v0.11 compatibility fallback only;
6. proxy credentials remain scoped to `openai-tunnel`, never `serverfs-mcp` or file-ingress containers.

Add a deterministic fake tunnel-client test for all three states: direct, proxied, invalid.

## 6. Phase C — Linux Agent Runtime proxy

### C1. Claude

Use the existing runtime proxy/environment builder.

When `SERVERFS_AGENT_USE_PROXY=true`:

- create a credentialless `RuntimeProxy` from the shared endpoint;
- set Claude runtime `use_proxy=true`;
- inject only the provider child overlay (`HTTPS_PROXY` + mandatory loopback `NO_PROXY`);
- keep the Bridge process environment proxy-free.

When false, omit the overlay exactly as today.

### C2. Qoder

Reuse the existing Qoder proxy seam (`client.set_proxy()` / runtime environment scrub) with the shared normalized endpoint. No new provider-specific proxy variable is introduced.

### C3. Codex — do not rely on an already-running managed daemon

This is the Linux-specific blocker.

Current Linux Codex connects to the user's managed daemon Unix socket. Passing a proxy environment to `codex app-server daemon start` only proves the start command received it; if a daemon is already running, Bridge cannot prove that daemon has the requested proxy environment.

Therefore when `SERVERFS_AGENT_USE_PROXY=true`, v0.12 must **not** use the shared managed daemon path.

Preferred implementation:

- start a Bridge-owned standalone `codex app-server --listen unix://<private-socket-path>` process;
- place the socket below Bridge private state/runtime storage;
- start the process with the already-scrubbed runtime environment plus credentialless proxy and `CODEX_HOME`;
- wait boundedly for socket readiness;
- reuse the existing Unix-socket `CodexConnection` transport rather than adding a new public protocol;
- keep one Bridge-owned app-server for the adapter lifetime if multi-client behavior is verified; otherwise use a bounded per-task child as the fallback implementation;
- terminate/reap the owned process on Bridge close and on failed startup; remove stale socket state safely.

First implementation test must prove whether standalone Unix-listen app-server accepts multiple simultaneous Bridge connections. If yes, use one child per Bridge; if not, use one child per active task rather than redesigning RPC multiplexing.

When `SERVERFS_AGENT_USE_PROXY=false`, preserve the existing Linux managed-daemon path exactly. Do not restart or mutate the user's shared daemon merely to change proxy policy.

### C4. Runtime proxy acceptance

For each runtime, prove both directions:

- proxy disabled: provider connection succeeds through a direct-capable harness and proxy observer sees zero provider requests;
- proxy enabled: direct route is made unavailable, provider task succeeds only through a controlled local forward proxy, and the observer sees the expected CONNECT/HTTP traffic;
- Bridge environment and Agent tool-visible environment contain no `SERVERFS_PROXY_*` / `SERVERFS_AGENT_*` credentials;
- Agent runtime proxy with username/password configured is rejected before provider start.

## 7. Phase D — Jev explicit proxy

Change `JevTaskPreflight.from_api_key()` to accept an optional normalized proxy URL.

When enabled:

```python
http_client = httpx2.AsyncClient(proxy=proxy_url, timeout=...)
AsyncTypeSafeClient(..., http_client=http_client)
```

Use the installed SDK's supported `http_client` seam. Do not:

- upgrade `typesafe-sdk` solely for proxy support;
- set `HTTP_PROXY`, `HTTPS_PROXY` or `ALL_PROXY` on `os.environ`;
- monkeypatch SDK internals;
- alter Jev model, retry policy, authority or fail-open semantics.

When `SERVERFS_JEV_USE_PROXY=false`, Jev must remain direct even if Agent or Tunnel proxy is enabled.

Tests must use a fake/local HTTP proxy and fake Jev transport/server where practical, proving:

- direct vs proxied routing;
- optional authenticated proxy URL construction;
- no credential in logs/config errors/MCP results;
- Jev network failure stays advisory/fail-open and never blocks Agent submission/model discovery.

## 8. Phase E — Real interactive approval

No new MCP tool or Bridge RPC is needed. Keep the existing provider-neutral request state machine and make provider policies explicit.

### E1. Codex

Set `approvalPolicy="on-request"` for every ServerFS-owned Codex turn (and any required thread start/resume parameter supported by the current protocol).

Update mock-provider assertions from "approvalPolicy absent" to the exact expected policy.

ServerFS must not use `never`, must not auto-answer provider approval requests, and must not change the public decision vocabulary.

### E2. Claude

Set `ClaudeAgentOptions.permission_mode="default"` explicitly whenever ServerFS supplies `can_use_tool`.

This prevents a persistent `bypassPermissions` setting from silently disabling the remote callback while preserving provider-native allow/deny rules: tools already allowed by native rules may still proceed without a prompt; native `ask` decisions must reach ServerFS.

### E3. Qoder

Set `QoderAgentOptions.permission_mode="default"` explicitly and validate with the installed SDK/runtime that an `ask` decision invokes `can_use_tool`. If live validation disproves that assumption, stop and adapt to the provider-supported interactive mode rather than emulating approval outside the SDK.

### E4. Approval acceptance contract

For Codex, Claude and Qoder where the provider can emit a native request:

1. submit a task designed to require provider approval;
2. observe `status=waiting_for_approval` and a normalized pending request;
3. confirm offered decisions;
4. respond `approve_once` through public `respond_agent_approval`;
5. provider action completes and task succeeds;
6. repeat with `deny` and prove the action is not executed;
7. prove timeout/cancel paths still stale/interrupt correctly;
8. if Jev is enabled, Approval Advisor remains advice only and `automatic=false`.

At least Codex and one SDK runtime must pass real-provider Linux E2E before release. Target is all three.

## 9. Phase F — Claude-reachable result spool

### F1. Configurable inline/spool boundary

Wire `SERVERFS_AGENT_RESULT_SPOOL_THRESHOLD_BYTES` through Linux `.env` -> rendered Bridge config -> `BridgeLimits.max_final_response_bytes`.

Default stays 256 KiB.

### F2. Large message event must not kill the final result

Keep full provider result data for final-result handling, but bound event evidence separately.

For `agent.message` events whose UTF-8 JSON payload would exceed `max_event_bytes`:

- emit a UTF-8-safe preview only;
- include `truncated=true`, original `size_bytes`, and SHA-256 (or equivalent bounded metadata);
- never modify the adapter's full in-memory final response merely to fit an event;
- keep small event payload shape backward-compatible.

Do not silently truncate arbitrary approval/question/tool payloads; their existing hard validation remains.

### F3. Real Claude positive path

Live Linux acceptance starts an isolated candidate Bridge with a low spool threshold (for example 1024/2048 bytes) while leaving the production default unchanged.

Then:

1. submit a no-tool Claude prompt that reliably returns more than the configured threshold;
2. task must succeed, not fail for event size;
3. `get_agent_task` must report `result.storage="spool"`, `retrievable=true`, exact size and SHA-256;
4. `final_response_truncated=true` and preview is bounded;
5. reconstruct the entire result with repeated `read_agent_task_result` calls;
6. reconstructed UTF-8 bytes must match `size_bytes` and SHA-256 exactly;
7. retention/GC still deletes the spool with its task.

The acceptance threshold is a test/deployment parameter, not a fake adapter and not a special provider code path.

## 10. Phase G — Deterministic test matrix

Minimum required deterministic coverage:

### Root / Linux deployment

- shared proxy bool parsing;
- tunnel true / false / legacy-unset compatibility;
- tunnel authenticated proxy encoding;
- Bridge config rendering for all three switches independently;
- no proxy fields reach unrelated Compose services;
- invalid switch/proxy combinations fail redacted;
- Agent authenticated proxy rejection;
- result spool threshold rendering/validation.

### Agent Bridge

- Jev explicit `http_client` proxy and direct mode;
- Jev close lifecycle closes the supplied HTTP client;
- Codex `approvalPolicy="on-request"` in start/resume/turn path as required;
- Claude/Qoder explicit `permission_mode="default"`;
- approval approve/deny/cancel/timeout regressions;
- Linux proxy-enabled Codex uses Bridge-owned standalone app-server, never the managed daemon endpoint;
- proxy-disabled Codex remains on the existing daemon path;
- Claude/Qoder child proxy injection and direct-mode absence;
- bounded large `agent.message` event + unmodified full result;
- configurable spool threshold, exact chunk reconstruction and UTF-8 boundary cases;
- default 256 KiB behavior remains unchanged.

### Full gates

- root `pytest`;
- Agent Bridge `pytest`;
- ruff for root and Bridge;
- existing Linux/container gates;
- existing Windows/native/Windows-Agent CI must remain green because common adapter/service code is touched.

No release proceeds with a Linux fix that regresses the v0.11 Windows contract.

## 11. Phase H — Linux live acceptance in a restricted-network shape

Build a controlled local HTTP forward proxy/observer and make the direct route unavailable for the target under test where practical.

Run these as separate assertions so one switch cannot accidentally satisfy another:

1. **Tunnel direct:** Tunnel proxy false; proxy observer sees no request.
2. **Tunnel proxy:** Tunnel proxy true; control-plane connectivity succeeds through proxy.
3. **Agent direct:** Agent proxy false; provider runs direct; no proxy observation.
4. **Agent proxy:** Agent proxy true; Claude, Qoder, and proxy-mode Codex succeed through proxy; direct path is not sufficient.
5. **Jev direct:** Jev false while Agent/Tunnel proxy may be true; Jev request does not hit proxy.
6. **Jev proxy:** Jev true; advisory request succeeds through proxy.
7. **Interactive approval:** real pending approval -> public response -> provider continuation.
8. **Claude spool:** real Claude result -> spool -> exact public reconstruction.

Evidence must record switch values, runtime versions, task IDs, normalized task/result metadata and proxy observer counts, but never proxy credentials or API keys.

## 12. Phase I — Documentation and release closure

After implementation/acceptance:

- bump project/package release versions to v0.12.0 according to the repository's unified release policy;
- update `.env.example`, deployment README, root README, Agent Bridge docs, Jev docs, configuration docs and website;
- clearly document the three independent switches and shared proxy fields;
- document that Agent runtime proxy is credentialless in v0.12 and why;
- document spool threshold default 256 KiB and the configurable deployment value;
- remove any v0.11 wording that falsely says Linux Agent proxy / interactive approval is unavailable once proven;
- run docs/site build and release-facing consistency scan;
- only after CI and live Linux acceptance are green is the tree eligible for the v0.12.0 tag.

## 13. Explicit non-goals

v0.12.0 does **not**:

- add SOCKS proxy support;
- implement a ServerFS authenticated-proxy credential broker for Agent processes;
- put proxy credentials into provider command lines or standard Bridge proxy environment variables;
- restart the user's existing Linux Codex managed daemon to impose ServerFS proxy policy;
- add automatic approval, automatic denial, or change Jev from advisory-only;
- add a new public MCP tool/RPC merely for approval or spool testing;
- change the default 256 KiB inline result threshold;
- redesign Windows native proxy/supervisor architecture.

## 14. Implementation order / stop conditions

Execute in this order:

1. shared config contract + tests;
2. Tunnel switch;
3. Jev explicit client proxy;
4. Claude/Qoder Linux Agent proxy;
5. proxy-enabled Linux Codex owned app-server path;
6. explicit provider interactive approval;
7. configurable spool threshold + bounded message events;
8. deterministic full matrix;
9. restricted-network Linux live E2E;
10. docs/version/release closure.

Stop and reassess rather than layering workarounds if any of these assumptions are disproved by live evidence:

- standalone Linux Codex `app-server --listen unix://...` cannot serve the required connection/concurrency shape;
- Qoder `permission_mode="default"` does not expose native `ask` requests through `can_use_tool`;
- explicit `httpx2.AsyncClient(proxy=...)` is incompatible with the installed Jev SDK lifecycle;
- a live Claude task cannot reliably exceed even a 1 KiB configurable spool threshold without tools.

In each case, preserve the public v0.11 contracts and choose the smallest provider-supported alternative rather than bypassing provider controls.

## 15. v0.12.0 release acceptance checklist

v0.12.0 is release-ready only when all are true:

- [x] `main` implementation is based on the v0.11.0 stable baseline and contains no VPS Windows-development residue.
- [x] Three independent `.env` proxy switches exist and use the same existing proxy fields.
- [x] OpenAI Tunnel explicit direct/proxy behavior is proven.
- [x] Jev explicit direct/proxy behavior is proven without process-wide proxy environment mutation.
- [x] Claude/Qoder Agent proxy behavior is proven on Linux.
- [x] Proxy-enabled Linux Codex is proven to use a Bridge-controlled process whose proxy environment is deterministic.
- [x] Agent proxy credentials are never exposed; authenticated Agent upstream is rejected unless represented by a credentialless local broker.
- [x] Codex real interactive approval round trip passes.
- [x] At least one Claude/Qoder real interactive approval round trip passes; Qoder passes in the frozen Linux live evidence.
- [x] Claude real result enters the spool path using the configurable threshold and is reconstructed exactly.
- [x] Default 256 KiB inline / 8 MiB spool contract remains unchanged.
- [x] Large message events cannot prevent a valid large final result from reaching spool handling.
- [x] Root + Bridge tests and ruff pass.
- [x] Linux/container CI passes.
- [x] Windows/native/Windows-Agent regression CI passes.
- [x] Restricted-network Linux live acceptance passes for Tunnel + Agent + Jev independently; see `docs/v0.12.0-linux-live-acceptance-2026-10-10.md`.
- [x] Documentation/site are aligned to the published v0.12.0 contract; the site builds 21 pages without warnings/errors and identifies v0.12.0 as the current stable release.
- [x] Secrets/proxy credentials are absent from the tracked diff and all nine new v0.12 files; live acceptance/public evidence is redacted.

Release CI evidence:

- `b4f64040ecfa8cffe4c34e14c939f61e179130d7`: Container run `38014406082` — success.
- `b4f64040ecfa8cffe4c34e14c939f61e179130d7`: Windows Native run `38014406119` — success, including hard-required symlink kernel cases, Ruff, Windows Python/native tests, release wheel build, and clean-environment wheel acceptance.
- `1bd54f8de9aac14aaeb5f4dbfe185986d6036e80` (direct parent product-code commit): Windows Agent run `38013965570` — success. The subsequent `b4f64040` commit changes only the Windows Native workflow trigger surface, so no Agent product code changed after this successful run.
