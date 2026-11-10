# ServerFS v0.24.2 — Native Windows Cloudflare Tunnel

Status: **release candidate pending final CI and long-lived SSE/reconnect evidence**. Native HTTP, cloudflared bootstrap, independent same-user Task Scheduler owners, credential ACL validation, and Windows implementation acceptance are complete. Live ChatGPT MCP acceptance passed for both ingress owners, including tools and cross-ingress read/write/edit/delete on the same writable workdirs, plus Codex and Claude. A >60-second SSE hold and same-session behavior after a Connector-only restart remain pending. v0.24.2 is **not published**; v0.24.1 remains stable. Depends on v0.24.1's accepted provider-selection and credential/lifecycle contract; do not silently inherit macOS operating-system assumptions.

## Objective and supported scope

Add an optional **non-admin native Windows 11 x64 Cloudflare Tunnel** ingress for ServerFS Streamable HTTP, preserving the existing v0.14+ Task Scheduler 2.0 → watchdog → managed runner lifecycle, official OpenAI Tunnel mode, Windows ABI3 native filesystem controls, Agent Bridge and Repo Ops. **Do not require Docker, administrator/UAC, Windows Service/SCM or LocalSystem.**

## Architecture constraints

- OpenAI Tunnel remains the native default. Support two independent same-user Task Scheduler owners: the existing OpenAI owner and a separately named Cloudflare owner (`ServerFS Cloudflare WorkPC`, managed through `service cloudflare-parallel`), each with its own venv, state, watchdog and runner. Each owner retains its own process containment and bounded restart policy; they may share the same writable workdirs and one Agent Bridge/Repo Ops service. A single owner still runs only one provider, and there is no universal automated provider handover. Retiring the OpenAI owner must not retire the shared Agent Bridge while Cloudflare still depends on it.
- Use a versioned, official, integrity-verified `cloudflared` Windows x64 executable staged **per user**, not a global PATH/system install. Verify supported run and token-file option in Phase 0; reject missing or untrusted binary.
- Store Connector Token outside all ServerFS workdirs in a current-user private location with **verified Windows DACL/owner protection**, not merely POSIX-style permissions or inherited writable ACLs. Never put the raw Token in subprocess argv, logs, Task XML, registry, `.env` inside workdirs or ChatGPT chat. Select DPAPI or ACL-based-at-rest handling only after evaluating integration and recovery tradeoffs.
- Cloudflare connects outbound to a **locally bound native HTTP /mcp origin** with strict Host/Origin allowance. Explicitly verify the native Windows HTTP listener/service orchestration rather than assuming Linux Compose semantics. Cloudflare edge HTTPS and independent ServerFS API Key/OAuth admission are mandatory.
- Preserve non-elevated Task Scheduler registration and stop order: disable task before controlled stop to prevent heartbeat resurrection. A failed Cloudflare startup must not cause uncontrolled process spawning or implicit takeover from a working OpenAI Tunnel.
- Keep v0.24.1 macOS and v0.24.0 Linux behavior unchanged, aside from explicitly shared tested interfaces.

## Phase 0 — Design/feasibility gate

1. Inspect `src/serverfs_mcp/windows_service.py`, `native/windows`, `windows_job.py`, `native_config.py`, `native_tunnel.py`, `tunnel_bootstrap.py`, `src/serverfs_mcp/main.py`, and Task Scheduler watchdog/runner tests. Verify a viable native loopback Streamable HTTP server lifecycle and process ownership.
2. Confirm Cloudflare's official Windows x64 executable, stable version/checksum verification, `--token-file` compatibility, filesystem ACL/DPAPI options, no-UAC install, and safe update/rollback behavior.
3. Compare planned provider selection and credentials from v0.24.1 with Windows-specific constraints. Freeze the minimal portable contract and OS-specific lifecycle changes; avoid speculative cross-platform frameworks.
4. Define migration/rollback acceptance from existing OpenAI Tunnel. Prove duplicate ownership refusal, runtime exit semantics, operator intentional stop, watchdog restart and controlled shutdown without leaving a public unauthenticated listener.

## Phased implementation (after Phase 0 approval)

- **A. Bootstrap/configuration:** verified official binary installation as current user; explicit provider configuration; safely validated private token path and effective HTTP auth guard.
- **B. Lifecycle:** Task Scheduler/Runner uses existing Job Objects, watchdog policy, restart/stop/uninstall and status observability; no second SCM service. Preserve non-admin install and working OpenAI mode.
- **C. Windows security/regression:** native path policy, DACL protections, no secrets in command lines/logs, Host/Origin, API Key/OAuth, benign read-only workdir; deterministic Windows-native tests including failure-injection, reconnect and rollback.
- **D. Live client acceptance:** on a separately configured Windows 11 x64 non-admin machine, verify public DNS/TLS, 401/200, ChatGPT tools, cross-ingress read/write/edit/delete on shared writable workdirs, and Codex/Claude through two independent owners. Keep the >60-second SSE hold and same-session behavior after a Connector-only restart as final pending evidence. Record the pre-existing Qoder provider error and the Python urllib default User-Agent WAF 1010 as scoped provider/edge caveats, not regressions in this release.
- **E. Documentation/release:** synchronize all seven Skills (Windows tunnel reference and provider routing), Plugin ZIP, Windows runbook, README bilingual, website, release notes, version metadata/locks and exact-SHA CI matrix; dispatch-first publication only after full validation.

## Release gates

- Fresh install and upgrade from supported OpenAI Tunnel mode, expected stopped/disabled semantics, rollback without losing credentials.
- Current-user DACL/secret protection and no admin permission; no public host port except explicitly approved loopback origin; Cloudflare only outbound, ServerFS caller admission independently enabled.
- No duplicate managed Tunnel or orphaned process; bounded reconnection and explicit error state for missing token, blocked Cloudflare, stale config, failed update and restart.
- Final live >60-second SSE and Connector-only restart/same-session evidence; do not assert lossless in-flight SSE delivery.
- Windows Native + Windows Agent suites, Linux and macOS regression CI, Skills/Plugin deterministic package and bilingual site build all pass.

## Exclusions

No Linux Compose refactoring, macOS 28/Intel support, general Windows ARM64 claim, automatic Cloudflare DNS/control-plane provisioning, new network-facing MCP tools, or unapproved replacement of existing production Tunnel.
