# v0.25.0 macOS — Named OpenAI Connector unstage identity repair

Date: 2026-11-16  
Scope: macOS `bridge_mode=disabled` instance cleanup and native launchd ownership verification.

## Result: PASS (source and regression)

Fixed a reproducible `KeyError: 'tunnel.blue'` in `unstage_file_only_instance()` when the private instance manifest still contained a named OpenAI Connector (`tunnel.<connector_id>`). The cleanup code previously translated only `cloudflare.<id>` before indexing the fixed `_ALLOWED_SERVICES` table. That table intentionally has no dynamic `tunnel.<id>` entry.

This was **not** a production incident and was not tested by modifying any live macOS service. The failure was reproduced with an isolated staged file-only Core and a named OpenAI Connector before changing the implementation.

## Red → green evidence

1. Added `test_file_only_unstage_checks_named_openai_launchd_pid_before_removing_state` to `tests/test_darwin_v0250_openai_connector_ids.py`. It stages a named Tunnel, simulates an exact named launchd process ID and requires `DarwinInstanceRuntimeError("named service process is still running")` while keeping the manifest and private instance state.
2. **Before the fix:** executed exactly this test: exit 1, `KeyError: 'tunnel.blue'` at `src/serverfs_mcp/darwin_instance_runtime.py:1027`.
3. **Fix:** derive the launchd label by three explicit cases: `cloudflare.<id> → macos_label("cloudflare", id)`, `tunnel.<id> → macos_label("openai", id)`, default known component via `_ALLOWED_SERVICES`. Retained the existing registration-state and PID refusal gate; no new infrastructure or cross-platform abstraction.
4. Added `test_file_only_unstage_removes_only_inactive_named_openai_state`: all named services reported inactive, cleanup removes only the owned staged instance while preserving the external Connector binary, bearer and control-plane key files.
5. **After the fix:** focused `test_darwin_v0250_openai_connector_ids.py`, `test_darwin_v0250_instance_runtime.py`, `test_darwin_v0250_connector_lifecycle.py`: **66 passed, 0 failed, 0 skipped, exit 0**. Targeted Ruff check and format check passed on two edited files; `git diff --check` passed. Full Root pytest exited **0** within 240 seconds. The quiet full run did not retain the passed/skipped counts and no count is invented.

No Agent Bridge source or lifecycle code changed, so Bridge full tests were not rerun in this change. No real native SMAppService registration was executed for these regression tests. No production service, credential, ingress, Agent process, Git commit, push, tag or release was modified.

## Concurrent ingress / provider diagnosis

Separately, read-only operations against the existing production service (metadata still identifies version **0.24.2**, not a v0.25.0 release) observed all four app-associated LaunchAgents running: OpenAI Tunnel PID 1779, Agent Bridge 1790, HTTP Core 40516 and Cloudflare Connector 66419. Core listened on `127.0.0.1:8765`, anonymous local and no-proxy public HTTPS checks reached the expected 401 authentication boundary, and ServerFS doctor reported 0 FAIL / 0 WARN. No authenticated live session was established in this particular diagnostic. A direct ChatGPT `list_workdirs` query through each ingress succeeded in the current turn, although an earlier attempt returned a transient connector tool error.

Cloudflare Connector historical log-pattern counts were high over November 15–16, but no active failure cause was established. No restart, Token/DNS/WAF edits, IPv4-only workaround or production crash injection is justified by this evidence alone.

The earlier Codex result `user rejected MCP tool call` occurred with both ingress providers. The adapter uses `approvalPolicy="on-request"` and `approvalsReviewer="user"`; the remote task completed without a confirmed ServerFS filesystem authorization failure. The precise provider MCP approval/consent event remains unverified. **Do not suppress or automatically approve it** merely to pass an E2E test.

## Remaining gates

- A clean, version-matched v0.25.0 release SHA, properly versioned Core/Bridge packages and platform-specific exact-SHA CI/native acceptance.
- Production Connector unclean-exit/KeepAlive behavior is unverified; the earlier test-only file Core KeepAlive recovery and controlled Connector restart are distinct successful gates.
- Diagnose provider-side nested external MCP tool consent only with supported user approval; investigate Cloudflare transient errors only if reproducible with timestamp-correlated evidence.

The macOS native-first ADR remains authoritative; this fix does not require Windows/Linux private IPC or service-manager code parity.
