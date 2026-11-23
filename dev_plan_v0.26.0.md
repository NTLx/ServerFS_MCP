# ServerFS MCP v0.26.0 — Three-Platform Parallel Development Plan

> Status: **DEVELOPMENT IN PROGRESS — NOT INTEGRATED / NOT RELEASE-AUTHORIZED**. Prepared 2026-11-23. This document is the planning baseline, not evidence that v0.26.0 integration, deployment, or publication has passed. Linux, macOS and Windows have independent local feature-branch implementation commits and scoped regression evidence; their shared GitHub CI and exact-SHA live acceptance remain pending. The v0.25.0 release checklist remains the authority for its own pending gates.
>
> Baseline observed at planning time: VPS and WorkPC clean `main`, and MacBook local `main` ref, all at `c0353077760675871538569d6cbff79a7a3a38fa`. Remote GitHub `main` must be reverified immediately before branch creation. MacBook working-tree cleanliness was not independently established (its ServerFS connector currently lacks Repo Ops). Live v0.25.0 service versions were not cryptographically tied to checkout HEAD. No runtime task or deployment was executed for this plan.

## 1. Intent and success criteria

Fix four specific gaps without rebuilding the v0.25.0 three-layer architecture:

1. **Shared Agent Bridge approval security:** preserve privacy of host paths while retaining trustworthy, per-request resource identity, approval-advice isolation, and locally inspectable audit evidence.
2. **Linux named signed-v2 Core:** support multiple independently authorized Agent and/or Repo Ops workdirs per Core, not just slot 01, while maintaining container/Core vs host Bridge isolation.
3. **macOS native shared Core:** support multiple independently authorized Agent-enabled workdirs against one existing Bridge owner; leave existing native owned multi-workdir behavior untouched.
4. **Windows native named Bridge:** restore explicit opt-in, credentialless per-runtime provider proxy support through the independent Bridge owner/bootstrap, without changing Connector/Jev networking or introducing WSL/VM.

The release succeeds only when all affected platform-specific paths pass deterministic, negative-security, isolated native/container, and authenticated public MCP checks **against the same integrated release candidate SHA**, with explicit publication authorization. Source compilation, runtime discovery, or an existing v0.25.0 deployment does not by itself prove acceptance.

## 2. Verified baseline and actual scope

| Concern | Linux named | macOS owned | macOS shared | Windows named |
| --- | --- | --- | --- | --- |
| Approval path/audit integrity | Common Bridge affected | Common Bridge affected | Common Bridge affected | Common Bridge affected |
| Multiple Agent-enabled workdirs | Restricted to one enabled slot 01 per Core in signed attachment | Already exposed two in the connected Mac service | Current native manifest enforces exactly one | Current connected service exposes two; native binding supports up to 16 |
| Repo Ops across enabled workdirs | Shares Linux single-enabled-slot restriction | Existing native policy governs | Currently excluded by shared-Core scope; **not** added by this plan | Already exposed on both connected workdirs |
| Runtime proxy | Named host Bridge has private proxy-capable config | Native owner policy supports per-runtime opt-in | Uses separately configured Bridge owner | Named provisioning rejects `use_proxy=true`; independent bootstrap provides no proxy |

Observed live capabilities: Mac workdirs `serverfs` and `codexsatellites` are Agent-enabled (Claude/Codex/Qoder; Repo Ops off); Windows workdirs `serverfs-mcp` and `cmtools` are Agent-enabled and Repo Ops-enabled (Claude/Qoder). This is configuration/capability evidence, **not** two-workdir execution or proxy traffic evidence.

Relevant code and contracts:

- `agent_bridge/src/serverfs_agent_bridge/policy.py`: `redact_host_path` maps disallowed external paths to `<outside-workdir>`.
- `agent_bridge/src/serverfs_agent_bridge/service.py`: `_wait_for_request` redacts before pending-request persistence and before `_approval_advice_fingerprint`. This risks *advice-cache* equivalence for otherwise distinct original targets; it does **not** demonstrate that authorization decisions are automatically reused.
- `agent_bridge/tests/test_codex_adapter.py`: outside-workdir approvals can be explicitly passed through to the provider despite sanitized display.
- `deployment/linux/compose.bridge-v2.yml`: exports only the slot-01 Agent/Repo Ops overrides.
- `agent_bridge/src/serverfs_agent_bridge/linux_v0250_provision.py`: single `alias/container_path/host_path/methods` Core entry; expects `/workdirs/01`.
- `src/serverfs_mcp/linux_v0250_core_binding.py`, `core_bridge_client_v2.py`, `linux_v0250_bridge_adapter.py`, `main.py`: one alias/policy binding per named Core.
- `agent_bridge/src/serverfs_agent_bridge/core_grants.py`: underlying signed grants already carry multiple alias/policy tuples. Reuse the effective grant semantics, not unrelated OS-specific lifecycle logic.
- `src/serverfs_mcp/darwin_instance_shared_manifest.py`: rejects more than one Agent workdir; trace shared staging, binding and client routing as a complete chain.
- `src/serverfs_mcp/windows_v25/core_bridge_binding.py`, `agent_core_grant_client.py`: already validate/map multiple aliases; preserve these semantics.
- `deployment/windows/configure-v25-private.py`: rejects any requested runtime/ACP `use_proxy`.
- `src/serverfs_mcp/windows_v25/runtime.py::run_independent_bridge_v2`: uses `bridge_child_environment(None)` and `bootstrap_frame_bytes(None, ...)`.
- `agent_bridge/src/serverfs_agent_bridge/config.py`, `runtime_proxy.py` and `main.py`: existing private proxy/runtime opt-in mechanisms. Mac owner implementation is `src/serverfs_mcp/darwin_bridge_policy.py`.

Read `AGENTS.md`, `docs/maintainers/invariants.md`, `docs/maintainers/workflow.md`, `docs/architecture/v0.25.0-instance-model.md`, `docs/architecture/v0.25.0-native-platform-boundaries.md`, and the current platform runbook before code changes. Historical v0.25.0 notes are scoped evidence, not authorization to edit release tags, live secrets, or installed snapshots.

## 3. Non-negotiable design boundaries

- Preserve independent identities and lifecycles of Connector, MCP Core and Agent Bridge. Maintain `instance_id`, `bridge_id`, `bridge_mode` (`disabled/owned/shared`), and `connector_id`. No central instance manager.
- Linux: Core and optional Cloudflare/OpenAI Connectors stay Compose-managed and independently composable; direct HTTP requires no Tunnel. **Bridge stays optional, host-native (user systemd) so already installed host Agents are callable.** Do not containerize it merely for uniformity. Preserve same-host Unix credential checks, signed-v2 grants, private volumes, UID/GID and per-project isolation.
- Windows: native Windows 11 Scheduler + Job Object + Named Pipe + private ACL/SID, windowless operation; no WSL/VM, privileged service, generic shell, or machine-wide environment proxy.
- macOS: native arm64/macOS 27/APFS, signed ServiceManagement/SMAppService and private native IPC/grants. Do not force private-wire identity with Windows/Linux. Preserve owned behavior; shared Core may not own or restart its Bridge.
- Shared Core authorization must bind its **own** identity, allowed aliases/methods and policy revisions; neither host UID/SID nor a shared socket constitutes authority. Revoked/stale/mismatched grants, foreign task IDs and cross-Core alias use fail closed. No downgrade to unsigned/v1.
- Preserve default read-only, hidden-path/deny policies, revision guards, writer leases, task ownership, process containment, credential handling, and Repo Ops' deterministic four-tool boundary.
- Provider authentication, model defaults and sandbox remain provider-owned. Jev advice remains advisory, never auto-approval; distinguish Agent proxy, Jev proxy and each Connector's egress.
- Do not replace create-only named provisioning or existing private manifests in place to simulate upgrade. Use isolated new instances/reprovisioning, with owner authorization and rollback. Production service migrations, secret rotation and final publication are separate gates.

## 4. Parallel Git work model

**Single authoritative repository: GitHub `NTLx/ServerFS_MCP`.** Three local independent working branches; a fourth integration branch is created centrally after checkpoint PRs. Branch names are proposed and must be checked for conflicts against live refs:

| Workstream | Host | Branch | Owns implementation paths |
| --- | --- | --- | --- |
| L — Linux + common approval/audit | VPS | `feature/v026-linux-bridge-audit` | `deployment/linux/**`, Linux-specific `src/serverfs_mcp/linux_*` and tests; common `agent_bridge/.../policy.py`, `service.py`, approval audit/storage and targeted Bridge tests |
| M — macOS native shared multi-alias | MacBook | `feature/v026-macos-shared-multiworkdir` | `src/serverfs_mcp/darwin_*`, `deployment/macos/**`, `native/macos/**` only if necessary, macOS-specific tests |
| W — Windows named runtime proxy | WorkPC | `feature/v026-windows-named-proxy` | `src/serverfs_mcp/windows_v25/**`, `deployment/windows/**`, Windows-specific tests; shared proxy code only through an explicit ownership handoff |
| I — integration and release gate | GitHub PR integration | `integration/v0.26.0` | Conflict resolution, shared docs, version metadata, CI, release notes, cross-platform acceptance records |

**Base/branch protocol**:

1. Recheck GitHub `main` SHA, pending v0.25.0 release work, and three local statuses. Never branch from an assumed stale ref. Do not branch atop an uncommitted plan copy that would be duplicated in each PR.
2. Make this plan visible in a single plan-only upstream commit **after a separately authorized Git commit/push**; ideally merge it to `main` first, leaving current v0.25.0 source and release tags unchanged. All three feature branches must share that exact plan-containing base SHA. If main moves, record the new common base and evaluate divergence before development.
3. Each host creates/checks out only its assigned branch, with a clean or explicitly preserved existing worktree; do not reset/stash/discard another session's changes. Feature branches must not be used for active installed service execution. Never auto-upgrade live Core/Bridge/Connector from a feature checkout.
4. While working, inspect the target path ownership. **No two branches edit the same production code file concurrently.** L is the temporary owner of shared approval/Bridge code; M/W may add isolated tests and document an issue but must request a handoff/PR instead of overlapping edits. Repo-wide metadata, README, bilingual site, `AGENTS.md`, lockfiles and shared CI are I-owned. An unavoidable shared edit requires advance handoff and a narrow independent commit.
5. Commit separately reviewed, narrowly scoped checkpoints to feature branches; push each to its own upstream and create PRs only when explicitly authorized. Record changed files, tests, SHA and open gates. No forced pushes, direct feature-to-feature merges, surprise `main` commits or automatic production deployments.
6. Once each feature PR has passed its **own** applicable CI, integrate via GitHub into `integration/v0.26.0`: first L (shared Bridge and Linux), then M, then W. This ordering assigns a single conflict-resolution owner, **not** a requirement to serialize local development. Re-run dependent tests after each integration, not just on the original PR SHA.
7. Create/update release metadata and current bilingual docs only in I; submit a final PR from integration to `main` after all release gates. If `main` advances, use a reviewed merge/rebase strategy that preserves history and rerun CI on the resulting exact SHA. The three platforms must test/build the **same final integration SHA** before release approval.
8. Branch cleanup and tags are post-merge/post-release operations with separate authorization. Never equate a feature PR green check with final release approval.

**Potential collision register**:

- `agent_bridge/src/serverfs_agent_bridge/{service,policy,store,core_grants*,config,runtime_proxy}.py`: L owns shared approval/audit; W should prefer existing proxy interfaces and Windows-local bootstrap. If W discovers a required common `runtime_proxy.py` change, stop on that file and request a reviewed handoff.
- `src/serverfs_mcp/{main,core_bridge_client_v2,agent_core_grant_client}.py`: L owns Linux-specific v2 routing modifications; M must keep native macOS scoped, W keep native Windows scoped. Any universal client change needs an explicit shared-code owner and cross-platform tests.
- `pyproject.toml`, `agent_bridge/pyproject.toml`, both `uv.lock`, `native/windows/Cargo.lock`, `site/**`, `docs/releases/**`, `.github/workflows/**`: integration ownership, except a demonstrably required platform-local test/CI change agreed in the PR.
- Platform runbooks may be edited in their own branch; integration resolves cross-platform links/terminology and v0.25.0 historical references.

## 5. Stream L (VPS) — Linux multi-workdir and shared approval security

### L1. Linux named multi-alias signed-v2 support (P0)

Desired observable result: a single Linux named Core can expose independently controlled Agent and Repo Ops workdirs in **any configured slot (including 02, 03 and 16)**, while file-only slots and Core-without-Bridge keep working.

Implementation sequence:

1. Inventory effective `WORKDIR_XX_*` parsing (1–16), Compose overrides, container mounts, per-workdir mode/repo flags, and existing Core/Bridge owner policy; write a negative-contract test first.
2. Expand only the signed-v2 opt-in overlay for all supported slots, retaining explicit false/disabled defaults; avoid silently enabling a provider or Repo Ops on slots that merely have a file mount.
3. Define a bounded **versioned** provisioner and private-binding representation for multiple `{alias, container_path, host_path, policy_revision, methods}` entries per Core. Use a strict 1–16 alias ceiling unless actual implementation constraints demand a narrower documented bound. Keep up to 32 initial Cores per owner unchanged. Validate canonical path, mount/host mapping, unique aliases, duplicate roots, method intersections, allowed modes, exact policy digests, and secret-file privacy; **do not** weaken v2 grant signatures/replay prevention.
4. Update Linux Core binding loader, v2 client adapter and task/repo dispatch so each request signs/routes using the *selected authorized workdir alias* and its own policy revision. Preserve scoped Task ID ownership for follow-up, events, approval, result, cancel and message operations; do not infer alias from untrusted task IDs or expose neighboring Core tasks.
5. Maintain owner/consumer sharing: two Cores may use one Bridge only with distinct Core identity and authorized alias/method scope; a file-only Core can run with no Bridge, and disabling one slot must not disable unrelated file tools.
6. Preserve create-only provisioning. For existing single-alias v0.25.0 installs, define explicit compatibility choice: bounded read of existing schema, or documented isolated reprovision migration. **Do not** silently reinterpret persisted single-alias credentials/grants; fail closed on mixed schema/owner-policy drift.
7. Update Linux runbook, example overlay/environment comments and tests, avoiding changes to live private env/tokens.

Required checks: slots 01+03, only slot 03, 16-slot upper bound, mixed read-only/read-write, Agent-only, Repo-only, Agent+Repo, missing Bridge, wrong path/revision, duplicate alias, malicious slot-to-root mismatch, wrong owner, forged/replayed grants, revoked grant, foreign task, concurrent writer lease, and separate-Cores shared owner. Verify both Cloudflare/OpenAI and direct HTTP still compose correctly; no Connector-specific behavior change is intended.

Acceptance: fresh disposable signed Core/Bridge pair; real host Agent task in two different authorized slots; actual four-tool Repo Ops policy checks on an authorized slot (including Agent-disabled Repo-only slot); denial on ungranted third slot; independently restart connector and Core without restarting Bridge; verify new authenticated public MCP call, not only Unix IPC.

### L2. Shared Agent approval privacy + audit integrity (P1, security-sensitive)

**Before implementation, lock a threat model**: distinguish provider-native permission decisions from ServerFS policy enforcement; the only presently demonstrated collision is in optional *approval advice caching*, not automatic approval replay.

Proposed behavioral contract:

- Public MCP approval payloads, task events and normal logs never reveal private absolute host paths, credentials or other workdir roots; authorized in-workdir paths remain interpretable as scoped relative paths. Preserve useful non-sensitive action type, file count, read/write scope and target classification.
- A sanitized `<outside-workdir>` token is **not an unambiguous resource identity**. The approval view must signal unresolvable/out-of-scope targets and must not imply that generic approval authorizes an unspecified arbitrary host write. Define explicit fail-closed policy for outside-workdir write/grant escalation, with a documented trusted operator path if legitimately supported. Respect provider-owned approval semantics without converting them into an unreviewed ServerFS authorization bypass.
- Compute optional approval-advice cache identity from the **validated original request** (or private keyed digest of canonical trusted attributes) before public redaction. Include task/provider/action/target context; keep original identifiers out of public payloads and free-text logs. A cache hit must never grant permission or decide an approval.
- Persist a bounded private, owner-only audit record tying request/task/Core/Bridge/workdir/resource identity/action/decision/timestamp and an integrity-protected fingerprint, with explicit retention and ACL/mode controls. Do not dump original prompts, credentials or raw arbitrary provider output. Audit failure and request expiry/cancellation policy must be explicit and testable; no hidden open-approval fallback.
- Ensure provider response is bound to the current pending request and exact allowed permission IDs; races, stale IDs, duplicate response, cross-task/Core response, task recovery and revoked policies fail closed. Independent approval advice failure remains advisory-only where no security escalation is involved.

Implementation first adds adversarial tests and a compact security design note, then updates only necessary Bridge modules. Reuse current storage/lease/event patterns when sufficient; do **not** add a central audit service or broad secrets database.

Required tests: two different host paths redacting to the same public token must have distinct private fingerprints/advice entries; in-root target renders relative; out-of-root write/grant escalation is blocked or explicitly verified by approved local path; path traversal/symlink/encoded path, provider-supplied free text and ACP permission IDs; approve_once/approve_session/deny/cancel_task; stale response, restart/expiry and credential nonleakage. Execute root **and** Bridge test gates plus Linux/macOS/Windows compatibility suites.

## 6. Stream M (MacBook) — native shared Core multi-Agent workdirs (P0)

Desired observable result: an existing separately owned macOS native Bridge can issue/verify a least-privilege grant to a shared Core configured with more than one Agent-enabled workdir. Owned native mode and its two-workdir behavior remain unchanged.

1. Trace shared manifest, shared staging/preflight, private `shared-bridge-binding.json`, native owner grant, launch configuration, workdir alias dispatch and task ownership. Add a reproduced two-Agent-workdir refusal test first.
2. Extend private binding to a bounded list/map of authorized workdir aliases with canonical roots, modes/runtimes and policy revision(s). Enforce exact Core config / owner policy parity and stable, versioned manifests. Keep create-only staging and signed ServiceManagement ownership; do not hand-edit an installed manifest or bypass binary identity pins.
3. In shared Core dispatch, use per-request authorized alias and preserve task ownership across follow-up calls; block foreign task access, stale grants and owner revocation. Preserve existing private macOS IPC/peer verification without transplanting Linux HMAC/private wire as an artificial cross-OS requirement.
4. Keep **shared Repo Ops unsupported** unless an explicit later scope decision authorizes a separate security design. Do not add named Connector staging to shared Core or change owned lifecycle, TCC, Login Items or shared-owner stop authority.
5. Tests: two Agent workdirs, one Agent/one file-only, conflicting aliases, wrong root/mode/runtime, stale binding, foreign Core, forbidden Repo Ops, revocation/restart, and unaffected owned/file-only modes. Verify both workdirs with bounded real native tasks against a disposable owner/consumer setup on supported Apple Silicon/macOS 27.
6. Recheck macOS proxy-capable owner and dual ingress as **regression** only; do not create new proxy architecture.

Required checks: macOS native tests, signed helper/build and wheel source checks, isolated SMAppService preflight/stage/recovery, explicit Login Items approval where needed, authenticated public MCP and cross-Core denial. Do not stop production OpenAI/Cloudflare while testing a disposable shared Core.

## 7. Stream W (WorkPC) — Windows named per-runtime proxy (P0)

Desired observable result: named Windows Bridge accepts **explicit** per-runtime `use_proxy=true` for Codex/Claude/Qoder and configured ACP profiles when the matching protected credentialless proxy endpoint is valid, while `use_proxy=false` runtimes remain direct even if ambient proxy variables exist.

1. Reproduce refusal in `deployment/windows/configure-v25-private.py::render_bridge` and inspect existing `agent_bridge/config.py`, `runtime_proxy.py`, `main.py`, `agent_lifecycle.py`, Windows private-storage/ACL policy and tests.
2. Define the minimal protected input contract: explicit private proxy endpoint, no URL credentials, no provider auth tokens, no arbitrary command or untrusted global environment. Reject `use_proxy=true` with absent/malformed/unapproved proxy. Preserve ACP profile-level opt-in and independent Jev/Connector routing.
3. Update **Windows-local** named config rendering and Bridge owner to pass a validated proxy via the existing private bootstrap mechanism. Verify `bootstrap_frame_bytes` shape and parsing on Windows, rather than merely setting environment variables. Preserve a clean child environment for unrelated runtimes, no implicit provider proxy inherit, and persistent settings across Scheduler restarts.
4. Keep existing legacy/default proxy behavior intact; avoid altering `run_bridge` or a shared Codex daemon to fix the named path. Retain current-user Task Scheduler, windowless spawn, Job containment, SID/pipe identity, ACL and health provenance.
5. Test explicit proxy on/off with a controlled local observer (no external secrets); verify genuine provider traffic where credentials and approvals already permit it; no silent direct fallback on proxy outage; no credential leakage in configs, bootstrap, logs or MCP; wrong owner, cross-instance isolation, two-Bridge concurrent operation, restart persistence and cancellation.
6. Confirm existing two Agent+Repo Ops workdirs remain usable and restricted exactly as before. Do not add unsupported Codex availability claims: current WorkPC public runtime list contains only Claude and Qoder; Codex/ACP are conditional tests and must be marked **NOT VERIFIED** if unavailable.

Required checks: Windows native and Agent tests, private install/package checks, disposable named Scheduler/Job/pipe lifecycle and public MCP tasks. Preserve isolated OpenAI/Cloudflare/HTTP transport behavior.

## 8. Integration Stream I — contract review, CI and exact-SHA acceptance

### I1. Public behavioral matrix

Use an executable matrix with **actual evidence**, not one blanket PASS:

| Dimension | Linux | macOS | Windows |
| --- | --- | --- | --- |
| Authorized Agent in two distinct workdirs | named v2, slots non-01 | shared Core | named Core (regression) |
| File-only Core without Bridge | required | required | required |
| Per-Core/alias policy parity, revoked grant and foreign task rejection | required | required | required |
| Repo Ops limited to granted alias and Agent-disabled Repo-only capability | required | shared Repo Ops intentionally unsupported | required existing named policy |
| Provider proxy opt-in/out and unavailable proxy behavior | regression | owner regression | named implementation acceptance |
| Approval privacy, distinct private identities and audit integrity | required | required | required |
| Independent component restart and existing installed service unaffected | required | required | required |
| Authenticated public MCP (Cloudflare/OpenAI/direct HTTP as configured) | required | required | required |

For each test case log: platform, host/fixture (redacted), source SHA, package/image digest, instance/Core/Bridge identity, transport, exact script/test, timestamp, exit/status, expected vs observed, sanitized artifact path and operator approval when relevant. Mark **PASS / FAIL / BLOCKED / NOT VERIFIED** independently; do not infer a tunnel/provider result from an in-process mock. Negative tests are mandatory, not optional nice-to-have.

### I2. CI and reproducible builds

- Run the repository's actual root and Bridge Ruff/format/pytest gates; root pytest does not collect `agent_bridge/tests`. Include `docker compose config --quiet`, signed Linux overlay/topology fixture rendering, scratch-tag image build (no production-tag overwrite), fresh-container acceptance and MCP conformance.
- Run macOS native build/ServiceManagement and Windows native locked build + separate Core/Bridge wheels where applicable; verify lockfiles, package metadata and manifests. Do not upgrade unrelated dependencies to get a green build.
- Re-run all applicable GitHub Actions workflows at every integrated conflict resolution point and **again on final exact SHA**: Container, macOS Native, Windows Native, Windows Agent and any required site/release-specific workflows.
- Fix failed steps with targeted tests; never waive failing checks silently. A previously passing run on a different SHA is not equivalent. Keep source-level, container-level, native-level and public-ingress evidence separate.

### I3. Release gate and rollback

**HOLD** until all required gates are complete:

- [ ] The v0.26.0 plan is shared from one reviewed source commit; the three platform feature branches have recorded common base and scoped ownership.
- [ ] L, M and W feature PRs are independently reviewed and green, with no unresolved collision in shared security-sensitive code.
- [ ] Common approval path/approval-advice/audit threat-model tests pass on all three supported platforms, including negative security behavior.
- [ ] Linux multi-slot Agent/Repo Ops and shared-v2 Core grant isolation pass in disposable Compose + host-Bridge tests and authenticated public ingress.
- [ ] macOS native shared multi-Agent workdir passes signed native and public shared-owner/revocation tests; owned mode remains unchanged.
- [ ] Windows named per-runtime proxy opt-in/out passes real native named Bridge lifecycle and isolated proxy/public checks; existing two-workdir and Repo Ops behavior survives.
- [ ] Existing file-only, connector independence, caller-auth, workdir permissions, Repo Ops, provider-model semantics and v0.25.0 behavioral regressions pass.
- [ ] Exact integrated SHA passes all required ordinary CI, package/site/lockfile/release validation and same-SHA three-host acceptance; missing providers/credentials remain explicit blockers.
- [ ] Updated platform runbooks, maintainer invariants, current bilingual docs, version metadata and new `docs/releases/v0.26.0.md` reflect **measured** scope, with a precise compatibility/migration/rollback guide.
- [ ] A separate maintainer decision authorizes any persistent service cutover, immutable release tag, GitHub Release, GHCR images, wheels, portable Plugin and website publication.

Rollback preserves current v0.25.0 installed services and private state: use independent disposable identifiers, ports, project names, private auth and Bridge endpoints; snapshot **configuration metadata** without copying secrets into Git; test component-scoped stop/revert and original MCP health after fixture teardown. Do not silently rotate old credentials, replace active binary pins or reuse production tunnel routes during development.

## 9. Deliverables / handoff requirements

Each feature branch/PR must provide a concise evidence bundle:

- Base SHA, head SHA, changed-file scope, design/security decisions and exact new public behavior.
- Reproducer (previous failure), positive and negative tests, exact commands/exits and any real runtime outcomes with sensitive fields removed.
- Compatibility/migration notes for old single-alias grants or installed native Bridge configs, with fail-closed policy and no in-place mutation assumptions.
- Known gaps listed as **NOT VERIFIED** or **BLOCKED**, and minimal operator-approved live acceptance steps.
- One explicit integration handoff stating which shared files were **not** modified and any requested cross-stream follow-up.

Integration owns the final change log, conflict-resolution decisions, unified compatibility matrix, CI run/job evidence, exact-SHA acceptance ledger, release checklist and publication request.

## 10. Current handoff / next action

The L/M/W feature branches have already been created independently from the verified `c0353077760675871538569d6cbff79a7a3a38fa` development baseline, and their implementation changes were committed locally before this Plan was published. Historical baseline and implementation scope above remain unchanged; do not recreate the feature branches or treat source-level test results as public acceptance.

1. Publish **only this Plan** through an isolated GitHub documentation branch and review its PR against current `main`, without introducing the L/M/W feature commits into that PR.
2. After the Plan-only PR is accepted, reconcile the three independent feature branches with the resulting common GitHub base. Preserve platform-native implementation and security ownership boundaries.
3. Push the three feature branches as separate PRs, diagnose CI at the exact head SHA, and stage integration in the documented L → M → W sequence. Never force-push or auto-merge feature code into `main`.
4. Run same-SHA CI, local native/container, isolated real Agent/Repo Ops, security and authenticated public MCP release gates before considering publication.

This document records intent and constraints only; **it grants no standing authorization** to publish a release, operate production services, change credentials or waive a failed gate.
