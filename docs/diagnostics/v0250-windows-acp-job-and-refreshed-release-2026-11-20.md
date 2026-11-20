# Windows v0.25.0 — ACP Job containment and refreshed release verification (2026-11-20)

**Verdict: PASS for refreshed Windows installed-Wheel regression, ACP synthetic stress and reviewed source/build gates. Not release or production authorization.**

## ACP lifecycle

The earlier installed Bridge full suite had one `AGENT_PROVIDER_STOP_UNKNOWN` in `test_reconciliation_requires_measured_load_capability`. The original failure was not reproduced in an additional 24 instrumented synthetic repetitions of the old Wheel. Its exact cause is still unestablished.

Replaced Windows ACP PID/parent-snapshot termination with the existing Bridge-native anonymous kill-on-close Job implementation: create suspended, assign exact retained process handle, then resume; terminate owned Job and prove zero active members plus leader exit. No PID-based fallback. An unproved stop remains false and is not retried as success. The shared ACP adapter now also cleans a child if connection setup fails before active registration. POSIX launch/stop implementation is unchanged; POSIX regression was not executed in this round.

Changed runtime paths:
- agent_bridge/src/serverfs_agent_bridge/adapters/acp.py
- agent_bridge/src/serverfs_agent_bridge/adapters/acp_process.py
- agent_bridge/src/serverfs_agent_bridge/adapters/acp_process_windows.py
- agent_bridge/src/serverfs_agent_bridge/repo_ops_windows.py

Changed tests: agent_bridge/tests/test_acp_adapter.py and agent_bridge/tests/test_acp_windows_process.py.

Initial focused gate: 47 passed, 5 skipped, 1 failed; the immediate post-stop PID query in the new orphan test was not sufficient to distinguish delayed handle signalling from a live descendant. Revised tests prove actual membership with IsProcessInJob and retain exact process handles prior to termination, then require bounded signalling; no product stop-proof weakening. The second focused gate passed 50, skipped 5, failed 0, with Ruff check/format exit 0. Added coverage for startup assignment refusal before resume, orphan descendants, unrelated sibling protection, unproved stop and cancelled waiter, and setup-failure cleanup.

## Refreshed snapshot and artifacts

Candidate HEAD 01d3e81dc56637df461857cf8fa2da06ef66790f (local; remote not rechecked in this gate). 1,163 snapshot files, 392 reviewed changed paths, 101 untracked paths excluded but retained. Exact 46-path delta from previous snapshot: 38 style repairs, 6 ACP paths, Windows Agent workflow and previous handoff document. Eight shared version metadata files synchronized to 0.25.0 in snapshot only; candidate metadata left unchanged. Both version gates, both offline lock checks, actual Ruff file inventory of 800 paths, check/format passed.

| Artifact | SHA256 |
| --- | --- |
| Core Wheel | 6308f00cc9db55a8695caa4072ffac61a6fc81aacc2efb806d2a9333363f44a8 |
| Bridge Wheel | d262552bde08c926e510fd5187fa83b8c1cf05f3bb7650bf9c7c8072a5642aa1 |
| Native ABI3 Windows x64 Wheel | 5b796e1d875def29657fcca42df5adf79abf8157a8fc821d9b135573baeecac6 |
| Agent Plugin ZIP | 717ab4faa9b89d7a9ef5842d4cd3a31b7d823705f92e57fc494b1ab5ee549178 |

Core and Plugin rebuilt identically; Native copied from previous validated artifact with unchanged native sources/version pins (not rebuilt). Bridge archive differs only in the four runtime modules above plus RECORD. RECORD membership, hashes and sizes checked for all three Wheels, and installed payloads verified before/after. Official Wheel version gate passed.

## Installed acceptance

| Scope | Passed | Skipped | Failed | Exit |
| --- | ---: | ---: | ---: | ---: |
| Windows Core/native (200 test files) | 1864 | 15 | 0 | 0 |
| Bridge complete default suite | 772 | 119 | 0 | 0 |
| Combined full suites | 2636 | 134 | 0 | 0 |
| ACP stress (8 repetitions of 3 selected tests) | 24 | 0 | 0 | 0 |

The 24 stress executions are repeated tests and are not additional distinct full-suite cases. Both isolated import-origin checks passed. Source and installed payload postflight unchanged. All 26 command exit markers zero, aggregate RELEASE_REFRESH_ACP=PASS, actual wrapper exit 0.

Evidence under .remember/tmp/v0250-win-versioned-python-20261120/:
- acp-stop-reproduction-20261120/audit.json
- acp-job-source-gate-20261120/audit.json (retain earlier failure)
- acp-job-source-gate-v2-20261120/audit.json
- release-snapshot-refresh-acp-20261120/audit.json
- release-snapshot-refresh-acp-20261120/source-manifest.json
- release-snapshot-refresh-acp-20261120/release-selection.json
- release-snapshot-refresh-acp-20261120/core.xml, bridge.xml and acp_stress_*.xml

## Remaining gates

The previous single full-suite failure no longer blocks the refreshed installed default Windows gate; root cause of that historical transient was not established. Latest exact Bridge Wheel still needs final native opt-in and controlled lifecycle/ingress acceptance where applicable. Existing I17 and four-component Scheduler proof refer to earlier exact artifact hashes. Public Cloudflare/OpenAI ingress, real I11-B exact provider output, coordinated source integration, shared release metadata, release notes and cross-platform CI remain distinct release gates. This handoff document is added after the frozen source and must be included or separately reviewed in the next selection refresh.

No original dirty checkout changes, production task/Tunnel changes, real provider calls, credentials, Git staging/commit/push/tag or release publication.
