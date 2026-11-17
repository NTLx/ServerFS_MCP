# macOS v0.25.0 current-worktree source gates — 2026-11-15

## Scope and disposition

Final source-quality verification after the Shared Core STOP last type guards
and documentation changes. This is evidence for the current dirty worktree,
not a release or publication claim. No source, configuration, version, test,
credential, service, or previous report was changed by this verification. No
Git commit, push, or tag was created.

The current `HEAD` remains `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`
(`feat(macos): checkpoint v0.25 independent Core and Cloudflare ingress`),
whose project metadata is version **0.24.2**. The worktree was already dirty
before this verification. v0.25.0 is not a published release.

## Environment and commands

All seven `SERVERFS_ENABLE_*` names found in the current repository were
explicitly unset for every root and Agent Bridge `uv` command:

- `SERVERFS_ENABLE_EPHEMERAL_SIGNED_CORE_TEST`
- `SERVERFS_ENABLE_SHARED_HTTP_E2E`
- `SERVERFS_ENABLE_SHARED_LOCKED_HTTP_E2E`
- `SERVERFS_ENABLE_SHARED_LOCKED_OWNER_E2E`
- `SERVERFS_ENABLE_SHARED_RUNTIME_INSTALL_TEST`
- `SERVERFS_ENABLE_SHARED_SIGNED_LIVE_PREFLIGHT_TEST`
- `SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST`

The commands used `--offline --frozen`; no network access or environment sync
was performed. Pytest disabled its cache provider and configured an empty
`addopts` value so the terminal summary is explicit.

From the repository root:

```text
uv run --offline --frozen ruff check .                         PASS
uv run --offline --frozen ruff format --check .                PASS (661 files already formatted)
uv run --offline --frozen pytest -p no:cacheprovider -o addopts='' -q
  PASS: 2372 passed, 417 skipped, 0 failed, 1 warning (120.85s)
```

The sole warning is Starlette's `anyio.abc.BlockingPortal` deprecation
warning from the installed test client.

From `agent_bridge/` using its existing environment:

```text
uv run --offline --frozen ruff check .                         PASS
uv run --offline --frozen ruff format --check .                PASS (120 files already formatted)
uv run --offline --frozen pytest -p no:cacheprovider -o addopts='' -q
  PASS: 538 passed, 134 skipped, 0 failed (50.35s)
```

Installed `xcrun swiftc` typechecks also passed without building or invoking
ServiceManagement:

```text
xcrun swiftc -typecheck -target arm64-apple-macosx27.0 \
  native/macos/service_manager/Sources/ServiceManager/main.swift
xcrun swiftc -typecheck -target arm64-apple-macosx27.0 \
  native/macos/service_manager/Sources/ServiceLauncher/main.swift \
  native/macos/service_manager/Sources/ServiceLauncher/SharedManifestGate.swift
```

`git diff --check` passed before and after this report was written. After
writing, the requested report path exists.

## Provenance and remaining limits

The test counts above come only from the two full-suite runs recorded in this
verification. The skipped tests include opt-in native/live and HTTP E2E
coverage; no native registration, stop/restart, Tunnel, Cloudflare, Windows,
or Bridge production change was made or verified here.

Two separately recorded locked Shared offline HTTP tests exited 0. Their
scratch cleanup is **NOT VERIFIED**. Three existing `sfsh3*` directories were
created hours before those tests, so their existence is not attributed to
either test and was not inspected or cleaned during this verification. See
[Shared three-process acceptance](v0250-macos-shared-three-process-acceptance-2026-11-15.md)
for prior HTTP/process provenance.

Wheel packaging remains **PARTIAL / NOT FULLY ACCEPTED**: prior records cover
wheel creation and preliminary archive metadata, but not isolated extracted-
wheel imports. See [release readiness](v0250-macos-release-readiness-2026-11-15.md).
The exact-commit runtime record also identifies the archived project version
as 0.24.2, not a v0.25.0 release SHA; see
[exact-commit offline runtime](v0250-macos-exact-commit-real-offline-install-2026-11-15.md).

These source gates pass for the current dirty checkout. They do not establish
wheel acceptance, live native lifecycle acceptance, or a published v0.25.0
release.
