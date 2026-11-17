# macOS v0.25.0 Shared Core site-packages integrity (2026-11-15)

## Objective and implementation

Close the installed-package integrity gap in the offline, create-only Shared
Core runtime. New installs write provenance schema v3 with a deterministic
SHA-256 inventory over every regular file beneath the runtime's versioned
`site-packages`: canonical POSIX relative path, byte length and file contents
are fed to the digest in deterministic sorted-directory traversal order. No
installed file classes are excluded, including `.pyc` files and
`__pycache__` entries. Symlinks and nonregular entries are rejected. Traversal
is bounded at 100,000 files and
2 GiB; the inventory digest, file count and byte count are verified before
candidate publication and again after the atomic candidate-to-target rename.

The managed runtime installs a small `sitecustomize.py` that sets
`sys.dont_write_bytecode = True`, so normal managed Python startup does not
mutate the bytecode inventory after publication. Existing probe invocations
also use `-B`. The site-packages inventory does not use mtime as content
identity; file identity/stat checks only detect a concurrent change while a
file is being read.

Schema 1 and 2 remain readable by the general verify-only path with their
historical checks. They do not attest site-packages and are rejected by
managed Shared Core admission. Such a legacy runtime is never rewritten or
deleted automatically; an operator must create a schema-v3 runtime before it
can be admitted for managed Shared Core staging/preflight/start. Existing
default and owned modes do not use this Shared Core gate and were not changed.
SHA-256 provenance detects content drift against its local record; it does not
prove publisher authenticity or prevent an actor who can rewrite both the
runtime and its private provenance record from forging a new consistent pair.

## Verification

- Targeted runtime install, verification and exact-commit source tests:
  **56 passed, 1 skipped**. Coverage includes valid schema-v3 non-release and
  exact-HEAD release installs, historical schema 1/2 parsing, malformed mixed
  schema rejection, installed package byte mutation, file-to-symlink
  replacement, rogue code file, missing file, injected bytecode cache and a
  FIFO/nonregular entry. A schema 1/2 runtime remains readable but fails
  managed admission.
- Full root suite with live opt-ins unset:
  **2,347 passed, 417 skipped, 1 warning** in 122.71s. Skips include opt-in
  runtime/live/native registration acceptance. The warning is the existing
  Starlette TestClient `BlockingPortal` deprecation warning.
- `uv run --offline --frozen ruff check .`: **passed**.
- Ruff format check for all four changed Python files: **passed**.
- `git diff --check`: **passed**.
- Full-root `ruff format --check .`: **not clean** because the pre-existing
  `docs/diagnostics/v0250-macos-exact-commit-real-offline-install-2026-11-15.md`
  has one formatting issue at its recorded `create_shared_core_runtime` call.
  That out-of-scope diagnostic was left untouched; the four Python files in
  this change pass format check.

## Real exact-HEAD offline install

One disposable install used the current local `HEAD` archive and exact release
mode, through the existing offline uv project environment. Exact values:

- Git `HEAD`: `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`
- Version in that committed archive: `0.24.2`
- Install result: `installed`; post-publication verification: `verified`
- Provenance schema: `3`
- Site-packages inventory: 1,106 files, 27,539,098 bytes
- Site-packages SHA-256: `983e936770d38bbf6a8fc57b9fdff23665cc26e2a10bb38fa7784edbefda8d26`
- Appending bytes to the installed `serverfs_mcp/__init__.py` caused verify to
  reject with `installed site-packages integrity mismatch`.

The installer created its target under a fresh private temporary directory;
the `TemporaryDirectory` context removed that exact scratch root. No existing
managed runtime was upgraded or altered. No service registration, startup,
launchd mutation, Bridge, Tunnel, Cloudflare, credential, production path or
Git operation occurred.

## Limits

The real exact-HEAD archive was the local v0.24.2 `HEAD`, not a published
v0.25.0 release SHA. The real install validates the installer/inventory
mechanism and tamper response, not release acceptance or publisher identity.
Native service lifecycle acceptance and any actual legacy runtime migration
were not performed. Historical schema compatibility was tested synthetically;
managed admission intentionally refuses those unattested schemas without
mutating them.
