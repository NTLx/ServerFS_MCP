# Phase 0B — Windows Named Pipe IPC and peer identity (measured 2026-10-04)

Status: **GATE PASS — byte-stream Named Pipe + measured client SID is frozen as the v0.11 Windows
Bridge IPC contract**, with three hard platform constraints added to `dev_plan_v0.11.md` §4.

Phase 0 experiment record. No product code changed. Harness: `%TEMP%\serverfs-phase0b\`
(`pipe_probe.py`, `imp_diag*.py`, `conc_probe.py`), throwaway, removed when Phase 0 closes.

Linux contract mirrored from `agent_bridge/src/serverfs_agent_bridge/protocol.py`: JSON lines,
`MAX_REQUEST_BYTES = MAX_RESPONSE_BYTES = 1_048_576`, `REQUEST_TOO_LARGE` / `INVALID_REQUEST` /
`RESPONSE_TOO_LARGE` coded behaviour, peer checked once per connection, fail closed.

Host: Windows 11 Pro 10.0.26200, CPython 3.13.3 x64, single interactive session (id 2), user SID
`S-1-5-21-1903659250-1886163475-1572399001-1001`. Server and client are always **separate real
processes**; a contending call that would block is reported as `BLOCKED`, never hidden.

## 1. Transport

| Row | Measurement | Verdict |
| --- | --- | --- |
| XFER-small | one JSON line + `\n`, reply line received | PASS |
| XFER-exactly-limit | 1 048 576-byte request including the newline is accepted (`"ok": true`, server saw `request_bytes=1048575`) | PASS |
| XFER-over-limit | 1 052 683-byte line → `REQUEST_TOO_LARGE`, connection then closes | PASS |
| XFER-partial-write | a 200 KB frame written in 4 KiB chunks reassembles | PASS |
| XFER-partial-write-64k | same frame in 65 536-byte chunks reassembles | PASS |
| XFER-coalesced | two frames written in **one** `WriteFile` arrive coalesced; both answered in order | PASS |
| XFER-malformed | `not json at all\n` → `INVALID_REQUEST`, connection survives | PASS |
| XFER-response-1mib | 1 048 531-byte response delivered complete | PASS |
| XFER-response-over | oversized response refused with `RESPONSE_TOO_LARGE` instead of truncating | INFO |
| NO-server | `CreateFileW` on a name with no instance → `2 ERROR_FILE_NOT_FOUND` in 0 ms | PASS |
| NO-accept | instance created but no `ConnectNamedPipe` → client `CreateFileW` **blocks indefinitely** | BLOCKED (by design; see §4) |
| EOL-pending | a frame with no newline produces no reply; the reader stays blocked in read | INFO |
| EOL-next-connection | an unterminated frame cannot poison the next connection | PASS |
| DEATH-server-at-accept / -after-reply / DEATH-client-mid-request | abrupt death on either side is observed as a read/write error, never a hang of the survivor | INFO |
| RESTART-same-name | a new server takes the same name after the previous one died | INFO |

Consequences for Phase B: byte mode + a **persistent frame buffer** is mandatory (coalescing is
normal, `XFER-coalesced`), the `MAX_*_BYTES` bounds and coded errors transfer 1:1, and a client
that sends no newline pins one server connection until it closes — so the server needs an idle
read timeout, which Linux gets from its asyncio reader.

## 2. Peer identity — measure then assert

| Row | Measurement |
| --- | --- |
| ID-before-read | `ImpersonateNamedPipeClient` **before the first read** fails: `1368 ERROR_CANT_IMPERSONATE_NAMED_PIPE` — "Impersonation of the pipe cannot be created before reading data from the named pipe". Confirmed in four independent runs. |
| ID-after-read | after ≥1 read: impersonation succeeds; `OpenThreadToken` + `GetTokenInformation(TokenUser)` returns `S-1-5-21-…-1001`, i.e. the **real** client user; `impersonation_level=2` (SecurityIdentification), `token_type=2`; `RevertToSelf` succeeds and no thread token remains (`token_present_after_revert=false`) |
| ID-after-read-other-thread | impersonation from a thread that did not perform the read also succeeds with the same SID — so the assertion may live on a worker thread, but only after that connection's first read |
| `GetNamedPipeClientProcessId` | returns the true client PID in every row above (cross-checked against the child PID) |
| `GetNamedPipeClientSessionId` | returns the true session id (2) |
| ID-REVERSE | the **client** side can also measure the server: `GetNamedPipeServerProcessId` → correct PID, `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)` + token → correct SID, `GetNamedPipeServerSessionId` → session |

Rule for Phase B, replacing the Linux "check at connect" ordering: **read the first frame → measure
→ assert SID equality → only then dispatch.** `PIPE_REJECT_REMOTE_CLIENTS` is set on creation
(measured: local clients unaffected). PID and session are diagnostics; SID equality is authoritative.
Nothing is ever inferred from the pipe name or from a claimed identity in the payload.

The measured level is SecurityIdentification, not Impersonation: the Bridge can read the peer SID but
cannot use the peer token for any object access. That is the narrower, desired capability.

## 3. Pipe DACL

| Row | Measurement | Verdict |
| --- | --- | --- |
| ACL-DEFAULT-SDDL | a pipe created through the default path carries `D:(A;;FR;;;WD)(A;;FR;;;AN)(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;<user>)` — **WellKnownDomain\Everyone and Anonymous get FILE_READ** | the default DACL is not safe; it must be replaced |
| ACL-EXPLICIT-ALLOW | `D:P(A;;GA;;;<user SID>)`: protected (no inheritance), the intended client connects, is served, and its SID is measured | PASS |
| ACL-EXPLICIT-DENY | `D:P(A;;GA;;;SY)(A;;GA;;;BA)`: the same local user is refused with `5 ERROR_ACCESS_DENIED` at `CreateFileW` | PASS — negative control proving the DACL is a real access boundary and the name is not |

So Phase B creates the pipe with an explicit `D:P(A;;GA;;;S-1-5-…-…)` descriptor (optionally + `SY`
for administrative diagnostics), never relying on the default DACL and never on the name.

## 4. Instance topology — the one thing that would have broken silently

| Row | Measurement |
| --- | --- |
| SEQ-per-instance | fresh instance per connection: 5/5 sequential clients served |
| SEQ-recycled-instance | one instance reused: `DisconnectNamedPipe` → `ConnectNamedPipe` returns `0`, then `535 ERROR_PIPE_CONNECTED` is normal on the next round; 3/3 served. Both shapes work; recycling matches the Linux socket-reuse model |
| CONC-one-server-thread | one serialized accept loop + 8 simultaneous clients → **5/8 answered** (6/8 in the first run); the losers fail immediately |
| CONC-pool1-retry1 | **1** listening instance + 6 simultaneous clients → 3/6 (4/6 in the first run); every failure is `231 ERROR_PIPE_NOT_CONNECTED` |
| CONC-pool1-retry100 | same, client retries the open with a 20 ms backoff → **6/6**, needing at most 2 retries |
| CONC-pool4-retry1 / -retry100 | 4 listening instances + 12 simultaneous clients → 12/12 immediately |
| CONC-pool-instances | one process holding a 6-instance pool that replenishes after each connection: 12/12 served |
| SQUAT-first-instance-flag | a second server creating an already-taken name with `FILE_FLAG_FIRST_PIPE_INSTANCE` → `5 ERROR_ACCESS_DENIED` (fail closed) |
| SQUAT-plain | the same second server without that flag **succeeds**, adding an instance to another process's name |
| SQUAT-detect | the client's measured `server_pid` identifies which of the competing servers it actually reached |

Rules Phase B must implement (this is the platform gap most likely to be missed):

1. A byte pipe serves at most as many simultaneous clients as there are **listening instances**.
   The Bridge keeps a small pool (≥2, sized by config) and replenishes an instance immediately
   after the connection is finished.
2. The MCP-side client must treat `109 ERROR_PIPE_BUSY` / `231 ERROR_PIPE_NOT_CONNECTED` /
   `2 ERROR_FILE_NOT_FOUND` as **retryable inside the existing request timeout** with a short
   bounded backoff, exactly as a Windows SMB/HTTP client does. Measured: without retry 3/6, with
   retry 6/6.
3. The Bridge creates its first instance with `FILE_FLAG_FIRST_PIPE_INSTANCE`, so a second Bridge
   process cannot silently join an existing name; it fails closed instead.
4. The MCP client additionally asserts the connected **server's** PID/SID (§2 ID-REVERSE), so a
   squatted or stale name is detected rather than trusted.
5. `NO-accept` proves a created-but-never-connected instance makes clients block: the Bridge must
   not report readiness until at least one instance is in `ConnectNamedPipe`.

## 5. Decision

`dev_plan_v0.11.md` §4 stands as designed, now with §4.5–§4.7 added: read-before-impersonate
ordering, explicit protected pipe DACL, listening-instance pool + retryable client connect,
`FILE_FLAG_FIRST_PIPE_INSTANCE`, and client-side server identity assertion.

## 6. Not verified

* A second real Windows account as a negative identity case: this host has one interactive user.
  The DACL negative control (§3) proves enforcement, and the SID equality check is exercised
  positively; a genuine cross-account row belongs to the Windows CI matrix (plan §15/Phase H).
* Low-integrity-level client: not exercised; mandatory labels only restrict, they never widen the
  DACL result, so it cannot weaken §3.
* Remote/network client against `PIPE_REJECT_REMOTE_CLIENTS`: single machine, no second logon
  session; local clients were confirmed unaffected.
* End-to-end Bridge RPC semantics (dispatch, approvals, task events) over the pipe: Phase B with
  FakeAdapter.
