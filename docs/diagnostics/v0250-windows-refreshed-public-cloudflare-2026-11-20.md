# Windows v0.25.0 — refreshed dual Cloudflare public acceptance (2026-11-20)

**Verdict: COMPLETE/PASS for two independent real Cloudflare Connectors and an isolated file-only Core using exact refreshed 0.25.0 Windows Core/Native Wheels. Not a production rollout or OpenAI Tunnel/real Provider acceptance.**

## Artifacts and isolation

- Core SHA256: 6308f00cc9db55a8695caa4072ffac61a6fc81aacc2efb806d2a9333363f44a8.
- Windows Native SHA256: 5b796e1d875def29657fcca42df5adf79abf8157a8fc821d9b135573baeecac6.
- Cloudflared executable SHA256: 2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343 (previously pinned installed I7 binary, independently matched on this host).
- New instance i7-refresh-71012840d63b, loopback port 18940, local Host i7-public.local.
- File-only private Fixture with synthetic guard content, explicit read-only policy, Agent delegation and Repo Ops disabled. No Bridge or real Provider required for this topology.
- Existing historical I7 Core and two named Connectors verified disabled/offline, actual private token identities matched their two dedicated test Tunnel UUIDs, binary pins checked locally. Existing token bytes never output. Newly copied test token files retired after exact task cleanup; historical inputs unchanged.

See [refreshed native lifecycle](v0250-windows-refreshed-native-lifecycle-2026-11-20.md) and [refreshed release regression](v0250-windows-acp-job-and-refreshed-release-2026-11-20.md) for separate Bridge and full-suite acceptance.

## Cloudflare control plane

Two existing remotely managed nonproduction tunnels reused, with no new Cloudflare resources or DNS mutations:

| Public hostname | Tunnel UUID | Before | Temporary test | Restored |
| --- | --- | --- | --- | --- |
| workpc-v25a.ntlx.top | aad24669-98ae-4eea-a54c-d89b872981a5 | 18875 / v7 | 18940 / v8 | 18875 / v9 |
| workpc-v25b.ntlx.top | 85ea7057-ea6c-4d2e-b71a-63074fa31441 | 18875 / v7 | 18940 / v8 | 18875 / v9 |

Each route remains a proxied CNAME to its exact UUID.cfargotunnel.com. Ingress is HTTP loopback with httpHostHeader i7-public.local and terminal HTTP 404; private WARP routing disabled. Version numbers necessarily advance on restore; configuration content returned to its original state. Production workpc-serverfs.ntlx.top stayed healthy, version 2, HTTP loopback origin 18767, unchanged DNS and ingress configuration.

Both test tunnels were down with zero connections before the gate, healthy with one connector connection each during the gate, and down with zero connections after scoped Windows cleanup and route restoration (transient control-plane healthy/degraded observations after local teardown were not treated as final offline proof).

## Windows execution evidence

Read-only preflight COMPLETE/PASS; fresh stage COMPLETE/PASS with install-core, install-cloudflare, configure-core exit 0 and no starts or credential copies. Live gate completed with actual exit 0 and eight PASS markers:

| Marker | Verified result |
| --- | --- |
| FRESH_PINNED_PRIVATE_CONNECTORS | Two fresh private per-Connector configurations |
| LOCAL_AUTH_AND_FILE_ONLY_SURFACE | Loopback missing/wrong Bearer rejected 401, foreign Host/Origin rejected, authenticated MCP and exactly 11 file-only tools, synthetic guard read |
| TWO_INDEPENDENT_CONNECTOR_OWNERS | Independent real Scheduler owners and process identities; same Core |
| BOTH_PUBLIC_NEGATIVE_AUTH | Both public missing/wrong Bearer rejected 401 and foreign Origin refused |
| BOTH_PUBLIC_CONCURRENT_MCP | Concurrent authenticated MCP sessions, tool inventory and exact synthetic fixture read |
| B_AND_CORE_SURVIVE_A_STOP | A stopped; B and Core generations unchanged; B public read succeeds |
| A_RESTART_AND_B_UNCHANGED | New A generation, B/Core unchanged, both public MCP sessions work |
| CORE_SURVIVES_BOTH_CONNECTORS_STOP | Both independent Connectors stopped; same Core remains usable directly |

Final audit state COMPLETE, verdict PASS, cleanup_verified=true. All three new scheduled tasks removed, new Connector manifests retired and only their exact copied tokens and temporary imports removed; test listener absent. Historical I7 tasks offline. All 40 protected Scheduler entries and production Core generation unchanged. Both Cloudflare control-plane test route restorations verified separately.

Public foreign Host handling is not claimed: the configured ingress rewrites the origin Host. The loopback Host denial and public foreign Origin denial were separately verified. No writable public workdir, Agent/Repo Ops public calls, sustained streaming stress, OpenAI Tunnel live test, or production cutover was exercised.

## Evidence paths

Under candidate .remember/tmp/v0250-win-versioned-python-20261120/:
- refreshed-public-cf-preflight-20261120/audit.json
- refreshed-public-cf-stage-20261120/audit.json
- refreshed-public-cf-live-20261120/audit.json
- scripts/refreshed_public_cf_preflight_20261120.py
- scripts/refreshed_public_cf_stage_20261120.py
- scripts/refreshed_public_cf_live_20261120.py

Cloudflare API preflight, temporary route change, live status, exact rollback and final offline observations were independently retrieved by the operator. No production lifecycle, credentials exported, Git staging/commit/push/tag or release publication. This documentation is a delta to the prior frozen source selection only; runtime Wheel bytes were not changed.

## Remaining release gates

Actual public OpenAI Tunnel with exact refreshed artifacts and separately verified nonproduction credentials, real I11-B Provider fidelity, cross-platform integration/CI, coordinated version metadata and release notes remain pending. The previous native and ACP regression results are independent evidence; do not claim every topology was exercised via public ingress.
