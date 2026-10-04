# ServerFS v0.11.0 Development Plan — Windows Native Agent Bridge

Status: IN PROGRESS — Phase 0 experiments; 0A CLOSED, 0B CLOSED, 0C measured with an open transport
decision (§10.1-§10.2), 0D/0E in progress
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

The Bridge environment must not receive Tunnel / Control Plane secrets or tunnel-only proxy credentials, while provider-native environment required by Codex/Claude/Qoder must remain available.

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

### 10.2 Selected candidate (awaiting maintainer confirmation)

Measured working, and recommended:

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

Cost of this choice, stated plainly: the Bridge owns its own `codex app-server` child (inside the Job
Object per §9) instead of attaching to the provider-managed daemon, so an in-flight turn does not
survive a Bridge restart. Windows Codex then has the same reconciliation floor as Windows
Claude/Qoder, while Linux Codex keeps the stronger daemon-backed `thread/read` proof. Thread resume
by ID still works because the shared `codexHome` state is the same.

Never, in any option: seize the managed daemon, bind non-loopback, run without authentication, or
reverse-engineer the daemon control protocol.

`codex app-server daemon enable-remote-control` is an official unexplored avenue that might expose
the managed daemon over loopback WebSocket and combine both options' benefits. Measuring it changes
the user's running provider configuration, so it is left for the maintainer to decide.

### 10.3 Protocol drift

The installed schema contains newer approval variants.

Unknown provider-native decisions must not silently degrade to decline or another known decision.

Unknown variants must be explicitly unsupported/omitted/rejected.

### 10.4 Windows readiness

serverfs doctor may report provider-supported read-only Codex Windows sandbox readiness.

Do not auto-run sandbox setup during normal startup.

## 11. Qoder runtime

Qoder is the second runtime track.

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

Implemented:
Nothing in product code, by design. One throwaway `ctypes` harness under
`%TEMP%\serverfs-phase0a\` (holder/contender child processes, per-stage Win32 error reporting,
hard deadline so a blocking call is reported as `BLOCKED@<stage>` instead of hanging). Kept outside
the repository (under `%TEMP%`) until Phase 0 closes, then deleted.

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

Status: **MEASURED — gate NOT closed, selection needs a maintainer decision** (2026-10-04).

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

Blocker for the gate: the plan's preferred transport is disproved, and each remaining candidate
gives up something the plan promised to keep — the proxy preserves the managed daemon and its strong
`thread/read` reconciliation but requires an invented RFC 6455 client over stdio; loopback WebSocket
uses only official flags with near-zero new protocol code but moves Codex onto the same "in-flight
work dies with the Bridge" reconciliation floor as Windows Claude/Qoder. This is a product trade-off,
recorded with the recommendation in §10.1-§10.2 and `docs/windows-phase-0c-codex-transport-2026-10-04.md` §5.

Not verified: `codex app-server daemon enable-remote-control` (would change the user's running
provider configuration), non-loopback bind behaviour (would expose a LAN listener), and real approval
server→client request round-trips (needs a turn, i.e. Phase E).

#### Phase 0D — Qoder SDK

In an isolated Bridge environment prove without inference:

- install;
- import;
- version;
- CLI resolution;
- auth helper;
- model catalog API.

#### Phase 0E — Claude SDK

In an isolated Bridge environment prove without inference:

- SDK installation from Windows resolution path;
- import;
- native claude.exe resolution;
- required SDK classes/options;
- permission result types.

Gate: Claude remains in v0.11 unless this produces a demonstrated upstream blocker.

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
- doctor integration;
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
- macOS Agent Bridge work.

## 18. Implementation order

The default implementation order is:

1. Phase 0A LockFileEx
2. Phase 0B Named Pipe + SID
3. Phase 0C Codex proxy transport
4. Phase 0D Qoder SDK
5. Phase 0E Claude SDK
6. Windows test-collection fixes
7. Platform seams
8. Fake Bridge E2E
9. Writer lease/recovery
10. native config/lifecycle
11. Codex
12. Qoder
13. Claude
14. packaging/CI
15. live ChatGPT acceptance
16. docs/site alignment
17. v0.11.0 tag/release

The first five are experiments rather than product code. Their purpose is to eliminate the remaining expensive assumptions before architecture is frozen.

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
