# Phase G — Claude Windows Runtime Acceptance Evidence (2026-10)

Execution host: WorkPC (Windows 11 x64, local NTFS). All real-provider turns below were
executed against the installed native `claude.EXE` through the public MCP surface, with the
frozen pin `claude-agent-sdk==0.2.156` running under the Bridge venv. No model was ever
specified: every turn used the provider-native default configuration, and no ServerFS-owned
credential path was created.

Status legend: **Pass** = executed and green on this host;
**Not verified** = not executed here (reason recorded).

## 1. Branch and frozen implementation

- Branch `phase-g-claude-windows-runtime`, based on `main` after the Phase F merge
  (`39ce843`).
- Product changes are surgical: `adapters/claude.py` (runtime proxy overlay, init-message
  session persistence), `main.py` (one wiring line), plus their tests and the G2 acceptance
  driver. No other adapter, no lifecycle, containment or transport change: Phase F's frozen
  infrastructure was reused as-is.

## 2. Prerequisites (Phase 0E base, revalidated live)

- Phase 0E (2026-10-04) proved the frozen pin installs on Windows, exposes every name the
  adapter imports, and completes a control round trip against the native CLI.
- 2026-10-08 read-only preflight (replacing the 10-04 evidence, as required): `claude.EXE`
  resolved at the native path, SDK import clean, real `connect` → `get_server_info` →
  `interrupt` → `disconnect` all clean. The server-info payload was never printed.
- Deterministic suite: `agent_bridge/tests/test_claude_adapter.py` — the stale module-level
  `linux_only` skip (whose flock rationale Phase D had already removed) was deleted, and the
  fixture now constructs `LeaseManager` with `policies.lease_ids()` exactly as `main.py` does,
  which is what let the whole suite execute on Windows for the first time: 11 pre-existing
  tests passed immediately, then grew to 19.

## 3. G2 Phase 1 — probe, direct real turn, session creation (Pass)

- `list_agent_runtimes`: claude listed, available, version present.
- Direct real turn (`use_proxy=false`): `submit_agent_task` → `succeeded`, workspace artifact
  byte-exact. This arm also answers the connectivity question: the WorkPC Claude provider path
  is reachable **direct**.
- Session creation: `native_session_id`/`native_turn_id` present in the TaskStore, and the
  public task projection hides both.
- Ownership (own Bridge via `require_own_bridge`) and chain cleanup passed.

## 4. G2 Phase 2 — proxy paired causality (Pass)

Paired experiment per the ruling, both questions answered separately:

1. **Does Claude consume the injected proxy?** Yes — proven, not assumed. With
   `use_proxy=true`, the supervisor's dedicated Agent endpoint was overridden to this run's own
   credentialless local forwarder (applied pre-spawn via the bootstrap namespace). The real turn
   succeeded, and the forwarder observed **17 CONNECTs, all external, 0 loopback targets**;
   **17/17 external CONNECTs carried a client PID attribution** (OS-level, queried via
   `Get-NetTCPConnection` → `Get-Process`), and the aggregate client images included `claude`
   (and `python`, a claude-side child tunneling through the same proxy). The attribution helper
   queries concurrent established clients on the listener port, so this evidence does not claim
   a one-to-one mapping of each CONNECT to a specific claude PID — the frozen contract requires
   real provider traffic attributable to the provider child, which is what was measured. The
   SDK↔CLI control channel is stdio and never touches the proxy; no fabricated loopback-bypass
   evidence was produced.
2. **Is the proxy required here?** No — the Phase 1 direct arm succeeded. Deployment fact now
   recorded: **Claude consumes `ClaudeAgentOptions.env`-injected `HTTPS_PROXY`; on this host
   direct access also works, so `use_proxy=false` remains a sane default and the Agent proxy is
   an optional routing policy, not a connectivity requirement.**

## 5. G2 Phase 3 — provider semantics, recovery, cleanup (Pass, 8/8)

- **file mutation**: real second write, artifact byte-exact.
- **continuation**: `continue_from` → succeeded, same native session, continuation's own turn
  id, artifact exact.
- **approval**: provider-originated request (`approval.requested` / `approval.resolved` /
  answered through `respond_agent_approval`), then the artifact.
- **question**: real `AskUserQuestion` round trip (`question.requested` / `question.answered`),
  selected marker written exactly.
- **interrupt** (independent evidence surface): a command observed mid-turn (start marker on
  disk, claude child PID baseline-diffed as new); after `cancel_agent_task` the claude child
  disappears within the bounded window and the completion marker is never written.
- **cancellation** (independent evidence surface): terminal `cancelled` on the public
  projection, task in the chain's own store, and a follow-up task succeeds — the writer lease
  was released with no observed delay. Neither gate derives from the other.
- **recovery**: abrupt supervisor/Bridge kill with the turn mid-flight. Containment held (Job
  reaped the Bridge and the claude child; a breakaway bystander survived). With the session
  durable pre-crash (see section 6), reconciliation classified the task `interrupted` and the
  two recovery facts were pinned separately from the public event surface:
  - `runtime.reconcile_finished`: `SESSION_RESUMABLE`, `provider_active: null` — the Claude
    adapter's own classification (identity known, liveness unknown);
  - `task.reconciled` (`BRIDGE_RESTARTED`): `SESSION_RESUMABLE`, `provider_active: false` —
    the service/startup layer combining the adapter's answer with the Windows containment
    proof, the only path allowed to turn unknown into inactive.
  - Post-restart continuation succeeded: same native session, its own turn id, artifact exact,
    and the pre-crash completion marker never appeared. This is a **session resume**, not an
    in-flight reattachment.
- **cleanup**: both chains' Bridges stopped, zero claude children remaining.

## 6. Real-smoke product defects found and fixed (regression red → fix → green → real rerun)

1. **`env=None` crashes the frozen SDK's spawn path.** Bisecting the adapter's exact options
   shape against a real turn measured `CLIConnectionError: Failed to start Claude Code:
   'NoneType' object is not a mapping` — the SDK field's real default is `{}`. Fix: the
   proxy-free arm **omits** the `env` parameter entirely (absence is exactly pure inheritance);
   the proxy arm passes a real mapping. Regression:
   `test_claude_never_passes_env_none_to_the_sdk`. Non-vacuity mutations for the earlier proxy
   pins (whole-environment overlay; dropped fail-closed; dropped `runtime_proxy` wiring) all
   redden their gates.
2. **Session identity was persisted only at turn completion.** The provider announces the
   native session in the `init` system message; the adapter discarded it, so a crash mid-turn
   was artificially classified `NOT_RECOVERABLE`. Fix (Qoder-shaped, maintainer-approved): the
   adapter handles `init` and persists `data["session_id"]` via the existing COALESCE path (no
   schema change; guard and store both update), and the continuation seed keeps
   `active.session_id` consistent. Regression:
   `test_claude_persists_the_native_session_before_the_turn_completes` (session durable
   mid-turn in store **and** guard, public projection still hides both). Non-vacuity: removing
   the persistence reddens it.
3. **Recovery classification is pinned, not inferred.** The recovery gate reads the two public
   events above and fails unless the adapter's own classification is
   `SESSION_RESUMABLE`/unknown and the bridged event is `SESSION_RESUMABLE`/`false`. Mutation
   check: forcing `reconcile_task` back to `NOT_RECOVERABLE` reddens the gate
   (`adapter_classification_session_resumable=false`, `effective_provider_inactive=false`).
   Adapter semantics stay exactly `SESSION_RESUMABLE` + `provider_active=None`; identity
   durability is not a liveness claim.

## 7. Deterministic gates — WorkPC local Windows evidence

Executed on the WorkPC itself (the Windows host), not in CI:

- Bridge suite: **496 passed / 21 skipped / 0 failed** (Windows), including 19 Claude tests.
- Root suite: **1389 passed / 130 skipped** (Windows), D9 native-lifecycle file: **29/29**.
- `ruff check .` and `ruff format --check .`: clean in both packages.

Note: an environment incident during this run is recorded as a local fix, not as CI evidence —
`uv sync --frozen` on the root venv uninstalled the locally installed native wheel (the trap
Phase C §38 recorded), D9 then failed with `ModuleNotFoundError: serverfs_windows_native`, a
clean-baseline comparison at `39ce843` reproduced it, and reinstalling
`dist/serverfs_windows_native-0.10.0-cp312-abi3-win_amd64.whl` restored D9 to 29/29.

## 8. Residual limitations

- Live steer remains `false` (falsified until the provider proves steering semantics); Claude
  model discovery remains `unsupported` (provider exposes no catalog through this path).
- The proxy gate measured one host: the deployment fact recorded in section 4 is
  host-specific, not a universal claim.
- The proxy gate's attribution is aggregate: `client_images_observed` included `python`
  alongside `claude` (a claude-side child tunneling through the same proxy), and the helper
  queries concurrent established clients on the listener port rather than proving a per-CONNECT
  one-to-one mapping. What the frozen contract requires — real provider traffic attributable to
  the provider child, with the aggregate images containing `claude` — is what was measured.

## 9. CI record

The last code-bearing PR head before the docs-only review fix was `95f3046`. GitHub Actions
for that code-bearing head ran the `Container` workflow (run 37865530233):

| Job | Result |
| --- | --- |
| Test (Linux root suite) | Pass |
| Agent Bridge test (Linux Bridge suite) | Pass |
| Container check | Pass |
| Publish | Skipped |

The subsequent evidence-only review commit changes no product or test code. Its PR-head
`Container` workflow was also required to be green before merge; that status is a review-time
GitHub fact rather than a SHA embedded back into this tracked evidence file.

The **Windows-native workflow was not triggered**: its `pull_request` paths filter covers
`native/**` and `src/serverfs_mcp/**` (plus named root-test files), none of which this PR
touches — the changes are confined to `agent_bridge/`, the G2 driver, docs and the plan. The
Windows-side Bridge evidence for this phase is therefore the local WorkPC run in section 7
(the host itself is Windows), and the Windows-native gate set remains green from the Phase F
merge CI at `39ce843`, which this PR does not put at risk.
