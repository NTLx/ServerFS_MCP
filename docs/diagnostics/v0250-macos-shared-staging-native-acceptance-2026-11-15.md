# macOS v0.25.0 Shared Core staging + signed native preflight — 2026-11-15

## Scope and release position

Completed a new inactive shared-Core staging/unstaging implementation and executed
macOS signed-bundle preflight in an ephemeral named instance. Production
ServiceManagement registrations, launchd, Cloudflare/OpenAI Tunnels, Core,
Bridge, credentials and other instances were not changed. No Git commit,
push, tag, release or cutover occurred; existing dirty working-tree changes
were preserved.

The Bridge owner owns grant provisioning/rotation/revocation, the Bridge
listener and process lifecycle. A Shared Core owns **only** its HTTP Core
service, instance-local data, logs and signed app.

## Implemented paths

- `src/serverfs_mcp/darwin_instance_shared_staging.py` (new):
  `stage_shared_core` requires an explicit non-default Shared identity,
  private owner-supplied binding, eligible Agent Core TOML, canonical
  current-user-owned Python, supported HTTP auth snapshot and unused TCP port.
  It generates only a new private state root, manifest and signed Core app,
  validates the staged manifest against the pinned interpreter, and rolls
  back its new candidate on error. It does **not** register a service, create
  a Bridge, grant, or owner configuration.
- `src/serverfs_mcp/darwin_instance_shared_manifest.py`: add opt-in
  `expected_executable` for inactive staging; running Core retains the
  existing `sys.executable` check by default.
- `src/serverfs_mcp/darwin_instance_cli.py`: explicit `shared-stage`
  (requires `--bridge-id`, owner binding and core-python) and
  `shared-unstage` operations alongside previous shared start/stop/status.
- `src/serverfs_mcp/darwin_instance_shared_unstage.py` (new):
  remove **only** a pristine inactive named Core after strict identity,
  ServiceManager registration, launchd PID, empty-state and signed-app
  checks. No implicit stop or deletion of logs/task state; never manages
  the shared Bridge.
- `native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift`:
  correct a native macOS alias mismatch in strict path equality. The
  Python-resolved physical path `/private/tmp/...` had been compared to
  Foundation `standardizedFileURL` spelling `/tmp/...`. Retain raw
  validated paths for exact manifest equality instead of silently rewriting
  them; reject `//`, trailing slash and lexical `.`/`..` components.
- New synthetic staging, unstage, Swift path and opt-in signed-bundle
  acceptance tests:
  `tests/test_darwin_v0250_shared_staging.py`,
  `tests/test_darwin_v0250_shared_unstage.py`,
  `tests/test_darwin_v0250_shared_swift_gate_paths.py`,
  `tests/test_darwin_v0250_shared_native_signed_gate_macos.py`.

## Evidence, exact meaning

1. Initial four-file synthetic staging/Shared tests: **65 passed**.
2. After staging formatting, staging/manifest tests: **31 passed**.
3. After Swift physical-path fix, source-to-Swift contract tests:
   **58 passed**.
4. Combined staging/unstage/Swift gate/manifest/lifecycle synthetic tests:
   **84 passed**.
5. **Actual native signed App isolated acceptance: 1 passed, exit 0**.
   The opt-in test used `SERVERFS_ENABLE_EPHEMERAL_SIGNED_CORE_TEST=1`
   and a fresh pytest `/tmp` root; it built a signed app, performed
   `codesign --verify --deep --strict` for template and staged app,
   validated `--validate-only <id> http-origin`, manager
   `register-preflight http-origin` and read-only status, rejected
   manifest identity tampering and insecure 0644 auth snapshot, then
   executed `shared-unstage` on exactly its random `shgate<hex>`
   candidate. No app registration or service start occurred. All stages
   succeeded without creating Core logs or Bridge socket.
6. Verified after native acceptance: **zero shgate candidate instances**,
   no matching launchctl registrations, removed unique scratch root;
   ruff check and format --check on the four named native/unstage tests:
   exit 0; `git diff --check`: exit 0.
7. **Final expanded macOS regression: 510 passed, 0 failed, 1 skipped**
   (511 collected; exit 0). The skipped test is the opt-in signed App
   acceptance already passed separately. The real two file-only Core
   subprocess test is separately excluded because it was executed in an
   earlier batch. Final Ruff lint and format checks on the eight affected
   Python paths, plus `git diff --check`, all exited 0. Existing default,
   named file-only, owned, Shared Bridge v2, Connector, Swift and service
   compatibility paths showed no failing tests in this selected suite.

These test sets overlap; **do not add their numbers** to claim unique
coverage. Native signed preflight is not the same as a true registered,
running two-Core-to-one-Bridge deployment.

## Diagnostic and correction

A first native signed preflight returned exit 1 with generic
`shared Core manifest validation failed`, and a source-vs-Swift gate
test rejected the *actual* staged manifest despite accepting handwritten
fixtures. A disposable instrumented Swift copy pinpointed the rejection
to a four-field path guard (source line 253). Physical paths were
`/private/tmp/...`; Foundation-standardized expected paths were
`/tmp/...`. After preserving the raw strict path identity, the
source-to-Swift test and real signed preflight passed. Debug instrumentation
was removed from product tests; source itself does not emit secret-bearing
paths on failure.

Independent Foundation experiments confirmed setting process `HOME`
does not change `FileManager.default.homeDirectoryForCurrentUser`.
The signed acceptance test therefore uses a unique test-owned candidate
in the actual current user's ServerFS instances tree, never touches
existing instances, never registers LaunchAgents, and verifies cleanup.
`.venv/bin/python` is a symlink and may not be used as the pinned
canonical executable; the resolved, owned Python binary is required.

## Still blocked / not claimed

- No actual Shared Core SMAppService register/start/stop of two signed
  Cores against one independently owner-managed Bridge.
- No complete native two-Core HTTP ingress plus authenticated Bridge v2
  task submission from each Core and cross-Core authorization checks.
  Existing in-process real Darwin Bridge IPC tests are separate evidence.
- No owner-controlled rotation/restart recovery through registered
  native jobs, no public Cloudflare connector + ChatGPT E2E.
- No exact-commit-SHA cross-platform Linux/Windows/macOS full release
  acceptance, Git publication or production cutover.

Next logical gated batch: create disposable owner Bridge + grants and two
fresh signed Core candidates under uniquely named test identities, then
perform explicitly scoped native registered/started E2E with strict rollback
and verification that Bridge lifetime is independent. Do not change
production ServiceManagement components.
