# ServerFS MCP macOS v0.25.0 Cloudflare in-place cutover audit — 2026-11-15

## Result

The dirty v0.25.0 source candidate Core and independent Cloudflare Connector are staged, registered, and running. This is an in-place cutover on the existing hostname and loopback port. It does not change DNS, WAF, Cloudflare Tunnel configuration, OpenAI credentials, Bridge configuration, or version metadata. The installed metadata may still report v0.24.2; this audit makes no release claim.

## Private rollback material

The four rollback files originally found under the repository workdir were migrated to:

`/Users/lx/Library/Application Support/ServerFS/v0250-cutover-2026-11-15-9a4dedacbde3`

Before removing the repository-local source, each destination file was checked against its source by SHA-256. Destination inodes were distinct, every entry was a regular file with one hard link, the directory mode was `0700`, and file modes were `0600`. The source directory was removed only after these checks. A post-migration check confirmed four files, the same modes/link properties, and no repository-local source directory. The original Cloudflare Token file was not copied or modified; its path remains `/Users/lx/Library/Application Support/ServerFS/credentials/cloudflare/macbook-serverfs.token`.

The private rollback plan's embedded env-file path refers to the removed repository directory. Its rollback sequence remains applicable with the migrated `rollback-core-http.env` at the private path above. No secret values or private file contents are recorded here.

## Cutover and local verification

Commands were invoked from the repository virtual environment; every listed lifecycle command exited `0`:

1. `service cloudflare unstage` — removed only the previous independent Cloudflare manifest entry.
2. `service core unstage` — removed only the previous independent Core manifest entry.
3. `service core stage` — canonical `/Users/lx/Projects/ServerFS_MCP/serverfs.toml`, migrated private env, port `8765`, hostname `macbook-serverfs.ntlx.top`; output `staged-not-registered`.
4. `service cloudflare stage` — existing local `cloudflared` binary and original Token file, same hostname; output `staged-not-registered`.
5. `service start --component core` — output `http-origin: enabled`.
6. `service start --component cloudflare` — output `cloudflare: enabled`.

Observed local state after startup:

- Core / HTTP Origin: PID **40516**, registration `enabled`, listener `127.0.0.1:8765`.
- Cloudflare Connector: PID **40543**, registration `enabled`; executable is the existing local `cloudflared` binary.
- Anonymous local MCP POST: HTTP **401**.
- Authenticated local MCP: initialize and `tools/list` succeeded; **23 tools**.
- `list_workdirs`: **2**, both `read-write`.
- `list_agent_runtimes`: Claude, Codex and Qoder all reported available.
- Core and Cloudflare component status commands exited `0`.

The Cloudflare log records four registered QUIC tunnel connections at 2026-11-15 13:58:56–13:58:59 UTC after earlier edge connection timeouts. This establishes connector-to-edge tunnel readiness at that time, not public MCP acceptance.

## Protected services and ports

The default service status command exited `0` and reported the protected OpenAI Tunnel `com.ntlx.serverfs.tunnel` still enabled at PID **1779**, and Agent Bridge `com.ntlx.serverfs.bridge` still enabled at PID **1790**. Both match the preparation baseline. No general service lifecycle command was used. The only configured production listener added by this cutover is loopback TCP **8765**; no temporary listener remains on **18765**.

## Public verification limit

A direct, non-proxied HTTPS unauthenticated request to `https://macbook-serverfs.ntlx.top/mcp` returned HTTP **403**. A separate curl request through the host's configured network path returned **401**, but its remote address was loopback and is not treated as public Cloudflare evidence. No WAF or Cloudflare security control was bypassed. Public caller-authenticated MCP initialization/tools discovery therefore remains **NOT VERIFIED**. The Cloudflare route and public URL are unchanged from the prepared baseline; no control-plane API mutation was performed.

## Post-cutover ChatGPT Cloudflare connector E2E (independent evidence)

After re-registering the Core/Connector, ChatGPT invoked the **Cloudflare** `macbook-serverfs` MCP connection directly and compared it against the separately preserved **OpenAI Tunnel** `macbook-serverfs-mcp` connection. Both `list_workdirs` calls returned the same two writable workdirs (`serverfs`, `codexsatellites`) and available Agent runtimes `claude`, `codex`, `qoder`. Both connections read the same repository `README.md` with identical content and revision.

- In `serverfs`, the Cloudflare connector created test file `v0250-cloudflare-cutover-e2e-20261115-j7m2.txt`; the OpenAI connector independently read identical contents and opaque revision. Cloudflare conditional edit propagated correctly to OpenAI's view and yielded the same new revision. A deliberately stale revision was rejected (`REVISION_CONFLICT`). Cloudflare deletion returned success.
- In `codexsatellites`, Cloudflare created `v0250-cutover-check-20261115-z8q1.txt`; OpenAI read matching content/revision; Cloudflare delete succeeded.
- Cloudflare `upload_binary_file` of five test bytes to `codexsatellites/v0250-cutover-bin-e2e-20261115-r3x9.dat` and OpenAI `download_binary_file` returned identical SHA-256 `2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824`. Test binary deletion succeeded.
- The existing Cloudflare Tunnel control plane was independently read after migration: Tunnel `macbook-serverfs` still **healthy**, remote config version **5**, original hostname `macbook-serverfs.ntlx.top` routes to `http://127.0.0.1:8765` with the original Host override, followed only by `http_status:404`. No remote route or DNS change occurred in this cutover.
- A read-only Codex task submitted via the Cloudflare connector (`agt_2f8de67c6a217221bd490c6c`) reached task state **`succeeded`** with no service `error_code`, but its *final response* reported that its attempted workspace-file tool reads were rejected (`user rejected MCP tool call`). This is evidence that submission and result retrieval work; it is **not** evidence that file-reading Agent delegation succeeded. Subsequently, a separate Cloudflare Codex task (`agt_eb88c912122b546aa0e72ffd`) performed a no-tool transport-only run and returned `CLOUDFLARE_AGENT_TRANSPORT_OK`, succeeded. Thus Cloudflare Agent **task transport is PASS**, while Agent **tool-access authorization and completed file-reading delegation remain NOT VERIFIED**. Cloudflare MCP direct file CRUD and runtime discovery were independently validated. No workdir files were changed by either Codex task.

These live ChatGPT connector actions constitute real public Cloudflare MCP **filesystem tool CRUD, revision guards and binary transfer** acceptance for the new Core/Connector service. They do not prove generic unauthenticated HTTPS status codes, persistent SSE sessions, arbitrary public clients, or completed Agent delegation after cutover; these remain separate gates. All named test files above were deleted using the Cloudflare MCP connector.

## Cross-connector delegated Agent permission comparison (2026-11-16)

The new Cloudflare MCP connector and preserved OpenAI Tunnel connector each submitted a separate **Codex Agent** with the same narrow operation: read only `codexsatellites/README.md` and report its first heading. In both cases the task was accepted and a read was attempted, but the final response reported **`user rejected MCP tool call`** (Cloudflare `agt_e8751fd4b0d6a2d32695ccdd`; OpenAI `agt_393224e6a462cdb284c48603`). Neither retried or modified files. Direct MCP file CRUD remains successful via both connectors; Cloudflare no-tool Codex task completed normally. A separate Claude Agent task did not produce a useful event stream and was cancelled; it provides no success/failure evidence.

**Interpretation:** the rejected delegated workspace reads are not unique to the newly deployed Cloudflare Connector. The shared Agent approval / execution policy is the next diagnosis target. The normalized task output does not disclose an exact rejected operation or approval policy rule; do not claim a specific configuration defect, downgrade privileges or bypass approvals. A positive delegated workspace read remains pending.

## Rollback

No rollback was required: both scoped stages and starts succeeded, local auth/MCP authorization passed, all services registered, protected PIDs remained stable, and the Connector established four edge connections. If rollback becomes necessary, stop only `cloudflare`, then `core`; unstage only those two independent manifest entries; restore Core and Connector definitions from the private original snapshots using the migrated private HTTP env path and original untouched Token file; start Core then Cloudflare; recheck local authorization, Tunnel/Bridge PIDs and connector readiness. Do not use the stale repository path embedded in the private plan.
