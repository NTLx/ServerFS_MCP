# Phase 0C — Codex Windows transport (measured 2026-10-04)

Status: **OPEN DECISION FOR THE MAINTAINER.** Phase 0C disproved the plan's preferred transport
assumption and validated the fallback. Both viable shapes now cost something the plan said we would
keep, so the selection is a product decision, not an implementation detail. Recommendation at §5.

No model inference was executed. Only `initialize`, `initialized` and `model/list`.

Harness: `%TEMP%\serverfs-phase0c\` (`codex_probe.py`, `raw_probe.py`, `raw_probe2.py`,
`ws_probe.py`). Host: Windows 11 Pro 10.0.26200, Codex CLI 0.159.2, managed app-server daemon 0.160.0
running (`codex app-server daemon version`), CPython 3.12.10 and 3.13.3, `websockets==17.1`.

## 1. The Linux transport cannot be reused

| Measurement | Result |
| --- | --- |
| `socket.AF_UNIX` on CPython 3.13.3 (win32) | **absent** |
| `socket.AF_UNIX` on CPython 3.12.10 (win32) | **absent** |
| Consequence | `websockets.asyncio.client.unix_connect`, the only transport in `adapters/codex_transport.py:15,68-79`, cannot run on Windows at all |
| Daemon endpoint still exists | `daemon version` → `socketPath = %USERPROFILE%\.codex\app-server-control\app-server-control.sock`, a real 0-byte socket file |

Per `AGENTS.md`/plan we do not build a private AF_UNIX workaround (raw Winsock `AF_UNIX` via ctypes
was deliberately **not** attempted).

## 2. `codex app-server proxy` — preferred candidate, measured unsuitable as a JSON-RPC stdio endpoint

| Row | Measurement | Result |
| --- | --- | --- |
| PROXY-initialize | send `{"jsonrpc":"2.0","id":"initialize",...}\n` to `codex app-server proxy --sock <daemon socket>` | **no bytes at all** on stdout after 45 s; process stays alive |
| raw framing test | newline-delimited / no-newline / pretty-printed JSON, read as **raw bytes** (so a reply without a trailing newline would still be seen) | 0 bytes in all three cases |
| nature of the relay | send an HTTP `GET /rpc Upgrade: websocket` handshake instead | stdout returns `HTTP/1.1 101 Switching Protocols … x-codex-websocket-max-unfragmented-message-bytes: 16777216` |
| failure observability | `--sock` pointing at a nonexistent path | exits 1 immediately with `failed to connect to socket at … (os error 10061)` on stderr |
| debug logging | `RUST_LOG=debug CODEX_LOG=debug` with a JSON-RPC line | no output; the silence is protocol-level, not log-level |

Conclusion: the proxy is a **transparent byte relay**, exactly as its help text says
("Proxy stdio bytes to the running app-server control socket"). It does not terminate WebSocket
framing, so a client behind it must implement RFC 6455 itself — handshake, masked client frames,
fragmentation up to 16 MiB, ping/pong/close — over a subprocess pipe. The relay itself is reliable
(it delivered the 101 response and reports connect failures cleanly); the cost is entirely on our
side: a hand-written WebSocket client replacing the `websockets` dependency for this one transport.

So plan §10.1's premise "smallest change to CodexAdapter" is **false by measurement**.

## 3. `codex app-server --listen ws://127.0.0.1:<port>` — fallback candidate, measured working

| Row | Measurement | Result |
| --- | --- | --- |
| WS-start | listener comes up on Windows | PASS — but only after **24.4 s** (three independent runs: 24.36 / 24.37 / 24.38 s) |
| binding | `netstat -ano -p TCP` | `TCP 127.0.0.1:<port> 0.0.0.0:0 LISTENING` — loopback only; a non-loopback bind was deliberately **not** tested |
| WS-initialize | `websockets` client to `ws://127.0.0.1:<port>/rpc` | PASS; `userAgent = serverfs-phase0c/0.159.2 (Windows 10.0.26200; x86_64) …`, result keys `codexHome, platformFamily, platformOs, userAgent` |
| WS-model-list | `model/list {limit:200, includeHidden:false}` | PASS — 8 models, `nextCursor=false`: `gpt-6.1-sol, gpt-6-astra, gpt-6-sol, gpt-6-luna, gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna, gpt-5.5` |
| WS-path-root | handshake path | both `/rpc` and `/` are accepted |
| WS-subprotocol | `Sec-WebSocket-Protocol: codex.app-server.v1` offered | accepted, server negotiates none (response works regardless) |
| WS-auth-start | `--ws-auth capability-token --ws-token-file <path>` | starts; note `--ws-token-file` and `--ws-token-sha256` are **mutually exclusive** (the CLI errors out) |
| WS-auth-anon | client without credentials | **refused**: `HTTP 401` |
| WS-auth-token | `Authorization: Bearer <token>` | accepted |
| WS-auth-token | `Sec-WebSocket-Protocol: bearer.<token>` | refused with 401 — the accepted form is the bearer header |
| WS-shutdown | `TerminateProcess`/`terminate()` on the app-server | listener released (only `TIME_WAIT` entries remain) |

Version-skew question from the discovery report (§23.13) is answered for this shape: with a
Bridge-owned `app-server` child, the answering version is the **CLI version (0.159.2)**, not the
managed daemon's 0.160.0, and `_version_from_user_agent` parses it correctly.

## 4. What each candidate costs against the frozen plan

| Plan §10.1 claimed benefit | proxy + WS codec | Bridge-owned `--listen ws://127.0.0.1` |
| --- | --- | --- |
| no Python AF_UNIX requirement | satisfied | satisfied |
| no exposed TCP listener | satisfied | **not satisfied** — loopback only, capability-token auth, but a listener exists |
| provider-managed daemon retained | satisfied | **not satisfied** — the Bridge owns its own app-server child, contained in the Job Object per plan §9 |
| `thread/read` reconciliation across Bridge restart (strongest of the three runtimes) | satisfied | **weakened** — an in-flight turn dies with the Bridge-owned child, so Codex on Windows would have the same reconciliation floor as Claude/Qoder (§17 of the discovery report); thread resume by ID still works because `codexHome` state is shared |
| smallest change to `CodexAdapter` | **not satisfied** — new RFC 6455 client over stdio, fragmentation up to 16 MiB, ping/pong, close semantics | satisfied — `CodexConnection` already uses `websockets`; the change is `unix_connect` → `connect(uri)` plus a bearer header |
| startup latency | daemon already running | ~24 s before the listener answers; readiness waits must accommodate it |
| new secret handling | none | a capability token in a private 0600 file under the Bridge data home |

## 5. Recommendation (pending maintainer decision)

Recommend **`--listen ws://127.0.0.1:<ephemeral>` + `--ws-auth capability-token` + a
Bridge-owned, Job-Object-contained app-server child** for Windows, because:

1. it uses only provider-official flags and the existing `websockets` dependency, so the amount of
   invented protocol code is near zero (plan §10.1's "smallest change" criterion, measured);
2. the listener is loopback-only and refuses unauthenticated clients with 401 (both measured), and
   the token lives in the Bridge's private state directory with the §6 ACL rules;
3. the reconciliation loss is real but bounded: after a Bridge restart the Windows Codex runtime
   would be at exactly the same provable floor v0.11 already accepts for Claude and Qoder, and
   `thread/resume` + `thread/read` still work against shared `codexHome` state;
4. the alternative buys daemon continuity at the price of a hand-written WebSocket client — a
   larger, less reviewable security-relevant surface than a loopback listener with a token.

Rejected regardless of the choice: raw Winsock `AF_UNIX` via ctypes, reverse-engineering the daemon's
control protocol, and any non-loopback or unauthenticated WebSocket bind.

Explicitly **not verified** because it would change the user's running provider configuration:
`codex app-server daemon enable-remote-control`. If that officially exposes the *managed* daemon over
loopback WebSocket, it would combine both candidates' benefits and should be re-measured with the
maintainer's approval before the decision is final.

## 6. Consequences for the plan if the recommendation is accepted

- §10.1/§10.2 swap: loopback WebSocket becomes the primary Windows Codex transport, the stdio proxy
  is recorded as measured-unsuitable, and the daemon is never seized.
- §9 gains: the Bridge-owned `codex app-server` child must be inside the Job Object, with the
  capability token file covered by §6 private-state ACLs.
- §13 doctor: report the ephemeral loopback listener, token-file presence/permissions and the ~25 s
  startup latency without ever printing the token.
- New implementation constraint measured here: `--ws-token-file` and `--ws-token-sha256` cannot be
  combined; the client authenticates with `Authorization: Bearer <token>`.
