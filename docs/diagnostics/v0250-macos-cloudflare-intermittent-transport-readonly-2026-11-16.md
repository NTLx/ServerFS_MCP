# macOS v0.25.0 candidate — intermittent Cloudflare MCP tool failure, read-only triage

Date: 2026-11-16; native diagnostic window: 13:05–13:21 CST (UTC+08:00).

## Scope

The existing production connector/core still report the **0.24.2** installed version. The ChatGPT-connected Cloudflare tool has intermittently returned `The tool failed internally` during `list_workdirs`, while the preserved OpenAI Tunnel tool was usable; other invocations through both connectors succeeded. This audit is **not a failure reproduction with a correlated request ID**, and no configuration, credential, daemon, process or Git state was changed.

## Verified production baseline

| Service label | PID | State |
| --- | ---: | --- |
| `com.ntlx.serverfs.tunnel` | 1779 | running |
| `com.ntlx.serverfs.bridge` | 1790 | running |
| `com.ntlx.serverfs.http-origin` | 40516 | running |
| `com.ntlx.serverfs.cloudflare` | 66419 | running |

Cloudflare Connector PID **66419** matches the earlier accepted 12:30 CST baseline, and its local process start was reported as 10:10:52 CST. Launchd showed `runs=1` and `last exit=never exited` for each default service; these fields do not establish historical root cause or guarantee future liveness.

- Direct loopback anonymous `GET http://127.0.0.1:8765/mcp`: **401** as expected.
- Direct, no-proxy public HTTPS anonymous `GET https://macbook-serverfs.ntlx.top/mcp`: **401**, verified TLS. This proves path-to-auth-boundary reachability, not authorized MCP session success.
- The Connector had two UDP sockets and a local TCP connection to port 8765. UDP sockets alone do not confirm four healthy QUIC connections or end-to-end reliability.

## Time-bounded log classification

Read only `cloudflare.stderr.log` and `http-origin.stderr.log` at their already owned paths; no credential values or raw log lines were copied to this report.

In the approximately 15-minute UTC **05:05–05:21:14** window, Cloudflare stderr contained **58 lines** matching broad network/connection/QUIC keywords and **14 lines** matching broad 5xx/runtime keywords (categories can overlap). Matching timestamps were within 05:06:07–05:21:14 UTC. HTTP Core stderr matched **zero lines** in the same network/auth/5xx keyword categories. These are broad text-classification counts, **not verified fault counts** and cannot be matched to particular ChatGPT tool calls.

## Attribution and action

The intermittent ChatGPT tool failure remains **UNATTRIBUTED**: possible client/provider invocation layer, Cloudflare edge/tunnel transport or a particular authenticated Core request. Without a failed tool invocation's exact client timestamp/timezone and request/event identifier, an anonymous HTTP 401 and broad Connector log patterns cannot distinguish them.

Do **not** restart the production Connector, rotate tokens, relax approval/authentication, disable QUIC or add blind retries based on these observations. Next supported diagnostic is a single failed and successful client call with exact timestamp/event ID correlated to corresponding request-level Connector/Core evidence. Any proposed remediation requires a measured failing layer.

No native recovery test performed in this audit. The previously verified synthetic no-network Connector KeepAlive recovery is a **separate PASS** and does not establish actual `cloudflared` crash/QUIC/public ingress recovery.
