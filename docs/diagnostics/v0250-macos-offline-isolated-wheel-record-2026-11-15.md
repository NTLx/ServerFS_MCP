# macOS v0.25.0 offline isolated wheel build and static RECORD audit — 2026-11-15

## Scope and provenance

This was a focused standard PEP 517 offline wheel build and static archive audit after read-only diagnosis. No dependency synchronization or installation, network access, wheel extraction/import/execution, native service registration, Tunnel/network operation, Windows operation, Bridge production change, commit, push, tag, or release publication was performed.

The source worktree was already extensively dirty at HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`. A read-only snapshot covered 139 pre-existing changed files before the builds and again before this report was written; both had SHA-256 `64b256bf7af7fe9d072933229849f39865a916502cfc85647d91592afdf18349`. No concurrent source change was observed during the build/audit window. This is evidence of a stable snapshot during that window, not a claim that the worktree was clean.

Both project metadata files still declare version `0.24.2`. The resulting archives therefore contain dirty-worktree sources with `0.24.2` metadata; they are **not** built from a v0.25.0 release SHA and are not release artifacts.

## Build evidence

Toolchain: `uv 0.9.7 (0adb44480 2025-10-30)` and Python `3.13.9`. Both commands used uv's ordinary isolated PEP 517 builder in offline mode; no `--no-build-isolation` was used.

```text
uv build --offline --wheel --out-dir deployment/macos/.serverfs-v0250-offline-wheel-8b0d64ea9057-root
PASS: serverfs_mcp-0.24.2-py3-none-any.whl (404,299 bytes)

uv build --offline --wheel --out-dir ../deployment/macos/.serverfs-v0250-offline-wheel-8b0d64ea9057-bridge
  [run from agent_bridge/]
PASS: serverfs_agent_bridge-0.24.2-py3-none-any.whl (213,148 bytes)
```

## Static archive audit

Python standard-library `zipfile`, `csv`, `base64`, `hashlib`, and `stat` inspection passed for both archives. Each had unique safe relative member paths, no `..` traversal, duplicate members, symlink entries, encrypted members, `.git`/`.env`/secret or credential paths, and plausible member count/uncompressed size. The expected normalized distribution name and version were present. Every non-`RECORD` member had a matching SHA-256 digest and byte count in `RECORD`; the `RECORD` self-entry was empty.

- Root wheel: **113 members**, **1,439,364 uncompressed bytes**. Required Shared Core/macOS Python lifecycle and runtime modules, native ServiceManager and ServiceLauncher Swift sources, and `build.sh` were present per root wheel force-include configuration.
- Agent Bridge wheel: **54 members**, **767,102 uncompressed bytes**. Core grants, private grants, protocol, and store modules were present.
- `git diff --check`: PASS.

No wheel bytes were extracted, imported, or executed. Extracted-wheel import integrity remains **NOT VERIFIED**.

## Retained outputs and cleanup

The two task-owned output directories remain under `deployment/macos/` with directory mode `0700`. Their paths are:

- `deployment/macos/.serverfs-v0250-offline-wheel-8b0d64ea9057-root/`
- `deployment/macos/.serverfs-v0250-offline-wheel-8b0d64ea9057-bridge/`

Cleanup was not accepted or attempted. The prior `.serverfs-v0250-wheel-static-dUpY22im` scratch remains untouched; its cleanup status remains **NOT ACCEPTED / NOT VERIFIED**. This task makes no claim about its removal.
