# ServerFS v0.21.0 Development Plan

Status: **Phases A–C complete; Phase D is next but cannot add hot-path instrumentation until its performance baseline is measured.** The v0.20.0 implementation prerequisite is accepted. Phase A inventories existing operational surfaces; Phase B implements the internal-only observation contract and bounded metric catalog; Phase C adds an opt-in sanitized common projection to the existing macOS/Windows `serverfs service status` while preserving default native status. Evidence is recorded in [`docs/v0.21.0-phase-a-observability-inventory-2026-10-22.md`](docs/v0.21.0-phase-a-observability-inventory-2026-10-22.md), [`docs/v0.21.0-phase-b-observation-contract-2026-10-22.md`](docs/v0.21.0-phase-b-observation-contract-2026-10-22.md), and [`docs/v0.21.0-phase-c-status-convergence-2026-10-22.md`](docs/v0.21.0-phase-c-status-convergence-2026-10-22.md). No new MCP tool, network listener, exporter dependency, collector, or authority was added. Release-facing site/README updates remain deferred to Phase F so stable v0.19 documentation does not advertise unreleased v0.21 behavior.

## 0. Decision summary

- **Frozen theme:** Operational Maturity — Observability & Contract Convergence.
- **Prerequisite:** v0.20.0 is complete and accepted. v0.21 implementation starts from its resulting repository state; this plan does not assume v0.20's proposed work has already landed.
- Define one internal, secret-safe observation model and make existing structured status, JSON/CLI output, `serverfs doctor`, host-observability guidance and Tunnel diagnostics converge on it.
- Establish stable, bounded metric names and semantics for service components and Agent task/lease/recovery behavior. Keep observation independent of exporter choice.
- Only after the internal model and operational consumers are stable, evaluate whether an optional Prometheus or OpenTelemetry exporter is justified. This plan selects no exporter API, wire format, or dependency in advance.
- Preserve narrow ServerFS capabilities, filesystem containment, explicit authorization, provider-owned runtime policy, and every platform's existing lifecycle/privilege boundary. Observability must not expand host authority.

## 1. Goal and completion outcomes

At completion, operators and support tooling can answer, from one consistent and sanitized contract:

1. Is the process alive, ready to serve its declared role, or degraded/unavailable, and why at the component level?
2. Which configured components are enabled, disabled, unsupported, unknown, or unhealthy without exposing credentials or private state?
3. What are the bounded Agent task outcomes, durations, queue pressure, writer-lease contention, and recovery/guard outcomes?
4. Do JSON status, human CLI status, doctor, Tunnel diagnostics and the read-only host-observability recipe agree on state and terminology?
5. Can the same shared contract be exercised on Linux, macOS and Windows while each implementation and service lifecycle remains platform-specific?

Deliverables are an internal observation model, stable metric catalog and cardinality policy, converged existing status/diagnostic projections, deterministic shared contract tests, platform acceptance evidence, operational documentation in English and Simplified Chinese where current-state site copy changes, and a release acceptance record.

## 2. Non-goals

- No expansion of host authority, MCP tools, workdir roots, filesystem permissions, network exposure, service privileges or Agent provider permissions.
- No change to filesystem mutation, path containment, revision, ingress, authentication, Tunnel ownership, Agent authorization, writer-lease or fail-closed recovery semantics except to expose their existing outcomes safely.
- No MCP Tasks, ACP v2, new Agent runtime, new auth provider, A2A, AG-UI or MCP Apps.
- No mandatory monitoring stack, remote telemetry, hosted telemetry service, dashboard, database, or required outbound connection.
- No exporter-first architecture and no preselected Prometheus/OpenTelemetry API, package, dependency, endpoint, or deployment surface.
- No high-cardinality task/workdir/runtime identity labels, tracing payload capture, log aggregation product, or retention of new sensitive event data.
- No forced unification of Linux, macOS and Windows implementations, launch/service managers, process containment, tunnel diagnostics, or filesystem internals.
- No new public MCP tool or schema merely to expose operational status. Existing product and admin surfaces remain distinct; Phase A inventories them before any surface changes.
- No claim that health probes prove an external provider, Tunnel control plane, filesystem, or downstream dependency works beyond the exact check performed.

## 3. Baseline, prerequisite and boundaries

The current stable baseline is v0.19.0. v0.20.0 is the immediate prerequisite and has its own plan. Before implementation, record the accepted v0.20 commit/release state and inspect the resulting implementation, tests and current docs. Any v0.20 observation/status capability that already satisfies this plan is reused, not duplicated.

Current repository evidence includes `serverfs doctor`, a Windows `serverfs service ... status` command, Bridge private-state inspection used by doctor, Agent task/event persistence and recovery tests, Tunnel readiness/control-plane diagnostics, and a read-only host-observability recipe. Their schemas, process boundaries and exact cross-platform availability must be inventoried in Phase A; this plan does not invent a unified endpoint or claim every item is present on every platform.

The observation model is an internal shared contract. Each platform/service adapter maps measured local facts into it. Existing public MCP schemas and stable errors remain product contracts. An operational projection may add documented fields compatibly, but may not silently alter MCP protocol responses or turn an operator diagnostic into a capability or authorization channel.

## 4. Observation model and status contract

### 4.1 Contract shape

Define a versioned internal model, with explicit schema version and observation timestamp, containing:

- **Service identity:** product version, process role, platform family, and supported capability declarations; no hostname, username, installation path, PID, command line, or environment dump by default.
- **Overall state:** `healthy`, `degraded`, `unavailable`, or `unknown`, plus a stable bounded reason code and the time/source of the observation.
- **Components:** stable component key, enabled/disabled/not-applicable/unsupported state, component health, and a small stable reason code. Component details are allowlisted, typed fields only.
- **Probe provenance:** `observed`, `configured`, `not_checked`, or `stale`; report must distinguish measured health from configuration presence and must not imply a dependency check that was not performed.
- **Freshness:** observation time and bounded freshness semantics, so stale cached status is not represented as live health.

The exact serialized field names and enum spelling are implementation-time decisions after inventory. They must be documented once and covered by schema/contract tests before CLI or exporter projections are built. Unknown future enum values must be handled safely by consumers.

### 4.2 Required component coverage

The contract must account for the following domains, with explicit applicable/not-applicable and not-checked states where a deployment does not own or cannot observe them:

| Domain | Minimum safe status facts |
|---|---|
| MCP transport | stdio/Streamable HTTP mode as applicable; listener/process state only where observable; transport probe provenance; no request bodies or peer identifiers |
| Authentication | disabled/configured mode and verifier/broker availability as applicable; never credential, token, OAuth code, principal, scope value, or key fingerprint |
| Workdirs | configured count only if already safely available, policy/config validity, filesystem readiness classification; never root paths, names, contents, or matcher details |
| Tunnel | lifecycle/readiness/control-plane state and last successful observation age where existing diagnostics provide it; no tunnel token, URL carrying credentials, remote identity, or raw control-plane payload |
| File ingress | enabled/disabled and sidecar/service reachability only when actually probed; no URL, file ID/name, payload, destination path, or fetch response body |
| OAuth broker | configured/disabled and broker process/readiness status; never identity records, issuer secrets, codes, tokens, principal IDs, or claims |
| Agent Bridge | enabled/disabled, RPC/store/worker health where measured, runtime availability by bounded configured runtime category; no provider session IDs, prompts, argv, environment or account data |
| Agent runtime | configured/runtime availability and bounded capability/error category; no provider transcript, model-private metadata or executable path |
| Agent tasks | bounded outcome counts and duration/queue summaries; task detail remains behind existing authorized task APIs and is not copied into metric labels/logs |
| Writer lease | held/free/contention and bounded acquisition outcome/duration; never expose lease identity, workdir identity or lock path in aggregate telemetry |
| Recovery | guard present/count only if safe and necessary, reconciliation outcome and bounded reason; never clear/relax a guard to improve readiness or metric state |

A component state can be healthy while the overall service is degraded, or not applicable for a given role. The aggregation table and precedence rules must be explicit and tested. Absence of an optional subsystem is not a failure. A required configured subsystem that cannot be verified is `unknown` or degraded according to the declared role policy, never fabricated as healthy.

### 4.3 Health, liveness, readiness and component status

- **Liveness** answers only whether this process/event loop or managed process is alive enough for its supervisor to avoid an unnecessary restart. It must not depend on optional providers or remote control planes.
- **Readiness** answers whether this declared ServerFS role can accept its intended work now. It may include required local initialization and configuration checks. It must not run mutations, trigger provider work, or claim external reachability unless that exact dependency is probed.
- **Health** is an operator-facing summary of component observations and their freshness, not a synonym for liveness or readiness.
- **Component status** reports one component's measured/configured state and reason. It is not authorization and does not grant access to its backing resource.
- **Unknown/stale** is distinct from healthy and unhealthy. A failed or unavailable probe is reported as unknown/degraded with a stable reason; it must not make a destructive restart decision by itself.
- Existing probe paths remain loopback/private or otherwise within the already-authorized boundary. Do not publish a new listener or weaken Host/Origin/auth policy to make probes easier.

Any existing health endpoint semantics are first documented and tested in Phase A. If a new readiness/liveness surface is justified, its bind address, auth, role and supervisor consumer must be specified and reviewed before implementation.

## 5. Minimal metric model and naming

### 5.1 Stable internal catalog

Define a transport-neutral observation/metric catalog before any exporter. Names use a stable lowercase namespace such as `serverfs_` followed by domain and quantity; the exact final names are frozen by the catalog and contract tests. Names describe units and type, not implementation class names. Proposed minimum families:

| Family | Type/units | Bounded dimensions |
|---|---|---|
| `serverfs_component_state` | state gauge/enum mapping | component, state, process_role |
| `serverfs_component_probe_total` | counter | component, outcome, reason_code |
| `serverfs_agent_tasks_total` | counter | outcome, runtime_family, mode |
| `serverfs_agent_task_duration_seconds` | histogram/summary observation | outcome, runtime_family, mode |
| `serverfs_agent_queue_depth` | gauge | queue_class, runtime_family where bounded |
| `serverfs_agent_queue_wait_seconds` | histogram/summary observation | runtime_family, outcome |
| `serverfs_agent_lease_acquisitions_total` | counter | outcome, lease_class |
| `serverfs_agent_lease_wait_seconds` | histogram/summary observation | outcome, lease_class |
| `serverfs_agent_recovery_total` | counter | action/outcome, reason_code |
| `serverfs_tunnel_state` | state gauge/enum mapping | bounded tunnel role/state |

The final catalog may consolidate families after inventory if the resulting contract still covers the required operational questions. Every family needs a definition, unit, event boundary, reset behavior, allowed dimensions, and whether it is emitted by the process or derived by an operator. Do not promise histogram bucket boundaries until workload evidence and memory/cost budgets are measured.

### 5.2 Outcome and timing semantics

- Agent outcome uses existing terminal task status vocabulary, with explicitly documented handling for interrupted, cancelled, failed, expired and unknown records.
- Task duration begins and ends at stable lifecycle transitions defined in the Bridge contract; queue wait is separate from execution duration.
- Queue depth is a bounded snapshot of eligible in-memory/persisted queued work, not an estimate of provider-internal queues.
- Lease wait measures acquisition latency and contention outcomes; it does not infer that a provider process has stopped.
- Recovery counts represent observed reconciliation/restart decisions, not proof of successful external recovery unless a subsequent observation confirms it.
- Counters are process-local unless an existing durable store supports a carefully defined aggregate. Restart resets must be documented; no new high-volume event store is introduced just for metrics.

### 5.3 Cardinality and sensitivity limits

Only finite, reviewed dimensions are allowed: component, state, process role, transport class, auth mode (not identity), runtime family from configured supported categories, task outcome, lease class and bounded reason code. Values must be normalized into closed enums. Prohibit task IDs, idempotency/correlation IDs, session IDs, workdir names/paths, user/principal/account IDs, model IDs, hostnames, IPs, URLs, filenames, arbitrary exception text, request values and provider-supplied strings in labels or metric names.

Set and test a maximum metric-family count and maximum series estimate per process using the allowed enum cross-product. Exporters must not create series for arbitrary input. Status detail fields are allowlisted; logs use stable codes and may include no sensitive payload. Diagnostic output must never print secrets, tokens, file contents, private paths, prompts, identities, or full environment/config values. Error messages are mapped to bounded reason codes before status/metric emission.

## 6. Surface convergence and exporter decision

### 6.1 Structured status first

Implement the internal observation model first, then project it consistently to:

1. machine-readable JSON status (reusing the narrowest existing CLI/admin entry point where suitable);
2. human-readable CLI status with the same component names, states, reason codes and freshness;
3. `serverfs doctor`, which retains its configuration and local safety checks while sharing status vocabulary and avoiding duplicate contradictory reports;
4. existing host-observability recipe, which consumes read-only local facts and documents what it cannot observe;
5. existing Tunnel diagnostics, which remain the source for Tunnel-specific deep diagnostics and project a compatible summary into the common model.

Do not make `doctor` a continuous monitor or a mutating repair command. Do not collapse a focused Tunnel diagnostic into a generic status dump. Separate component status from configuration validation and from platform service lifecycle commands. Preserve stdout/stderr and machine-readable output conventions of existing commands.

### 6.2 Exporter decision gate

After the internal model, JSON/CLI projections, tests and documented operational questions are stable, evaluate optional exporter value against: operator demand, data sensitivity, cardinality, scrape/push topology, platform parity, dependency footprint, offline behavior, packaging, support/maintenance cost and measurable overhead.

Only then choose among no exporter, an optional Prometheus-compatible projection, or an optional OpenTelemetry integration. Exact APIs, packages and dependencies remain undecided until that probe. Any exporter must be opt-in, disabled by default, local/private by default, absent from core dependency paths, and incapable of sending telemetry externally without explicit operator configuration. It must project the internal catalog without creating a second semantic model. If evidence does not justify one, release v0.21 with the internal model and operator-facing surfaces only, recording the decision.

## 7. Cross-platform shared contract and platform boundaries

Build a shared contract suite around normalized observations and consumer behavior; use platform-specific fixtures/adapters to supply real measured states. Required shared contract areas:

- filesystem: workdir/config status classification and safe redaction; no path-containment implementation changes;
- Agent: task lifecycle, terminal outcome, queue/timing boundaries, cancellation and result redaction;
- cancellation: terminal-first semantics, bounded interrupt, no metric/status that claims stop without evidence;
- recovery: Bridge restart reconciliation, writer lease, recovery guard preservation and observed outcomes;
- lifecycle: process/service liveness, readiness and intentional-stop distinction;
- configuration: enabled/disabled/invalid/unsupported/not-checked component states and safe JSON/CLI/doctor parity.

Linux, macOS and Windows must each map the shared vocabulary without pretending they use the same service manager, process containment primitive, filesystem semantics, tunnel ownership or health probe. Linux OCI/container checks, macOS Apple M-series arm64/macOS 27 app-associated ServiceManagement boundary, and Windows current-user non-elevated Task Scheduler 2.0 lifecycle remain explicit. No hosted test may mutate persistent user services/Login Items/Scheduled Tasks. Unsupported platform/component observations are explicit, not silently inherited from another platform.

## 8. CI, tests, performance budget and fault injection

### 8.1 Deterministic contract tests

Add tests for:

- contract schema/version, closed enums, unknown future enum handling, freshness and deterministic aggregation;
- each component's configured/disabled/not-applicable/healthy/degraded/unknown states where applicable;
- exact JSON and CLI field/state/reason parity; stable exit codes and stdout/stderr behavior;
- doctor and Tunnel diagnostic projection parity without leaking raw diagnostic payloads;
- every metric family name/type/unit/dimension allowlist, counter lifecycle/reset semantics, and cardinality bounds under randomized task/workdir/provider input;
- Agent terminal outcomes, latency boundary selection, queue and lease measurement, cancellation and recovery event semantics;
- redaction of credentials, paths, URLs, task/provider identifiers, prompts and exception strings in captured logs, JSON, CLI and any exporter fixture;
- Linux/macOS/Windows adapter conformance using native jobs/fixtures, while retaining each platform's existing lifecycle assertions.

### 8.2 Failure injection

Inject bounded failures at observation collection boundaries: missing/disabled optional service; permission-denied or inaccessible local state; corrupt/unreadable Bridge state; stale Tunnel probe; provider unavailable; queue/store read failure; lease contention; interrupted task during cancellation; recovery guard present; platform service manager query failure; exporter bind/config failure if an exporter is approved. Verify status becomes accurate unknown/degraded, never crashes the service, emits no secret, triggers no unsafe recovery/authorization action, and preserves existing guards/leases. Do not use live providers or remote telemetry services in deterministic CI.

### 8.3 Performance and resource budget

Before implementation, capture baseline startup time, steady-state CPU/memory and representative Agent submission/completion latency on available reference environments. The shared observation path must be lazy or bounded and must not synchronously block MCP requests on slow/remote dependencies. Status snapshot collection must use bounded local reads, explicit timeouts where probes exist, and no unbounded task/event scan.

Provisional release budgets, to be validated/refined in Phase A with reproducible measurements: no more than 5% increase in idle CPU or resident memory attributable to always-on core observation; no more than 5 ms p95 added to ordinary MCP request handling when metrics are recorded; status/doctor local snapshot p95 under 250 ms on reference test hosts excluding explicitly invoked external Tunnel diagnostics. Optional exporter overhead is measured separately and is zero when disabled. If workload variance makes a threshold invalid, document measured baseline and a maintainer-approved revised budget before coding the affected path; do not waive a regression after the fact.

CI records repeatable before/after benchmark evidence for the changed hot paths. Performance checks should use stable local fixtures and generous noise controls; flaky shared-runner timings are informational, not a false precision release blocker.

## 9. Security, compatibility and rollback constraints

- Read `docs/maintainers/invariants.md` before changing any boundary touched by observation; observation does not authorize access to protected state.
- No secret, token, OAuth code, key, principal, URL query, private path, workdir identity, prompt, file content, provider session ID, task ID, or arbitrary exception text in logs/status/metric dimensions.
- Keep protocol stdout clean. Diagnostic output uses the existing intended channel and is opt-in where current behavior requires it.
- No new network listener, port publication, egress, auth bypass, wildcard Host/Origin, or privileged service. Any exporter is optional and private/local unless an explicit later deployment decision says otherwise.
- Do not change authorization, workdir policy, writer lease, recovery guard, provider sandbox/approval, Tunnel generation ownership or service lifecycle to improve a health score.
- JSON/CLI additions are backward-compatible: retain existing fields, commands, meanings and exit codes unless a separately documented compatibility decision is approved. New consumers ignore unknown fields/enums safely. Public MCP schemas and stable protocol/error contracts do not change for observability alone.
- Metrics are operational hints, not durable audit records or proof of provider stop, authorization, filesystem safety, or successful recovery.
- Rollback is possible by disabling/removing optional exporter wiring and reverting the internal projection while preserving existing commands and operational diagnostics. No persistent state migration or new durable event table is allowed without a separately reviewed rollback/migration plan.
- Keep v0.20 protocol/distribution work and v0.19 runtime support intact. Do not rewrite historical plans/evidence as current-state documentation.

## 10. Phased implementation sequence

### Phase A — Prerequisite and surface inventory — **COMPLETE**

Completed in [`docs/v0.21.0-phase-a-observability-inventory-2026-10-22.md`](docs/v0.21.0-phase-a-observability-inventory-2026-10-22.md). The inventory maps the actual `doctor`, Agent doctor/private-state inspector, Windows/macOS native service status, Tunnel health layers, Bridge task/event store, writer lease/recovery seams, structured logs and read-only host-observability recipe. Each candidate signal has an owner, observed-vs-configured provenance, sensitivity/redaction boundary, platform applicability and include/exclude decision.

Phase A froze these implementation constraints for Phase B: internal model first; no new public MCP tool/listener; configured is not healthy; explicit `observed` / `configured` / `not_checked` / `stale`; no task/workdir/user/path/model/provider identifiers in common status or metric dimensions; native lifecycle adapters remain platform-specific; exporter choice is deferred. Unsupported or unobservable domains remain explicit rather than inheriting another platform's claim.

**Gate: PASS.** No duplicate status service or exporter assumption is required. Reliable reproducible performance baselines were not established in Phase A and are explicitly **Not measured**; they remain a prerequisite before Phase D hot-path instrumentation, not a fabricated Phase A result.

### Phase B — Internal observation contract — **COMPLETE**

Implemented in `src/serverfs_mcp/observability.py` with focused contract tests in `tests/test_observability_contract.py`; design and evidence are recorded in [`docs/v0.21.0-phase-b-observation-contract-2026-10-22.md`](docs/v0.21.0-phase-b-observation-contract-2026-10-22.md). The standard-library immutable model freezes bounded component/applicability/health/provenance/reason vocabularies, snapshot schema v1, deterministic liveness/readiness/health aggregation, unknown-future fail-closed behavior, and the transport-neutral metric catalog/cardinality ceiling. It adds no collector, public projection, MCP schema, listener, exporter dependency, network I/O, persistence, or authority.

**Gate: PASS.** Focused validation: `uv run pytest tests/test_observability_contract.py` → 16 passed. Full root regression: `uv run pytest` → 1724 passed, 317 skipped; `uv run ruff check .`, `uv run ruff format --check .`, and `git diff --check` → PASS. Configured/not-checked/stale evidence cannot assert healthy, disabled optional components do not degrade the role, and arbitrary/high-cardinality metric label values are rejected. Platform mappings remain an explicit Phase C adapter responsibility rather than being invented in the shared contract.

### Phase C — Structured status and operator convergence — **COMPLETE**

Implemented in `src/serverfs_mcp/service_observability.py` and the existing `serverfs service status` CLI. The default native JSON remains unchanged; explicit `--format common-json` and `--format common-human` project the Phase B contract on supported macOS/Windows native lifecycle surfaces without exposing paths, PIDs, task names or raw platform state. Windows maps existing recovery/watchdog/runner + Tunnel-generation evidence; macOS conservatively maps only managed Tunnel lifecycle evidence and does not guess Agent Bridge applicability. Linux Compose keeps its existing Tunnel/private health boundary rather than receiving a fabricated native-service projection. Doctor, Tunnel deep diagnostics and read-only host observability retain their distinct responsibilities; the relationship and public-doc timing are recorded in [`docs/v0.21.0-phase-c-status-convergence-2026-10-22.md`](docs/v0.21.0-phase-c-status-convergence-2026-10-22.md).

**Gate: PASS.** Focused Phase B+C status tests: 22 passed. Full root regression: `uv run pytest` → 1730 passed, 317 skipped; root Ruff check/format and `git diff --check` → PASS. Human/JSON aggregate parity and actual CLI stderr/no-stdout behavior are tested, default native output is unchanged, and unobserved Tunnel state remains `unknown`. Public stable-release site copy is intentionally unchanged until Phase F.

### Phase D — Agent observations and runtime cost

Instrument task outcomes, queue wait/depth, execution duration, lease acquisition/contention and recovery/reconciliation with the stable internal metric catalog. Confirm event boundaries against task state machine and adapter semantics. Run fault injection and before/after performance measurements.

**Gate:** task outcome accounting matches persisted lifecycle fixtures; cancellation/recovery/lease evidence is truthful; budgets pass or have an explicitly approved evidence-based adjustment.

### Phase E — Optional exporter decision and isolated implementation

Review operator need and Phase B–D evidence. Record a decision: no exporter, or one optional exporter direction with a concrete API/dependency/security/platform proposal. If approved for this release, implement only a thin projection of the internal model, with opt-in configuration, private bind defaults, dependency isolation, cardinality tests and disabled-by-default proof. Do not add multiple exporter backends.

**Gate:** documented decision and threat/performance review; core works without exporter dependency/configuration; exporter failures do not affect MCP/Bridge service availability.

### Phase F — Native platform acceptance and documentation

Run shared contract tests plus each platform's real status/lifecycle path. Verify Linux container/deployment diagnostics, supported macOS native arm64 service boundary, and native Windows current-user lifecycle. Update release-facing docs and both site locales where current-state copy changes. Keep historical plan facts intact and classify stale-scan hits.

**Gate:** each supported claim has direct platform evidence; unsupported cells are explicit; English/Simplified Chinese current-state copy agrees; site build and docs gates pass when site files change.

### Phase G — Release readiness

Run all relevant root, Bridge, platform, CI, fault-injection, redaction and performance gates. Review generated status examples and release notes, rollback path, compatibility map, exporter decision and known limits. Produce a release acceptance record with evidence locations and pass/fail status.

**Gate:** every criterion in §14 is satisfied before v0.21.0 is called accepted.

## 11. Test matrix

| Area | Deterministic cases | Required evidence |
|---|---|---|
| Contract | schema/version, enum evolution, aggregation precedence, freshness | shared contract suite and fixture snapshots |
| Component status | transport/auth/workdirs/Tunnel/ingress/OAuth/Bridge/runtime enabled and failure modes | source-to-observation mapping table; no invented checks |
| CLI/doctor | JSON-human parity, exit codes, stderr/stdout, missing/invalid config | subprocess tests on supported platforms |
| Tunnel/host diagnostics | stale/healthy/unknown control-plane observation; safe shared summary | captured synthetic fixture and docs parity |
| Agent task | all existing terminal outcomes, queue wait, duration, duplicate/idempotent submission boundaries | state-machine tests, counters equal terminal fixture set |
| Lease/recovery | free/contended, guard present, stop unproven, reconciliation success/failure | lease/recovery tests; no false release/healthy claim |
| Cancellation | running, terminal-first cancellation, delayed/failed provider interrupt | existing cancellation contract plus observations do not overclaim stop |
| Redaction | secrets, paths, URLs, identifiers, prompts, exception payloads | log/status/metric capture assertions with sentinel values |
| Cardinality | randomized high-volume IDs and arbitrary provider values | finite series upper bound and rejection/normalization tests |
| Failure injection | unreadable state, timeout, stale probe, unavailable provider, failed exporter | bounded degraded/unknown result; service remains safe and responsive |
| Platform lifecycle | native Linux/macOS/Windows status and intentional-stop semantics | per-platform CI/local acceptance evidence; no hosted persistent-service mutation |
| Performance | idle cost, MCP overhead, snapshot latency, optional exporter disabled/enabled | reproducible before/after report and budget decision |

## 12. CI and release gates

### Pull request CI

- Root: frozen dependency sync, Ruff lint/format, root pytest, applicable Compose render and scratch-tag container checks.
- Agent Bridge: frozen dependency sync, Ruff lint/format and Bridge pytest whenever Bridge/model/task observation code changes.
- Shared contract suite runs against all platform fixtures in normal CI; native Windows and macOS jobs execute their platform-specific adapters and subprocess/CLI checks.
- Security regression job scans captured outputs for sentinel secrets/paths and tests the closed metric-label allowlist/cardinality ceiling.
- Fault-injection tests are deterministic and offline; no provider account, IdP, hosted telemetry backend, tunnel control-plane service or Registry dependency is required.
- Performance benchmark is reproducible; noisy hosted measurements are reported separately from deterministic functional gates.
- If site files change, run the existing site gate (`npm ci` and `npm run build`) and review English/Chinese parity. Static build validates syntax/routes, not visual geometry.
- Run `git diff --check`, secret scan and changed-file/scope review. No generated caches, telemetry payloads or local state artifacts are committed.

### Release acceptance gate

- v0.20 prerequisite acceptance is recorded; v0.21 schema and metric catalog have stable definitions and compatibility notes.
- JSON/CLI/doctor/Tunnel/host-observability projections agree on component naming, state, reason and freshness.
- Agent task outcome, queue, latency, lease and recovery metrics reconcile with deterministic lifecycle evidence.
- All sensitive-field, cardinality, failure-injection and safety assertions pass.
- Performance budgets are met or adjusted before implementation through recorded maintainer decision and measurements.
- Linux, macOS and Windows platform claims have evidence within documented support boundaries; unsupported cases are labeled.
- Exporter decision is explicit. If implemented, exporter remains optional, private by default, isolated from core dependencies and off by default.
- Docs and site current-state claims are aligned; rollback and known limitations are documented.
- No MCP Tasks, ACP v2, new runtime/auth provider, A2A, AG-UI or MCP Apps scope has entered the release.
- No tag/release publication is performed by this plan without explicit maintainer action under the existing release workflow.

## 13. Platform acceptance boundaries

### Linux

Run root and Bridge suites on the supported Linux deployment, verify the existing container/service health behavior and Tunnel diagnostics, and ensure status collection uses only local bounded reads unless the existing Tunnel diagnostic is explicitly invoked. Confirm base MCP listener publication, egress, auth and Tunnel ownership remain unchanged. Exercise optional exporter only if selected, using its documented private bind/configuration and scratch deployment. Inspect container dependency diff and idle/resource cost.

### macOS

Acceptance remains Apple M-series, native arm64, macOS 27 Golden Gate. Run root/Bridge native suites and status/doctor checks; verify app-associated `SMAppService`/LaunchAgent lifecycle is observed accurately without changing Login Items or claiming that launch registration means the process is ready. Hosted CI must not mutate Login Items. Do not claim Intel, Rosetta, macOS 26/28 or `darwin-amd64` support.

### Windows

Run native Windows root/Bridge suites and CLI/status subprocess checks. Observe current-user, non-elevated Task Scheduler 2.0/watchdog/runner lifecycle without changing its authority or treating intentional stop as unhealthy restart demand. Preserve native filesystem and revision semantics. Hosted CI must not install or mutate persistent scheduled tasks. No WSL/Docker run substitutes for native evidence.

### Cross-platform rule

The shared model standardizes meaning and redaction; it does not standardize service managers, process containment, health probe transport, filesystem internals or deployment topology. Any platform unable to observe a fact reports `unsupported`, `not_checked` or `unknown` as defined, never a borrowed claim.

## 14. Documentation, compatibility and explicit release acceptance

Update only current-state surfaces required by implemented behavior: README where operator entry points change, relevant operational docs for status/doctor/Tunnel/host observation, configuration reference if an optional exporter is approved, and English root/Simplified Chinese site pages together. Keep shared Astro/Starlight layout, project base `/ServerFS_MCP/`, locale structure and self-hosted font behavior. Use synthetic, secret-free output examples. Do not rewrite v0.19/v0.20 historical evidence except to correct present-tense status where it has become false.

**v0.21.0 is accepted only when all are true:**

1. The v0.20 prerequisite is complete and its resulting state is the implementation baseline.
2. One internal versioned observation contract covers all required domains, represents applicability/freshness/provenance explicitly, and passes shared cross-platform contract tests.
3. Health, liveness, readiness and component status have distinct tested meanings; probes do not cause repair, authorization or restart actions.
4. Structured status, JSON/CLI, doctor, existing host-observability guidance and Tunnel diagnostics converge without losing needed domain-specific diagnostics or changing MCP product contracts.
5. The stable metric catalog covers component state/probes and Agent outcome, latency, queue, lease and recovery; names, units, dimensions, reset behavior and cardinality ceiling are tested.
6. Redaction tests prove that credentials, identities, paths, prompts, contents and arbitrary provider/error data do not reach status, logs or metric dimensions.
7. Fault injection demonstrates safe degraded/unknown handling, no false claims of stop/recovery, no cleared recovery guard, and no service crash or authority expansion.
8. Reference performance measurements satisfy approved budgets; disabled optional exporter adds no runtime/dependency cost. Any selected exporter passes isolation, opt-in, bind, failure and cardinality tests.
9. Linux/macOS/Windows acceptance respects each support and lifecycle boundary and contains direct evidence for every support claim.
10. Required root/Bridge/platform/CI/docs gates pass, English and Simplified Chinese current-state documentation is aligned, and stale-scan hits are classified.
11. Compatibility and rollback instructions are reviewed; no prohibited v0.21 non-goal has entered the release.

A skipped or unavailable check is recorded as **Not verified**, with its platform/environment and impact; it is not reported as passing.
