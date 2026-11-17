# macOS v0.25.0 post-fresh-wheel/public-SSE full regression — 2026-11-16

## Scope

Verification-only final current-tree regression after the fresh Python 3.12
hash-locked installed-wheel three-process test, real signed two-Shared-Core
reauthorization test, and public authenticated Cloudflare initialize/SSE
acceptance. Those prior acceptance results are supplied by the operator; this
record covers the requested full Python suites, source quality gates and
read-only production-process checks. No source, tests, dependencies, service
configuration, credentials, WAF, DNS or Tunnel state were changed. No
`SERVERFS_ENABLE_*` variable was set. No service was started or stopped, and
no commit, push or version change was made.

## Full Python suites

Each suite was run from the repository root, sequentially, in a subprocess with
a 240-second timeout. The subprocess completed both before the timeout. Exact
return codes were captured:

| Suite | Command | Result |
| --- | --- | --- |
| Root | `.venv/bin/python -B -m pytest -p no:cacheprovider -q` | **PASS**, return code 0; completed within 240 seconds |
| Bridge | `agent_bridge/.venv/bin/python -B -m pytest -p no:cacheprovider -q agent_bridge/tests` | **PASS**, return code 0; completed within 240 seconds |

The project pytest configuration adds `-q` in both `pyproject.toml` files, so
the requested command applies quiet mode twice. The captured successful output
did not include a passed/skipped count; the verification wrapper retained only
summary and warning lines and did not persist the complete test output. Thus
passed/skipped counts are **not available from this run's retained evidence**.
The root output reported these two warning types:

- Starlette `TestClient` deprecation: `anyio.abc.BlockingPortal` alias.
- `zipfile` `UserWarning`: duplicate member `serverfs_mcp/darwin_lifecycle.py`.

No warning was reported for the Bridge suite. No timeout or failing test was
reported by either process.

## Static checks

To ensure the existing environments were used without installing or syncing
dependencies, the requested Ruff checks ran with `--no-sync` added to `uv run`:

| Boundary | Command | Result |
| --- | --- | --- |
| Root lint | `uv run --offline --frozen --no-sync ruff check .` | PASS — `All checks passed!` |
| Root format | `uv run --offline --frozen --no-sync ruff format --check .` | PASS — `698 files already formatted` |
| Bridge lint | `(cd agent_bridge && uv run --offline --frozen --no-sync ruff check .)` | PASS — `All checks passed!` |
| Bridge format | `(cd agent_bridge && uv run --offline --frozen --no-sync ruff format --check .)` | PASS — `120 files already formatted` |
| Whitespace | `git diff --check` | PASS — exit code 0 |

## Production process and test-residue check

Read-only process and launchd inspection confirmed the four expected production
owners and PIDs:

| Owner | launchd label | PID |
| --- | --- | ---: |
| OpenAI Tunnel | `com.ntlx.serverfs.tunnel` | 1779 |
| Agent Bridge | `com.ntlx.serverfs.bridge` | 1790 |
| Cloudflare HTTP Core | `com.ntlx.serverfs.http-origin` | 40516 |
| Cloudflare Connector | `com.ntlx.serverfs.cloudflare` | 40543 |

No `shsm-` launchd row was present. `lsof -nP -iTCP:18765 -sTCP:LISTEN`
reported no listener (it emitted an unrelated warning that a Time Machine
SMB mount could not be stat'ed, so its general output may be incomplete; the
requested port had no reported listener).

## Result and limitation

Both requested full suites returned zero within their individual 240-second
bounds. All requested static checks passed, and the protected PID baselines
matched. The test-count breakdown (passed/skipped) is not retained because the
specified double-quiet pytest invocation did not emit a count in the captured
summary. No test was repeated to reconstruct that missing breakdown.
