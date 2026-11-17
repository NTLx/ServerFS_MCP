# macOS v0.25.0 synthetic Connector KeepAlive acceptance — 2026-11-16

## Result

**PASS — one bounded native acceptance run completed.** On Apple Silicon macOS 27.2.1, the existing acceptance script staged and registered one randomly named, signed file-only Core and a synthetic no-network Connector that executes `/bin/sleep 300`. The Connector's one verified test-owned PID was SIGKILLed by the script; its same native launchd label recovered under a new PID in 5.976 seconds. The script reported four authenticated local MCP file-operation roundtrips, unchanged Core and production PIDs, enabled native registration, and successful cleanup.

Result and postflight were recorded at **2026-11-16 12:30 CST (Asia/Shanghai)**.

This is **not production Cloudflare crash recovery** and **not a live Cloudflare network test**. The real Cloudflare Tunnel executable, token and hostname were not used or changed.

## Command and captured result

The exact script command was run once from the repository's existing `.venv`, with `-B`, through a current-user Python subprocess runner capped at 240 seconds. The runner captured stdout, stderr and exit status:

```sh
.venv/bin/python -B deployment/macos/v0250_named_connector_lifecycle_acceptance.py --execute --verify-keepalive
```

Runner exit: **0**. Script exit: **0**. Stderr was empty. Script JSON:

```json
{
  "cleanup": "test-owned jobs stopped and staged state removed",
  "connector_only_stop_restart": true,
  "core_pid_unchanged": true,
  "crash_pid": 77411,
  "errors": [],
  "fixture": "no-network exec /bin/sleep",
  "instance": "v25c-dbb587ad",
  "keepalive_verified": true,
  "mcp_roundtrips": 4,
  "named_state_removed": true,
  "original_production_pids_unchanged": true,
  "recovery_pid": 77476,
  "recovery_seconds": 5.975903250044212,
  "result": "PASS",
  "retained_diagnostic_dir": null
}
```

The script's KeepAlive preflight verified the signed app with `codesign`, `KeepAlive=true` in the staged launchd plist, and the test Connector PID's exact current UID and `/bin/sleep 300` command before its single SIGKILL. Authenticated MCP roundtrips ran before and after recovery. The recovered PID and native registration were rechecked and registration remained enabled.

## Read-only baseline and postflight

Before registration, the host reported `arm64`, macOS 27.2.1 and UID 501. The only `com.ntlx.serverfs.*` launchd labels were the four expected defaults; each was running. Full process commands and launchd PID mappings matched:

| Production label | PID | Process |
| --- | ---: | --- |
| `com.ntlx.serverfs.tunnel` | 1779 | Repository `.venv` `serverfs_mcp.cli tunnel` |
| `com.ntlx.serverfs.bridge` | 1790 | `agent_bridge/.venv` `serverfs-agent-bridge` |
| `com.ntlx.serverfs.http-origin` | 40516 | Repository `.venv` `serverfs_mcp.cli serve` on port 8765 |
| `com.ntlx.serverfs.cloudflare` | 66419 | Existing `cloudflared tunnel` process |

No pre-existing `v25c-*` launchd label, instance root, or `serverfs-v25-named-connector-*` scratch directory was found. After the run, the four labels still mapped to the same PIDs and command lines. The test label, instance root `/Users/lx/Library/Application Support/ServerFS/instances/v25c-dbb587ad/service-manager`, and scratch directory were absent. PIDs 77411 and 77476 were absent, and no `/bin/sleep 300` child remained. The Core process and its listener were gone after cleanup; the script does not include the randomly selected port in its result, so no port number was available for a separate exact-port probe.

## Scope and limitations

Only the new named test services, temporary synthetic files/credentials, and their local loopback MCP endpoint were involved. No production service was manually killed, restarted, registered, or reconfigured. The single authorized SIGKILL targeted only the verified synthetic Connector PID. This evidence covers this isolated macOS KeepAlive path; it does not establish recovery of real `cloudflared`, production Tunnel connectivity, public ingress, or other platforms.
