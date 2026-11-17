# macOS v0.25.0 Cloudflare Connector-only restart acceptance — 2026-11-16

## Result

**PASS for one bounded Connector-only restart and observed recovery** of the
existing production hostname `macbook-serverfs.ntlx.top`. The independent HTTP
Core, existing OpenAI Tunnel, and Agent Bridge remained online with unchanged
PIDs. The Cloudflare Connector obtained a new PID/generation, registered all
four QUIC connections, and served anonymous/authenticated public HTTPS and a
fresh authenticated MCP session with a continuous SSE stream. An orchestrator
also observed public ChatGPT Cloudflare-to-OpenAI cross-connector file
verification (details below).

No Core, default OpenAI Tunnel, or Bridge lifecycle command was invoked. No
Cloudflare DNS, Tunnel, WAF, TLS, Token, or authentication configuration was
changed. No test, source, or deployment file was changed. This is live
operational evidence for the current working-tree candidate, not a release
claim.

## Scope and preflight

Read before action:

- `AGENTS.md`
- `docs/maintainers/workflow.md`
- `deployment/macos/README.md`, including the v0.25.0 independent Core and
  Connector lifecycle section
- Existing cutover and public Cloudflare acceptance reports dated
  2026-11-15 and 2026-11-16

The checkout had substantial pre-existing dirty/untracked state before this
operation. It was left untouched. The only file created for this task is this
report.

Read-only manifest inspection of
`~/Library/Application Support/ServerFS/service-manager/services.json`
confirmed:

- `http-origin`: `origin_owner=independent-core`, configured port `8765`.
- `cloudflare`: `connector_owner=independent-connector`, `core_port=8765`.
- Existing default `tunnel` and `agent-bridge` entries remained present.

Pre-restart `serverfs service core status` reported independent Core PID
`40516`, registration `enabled`; `serverfs service cloudflare status` reported
Cloudflare PID `40543` and Core PID `40516`, both `enabled`. `launchctl list`
reported all four labels active:

| Owner | launchd label | PID before | PID after |
|---|---|---:|---:|
| Independent HTTP Core | `com.ntlx.serverfs.http-origin` | 40516 | 40516 |
| Independent Cloudflare Connector | `com.ntlx.serverfs.cloudflare` | 40543 | 66419 |
| Default OpenAI Tunnel | `com.ntlx.serverfs.tunnel` | 1779 | 1779 |
| Default Agent Bridge | `com.ntlx.serverfs.bridge` | 1790 | 1790 |

`lsof -nP -iTCP:8765 -sTCP:LISTEN` showed only Core PID `40516` listening on
`127.0.0.1:8765` before and after.

Pre-restart authenticated local MCP initialize and `tools/list` both returned
HTTP 200 with 23 tools. The API key was read from the private HTTP-origin
environment file in process memory only; neither it nor session identifiers
were printed or recorded.

Pre-restart public checks used direct no-proxy HTTPS with normal TLS
verification. Anonymous `HEAD /mcp` returned HTTP 401 from public address
`172.67.130.247` (`ssl_verify_result=0`). Authenticated `initialize` and
`tools/list` each returned HTTP 200 with 23 tools on a fresh session. No
headers were spoofed and no WAF/DNS/TLS/Token setting was changed. The existing
Cloudflare log contained successful QUIC registrations for connIndex 0–3,
although connIndex 3 had recent retries caused by network timeouts and IPv6
`no route to host`; the final pre-restart connIndex 3 registration succeeded
at `2026-11-16T01:57:12Z`.

## Bounded action

Executed exactly once:

```bash
.venv/bin/serverfs service restart --component cloudflare
```

It exited `0` and printed `cloudflare: enabled`. No broad/group restart,
Core/Bridge/OpenAI lifecycle action, or second restart was attempted.

## Post-restart evidence

At `2026-11-16T02:10:58Z`, Core status reported PID `40516` and Cloudflare
status reported Cloudflare PID `66419`, Core PID `40516`; both registrations
were `enabled`. `launchctl list` showed all four labels active. The listener
remained `127.0.0.1:8765` owned by PID `40516`. The default service status
continued to report Tunnel PID `1779` and Bridge PID `1790`.

The new Connector process logged four successful QUIC registrations:

| connIndex | Registered at UTC | Protocol | Edge location |
|---:|---|---|---|
| 0 | 2026-11-16 02:10:53 | QUIC | lax12 |
| 1 | 2026-11-16 02:10:54 | QUIC | lax13 |
| 2 | 2026-11-16 02:10:55 | QUIC | lax01 |
| 3 | 2026-11-16 02:10:56 | QUIC | lax11 |

The public anonymous direct no-proxy HTTPS `HEAD /mcp` returned HTTP 401 with
normal TLS verification (`ssl_verify_result=0`) and a non-loopback Cloudflare
edge address. A fresh authenticated direct no-proxy public session returned
HTTP 200 for `initialize` and `tools/list` (23 tools). A single GET on that
session returned HTTP 200 and remained open for **75.018 seconds**, with 11
observed stream lines. The API key and session ID were not emitted or recorded.

**Orchestrator-observed external ChatGPT E2E:** through the public Cloudflare
`macbook-serverfs` connection, `list_workdirs` returned two read-write aliases.
The preserved OpenAI Tunnel connection independently returned the same two
aliases. Cloudflare created disposable
`codexsatellites/v0250-cf-restart-crossentry-20261116-nx72.txt` (71 bytes,
revision `v1:b59b9684c414c1ae`); OpenAI independently read the same 71 bytes
and revision; Cloudflare then successfully performed revision-guarded delete.
This is externally observed cross-connector evidence, separate from the local
curl/MCP probes.

## Commands and gates

Read-only commands/checks executed before and after the one restart:

```bash
.venv/bin/serverfs service status
.venv/bin/serverfs service core status
.venv/bin/serverfs service cloudflare status
launchctl list
lsof -nP -iTCP:8765 -sTCP:LISTEN
ps -p 40516,40543,1779,1790 -o pid=,ppid=,comm=,args=
curl --noproxy '*' --head https://macbook-serverfs.ntlx.top/mcp
```

Authenticated local and public MCP probes performed initialize and
`tools/list`; post-restart public verification additionally held one SSE GET
for more than 65 seconds. External ChatGPT cross-ingress CRUD evidence was
provided by the orchestrator. No tests, builds, source checks, or release
gates were run because this was an operational verification task with no
source changes.

## Limitations and notes

- Historical/pre-restart Connector logs showed intermittent QUIC dial timeouts
  and IPv6 route failures. After restart, all four connIndex values registered
  successfully within approximately four seconds. This acceptance proves the
  observed recovery and stream interval, not uninterrupted edge transport or
  network stability outside that interval.
- The default `service status` output included an old handover diagnostic with
  `ok=false` and detail `tunnel is enabled but no managed process became
  active`; its timestamp predates this operation. In the same status output,
  the actual OpenAI Tunnel and Bridge services were enabled at unchanged PIDs
  `1779` and `1790`; their launchd labels were active. No handover command was
  initiated in this acceptance.
- No rollback or recovery action was needed. No second restart was attempted.
- Existing unrelated dirty/untracked checkout state was not modified. The
  requested report is the only task-created tracked-path change; it remains
  uncommitted.
