# macOS v0.25.0 wheel offline preflight — 2026-11-15

## Result

The isolated wheel install/import acceptance remains **NOT VERIFIED**. The two
available app wheels are valid `0.24.2`-labelled archives from the dirty local
worktree, not v0.25.0 release artifacts. Their static archive gate passed, but
this task did not install, extract, or import them.

The failed resolver condition is distinguishable from installed dependency
availability:

- Root `.venv` has all four wheel requirements at satisfying versions:
  `mcp 2.3.0`, `pydantic 2.13.5`, `starlette 1.6.0`, and `uvicorn 0.53.0`.
- `agent_bridge/.venv` has all six Bridge wheel requirements:
  `agent-client-protocol 0.12.1`, `claude-agent-sdk 0.2.156`, `httpx2 2.13.0`,
  `qoder-agent-sdk 1.0.15`, `typesafe-sdk 0.7.1`, and `websockets 17.1`.
- Neither environment's installed distribution metadata is itself a wheel
  archive available to a new offline installer. A read-only scan found 58 wheel
  archives under the uv cache and no matching archives for those ten direct
  requirements. In particular, the cache lacks `mcp==2.3.0`.

The root and Bridge lockfiles resolve the matching versions above (Bridge's
environment separately contains `mcp 1.30.0`). The existing environments are
not modified or used as dependency sources for a new runtime. The earlier
workspace scratch at
`deployment/macos/.serverfs-v0250-wheel-import-uNINb4/` was retained untouched.
No new scratch was created.

## Reproducible read-only preflight

`deployment/macos/v0250_wheel_offline_preflight.py` reads wheel
`Requires-Dist`, installed environment metadata, and wheel archives in a
specified cache. It reports each direct requirement as installed-only,
cache-only, both, or missing from both. This separates “installed in an
existing environment” from “available as an offline install input”; the report
explicitly does not treat cache metadata as an installation or integrity proof.

The preflight was run against the two existing `0.24.2` wheels, the root and
Bridge `site-packages`, and `/Users/lx/.cache/uv`. All ten requirements were
reported `installed-only`. Installation was not attempted because a new
isolated runtime cannot be populated from the available wheel cache without
borrowing/copying existing environment contents, and that provenance and
isolated runtime path were not established here. No `--no-deps` bypass was
used.

## Validation

- Existing `v0250_wheel_archive_gate.py`, expected version `0.24.2`: **PASS**;
  root 113 members and Bridge 54 members, including RECORD integrity.
- `tests/test_darwin_v0250_wheel_offline_preflight.py`: **2 passed**.
- Focused Ruff lint and format check for the new script/test: **PASS**.
- `git diff --check`: **PASS**.
- Isolated offline install and `-I` import/module-origin acceptance: **NOT
  VERIFIED**.

No network access, production environment/service changes, or cleanup occurred.
