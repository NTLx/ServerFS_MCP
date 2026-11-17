# v0.25.0 macOS Shared Core lifecycle and cross-mode regression — 2026-11-15

## Scope

Continued macOS-native development with no live production service cutover.
The v2 Bridge owner retains independent lifecycle and grant administration.
Existing default, file-only and owned deployments are not replaced. No git
commit, push, tag or release was performed; pre-existing uncommitted changes
were preserved.

## Source changes

1. \`native/macos/service_manager/Sources/ServiceLauncher/main.swift\`:
   extend signed \`--validate-only <instance-id> http-origin\` to exact named
   file-only / owned / shared owner shapes. Reject default/unsupported shapes
   without environment loading, logging or \`execv\`.
2. \`native/macos/service_manager/Sources/ServiceManager/main.swift\`:
   perform the signed launcher's named Core preflight before any named
   \`http-origin\` SMAppService registration. Add explicit
   \`register-preflight http-origin\` diagnostic. Do not change the existing
   independent owned-Bridge admission gate.
3. \`src/serverfs_mcp/darwin_instance_shared_lifecycle.py\` (new):
   \`shared_core_status\`, \`start_shared_core\`, \`stop_shared_core\`.
   Startup verifies the strict manifest and authenticates the live v2 Core
   grant before it checks per-instance registration and port ownership.
   It registers only the named Core and performs Core-only rollback on
   readiness failure. Stop remains possible after grant/manifest damage and
   never stops, unregisters or modifies the owner Bridge.
4. \`src/serverfs_mcp/darwin_instance_cli.py\`: explicitly named
   \`shared-status\`, \`shared-start\`, and \`shared-stop\` commands requiring
   \`--bridge-id\`, isolated from file-only/owned commands.
5. \`tests/test_darwin_v0250_native_validation.py\`: source guards for
   mandatory named Core registration preflight.
6. \`tests/test_darwin_v0250_shared_lifecycle.py\` (new): synthetic
   safe-registration, identity, alias, rollback, rejected registration,
   stop-after-bad-binding, status and CLI routing acceptance.
7. \`tests/test_core_bridge_v2_restart_rotation_macos.py\` (new):
   actual Darwin AF_UNIX owner-controlled v2 Bridge restart, grant rotation,
   revocation, per-Core task access, and nonce replay persistence.
8. \`docs/architecture/v0.25.0-macos-shared-core-manifest.md\`: revised
   integration state and explicit shared-only Core lifecycle commands.

The new lifecycle source/test files received only deterministic Ruff
formatting and import-order fixes.

## Executed evidence

### Narrow implementation gates

- Initial combined Mac Shared/Bridge v2 test set: **46 passed**.
- New Shared Core lifecycle/rollback tests plus Shared IPC/native gates:
  **59 passed**, then **60 passed** after formatting adjustments.
- Ruff check and Ruff format --check for the six edited Python code/test
  paths: both passed (exit 0).
- git diff --check passed (exit 0).
- Native ServiceManager build in disposable /tmp build root passed (exit 0)
  after Swift registration-preflight changes; plutil lint and
  \`codesign --verify --deep --strict\` passed. The isolated signed app was
  cleaned up and was never installed.

### Broader regression

- **38 synthetic/isolated test files, 483 passed, 0 failed, 0 skipped**.
  Includes macOS v0.25 named instances, file-only, owned, Connectors,
  native validation, Shared Client/IPC, grant rotation/revocation and
  legacy Darwin service compatibility. PYTHONDONTWRITEBYTECODE=1 and
  pytest -p no:cacheprovider were used with isolated /tmp basetemp.
- Separate real **two independent file-only HTTP Core subprocess E2E:
  1 passed**, using disposable synthetic workdirs, local bearer keys and
  two loopback ports; the test terminated both children and its scratch
  directory was removed. The test did not print the chosen port numbers,
  so independent post-run port-by-port identification was not established.
- These test batches overlap other focused batches; **do not sum
  overlapping results**. The two-Core subprocess test is distinct from
  the 483-test matrix.

Test evidence is from this Mac working tree and does not certify the
Windows/Linux implementations or an immutable Git SHA.

## Still unverified / release blockers

- The Shared Core signed bundle and full private owner-provisioned
  manifest/binding installation do not yet have a complete single-command
  staging flow; lifecycle assumes they already exist.
- No actual named Shared Core SMAppService registration/start/stop using
  two signed apps and one independent owner Bridge. The source-level
  lifecycle tests do not substitute for native lifecycle E2E.
- No end-to-end public ChatGPT Cloudflare/OpenAI access to two Shared
  Core endpoints or recovery following owner Bridge process restart.
- No release-wide exact-SHA cross-platform CI, full root/Bridge test
  suites, Git publication, or production cutover.
- The owner's key rotation/revocation requires an explicit, owner-controlled
  restart; Shared Core start/stop is not authorized to perform it.

## Recommended next implementation batch

Design and test atomic staging of a non-default Shared Core app plus
manifest, private environment and owner-supplied Core grant binding without
creating grants or claiming Bridge ownership. Keep generated secrets out of
the repository. Then run native signed-bundle validate-only acceptance,
real isolated two-Core-to-one-owner-Bridge SMAppService registration,
revocation/restart recovery and Cloudflare/OpenAI public ingress tests.
Only after those gates pass should cross-platform final release acceptance
be considered.
