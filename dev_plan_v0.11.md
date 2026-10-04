# ServerFS v0.11.0 Development Plan — Windows Native Agent Bridge

Status: Phase 0 CLOSED

```text
0A PASS — LockFileEx
0B PASS — Named Pipe + SID
0C PASS — authenticated loopback Codex WebSocket
0D PASS — Qoder SDK
0E PASS — Claude SDK
0F PASS — Runtime Egress Proxy
```

No Phase 0 gate or design decision is outstanding (§18). Phase A (portability foundation) proceeds on
its own branch.
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

### Phase C — writer lease and recovery

Implement:

- platform-neutral lease identifier;
- LockFileEx Windows backend;
- MCP read-only lease probe;
- alias-derived Windows lock/guard artifacts;
- active recovery guards;
- native mutation integration.

Required tests:

- Agent workspace-write lease blocks MCP mutation;
- MCP mutation blocks Agent workspace-write acquisition;
- live lease beats guard;
- stale guard returns WORKDIR_RECOVERY_REQUIRED;
- provider reconciliation clears guard only when safe;
- crash releases live lock but leaves guard.

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
the phase ordering is blocked on an open decision; Phase A proceeds on its own branch. The WorkPC deployment requirement — Codex and
OpenAI traffic only reachable through an outbound proxy — is now a measured product contract instead of
ambient developer-shell state, and Phase D may not implement an environment builder that contradicts
§7.2.

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
