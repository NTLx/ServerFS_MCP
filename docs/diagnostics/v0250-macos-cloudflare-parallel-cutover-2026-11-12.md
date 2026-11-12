# v0.25.0 macOS Cloudflare Parallel Cutover

**Outcome: DEPLOYED**

Transaction result: local and public MCP verification passed

Private backup and credential files were preserved. No Cloudflare WAF, DNS, Tunnel token, OpenAI Tunnel configuration, or Agent Bridge configuration was changed.

## Gates

- six_preflight_gates: PASS
- manifest_byte_identity: PASS
- snapshot_byte_identity: PASS
- process_baseline: PASS
- backup_source_identity: PASS
- private_core-http.env: PASS
- private_source-paths.json: PASS
- private_original-services.json: PASS
- private_original-http-origin-environment.json: PASS
- public_anonymous_401: PASS
- old_pair_retired: PASS
- new_pair_staged: PASS
- new_pair_started: PASS
- openai_bridge_unchanged: PASS
- http://127.0.0.1:8765/mcp_mcp_tool_surface: PASS
- http://127.0.0.1:8765/mcp_workdirs_visible: PASS
- http://127.0.0.1:8765/mcp_workdirs_writable_agent: PASS
- http://127.0.0.1:8765/mcp_agent_runtimes: PASS
- https://macbook-serverfs.ntlx.top/mcp_mcp_tool_surface: PASS
- https://macbook-serverfs.ntlx.top/mcp_workdirs_visible: PASS
- https://macbook-serverfs.ntlx.top/mcp_workdirs_writable_agent: PASS
- https://macbook-serverfs.ntlx.top/mcp_agent_runtimes: PASS
- openai_bridge_final_pids: PASS

Ingress verification (tools/workdirs only; no secret values): {"local": {"agent_tool": true, "tools": 23, "workdirs": 2}, "public": {"agent_tool": true, "tools": 23, "workdirs": 2}}

Protected process owners `tunnel` and `agent-bridge` retained their baseline PIDs.

## Post-cutover independent verification

- Cloudflare control plane: `macbook-serverfs` Tunnel remains **healthy**, with one macOS connector and four active connections; ingress hostname `macbook-serverfs.ntlx.top` points to `http://127.0.0.1:8765` with exact matching origin Host header. No DNS, WAF, Tunnel config or Access modification was required.
- Newly supervised HTTP Core PID **99271** and Cloudflare Connector PID **99279** were observed enabled after cutover. Pre-existing OpenAI Tunnel PID **1779** and Agent Bridge PID **1790** remained unchanged.
- Existing OpenAI Tunnel-backed ChatGPT ServerFS connector independently verified: both `serverfs` and `codexsatellites` show read-write access, binary transfer enabled, workspace-write Agent modes and runtimes `claude/codex/qoder`. All three runtimes returned as available.
- Cloudflare public MCP `tools/list`, `list_workdirs` and `list_agent_runtimes`: PASS. 23 tools exposed; both workdir policies and all three runtimes verified through the new public ingress.
- **Historical Python SDK harness outcome: NOT_VERIFIED (subsequently resolved).** Two bounded post-cutover harnesses failed to obtain a consistently interpreted `CallToolResult` during their first tool-call steps; an earlier harness also misclassified expected `isError=true` results as exceptions. These harness failures were not evidence of a ServerFS product regression. Subsequent direct ChatGPT MCP tool verification is recorded below. No actual Agent job execution or Repo Ops was performed.
- **Residual-file cleanup independently VERIFIED.** Direct read-write ServerFS MCP (existing OpenAI ingress) enumerated both workdir roots and found one leftover random-named `v0250-cf-crud-*.txt` with exact test content `phase1` in `codexsatellites`. It was removed using `delete_file` with a matching expected revision. A second complete root enumeration of both workdirs found zero files matching either `v0250-cf-crud-*.txt` or `v0250-cf-acceptance-*.txt`. No other workdir files were touched in this cleanup.

## Direct ChatGPT Cloudflare connector acceptance (2026-11-12)

The newly registered `macbook-serverfs` ChatGPT connector was used directly (not through a local Python SDK harness). Independent comparison used the existing `macbook-serverfs-mcp` OpenAI Tunnel connector:

- `list_workdirs` on both connectors: identical `serverfs` and `codexsatellites` aliases; read-write, binary transfer enabled, `workspace-write` Agent mode and runtimes `claude/codex/qoder`: **PASS**.
- Cloudflare `list_agent_runtimes`: all three runtimes present and available: **PASS**.
- Cloudflare reading this existing cutover report from the `serverfs` workdir: **PASS**.
- **Cloudflare CRUD with revision guard: PASS in both workdirs.** Each workdir was tested with a unique `v0250-cf-direct-*.txt` test file. Cloudflare `create_text_file` wrote `phase1`; both connectors independently read matching text and identical revision. Cloudflare `edit_text_file` with the current expected revision changed it to `phase2`; both connectors read back matching content and revision. A second Cloudflare edit using the *stale* original revision was rejected with `REVISION_CONFLICT` in both workdirs. Cloudflare `delete_file` with the new current revision succeeded in both workdirs.
- **Cleanup: PASS.** Complete root-directory enumerations through *both* connectors confirmed no files matching `v0250-cf-direct-*.txt`, `v0250-cf-crud-*.txt` or `v0250-cf-acceptance-*.txt` remained in either workdir.
- **Cross-ingress data and revision consistency: PASS.** The two public MCP ingress implementations returned identical contents and opaque revision IDs for the same test files before and after edits.

## Read-only Agent task E2E via registered Cloudflare ChatGPT connector

- **PASS**: A real `codex` Agent task was submitted with the new `macbook-serverfs` MCP connector (task `agt_589e4b79247bae63157c1e76`) and transitioned through `queued` -> `running` -> `succeeded`. The returned provider result verified that the existing `AGENTS.md` was readable and reported its stable-release line (`v0.24.2`). No file mutation was requested.
- This confirms the new Cloudflare ChatGPT MCP connector can submit, observe, and obtain a completed native Codex Agent task. It does **not** constitute a workspace-write Agent mutation test, a full model/provider matrix, or Repo Ops acceptance.

The authenticated dual-ingress deployment is operational: **public filesystem CRUD/revision acceptance and a read-only Codex Agent E2E have passed**. Workspace-writing Agent job E2E and per-provider Agent execution remain untested. The uncommitted v0.25.0 candidate does **not** authorize project-wide v0.25.0 release, nor does it imply two independently running Agent Bridges: both ingress implementations intentionally use the existing native Bridge.
