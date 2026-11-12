# ServerFS v0.24.3 — Native macOS Cloudflare Production Workdir Parity

Status: **implementation in progress** (2026-11-10). Phase A baseline review and Phase B's narrow installer change and deterministic regression passed; full macOS public writable-workdir/Agent acceptance and production parity are **not yet verified**. No release authorization or publication is implied.

## Baseline and prerequisite

- The GitHub `main` branch was independently verified at commit `5e952cb9f5b787670a0d11fecea2b69720f16083`. The Mac checkout was initially five commits behind at `75a913c1a22dd783e1eefb1f1c1baf9c837995cd`. After diagnosing a failing local SSH proxy, the Mac completed a **verified five-commit `git pull --ff-only`** using a one-command SSH `ProxyCommand=none` override. Final Mac `HEAD == origin/main == 5e952cb9f5b787670a0d11fecea2b69720f16083`; tracked files are clean and this plan is the only untracked file. No persistent Git/SSH/proxy configuration or running Tunnel service was changed.
- GitHub `main` identifies **v0.24.2 as the current stable release**. That release adds Windows-native Cloudflare coexistence and measured shared writable-workdir/Agent E2E, subject to its explicitly recorded recovery-test waiver. Its acceptance is useful precedent, **not evidence of macOS acceptance**.
- The source baseline is now synchronized. Before implementation, recheck `main` against the intended upstream (and fast-forward again only if newer commits exist), confirm a clean tracked state except this planning document, and re-read any changed implementation/tests and maintainer guidance at that HEAD. Never use `reset`, `stash`, forced checkout, or an unverified downloaded snapshot as a substitute for Git synchronization.
- Review `AGENTS.md`, `docs/maintainers/invariants.md`, `docs/maintainers/workflow.md`, `dev_plan_v0.24.1.md`, `docs/releases/v0.24.1.md`, `docs/releases/v0.24.2.md`, and `deployment/macos/README.md` before implementation. Historical candidate text is dated evidence, not current release state.

## Goal and success definition

On supported native Apple Silicon/macOS 27, an explicitly authenticated Cloudflare Streamable HTTP ingress must be able to use the **same authoritative native `serverfs.toml` and effective capability policy** as the concurrently running OpenAI Tunnel: the same workdir aliases/roots, default or explicit read/write permissions, hidden-file and deny policy, optional binary transfer, enabled Agent runtimes, shared Agent Bridge/writer lease, and optional Repo Ops **only where separately enabled by policy**.

The Cloudflare Connector Token authenticates only the connector. The HTTP origin still independently requires an approved ServerFS API Key or OAuth identity, exact public Host validation, loopback-only binding and TLS at the Cloudflare boundary. Successful authentication does not introduce any new per-tool entitlement model: an admitted caller obtains only the capabilities already exposed by the configured ServerFS workdirs and policy.

This is a **macOS-specific removal of a staging-only installer restriction**, not a general relaxation of filesystem or credential protections.

## Evidence and root cause

The macOS `src/serverfs_mcp/darwin_service.py::_cloudflare_service_specs` currently contains:

```python
if not workdirs or any(not item.read_only for item in workdirs):
    raise DarwinServiceError("Cloudflare service requires read-only staging workdirs")
```

The workdir-level default is read-only and the existing native configuration parser already supports explicit `read_only = false`. Native `serverfs serve --transport streamable-http` uses the same `load_native_config` and `_native_agent_settings` wiring as native stdio, including a fail-closed macOS Agent Bridge policy-sync check. v0.24.1 limited installation to read-only fixtures to isolate initial acceptance while preserving the existing OpenAI Tunnel. That was a **release acceptance boundary**, not an architectural Cloudflare requirement.

The two ingress processes must continue to share the same macOS Agent Bridge endpoint/lock identity when the selected config requires Agent/Repo Ops. Their independent MCP session/authentication state does not confer an independent writer lease or permission bypass.

## Scope and non-goals

**In scope**
1. Remove only the unconditional all-workdirs-read-only admission check in the macOS Cloudflare install path; continue rejecting an empty workdir configuration and retain the native parser's default read-only and Agent/mutation gating.
2. Permit the HTTP origin to be registered against the **same canonical config file** used by the active OpenAI service; verify the actual manifest/config paths and resulting effective policies before rollout.
3. Add macOS-focused deterministic policy/HTTP/Agent regression tests and live public MCP acceptance, with a reversible Cloudflare-only deployment and clear production migration instructions.
4. Align current macOS runbook, Cloudflare and configuration docs, Agent Skills, English/Chinese site, version/release artifacts after tests.

**Out of scope**
- No new generic shell, unrestricted process execution, wildcard Host/Origin, uncontrolled HTTP port, filesystem traversal exceptions, credential visibility, default write access, or automatic new Agent/Repo Ops privileges.
- No automatic replacement, shutdown or service migration of the existing OpenAI Tunnel; do not touch Agent Bridge lifecycle merely to change the connector.
- No modification to Linux Cloudflare topology or to already released Windows v0.24.2 dual-owner semantics, aside from regression tests and accurate cross-platform documentation.
- No new permission flag or duplicated second production TOML merely to work around the existing `read_only` model, unless a concrete security finding establishes its necessity.
- No automatic Cloudflare DNS/Tunnel/Access policy changes or credential rotation.

## Phases and implementation tasks

### Phase A — Fresh baseline, architecture/security gate

1. Reconfirm the now-synchronized Mac `main` remains at the intended upstream HEAD, and that the stable v0.24.2 source baseline matches GitHub before implementation. Inspect the exact installed native CLI and ServiceManager bundle as distinct from checkout HEAD.
2. Capture sanitized, read-only production baselines: OpenAI/Bridge/Cloudflare and HTTP Origin service ownership/PIDs, manifest config paths, effective workdir/policy list, caller-auth mode, exact public Host, private token/env paths and service health. Never report secret contents.
3. Confirm that the existing OpenAI service is using the intended canonical TOML and that two native frontends resolve the same Bridge socket, data home, writer lease and policy snapshot. Identify any divergence before implementation. Avoid resetting a live Bridge or changing provider configuration to force agreement.
4. Threat-model the new public capability: bearer-key reuse/leak, write and Agent delegation by an admitted caller, two-frontends concurrent file edits, cross-ingress task ownership/approval, private credential placement, and rollback while jobs are in flight. Record which risks are already governed by existing policy and which require targeted tests.

**Gate A:** actual synchronized SHA and a documented, security-reviewed minimal change with no regression to caller authentication or shared Bridge ownership.

### Phase B — Minimal product change and deterministic regression

1. In `_cloudflare_service_specs`, replace the staging-only rejection with the necessary **non-empty workdir** admission check. Preserve validation of config ownership/path, native config parsing, canonical workdir roots, symlink components, private Connector Token/env file mode/location, pinned executable, exact Host, API Key/OAuth mutual exclusion, loopback port and singleton ServiceManagement ownership.
2. Add focused tests to `tests/test_darwin_cloudflare_service.py`: explicit writable workdir accepted; read-only/default workdir remains accepted and unchanged; zero workdirs rejected; malformed configs rejected; private token/env inside **any** configured workdir rejected; unsafe symlinks/mode/Host/auth rejected; origin spec points to the intended shared config; installation failure/rollback does not alter OpenAI/Bridge.
3. Test native Streamable HTTP with a writable config: correct `list_workdirs` access, authenticated file create/edit/delete and revision guards, negative read-only/deny/traversal/credential paths, binary-transfer gating and Agent/Repo Ops exposure strictly matching the loaded policy. Tests must not silently disable policy-sync verification.
4. Verify startup fails closed if Agent policy does not match the live Bridge. Verify the accepted matching configuration addresses the same Bridge socket and lock state as native OpenAI ingress, rather than creating a second unmanaged Agent Bridge.

**Gate B:** tests, Ruff and macOS-focused service-spec regression pass. No claim of public runtime acceptance from unit tests alone.

### Phase C — Isolated live macOS acceptance, then guarded production parity

1. Use a disposable, authenticated, least-privilege writable test workdir on the supported Mac to validate the new HTTP Origin end-to-end through a **separate or deliberately scheduled Cloudflare service window**. Respect the macOS native Cloudflare ServiceManagement singleton; never register an uncontrolled second Cloudflare service pair. Keep OpenAI Tunnel and Agent Bridge live.
2. Public HTTPS/MCP checks: missing/wrong credential `401`; authenticated `initialize`/`tools/list`/`list_workdirs` `200`; real filesystem create/edit/read/delete of only a disposable test file; read-only and denied-path refusal; binary upload/download only where enabled; persistent SSE >60 seconds and fresh-session behavior after a controlled Connector-only restart. Do not claim lossless SSE replay.
3. Validate Agent delegation through the public Cloudflare endpoint: runtime discovery, a bounded benign Codex task, lifecycle/events and shared Bridge coordination. Test Claude/Qoder where both are actually enabled and available; label an unavailable runtime **not verified**, never silently infer parity. Test cross-ingress OpenAI ↔ Cloudflare access and writer lease behavior without invoking parallel conflicting writes against valuable data.
4. After isolated acceptance, perform a **separate controlled production re-registration of the Cloudflare/HTTP Origin pair only** using the authoritative production TOML, preserving the current private Token and caller-auth env. Check that the original OpenAI Tunnel and Bridge PIDs/health remain stable and that both connections expose the same effective capabilities. Never mutate production workdirs just for a demonstration; use an explicitly disposable file and clean it up by the supported revision-guarded tools.
5. On any failure, restore the previous read-only Cloudflare origin registration with the existing private config/env; preserve OpenAI Tunnel/Bridge and all production secrets. Do not delete rollback inputs before successful revalidation. If the Cloudflare service pair cannot be safely rolled back, stop and present the exact recovery state.

**Gate C:** supported real Mac, connected Cloudflare Tunnel, independent auth/Host checks, public read/write and applicable Agent E2E, identical effective workdir/policy lists on both ingresses, no OpenAI/Bridge interruption, recoverable Cloudflare-only rollback.

### Phase D — Documentation, build and v0.24.3 release

1. Update macOS native deployment runbook and `skills/serverfs-tunnel/references/macos.md` to distinguish the **historical v0.24.1 read-only staging acceptance** from the new explicitly configured production write support. Update `skills/serverfs-configure`, `skills/serverfs-auth` and other impacted Skills only as necessary; do not rewrite historical acceptance records.
2. Align current README/README_EN, Cloudflare docs, release notes and bilingual site. Clearly document independent caller authentication, shared workdir/Agent authority, existing default read-only behavior, CLI install/uninstall procedure and migration/rollback steps.
3. Run project validation appropriate to affected boundaries: `uv run ruff check .`, `uv run ruff format --check .`, full root `uv run pytest`, macOS native/HTTP/Cloudflare regression, Agent Bridge tests if its source changed, native ServiceManager bundle/wheel packaging, Plugin Skills validation, Linux/Windows nonregression CI and site build. Capture exact commands/results, not inferred success.
4. Only after all gates pass, align versions/locks/Plugin release surfaces to **0.24.3**; run exact final-SHA CI and the required dispatch-first release workflow. Commit, push, tag and publication require a separate explicit operator instruction.

**Gate D:** published artifacts and documentation reflect only measured macOS support; no earlier `v0.24.3` release or commit is implied by this plan.

## Implementation evidence — 2026-11-10 (unreleased working tree)

- **Phase A baseline:** `main == origin/main` at `5e952cb9f5b787670a0d11fecea2b69720f16083`, installed `serverfs-mcp` v0.24.2. Live OpenAI Tunnel/Agent Bridge/Cloudflare Connector/HTTP Origin were enabled with verified PIDs 1779/1790/1784/1786, respectively, and did not restart during the test run. OpenAI uses the two writable workdirs in the checkout's authoritative `serverfs.toml`; the Cloudflare HTTP origin still points to the separate, one-workdir read-only private TOML. OpenAI `policy_is_synced` returned `True`; Cloudflare shared Agent Bridge parity is not yet runtime-verified. An existing Tunnel handover diagnostic was inconsistent with its live PID; do not claim full historical handover success.
- **Phase B code:** `src/serverfs_mcp/darwin_service.py` removes the unconditional all-read-only workdir gate while retaining at-least-one workdir admission; the native parser's early empty-config error receives a Cloudflare-specific sanitized error. No filesystem, Agent Bridge, auth, Host or credential authorization primitives were changed. Targeted tests were added to `tests/test_darwin_cloudflare_service.py` and `tests/test_darwin_cli.py` for explicit writable/default read-only, empty workdirs, private token/env inside a second writable root, auth restrictions and HTTP Agent policy-sync failure.
- **Executed verification:** targeted Cloudflare tests **28 passed, 1 skipped**; HTTP/stdio policy-sync **2 passed**; full root `uv run --no-sync pytest` **1766 passed, 408 skipped, 1 warning** (66.04 s). `uv run --no-sync ruff check .`, `uv run --no-sync ruff format --check .` (516 files), Plugin validation and `git diff --check` all passed. Do not treat skipped tests as executed evidence. No Docker build or public/macOS live writable acceptance was performed.
- **Current-source documentation:** staged candidate-only macOS runbook, native Tunnel Skill and English/Chinese macOS website notes; released v0.24.2 behavior is explicitly preserved. Historical v0.24.1 acceptance text is not overwritten.
- **Completed documentation checks:** `uv run --no-sync pytest tests/test_agent_skills.py` **30 passed**, Plugin validation passed and `npm --prefix site run build` generated **41 pages**; English/Chinese candidate status remained consistent. `git diff --check` passed.
- **Pending gates:** macOS public MCP write/Agent/binary/SSE test and shared-workdir parity, Cloudflare-only reversible re-registration, cross-platform CI, final release metadata and exact-SHA publication. Production Cloudflare stays on its read-only config until the live gate is passed.

## Phase C blocked acceptance ledger — 2026-11-10

- **Ephemeral local HTTP Origin (not verified):** a bounded standalone loopback MCP test used a disposable private workdir, but the child did not become reachable; the exact startup failure could not be established from the discarded output. The local HTTP write/revision/401/200 flow was therefore not verified by this attempt. The disposable directory and child were confirmed absent afterward; the four original managed service PIDs remained unchanged.
- **Managed Cloudflare-only staging preflight (stopped safely):** the read-only precheck confirmed the installed venv imports the updated checkout source and the public Cloudflare MCP endpoint rejects unauthenticated requests with HTTP 401. It found Bridge `active_task_count=1` while the checking Codex task itself was active, plus two historically observed non-managed-process PID references which disappeared; the latest `unmanaged_tunnel_pids` list was empty. Task ownership could not be conclusively established under that gate, so the Agent stopped **before** creating fixtures or unregistering any service. A follow-on delegated cutover request was denied by the execution environment's safety gate before running. Do not route around that restriction.
- **Production preserved:** HTTP Origin/Cloudflare Connector still use the previous read-only private TOML and the original authenticated hostname; OpenAI Tunnel and Agent Bridge remain available. No public writable operations, cloudflare-only service reinstall or shared Agent E2E were performed.
- **Required to resume Phase C:** use an authorized execution environment to establish active-task ownership and unmanaged-process status without bypassing safeguards, capture sanitized service snapshot and rollback inputs, then perform isolated authenticated public MCP acceptance through the existing singleton Cloudflare service pair. Keep the original OpenAI Tunnel and Bridge untouched. Until actual evidence is recorded, mark all runtime parity and production migration gates **NOT VERIFIED**.

## Acceptance matrix

| Boundary | Required evidence |
| --- | --- |
| Default policy | Absent or true `read_only` stays read-only; writable requires explicit false |
| Installer admission | Read-only and explicit writable configs accepted; empty/malformed denied |
| Credentials | Private token/env outside all workdirs; correct owner/mode; auth mode exactly one |
| Public HTTP | HTTPS; loopback origin; exact public Host; invalid/missing API Key or OAuth denied |
| Filesystem | Aliases/paths/policies identical; allowed file edits pass; revision/deny/traversal guard remains |
| Binary | Upload/download strictly governed by the configured opt-in |
| Agent / Repo Ops | Same enabled runtimes/Bridge/lease; Repo Ops remains disabled if original disabled |
| Parallel operation | OpenAI Tunnel and Bridge unaffected; no duplicate uncontrolled native service |
| Recovery | Cloudflare-only failed install/restart can restore previous read-only pair |
| Client protocol | Public MCP discovery/operations and >60-second SSE actually pass |
| Release | Native Mac + cross-platform regression + docs/Skills/site + final exact-SHA gates |

## Current state / next action

**Phase A/B implemented, Phase C/D incomplete.** Mac Git synchronization completed at `5e952cb9f5b787670a0d11fecea2b69720f16083`. The Cloudflare installer now accepts explicitly writable workdirs; the non-empty config guard, credential isolation, HTTP auth/Host checks and native Bridge policy-sync guard remain enforced. Candidate docs/Skills/site text is explicitly marked unreleased. Current running Cloudflare origin has **not** been re-registered against the production writable TOML; actual public write/Agent parity remains **Not verified**. Next actions: complete isolated runtime acceptance and controlled Cloudflare-only production cutover with rollback, then document actual evidence. **No commit, push, release or OpenAI Tunnel retirement has occurred.**
