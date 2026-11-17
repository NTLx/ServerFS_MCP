# macOS v0.25.0 Shared Core crash recovery

Date: 2026-11-16. Result: **PASS** for the isolated opt-in native acceptance
on candidate `9f3c2a7b6d41`; this is not release evidence.

## Scope

Extend the existing opt-in signed two-Core `SMAppService` acceptance to kill
only the randomly owned Core B process after independently validating its
launchd label and PID, active registration, private Shared Core manifest,
exact executable and argv, current UID, signed app identity, and `KeepAlive`
LaunchAgent plist. Unknown or conflicting state refuses the signal. Recovery
must produce a different verified PID within 75 seconds while Core A, the
synthetic Bridge owner, the protected production services, authenticated local
filesystem MCP endpoints, and v2 task ownership remain intact. This phase runs
before the existing owner grant revocation, restore, and key rotation checks.

## Implementation validation

- The signal guard accepts only a random `shsm-b-<10 hex>` identity and checks
  the exact registered launchd label/PID, private manifest pin, exact Core
  config/port/executable/argv, current UID and process command line, valid
  signed app identity, and signed `KeepAlive` plist. The signal call is a
  single `os.kill(verified_pid, SIGKILL)` after a second immediate proof.
- Negative fake-state cases cover wrong UID, port, config, argv and launchd
  label, with no signal; a complete valid fixture proves the guard can pass.
- Authenticated Core B MCP readiness polls within the original 75-second
  recovery deadline. Deterministic tests prove delayed readiness succeeds and
  unavailable readiness times out closed.
- Focused default pytest: **28 passed, 1 skipped**. Ruff check and format:
  **PASS** for both changed test files.
- Candidate source gate: **PASS**, 206 reviewed paths, 2,442 excluded scratch
  entries, HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`.

## Native acceptance attempts

The three bounded attempts are retained chronologically under `/tmp`; every
JUnit, log and numeric exit file is UID-owned mode 0600.

1. `/tmp/serverfs-v0250-shared-core-crash-recovery-2026-11-16.{xml,log,exit}`
   — exit **1**, 70.951 seconds. The exact Core B signal guard correctly
   refused to signal because the helper expected an absolute
   `ProgramArguments[0]`; the signed plist contract uses the literal
   `serverfs-service-launcher`. No SIGKILL occurred and cleanup passed.
2. `/tmp/serverfs-v0250-shared-core-crash-recovery-retry-2026-11-16.{xml,log,exit}`
   — exit **1**. With the plist contract corrected, the new Core B PID and
   signed Bridge RPC worked, but the immediate first HTTP/MCP request raced
   listener readiness and got a connection error. Cleanup passed.
3. `/tmp/serverfs-v0250-shared-core-crash-recovery-final-2026-11-16.{xml,log,exit}`
   — **PASS**, exit **0**, 68.793 seconds; one test, zero failures/errors.
   With readiness polling inside the same recovery deadline, Core B's
   authenticated filesystem MCP and v2 task operations worked after recovery.
   Core A and the synthetic Bridge owner's PIDs remained unchanged; prior and
   new task ownership was preserved, and cross-Core reads were rejected.

Before the final attempt: the exact candidate and its own installed Bridge
0.25.0 Python passed the source/interpreter checks; production labels and PIDs
were stable; no `shsm-*` test job or scratch remained. After the pass, protected
production PIDs were unchanged in Tunnel/Bridge/Cloudflare/HTTP order:
`(1779, 1790, 66419, 40516)`. Only the four default ServerFS labels remained;
no `shsm-*` job or `/tmp/shsm-*` scratch remained. The acceptance's own cleanup
verified and removed both random Core owners and their processes.

No product source, production service, credential, or network configuration
was changed. The `ProgramArguments` mismatch and endpoint readiness delay were
acceptance-harness issues; neither established a product defect.

After the native PASS, full default regressions also passed in bounded runs:

- Root: **2,936 tests**, 0 failures, 0 errors, 420 skipped; exit 0. Private
  JUnit: `/tmp/serverfs-v0250-shared-core-crash-recovery-root-2026-11-16.xml`
  (mode 0600).
- Agent Bridge: **672 tests**, 0 failures, 0 errors; exit 0. Private JUnit:
  `/tmp/serverfs-v0250-shared-core-crash-recovery-bridge-2026-11-16.xml`
  (mode 0600).
- Root Ruff check/format: **PASS**, 720 files formatted.
- Agent Bridge Ruff check/format: **PASS**, 120 files formatted.

Only the requested native acceptance test, its safety tests, this report, and
the reviewed candidate source path list were modified. No product source or
production configuration was changed.
