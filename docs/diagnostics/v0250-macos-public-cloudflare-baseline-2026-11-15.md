# ServerFS MCP v0.25.0 — macOS Cloudflare public ingress baseline (2026-11-15)

## Outcome and boundary

**Existing public Cloudflare MCP ingress: PASS through the already registered ChatGPT Cloudflare connector. New isolated v0.25.0 HTTP origin routed publicly: NOT ACCEPTED.** These are different gates. No production cutover or native v0.25.0 SMAppService registration was performed. No Git commit, push or release was performed.

## Live baseline

- Mac: Apple Silicon, macOS 27.2.1.
- Protected default service PIDs observed: OpenAI Tunnel `1779`; Agent Bridge `1790`; HTTP origin `99271`; Cloudflare Connector `99279`. The default HTTP origin listens on `127.0.0.1:8765` and the Cloudflare connector readiness endpoint returned 200.
- Existing `macbook-serverfs` Cloudflare Tunnel ID: `1a19184a-741d-41de-a86f-c0f3119f95bb`. Cloudflare API returned `healthy`, `remote_config=true`.
- Remote configuration version `3`: one ingress hostname `macbook-serverfs.ntlx.top` -> `http://127.0.0.1:8765` (Host override `macbook-serverfs.ntlx.top`), followed by `http_status:404`. DNS is proxied CNAME to the same Tunnel. Verified again after this round; original route unchanged.
- Running macOS cloudflared uses a private `--token-file` credential at `~/Library/Application Support/ServerFS/credentials/cloudflare/macbook-serverfs.token`. Credential bytes and API key values were not read or displayed in this record.
- Currently registered launchd services correspond to existing default 0.24.2 deployment; no named v0.25 Shared Core or Connector job was found live.

## Authenticated MCP evidence

1. Local `127.0.0.1:8765/mcp`: anonymous returned HTTP 401; with existing local caller authorization `initialize`, `tools/list` and `list_workdirs` returned successfully (MCP tool inventory 23; two read-write aliases `serverfs`, `codexsatellites`).
2. From the Mac as an external HTTPS client, anonymous and authenticated requests to `https://macbook-serverfs.ntlx.top/mcp` returned Cloudflare **403 / error 1010**, before reaching MCP. The client stopped; it did not spoof headers or weaken edge security. These Mac-originating HTTPS probes do **not** pass a public MCP acceptance gate.
3. Separately, ChatGPT's already connected **Cloudflare** `macbook-serverfs` connector (not the legacy OpenAI Tunnel connector) successfully invoked `list_workdirs` and then performed live MCP filesystem CRUD: created unique test file `v0250-public-mcp-e2e-20261115-h6p9.txt` in `serverfs`; the OpenAI Tunnel `macbook-serverfs-mcp` connector independently read exactly the same bytes and opaque revision; a Cloudflare conditional edit was visible through both ingress paths with matching new revision; editing with the stale revision rejected with `REVISION_CONFLICT`; Cloudflare delete succeeded. Exact temporary test file is deleted. This establishes currently operational ChatGPT Cloudflare MCP tool ingress, not deployment of a new v0.25 origin.

## Isolated v0.25 public origin attempt

- Target test hostname was `macbook-v0250-serverfs.ntlx.top`, intentionally different from the production hostname but sharing the existing `ntlx.top` DNS zone and the `macbook-serverfs` Tunnel. The dedicated proxied CNAME record was created and then **deleted** (Cloudflare API verified zero remaining test-host records). No new Tunnel ingress rule was added, no production hostname/route/DNS was modified, and the Tunnel config is still version 3.
- Local temporary candidate attempted port `18765`, using separate previously installed Core/Bridge wheel venvs and a supervised 240-second cleanup boundary. Local startup did **not** reach readiness owing to mistakes in the fresh ephemeral test harness's Bridge runtime policy and Core state/manifest path setup. Diagnostic work verified `serverfs_mcp/instance_identity.py` is **present** in the candidate wheel archive and installed wheel venv; an earlier tentative claim of missing package content was disproved. No successful local HTTP origin readiness, DNS-to-test-origin routing, or public v0.25 MCP E2E is claimed.
- Candidate supervisor confirmed the test processes, Unix socket and its scratch were removed. The production Core, Tunnel, Bridge, and live sessions were not modified.

## Next acceptance requirements

1. Reuse the proven signed `resolve_shared_core_manifest`, `instance_service_home` and installed-wheel three-process test fixture to prepare a **real v0.25** local Core on a free loopback port, and confirm authenticated local MCP before changing Cloudflare.
2. Preserve the existing Tunnel credential, existing hostname route, and production services. Add an independently scoped temporary test hostname to the remotely managed Tunnel and its DNS only after the isolated origin is READY. Verify the control plane configuration can be restored, then test HTTPS endpoint without defeating Cloudflare WAF.
3. Run authenticated public `initialize`, `tools/list`, `list_workdirs`, authorization negative tests and recovery; do not equate Cloudflare 1010 (edge block) with origin failure. Remove the test ingress and CNAME, verify exact production routing and process baselines, and only then mark public v0.25 E2E accepted.
4. Complete real two-Shared-Core macOS `SMAppService` registration/recovery and exact SHA cross-platform release acceptance separately. Existing wheels carry 0.24.2 metadata from a dirty candidate checkout, not an official v0.25.0 release.

## Follow-up: real isolated HTTP origin + temporary Tunnel ingress (same day)

The first handmade origin supervisor failed preflight because of scratch Bridge runtime policy and Core manifest subdirectory handling. In contrast, a follow-up opt-in HOLD mode reused the **already passing** installed-wheel three-process fixture in `tests/test_darwin_v0250_installed_wheel_three_process_macos.py` and its wrapper. The normal wrapper E2E passed first. The HOLD used one Bridge and two separate HTTP Core processes from the existing independent wheel venvs with `python -I -B`, random Bridge grant secrets and random HTTP Bearer tokens, with no checkout source injection in any service subprocess. Its Core A served `127.0.0.1:18765`, confirmed local anonymous **401** and authenticated `list_workdirs` before creating private `0600` READY metadata and bearer files. The hold was strictly limited to **180 seconds**; its eventual pytest exit was **0**, with all three child PIDs, the Unix socket and the scratch removed after timeout. A mistaken interim assessment that the Core wheel lacked `instance_identity.py` was definitively refuted by inspection of the archive and installed venv.

While that origin was READY, the authenticated Cloudflare API added a **temporary test** proxied CNAME `macbook-v0250-serverfs.ntlx.top` to the **existing** `macbook-serverfs` Tunnel and inserted a separate ingress to `http://127.0.0.1:18765` with a localhost Host override. The production hostname ingress to `http://127.0.0.1:8765` and the fallback rule were preserved. Remote Tunnel config advanced from version **3 to 4**, with Tunnel health remaining healthy. A separate native-agent anonymous local GET to the v0.25 Core returned **401**; the anonymous HTTPS GET to the temporary hostname **timed out after approximately 8.2 seconds**, yielding no HTTP status or proven application request. A separate web-client probe also could not access the test hostname. **Public v0.25 MCP initialization, authentication and tools were NOT VERIFIED.** A hostname/Tunnel configuration existing in the control plane is not itself public transport evidence, and the external request timeout is not proof of an origin bug.

After the bounded origin terminated, the Cloudflare Tunnel ingress was restored to precisely the original production hostname/port and fallback; remote config advanced **4 to 5**. The exact temporary CNAME record was deleted. A final independent Cloudflare API read confirmed **healthy** Tunnel, exactly the two original ingress rules, the same production Host override, and **zero** records for the temporary hostname. Production service sessions and credentials were not modified. No Git commit, push or official release was performed.

**Next:** establish a test route with an observable external client that can complete TLS/MCP over the public hostname without bypassing Cloudflare security. Diagnose edge DNS/TLS/WAF behavior, retaining a bounded local v0.25 origin through route propagation; do not re-label the existing public 0.24.2 Cloudflare connector's successful CRUD tests as new v0.25 public acceptance. The current v0.25 isolated Core/Bridge E2E fixture and its hold mode can be reused without manually recreating signed manifest state.

