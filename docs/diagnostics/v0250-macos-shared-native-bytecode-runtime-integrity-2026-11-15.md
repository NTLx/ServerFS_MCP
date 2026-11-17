# macOS v0.25.0 — Shared Core native runtime bytecode integrity regression

## Confirmed outcome

**The native two-Shared-Core SMAppService end-to-end gate remains NOT ACCEPTED.** A live opt-in run on Apple Silicon macOS 27.2.1 performed offline runtime provisioning, built the signed native companion, registered and started the first random Shared Core, then failed while staging the second with `managed Shared Core runtime integrity check failed`. That run returned **1 failed / exit 1**. Its test-owned labels, instances, processes and scratch were checked absent afterward; the four production PIDs stayed unchanged (OpenAI Tunnel 1779, Bridge 1790, HTTP Core 40516, Cloudflare Connector 40543).

## Root cause, independently reproduced

`src/serverfs_mcp/darwin_shared_runtime.py` creates `sitecustomize.py` in the managed runtime to set `sys.dont_write_bytecode = True` and records a SHA-256 inventory of *all* `site-packages` files. On the first normal interpreter launch, Python generates `__pycache__/sitecustomize.cpython-313.pyc` **before** executing the `sitecustomize.py` assignment. The added cache entry invalidates the recorded runtime inventory; a subsequent Core stage correctly rejects the altered runtime. This was confirmed in an isolated scratch reproduction: the ordinary launch adds the cache, while an otherwise equivalent interpreter launch with `-B` creates no new file.

## Minimal fail-closed fix

- `src/serverfs_mcp/darwin_instance_shared_staging.py`: Shared Core executable argv now begins `-B -m serverfs_mcp.cli …` to disable bytecode creation from interpreter startup.
- Python Shared Core manifest validator and native Swift `SharedManifestGate.swift` enforce the same **exact** argv (missing `-B` is rejected), rather than weakening runtime integrity checks.
- Narrow staging, manifest and native gate tests updated for the consistent strict contract; no production service registrations, credentials, Tunnel resources or default Core/Bridge processes were modified by the code fix.

## Verification and remaining blocker

- The fix task recorded **93 passed** across five focused test files; isolated bytecode test reproduced the diagnosis.
- Follow-up full-project `ruff check .` and `ruff format --check .` both returned **0**, with **686 files formatted**, after mechanical formatting of two already-touched files.
- The next native test attempt was **not executed**: while proceeding toward additional Swift/native validation the agent encountered an opaque command approval request showing only `<outside-workdir>`. Its scope could not be established, so the task was cancelled instead of approving an unknown external operation. No second native acceptance result is claimed.
- The prior cleanup and four production PID invariants were confirmed; the opt-in registered two-Core lifecycle must still be rerun successfully on macOS with inspectable authorization.

## Post-fix complete source test regression

A separate read-only run after the above source fix completed **both full test suites** without installing packages or touching native services:

- Root `.venv/bin/python -B -m pytest -p no:cacheprovider -q`: **exit 0, 100%**; 2 warnings (existing Starlette/AnyIO deprecation and duplicate ZIP member fixture). The quiet output did not supply a verified passed/skipped count, so none is inferred.
- Agent Bridge `agent_bridge/.venv/bin/python -B -m pytest -p no:cacheprovider -q agent_bridge/tests`: **exit 0, 100%**; no warning summary.
- `uv run --offline --frozen ruff check .`: **exit 0**.
- `.venv/bin/ruff format --check .`: **exit 0, 687 files already formatted** (the initial bare `ruff` attempt returned shell **127** because no global executable is on PATH; the installed local Ruff ran successfully).
- `git diff --check`: **exit 0**.

This full source regression does **not** replace the blocked post-fix real two-Core `SMAppService` test. No native registration or service modification occurred during this separate verification.

## Suggested next gate

Run the existing explicit opt-in test `tests/test_darwin_v0250_shared_smappservice_live_macos.py` once, *only in an execution environment that transparently permits the test-owned private runtime and signed App setup*, with two unique random test instances, live native register/start/stop, Bridge owner lifecycle and task segregation. Confirm the two test jobs and runtime scratch are fully removed and the protected production PIDs remain unchanged. Do not bypass macOS Login Items approval or attempt opaque system commands. This is a release gate, not currently a passed result.
