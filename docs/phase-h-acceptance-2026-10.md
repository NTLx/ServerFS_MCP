# Phase H — CI, packaging and release closure: acceptance evidence

Phase H closes v0.11 to the state "safe to merge; merge leaves only the stable
tag/release outstanding". Baseline: `81bdc8e` (the Phase G merge). Branch:
`phase-h-release-closure`. PR: #40. Nothing in this phase merges, tags or
releases; the release-state wording everywhere describes v0.11 as
in-development.

## Layered evidence map

| Layer | What it proves | Where |
| --- | --- | --- |
| GitHub CI, final PR head `898cb7a` | Linux root/Bridge suites, container, Windows native kernel, **Windows Agent** (three-wheel build + split-env packaging gate + both doctor gates + Bridge suite + root agent tests), site build | PR #40 current-head checks: Container, Windows native, Windows Agent and Pages all green |
| WorkPC local deterministic at `62186d9` | root **1392 passed / 130 skipped**, Bridge **501 / 21**, D9 included, native Rust fmt + clippy + **48 tests**, ruff ×2 packages | H8 final matrix (see "Verification window" below); `898cb7a` adds only H9 plan/evidence documentation |
| Wheel-only acceptance | three 0.11.0 wheels, METADATA version gate, two isolated clean venvs, neither can import the other's package, `serverfs --version` = 0.11.0 | H3 + H8 final packaging gate |
| Real-provider package smoke | same implementation, as shipped, one real turn per runtime | H5 (`tests/e2e/run_phase_h_package_smoke.py`) |
| Live ChatGPT Tunnel E2E | ChatGPT → Secure MCP Tunnel → native ServerFS → Named Pipe → Bridge → provider | H6 (`docs/phase-h6-live-chatgpt-e2e-2026-10-09.md`), CLOSED-PASS |

## Version convergence (H1)

`serverfs-mcp` 0.10.0 → **0.11.0**; `serverfs-agent-bridge` 0.9.0 → **0.11.0**;
`serverfs-windows-native` 0.10.0 → **0.11.0** (Cargo remains the native
authority). `uv.lock` ×2 and `Cargo.lock` refreshed. `SERVER_VERSION` still
reads installed package metadata — no second version constant.

## Two real defects the phase measured and fixed

1. **The dependency-resolution failure was an acceptance-harness violation,
   not a product incompatibility** (maintainer ruling D). The first
   clean-install attempt installed all three wheels into one venv and
   correctly failed: root freezes `mcp==2.2.0`, the frozen
   `qoder-agent-sdk==1.0.15` declares `mcp<2.0.0` (measured: the SDK imports
   fully under 2.2.0, so the upstream bound is conservative; `1.0.15` is
   upstream's latest). Nothing was overridden; acceptance was corrected to
   the already-frozen `SERVERFS_BRIDGE_PYTHON` two-interpreter boundary, and
   the packaging gate now proves neither environment can import the other's
   package. (Recorded in dev_plan §Phase H.)
2. **Windows object ownership vs DACL trustee are two identity fields**
   (maintainer blocker on the first fix). The elevated GitHub runner's
   `TokenUser` is the built-in Administrator account while its `TokenOwner`
   is the Administrators group; the first correction conflated them and
   would have widened the frozen "Bridge user alone" descriptor to a whole
   group. The landed fix separates them end to end
   (`_assert_windows_private(expected_owner_sid, expected_trustee_sid)`;
   creation builds the DACL from `current_user_sid()`; ownership verifies
   against `current_token_owner_sid()`), with elevated-shape deterministic
   regressions and a mutation proof. Raw-SID/path diagnostics were withdrawn
   from product errors.

## Deployment-shaped fixes landed in the PR

- `deployment/windows/start-native.ps1` injects the Agent-era values
  (`SERVERFS_AGENT_PROXY_URL`, `SERVERFS_AGENT_NO_PROXY`,
  `SERVERFS_BRIDGE_PYTHON`) from the operator's own `.env` into the launcher
  environment — measured during H6: the tunnel's `.env` auto-discovery
  covered only the Control-Plane proxy values, so the Agent values never
  reached the supervisor or the Bridge.
- `tests/e2e/**` added to the Windows Agent workflow paths: the acceptance
  drivers are Agent acceptance infrastructure, and changing them must not
  silently skip the Windows gate (the hole Phase G measured).

## H6 production deployment (recorded)

The production WorkPC deployment was switched to the candidate wheels during
this phase, with a full rollback baseline preserved first
(`%LOCALAPPDATA%\ServerFS\rollback-0.10.0\`: complete connector-env snapshot,
config/.env backups, ROLLBACK.md). Post-switch: production doctor 0 FAIL /
0 WARN, Bridge running from `bridge-env`, tunnel metadata fetched,
`/healthz` 200. The live E2E then ran from the refreshed ChatGPT connection
(H6 doc above).

## Verification window (H8 local matrix)

The per-user lifecycle lease is a machine-level fact by design (Phase F:
known-folder anchored, not redirectable), so after the Agent-enabled
production switch the deterministic suites that launch a real supervisor
chain require a window with no other Agent-enabled supervisor. The H8 local
matrix ran inside such a window: the production tunnel was paused, root and
Bridge suites ran (all green, numbers above), and the production tunnel was
restarted immediately afterwards (doctor 0 FAIL / 0 WARN, healthz 200 on its
ephemeral loopback port). The window was measured at about four minutes and
the production chain was verified back before anything else proceeded.

## Release contract status (per dev_plan §Phase H)

| Item | State |
| --- | --- |
| Three packages = 0.11.0 | ✅ |
| Three wheels build | ✅ (`dist/h6-final`, built locally at H8/H7 head `62186d9`; current PR head CI rebuilt all three successfully) |
| Three wheels version-gated | ✅ (`wheel_release.py version-gate --tag v0.11.0`) |
| Three wheels clean-install | ✅ (two environments, provenance both ways) |
| Windows native CI green | ✅ |
| Windows Agent CI green | ✅ (all five jobs PASS on `62186d9`) |
| Linux CI green | ✅ |
| Doctor green | ✅ (agent-disabled and agent-enabled static) |
| WorkPC wheel-only provider smoke | ✅ (H5, three runtimes) |
| Live ChatGPT Tunnel E2E | ✅ (H6, CLOSED-PASS) |
| Docs/site truthful and aligned | ✅ (H7; v0.11 marked in-development everywhere) |
| Secrets clean | ✅ (pattern scan over the full PR diff) |
| Phase H PR green and mergeable | ✅ (#40, OPEN / non-draft / MERGEABLE, current head `898cb7a`) |
| **Stable tag `v0.11.0`** | ⬜ outstanding by design |
| **Stable release** | ⬜ outstanding by design |

## Candidate wheel digests (built from the final head)

```text
5c058978d116a72cc8d2df41ee0fe70740bcd2f2a97cf27f238c455837b19c52  serverfs_agent_bridge-0.11.0-py3-none-any.whl
20047f5e3500c098023155ca80c29d10b70bd55829710c5eeed2d01134b23a79  serverfs_mcp-0.11.0-py3-none-any.whl
26106f638f416b203b11ecfd1fb337361756ea56954e0f179692967be39c7ca6  serverfs_windows_native-0.11.0-cp312-abi3-win_amd64.whl
```

These are the locally recorded H8 acceptance artifacts built at `62186d9`.
The current PR head `898cb7a` changes only `dev_plan_v0.11.md` and this H9
evidence document relative to that head, and the current-head Windows Agent
workflow rebuilt, version-gated and clean-installed all three wheels
successfully. These local hashes are evidence, not promised release hashes;
the release workflow rebuilds from the stable tag and its produced artifacts
are authoritative.
