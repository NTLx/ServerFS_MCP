# Phase J — Native macOS Agent Acceptance (real bridge / AF_UNIX / launchd)

**Date:** 2026-10-10
**Machine:** MacBook Air (Mac16,12), Apple M4
**OS:** macOS 27.0.1 (26A434), native arm64, not Rosetta
**ServerFS commit:** `df94673e16927e4a27d22428ce693cac5f3ec665`
**Runtimes (native arm64 binaries):** codex-cli 0.162.0 (managed daemon 0.162.1),
Claude Code 2.1.294; Qoder not installed on this host (SDK-driven, see limitations)

## Results — 9/9 PASS (live bridge on AF_UNIX with getpeereid authorization)

| # | Check | Result |
| --- | --- | --- |
| 1 | fake-runtime task lifecycle over the real bridge (submit → events → succeeded) | PASS |
| 2 | task events retrievable through `task.events` | PASS |
| 3 | writer-lease artifacts created for workspace-write (flock, shared MCP/Bridge identity) | PASS |
| 4 | result spool: 204,000-byte result spooled at a temporarily lowered 1 KiB threshold (`storage: "spool"`) | PASS |
| 5 | spooled result byte-exact via chunked `task.result.read` (204,000 bytes reconstructed) | PASS |
| 6 | spool SHA-256 exactly matches the payload | PASS |
| 7 | **live Codex probe through the real managed daemon** — `available=true, version=0.162.1` | PASS |
| 8 | **live Claude probe through the real CLI** — `available=true, version=2.1.294` | PASS |
| 9 | **live Codex model discovery** — `runtime.models` → 11 models via the daemon's `model/list` | PASS |

## launchd lifecycle (real per-user LaunchAgent)

`tests/test_darwin_lifecycle.py::TestLiveLaunchd` (run with `SERVERFS_LIVE_LAUNCHD=1`,
PASSED on this machine):

- `launchctl bootstrap gui/501` of the generated plist running the real
  `serverfs-agent-bridge` console script with a fake-runtime config;
- the Bridge's AF_UNIX endpoint appears inside the sun_path budget and serves
  `runtime.list` through the real client with getpeereid peer authorization;
- `launchctl kickstart -k` restarts the job (new PID, endpoint serves again);
- `launchctl bootout` removes the job — the process is gone and the socket file unlinked;
- `launchctl print` reports the job state (measured vocabulary: `running|active`).

## Containment statement (per dev_plan_v0.13.md §12 E4)

launchd is a service manager, not a containment kernel. No Job-Object equivalence is claimed:
a bootout provably stops the Bridge process itself, but descendant provider processes cannot be
proven stopped from launchd state alone. The v0.12 recovery guard and the workspace-write
fail-closed behavior are therefore retained unchanged on macOS; recovery stays provider-aware.

## Proxy / Jev

- No Darwin-specific Jev or proxy code exists (grep-verified); v0.12 is reused unchanged.
- The preflight/proxy/fail-open/advisory-authority suites all pass on darwin
  (test_preflight, test_runtime_proxy_overlay, test_runtime_proxy_ambient_url,
  test_runtime_env_consumption, test_config — 96 tests).
- Live proxied egress evidence (Tunnel/Agent/Jev through a real HTTP proxy) requires an
  operator-provided proxy endpoint and is recorded as the remaining operator-assisted item
  together with the ChatGPT E2E (see the live ChatGPT E2E record).

## Limitations on this host

1. **Qoder**: no `qodercli` binary is installed; the Qoder adapter is driven by the official
   Python Agent SDK and its coverage is the unit suite (`test_qoder_adapter.py`). Live Qoder
   acceptance requires a host with the Qoder IDE CLI installed and logged in; the release
   statement must remain narrowed accordingly until that run happens.
2. **Live provider task turns**: Codex and Claude are proven live at the probe/daemon/model-list
   level. A full paid provider turn (approvals, questions, cancellation against the real
   provider) consumes the operator's provider quota and is listed under operator-assisted
   evidence; the corresponding flows are covered by the adapter contract tests with harnesses.
3. **Approvals / questions / cancellation live flows**: exercised against harnesses in the
   automated suite (fake runtime + adapter tests) on this machine; the frozen semantics are
   unchanged from v0.12.
