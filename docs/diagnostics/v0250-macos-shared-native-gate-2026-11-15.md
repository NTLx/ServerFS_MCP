# v0.25.0 macOS Shared Core native gate — 2026-11-15

## Scope and disposition

Source integration and isolated build verification only. No production
SMAppService/LaunchAgent, Tunnel, credentials, active Core, or Bridge were
modified. No Git commit, push, tag, or deployment was performed. The existing
dirty working tree was preserved.

## Changes made in this continuation

1. `native/macos/service_manager/build.sh`: compile
   `Sources/ServiceLauncher/SharedManifestGate.swift` together with
   `main.swift`; the original omission would leave the native launcher
   without the newly referenced Shared Core validation symbols.
2. `native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift`:
   reject JSON floating-point encodings and booleans for the integral
   `schema_version` and `core.port` fields rather than silently converting
   their `NSNumber.intValue`.
3. `native/macos/service_manager/Sources/ServiceLauncher/main.swift`:
   allow `--validate-only <instance-id> http-origin` exclusively for the
   `v0.25-named-shared-core` owner. Reuse `loadManifest` / signed bundle
   checks and exit before environment loading, log opening, chdir or exec.
   Owned/file-only Core manifests continue to be rejected for this new mode.
   Native-only preflight is not proof of authenticated live Bridge readiness.
4. `tests/test_darwin_v0250_shared_swift_gate.py`:
   add regression coverage for build inputs and strict JSON integers.
5. `tests/test_darwin_v0250_native_validation.py`:
   exercise the source-level no-side-effects and Shared-only preflight contract.
   Minimal formatter-compatible cleanup was applied to this modified test file.
6. `docs/architecture/v0.25.0-macos-shared-core-manifest.md`:
   update stale source-only wording to reflect code integration without
   presenting it as an installed or publicly verified service.

## Executed evidence

- Initial four focused Shared Core suites: **57 passed**, 0 failed, 0 skipped.
- Follow-up native preflight / Swift gate / instance / interpreter tests:
  **62 passed**, 0 failed, 0 skipped.
- After the final test-only formatting cleanup: **62 passed** again,
  0 failed, 0 skipped, using
  `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider`
  for `test_darwin_v0250_native_validation.py`,
  `test_darwin_v0250_shared_swift_gate.py`,
  `test_darwin_v0250_instances.py`, and
  `test_darwin_v0250_shared_interpreter_pin.py`.
- Native app: `bash native/macos/service_manager/build.sh <isolated-temp-build-root>`
  succeeded with exit 0. Swift compilation, plist lint, codesign and
  `codesign --verify --deep --strict` succeeded (exit 0). Scratch app
  was removed; no persistent installed artifact was changed.
- `.venv/bin/ruff check` and `.venv/bin/ruff format --check` for the
  two modified Python test files: both exit 0.
- `git diff --check`: exit 0.

The transient `build.sh` direct-execution error (exit 126, non-executable
shell script) was corrected by invoking it through `bash`, as required by
the maintainer build procedure. A Claude Code verification attempt was blocked
by an independent WorkBuddy authentication 401 and produced no test evidence;
verification was then completed using Codex.

## Not accepted / next gates

- No real signed Shared Core app registration/start/stop via SMAppService.
- No native launcher `--validate-only` executable test against an installed
  isolated Shared Core manifest; existing Swift gate executable tests are
  separate from a full launcher process.
- No complete owner provision/revoke/rotate/grant lifecycle or true native
  two-Core-to-one-Bridge acceptance in this continuation.
- No production Cloudflare/OpenAI connector/public ChatGPT MCP verification.
- No macOS release claim, cross-platform release gate, Git publication,
  or production cutover.

Next: native isolated Shared Core launchd and authenticated v2 IPC tests using
an explicitly provisioned independent Bridge/grants and disposable app
identity, followed by two-Core isolation/restart/revocation coverage. Keep
Shared Core unable to own or stop the shared Bridge and keep default v1,
owned and file-only regressions green.
