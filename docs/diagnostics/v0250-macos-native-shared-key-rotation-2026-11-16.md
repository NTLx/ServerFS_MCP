# macOS v0.25.0 — Shared Core owner grant key rotation

Date: 2026-11-16. Result: **PASS** for the isolated opt-in native acceptance on
the reviewed private candidate `9f3c2a7b6d41`; this is not release evidence.

## Acceptance

The existing `tests/test_darwin_v0250_shared_smappservice_live_macos.py` test
passed with explicit opt-in and the candidate's own installed Bridge 0.25.0
Python. It stopped only the synthetic test owner, atomically replaced Core B's
owner grant with a fresh 32-byte secret and key ID, and restarted that same
Bridge source while both signed Core Login Items remained registered.

- Core A continued to authenticate with its unchanged grant.
- Core B's pinned private identity and the old direct signer both received
  `CORE_NOT_AUTHORIZED`; the Core's pinned binding was not changed.
- Core B's local authenticated filesystem MCP stayed available, and both Core
  launchd PIDs remained unchanged.
- A newly constructed test-only `CoreBridgeClientV2` using B's new grant
  authenticated successfully. Access to Core A's historical task returned
  `CORE_TASK_NOT_FOUND`.
- The existing owner restart, revocation, original-key re-authorization and
  Core stop coverage remained in the test.

The bounded pytest process exited **0** (`1 passed`, 38.120 seconds). Private
JUnit evidence remains at `/tmp/serverfs-v0250-shared-key-rotation-2026-11-16.xml`
(UID-owned, mode 0600; 1 test, 0 failures, 0 errors, 0 skipped).

## Safety and source checks

Protected production launchd PIDs before and after were unchanged: Tunnel
1779, Bridge 1790, Cloudflare 66419 and HTTP Origin 40516. No test `shsm-*`
launchd job, process or scratch directory remained. The focused source suite
reported **21 passed, 5 skipped**; the skips include opt-in/platform-specific
coverage. `git diff --check` passed.

Ruff lint and format checks on the full test module still report pre-existing
import-order, line-length and formatting issues outside the new rotation
assertions. Ruff was not used to reformat unrelated test code. No Bridge/Core
product authorization logic changed.

## Remaining gates

This is one local signed macOS candidate acceptance, not release-tagged
v0.25.0 evidence. Cross-platform release gates, exact-SHA packaging/publication,
and broader production workdir/Agent acceptance remain separate.

## Append-only lint and regression follow-up — 2026-11-16

The requested Ruff cleanup was limited to
`tests/test_darwin_v0250_shared_smappservice_live_macos.py`: standard-library
import ordering, wrapping the Bridge subprocess argv, and formatter whitespace.
No test assertions, control flow, constants, cleanup, or safety preflight changed.
AST comparison against the pre-edit file passed: all 19 non-import top-level
nodes are identical and all 36 import bindings are preserved. Ruff reordered the
imports and split the owner-process imports into two `from` statements; the
binding sequence changed to Ruff order, while the binding multiset is identical.

Validation completed with exit code 0:

- Target Ruff check and format check; unfiltered repository Ruff check and
  format check (`719 files already formatted`).
- Candidate source gate: `CANDIDATE_INPUT=PASS`, 205 reviewed paths, 2,442
  excluded scratch entries, HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`.
- `git diff --check`.
- Focused Shared Core source/safety/regression selection: 122 tests, 0 failures,
  0 errors, 4 skipped.
- Root default suite: 2,928 tests, 0 failures, 0 errors, 420 skipped.
- Agent Bridge default suite: 672 tests, 0 failures, 0 errors, 134 skipped.

The live SMAppService acceptance remained opt-in and was not enabled or rerun.
No production service, Connector, credential, or native registration state was
mutated. The initial bare `ruff` invocation was unavailable on `PATH`; all Ruff
gates above used the repository `.venv/bin/ruff`. Two early AST-check helper
invocations failed in the comparison script itself (unhashable list keys, then
passing a list to `ast.dump`); after correcting the helper, the AST comparison
passed as recorded above. These helper errors were not test or product failures.
