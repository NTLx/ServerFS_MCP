# ServerFS v0.24.1 — Native macOS Cloudflare Tunnel

Status: **candidate in progress — Phase 0/design and implementation verification underway; not release ready**. Initial native macOS implementation evidence has been gathered, but Phase 0 approval, live service registration, isolated public Tunnel acceptance, rollback, cross-platform CI, documentation/Skills, version alignment and release gates remain open. Depends on the v0.24.0 Linux Cloudflare release being published and verified. Owner of existing production OpenAI Tunnel must remain unchanged unless separately approved.

## Objective and platform boundary

Add an optional **native user-level Cloudflare Tunnel ingress on supported Apple Silicon/macOS 27** with the same ServerFS Streamable HTTP and independent API Key/OAuth admission contract as v0.24.0, **without Docker**. Preserve existing OpenAI Secure MCP Tunnel, stdio, Agent Bridge, Repo Ops, macOS SMAppService lifecycle, workdir restrictions and safe defaults. Do not claim Intel, Rosetta or untested macOS versions.

## Decisions and non-goals

- Reuse the existing `native/macos/service_manager` app-associated `SMAppService`/LaunchAgent ownership; no root/LaunchDaemon, uncontrolled plist, second external daemon, or mandatory login-shell startup.
- Use an **official, integrity-verified macOS arm64 cloudflared** executable staged in a per-user ServerFS installation location. Record tested upstream version and authenticated checksum/release provenance. No unverified `curl | sh`, global PATH dependency, or automatic silent upgrades.
- Tunnel is a remotely managed named Cloudflare Tunnel with an operator-configured Host routing to a **private native loopback Streamable HTTP origin**. The Connector Token resides outside every configured workdir and source repository in user-private storage; file security and rotation must be tested. No Cloudflare API/DNS mutations as a side effect of bootstrap/service install.
- A Tunnel Token only authenticates the Connector; remote MCP callers still need effective ServerFS API Key/OAuth (or a separately verified upstream authentication boundary), narrow Host/Origin controls, TLS termination at Cloudflare and explicit public-endpoint admission.
- Define explicit Tunnel provider selection while preserving existing OpenAI Tunnel CLI/config defaults and its single-managed-owner contract. Reuse current service state/observability models, do not create a generic command executor or unnecessary provider abstraction.
- Product and public docs must not imply production workdir/Agent or automatic credentials sharing. Query-string API keys can be logged and are not a preferred production authentication pattern.

## Phase 0 — Evidence-led feasibility and design gate

1. Inspect `src/serverfs_mcp/native_tunnel.py`, `darwin_service.py`, `darwin_lifecycle.py`, `native_config.py`, `tunnel_bootstrap.py`, `src/serverfs_mcp/main.py`, and the native service templates. **Determine whether the current macOS persistent chain actually exposes a private HTTP /mcp listener independently of the OpenAI tunnel-client.** Do not assume the Linux Compose HTTP origin applies on macOS.
2. Confirm official cloudflared Darwin arm64 binary packaging, integrity verification and compatible `tunnel run --token-file` behavior on the supported OS. Select the least invasive versioned binary-management contract.
3. Document the provider-selection surface and service ownership model, coexistence/migration policy with OpenAI Tunnel, native HTTP listener lifecycle, how Agent Bridge continues to work, and rollback semantics. If the existing managed runner cannot safely switch providers, revise the architecture and obtain explicit approval before implementation.
4. Produce concrete configuration examples, safe token storage path/permissions and an isolated local acceptance topology. Preflight must fail closed on missing token, missing HTTP auth, unexpected Host/Origin, duplicate managed process or user Login Items denial.

### Progress log — 2026-11-06

- Repository baseline: after fetch, latest Git `main` was `be73c3e`, with ahead/behind counts both 0. The checkout already contained uncommitted, opt-in macOS Cloudflare code before this plan update; this log records verification evidence and does not establish that the work is approved or release ready.
- An independent HTTP-origin environment JSON snapshot fix was verified, including exact public `Host` handling and revalidation.
- On macOS 27.2 arm64, the official `cloudflared` 2026.10.7 archive SHA-256 was verified as `3586ccf...`; the binary SHA-256 was `213643...`; code signing reported TeamIdentifier `68WVV388M8`; and the binary version was verified. The abbreviated archive and binary digests are recorded as supplied in the evidence summary, not as full checksum values.
- Native root pytest completed: 1646 passed, 403 skipped, 1 warning. `ruff format` reported 499; the companion app and wheel builds were green and contained the two new plists. Plugin validation still reports v0.24.0.
- **Not verified / remains open:** live `SMAppService` registration of the new agent (the existing installed app lacks the two new plists); a named Cloudflare Tunnel with public ChatGPT tool calls and SSE; isolation and rollback; Linux and Windows CI; bilingual site docs, Skills, version bump and release. No publication or release-readiness conclusion follows from the checks above.

## Phased implementation (after Phase 0 approval)

- **A. Native bootstrap and configuration:** official verified binary, private token-file contract, provider selection and diagnostics; no unauthorized Cloudflare control-plane creation.
- **B. Persistent service integration:** use the existing `SMAppService` app-associated LaunchAgent and user-owned service install/start/stop/restart/uninstall state; preserve existing Bridge lifecycle/handover, avoid accidentally replacing production Tunnel, and support reversible provider migration.
- **C. Protocol/security regression:** loopback-only native origin, API Key/OAuth/Host/Origin enforcement, workdir confinement, hidden-path policy, no secret disclosure; deterministic unit/platform tests and rollback.
- **D. Real macOS acceptance:** isolated test workdir and separate named Tunnel/hostname, TLS 401/200, real ChatGPT plugin file read/search, negative mutation/hidden/traversal, >60-second SSE, session use after controlled Connector restart, login/logout persistence where supported; record actual platform/arch and evidence.
- **E. Release closure:** update all **seven** existing Skills and reference branches, plugin manifest/ZIP, README/README_EN, native macOS runbook, bilingual website and release notes; align project, Bridge/native metadata and locks; full CI and dispatch-first exact-SHA release workflow.

## Acceptance criteria

- Correct official binary + authenticated version verification; no root access and no unmanaged background service.
- `serverfs service status/start/stop/restart` reports and controls the chosen mode without unexpected OpenAI Tunnel takeover, Agent Bridge death, or duplicate ownership.
- Actual Cloudflare Connector connected, public HTTPS verified, independent MCP admission gates deny unauthorized clients.
- Direct ChatGPT tool discovery/read with isolated fixture; private workdir enforcement, 60+ seconds SSE, existing session reuse after Connector restart; no claim of in-flight SSE lossless replay.
- Demonstrated rollback to the previously working OpenAI Tunnel behavior without losing private configuration or user authority.
- All applicable macOS native, Linux nonregression, Agent/Skill/Plugin and site build gates green before v0.24.1 release.

## Out of scope / handoff

No Windows Cloudflare native lifecycle in this release; it is planned for **v0.24.2**. No Linux Tunnel redesign, general macOS platform expansion, Cloudflare WAF/DNS automation, new MCP tools, or deployment against the operator's existing production instance without explicit approval.

## Progress and remaining gates — 2026-11-06

This dated ledger supplements the earlier progress log; it preserves that record and does not change the candidate's release status. Evidence below is limited to the checks and observations recorded for this date.

### Verified progress

- The supported test host was macOS 27.2 on arm64. After fetch, local `main` was `be73c3e` and had zero commits ahead of and behind `origin/main`. The worktree contains uncommitted v0.24.1 source changes; no commit or publication is implied.
- The `ServiceManager.app` bundle was upgraded in the current user's `service_home` only. It contains four signed plists. The existing OpenAI Tunnel process (PID 1516) and Agent Bridge process (PID 1526) remained unchanged during this bundle-only upgrade. Direct MCP calls to `list_workdirs` and `agent runtimes` succeeded.
- The previous app bundle was securely backed up in a private per-user backup location.
- The native HTTP dotenv-to-0600 JSON snapshot bug was fixed. The exact public `Host` handling and TOCTOU recheck were fixed. Failed-unregister rollback now preserves the private recovery manifest and snapshot.
- Targeted rollback tests completed with **42 passed, 1 skipped**. After the last rollback change, full root `pytest` was rerun and passed with **1648 passed, 403 skipped, 1 Starlette deprecation warning**; Ruff check, Ruff format check (499 files), Plugin validation (still v0.24.0) and `git diff --check` also passed.
- The official Cloudflare `cloudflared` 2026.10.7 archive and extracted binary digest and code signature were verified on the supported Mac. The archive and binary digests and signing details remain in the private verification evidence; this plan intentionally records no account identifiers or private file contents.
- An independent named staging Tunnel and DNS route were created for `mac-v0241-stage.ntlx.top`, with origin `127.0.0.1:8765`; the Tunnel remains inactive. Outside the repository, `~/Library/Application Support/ServerFS/v0241-mac-stage` was created with one read-only fixture workdir and private HTTP authentication. The official binary was installed separately under the per-user ServerFS `bin/` directory.
- A local `OPERATOR-TASKBOOK.md` with mode `0600` was prepared for the operator to save the Connector Token out of band. No token or key is recorded here.
- Earlier companion app and wheel builds completed successfully with the two new LaunchAgent plists included. No site build, cross-platform CI, or stable release check is claimed by this entry.

### Live acceptance follow-up — 2026-11-06 (operator report)

The Mac operator reported completing the isolated seven-step staging checklist; these results are operator evidence, not a claim that release publication was authorized. Both Cloudflare and HTTP origin reached `enabled`; the production OpenAI Tunnel and Bridge remained at PIDs 1516/1526. Public HTTPS rejected unauthenticated requests with 401 and returned authenticated MCP `text/event-stream` with 200. The staging endpoint exposed 11 tools, read a 64-byte fixture, and rejected writes, traversal, absolute paths and hidden paths. SSE stayed live for 65.1 seconds with keepalives and remained usable; connector-only restart and session replacement passed 8/8. Local lifecycle stop/start and SMAppService persistence passed. **A real macOS logout/login was NOT performed**, so this release gate remains open. The operator's sanitized evidence is in their private local-ops acceptance report, outside this repository.

The operator found and repaired two production issues: preserve the virtual-environment interpreter path via `Path(sys.executable).absolute()` in the HTTP origin service spec; address the connector termination/restart race. Follow-up review rejected treating a matching token-file command string as proof of process ownership; Cloudflare-only restart now waits up to a bounded interval for the exiting connector and fails closed if an unmanaged process persists. The original 12 unit-test failures were due to the active staging server occupying a hard-coded port; unit tests now isolate the port probe while a separate real-socket negative test verifies collision handling. The staging service was not stopped for tests.

Post-fix local macOS gates: `pytest` **1653 passed, 403 skipped**, Ruff check and format check, plugin validation (still v0.24.0) and `git diff --check` all passed. ServiceManager.app and wheel were rebuilt successfully; the wheel contains the four LaunchAgent plists. The pending changes were scanned for common secret signatures; two long-string source hits were independently classified as ordinary diagnostic text, and a test fixture hit as a static placeholder. This was a heuristic candidate-only scan, not a comprehensive credential audit.

### Not done — release gates remain open

- Execute actual macOS logout/login in a planned local maintenance window and verify automatic recovery of the new Cloudflare service and existing production OpenAI Tunnel/Bridge. Do not cut the current MCP control connection during an agent task.
- Complete exact-SHA cross-platform CI and any remaining native recovery, rollback and public ChatGPT product acceptance gates not yet evidenced.
- Update all seven Skills, bilingual current-state documentation and site, and required v0.24.1 version surfaces; run the applicable validation gates.
- Complete and verify a stable release using the repository's established exact-SHA publication workflow **only after explicit release authorization**.

Until those gates are complete, v0.24.1 remains an unreleased development candidate. A code checkpoint commit is not a release.
