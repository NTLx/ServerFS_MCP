# ServerFS v0.11.0 Development Plan — Windows Native Agent Bridge

Status: Phase 0/A/B CLOSED · Phase C: C0, C0.7 and C1–C5 CLOSED (contract decision B implemented)

```text
0A PASS — LockFileEx
0B PASS — Named Pipe + SID
0C PASS — authenticated loopback Codex WebSocket
0D PASS — Qoder SDK
0E PASS — Claude SDK
0F PASS — Runtime Egress Proxy
A  PASS — portability foundation (root suite and Bridge suite run on Windows;
      Linux root + Linux Bridge gates green in CI)
B  PASS — Windows Named Pipe IPC, measured client-SID peer identity and owner-SID/DACL
      private state, with the Windows data home and a real two-process FakeAdapter E2E
C  C0  CLOSED — CONTRACT DECISION B (maintainer, 2026-10-05). No O(1) NTFS signal detects a
      same-tick same-size rewrite; measured and rejected in order: ChangeTime (no extra detection,
      costs rename stability), every other metadata field, and the per-file
      `FSCTL_READ_FILE_USN_DATA` USN (reachable without elevation, but static: 200/200 unchanged).
      The Windows revision is therefore frozen as a metadata-derived opaque optimistic-concurrency
      token, the same-tick same-size external-rewrite blind window is an accepted and documented
      product boundary, and edit/overwrite/delete must hold the restricted-share target across
      validate → commit (see docs/windows-phase-c-revision-correctness-2026-10-05.md).
      C0.7 error precedence is closed on both backends.
C  C1–C5 PASS (2026-10-06) — the writer lease has a Windows twin: a platform-neutral lease id
      (`slot:NN` keeps the legacy `01..16.lock`/`active/NN` layout byte-for-byte, `alias:<exact>`
      hashes so case folding cannot merge workdirs), an exclusive non-blocking full-range
      `LockFileEx` opened `GENERIC_READ`/`OPEN_EXISTING` on both sides and never created by the
      reader, every published mutation channel behind that lease and no read channel in front of it,
      `edit_text_file` reading its source from the held object inside one transaction, and a real
      Windows workspace-write task that completes over the pipe while an MCP mutation is refused,
      survives a `TerminateProcess` as recovery state, and is cleared by a restarted Bridge.
      Windows Bridge 249 passed / 50 skipped, Windows root 993 passed / 128 skipped, Linux root 1137
      passed / 33 skipped and Linux Bridge 230 passed in CI at code head `24a2830`; cargo green on
      both feature settings.
```

No Phase 0 gate or design decision is outstanding (§18). Phase D (native configuration and
lifecycle) is the next phase.
Baseline: v0.10.0 / current main
Primary target: Windows 11 x64 + local NTFS + native ServerFS
Runtime target: Codex + Claude Code + Qoder

## 1. Release goal

v0.11.0 adds the existing provider-neutral Agent Bridge capability to the Windows-native ServerFS deployment introduced in v0.10.0.

Target topology:

```text
ChatGPT
  -> OpenAI Secure MCP Tunnel
  -> native ServerFS stdio
  -> Windows local Agent IPC
  -> serverfs-agent-bridge
       |- Codex
       |- Claude Code
       \- Qoder
```

The release succeeds only if Windows gains the existing Agent semantics without weakening the v0.3-v0.9 contracts:

- structured asynchronous Agent tasks;
- workdir authorization;
- workspace-write exclusivity;
- approval/question interaction;
- cancellation;
- task and interaction timeouts;
- durable events/state/results;
- restart reconciliation;
- idempotent submission;
- result spooling;
- model discovery where supported;
- per-task model override;
- optional Jev advisory behavior;
- no generic shell/argv/env MCP surface.

v0.11 is a platform-portability release, not a new Agent orchestration architecture.

## 2. Frozen invariants

### 2.1 Public MCP surface

Keep the existing ten Agent tools unchanged:

- list_agent_runtimes
- list_agent_models
- submit_agent_task
- get_agent_task
- read_agent_task_events
- read_agent_task_result
- respond_agent_approval
- answer_agent_question
- send_agent_message
- cancel_agent_task

No Windows-specific Agent MCP tools.

### 2.2 Bridge RPC

Keep PROTOCOL_VERSION=1 and preserve the current request/response envelope, method names, coded errors, request IDs, size bounds and asynchronous task model.

The underlying local transport may differ by platform.

### 2.3 Provider-neutral core

Do not redesign the task model, status transitions, event envelope, execution manifest, idempotency, approvals/questions, interaction timeout, result spool format, SQLite persistence schema, reconciliation contract, model discovery orchestration, model override semantics or Jev authority.

### 2.4 Provider authority

ServerFS configures whether a runtime may execute, not the provider's persistent model/default/session configuration.

model=None continues to mean no ServerFS model override.

### 2.5 Linux compatibility

Linux Docker + UDS + SO_PEERCRED + flock + systemd behavior remains supported and regression-green.

Windows support must not force Linux deployments to migrate configuration, lock files, service definitions or deployment layout.

## 3. Core architectural decision

Introduce explicit platform seams only around the components proven non-portable:

```text
Agent Bridge core
|
|- Local IPC
|    |- Linux: Unix socket
|    \- Windows: Named Pipe
|
|- Peer identity
|    |- Linux: SO_PEERCRED UID/GID/PID
|    \- Windows: Pipe client SID + PID/session
|
|- Writer lease
|    |- Linux: flock
|    \- Windows: LockFileEx
|
|- Private-state authorization
|    |- Linux: UID + mode
|    \- Windows: owner SID + NTFS DACL
|
|- Process containment
|    |- Linux: systemd user cgroup
|    \- Windows: Job Object
|
\- Runtime transports
     |- Codex Unix transport
     \- Codex Windows transport
```

Everything above these seams remains provider-neutral.

Do not create parallel Windows implementations of service.py, store.py, task lifecycle or provider adapters.

## 4. Windows local IPC

### 4.1 Decision

Use Windows Named Pipes for ServerFS <-> Agent Bridge RPC.

Do not use loopback TCP as the primary trust boundary, CPython AF_UNIX, generic localhost HTTP, or a shared secret as a replacement for OS identity.

### 4.2 Endpoint

Use one user-scoped deterministic pipe name, conceptually:

```text
\\.\pipe\serverfs-agent-bridge-v1-<user-identity-hash>
```

The suffix should derive deterministically from the current Windows user's SID, not username text.

The name is disambiguation only, not authentication.

### 4.3 Framing

Keep the existing JSON-lines framing and current size limits:

```text
JSON request + newline
JSON response + newline
```

Prefer byte mode to avoid inventing a second protocol.

### 4.4 Authorization

Use two independent layers.

Layer 1: create the pipe with an explicit current-user DACL.

Layer 2: after connection, measure the peer:

1. obtain client PID;
2. obtain client session ID;
3. impersonate the connected pipe client;
4. obtain TokenUser;
5. compare the SID with the configured/expected ServerFS user SID;
6. revert impersonation immediately.

SID equality is authoritative. PID/session are evidence and diagnostics.

The rule remains: measure the real connecting identity and then assert it; never infer trust from the pipe name.

### 4.5 Measured ordering constraint (Phase 0B)

`ImpersonateNamedPipeClient` fails with `1368 ERROR_CANT_IMPERSONATE_NAMED_PIPE` until at least one
read has been performed on the connected instance. The Windows order is therefore:

```text
ConnectNamedPipe
-> read the first complete frame
-> impersonate, read TokenUser SID, RevertToSelf
-> assert SID equality            # fail closed here, before any dispatch
-> dispatch
```

Linux asserts at connection setup; Windows asserts between first read and dispatch. No request may
be dispatched before the assertion, and the frame is buffered, not executed, while identity is
measured. Impersonation may be performed on a worker thread that did not do the read (measured).
The measured level is SecurityIdentification: the Bridge can read the peer SID but cannot use the
peer token for object access, which is the narrower capability we want.

### 4.6 Instance pool, connect retry and single-owner name

A byte pipe serves at most as many simultaneous clients as there are listening instances. Measured:
one listening instance plus six simultaneous clients answered 3/6 (4/6 on the first run), every
failure being `231 ERROR_PIPE_NOT_CONNECTED`; with a bounded client-side retry the same run answered
6/6 needing at most 2 retries; four instances plus twelve clients answered 12/12, and a
replenishing six-instance pool served 12/12.

Therefore:

- the Bridge keeps a pool of listening instances (at least two, configurable) and replenishes an
  instance immediately after a connection finishes; both a fresh instance per connection and
  `DisconnectNamedPipe` + re-`ConnectNamedPipe` on the same instance are measured working;
- the MCP-side client treats `ERROR_PIPE_BUSY`, `ERROR_PIPE_NOT_CONNECTED` and
  `ERROR_FILE_NOT_FOUND` on connect as retryable inside the existing request timeout, with a short
  bounded backoff;
- the Bridge creates its first instance with `FILE_FLAG_FIRST_PIPE_INSTANCE`, so a second Bridge
  process cannot silently join an existing name and fails closed instead (measured
  `ERROR_ACCESS_DENIED`); without the flag a second process succeeds and steals connections;
- the client additionally asserts the connected server's real PID and SID
  (`GetNamedPipeServerProcessId` + process token, both measured working), so a squatted or stale
  name is detected rather than trusted;
- the Bridge must not report readiness until at least one instance is inside `ConnectNamedPipe`;
  a created-but-never-connected instance makes client opens block indefinitely (measured).

### 4.7 Pipe DACL and framing

The default pipe DACL is not safe: a pipe created through the default path carries
`(A;;FR;;;WD)(A;;FR;;;AN)`, i.e. Everyone and Anonymous get read access. The Bridge must create the
pipe with an explicit protected descriptor (`D:P(A;;GA;;;S-1-5-…)`), and this is enforced, not
cosmetic: with a DACL granting only `SY`/`BA`, the same local user is refused at
`CreateFileW` with `ERROR_ACCESS_DENIED` (measured negative control).

Framing transfers 1:1 (`MAX_REQUEST_BYTES` accepted exactly, oversized refused, ~1 MiB response
delivered complete, malformed line refused with `INVALID_REQUEST`), but byte mode coalesces writes:
two frames in one `WriteFile` arrive together, so the reader must keep a persistent frame buffer and
drain every complete line before waiting again. A client that sends no newline pins one connection
until it closes, so the Windows server needs an idle read timeout that Linux gets from asyncio.

## 5. Windows writer lease

### 5.1 Decision

Use native LockFileEx as the production Windows lease primitive.

Phase 0A confirmed this by measurement on real NTFS (2026-10-04): an **exclusive** `LockFileEx`
succeeds on a handle opened with `GENERIC_READ` only, so the documented `GENERIC_WRITE`
requirement is not actually needed and the Linux reader shape survives unchanged. Evidence and
the full matrix are in `docs/windows-phase-0a-lockfileex-2026-10-04.md`.

Do not use msvcrt.locking or a named mutex as the primary production lease. Phase 0A measured both
alternatives and kept neither: `msvcrt.locking` is the same underlying primitive (an
`msvcrt.locking` holder blocks a `LockFileEx` exclusive request with `ERROR_LOCK_VIOLATION`), and a
named mutex has no filesystem artifact, so it cannot express per-alias artifacts or
reader-never-creates. `CreateFileW(dwShareMode = 0)` handle-holding is a viable fallback but is
strictly worse for operability (nothing at all can open the artifact while it is held), and it is
not needed.

Required behavior remains equivalent to Linux:

```text
Bridge pre-creates lock artifact
-> workspace-write Agent holds exclusive lease for whole active task
-> ServerFS mutation opens existing artifact only
-> ServerFS tries same exclusive lock
-> busy => AGENT_WORKDIR_BUSY
```

### 5.2 Reader-never-creates invariant

The MCP/ServerFS mutation side must:

- open an existing lock artifact read-only;
- never create it;
- fail closed if lock infrastructure is absent or invalid.

### 5.3 Workdir identity

Do not restore Docker slot IDs as a Windows core concept.

Introduce an internal platform-neutral lease identifier.

Conceptually:

```text
Linux legacy:  slot:01
Windows native: alias:<exact alias>
```

Windows filesystem artifact names should use a deterministic collision-resistant encoding/hash of the exact alias so NTFS case folding cannot merge distinct ServerFS aliases such as Foo and foo.

Phase 0A measured both halves of that rule: `Foo.lock` and `foo.lock` in one directory are the same
NTFS file object, so alias text in the filename is rejected; `sha256` of the exact logical lease id
truncated to 40 hex characters gives 11 distinct artifacts for `Foo`/`foo`/`FOO`/`con`/`prn`/`nul`/
a 200-character alias/`中文 ` with a zero-width space/`foo bar`/`..`/a 260-character alias, with a
bounded 45-character name and independent concurrent leases.

Linux may preserve the existing NN.lock layout for backwards compatibility.

Phase C implementation: the key kind is one explicit per-config switch (`lease_key: slot | alias`,
default `slot`, never mixed inside one config) rather than a per-entry choice. The two sides derive
artifact names independently, so a deployment where only one of them carries slots would lock two
different files and still report success; one switch per deployment makes that unrepresentable, and
the native renderer (Phase D) writes `alias`.

### 5.4 Recovery guard

Keep existing semantics:

```text
live lease present
  -> workspace actively owned

no live lease + no guard
  -> available

no live lease + active guard
  -> WORKDIR_RECOVERY_REQUIRED
```

Guard contents remain provider-aware JSON and reconciliation remains authoritative.

### 5.5 Measured production shape (Phase 0A closed)

`docs/windows-phase-0a-lockfileex-2026-10-04.md` §4 freezes the implementation shape Phase C must
use:

1. Artifact name is derived from the exact logical lease id
   (`alias:<exact alias>` on Windows), never from alias text:
   `<sha256(lease_id)[:40]>.lock` inside the private `locks\` directory.
2. Both the Bridge and the ServerFS mutation reader acquire identically:
   `CreateFileW(access = GENERIC_READ, share = READ|WRITE|DELETE, disposition = OPEN_EXISTING)`
   followed by `LockFileEx(LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY)` over the full file
   range. `ERROR_LOCK_VIOLATION` → `WORKDIR_BUSY`; `ERROR_FILE_NOT_FOUND` → `AGENT_LOCK_UNAVAILABLE`;
   `ERROR_ACCESS_DENIED` on the open → `LOCK_PATH_UNSAFE`.
3. A conflicting `CreateFileW` never blocks — measured for every data-access mask, all opens
   returned in the same millisecond. The busy probe therefore cannot hang the mutation path.
4. Verification must happen on the same locking handle: `FILE_READ_ATTRIBUTES` alone cannot take
   any lock, so the reader cannot "check then lock" with two handles.
5. The OS reclaims the lock on handle close, on `os._exit()` and on `TerminateProcess`, while the
   persistent guard file survives — the crash→`WORKDIR_RECOVERY_REQUIRED` path is intact.
6. `LockFileEx` is OS-enforced, not advisory: content reads and writes by another process on a
   locked artifact fail. The primitive is therefore legal only on lease artifacts, never on a
   workdir file or any path a runtime may touch.
7. Rename-by-name and delete-by-name of a locked artifact are permitted (same as `flock`); after a
   delete the next reader open fails closed with `ERROR_FILE_NOT_FOUND`.
8. Platform difference, stated explicitly: on Windows-native deployment the ServerFS process and
   the Bridge run as the same login user, so reader-never-creates cannot be enforced by an identity
   split the way Phase E's container group bit plus read-only bind mount does. It is enforced by
   construction — `OPEN_EXISTING` only, no `CREATE` anywhere on the reader path, Bridge-side
   pre-creation, and `serverfs doctor` verification that the artifacts exist and are not reparse
   points.
9. Private-state ACLs must be set and verified per artifact with an explicit non-inherited DACL.
   Replacing a parent directory DACL on this profile also emptied the child artifact's inherited
   ACEs, which silently denied the reader's open. Inheritance is not a security boundary here.

Phase C implementation notes against this frozen shape (both are clarifications, neither changes the
acquisition):

- Item 2's error mapping is stated per process. The Bridge answers an unopenable or invalid artifact
  with its existing `LOCK_PATH_UNSAFE`; the ServerFS reader answers with the three MCP codes the
  Linux lease already produced (`AGENT_LOCK_UNAVAILABLE`, `WORKDIR_BUSY`, `WORKDIR_RECOVERY_REQUIRED`).
  They are the same condition seen from the two sides, and the reader keeps the frozen public
  vocabulary instead of gaining a new code.
- `LockFileEx` reads `Overlapped.Offset` for the range start even when the handle was opened without
  `FILE_FLAG_OVERLAPPED`, and a NULL `lpOverlapped` faults on this OS build (measured while
  implementing: `None` raised an access violation at offset `0x10`, a real zeroed `OVERLAPPED`
  succeeded, with and without `use_last_error`). Both backends pass a fresh zeroed structure per
  call. Evidence: `docs/windows-phase-c-revision-correctness-2026-10-05.md` §10.

## 6. Windows private state

Reuse the v0.10 Windows data-home convention.

Default:

```text
%LOCALAPPDATA%\ServerFS\agent-bridge\
```

with the existing SERVERFS_DATA_HOME override.

Suggested layout:

```text
agent-bridge\
  state\
    state.sqlite3
    results\
  locks\
  active\
  generated\
    bridge-config.json
```

### 6.1 Authorization

Do not translate POSIX mode bits numerically.

Introduce a small private-state security abstraction such as:

- ensure_private_directory()
- ensure_private_file()
- verify_private_directory()
- verify_private_file()

Linux keeps existing UID/mode semantics.

Windows uses owner SID + NTFS DACL semantics.

Windows tests must freeze:

- expected owner SID;
- allowed ACEs;
- inheritance behavior;
- treatment of Everyone / Users / Authenticated Users;
- reparse-point rejection where relevant.

Do not duplicate raw ACL logic independently across store.py, result_spool.py, recovery.py and transport code.

## 7. Native configuration

serverfs.toml becomes the single operator-facing source of truth for Windows native Agent deployment.

Suggested additive shape:

```toml
[agent]
enabled = false

[agent.codex]
enabled = false
codex_bin = "codex"

[agent.claude]
enabled = false
claude_bin = "claude"

[agent.qoder]
enabled = false
qoder_bin = "qodercli"

[[workdirs]]
alias = "project"
path = "D:\\project"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["codex", "claude", "qoder"]
```

Existing task timeout, interaction timeout, max-active-task, retention and optional Jev configuration should map to the frozen Bridge settings rather than creating duplicate semantics.

Do not require operators to manually maintain both serverfs.toml and bridge.json.

The Windows launcher may render a private internal Bridge JSON file from serverfs.toml. That generated JSON is an implementation artifact, not a second configuration source.

agent.enabled=false remains the default. An existing v0.10 native installation upgrades to v0.11 with the same filesystem-only surface until Agent delegation is explicitly enabled.

### 7.1 Agent Runtime Egress Proxy

Windows Agent runtimes need an explicit outbound-proxy capability. This is a separate trust domain from the v0.10 Tunnel / Control Plane proxy and MUST NOT be implemented by blindly inheriting or copying `SERVERFS_PROXY_*` into the Bridge or provider children.

The frozen design goals are:

- Agent runtime proxy is disabled by default.
- v0.11 supports an HTTP proxy endpoint for provider egress; HTTPS provider destinations use normal HTTP CONNECT semantics. SOCKS5 is out of scope.
- operator-facing non-secret enablement belongs in `serverfs.toml`;
- endpoint material is supplied from a dedicated Agent proxy namespace, not the Tunnel proxy namespace;
- the dedicated environment contract is `SERVERFS_AGENT_PROXY_URL` (required when enabled) plus optional `SERVERFS_AGENT_NO_PROXY`. These names are frozen by Phase 0F; no provider consumes them, so the product must map them downward (§7.2);
- `SERVERFS_AGENT_PROXY_URL` must not contain URL userinfo. Phase 0F measured the credential-safety question and answered it negatively: with userinfo present the provider child sends `Proxy-Authorization` itself, injected env variables reach tool code executed by the runtime (grandchild verified), and an unrelated same-user process can open the runtime process with `PROCESS_QUERY_INFORMATION|PROCESS_VM_READ`. So an authenticated upstream is not supported by environment injection in v0.11; the only acceptable future shape is a credentialless local forwarder/broker owned by ServerFS (measured technically viable);
- if the real deployment proxy requires authentication, v0.11 does not ship it. The approved shape for a later release is a ServerFS-owned credentialless local forwarder that chains to the authenticated upstream and keeps the credential in its own process state only. Do not ship naive credential injection merely to make connectivity work;
- the provider child receives only the proxy variables it actually needs, derived from the dedicated Agent proxy configuration;
- the Bridge/supervisor must not copy Tunnel / Control Plane proxy credentials into provider children;
- local control traffic must bypass egress proxy. The effective no-proxy set MUST include `127.0.0.1`, `localhost` and `::1`, so the selected Codex loopback WebSocket transport can never be routed through the egress proxy;
- provider runtime proxy injection must not alter machine-wide proxy settings, WinHTTP global proxy, registry state, provider persistent config, or the user's shell environment;
- proxy endpoint/credential values never appear in MCP output, normal logs, generated evidence, test fixtures, plan text or doctor output. Diagnostics report only redacted state such as configured/reachable/auth-mode;
- runtime-specific behavior is measured rather than assumed: Codex, Claude and Qoder may consume standard proxy environment variables differently, and SDK-side versus CLI-child network ownership must be verified in Phase 0F.

Frozen non-secret TOML shape (Phase 0F confirmed the per-runtime policy values):

```toml
[agent.proxy]
enabled = false
source = "env"

[agent.codex]
enabled = false
codex_bin = "codex"
use_proxy = true

[agent.claude]
enabled = false
claude_bin = "claude"
use_proxy = false

[agent.qoder]
enabled = false
qoder_bin = "qodercli"
use_proxy = false
```

The booleans above are per-runtime routing policy, not proxy secrets. The endpoint itself remains outside TOML. `use_proxy = true` for Codex is a measured requirement on WorkPC (`api.openai.com` and `chatgpt.com` time out direct); Qoder's provider endpoint is reachable directly here and Claude's requirement is not yet measurable without a real turn, so both stay `false` by default and per-workdir/per-runtime policy decides.

### 7.2 Frozen proxy mapping (Phase 0F closed)

Evidence: `docs/windows-phase-0f-runtime-egress-proxy-2026-10-04.md`.

Parse-time validation of the dedicated namespace:

- `SERVERFS_AGENT_PROXY_URL` must be an absolute `http://` (or `https://`) URL with host and port;
  userinfo is rejected at parse time, not warned about;
- SOCKS5 and any non-HTTP scheme are rejected (out of scope by §7.1);
- the value is never written into `serverfs.toml`, generated Bridge config, logs, MCP results or
  evidence; doctor reports only `enabled / source / auth mode / per-runtime reachable / local bypass`.

Per-child environment mapping (nothing is bulk-inherited):

| Runtime | Network-owning process | Variables the child actually needs (measured) | Mechanism |
| --- | --- | --- | --- |
| Codex | the Bridge-spawned `codex app-server` child | `HTTPS_PROXY` + merged `NO_PROXY` | explicit env dict passed by the Bridge when it spawns the child |
| Claude | the `claude.exe` child (the SDK↔CLI channel is stdio; the SDK makes no provider connection during connect/control) | `HTTPS_PROXY` + merged `NO_PROXY` (consumption by the CLI itself is not yet observable without a turn) | `ClaudeAgentOptions.env` |
| Qoder | the `qodercli.exe` child — verified by attributing observer tunnel requests to that process, in both parent-env and child-env cases | `HTTPS_PROXY` + merged `NO_PROXY` | `QoderAgentOptions.env`; `env={NAME: None}` additionally removes inherited names |

Case behaviour is measured, not assumed: the installed Codex build accepts both `HTTPS_PROXY` and
`https_proxy` (and `ALL_PROXY`/`all_proxy`), and `HTTP_PROXY` alone is insufficient for HTTPS
destinations. The canonical product form is upper case `HTTPS_PROXY` + `NO_PROXY`, and nothing else is
injected.

Environment inheritance is the hard security rule this experiment produced:

1. Both SDKs build the provider child environment by copying the **whole** Bridge environment
   (`os.environ`, minus one internal marker) and then layering `options.env` on top. A
   Tunnel-namespace variable planted in the Bridge process was measured present in the child for both
   SDKs.
2. Claude's SDK offers no way to unset an inherited variable; Qoder's does (`None`). Therefore
   prevention must happen in the Bridge, not in the child mapping.
3. Consequently: the Bridge process environment MUST NOT contain Tunnel / Control Plane credentials,
   and MUST NOT contain any proxy variable. The native supervisor scrubs both before starting the
   Bridge (extending the v0.10 `native_tunnel` env-stripping discipline), and per-runtime proxy policy
   is applied strictly downward into the child environment.
4. `SERVERFS_AGENT_PROXY_*` names are not forwarded to providers at all — they exist only to be mapped.
   Measured: providers do not consume those names, and setting only the Tunnel namespace leaves Codex
   health failing, which proves the two domains are independent.

Local bypass rule (defense in depth, both belts required):

- the effective child `NO_PROXY` is the operator value **merged with** `127.0.0.1`, `localhost`, `::1`;
  an operator value can add entries but can never remove a mandatory one, and an empty operator value
  must not disable the bypass (measured: empty `NO_PROXY` re-enables proxying of loopback);
- the Bridge's own Codex control client keeps `proxy=None` on the WebSocket connection, because
  `websockets` was measured to route even `ws://127.0.0.1` through a configured proxy when `NO_PROXY`
  does not cover it;
- Codex genuinely honours `NO_PROXY`: excluding the provider domains it had just requested made the
  observer see zero tunnel requests and the provider health check fail again.


### 7.3 Authenticated proxy broker — OUT OF SCOPE for v0.11 (maintainer decision 2026-10-05)

v0.11 supports exactly two proxy shapes:

- a credentialless HTTP runtime egress proxy, configured through §7.1 and mapped by §7.2; and
- an operator- or externally supplied credentialless local broker that the Agent proxy endpoint simply
  points at. ServerFS treats it as an ordinary credentialless HTTP endpoint and owns nothing of it.

ServerFS v0.11 does **not** implement an authenticated-upstream credential broker. Phase 0F proved an
environment-injected proxy credential is not a security boundary: the provider child sees it, an
agent-executed grandchild inherits it, and a same-user process can inspect the provider process. A real
broker would therefore be a separate purpose-built trust component, which is a bigger design than this
release can carry honestly.

Frozen for v0.11:

- no upstream proxy username or password is stored by ServerFS anywhere;
- no proxy credential is injected into any Bridge or provider environment;
- no broker listener, process or credential store is added, so the Job Object, private-state ACL and
  secret-lifecycle surfaces do not grow;
- if a later deployment needs an authenticated upstream, the shape is
  `provider runtime -> credentialless local broker -> authenticated upstream proxy`, designed and
  reviewed as its own version.

Release documentation may state only: `credentialless Agent Runtime HTTP proxy supported`. It must not
claim native authenticated proxy support.

### 7.4 Jev advisory egress (maintainer decision 2026-10-05)

Jev may reuse the same operator-facing source — `SERVERFS_AGENT_PROXY_URL` and
`SERVERFS_AGENT_NO_PROXY` — but not the same mechanism. Providers are given a per-child environment;
Jev is an HTTP client inside the Bridge process, so:

- the Bridge parses the dedicated Agent proxy configuration and passes the endpoint explicitly into
  the Jev HTTP client construction;
- the endpoint exists only in that in-memory client configuration: it is never written back to
  `os.environ`, never handed to a provider SDK, never rendered into generated Bridge config, never
  logged and never returned through MCP;
- the hard rule still stands that the **Bridge process environment MUST remain proxy-free**. Setting
  `HTTPS_PROXY`, `HTTP_PROXY` or `ALL_PROXY` in the Bridge environment just to make Jev work is
  forbidden, because §7.2 measured that the provider SDKs inherit that environment wholesale;
- if the installed `typesafe-sdk`/Jev client exposes no explicit proxy parameter, Jev stays direct under
  its existing fail-open semantics. The dependency is not patched, wrapped in a monkeypatch or
  worked around in this release.

Jev stays optional, advisory and fail-open: a Jev network failure must never block an Agent task, and
Jev authority is unchanged. Implementation belongs to Phase D, not Phase A.

## 8. Agent tool portability fixes

### 8.1 Remove demonstrated Linux import-time failures

Fix only the proven portability defects:

- module-level os.O_DIRECTORY assumptions;
- unsafe os.geteuid() test predicates;
- direct fcntl imports on portable import paths.

Linux behavior must remain unchanged.

### 8.2 CWD validation

Replace direct Agent tool dependence on Linux fdio traversal with the backend-neutral WorkdirSession abstraction.

relative_cwd remains a ServerFS virtual relative path using "/" separators on every platform.

Never expose Windows drive paths, UNC paths, backslashes or native host paths through the public Agent MCP contract.

### 8.3 Runtime imports

Make real runtime adapters lazy/conditional.

Enabling codex must not require importing claude-agent-sdk or qoder-agent-sdk; the same rule applies symmetrically.

## 9. Windows process lifecycle

Use Windows Job Objects for Bridge-owned provider process containment.

Use JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE or an equivalent proven configuration so abnormal supervisor/Bridge termination does not strand Bridge-owned provider children.

Do not seize control of an independently managed Codex daemon.

### 9.1 Native launch model

Keep the normal operator entry point:

```text
serverfs tunnel --config serverfs.toml ...
```

When Agent delegation is disabled, behavior is identical to v0.10.

When enabled, the native supervisor should:

```text
load config
-> render private Bridge config
-> start Bridge
-> place Bridge-owned process tree in Job Object
-> wait for authenticated Bridge readiness
-> start native ServerFS stdio child
-> tunnel serves MCP
```

Shutdown:

```text
stop accepting new Agent work
-> request Bridge graceful shutdown
-> bounded wait
-> close Job Object as final containment
```

Existing tunnel API-key and proxy secret isolation remains unchanged.

The Bridge environment must not receive Tunnel / Control Plane secrets or tunnel-only proxy credentials. Provider-native auth/config required by Codex/Claude/Qoder remains available according to the runtime adapter contract.

Agent egress proxy is the one deliberate network-environment exception: when a runtime has `use_proxy=true`, the supervisor/adapter injects only the dedicated Agent proxy variables frozen in §7.2 into that runtime's network-owning process. It must not bulk-forward the parent process proxy environment. The effective child environment must force local control endpoints (`127.0.0.1`, `localhost`, `::1`) into no-proxy so Codex's authenticated loopback WebSocket remains local.

Phase 0F measured why "must not receive Tunnel secrets" has to be enforced one level earlier than this paragraph assumed: both provider SDKs build the CLI child environment by inheriting the entire Bridge environment, and Claude's SDK cannot unset an inherited variable. So the Bridge environment itself must be free of Tunnel / Control Plane credentials and of any proxy variable — scrubbing happens when the supervisor starts the Bridge, and per-runtime policy is applied only downward. See §7.2.

## 10. Codex runtime

Codex is the first runtime implementation track because the installed Windows CLI and app-server schema already match the existing adapter at the protocol level.

### 10.1 Preferred Windows transport — measured, assumption disproved

The plan preferred:

```text
codex app-server proxy
```

against the managed daemon, on the assumption that it preserves the bidirectional JSON-RPC stream
with the smallest change to `CodexAdapter`.

Phase 0C measured that assumption to be false (full evidence in
`docs/windows-phase-0c-codex-transport-2026-10-04.md`): the proxy is a **transparent byte relay**,
not a stdio JSON-RPC endpoint. Newline-delimited JSON-RPC gets no reply at all, while an HTTP
WebSocket upgrade sent through it returns `101 Switching Protocols` from the daemon. Using it would
require hand-writing an RFC 6455 client (masked frames, fragmentation up to 16 MiB, ping/pong/close)
over a subprocess pipe, replacing the `websockets` dependency for this one transport. The relay
itself works and its failures are observable; the cost is entirely invented client code.

Python cannot reuse the Linux transport directly either: `socket.AF_UNIX` is absent on CPython
3.12.10 and 3.13.3 on this OS build. A raw Winsock `AF_UNIX` shim was deliberately not attempted —
that is the forbidden private workaround.

### 10.2 Selected Windows transport — maintainer decision

Maintainer decision (2026-10-04): select the measured loopback WebSocket transport:

```text
codex app-server --listen ws://127.0.0.1:<ephemeral> --ws-auth capability-token --ws-token-file <private path>
```

- loopback-only bind confirmed by `netstat`; a non-loopback bind was deliberately not tested;
- unauthenticated clients are refused with HTTP 401; `Authorization: Bearer <token>` is the accepted
  credential form (`Sec-WebSocket-Protocol: bearer.<token>` is refused);
- `initialize` and `model/list` complete over `websockets` with no new protocol code;
- the listener only answers after ~24 s, so readiness waiting must accommodate that;
- `--ws-token-file` and `--ws-token-sha256` are mutually exclusive;
- the answering `userAgent` version is the CLI version (0.159.2 here), not the managed daemon's.

This trade-off is accepted deliberately. The Bridge owns its own `codex app-server` child (inside
the Job Object per §9) instead of attaching to the provider-managed daemon, so an in-flight Windows
Codex turn does not survive a Bridge restart. Windows Codex therefore uses the same conservative
reconciliation floor as Windows Claude/Qoder, while Linux Codex keeps the stronger daemon-backed
`thread/read` proof. Thread resume by ID still works because the shared `codexHome` state is the
same. This platform-specific recovery difference must be explicit in runtime capability/evidence;
it must not be hidden behind a stronger provider-neutral claim.

The alternative `app-server proxy` is rejected for production because it would require ServerFS to
own a new RFC 6455 implementation over subprocess pipes solely to preserve the managed daemon. That
complexity is not justified by the recovery benefit when the official loopback WebSocket transport
already works with the existing `websockets` stack. The managed daemon's remote-control setting is
also not part of this design: it is a separate persisted provider feature, still leaves the daemon's
local control path Unix-socket based, and changing it would mutate/restart user-managed Codex state
without solving the CPython AF_UNIX boundary.

Security/lifecycle requirements for the selected transport are frozen as follows:

- bind only `127.0.0.1` on an ephemeral port; never `0.0.0.0`, LAN, or a fixed public port;
- require `--ws-auth capability-token`; anonymous connections must remain rejected;
- generate a fresh high-entropy token for each Bridge-owned Codex child start;
- store the token only in a private generated file under the Windows Agent Bridge state area, outside
  every workdir, protected by the Windows private-state ACL contract from §6;
- pass the token file with `--ws-token-file`; do not put the token value in config, command-line
  arguments, normal logs, audit records, MCP results, or persistent provider settings;
- retain the token in Bridge memory only as long as needed for the authenticated WebSocket session
  and delete the generated token file when the child is torn down;
- use a bounded readiness wait sized for the measured ~24 s startup rather than assuming immediate
  availability;
- place the Bridge-owned `codex app-server` process in the Windows Job Object; do not place the
  provider-managed daemon in that Job Object;
- do not seize, stop, restart, reconfigure, or enable remote control on the user's managed daemon;
- keep Linux Codex transport unchanged.

Never: seize the managed daemon, bind non-loopback, run without authentication, reverse-engineer the
daemon control protocol, or implement a private Winsock AF_UNIX workaround.

### 10.3 Protocol drift

The installed schema contains newer approval variants.

Unknown provider-native decisions must not silently degrade to decline or another known decision.

Unknown variants must be explicitly unsupported/omitted/rejected.

### 10.4 Windows readiness

serverfs doctor may report provider-supported read-only Codex Windows sandbox readiness.

Do not auto-run sandbox setup during normal startup.

## 11. Qoder runtime

Qoder is the second runtime track.

Phase 0D measured this to be true on Windows: `qoder-agent-sdk==1.0.15` (the frozen pin) installs on
Windows, exports every name `adapters/qoder.py` imports, accepts every option the adapter sets, and
its real CLI transport connects, answers a control request, interrupts and disconnects cleanly
against the native `qodercli.EXE` — with no model inference. `get_available_models()` returned 17
models, and the catalog carries per-model `isFree`/`priceFactor`/`originalPriceFactor`, so cost
status is read live and never hardcoded. Evidence:
`docs/windows-phase-0d-0e-agent-sdks-2026-10-04.md` §1.

qoder-agent-sdk publishes a Windows x64 distribution. Phase 0D must first install the existing pinned SDK into the dedicated Bridge environment and verify import/API compatibility before product integration.

Preserve:

- native Qoder CLI login/configuration;
- explicit cli_path;
- setting_sources;
- approval mapping;
- AskUserQuestion;
- session resume;
- interrupt;
- provider-native model discovery;
- live_steer=false.

Do not carry over any historical claim that Qwen3.8-Flash is free.

Live validation must enumerate current model IDs first and then explicitly choose a model for smoke testing.

## 12. Claude runtime

Claude remains an intended v0.11 runtime.

The absence of a PyPI Windows wheel alone is not sufficient evidence to remove it from scope.

Phase 0E measured the real answer, and it is more specific than the discovery report assumed: the
**frozen pin** `claude-agent-sdk==0.2.156` does publish a Windows `win_amd64` wheel, Windows wheel
availability across recent releases is intermittent (`0.2.156` yes, `0.2.157` no, `0.2.158/159` yes,
`0.2.160`-`0.2.163` no), and **every** release publishes an sdist that builds on Windows without a
compiler because the runtime path is the subprocess-over-stdio transport against a native CLI. With
the pin installed from that normal resolution path, the official SDK connects, answers a control
request, interrupts and disconnects cleanly against the existing `claude.EXE`, and all names the
adapter imports exist in `claude_agent_sdk` and `claude_agent_sdk.types`. No model inference was run.
Evidence: `docs/windows-phase-0d-0e-agent-sdks-2026-10-04.md` §2-§3.

Consequence for the upgrade clause below: upgrading `claude-agent-sdk` to a release with no Windows
wheel is allowed only after re-checking that distribution matrix and re-verifying the sdist path on
Windows; "the current upstream works" is not by itself Windows evidence.

Phase 0E must install the pinned/current Python SDK through its normal Windows resolution path in an isolated Bridge environment and point it explicitly at the already-installed native claude.exe.

Verify:

- ClaudeSDKClient;
- ClaudeAgentOptions;
- can_use_tool;
- interrupt;
- resume/session IDs;
- setting_sources;
- PermissionResultAllow/Deny.

If the frozen SDK pin fails but a current upstream SDK succeeds, a dependency upgrade is allowed only after reviewing the change and rerunning Linux adapter tests.

Do not reverse-engineer private Claude daemon/session protocols to force Windows support.

If the official Agent SDK path is genuinely unusable on Windows after the sdist/native-CLI experiment, stop the Claude track and record the upstream blocker before reducing release scope.

## 13. serverfs doctor

When Agent is enabled, doctor should add non-mutating checks for:

- Agent Bridge package;
- Agent configuration parse;
- private state security;
- Named Pipe capability;
- Bridge reachability;
- peer identity verification.

Per runtime:

Codex:
- executable/version;
- provider-supported read-only auth status;
- app-server/transport availability;
- Windows sandbox readiness where safely queryable.

Claude:
- executable/version;
- Agent SDK installed/importable;
- provider-supported read-only auth status.

Qoder:
- executable/version;
- Agent SDK installed/importable;
- provider-supported read-only auth status.

Doctor must never trigger model inference, login/logout, provider setting mutation, credential output, account identifier output, or normal host-root disclosure.

## 14. Packaging

Keep agent_bridge as a separate package.

Do not merge it into serverfs-mcp merely for Windows.

Target release assets:

```text
serverfs_mcp-0.11.0-...
serverfs_windows_native-0.11.0-...
serverfs_agent_bridge-0.11.0-...
```

Windows-specific dependencies such as pywin32 should be declared explicitly and conditionally rather than relying on unrelated transitive installation.

Phase 0D/0E measured exactly why: both probe environments pulled `pywin32==312` transitively through
`mcp`, so a filesystem-only or Bridge install that happened to receive it must not be treated as the
contract. Declare it, do not inherit it. The same experiment also measured the practical size
constraint: the platform wheels are ~98 MB (Qoder) and ~105 MB (Claude) and the host CDN throughput
made them unreliable, while the sdists (143 KB / 347 KB) installed cleanly. Packaging and CI must
therefore treat the Windows wheel as the intended artifact but keep the sdist path verified.

A sdist-installed SDK carries **no bundled provider CLI** (measured: `qoder_agent_sdk` 41 files with
no executable; `claude_agent_sdk` `_bundled/` holding only a `.gitignore`). Its transport falls back to
`shutil.which(...)` for a native `.exe`, and the Claude resolver explicitly refuses an npm
`claude.cmd` shim. Phase H must therefore decide per runtime: ship the platform wheel that bundles the
CLI, or ship pure Python and require an explicit native CLI plus the `cli_path` the adapters already
pass. Both shapes are measured working; the second is what the current adapters assume.

When Agent mode is disabled, filesystem-only ServerFS must not require provider SDKs.

## 15. Development phases

### Phase 0 — decisive experiments

No product architecture changes until these experiments finish.

#### Phase 0A — LockFileEx

On real NTFS prove:

- Bridge-created lock file;
- MCP reader opens existing artifact with read access only;
- exclusive conflict across processes;
- second-handle semantics;
- abnormal owner-process death releases lock;
- MCP reader never creates lock artifact;
- correct share-mode combination;
- alias-derived artifact naming.

Gate: freeze LockFileEx as the Windows writer-lease primitive.

Status: **CLOSED — gate PASS** (2026-10-04, WorkPC, Windows 11 Pro 10.0.26200, local NTFS `C:`).
PR: https://github.com/NTLx/ServerFS_MCP/pull/32 (Phase 0 branch `v0.11-phase0-experiments`)

Implemented:
Nothing in product code, by design. One throwaway `ctypes` harness under
`%TEMP%\serverfs-phase0a\` (holder/contender child processes, per-stage Win32 error reporting,
hard deadline so a blocking call is reported as `BLOCKED@<stage>` instead of hanging). Kept outside
the repository (under `%TEMP%`) and discarded once this Phase 0 review concludes.

Measured:
Every matrix item passed. Exclusive `LockFileEx` succeeds on a `GENERIC_READ` handle, including on
an artifact whose DACL grants the identity read only (and `GENERIC_WRITE` is genuinely denied on
that artifact); contention is bidirectional and returns `ERROR_LOCK_VIOLATION` in the same
millisecond; a conflicting `CreateFileW` never blocks for any access mask; a second independent
handle in the same process conflicts, matching `flock` and `agent_bridge/tests/test_leases.py`;
the lock is reclaimed by `CloseHandle`, `os._exit()` and `TerminateProcess`; the locking handle can
read `FILE_ATTRIBUTE_TAG_INFO`/`FILE_ID_INFO` for validation; `msvcrt.locking` is the same
primitive; a named mutex abandons on owner death but has no filesystem artifact;
`dwShareMode = 0` works as a fallback; `Foo.lock`/`foo.lock` collide while `sha256(alias)` names do
not. Full evidence and the Phase C production shape are in
`docs/windows-phase-0a-lockfileex-2026-10-04.md`; the plan amendments it forced are §5.1, §5.3 and
the new §5.5.

Tests:
No new automated test in Phase 0 (experiment, not product behaviour). The contract this
experiment validates is already expressed as Linux regression tests
(`tests/test_agent_leases.py`, `agent_bridge/tests/test_leases.py`); Phase C adds the Windows
`LockFileEx` equivalents for every row of §5.5, including reader-never-creates, busy, crash→guard,
and the `Foo`/`foo` independent-lease case.

Residual, recorded in the evidence doc §5: file-level reparse rejection needs
`SeCreateSymbolicLinkPrivilege` or Developer Mode (only a directory reparse point was measurable
here); cross-account/cross-integrity contention was not measurable on this single-user host and
moves to Phase 0B, which can construct a restricted-token client.

#### Phase 0B — Named Pipe security

Prove:

- byte-stream JSON-lines round trip;
- 1 MiB request/response boundaries;
- multiple sequential/concurrent clients;
- client PID;
- client session ID;
- client SID via impersonation;
- explicit current-user ACL;
- stale server/crash cleanup;
- practical negative identity cases fail closed.

Gate: freeze the Windows IPC + peer identity contract.

Status: **CLOSED — gate PASS** (2026-10-04, WorkPC, Windows 11 Pro 10.0.26200).
PR: https://github.com/NTLx/ServerFS_MCP/pull/32 (Phase 0 branch `v0.11-phase0-experiments`)

Implemented:
Nothing in product code. Throwaway `ctypes` pipe harness under `%TEMP%\serverfs-phase0b\` with real
separate server/client processes, per-stage error reporting and hard deadlines, plus two follow-ups
that isolated the impersonation ordering (`imp_diag3`) and the listening-instance/retry behaviour
(`conc_probe`).

Measured:
Byte-stream JSON-lines framing reproduces the Linux contract exactly, including the exact 1 MiB
boundary, `REQUEST_TOO_LARGE`, `INVALID_REQUEST` and a complete ~1 MiB response. Peer identity is
real and measurable: client PID, session and `TokenUser` SID, `RevertToSelf` clean afterwards, and
the client can also measure the connected server's PID/SID. Two constraints were proven rather than
assumed: impersonation is impossible before the first read (`1368`), so assertion moves between
"first frame read" and "dispatch"; and a byte pipe only serves as many simultaneous clients as
there are listening instances (4/6 without retry, 6/6 with a bounded retry), so the Bridge needs an
instance pool and the MCP client needs retryable connect. The default pipe DACL grants Everyone and
Anonymous read access, so the explicit protected DACL is mandatory and its enforcement was proven
with a real negative control. `FILE_FLAG_FIRST_PIPE_INSTANCE` fails closed on a taken name.
Full matrix in `docs/windows-phase-0b-namedpipe-identity-2026-10-04.md`; the plan text it forced is
the new §4.5-§4.7.

Tests:
No new automated test in Phase 0. Phase B implements the pipe server/client and its tests must cover
each measured row: framing sizes, coalesced and partial writes, malformed frame, idle client,
either-side abrupt death, SID assertion (including the read-before-impersonate ordering), explicit
DACL allow/deny, instance pool plus connect retry, and first-instance-name fail-closed.

Residual, recorded in the evidence doc §6: no second real Windows account on this host (the DACL
negative control stands in; a cross-account row belongs to the Windows CI matrix), no low-integrity
client, and no remote/network client against `PIPE_REJECT_REMOTE_CLIENTS`.

#### Phase 0C — Codex transport

Without model inference, exercise through candidate Windows transports:

- initialize;
- initialized;
- model/list.

Test codex app-server proxy first.

Test loopback WebSocket only if proxy is unsuitable.

Gate: select exactly one production Windows Codex transport.

Status: **CLOSED — gate PASS; maintainer selected authenticated loopback WebSocket** (2026-10-04).
PR: https://github.com/NTLx/ServerFS_MCP/pull/32 (Phase 0 branch `v0.11-phase0-experiments`)

Implemented:
Nothing in product code. Throwaway probes under `%TEMP%\serverfs-phase0c\`: stdio framing attempts
against `app-server proxy` with raw-byte reads, a WebSocket-upgrade probe to identify the relay's
nature, and a full `--listen ws://127.0.0.1` session (initialize/initialized/model/list) including
capability-token auth positive and negative cases and `netstat` bind verification. No inference.

Measured:
`socket.AF_UNIX` is absent in CPython 3.12.10 and 3.13.3, so the Linux transport cannot be reused.
`app-server proxy` is a transparent byte relay: JSON-RPC lines produce zero bytes, a WebSocket
handshake gets a real `101` back from the daemon, and a missing socket exits 1 with a clear error.
`--listen ws://127.0.0.1:<ephemeral>` works end to end (initialize `userAgent` 0.159.2, model/list
8 models, both `/rpc` and `/` accepted, no subprotocol required), binds loopback only, refuses
anonymous clients with 401, accepts `Authorization: Bearer <token>`, refuses the
`Sec-WebSocket-Protocol: bearer.<token>` form, rejects `--ws-token-file` together with
`--ws-token-sha256`, takes ~24 s before it answers, and releases the port on terminate.

Gate decision: use the official authenticated loopback WebSocket listener described in §10.2.
`app-server proxy` is rejected because preserving the managed daemon would require a ServerFS-owned
RFC 6455 client over subprocess pipes; that extra protocol implementation is not justified. The
accepted cost is weaker Windows in-flight Codex recovery across a Bridge crash, explicitly surfaced
as a platform capability difference. Managed-daemon remote control is out of the v0.11 local
transport design and does not need a Phase 0 mutation experiment.

Not verified, by design: non-loopback bind behaviour (it is forbidden and therefore need not be
probed), and real approval server→client request round-trips (needs a turn, so remains Phase E).

#### Phase 0D — Qoder SDK

In an isolated Bridge environment prove without inference:

- install;
- import;
- version;
- CLI resolution;
- auth helper;
- model catalog API.

Status: **CLOSED — gate PASS** (2026-10-04).
PR: https://github.com/NTLx/ServerFS_MCP/pull/32 (Phase 0 branch `v0.11-phase0-experiments`)

Implemented: no product code; isolated `uv` venv on Python 3.12.10 under `%TEMP%\serverfs-phase0d\`.

Measured: pinned `qoder-agent-sdk==1.0.15` installs on Windows (the sdist path completed; the
`win_amd64` wheel is published for every 1.0.8-1.0.15 release but could not be downloaded reliably on
this host), all 11 adapter imports exist, `QoderAgentOptions` accepts `auth/cwd/cli_path/setting_sources/resume/continue_conversation/session_id/permission_mode/can_use_tool/model`, `qodercli_auth()`
returns against the existing native login, `shutil.which('qodercli')` resolves
`%USERPROFILE%\.qoder\bin\qodercli\qodercli.EXE`, and the real transport connect → control request →
`interrupt()` → `disconnect()` sequence completed with no orphaned child. `get_available_models()`
returned 17 models whose catalog carries per-model `isFree`/`priceFactor`/`originalPriceFactor`, so
cost is read live. `live_steer` untouched, still `false`.

Tests: Phase F installs the dependency into the real Bridge environment and runs deterministic adapter
tests before any live smoke; the live smoke must enumerate models first and choose one explicitly.

#### Phase 0E — Claude SDK

In an isolated Bridge environment prove without inference:

- SDK installation from Windows resolution path;
- import;
- native claude.exe resolution;
- required SDK classes/options;
- permission result types.

Gate: Claude remains in v0.11 unless this produces a demonstrated upstream blocker.

Status: **CLOSED — gate PASS, Claude remains in scope.** No upstream blocker exists.
PR: https://github.com/NTLx/ServerFS_MCP/pull/32 (Phase 0 branch `v0.11-phase0-experiments`)

Implemented: no product code; second isolated `uv` venv under `%TEMP%\serverfs-phase0e\`.

Measured: `claude-agent-sdk==0.2.156` (the frozen pin) installs on Windows — the sdist builds with no
compiler — and publishes a `win_amd64` wheel. Every name imported at `adapters/claude.py:20-33`
exists across `claude_agent_sdk` and `claude_agent_sdk.types`; `ClaudeAgentOptions` accepts
`cli_path/cwd/setting_sources/permission_mode/can_use_tool/resume/continue_conversation/session_id/model/allowed_tools/env`;
the real transport against the native `C:\Users\lx\.local\bin\claude.EXE` completed
connect → `get_server_info()` control round trip → `interrupt()` → `disconnect()` with no leftover
`claude` child; `_internal.transport.subprocess_cli` imports cleanly. Distribution matrix correction
and the upgrade condition are in §12 and `docs/windows-phase-0d-0e-agent-sdks-2026-10-04.md` §3.

Not done here, deliberately: no prompt, no turn, no session content, and the WorkPC-specific
`ANTHROPIC_BASE_URL` configuration observed in the server-info payload is not generalized to standard
Claude environments (instruction §17). Real approvals/questions/cancel/resume belong to Phase G.

#### Phase 0F — Agent Runtime Egress Proxy

This is an additive gate introduced after Phase 0A-0E because the WorkPC deployment has a real network constraint: Codex/OpenAI services require an outbound proxy.

The WorkPC AI Agent is allowed to discover and use the actual development-environment proxy for this experiment. It must not record the raw endpoint, credentials or account-specific values in the repository, plan, logs or report.

Prove, without changing machine-wide/provider-persistent settings:

- what proxy form is actually available on WorkPC (HTTP endpoint, auth required or not), reported only as redacted metadata;
- direct Codex/OpenAI network health fails or is degraded when the proxy is absent, where a safe provider-supported health check can demonstrate that;
- the same health path succeeds when the dedicated Agent proxy is injected only into the Bridge-owned Codex app-server process;
- the selected Codex loopback `ws://127.0.0.1:<ephemeral>` control connection bypasses the proxy even while provider egress uses it;
- which standard variables the installed Codex build actually consumes (`HTTP_PROXY`, `HTTPS_PROXY`, lowercase variants, `ALL_PROXY`, `NO_PROXY`, or provider-specific settings), using measured behavior rather than assumptions;
- whether Claude SDK networking is owned by the SDK process or native `claude.exe` child, and the minimum proxy injection point required;
- whether Qoder SDK networking is owned by the SDK process or native `qodercli.exe` child, and the minimum proxy injection point required;
- provider control connect/disconnect remains clean with proxy enabled and no model inference;
- proxy variables are not copied from `SERVERFS_PROXY_*`; dedicated Agent proxy configuration works independently;
- diagnostics can report configured/reachable without exposing endpoint/user/password;
- if the actual proxy requires credentials, determine whether a provider process or agent-launched tool can read those credentials. If yes, STOP the credentialed-proxy implementation and propose a credential-brokering/local-forwarder design before product code. Do not accept secret exposure as the price of connectivity.

Gate: freeze the dedicated Agent runtime proxy contract and the exact per-runtime injection points before Phase A/D product implementation relies on them.

Status: **CLOSED — gate PASS** (2026-10-04, WorkPC). PR: https://github.com/NTLx/ServerFS_MCP/pull/32 (Phase 0 branch `v0.11-phase0-experiments`)

Implemented:
Nothing in product code. Isolated probes under `%TEMP%\serverfs-phase0f\`: registry/env/WinINet/WinHTTP
discovery that classifies instead of echoing, raw TLS and `CONNECT` health probes, `codex doctor`
health in eight environment configurations, a loopback fake/observer proxy that attributes each tunnel
request to its owning process and records only non-sensitive classifications, a
`--ws-auth capability-token` app-server run with the proxy injected, a `websockets` loopback matrix, an
environment-inheritance capture at the SDK's own process-spawn boundary, and a credential/descendant
visibility test using synthetic values.

Measured:
WorkPC's available proxy is an HTTP CONNECT forwarder on a public-network address with **no
credentials required**; `api.openai.com` and `chatgpt.com` time out direct and succeed through it, so a
true no-proxy baseline exists and Codex health legitimately flips `fail → ok`. Codex consumes
`HTTPS_PROXY` (upper or lower case) or `ALL_PROXY`, and `HTTP_PROXY` alone is insufficient; it honours
`NO_PROXY` for real. With the proxy injected into the Bridge-shaped app-server, `initialize` and
`model/list` still work and the observer recorded seventeen external tunnel attempts and **zero**
loopback-target attempts. On the client side, `websockets` was proven to route `ws://127.0.0.1` through
a configured proxy unless `proxy=None` or a covering `NO_PROXY` is present, and an empty operator
`NO_PROXY` re-enables that routing — which is why the mandatory local set is merged, never replaced.
Qoder's provider connections are owned by `qodercli.exe` and are steered by `QoderAgentOptions.env`
exactly as by the parent environment; its endpoint is directly reachable here and it falls back to
direct when the proxy fails. Claude's control path performs no provider network at all, so its proxy
consumption cannot be observed without a turn and is deferred to Phase G with that reason recorded.
The decisive finding is environment inheritance: both SDKs copy the entire Bridge environment into the
provider child (a planted Tunnel-namespace variable arrived there in every case) and Claude offers no
removal mechanism, so the Bridge environment itself must be secret-free and proxy-free. Finally,
authenticated-proxy injection was measured to be unsafe in this trust model — the provider child sends
`Proxy-Authorization` itself, descendant tool code inherits the variable, and a same-user process can
open the child for memory reads — so v0.11 ships credentialless/local-broker support only.

Tests:
No automated test in Phase 0. Phase D's environment builder and Phase H's CI must prove each frozen
row: `use_proxy=false` injects nothing, `use_proxy=true` injects exactly `HTTPS_PROXY` + merged
`NO_PROXY`, mandatory entries survive an operator value and an empty value, Tunnel secrets and proxy
variables are absent from the Bridge environment and therefore from provider children, the Codex client
passes `proxy=None`, a fake HTTP proxy sees external targets but never loopback ones, and doctor
output stays endpoint-free.

Not verified, with reasons: Claude CLI proxy consumption needs a real request (Phase G); no
machine-wide or provider-persistent configuration was touched. Two follow-on questions are now
**decided**, not open: the authenticated-upstream broker is out of scope for v0.11 (§7.3), and Jev
advisory egress may consume the dedicated Agent proxy configuration only through an explicit client
parameter, never through the Bridge environment (§7.4, implemented in Phase D).

### Phase A — portability foundation

Implement only structural portability fixes:

- platform-safe imports;
- platform-safe tests;
- lazy runtime imports;
- backend-neutral Agent cwd validation;
- platform abstractions for IPC/private-state/lease.

No real Windows runtime enabled yet.

Exit:

- Linux root tests green;
- Linux Bridge tests green;
- root suite collects on Windows;
- portable Bridge tests run on Windows.

Status: **CLOSED — exit PASS** (2026-10-05 local; CI job logs carry 2026-10-04T17:5xZ, WorkPC).
Branch `v0.11-phase-a-portability`,
PR: https://github.com/NTLx/ServerFS_MCP/pull/33

Implemented:

- **A1 platform-safe imports.** `agent_bridge/src/serverfs_agent_bridge/platform_seams.py`
  names the four proven seams (`LOCAL_IPC`, `PEER_IDENTITY`, `WRITER_LEASE`, `PRIVATE_STATE`)
  and `require_linux_seam` fails closed with `BRIDGE_PLATFORM_UNSUPPORTED` when a Windows
  Bridge process asks for a Linux mechanism. `leases.py` imports `fcntl` under guard; the
  Unix-socket server, the flock lease, and the UID/mode state owners (`TaskStore`,
  `ActiveGuardManager`, `ResultSpool`) check their seam at construction. Linux behavior is
  unchanged — the guard returns immediately there, and no primitive was replaced or emulated.
  The Linux kernel modules (`fdio`, `binary`, `filesystem`, `mutations`, `search`,
  `linux_backend`) still cannot be imported on Windows **by design** (§11 import boundary):
  the product layer reaches them only through `get_backend()`.
- **A2 platform-safe tests.** `tests/platform_contract.py` classifies per test — portable,
  Linux-kernel contract, or measured cross-platform difference — with the mechanism named in
  each reason. Byte-fidelity assertions seed with `write_bytes` and compare `read_bytes`
  (Windows newline translation and the GBK locale codec were producing fake failures), and
  assertions that expect a revision to change call `settle_file_time()` instead of depending
  on process speed. No portable test was skipped to reach green.
- **A3 lazy runtime imports.** `adapters/__init__.py` resolves the three SDK-backed adapters
  on first attribute access; `main.py` touches them only inside the enabled branch. Enabling
  one runtime no longer imports another runtime's SDK, and a missing SDK names the runtime.
- **A4 backend-neutral Agent cwd validation.** `agent_tools._validate_agent_cwd` uses
  `WorkdirSession.validate_directory` (the pre-open contract find/search already had) instead
  of `fdio`, and shares `tools.fs_error`, so codes match on both platforms. On the Bridge side,
  `resolve_relative_cwd` now refuses backslash and drive-prefix forms: it treated a native path
  as one relative component, so on Windows `C:\<workdir>\sub` was silently accepted as an Agent
  cwd whenever it happened to land inside the workdir.
- **A5 seams only.** Only the §3 seams above were introduced. No Phase B/C/D Windows
  implementation, no parallel Windows service/store/lifecycle module.
- **A6 no proxy code.** Nothing in `src/` or `agent_bridge/src/` mutates the process environment
  (verified: no `os.environ[...]`, `setdefault`, `putenv` or `update` writes), and no
  `SERVERFS_AGENT_PROXY_*` reader was added. Phase D owns that.
- **A7 Jev unchanged.** No Jev file was touched.

Executed gates:

| Gate | Result |
| --- | --- |
| Windows `uv run ruff check .` | All checks passed — VERIFIED |
| Windows `uv run ruff format --check .` | 193 files already formatted — VERIFIED |
| Windows `uv run pytest` (full root) | **905 passed, 127 skipped, 1 xfailed** — VERIFIED (was: 12 collection errors, suite not runnable) |
| Windows v0.10 gate set (15 files) | **359 passed, 3 skipped** — VERIFIED, unchanged |
| Windows `agent_bridge` gate (`ruff check`, `ruff format --check`, `pytest`) | **51 passed, 103 skipped** — VERIFIED |
| Linux root suite (CI `Container / Test`) | **1074 passed, 11 skipped** — VERIFIED |
| Linux root suite on `main` for comparison (same runner) | 1067 passed, 11 skipped — VERIFIED; the delta is exactly the 7 added §8.2 cwd cases, and the skip count is unchanged, so no Linux test was lost |
| Linux Bridge suite (CI `Container / Agent Bridge test`) | **154 passed** — VERIFIED (154 = the 51 portable + 103 classified Linux-contract tests, all of which run on Linux) |
| CI `windows-native / native-kernel` (cargo kernel, ruff gate, Windows Python/native test set, wheel acceptance in a clean env) | all steps success — VERIFIED (per-step counts not extracted from the job log) |
| CI `Windows native / native-kernel`: ruff gate, 13-file Python set, wheel acceptance | all steps success — VERIFIED (step conclusions; per-step counts not extracted) |
| `bash -n deployment/agent-bridge/*.sh` | Not run — no Phase E shell script changed |
| `uv sync --frozen` (root) | exit 0 — VERIFIED, see the wheel note below |
| `docker compose config` | exit 0, output not printed — VERIFIED |
| `SERVERFS_IMAGE=serverfs-mcp:dev docker compose build` | Not run locally — CI `Container check` builds the image on Linux and passed |

Repository gate note (measured, cost a re-run): on a Windows checkout `uv sync --frozen`
**uninstalls the locally installed `serverfs-windows-native` wheel**, because that wheel is not part of
the locked root dependencies. With it gone, every native-kernel test errors instead of skipping and the
suite no longer measures what this table claims. Re-install the built wheel after any frozen sync before
trusting a Windows gate:
`uv pip install native/windows/target/wheels/serverfs_windows_native-<version>-cp312-abi3-win_amd64.whl`.
The full root gate was re-executed after restoring it and reproduced the numbers above.

Container CI gained a `bridge-test` job because the root pytest configuration does not collect
`agent_bridge/tests` (AGENTS.md → Change protocol); without it the Linux Bridge gate had no
runner at all.

Recorded differences and findings:

1. `edit_text_file` on a directory target with a stale revision returns `REVISION_CONFLICT` on
   the v0.10 native kernel and `NOT_A_FILE` on the Linux FD pipeline. Measured directly: with the
   exact revision both report `NOT_A_FILE`, so the native kernel checks the revision guard before
   the target type. Mutation is refused either way; only the code ordering differs. Marked
   `xfail` rather than re-asserted, so Phase C/D must decide whether to align the precedence.
2. Windows records file times from the sampled system clock, so two mutations inside one step share
   a stat tuple and therefore a revision token. Measured on WorkPC: 17 of 20 same-size rewrites in
   a tight loop kept an identical revision, and consecutive recorded times ranged 0.4 ms–15 ms.
   This is an input to Phase C (lease and revision-guard precision) and Phase D, not a test fix.

Not verified, with reasons: ChatGPT → Tunnel → deployment E2E belongs to the maintainer; the
ripgrep-parity tests self-skip on this host because `rg` is not on the Python PATH on Windows
(they run in the Linux CI job); Windows production use of the seams is not enabled — a Windows
Bridge process still cannot serve, lock or own private state until Phases B–D provide those twins.

### Phase B — Windows IPC and private state

Implement:

- Named Pipe server/client;
- SID/PID/session verification;
- explicit pipe ACL;
- Windows data-home;
- Windows private-state ACL guards;
- Windows Bridge startup with FakeAdapter.

Exit:

A real Windows ServerFS process can invoke the Fake Bridge through the existing MCP Agent tool contract.

Phase B closure (2026-10-05): **CLOSED-PASS** on Windows 11 x64 + local NTFS.

Implemented — only the three frozen seams, plus the data home and the client half of the IPC seam:

- **LOCAL_IPC** — `agent_bridge/src/serverfs_agent_bridge/windows_ipc.py` (a bounded pool of byte
  Named Pipe instances behind a `StreamReader`/`StreamWriter` facade) on top of `windows_pipe.py`
  (overlapped connect/read/write, `CancelIoEx`, bounded waits, peer measurement). The RPC core in
  `protocol.py` is shared: same `PROTOCOL_VERSION`, envelope, method names, error vocabulary,
  `MAX_REQUEST_BYTES`/`MAX_RESPONSE_BYTES` (§5, §16–§18).
- **PEER_IDENTITY** — the first frame is read and buffered, then the client is impersonated, its
  token SID/PID/session measured, `RevertToSelf` run in `finally`, and only then is the frame
  dispatched (§9, §10). A peer that was never measured is refused. Authorization is the SID;
  PID/session are recorded evidence (§11). The client half (`src/serverfs_mcp/windows_agent_pipe.py`,
  reached from `agent_client.py` only on Windows) measures `GetNamedPipeServerProcessId` plus that
  process's token SID and asserts it equals this process's own SID **before** writing a request
  byte (§12). ctypes throughout — no new dependency, no private asyncio or multiprocessing API (§20).
- **PRIVATE_STATE** — `private_state.py` converges the scattered POSIX owner/mode logic that used
  to be restated in `store.py`, `recovery.py`, `result_spool.py`, `leases.py` and the spool reader
  (§24). Linux semantics are byte-identical. Windows creates each object with an explicit
  protected descriptor (`D:P(A;OICI;GA;;;<sid>)`), verifies owner SID + ACE set + reparse state,
  and **never repairs** an object it did not create (§25–§29); SQLite `-wal`/`-shm` companions are
  verified against the protected state directory's inheritance.
- **Data home** — `data_home.py` mirrors the v0.10 `SERVERFS_DATA_HOME` / `%LOCALAPPDATA%\ServerFS`
  contract Bridge-side with parity tests instead of a cross-package import, and fails closed when
  neither is configured; no `platformdirs`, no cwd/TEMP guess (§22, §23).
- **Pipe name** — `local_ipc.derive_pipe_name` hashes the user SID into
  `\\.\pipe\serverfs-agent-bridge-v1-<16 hex>`; the name is disambiguation, never authentication
  (§6). `FILE_FLAG_FIRST_PIPE_INSTANCE` is claimed by the process's first instance and a collision
  fails closed with `PIPE_NAME_UNAVAILABLE` (§7).
- No `WindowsBridgeService`/`WindowsTaskStore`/`WindowsAgentService` and no parallel core were
  added (§4); no new MCP tool was introduced (§34); the writer lease, process containment, native
  `[agent]` TOML lifecycle, runtime egress proxy, doctor and real provider runtimes remain in their
  own phases (§3, §33).

Executed gates:

| Gate | Result |
| --- | --- |
| Windows root `ruff check` / `ruff format --check` | All checks passed / 208 files already formatted — VERIFIED |
| Windows root `uv run pytest` | **912 passed, 127 skipped, 1 xfailed** — VERIFIED (Phase A recorded 905/127/1: the delta is exactly the 7 new Windows Agent-E2E cases, no skip lost, no failure) |
| Windows v0.10 native set (`test_native_windows`, `test_native_config`, `test_native_tunnel`, `test_windows_backend`, `test_windows_mcp_e2e`, `test_windows_native_stdio`, `test_windows_path_acceptance`, `test_doctor`, `test_cli`) | **204 passed, 2 skipped** — VERIFIED |
| Windows `agent_bridge` `ruff check` / `ruff format --check` | All checks passed / 56 files already formatted — VERIFIED |
| Windows `agent_bridge` `uv run pytest` | **128 passed, 93 skipped** — VERIFIED (Phase A: 51/103. The 11 store private-state cases that now run on both platforms, plus 67 new Windows and platform-neutral cases) |
| Linux root suite in a `python:3.12` container from a git-tracked + Phase B working tree, `ripgrep` installed: `ruff check`, `ruff format --check` | All checks passed / 208 files already formatted — VERIFIED |
| Linux root suite collected-test comparison against `origin/main` in the identical image | main 1080 → Phase B 1087, and the per-file diff is exactly one added line (`tests/test_windows_mcp_agent_e2e.py: 7`). No Linux file or case left the suite, so §35's no-coverage-loss requirement holds — VERIFIED |
| Linux root `pytest` in that container | **Indicative only.** Repeated runs of the same image and tree ranged 1070–1097 passed / 13 skipped with 0–6 failures, always in `tests/test_agent_deployment.py` (Phase E `systemctl` and `set -o pipefail` script cases) or `tests/test_revision.py` opacity — container-environment cases that do not reproduce. The authoritative run is the CI job below, which is green |
| CI `Container / Test` (Linux root, `ruff check`, `ruff format --check`, `pytest`) | **All checks passed / 205 files already formatted / 1074 passed, 18 skipped** — VERIFIED on head `2bc517c` (the local format count reads 208 because this working tree also carries gitignored `scratchpad/` files). Phase A recorded 1074 passed / 11 skipped on the same job: identical pass count, and the +7 skips are exactly the new Windows-only MCP-surface E2E file, so §35's no-loss requirement holds in CI as well |
| CI `Container / Agent Bridge test` (Linux Bridge) | **All checks passed / 162 passed** — VERIFIED (§35's 154 baseline plus the 8 platform-neutral cases) |
| CI `Container check` (image build) and `Windows native / native-kernel` | both runs success on head `2bc517c` — VERIFIED (per-step counts not extracted from the job logs) |
| Re-run after the two follow-up fixes (pipe matrix + two-process E2E, three consecutive runs) | **27 passed, 0 failed** each, full Windows Bridge suite unchanged at 128 passed / 93 skipped, Windows root suite unchanged at 912 passed / 127 skipped / 1 xfailed — VERIFIED. The first-instance collision case is no longer timing-dependent |
| Linux `agent_bridge` suite in the same container: `ruff check`, `ruff format --check`, `pytest` | All checks passed / 56 files already formatted / **162 passed, 0 failed** in two independent runs — VERIFIED (§35's 154-passed baseline plus the 8 new platform-neutral `test_local_ipc_contract.py` cases; the four Windows-only files are `collect_ignore`d on Linux by the new `agent_bridge/tests/conftest.py`) |
| Linux root pass/skip totals and the CI `Container / Test`, `Container / Agent Bridge test`, `Container check` and `windows-native` jobs | To be recorded from the Phase B PR's CI run (§39) — **Not verified here** until that run is green |
| `bash -n deployment/agent-bridge/*.sh` | Not run — no Phase E shell script changed |
| `docker compose config` | exit 0, output not printed — VERIFIED |
| `SERVERFS_IMAGE=serverfs-mcp:dev docker compose build` | built under the scratch tag — VERIFIED (the running deployment was not touched; §38's `uv sync --frozen` wheel note does not apply because no `uv sync` was run in the root Windows venv this phase, so `serverfs-windows-native` stayed installed and the native suites ran) |

Security evidence, by requirement:

- §6/§7: `agent_bridge/tests/test_local_ipc_contract.py` (SID-derived name, canonical-SID refusal,
  determinism), `test_windows_pipe_ipc.py::test_first_instance_collision_fails_closed`.
- §8: `test_explicit_dacl_admits_the_expected_identity` and `test_explicit_dacl_denies_a_non_trustee`
  (the pipe DACL, not the name, is the gate; the default descriptor's Everyone/Anonymous read
  grant is replaced). The second is the automated negative proof; a real cross-account logon is
  recorded below as not verified.
- §9/§10/§11/§12: `test_windows_peer_identity.py` — impersonation before a completed read fails
  with `ERROR_CANT_IMPERSONATE_NAMED_PIPE`, identity is measured after it, `thread_is_impersonating()`
  is false on the same thread after success, after a failed impersonation, and after the caller's
  own assertion raises; PID/session are measured both directions; the client reads the server's
  SID. `test_unauthorized_peer_sid_is_refused_before_dispatch` shows an unmeasured/foreign SID is
  answered with `PEER_NOT_AUTHORIZED` and no `request_id`, i.e. nothing was dispatched. `§11`
  also has `test_the_peer_identity_never_prints_its_sid`.
- §13–§19: `test_windows_pipe_ipc.py` — pool bounds and replenishment, bounded connect retry,
  exact-1 MiB dispatch vs 1 MiB+1 → `REQUEST_TOO_LARGE`, `INVALID_REQUEST` on malformed JSON,
  split/coalesced frames, partial-frame idle release, client close mid-frame, server stop and
  restart reclaiming the name.
- §22–§30: `test_windows_private_state.py` — data home, override, missing-`LOCALAPPDATA` fail
  closed, protected creation of directories/files/ancestors, pre-planted broad or foreign-trustee
  descriptors refused without repair, junction reparse points refused at the target and along the
  parent chain, SQLite `-wal`/`-shm` confinement, spool directory/file protection, active-guard
  protection, `shared_gid` refused on Windows, and the writer lease still failing closed.
- §31/§32/§34: `agent_bridge/tests/test_windows_pipe_e2e.py` drives a real Bridge **subprocess**
  over the real pipe through `runtime.list`, `runtime.models`, `task.submit`, `task.get`,
  `task.events`, `task.result.read` (spooled, multi-chunk, and a 60 KB single frame reassembled),
  approval, question, message, cancel and idempotent replay, then asserts the SQLite state and the
  results tree. `tests/test_windows_mcp_agent_e2e.py` drives the published ten MCP tools over the
  same pipe against the same harness process.

Measured platform facts that changed a Phase B design assumption:

- A directory created by another process under `%TEMP%` on this machine inherits grants for two
  foreign user SIDs in addition to SYSTEM, Administrators and SELF. §25 forbids repairing an
  existing object, so the E2E fixtures let the Bridge create its own state/lock trees, and
  `_assert_windows_private` tolerates exactly `S-1-5-18`, `S-1-5-32-544` and `S-1-3-4` (SELF, which
  resolves to the owner §26 has already asserted) on an object the Bridge did not create. A foreign
  user SID is still refused. `windows_security.SYSTEM_TRUSTEES` carries the reason.
- The impersonated client token is `TokenImpersonation` at `SecurityImpersonation` level, and the
  client-side read of a same-process server reports that process's own SID — both are asserted in
  the identity suite rather than assumed.
- `CreateFileW` with `CREATE_NEW` reports `ERROR_ACCESS_DENIED`, not `ERROR_FILE_EXISTS`, when the
  path already exists and its descriptor denies this create; that code is therefore the §25
  pre-planting signal, and `create_private_file` returns "already exists" for it so the caller
  verifies and refuses instead of repairing.
- The first-instance claim cannot live in a racing accept thread. With one `CreateNamedPipe` per
  pool slot and only slot 0 carrying `FILE_FLAG_FIRST_PIPE_INSTANCE`, the other slots can create a
  plain instance first, which both makes this process's own claim fail and lets a second process
  silently join the name — so `NamedPipeEndpoint.start()` now claims the name once, before any
  thread exists, and hands that instance to slot 0. Verified: a held name produces
  `PIPE_NAME_UNAVAILABLE` every time, and the collision case ran green three times in a row after
  the change where it had previously been timing-dependent.
- Handing that instance over through a shared field was itself a race: four threads reading and
  clearing one attribute let a non-zero slot clear the claim before slot 0 read it, leaving the
  instance that carried the flag unserved. `start()` now takes it out before any thread exists and
  passes it to slot 0 as that thread's argument.

Not verified, with reasons:

- Cross-account pipe and NTFS denial (a second real Windows user logon) — §8 allowed the DACL-based
  negative proof instead; it stays a Phase H/acceptance item.
- A file-level symlink reparse point: creating one needs Developer Mode or privilege, so
  `test_symlink_as_final_state_object_fails_closed` skips with that reason on this host and the
  junction cases carry the §28 evidence.
- A *completed* task through the ten MCP tools on Windows. The frozen rule in
  `agent_tools._authorize_submit` requires a native runtime name to submit with `workspace-write`,
  and that profile is the writer lease's — Phase C. The lifecycle is instead proven over the same
  pipe and the same subprocess with the review profile, and the MCP case asserts the lease seam's
  own `BRIDGE_PLATFORM_UNSUPPORTED` answer arriving back through the published surface.
- Supervisor PID binding (§12's Phase D strengthening), `SERVERFS_DATA_HOME` production rendering,
  and any live ChatGPT → Tunnel acceptance, which belongs to the maintainer.

### Phase C — writer lease and recovery

Recorded prerequisites (maintainer decision 2026-10-05, from the Phase A closure findings; Phase B
did not touch either, because both are mutation/kernel semantics rather than IPC or private state):

- **Error precedence must be unified before the lease lands.** A directory target combined with a
  stale `expected_revision` answers `NOT_A_FILE` on Linux and `REVISION_CONFLICT` on the Windows
  native kernel. Phase C unifies this backend-neutrally with the **type check first**, so a
  directory returns `NOT_A_FILE` on both platforms regardless of revision state.
- **Revision precision is a real correctness risk, and test settling does not fix it.** Measured on
  the native kernel: 17 of 20 same-size external rewrites kept an identical revision token, because
  the public Windows revision material is volume serial, `FILE_ID`, attributes, size, link count and
  `LastWriteTime`, and `LastWriteTime` has a coarse sampled resolution. `settle_file_time()` in the
  tests only hides this for ServerFS's own mutations; it does nothing for an external non-ServerFS
  writer, and the writer lease does not cover that writer either. Phase C must therefore first run
  a revision experiment across `LastWriteTime`, `ChangeTime`, `FILE_ID`, size, attributes and link
  count for the mutation and external-write events it cares about, and must prioritize
  **stale-mutation safety over rename stability**. If `ChangeTime` is not sufficient, evaluate
  handle-based, bounded, documented and public O(1) change signals (for example the USN journal)
  with no admin-only assumption and no raw host identifier exposure; if none is viable, stop and
  report rather than ship a weaker guard. Do not adopt a SHA-256 of full contents on every `stat`
  without a new formal performance and contract decision. `settle_file_time()` stays a temporary
  Phase A test mechanism to be re-evaluated after Phase C, and the revision token *shape* decision
  waits for Phase C as well.
  *(Recorded as written. The experiment ran, no viable O(1) signal was found, the stop-and-report
  branch was exercised rather than bypassed, and the maintainer resolved the resulting contract
  choice as **Option B** — see the C0 decision below, whose completion contract supersedes this
  bullet's gate condition.)*
- **Phase B adds one exit requirement to this phase:** the ten MCP Agent tools cannot complete a
  task on Windows until the lease exists, because `_authorize_submit` requires a native runtime name
  to submit with `workspace-write`, and that profile is the writer lease's. Phase C's acceptance
  therefore includes a *completed* task through the published surface on Windows, not only the lease
  tests below.

**C0 as measured (2026-10-05).** The experiment in
`docs/windows-phase-c-revision-correctness-2026-10-05.md` measured the candidate signals on held
handles over 120-trial rapid same-size rewrites at three durability points, plus a granularity sweep
and a rename/attribute/replacement matrix. Outcome: `ChangeTime` moves in exactly the trials
`LastWriteTime` moves (`only ChangeTime moved = 0` of 120) and additionally moves on 2 of 10
renames, so it buys no detection and costs rename stability; size, allocation size, attributes, link
count and file identity are unchanged in 120/120 same-size rewrites; the undetectable window is the
timestamp tick (on WorkPC, ≥ ~0.25 ms separation detected 40/40 — evidence about that machine, not a
guaranteed maximum). The first round disqualified the USN route on the premise that it needs a
privileged volume handle; that premise was wrong — `FSCTL_READ_FILE_USN_DATA` is a distinct
per-file/per-directory query — so it was measured separately (C0b below). At that point §15's
conditions 1 and 2 could not be met by metadata material, and entering `C1`+ would have violated the
gate, so the two candidate answers and then the contract-level A/B choice went to the maintainer
rather than being adopted here.

**C0b — the per-file USN route, measured and failed.** `FSCTL_READ_FILE_USN_DATA` (`0x000900EB`) does
work without elevation and without a volume handle: on an ordinary non-elevated file handle it returns
a `USN_RECORD_V2` with a nonzero USN, it works on a directory handle opened with
`FILE_FLAG_BACKUP_SEMANTICS`, and it behaves the same on both local fixed NTFS volumes. The value is
simply static: unchanged across 200/200 completed same-size rewrites with no sleep, and unchanged for
a different-size rewrite, an attribute toggle, a rename away-and-back, a same-name replacement, a
directory child create/remove, and ServerFS's own `replace_bytes`, with `Reason` zero throughout. §5's
gate is `unchanged = 0`, so C0b fails and §16's instruction applies: do not enter `C1`, and do not
substitute a bounded content digest. What C0b did confirm, independently: while an external writer
still holds `WRITE` access the ServerFS restricted-share target open is refused
(`ERROR_SHARING_VIOLATION`, never surfaced raw) and succeeds once that writer closes — so the
active-writer half of the combined model holds by sharing, while nothing available today reveals a
write that finished earlier. The remaining choice is therefore contract-level and belongs to the
maintainer: **A** — a full content-derived component in the published revision, changing the
`stat_file`/`read_text_file` read and performance contract; or **B** — formally lower the
external-writer guarantee, documenting the tick-sized blind window together with the share-mask hold
and stating that the Windows revision compares metadata rather than content.

**C0 status (2026-10-05): CLOSED — CONTRACT DECISION B.** Not "closed because the blind window was
fixed": the window was measured, the candidate strong signals were measured, none qualified, and the
maintainer converged the residual into an explicit product concurrency boundary. The experiments and
the USN follow-up stay recorded as they happened (§9 of the evidence document); what changed is what
the product promises.

Frozen Windows revision semantics:

- the public revision stays `v1:<16 hex>`, computed from object identity plus the relevant observable
  NTFS metadata — an **opaque optimistic-concurrency token**;
- it is not a content hash, not a cryptographic content identity, and not an atomic
  compare-and-swap token against arbitrary same-user processes;
- the deliberately accepted Windows-11-x64 + local-NTFS boundary: a non-ServerFS-coordinated external
  writer that completes a same-object, same-size in-place rewrite **within one filesystem timestamp
  tick** may leave the public revision unchanged. The WorkPC measurement (`≥ ~0.25 ms` separation ⇒
  40/40 detected) is recorded as measured evidence on that machine, never as a guaranteed maximum
  window; product wording stays "same timestamp tick".
- Option A (full content-derived public revision) was rejected because it turns `stat_file` and every
  other path that produces a real revision from an O(1) metadata query into an O(file-size) content
  scan for a size that has no natural bound — a published-performance regression, hidden I/O, added
  latency, and a new resource-exhaustion surface, all to close one narrow non-cooperating-writer
  alias. A strong content-identity revision mode, if ever wanted, is a separate product design and is
  not v0.11.

The three-layer concurrency model Phase C must implement and document:

1. **ServerFS-coordinated writers** — MCP mutation versus Agent `workspace-write`, serialized by the
   writer lease (C1–C5, the main object of this phase).
2. **Active external writer** — while another process still holds `WRITE` access, the frozen
   restricted-share strategy on the mutation target must refuse the ServerFS open
   (`ERROR_SHARING_VIOLATION` internally, never surfaced raw), so the transaction cannot even start.
   Measured in C0b: writer open ⇒ refusal; writer closed ⇒ the same open succeeds.
3. **Completed external writer** — object replacement (file identity change), size change and any
   observable metadata change are detected by the revision; the single accepted blind spot is the
   same-tick same-size in-place rewrite above.

**C0 completion contract, replacing the old gate condition** ("a rapid same-size external rewrite must
always change the revision" is no longer a release gate):

1. metadata and USN capability fully measured;
2. no ordinary-user O(1) strong change signal exists;
3. the blind window is precisely documented;
4. an active external writer is excluded by the restricted-share transaction;
5. `edit_text_file`'s read → commit runs entirely inside one target hold;
6. ServerFS-coordinated writers are serialized by the Phase C lease;
7. the public revision makes no content-identity or arbitrary-process-CAS claim.

Item 5 is a requirement, not an observation: the Windows edit channel currently reads the source bytes
*before* the replacement transaction opens the target, so an external writer can enter between the two.
Phase C must turn it into one transaction — acquire the restricted-share target hold → validate
regular-file type → validate `expected_revision` → read the source bytes from that same held object →
apply text semantics → build the replacement → final identity/revision/fingerprint gate → atomic
handle-relative publication — with the hold kept throughout. This narrows the **active-writer** race;
it does not and must not be described as closing the historical same-tick token alias, which Option B
accepts.

`upload_binary_file(overwrite)` and `delete_file` have no pre-read content problem, but the same
acquire-hold → validate → commit shape applies: the restricted-share target handle must stay held
across type validation, revision validation and the final commit/delete, rather than being released
and reacquired. Read channels (`read_text_file`, `download_binary_file`, `stat_file`) must not gain
long-lived writer-excluding sharing, and plain reads must still coexist with Agent `workspace-write`;
the existing before/after metadata check stays.

Closed independently of the decision: **C0.7 error precedence**, now green on both backends.
Tracing the channel showed the divergence lived in *two* layers, and the one that actually produced
`REVISION_CONFLICT` for `edit_text_file` was the Python backend: `windows_backend.replace_file`
compared the freshly statted revision before looking at the object type, with a comment claiming
that ordering was Linux parity — it is not, because Linux opens the target as a regular file and so
answers the type first. That check is now type → reparse → revision, and
`native/windows/src/mutation.rs` gates type-then-revision too (`check_file_target`) on the file-target
channels — replacement and `delete_file`, initial and final gate — while `delete_directory` keeps its
revision guard, since a directory is its correct target type.

Evidence: `cargo test` 10/10 lib tests plus the NTFS integration targets green, including
`directory_target_is_refused_before_the_revision_guard` (stale and current token); the Phase A
`windows_difference` xfail is retired and replaced by
`test_directory_target_stale_revision`/`test_directory_target_current_revision`, plus a
Windows-native MCP case covering `edit_text_file`, `upload_binary_file(overwrite)` and `delete_file`
on a directory, and a Linux-side stale-token case in the Linux-only overwrite suite. Full Windows
root gate on the locally rebuilt wheel (`maturin build --release --features pyo3 --locked`, the same
command CI uses): **915 passed, 127 skipped, no xfail** (was 912/127/1 xfail); Windows Bridge suite
unchanged at 128 passed / 93 skipped; root `ruff check`/`ruff format --check` clean.

Implement:

- platform-neutral lease identifier;
- LockFileEx Windows backend;
- MCP read-only lease probe;
- alias-derived Windows lock/guard artifacts;
- active recovery guards;
- native mutation integration;
- the C0 decision's transaction shape: `edit_text_file` reads the source bytes from the held
  restricted-share target object inside one acquire-hold → validate → commit transaction, and
  `upload_binary_file(overwrite)` / `delete_file` keep that hold across type validation, revision
  validation and the commit instead of releasing and reacquiring.

Required tests:

- Agent workspace-write lease blocks MCP mutation;
- MCP mutation blocks Agent workspace-write acquisition;
- live lease beats guard;
- stale guard returns WORKDIR_RECOVERY_REQUIRED;
- provider reconciliation clears guard only when safe;
- crash releases live lock but leaves guard;
- every mutating MCP tool goes through the writer lease — a guard test over the tool list, not a
  per-tool spot check — and no read channel takes one;
- an active external writer that still holds `WRITE` is refused on the mutation target, normalized
  and never surfaced as a raw `ERROR_SHARING_VIOLATION`;
- a deterministic seam proving the edit read happens on the held object inside the transaction (the
  hold is observable as held at read time, not merely before and after);
- binary overwrite and delete hold the target across validate → commit;
- a completed same-tick same-size external rewrite is recorded as accepted-boundary evidence, at
  evidence level — not as a permanent product xfail, and not described as fixed;
- a public-surface Windows Agent task submitted with `workspace-write` completes a real workspace
  mutation through the ten MCP tools and the pipe.

**Phase C closure (2026-10-06): C1–C5 implemented.** Each item below is the shipped shape, not a
plan, and every one of the required tests above has a named case behind it.

*C1 — platform-neutral lease identity.* `lease_identity` exists on both sides of the lease (the
packages must not import each other, §23), and cross-boundary agreement is pinned by an identical
11-vector table in `tests/test_lease_identity.py` and `agent_bridge/tests/test_lease_identity.py` —
shared data, not a shared import. A legacy Compose deployment keeps `slot:NN` and therefore the exact
`01..16.lock` and `active/NN` artifact names a running v0.10 Bridge created, so an upgrade finds its
own locks and guards. A native deployment is keyed by `alias:<exact alias>`, whose artifact name is
`sha256(lease_id)[:40]` (`.lock` for the lease, bare for the guard): 45 characters bounded, hex-only,
and — the reason alias text is never used — `Repo` and `repo` cannot merge into one lease on a
case-folding filesystem. Reserved DOS names (`con`, `nul`) and a 200-character alias are covered.
The key kind is one explicit config switch (`lease_key: slot | alias`, default `slot`, never mixed
inside one config) rather than a per-entry accident, because the two sides derive names
independently and a disagreement would silently lock two different files. Tasks store no new column:
`TaskRecord.lease_id` is derived from the stored `(workdir_slot, workdir_alias)` pair, with
`NO_LEGACY_SLOT = 0` as the "no slot" marker — no schema change, and the RPC payload shape is
untouched. The guard payload
carries `lease_id` additively and `read()` still accepts a v0.10 slot-only payload; a payload that
contradicts the artifact it occupies is invalid.

*C2 — the LockFileEx backend.* `windows_lease` on both sides implements the §5.5 shape: `GENERIC_READ`
plus `OPEN_EXISTING`, share `READ|WRITE|DELETE`, validated on the same locking handle
(`FileAttributeTagInfo`: a directory or a reparse object is refused), exclusive
`LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY` over the full range. Nothing on the reader path
ever creates the artifact, and an absent, denied, non-regular or planted object fails closed.
One deviation from §5.5 item 2 is deliberate and is recorded here instead of being glossed over: the
Bridge maps every open failure to its existing `LOCK_PATH_UNSAFE` code, and the ServerFS reader maps
to the three MCP codes the Linux lease already produced (`AGENT_LOCK_UNAVAILABLE`, `WORKDIR_BUSY`,
`WORKDIR_RECOVERY_REQUIRED`). `LOCK_PATH_UNSAFE` and `AGENT_LOCK_UNAVAILABLE` are the same condition
seen from the two processes; §5.5 listed both against one side, and inventing a new public MCP code
would have changed the frozen tool surface for a wording improvement.

One new measurement, found while implementing this and worth recording because Phase 0A did not state
it: **`LockFileEx` reads `Overlapped.Offset` even for a handle opened without `FILE_FLAG_OVERLAPPED`,
and a NULL `lpOverlapped` faults on this OS build** — every call with `None` raised an access
violation at offset `0x10` (the `Offset` member) while the identical call with a real zeroed
`OVERLAPPED` succeeded, on both `use_last_error` settings. The range start is therefore a fresh
zeroed structure per call, not a sentinel; per-call rather than shared so two threads cannot touch
the same structure.

*C3 — mutation integration and the held edit transaction.* `tests/test_mutation_lease_coverage.py`
observes the lease boundary through the published surface: each of the six mutation tools takes the
lease, `upload_binary_file(overwrite)` takes it too, no read channel takes one, and a mutation refused
for a stale revision still went through the lease. It runs on both platforms, so a future mutation
tool that forgets the lease fails in either gate. The edit channel is now one kernel transaction:
`replace_source_with` opens the target with the frozen restricted share, answers object kind and
revision, reads the source from *that held handle* (`read::read_open`, extracted from the existing
bounded read so both channels share one implementation), hands `(data, revision_before)` to the
caller's build step and publishes the result without releasing the hold. A build step that raises
aborts the mutation and its own exception reaches the caller unchanged — a text-edit refusal is not a
filesystem failure. Measured at the boundary (`tests/test_native_windows.py`): while the hold is
live an external `O_WRONLY` open is refused and a read of the same object still succeeds, and after
the transaction both succeed. Per contract decision B this narrows the **active-writer** race only;
it is not, and is not described as, a fix for the same-tick token alias.

Two kernel findings came out of this. `check_file_target` now answers a reparse object before a
directory, matching `entry_type`, `capture_snapshot` and the traversal gate, so every mutation channel
gives the precise Windows code for a junction target. And the mutation leaf open *follows* a junction
(the `IsADirectory`/`NotADirectory` answer comes from the followed object), which is why the edit
channel keeps its non-following `stat` classification ahead of the transaction: that is what produces
`REPARSE_POINT_NOT_ALLOWED`, the code v0.10 shipped, and removing it changed the code — a real
regression caught by the v0.10 gate, not a theoretical one. The transaction still re-answers kind and
revision on the handle it holds, so the classification is a precision concern and never the guard.

*C4 — recovery parity by moving evidence, not by duplicating code.* `recovery.py`, `service.py` and
provider reconciliation are shared, so instead of a Windows shadow implementation the two Linux-marked
lifecycle suites now run on Windows: the Bridge service suite (33 cases) and the runtime-reliability
suite (12 cases), i.e. 43 tests that used to skip on Windows and cover busy acquisition, guard
creation and clearing, cancellation, idempotency, timeouts, spooling, live-lease-beats-guard and
restart reconciliation against the LockFileEx lease and the Windows descriptor. One reliability case
stays Linux-marked, because its fixture pre-creates a `0700` state directory and Windows private state
must create its own tree rather than repair an inherited foreign grant (§25). A new portable
`agent_bridge/tests/test_recovery_identity.py` pins the guard identity rules for both platforms,
including the upgrade path for a v0.10 slot-only guard and the rule that an unreadable guard is a
recovery condition rather than something a scan may skip.

*C5 — the public Windows surface.* `tests/test_windows_mcp_agent_e2e.py` drives a real second process
over a real Named Pipe: a `workspace-write` task submitted through the ten MCP tools completes, the
Bridge-created artifact is the alias-derived one ServerFS opens, an MCP mutation during the turn is
refused `WORKDIR_BUSY` and writes nothing, the same mutation succeeds after the turn and the guard is
gone. The crash matrix runs against `TerminateProcess`: the guard survives, the lock does not, the
surface answers `WORKDIR_RECOVERY_REQUIRED`, and a Bridge restarted on the same state and lock trees
reconciles the task and clears the guard. The other direction is proven too: a lease held by a
separate process makes the Bridge refuse a `workspace-write` submission with `WORKDIR_BUSY`.
The accepted blind window is measured, not xfailed: `SERVERFS_MEASURE_BLIND_WINDOW=1` runs 200 tight
same-size external rewrites through the published `stat_file`/write path. On WorkPC: **75 unchanged,
125 detected**. That is the documented boundary of contract decision B, and the release suite carries
no permanent expected-failure for it.

Layer 2 is also asserted at the published surface, in both directions: with an outside process
holding `WRITE` on a target, `edit_text_file` and `delete_file` refuse the mutation, write nothing and
leave the file intact, and the agent-visible text carries only a redacted code (the frozen
`NATIVE_IO_ERROR` family on this build) with no Win32 sharing message and no host path; once the
writer is gone the same edit succeeds. The refusal is a refusal of the *open* — it is not a
compare-and-swap claim, and the observed code is deliberately not widened into a new public value.

**Re-evaluated test mechanism.** `settle_file_time()` was recorded as a temporary Phase A device to
be revisited after Phase C. The re-evaluation keeps it, for a reason that is now stated in
`tests/platform_contract.py`: four assertions (a same-size content change, a directory gaining an
entry, and the two directory-revision cases) depend on the clock having stepped, and under contract
decision B that is exactly the shape of the promise — the token detects a change once the timestamp
moves, and the same-tick same-size case is the documented limit. It is therefore an expression of the
boundary, not a workaround for a defect, and the boundary itself is asserted directly in
`tests/test_revision.py::TestWindowsAcceptedRevisionBoundary` rather than hidden behind the wait. The
Phase C instruction to remove it as obsolete is recorded here as considered and rejected, with the
count of the assertions that still need it.

Gates executed on WorkPC for Phase C: `cargo fmt --check`, `cargo clippy --all-targets -D warnings`
with and without the `pyo3` feature, `cargo test --locked` (13 lib + 17 ntfs_mutation + 9 ntfs_read +
9 ntfs_traversal, 3 symlink cases ignored without `SERVERFS_REQUIRE_SYMLINK`), the wheel rebuilt with
`maturin build --release --features pyo3 --locked` and reinstalled, `docker compose config` validated
and `SERVERFS_IMAGE=serverfs-mcp:dev docker compose build` completed under the scratch tag, Windows
root suite **993 passed / 128 skipped / no xfail**, Windows Bridge suite **249 passed / 50
skipped**, `ruff check` and `ruff format --check` clean in both packages. One root-gate step was
deliberately not re-run locally: `uv sync --frozen` would reinstall the pinned published
`serverfs-windows-native` and shadow the wheel built from this branch — the trap §38 records — and the
`Windows native` CI job runs the frozen sync and then tests the built wheel as an artifact, so that
step is covered where it can be covered honestly. Linux is evidenced by this
branch's CI (`Container / Test`, `Container / Agent Bridge test`, `Container check`, `Windows
native`), all green at code head `24a2830`: Linux root **1137 passed / 33 skipped**, Linux Bridge
**230 passed**. Those jobs earned their keep — the first Linux run caught a POSIX guard branch
reading an unassigned value and a Windows-only test file being collected on Linux, and the next one
caught a call site still passing a raw slot number, all fixed normally, which is exactly why the PR
jobs are the authoritative Linux evidence. Not verified: ReFS/SMB, a second Windows account or
integrity level, file-symlink (non-directory) reparse artifacts on the lease paths, and
real-provider `workspace-write` through Codex/Claude/Qoder (Phases E–G).

Exit: Windows reaches v0.7 lifecycle safety semantics.

### Phase D — native configuration and lifecycle

Implement:

- [agent] TOML;
- per-workdir agent_mode;
- agent_runtimes;
- private Bridge config renderer;
- dedicated Agent runtime egress-proxy config and environment builder from §7.1 / Phase 0F;
- per-runtime `use_proxy` policy and forced local no-proxy set;
- doctor integration with redacted proxy configured/reachable diagnostics;
- optional Bridge startup from native supervisor;
- Job Object containment;
- graceful shutdown.

Exit:

- Agent-disabled native upgrade behaves exactly like v0.10;
- Fake runtime works through full Tunnel -> MCP -> Bridge path.

### Phase E — Codex

Implement the selected Windows Codex transport and run deterministic adapter tests.

Then run a real smoke proving:

- runtime probe;
- real OpenAI/provider connectivity through the configured Agent runtime proxy on WorkPC;
- the provider egress proxy and Codex loopback WebSocket coexist, with localhost traffic bypassing the proxy;
- model discovery;
- new task;
- native thread/session ID persistence;
- file mutation under writer lease;
- approval;
- user question;
- cancellation;
- continuation/resume;
- per-task model override;
- Bridge restart reconciliation;
- clean lease/guard cleanup.

### Phase F — Qoder

Install/freeze Windows SDK dependency and run deterministic adapter tests plus real smoke covering:

- runtime probe;
- when `use_proxy=true`, real provider connectivity through the dedicated Agent runtime proxy with local control traffic bypassed;
- model discovery;
- new session;
- continuation;
- file mutation;
- approval;
- AskUserQuestion;
- cancellation;
- recovery behavior;
- cleanup.

live_steer remains false.

### Phase G — Claude

Use the proven Phase 0E SDK/native-CLI path.

Run deterministic adapter tests plus real smoke covering:

- runtime probe;
- when `use_proxy=true`, real provider connectivity through the dedicated Agent runtime proxy with local SDK/CLI control traffic bypassed;
- session creation;
- continuation;
- file mutation;
- approval;
- question;
- interrupt;
- cancellation;
- recovery classification;
- cleanup.

Do not claim stronger in-flight recovery than the provider exposes.

### Phase H — CI, packaging and release closure

Required Windows CI includes:

- root lint;
- root format;
- full Windows-collectible root suite;
- v0.10 native filesystem acceptance;
- provider-neutral Bridge tests;
- Named Pipe tests;
- Windows lease/recovery tests;
- Fake runtime MCP E2E;
- deterministic Codex adapter tests;
- deterministic Qoder adapter tests;
- deterministic Claude adapter tests when the dependency path is available;
- runtime-proxy environment-builder tests proving Tunnel proxy isolation and forced local no-proxy;
- a fake/local HTTP proxy test where practical so CI can prove routing without external provider traffic;
- three-wheel clean install;
- doctor smoke.

Linux CI remains unchanged and must stay green.

Release acceptance additionally requires real WorkPC provider E2E and live ChatGPT Tunnel E2E.

## 16. Release acceptance contract

v0.11.0 may be released only when all applicable items are satisfied:

1. v0.10 Windows filesystem behavior remains regression-green.
2. Linux Agent Bridge remains regression-green.
3. Windows Agent mode remains disabled by default.
4. Named Pipe IPC uses an explicit user-scoped ACL.
5. Connected client SID is measured and validated.
6. No TCP Bridge listener is required by the preferred architecture.
7. LockFileEx writer lease preserves the existing fail-closed mutation contract.
8. Native workdirs use stable alias-derived lease identity without restoring Docker slots.
9. Active recovery guards survive Bridge crashes.
10. Bridge-owned provider children are contained by a Job Object.
11. State/database/spool/guard paths are protected by verified Windows ACL semantics.
12. Agent tools import/register cleanly on Windows only when enabled.
13. Base filesystem-only tool surface remains unchanged when disabled.
14. Codex executes a real Windows task end to end.
15. Qoder executes a real Windows task end to end.
16. Claude executes a real Windows task end to end, or an explicit upstream SDK blocker is documented and release scope is reconsidered before tagging.
17. Approvals/questions/cancellation are demonstrated on every runtime claimed as supported.
18. Model discovery matches each provider's actual supported behavior.
19. Request-scoped model override remains non-persistent.
20. Task lifecycle/idempotency/result-spool behavior remains provider-neutral.
21. No provider credential enters MCP output/logs.
22. No Control Plane credential enters the Bridge.
23. Windows clean install requires no Docker/WSL.
24. Release assets install into a clean Windows environment.
25. Documentation/site matches the actual supported runtime matrix.
26. Live ChatGPT E2E succeeds before the stable tag.
27. Agent runtime egress proxy is explicit, disabled by default and independently configurable from the Tunnel/Control Plane proxy.
28. WorkPC Codex real-provider acceptance succeeds with the actual development proxy attached through the dedicated Agent proxy path.
29. Codex loopback WebSocket control traffic is proven to bypass the egress proxy.
30. `SERVERFS_PROXY_*` / Tunnel proxy secrets are never automatically forwarded into Bridge/provider children.
31. Proxy endpoint or credentials never appear in MCP output, logs, doctor output, generated Bridge config or repository evidence.
32. Agent runtime proxy support is claimed only as credentialless Agent Runtime HTTP proxy (§7.3): no
    upstream proxy credential is stored or injected, no broker process is added, and an
    operator-supplied credentialless local broker is the only authenticated-upstream path a deployment
    may use. Release documentation must not claim native authenticated proxy support.
33. The Windows revision token is documented and tested at its actual guarantee level (C0 decision B):
    a metadata-derived opaque optimistic-concurrency token; an active external writer is excluded by
    the restricted-share target open; ServerFS-coordinated writers are serialized by the writer lease;
    the same-tick same-size external rewrite is published as an accepted boundary. No release, README,
    site or tool text may claim content identity or compare-and-swap against arbitrary processes, and
    no machine-specific window size may be quoted as a guarantee.

## 17. Explicit non-goals

v0.11 does not add:

- generic shell execution;
- arbitrary argv/env execution;
- Windows Service installation;
- Task Scheduler deployment;
- AppContainer;
- new Agent MCP tools;
- a new public RPC protocol version;
- workflow/DAG orchestration;
- automatic runtime selection;
- automatic model selection;
- auto-approval;
- persistent ServerFS model defaults;
- native file-parameter ingress;
- ReFS/SMB Agent support claims;
- macOS Agent Bridge work;
- an authenticated-upstream proxy credential broker (§7.3): no stored proxy credential, no injected proxy
  credential, no broker listener or process; a credentialless local broker may be supplied externally.
- a content-derived Windows revision identity (C0 Option A): no content hash in `stat_file`,
  `read_text_file` or any other channel that produces a revision, and no strong content-identity
  revision mode. That would be a separate performance and contract design, not v0.11.

## 18. Implementation order

The default implementation order is:

1. Phase 0A LockFileEx
2. Phase 0B Named Pipe + SID
3. Phase 0C Codex proxy transport
4. Phase 0D Qoder SDK
5. Phase 0E Claude SDK
6. Phase 0F Agent Runtime Egress Proxy
7. Windows test-collection fixes
8. Platform seams
9. Fake Bridge E2E
10. Writer lease/recovery
11. native config/lifecycle + runtime proxy
12. Codex
13. Qoder
14. Claude
15. packaging/CI
16. live ChatGPT acceptance
17. docs/site alignment
18. v0.11.0 tag/release

The first six are experiments rather than product code. Their purpose is to eliminate the remaining expensive assumptions before architecture is frozen.

Phase 0 status (2026-10-04/05): 0A LockFileEx CLOSED-PASS, 0B Named Pipe + SID CLOSED-PASS, 0C
Codex transport CLOSED-PASS with authenticated loopback WebSocket selected (§10.2), 0D Qoder SDK
CLOSED-PASS, 0E Claude SDK CLOSED-PASS, 0F Agent Runtime Egress Proxy CLOSED-PASS (§7.1-§7.2). The two
decisions 0F left behind are now settled too: the authenticated-upstream broker is out of scope for
v0.11 (§7.3) and Jev may consume the dedicated Agent proxy configuration only through an explicit HTTP
client parameter, never through the Bridge environment (§7.4, Phase D work). Nothing in Phase 0 or in
the phase ordering is blocked on an open decision.
Phase A status (2026-10-05): **CLOSED-PASS** (§15 Phase A closure). Windows root suite runs
(905 passed / 127 classified skips / 1 recorded xfail), the v0.10 Windows gate set is unchanged at
359 passed / 3 skipped, the Bridge core imports on Windows with the §3 seams failing closed
(51 portable tests pass, 103 Linux-contract tests classified), and both Linux gates are green in CI
(root 1074 passed / 11 skipped; Bridge 154 passed, newly a CI job). Phase B is next and unblocked.
Phase B status (2026-10-05): **CLOSED-PASS** (§15 Phase B closure). The three frozen seams
`LOCAL_IPC`, `PEER_IDENTITY` and `PRIVATE_STATE` now have real Windows twins behind them, plus the
Windows data home and the client half of the IPC seam, and a real Windows Bridge **subprocess**
serves FakeAdapter tasks over a real Named Pipe. Windows root 912 passed / 127 skipped / 1 xfailed
(Phase A: 905 / 127 / 1 — the delta is the 7 new E2E cases), Windows Bridge 128 passed / 93 skipped
(Phase A: 51 / 103), and CI green on the code head `2bc517c`: Linux root 1074 passed / 18 skipped,
where the +7 skips are exactly the new Windows-only MCP-surface E2E file, Linux Bridge 162 passed on
top of §35's 154 baseline, `Container check` and `Windows native` success. Only documentation
follows that head. No Windows twin weakened a Linux contract. `WRITER_LEASE` and `PROCESS_CONTAINMENT`
still fail closed, and Phase C has three recorded prerequisites (§15 Phase C). Phase C is next and
unblocked.
The WorkPC deployment requirement — Codex and
OpenAI traffic only reachable through an outbound proxy — is now a measured product contract instead of
ambient developer-shell state, and Phase D may not implement an environment builder that contradicts
§7.2.
Phase C status (2026-10-05): **C0 CLOSED — CONTRACT DECISION B; C0.7 CLOSED — PASS.** The revision
experiment and its C0b follow-up are done and recorded
(`docs/windows-phase-c-revision-correctness-2026-10-05.md`): no O(1) non-elevated NTFS signal detects
a same-tick same-size external rewrite — `ChangeTime` adds zero detection and costs rename stability,
size/allocation/attributes/links/identity never move, and the per-file USN query, though it works
without elevation on both local NTFS volumes, returns a record that is static across 200/200 completed
rewrites. §15's original conditions 1 and 2 therefore cannot be met by any ordinary-user metadata
channel, which the maintainer resolved as **Option B**: the Windows public revision stays a
metadata-derived opaque optimistic-concurrency token, the same-tick same-size external-rewrite blind
window becomes an explicitly documented product boundary, and the full content-derived revision
(Option A) is rejected for v0.11 on published-performance grounds. The replacement C0 completion
contract and the mandatory hold-before-read transaction shape are recorded in §15 Phase C. C0b did
prove the active-writer half of the model: a writer that still holds `WRITE` is refused by the
restricted-share target open, and the same open succeeds once it closes. **C0.7** landed on its own:
type-before-revision precedence on both backends, with the Phase A xfail retired (Windows root then
915 passed / 127 skipped / no xfail; Windows Bridge 128 / 93; `cargo test` 10/10 lib plus the NTFS
integration targets).

Phase C status (2026-10-06): **C1–C5 CLOSED — exit PASS.** The lease identity, the `LockFileEx`
backend, the mutation integration and the held edit transaction, the recovery/crash parity and the
public Windows workspace-write E2E are implemented and recorded in §15 Phase C, with the two
clarifications §5.5 needed (per-process error-code mapping; `LockFileEx` requires a real
`OVERLAPPED` on this build). Gates as executed: cargo fmt, clippy `-D warnings` with and without the
`pyo3` feature, `cargo test` (13 lib / 17 ntfs_mutation / 9 ntfs_read / 9 ntfs_traversal), wheel
rebuilt with `maturin build --release --features pyo3 --locked`, Windows root 993 passed / 128
skipped with no xfail, Windows Bridge 249 passed / 50 skipped, `docker compose config` validated and
`SERVERFS_IMAGE=serverfs-mcp:dev docker compose build` completed, and
PR #35 CI green at the code head `24a2830` — Linux root 1137 passed / 33 skipped, Linux Bridge 230
passed, `Container check` and `Windows native` success. `uv sync --frozen` was not re-run in the local
root venv because it would shadow the locally rebuilt native wheel (§38); the `Windows native` job runs
it and tests the wheel artifact. The Linux jobs earned their keep: the first run exposed a POSIX
recovery-guard branch reading an unassigned value and a Windows-only test file being collected on
Linux, both fixed normally. Residual limitations, recorded rather than papered over: ReFS/SMB and a
second Windows account or integrity level are untested; file-symlink (non-directory) reparse
artifacts on lease paths still need Developer Mode; the guard read is unbounded by size (unchanged
v0.10 behaviour); `serverfs doctor` verification of the lease artifacts and the native config
renderer that writes `lease_key: alias` are Phase D work; real-provider `workspace-write` and live
ChatGPT acceptance belong to Phases E–G and to the maintainer.

## 19. Development discipline

- Read AGENTS.md and this plan before each implementation phase.
- The latest completed phase evidence plus tests and implementation are authoritative over older assumptions.
- Do not skip a phase gate merely because a later design appears straightforward.
- When a Phase 0 experiment disproves an assumption, update this plan before product implementation continues.
- Prefer surgical changes. Do not refactor unrelated Linux code.
- Every platform abstraction must preserve existing Linux behavior.
- Never weaken fail-closed behavior to make Windows tests pass.
- Do not silently substitute provider-private or undocumented APIs for a failed official integration path.
- Real provider inference belongs only in explicit live-smoke/acceptance phases.
- Keep secrets out of logs, reports, tests and generated fixtures.
