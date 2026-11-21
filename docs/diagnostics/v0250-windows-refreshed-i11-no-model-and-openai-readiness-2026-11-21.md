# Windows v0.25.0 — refreshed I11 native isolation and OpenAI readiness (2026-11-21)

**Verdict: COMPLETE/PASS for two fresh owned Claude-capable native pairs without model execution. OpenAI public ingress and strict real-Provider acceptance remain OPEN.**

## Exact artifact inputs

Same refreshed frozen 0.25.0 artifacts as the [native lifecycle gate](v0250-windows-refreshed-native-lifecycle-2026-11-20.md) and [Cloudflare public gate](v0250-windows-refreshed-public-cloudflare-2026-11-20.md):

- Core: 6308f00cc9db55a8695caa4072ffac61a6fc81aacc2efb806d2a9333363f44a8
- Bridge: d262552bde08c926e510fd5187fa83b8c1cf05f3bb7650bf9c7c8072a5642aa1
- Windows Native: 5b796e1d875def29657fcca42df5adf79abf8157a8fc821d9b135573baeecac6

Two fresh nonproduction owned pairs, i11r-a-e294c734c755 (loopback 18960) and i11r-b-ac4d80e9d163 (18961), installed through the generic offline and private configuration entry points. Both use the previously verified local native Claude executable, independently signed grants and private writable synthetic Fixture roots; Repo Ops disabled. No production workdir or credential is imported. All six installer/configurer steps exited 0. Installed package file parity, current owner identity, exact private policies, and frozen source digests checked before activation.

## OpenAI Connector readiness

Read-only inventory of configured protected OpenAI manifests:

| Classification | Registered | Enabled | Running |
| --- | --- | --- | --- |
| Default, production-associated Tunnel | yes | no | no |
| inst-v25-0190b734a911 / oa-97045fd3, known synthetic test input | no | no | no |

There are zero independently verified real nonproduction Tunnel credentials in this manifest inventory. The synthetic input provenance is recorded by the previous [generic Connector provisioning gate](v0250-windows-generic-connector-provision-2026-11-20.md). Enumeration is confined to configured manifests; no control-plane key file contents were read, no arbitrary credential search, no OpenAI control-plane or network calls. The default Connector's local Disabled state must not be inferred to mean the separate production legacy Tunnel is available for reuse.

**Readiness: BLOCKED_NO_VERIFIED_NONPRODUCTION_TUNNEL.** The operator needs a dedicated OpenAI Tunnel identity and its private credential file, with separately trusted matching tunnel-client and bundled cloudflared binary pins. Do not provide keys in chat, command-line values, an exposed workdir, or source control. Validate the actual client-side route separately before activation. Do not substitute a production Tunnel or the known synthetic credential.

## Native no-model I11 gate

The original strict I11-B history remains unmodified and open. A proposed new real dual-provider run was blocked by the execution platform's safety check before its script was written or any model task submitted. No bypass or retry was attempted. A separate no-model native gate exercised the freshly staged pair instead.

| Marker | Result |
| --- | --- |
| EXACT_WHEEL_AND_OWNED_PAIR_PREFLIGHT | PASS: all six installed environments match pinned payloads; correct independent owned bindings |
| FOUR_NATIVE_COMPONENTS_STARTED | PASS: two Core and two Bridge Scheduler owners with four distinct process generations |
| TWO_SIGNED_CLAUDE_DISCOVERY_AND_CROSS_AUTH_REFUSAL | PASS: both actual signed Windows pipes, Claude availability, A's foreign HMAC refusal, both HTTP foreign and invalid Bearer refusals, separate MCP workdir/runtime discovery |
| A_FILE_CORE_AND_B_CLAUDE_DISCOVERY_SURVIVE_A_BRIDGE_STOP | PASS: Bridge A disabled, both Core generations and Bridge B unchanged; A fixture file read and B runtime discovery succeed |
| A_CLAUDE_DISCOVERY_RESTORED_B_CORE_GENERATIONS_UNCHANGED | PASS: fresh Bridge A generation restores discovery, with both Core and Bridge B generations unchanged |

REFRESHED_I11_NO_MODEL=PASS, terminal audit COMPLETE/PASS, cleanup_verified=true. Four exact new scheduled tasks uninstalled; both test ports released; both synthetic fixture files unchanged. Private fixture installations retained for future explicitly authorized tests. Original 40 protected Scheduler entries and production Core generation unchanged; source snapshot digests unchanged.

This is NOT a real model-output, task event/result, or cross-owner real task cancellation proof. The earlier I11-B exact-response failure remains open; the new stage/no-model PASS does not relabel it. The actual Claude runtime availability and lifecycle evidence is transport/control-plane evidence only.

## Evidence

Paths relative to candidate .remember/tmp/v0250-win-versioned-python-20261120/:
- refreshed-i11-provider-stage-20261121/audit.json
- refreshed-i11-no-model-20261121/audit.json
- scripts/refreshed_i11_provider_stage_20261121.py
- scripts/refreshed_i11_no_model_20261121.py

No production lifecycle mutation, Cloudflare or OpenAI route modification, real model calls from fixture, credential export, Git stage/commit/push/tag, or release publication. The host execution Agent only ran bounded native verification scripts. The two remaining external acceptance gates, shared POSIX ACP regression, three-platform integration/CI and release metadata coordination remain separate from this native PASS. This is a documentation-only delta to the frozen release candidate; runtime Wheel bytes were not changed.
