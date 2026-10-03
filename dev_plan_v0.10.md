# ServerFS v0.10.0 Development Plan — Native Windows Filesystem Backend

Status: Phase A-D closed; Windows connectivity prerequisite delivered; Phase E1/E2 closed (wheel packaging, doctor, bootstrap, publication channel); Phase F in progress — executed acceptance evidence in docs/phase-f-acceptance-2026-10.md
Baseline: v0.9.0 / main  
Primary release target: Windows 11 x64 + local NTFS workdirs  
Scope: add a first-class Windows-native ServerFS filesystem implementation with no Docker or WSL runtime dependency; keep the MCP/product layer in Python; implement the Windows filesystem security kernel as a small Rust/PyO3 native backend; preserve the existing public filesystem tool contract wherever platform semantics permit; leave Windows Agent Bridge and native file-parameter ingress for later releases.

## 1. Problem statement

ServerFS through v0.9.0 is a Linux-first system. Its filesystem security model is intentionally built on Linux kernel primitives:

- directory-file-descriptor traversal;
- `openat`/`dir_fd`;
- `O_NOFOLLOW`;
- `fstat` object identity;
- `renameat`/`linkat`/`unlinkat`-style mutation;
- `/proc/self/fd/N` anchoring for ripgrep;
- Linux ownership/mode/xattr metadata;
- Docker bind mounts as a second read-only enforcement layer.

That design is strong on Linux, but it is not a portable abstraction. Running the Linux image through Docker Desktop on Windows makes the existing service usable, but it does not make Windows a first-class ServerFS platform. Windows workdirs are still accessed through a Linux VM/filesystem translation layer, native NTFS semantics are not the security authority, Agent and filesystem behavior are split across kernels, and Docker/WSL become mandatory dependencies for a product that fundamentally only needs controlled filesystem access and MCP connectivity.

v0.10.0 therefore does **not** add another compatibility layer around the Linux implementation. It establishes the first native non-Linux filesystem backend and uses that work to remove Linux/Docker deployment assumptions from the ServerFS domain model.

The release goal is:

> ServerFS v0.10.0 runs as a native Windows MCP filesystem service against explicitly configured Windows directories, without Docker or WSL, while preserving ServerFS's fail-closed path confinement, object-identity, optimistic-concurrency and atomic-publication contracts.

The release is successful only if Windows is supported as a real security model, not merely if the Python process starts on Windows.

## 2. Goals

v0.10.0 has six primary goals.

1. **Native Windows filesystem service**
   - no Docker Desktop;
   - no WSL;
   - no Linux VM dependency;
   - no system-wide Python requirement when `uv` manages the project interpreter/environment.

2. **Preserve the existing MCP filesystem contract**
   - `list_workdirs`;
   - `list_directory`;
   - `find_files`;
   - `search_text`;
   - `read_text_file`;
   - `stat_file`;
   - existing text mutation tools;
   - existing native base64 binary transfer when enabled;
   - existing coded error model and audit discipline.

3. **Keep the product/control plane in Python**
   - MCP registration and schemas;
   - policy;
   - limits;
   - workdir registry;
   - edit semantics;
   - logging;
   - configuration;
   - orchestration.

4. **Move Windows filesystem security primitives into a narrow Rust native module**
   - HANDLE-relative traversal;
   - reparse-point rejection;
   - native object identity;
   - secure directory enumeration;
   - secure read;
   - native revision material;
   - atomic mutation;
   - platform metadata handling.

5. **Minimize runtime dependencies**
   - Python packages remain managed by `uv`;
   - Windows users consume a prebuilt Rust/PyO3 wheel and do not install Rust, Cargo, MSVC Build Tools or the Windows SDK;
   - no `pywin32`;
   - no Windows `rg.exe` requirement;
   - the official OpenAI `tunnel-client` may be kept project-local and version-pinned.

6. **Create a reusable platform boundary**
   - Linux keeps its proven implementation;
   - Windows gets a native implementation;
   - a future macOS backend can implement the same high-level filesystem contract without forcing Linux or Windows primitives onto Darwin.

## 3. Non-goals

v0.10.0 does **not** include:

- Windows Agent Bridge;
- Windows Codex/Claude/Qoder runtime integration;
- Windows writer-lease replacement for Agent workspace-write mode;
- Windows Task Scheduler or Windows Service installation;
- native Windows ChatGPT/OpenAI file-parameter ingress;
- AppContainer or a new Windows sandbox;
- OAuth redesign;
- a ServerFS-wide rewrite in Rust;
- a Linux filesystem rewrite in Rust;
- a portable emulation of Linux `stat_result`;
- a claim that every Windows filesystem is supported;
- guaranteed support for ReFS, SMB/network shares, OneDrive/cloud placeholders or arbitrary filesystem filter drivers;
- following symbolic links, junctions, mount points or other reparse points;
- NTFS Alternate Data Stream access through ServerFS virtual paths;
- recursive delete;
- shell/command execution;
- indexing/RAG;
- a generic regex search engine.

The v0.10.0 GA support claim is deliberately narrow:

> **Windows 11 x64, native CPython managed by uv, local NTFS workdirs, ordinary files/directories, reparse points fail closed.**

Windows arm64 may be published when the native wheel and acceptance suite are reproducibly built and exercised, but it is not a GA claim merely because cross-compilation succeeds.

## 4. Design principles and frozen invariants

### 4.1 Native platform semantics, common product contract

ServerFS shares a public contract across platforms, not a fake common syscall layer.

~~~text
MCP / policy / tool contract
            |
     FilesystemBackend
       /          \
 Linux backend   Windows backend
 openat/fd       HANDLE/NT APIs
~~~

Linux and Windows may use different implementations as long as they satisfy the same externally visible security and tool contracts.

### 4.2 No path-string revalidation as the Windows security primitive

The Windows backend must not implement confinement as:

~~~python
Path(root, relative_path).resolve()
if resolved.is_relative_to(root):
    open(resolved)
~~~

That model has the same check/use race ServerFS deliberately removed on Linux.

Request-controlled traversal must be anchored to an already opened directory object. The intended Windows primitive is documented `NtCreateFile`/`NtOpenFile` relative naming through `OBJECT_ATTRIBUTES.RootDirectory`, with no-follow/reparse handling verified against real Windows behavior before mutation ships.

Microsoft documents that `NtCreateFile` can name an object relative to the directory handle stored in `RootDirectory`:

https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile

### 4.3 Reparse points fail closed

All request-controlled Windows reparse points are unsupported in v0.10.0.

This includes, without trying to reinterpret them as Unix symlinks:

- symbolic links;
- junctions;
- mount points;
- cloud placeholders;
- projected/virtual filesystem reparse points;
- third-party reparse tags.

The backend opens/inspects the object itself rather than following normal reparse processing and refuses traversal through any reparse-point component. Microsoft explicitly documents the need for open-reparse-point behavior when applications intend to operate on the reparse object rather than follow it:

https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points-and-file-operations

A final reparse object may be reported by `stat_file` as `reparse_point` but is never read, entered, replaced or deleted through normal file/directory tools in v0.10.0.

### 4.4 Opaque backend-owned revisions

Revision generation moves behind the filesystem backend contract.

Python must not assume `os.stat_result` or POSIX inode semantics. Linux may continue deriving revisions from its current stat tuple. Windows derives its material from native handle metadata, including stable file identity and relevant mutable metadata.

Windows should use `FILE_ID_INFO` where available. Microsoft documents `VolumeSerialNumber + FILE_ID_128` as an object identity pair that uniquely identifies a file on a single computer:

https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info

The MCP output remains opaque:

~~~text
v1:<digest>
~~~

No raw volume serial, file ID, SID, ACL or host path is exposed to the agent.

### 4.5 Atomicity is a release gate, not an aspiration

Create/edit/binary overwrite must preserve the existing publication rule:

> A concurrent reader sees either the complete prior state or the complete published state, never a partially written destination.

Windows mutation does not ship until same-directory temporary creation plus confined native publication has been demonstrated against real NTFS concurrency tests.

### 4.6 Fail rather than silently lose metadata

The existing Linux rule remains conceptually authoritative:

> Losing platform-significant metadata silently is worse than refusing an edit.

The metadata set is platform-specific. Windows is not required to mimic POSIX ownership/mode/xattr, but it must define which Windows metadata it preserves and refuse mutations where a safe preservation path is not implemented.

### 4.7 Keep unsafe code narrow

The Rust crate may require `unsafe` for Windows FFI. Unsafe blocks must be concentrated in a small native layer, not spread through traversal and product logic.

Every raw HANDLE returned by FFI is immediately wrapped in an owned RAII type and closed automatically on all paths.

### 4.8 No unrelated cross-platform rewrite

The existence of a Rust Windows backend is not a reason to rewrite Linux `fdio.py`, MCP tools, Agent Bridge, logging or configuration behavior unrelated to v0.10.0.

## 5. Target architecture

### 5.1 Windows native runtime

~~~text
ChatGPT
   |
OpenAI Tunnel
   |
tunnel-client.exe
   |   (started by `serverfs tunnel`; holds the `file:` API-key reference and
   |    the derived CONTROL_PLANE_HTTP_PROXY — neither reaches ServerFS)
   | stdio (default native profile)
   v
sanitizer supervisor (serverfs_mcp.supervisor)
   |   forwards MCP stdin/stdout transparently and creates a sanitized
   |   environment before spawning the real child
   v
ServerFS MCP child (Python)
   |
   +-- config / policy / limits / schemas / audit
   |
   v
serverfs-windows-native (Rust/PyO3)
   |
   +-- retained root HANDLE
   +-- HANDLE-relative traversal
   +-- directory enumeration
   +-- read/stat/find/search
   +-- mutation / revision / metadata
   |
   v
NTFS
~~~

There is no Docker or WSL component in the supported Windows path.

### 5.2 Linux runtime remains valid

The current Linux Docker deployment remains supported and retains its existing Streamable HTTP topology and security model.

The platform split is:

~~~text
Linux:
  existing Python/POSIX backend
  Docker deployment remains supported
  Streamable HTTP remains supported

Windows:
  Python + Rust native backend
  native stdio is the default tunnel binding
  Docker/WSL are not the supported Windows architecture
~~~

A later Linux-native no-Docker deployment may reuse the native configuration/stdio work, but it is not required for v0.10.0.

### 5.3 HTTP proxy scope

The v0.10 public proxy configuration contract is shared across supported
platforms. Its fields are
`SERVERFS_PROXY_HOST`, `SERVERFS_PROXY_PORT`, `SERVERFS_PROXY_USERNAME`, and
`SERVERFS_PROXY_PASSWORD`. There is no protocol selector: v0.10 supports HTTP
proxy only. Empty host disables the proxy and requires the other fields to be
empty; a host requires a valid port. Empty username and password means no
authentication. A non-empty username enables HTTP proxy authentication, and
the password may be empty. Password without username is invalid. Credentials
are percent-encoded when deriving the tunnel client's internal
`CONTROL_PLANE_HTTP_PROXY` URL.

Linux Compose passes these values only to `openai-tunnel`; it does not pass raw
proxy configuration or derived credentials to `serverfs-mcp`, Agent Bridge, or
the v0.5 file-ingress sidecar. The Windows launcher uses the same four fields,
places the derived URL only in tunnel-client's environment, and uses a
supervisor to remove that environment before starting ServerFS. Never log
credentials or a credential-bearing URL. The default empty configuration
leaves tunnel behavior equivalent to the current proxy-disabled deployment.
This does not add SOCKS, PAC or system-proxy discovery, a generic proxy
service, host ports, or TLS interception.

Proxy credential rules (security requirements, frozen):

1. proxy credentials are deployment secrets and never belong in `serverfs.toml`;
2. no ServerFS filesystem backend reads or interprets proxy configuration;
3. proxy settings never change workdir authorization, filesystem roots or MCP schemas, and grant `serverfs-mcp` no Internet egress;
4. the derived credential-bearing URL must not be written back to `.env` or `serverfs.toml`, printed in logs, exposed by `serverfs doctor`, propagated into the ServerFS child environment, or included in MCP-visible errors;
5. human-facing diagnostics may show only redacted state (enabled/protocol/host/port/`authentication: configured`), never username or password;
6. tests must cover usernames/passwords containing URL-reserved characters to prove percent-encoding and redaction;
7. the Linux file-ingress SSRF boundary is unchanged;
8. malformed configuration (port outside 1..65535, password without username) fails before tunnel startup with an operator-recoverable coded error that does not echo the URL.

## 6. Platform-neutral domain refactor

The current model contains Compose artifacts that must stop being mandatory domain concepts:

- fixed `/workdirs/01..16` paths;
- exactly 16 workdir slots;
- `container_path` as the canonical root;
- `.serverfs-disabled` as the only way to express an unused slot;
- `WORKDIR_XX_*` as the primary configuration representation.

v0.10.0 introduces a platform-neutral resolved workdir model, conceptually:

~~~text
Workdir
  alias
  root
  description
  read_only
  effective_policy
  backend_session
  legacy_slot?       # only when built from the legacy Compose adapter
~~~

The public MCP identity remains the alias. A fixed numeric slot is no longer required by the filesystem core.

The legacy slot model remains available to preserve v0.9.0 Docker/Agent compatibility. It becomes an input adapter, not the canonical in-memory model.

## 7. Native configuration model

### 7.1 New TOML configuration

Native deployments use an explicit TOML file, read with Python 3.12 `tomllib` so no new parser dependency is required.

Example:

~~~toml
[server]
log_level = "INFO"

[defaults]
max_read_bytes = 524288
max_read_lines = 500
max_write_bytes = 1048576
allow_hidden = false
binary_transfer_enabled = false
max_binary_transfer_bytes = 8388608

[[workdirs]]
alias = "projects"
path = 'D:\Projects'
description = "Development projects"
read_only = false

[[workdirs]]
alias = "documents"
path = 'C:\Users\me\Documents'
read_only = true
~~~

Rules:

- `path` is operator configuration, never an MCP argument.
- workdir paths must be absolute native paths.
- aliases keep the current public validation rules unless a concrete compatibility reason requires change.
- duplicate aliases fail startup.
- invalid or unsupported roots fail startup.
- configuration is parsed once; effective policy is frozen at startup.
- native configuration has no arbitrary environment interpolation.
- secrets do not belong in `serverfs.toml`.

### 7.2 Legacy environment/Compose adapter

The existing `SERVERFS_*` and `WORKDIR_XX_*` environment configuration remains supported for the existing Linux deployment.

The adapter resolves the legacy environment + bind-mount layout into the same platform-neutral runtime model.

v0.10.0 must not force Linux operators to migrate configuration as part of the Windows work.

### 7.3 No fixed workdir count in native mode

The native TOML format does not impose the legacy 16-slot limit.

Reasonable startup/config-size limits may exist for denial-of-service protection, but they are resource limits rather than numbered deployment slots.

## 8. Native transport and tunnel integration

### 8.1 Stdio is the default native transport

The official OpenAI `tunnel-client` supports local MCP commands through `--mcp.command` / `MCP_COMMAND` and uses the child process stdin/stdout for MCP frames:

https://github.com/openai/tunnel-client/blob/master/docs/configuration.md

Therefore the default Windows profile is:

~~~text
tunnel-client
    |
    +-- starts the sanitizer supervisor once (as its MCP stdio command)
         |
         +-- supervisor spawns the ServerFS MCP child with a sanitized environment
    +-- MCP over stdin/stdout (forwarded by the supervisor)
~~~

Benefits:

- no localhost listening socket;
- no port allocation/conflict;
- no LAN exposure;
- no local process probing an unauthenticated HTTP port;
- no Host/Origin/DNS-rebinding policy in the native stdio path;
- tunnel-client owns the sanitizer-supervisor lifecycle; the supervisor owns the actual ServerFS child lifecycle;
- existing ServerFS structured logs already go to stderr, leaving stdout available for MCP frames.

The existing Streamable HTTP implementation remains for the Linux Docker profile.

### 8.2 Tunnel credential inheritance is an explicit security concern

The tunnel-client stdio implementation starts its MCP child with Go `exec.Command` and does not supply a replacement child environment, so the child inherits the tunnel-client environment by default.

Source:

https://github.com/openai/tunnel-client/blob/master/pkg/mcpclient/stdio_command.go

The delivered native deployment therefore never places the Control Plane API key in an environment that reaches ServerFS. The mechanism (implemented and regression-tested):

- `serverfs tunnel` passes the key to tunnel-client only as a `file:` API-key reference; the credential file must live outside every configured ServerFS workdir.
- tunnel-client's MCP command is the sanitizer supervisor (`serverfs_mcp.supervisor`), which may inherit the tunnel environment, but it constructs the ServerFS child environment by removing every `CONTROL_PLANE_*`, `TUNNEL_CLIENT_*`, `OPENAI_*`, `MCP_*` and `SERVERFS_PROXY_*` variable plus `HTTP(S)_PROXY`/`ALL_PROXY`/`NO_PROXY` before spawning `serverfs serve` and forwarding stdio.

Acceptance (proven by the Windows native stdio/supervisor tests; re-verified end-to-end in Phase F):

- the ServerFS MCP child environment does not contain the Control Plane API key value;
- the credential file is outside every configured ServerFS workdir;
- the credential file is restricted to the current Windows user;
- ServerFS logs never include the key or credential-file contents.

This is not claimed to be equivalent to a container security boundary against arbitrary same-user code execution. The ServerFS native threat model protects configured filesystem boundaries against untrusted MCP input/content; it does not claim to sandbox a fully compromised Python process from the rest of the current user's account.

### 8.3 One active stdio tunnel instance

The tunnel-client documents that multiple active instances sharing one tunnel ID are unsupported for stdio bindings. Native operations/documentation must reflect that constraint and avoid overlapping restart strategies.

## 9. Windows virtual path contract

MCP paths remain ServerFS virtual paths, not Windows paths.

Public examples remain:

~~~text
src/serverfs_mcp/main.py
docs/architecture.md
~~~

They never become:

~~~text
D:\Projects\src\...
\\server\share\...
\??\...
~~~

ServerFS virtual paths:

- are workdir-relative;
- use `/` as the only separator;
- reject NUL;
- reject leading root/absolute forms;
- reject escape above the root;
- reject `\` in a component;
- reject `:` in a component, which also excludes NTFS Alternate Data Stream syntax;
- reject ambiguous Windows trailing dot/space names;
- reject reserved DOS device-name forms conservatively;
- reserve ServerFS internal names before touching the filesystem.

The exact Windows component validator lives at the platform-policy boundary and has dedicated tests.

Policy comparisons for reserved/credential-sensitive names are case-insensitive on Windows even if a particular NTFS directory has case-sensitive behavior enabled. This is intentionally conservative so `.ENV` cannot bypass a `.env` protection rule.

## 10. Root acquisition and WorkdirSession capability

### 10.1 Open roots once at startup

Native workdir roots are acquired once during startup and held for the ServerFS process lifetime.

Conceptually:

~~~text
configuration path
    |
trusted root acquisition
    |
validated native root HANDLE
    |
WorkdirSession
    |
all request-controlled operations
~~~

Subsequent MCP requests never re-establish trust by re-resolving the configured absolute root path.

If an operator intentionally changes a workdir root, restart ServerFS.

### 10.2 Trusted root anchor vs untrusted relative path

The configured absolute root is administrator-controlled input and is the trust anchor. Request-controlled path components begin below the retained root handle.

v0.10.0 must reject a final configured root object that is itself an unsupported reparse point. Ancestor behavior in the administrator-supplied absolute path is outside the request-path confinement problem; `serverfs doctor` should report the acquired root's canonical identity/filesystem so operators can detect surprising topology.

### 10.3 Read-only and writable capabilities

The Rust module must not expose one unconstrained session with a boolean that every call ignores.

Conceptually:

~~~text
ReadOnlyWorkdirSession
  read/list/stat/find/search

WritableWorkdirSession
  read/list/stat/find/search
  create/replace/delete/mkdir/rmdir
~~~

Python still performs the public authorization check first. The native backend then enforces the capability boundary independently.

On Windows, HANDLE access requests should use the minimum access rights needed by the operation.

This is defense in depth against implementation mistakes. It is **not** represented as equivalent to Docker's read-only mount sandbox: native ServerFS remains one current-user process and does not claim to survive arbitrary code execution inside that process.

## 11. Windows native module boundary

The Python/Rust boundary is deliberately high-level.

Python must not manipulate raw Windows HANDLE values.

Target interface shape:

~~~text
open_workdir(root, mode) -> WorkdirSession

session.stat(path)
session.list(path, ...)
session.read(path, ...)
session.find(path, pattern, ...)
session.search(path, query, ...)
session.create_file(path, bytes, ...)
session.replace_file(path, bytes, expected_revision, ...)
session.delete_file(path, expected_revision)
session.create_directory(path)
session.delete_directory(path, expected_revision)
~~~

Exact method names may change during implementation, but the boundary follows these rules:

- native handles never cross into general Python code;
- native object identity/revision stays in Rust;
- Windows error details are normalized before returning to the MCP layer;
- no public Python method accepts an absolute Windows path after the root session has been created;
- no generic “open arbitrary path” escape hatch exists.

## 12. Rust crate and FFI strategy

Proposed repository layout:

~~~text
native/
  windows/
    Cargo.toml
    Cargo.lock
    src/
      lib.rs
      ffi.rs
      handle.rs
      path.rs
      metadata.rs
      traversal.rs
      read.rs
      enumerate.rs
      search.rs
      mutation.rs
      error.rs
~~~

Runtime/build choices:

- Rust stable, pinned for reproducible CI;
- PyO3 for the Python extension boundary;
- Maturin for wheel building;
- Microsoft `windows-sys` or narrowly generated Windows bindings for documented Win32 APIs;
- a minimal explicit FFI declaration only for documented NT entry points not cleanly exposed by the pinned Windows binding crate;
- exact Cargo lockfile committed;
- no broad convenience framework.

Unsafe policy:

- raw FFI calls are concentrated in `ffi.rs` / handle construction;
- a successfully returned HANDLE is immediately converted to an owned RAII type;
- higher layers operate on safe wrappers;
- no handle leaks on exceptions/panics;
- panic must not unwind across the Python FFI boundary.

## 13. Secure HANDLE-relative traversal

The intended traversal model mirrors the security property of Linux `openat` rather than its syntax.

For:

~~~text
src/serverfs_mcp/main.py
~~~

the Windows backend conceptually performs:

~~~text
retained root HANDLE
  -> open literal child "src" relative to root
  -> validate child is directory and not reparse point
  -> open "serverfs_mcp" relative to retained child HANDLE
  -> validate
  -> open "main.py" relative to retained parent HANDLE
  -> validate type
~~~

Each component is opened from an already trusted parent directory object.

Required properties:

- no whole-path `Path.resolve()` security decision;
- no lstat-then-open pattern;
- no reparse following;
- component identity checked on the returned handle;
- parent handles retained while descendants are opened;
- no fallback to absolute-path reopening after validation.

`NtCreateFile`/`NtOpenFile` RootDirectory behavior is the target primitive. Exact access masks, share modes, object attributes and open flags must be proven by a focused Windows prototype before the full backend is written.

## 14. Entry types

The platform-neutral MCP type set is extended additively so Windows does not mislabel every reparse object as a Unix symlink.

Expected values:

- `file`
- `directory`
- `symlink` — Linux/macOS when applicable
- `reparse_point` — Windows unsupported reparse object
- `other`

Older clients treating the field as an opaque string remain compatible.

Windows reparse tags are never exposed unless a future explicit contract requires them; detailed reparse metadata can reveal topology and is not needed for agent recovery.

## 15. Read and stat

### 15.1 Read

Windows read behavior must preserve the current ServerFS contract:

- only ordinary regular-file base data;
- UTF-8 text for `read_text_file`;
- NUL/binary detection unchanged at the product layer where practical;
- byte/line limits unchanged;
- binary download returns exact base-stream bytes;
- content read is tied to the native object whose handle was opened;
- revision is computed from that object;
- if a binary download detects object change during the operation, no mixed/stale payload is returned.

### 15.2 Stat

`stat_file` returns the same normalized fields:

- workdir;
- virtual relative path;
- type;
- size where meaningful;
- modified time;
- MIME best effort;
- opaque revision.

No raw host path, volume serial or file ID appears in the result.

## 16. Directory enumeration and find

Directory listing and recursive find are implemented through the Windows backend, not Python path walking.

Security requirements:

- enumeration begins from an already opened/validated directory handle;
- child metadata is obtained without following reparse points;
- reparse directories are reported but never traversed;
- denied/hidden/reserved entries are filtered consistently;
- walk-entry and result limits remain global;
- directory races produce bounded skip/retry/error behavior but never path escape;
- returned paths always use ServerFS `/` virtual syntax.

The implementation may use documented native directory enumeration APIs selected during the prototype. The public contract, not a specific enumeration syscall, is frozen here.

## 17. Windows search implementation

Windows v0.10.0 does **not** depend on ripgrep.

The existing Linux search safety depends on running `rg` with cwd anchored to `/proc/self/fd/<validated-directory-fd>`. Windows has no equivalent way to give an arbitrary third-party `rg.exe` process an already validated ServerFS directory HANDLE as its working-directory capability without falling back to pathname resolution.

The Windows backend therefore implements the narrow existing `search_text` contract itself:

- literal/fixed-string search only;
- optional file glob;
- case-sensitive toggle;
- UTF-8/text behavior compatible with the current contract;
- per-file size ceiling;
- wall-clock deadline;
- global result limit and real early stop;
- line number and matched line text;
- hidden/deny/reserved policy filtering;
- no reparse traversal.

This is not an attempt to reimplement ripgrep as a general regex engine.

Linux continues using ripgrep unless a separate future change demonstrates a reason to replace it.

Search compatibility tests must compare important edge cases against the released Linux behavior so the two backends do not silently become different products.

## 18. Revision design

The revision contract remains:

> opaque token representing the object identity and relevant metadata observed by ServerFS.

Windows revision material should include at least:

- volume identity;
- 128-bit file ID;
- object type/attributes relevant to ServerFS semantics;
- size;
- last-write/change metadata available from the handle;
- link count where available.

The exact tuple is frozen only after a Windows probe confirms which metadata changes reliably reflect the conflicts ServerFS needs to detect.

Properties:

- stable for an unchanged object;
- changes when content or relevant metadata changes;
- distinguishes replacement objects even when a pathname is reused;
- never exposes raw identity data;
- hashed into the existing `v1:...` representation unless a format change is demonstrably required.

If a filesystem cannot provide the identity guarantees required by this contract, v0.10.0 fails that workdir closed rather than downgrading optimistic concurrency silently.

## 19. Mutation and atomic publication

Mutation support ships only after read-only traversal is accepted.

### 19.1 Create

Create semantics remain:

- target must not already exist;
- parent must already exist;
- write same-directory reserved temp object;
- flush data;
- publish atomically under the final name without overwrite;
- clean temp on every failure;
- compute revision after publication.

Direct “open final path then stream bytes into it” is not acceptable because concurrent readers could observe partial content.

### 19.2 Replace/edit

Edit/binary overwrite semantics remain:

- open and validate the existing regular file;
- reject stale `expected_revision`;
- reject unsupported multi-hardlink/metadata cases;
- create same-directory replacement temp;
- write complete content;
- preserve supported Windows metadata;
- flush;
- revalidate the expected target identity/revision immediately before publication;
- publish by a HANDLE-confined native rename/replace operation;
- compute returned revision from the published replacement.

The preferred primitive is a handle-based `SetFileInformationByHandle` rename/replace path (for example the appropriate `FileRenameInfo`/`FileRenameInfoEx` contract) rather than a path-only replacement API. Exact flags are an implementation proof item: they are not guessed in this plan.

If no handle-confined primitive can satisfy the required create-only/replace atomicity on the supported Windows/NTFS baseline, the corresponding mutation does not ship.

### 19.3 Delete

Delete remains:

- revision guarded where currently required;
- regular-file only for `delete_file`;
- empty-directory only for `delete_directory`;
- no recursive delete;
- no “force” option;
- reparse objects rejected.

### 19.4 Concurrency boundary

Windows and Linux make the same concurrency claim. ServerFS-coordinated mutations are serialized by the process-global mutation lock; a held target object is revision/identity checked again immediately before publication or deletion; and publication itself is atomic. Neither backend claims an atomic compare-and-swap against arbitrary non-cooperating same-user processes across the final-check-to-commit interval: POSIX rename and ordinary Windows rename do not accept an expected destination file ID or revision as a commit condition. This is the existing Linux `mutations.py` boundary as well as the Windows boundary; neither may be described as providing the stronger guarantee.

Windows should narrow this interval where practical. Mutation target handles should use restrictive sharing that denies new ordinary WRITE and DELETE/rename sharing while the target is held, remain open through publication, and receive the final name-relative identity/revision check immediately adjacent to commit. Replacement uses a HANDLE-relative `FileRenameInformationEx` or equivalent operation with the required atomic replacement semantics and no pre-delete. Sharing-mode hardening is defense in depth only: it is not proof against every possible POSIX-style rename primitive or an arbitrary hostile same-user process, and it does not remove the final-check-to-commit limitation.

Phase D1 implementation note (measured on WorkPC and Windows CI NTFS): delete-style targets, created directories and every temp are held with `FILE_SHARE_READ` only — new external WRITE opens and new DELETE opens (plain delete and path rename both require DELETE access) are refused while the handle lives. Replacement targets must be held with `FILE_SHARE_READ | FILE_SHARE_DELETE`: the atomic `FileRenameInformationEx` replacement cannot land on a destination whose open handles do not grant `FILE_SHARE_DELETE`, so the strict read-only mask would deadlock our own publication. Replacement therefore refuses new external WRITERS across the whole held window; an external delete or rename that exploits the retained DELETE share is caught by the name-relative final gate. ServerFS helper/gate opens keep the full sharing mask because NT sharing is checked in both directions: a new open must also cover the access already held.

## 20. Windows metadata preservation

Windows metadata is not mapped to POSIX fields.

The Windows backend must explicitly classify metadata into:

1. **must preserve or fail**;
2. **safe to recompute/inherit**;
3. **unsupported for writable v0.10 workdirs**.

At minimum the design review must cover:

- DACL/security descriptor behavior;
- standard file attributes;
- creation/write timestamps where the current tool contract requires preservation;
- compression;
- encryption/EFS;
- sparse-file state;
- named streams / Alternate Data Streams;
- hard links.

Conservative initial policy:

- multiple hard links remain unsupported for replacement;
- request paths containing `:` are rejected so ADS cannot be addressed directly;
- if replacing a file would silently discard an existing named stream or other significant metadata that the backend does not yet preserve, the mutation fails before publication;
- EFS or other semantics that cannot be safely preserved in the first release may be read-only/unsupported for mutation.

The acceptance suite, not optimistic documentation, decides which metadata classes are writable in v0.10.0.

Replacement metadata inspection and copying are handle-only. The implementation must fail closed before publication when it cannot establish that the target's named streams and other significant NTFS state are preserved. The ordinary edit contract includes supported basic metadata and owner/group/DACL when safely available without privileged SACL access; it does not claim SACL preservation unless SACL data was actually obtained and copied.

Phase D1 keeps the public Windows `v1:<16hex>` revision material frozen. Replacement
captures a separate internal preservation snapshot from the held target handle and
fingerprints its basic metadata, owner/group/DACL descriptor and unsupported-state
classification. The fingerprint never crosses PyO3. Immediately before publication,
the destination is reopened relative to the same parent and both public revision/object
identity and this private fingerprint must still match. A DACL-only change therefore
conflicts even though it does not change the public revision. The final gate narrows the
race but is not an atomic compare-and-swap: a non-cooperating writer can still race
between the gate and the relative rename.

The initial D1 preservation matrix is deliberately explicit: copy handle-readable basic
timestamps and supported DOS attributes plus DACL; require temporary-file owner/group
to already match the original and refuse otherwise. Do not request or claim audit-SACL
preservation. Refuse named streams, EAs, object IDs, multiple hard links, reparses,
sparse/compressed/encrypted/integrity/offline/cloud and unknown attribute state when
their loss is detectable. A failed or unavailable handle-only query is also a refusal,
not evidence that the feature is absent. `FlushFileBuffers` flushes the temporary file
handle before publication; D1 does not claim a directory-entry fsync equivalent or
power-loss durability for the subsequent rename. File deletion marks the verified
object handle for deletion; if an external writer renames that object after the final
name gate, deletion follows the object to its new name rather than deleting an
unverified replacement.

## 21. Error normalization

Windows native errors map to the existing agent-recoverable ServerFS vocabulary wherever semantics match.

Examples:

- path missing -> `PATH_NOT_FOUND`;
- parent missing -> `PARENT_NOT_FOUND`;
- access denied -> `ACCESS_DENIED` / existing mutation-specific code;
- reparse object -> `SYMLINK_NOT_ALLOWED` for parent traversal compatibility or a clearly documented additive `REPARSE_POINT_NOT_ALLOWED` if needed;
- target exists -> `PATH_ALREADY_EXISTS`;
- stale object -> `REVISION_CONFLICT`;
- unsupported type -> `UNSUPPORTED_FILE_TYPE`;
- unsupported metadata preservation -> `METADATA_PRESERVATION_FAILED`;
- resource exhaustion -> `RESOURCE_EXHAUSTED`.

Raw NTSTATUS, Windows error strings, absolute paths, SIDs, file IDs and volume IDs do not escape through MCP errors.

A new error code is added only when collapsing the condition into an existing code would make correct agent recovery impossible.

## 22. Read-only defense in depth

The Linux Docker deployment currently has two independent write barriers:

1. ServerFS authorization;
2. read-only bind mount.

A native Windows current-user process cannot truthfully claim the same container boundary.

v0.10.0 therefore defines its defense in depth honestly:

1. Python workdir authorization;
2. Rust read-only vs writable session capability;
3. minimum native HANDLE access rights;
4. Windows filesystem ACL enforcement;
5. no generic path/open primitive exported to the MCP layer.

This protects against input-driven logic mistakes and overbroad native calls. It is not an arbitrary-code-execution sandbox.

A future optional Windows sandbox/AppContainer design can be investigated separately; it is not required to call v0.10.0 native.

## 23. Native file ingress is deferred

The Linux file-ingress sidecar is a real network/filesystem isolation boundary:

- network egress;
- no workdir mounts;
- no tunnel credentials.

Simply turning it into a second same-user Windows Python process would not preserve that security property.

Therefore Windows v0.10.0 supports native binary transfer from explicit base64 payloads but does not claim support for ChatGPT/OpenAI remote `fileParams` ingress.

The existing Linux sidecar remains unchanged.

A later Windows-native ingress design must first establish an equivalent isolation boundary before the feature is enabled.

## 24. Packaging and dependency model

### 24.1 Python package remains primary

`serverfs-mcp` remains the Python product package and keeps the existing packaging approach where practical.

Do not convert the whole project to a Maturin build merely because one platform backend is Rust.

### 24.2 Separate native wheel

Distribution shape (implemented by Phase E1):

~~~text
serverfs-mcp
serverfs-windows-native
~~~

The Windows native wheel is selected only on Windows and imported behind the filesystem backend interface.

End users receive a prebuilt wheel.

They do **not** need:

- Rust;
- Cargo;
- Visual Studio Build Tools;
- Windows SDK;
- pywin32.

### 24.3 PyO3 ABI strategy

PyO3 supports prebuilt extension wheels and stable ABI (`abi3`) builds. The project should prefer the narrowest ABI strategy that keeps the supported Python matrix small and reliable.

Reference:

https://pyo3.rs/main/building-and-distribution

Phase E1 adopts an abi3-py312 wheel and verifies the built artifact on CPython 3.12 in a clean wheel environment. Phase F must either install/import/smoke the same wheel on at least one newer CPython minor (3.13+) or explicitly bind the v0.10 Windows native support statement to Python 3.12 in the release documentation.

### 24.4 Rust dependency budget

Target direct Rust dependencies:

- `pyo3`;
- `windows-sys` or equivalent narrow Microsoft binding package.

Any additional direct crate must have a concrete correctness/performance reason documented in the change that introduces it.

For example, Windows literal search may justify a small well-audited matcher dependency if reproducing Unicode case-folding correctly is materially safer than a hand-rolled implementation. “Zero crates” is not a goal when it decreases correctness.

## 25. OpenAI tunnel-client distribution

The official tunnel-client currently publishes Windows amd64/arm64 artifacts and checksum manifests.

Release source:

https://github.com/openai/tunnel-client/releases

v0.10.0 should support keeping a pinned tunnel-client binary under a project-managed data/bin directory rather than requiring a machine-wide install.

Implemented in Phase E2 by `serverfs_mcp/tunnel_bootstrap.py` behind the
`serverfs bootstrap tunnel-client` CLI command. The delivered shape:

1. resolve the pinned release (pinned tag constants);
2. download the exact platform archive from the official distribution
   (https-only, GitHub release-asset host allowlist, redirects validated);
3. verify the published SHA-256 (double anchor: the `SHA256SUMS.txt`
   manifest itself must match a pinned digest before it is trusted, then
   the archive is re-hashed against the manifest entry);
4. extract the runtime member set top-level-only (the official zips carry
   `cloudflared.exe` plus `cloudflared-manifest.json` beside the client;
   provenance `.spdx.json`/`-licenses.txt` assets are dropped, traversal
   members are refused);
5. store it below the user-owned ServerFS data directory
   (`SERVERFS_DATA_HOME` override; `%LOCALAPPDATA%\ServerFS\bin` on Windows);
6. never modify global PATH.

`serverfs bootstrap native-wheel --url … --sha256 …` applies the same
verify-then-store discipline to the published wheel; installation remains an
explicit operator step.

The filesystem service itself must remain runnable/testable without tunnel-client; tunnel-client is connectivity infrastructure, not part of the filesystem security kernel.

## 26. CLI and native operations

A minimal stdlib-`argparse` CLI is delivered (`serverfs_mcp/cli.py`); no additional CLI framework was introduced.

Target commands:

~~~text
serverfs serve --config serverfs.toml
serverfs doctor --config serverfs.toml [--env-file .env] [--tunnel-client PATH]
serverfs tunnel --config serverfs.toml --env-file .env \
  [--tunnel-client C:\\tools\\tunnel-client.exe] --tunnel-id tunnel_... \
  --api-key-file C:\\Users\\me\\.config\\serverfs\\api-key
serverfs bootstrap tunnel-client | native-wheel --url ... --sha256 ...
~~~

Native `serve` and `tunnel` are Windows-only in v0.10 and fail closed on
other platforms. Native default transport is stdio. Linux keeps its existing
Docker deployment; Linux-native no-Docker service may reuse this work later.
`serve` and `tunnel` are implemented and regression-tested (the `tunnel` path
wires the §8.2 `file:`-reference credential boundary and the sanitizer
supervisor); `tunnel` now defaults to the bootstrapped client when
`--tunnel-client` is omitted. `serverfs doctor` is implemented in full
(Phase E2, `serverfs_mcp/doctor.py`); the reported list below is the
acceptance contract it satisfies:

- ServerFS version;
- Python version;
- platform/architecture;
- native backend import/version;
- config parse result;
- each workdir alias and access mode;
- root open success;
- supported filesystem type;
- root reparse status;
- basic read/write capability assessment without mutating user files;
- tunnel-client presence/version when configured;
- clear “not supported” reasons.

Doctor output must not print secret values or expose host roots in contexts where logs may be agent-visible. A deliberate local human-facing verbose mode may show configured roots, but it must be clearly separated from MCP/audit output.

## 27. Logging and stdout discipline

Native stdio makes stdout protocol-critical.

Rules:

- MCP frames only on stdout;
- all structured ServerFS logs remain on stderr;
- Rust native backend never prints to stdout/stderr directly during normal operation;
- Rust errors return structured values/exceptions to Python;
- panic hook must not dump secrets or paths into protocol output;
- tunnel-client diagnostics remain separate from MCP frames.

The existing JSON audit contract remains unchanged where possible.

## 28. Linux compatibility requirements

Phase A must prove that refactoring toward `FilesystemBackend` does not change released Linux behavior.

Linux v0.9.0 invariants remain authoritative:

- same filesystem tool names and schemas;
- same error codes;
- same hidden/deny behavior;
- same revisions for unchanged Linux implementation unless a deliberate migration is documented;
- same ripgrep behavior;
- same Docker compose deployment;
- same Streamable HTTP transport security;
- same file-ingress sidecar;
- same Agent Bridge integration and writer lease;
- same 16-slot environment adapter for existing deployments.

Do not “clean up” Linux behavior just to make Windows code look symmetric.

## 29. Test architecture

### 29.1 Contract tests

Extract/extend backend-neutral tests that exercise the same MCP surface against each available backend.

Contract cases include:

- path normalization;
- hidden policy;
- deny policy;
- reserved names;
- read limits;
- list pagination;
- find limits;
- search limits;
- text/binary distinction;
- revisions;
- create/edit/delete semantics;
- error redaction;
- audit redaction.

### 29.2 Windows native security tests

Required Windows cases include:

- ordinary file/directory traversal;
- `..` escape attempts;
- `\` and absolute/drive/UNC/NT namespace attempts;
- colon/ADS syntax;
- DOS device names;
- trailing dot/space ambiguity;
- final symbolic link;
- parent symbolic link;
- junction;
- mount point;
- other available reparse tags;
- case variants of deny/reserved names;
- hard links;
- concurrent rename of parent;
- concurrent replacement of final target;
- concurrent host edit during revision-guarded mutation;
- target replacement between check and publish;
- ACL-denied object;
- read-only attribute;
- very deep paths;
- long paths beyond legacy MAX_PATH where supported;
- Unicode names/content;
- files that vanish during enumeration;
- large directory breadth under bounded resource use;
- stale revision;
- metadata-preservation failure;
- temp cleanup after every injected failure point.

### 29.3 Search tests

Search must cover:

- literal match;
- no regex interpretation;
- line numbers;
- UTF-8;
- case sensitivity toggle;
- cross-chunk match boundary;
- max-file-size skip;
- global result limit;
- timeout/early stop;
- hidden/deny/reserved paths;
- reparse directory never traversed;
- file disappearing during scan;
- compatibility with representative Linux `rg` output semantics.

### 29.4 Fault injection

The Rust backend should expose test-only fault injection or internal units sufficient to exercise:

- short write;
- flush failure;
- metadata copy failure;
- rename/publish failure;
- target identity change;
- temp cleanup;
- allocation/handle failures.

Production builds expose no fault-injection controls.

### 29.5 Real filesystem acceptance

Mocked Win32 calls are not sufficient for release.

The release requires tests against a real Windows 11 NTFS filesystem, including concurrency/reparse behavior.

## 30. CI and build matrix

Minimum CI gates:

### Existing Linux root gate

Retain the current project gate required by the files changed.

### Windows Python/native gate

On a real Windows GitHub Actions runner or equivalent:

~~~text
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest <platform-neutral + Windows-safe tests>
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
maturin build --locked <wheel args>
install built wheel into a clean uv environment
run native MCP/doctor smoke tests
run Windows NTFS security acceptance subset
~~~

Release CI must additionally test the wheel as an artifact rather than only testing an in-tree debug build.

### Architecture

Windows x64 is mandatory for v0.10.0 release.

Windows arm64 becomes supported only when build and runtime acceptance evidence exists.

## 31. Development phases

### Phase A — Platform boundary refactor

**Status: CLOSED.** Contract-only `backends.py` seam, kernel-free product imports (fdio/fcntl unimportable constraint test), `concurrency.py`/`binary_payload.py`/`errors.py` splits and the full `WorkdirSession` protocol landed via PR #13.

Objective: create the backend/configuration seams without changing Linux behavior.

Work:

- define `FilesystemBackend` / session contract;
- move revision ownership behind the backend;
- separate platform-neutral virtual-path policy from Linux path mechanics;
- remove `container_path` and mandatory numeric slot assumptions from the core model;
- retain a legacy Compose/env adapter;
- keep Linux implementation using existing `fdio.py`;
- add native TOML config parser;
- add minimal CLI skeleton.

Exit criteria:

- released Linux filesystem behavior unchanged;
- existing Linux tests green;
- existing Docker config/build green;
- no Windows feature claimed yet;
- no Agent Bridge behavior changed.

Closure addendum (2026-10-02, import-safety/protocol pass):

- `backends.py` now carries only `BackendError`, `TextPage`, the two Protocols and
  a lazy `get_backend`; the Linux session implementation moved to
  `linux_backend.py`, the single module-level `fdio` consumer in the MCP product
  path.
- Platform-neutral extractions: `concurrency.py` (`mutation_lock`),
  `binary_payload.py` (`BinaryTransferError`, `BinaryRead`,
  `decode_base64_payload`), `errors.py` (`MutationError` base plus the three
  Agent-lease error classes, so `agent_leases.py` stays Linux-only).
- `SearchTimeout` was normalized to `BackendError("SEARCH_TIMEOUT", ...)` raised
  by the search kernel; the tool layer maps it through the existing
  BackendError branch, so the agent-visible code is unchanged.
- `tools.py` no longer imports `agent_leases`, `mutations`, `search` or `fdio` at
  module level; the Unix lease manager loads lazily and only on the
  Agent-enabled mutation path. `main.py` imports `agent_tools` only when the
  Agent surface is actually enabled.
- `WorkdirSession` declares `create_binary_file`/`replace_binary_file` and every
  session method has an explicit return type; `tests/test_backends.py` enforces
  protocol coverage against the Linux session and the fake session.
- `tests/test_import_safety.py` proves the product layers import with
  `serverfs_mcp.fdio` and `serverfs_mcp.agent_leases` force-blocked (measured
  natively on Windows, where `fcntl` cannot exist at all, and in a Linux
  container).

Phase B closure decision: `WindowsBackend` retains one process-lifetime
session/root capability per workdir. Tool calls reuse that retained capability
and do not reopen the Windows root HANDLE from the configured pathname.

### Phase B — Windows native read/security kernel

**Status: CLOSED.** NtCreateFile HANDLE-relative traversal prototype hardened (strict `X:\` root namespace, trailing-separator fail-closed, per-role Send/Sync proof), PyO3 boundary with no raw HANDLE, process-lifetime retained roots and cached `WindowsWorkdirSession`, backend-owned `v1:<hex>` revisions frozen by real NTFS probes, and the stat/list/read-text/read-binary/validate-directory channels closed with the §30 Windows CI gate (PR #13/#15-era acceptance).

Objective: prove the Windows security primitive before broad implementation.

Work:

- create Rust/PyO3 crate;
- owned HANDLE abstraction;
- trusted root acquisition;
- HANDLE-relative component open prototype;
- reparse detection/rejection;
- native stat/object identity;
- directory enumeration;
- read;
- Windows error normalization;
- real NTFS tests for traversal and concurrent rename.

Exit criteria:

- no path-string TOCTOU fallback;
- parent and final reparse tests fail closed;
- root/session identity remains stable across host-side rename;
- no handle leaks under stress/failure tests;
- stat/read/list MCP tests pass on Windows.

Phase B closed only after these invariants were demonstrated on real Windows/NTFS and in the Windows CI gate; later phases must not weaken them.

### Phase C — Native find/search

**Status: CLOSED.** HANDLE-relative recursive find walk and a native literal searcher (no `rg.exe`), frozen against a Linux-rg black-box parity table (glob pathname rules, 64 KiB-buffer binary suppression, undecodable lines never match, CRLF `\r` retained, `.git/.hg/.svn` always excluded from search but not from find), timeout/limit early-stop on wide trees, and a Linux/Windows agent-visible contract matrix (PR #18).

Objective: complete the read-only six-tool surface without ripgrep.

Work:

- recursive HANDLE-relative walk;
- find glob behavior;
- literal search engine;
- timeout/limit/size handling;
- compatibility tests against Linux behavior;
- bounded resource/handle usage.

Exit criteria:

- six read tools pass Windows MCP E2E;
- reparse trees cannot escape or be traversed;
- search early-stop/timeout are demonstrated;
- no `rg.exe` dependency.

### Windows connectivity prerequisite brought forward before Phase D

**Status: DELIVERED.** Windows-only native MCP stdio serving (`serverfs serve`), the
Windows native tunnel-client launcher (`serverfs tunnel`) using the frozen four-field
HTTP proxy contract, the `file:`-reference API-key boundary, and the secret-isolating
supervisor that strips the tunnel environment before spawning the ServerFS child. This
was merged and regression-tested (PR #19) before Phase D mutations; it does not complete
Phase E. Prebuilt-wheel distribution, `serverfs.toml.example`, full doctor/root health
checks, tunnel-client bootstrap and release documentation and closure remain Phase E/F
work.

### Phase D — Windows native mutation and binary transfer

**Status: CLOSED (D1 + D2).** D1 delivered the native mutation kernel: HANDLE-relative
create/replace/delete/mkdir/rmdir over the retained root with no request-derived host
path, same-directory high-entropy reserved temps, exact write plus `FlushFileBuffers`,
create-only atomic publication and atomic no-pre-delete `FileRenameInformationEx`
(`REPLACE_IF_EXISTS|POSIX_SEMANTICS`) replacement, the final name-relative
identity/public-revision/private-fingerprint gate, multi-hardlink and unsupported-NTFS
fail-closed preservation (ADS/EA/object-ID/sparse/compressed/encrypted/integrity;
query failure is a refusal, never absence), handle-only basic-metadata + owner/group/DACL
preservation with `SE_DACL_AUTO_INHERITED` masked as recomputable, no SACL claim,
kernel-side read-only enforcement and the approved §19.4 sharing boundary
(delete-style targets and temps held `FILE_SHARE_READ`; replacement targets
`FILE_SHARE_READ|FILE_SHARE_DELETE` because atomic publication cannot land on a
destination whose open handles lack DELETE share — defense in depth, never an
external-CAS claim). Deterministic final-gate fault hooks exist only under
`#[cfg(test)]`; EA and object-ID fail-closed paths carry positive NTFS fixtures or
pinned API-error evidence. D2 wired the seven `WorkdirSession` mutation channels to the
kernel without changing the security model (except adding `FILE_READ_DATA` to the
delete-target open so Windows keeps the Linux "unreadable file is not deletable"
product property): v0.9 result models, backend-side code mapping
(`PARENT_NOT_FOUND`, `WRITE_TOO_LARGE`), `WORKDIR_READ_ONLY` precedence, reentrant
`mutation_lock`, and a single shared kernel-free `mutation_text` module so both
platforms apply identical text/edit/BOM/NUL semantics. Platform-visible documented
differences: reparse mutation targets report `REPARSE_POINT_NOT_ALLOWED` (Windows)
where Linux reports `SYMLINK_NOT_ALLOWED`, and Windows directory revisions must not be
assumed to move when children change — `delete_directory` correctness rests on the
physical emptiness scan, not revision drift. Real-NTFS MCP E2E covers atomicity under
concurrent readers, stale revisions, torn-write absence, temp invisibility through all
channels, retained-root rename survival and bounded handle counts.

Objective: add safe writable workdirs.

Work:

- same-directory temp creation;
- durable write/flush;
- create-only atomic publication;
- revision-guarded replace;
- delete/rmdir;
- hard-link handling;
- Windows metadata preservation policy;
- binary base64 create/overwrite/download;
- exhaustive cleanup/fault tests.

Exit criteria:

- atomicity demonstrated with concurrent readers;
- stale revisions cannot commit;
- unsupported metadata fails before publication;
- no partial destination content;
- no temp artifacts exposed through tools;
- all mutation MCP tests pass on real NTFS.

### Phase E — Native packaging and release operations

**Status: E1 CLOSED; E2 implemented, pending review.** `native/windows/pyproject.toml`
+ maturin produce the separate `serverfs-windows-native` `cp312-abi3-win_amd64` wheel
(Cargo owns the version; root package stays Hatchling); CI builds it and runs the native
+ backend + MCP E2E suites in a clean venv where the wheel is the only provider of the
native module, and uploads the wheel artifact. `Private :: Do Not Upload` guards
accidental PyPI publication until the release decision.

E2 delivered (WorkPC-verified):

- `serverfs.toml.example` documenting the full native TOML schema (parsed by
  the real loader in a regression test);
- full `serverfs doctor` (`serverfs_mcp/doctor.py`): version/Python/platform,
  native backend import/distribution-version, config parse, per-workdir root
  open through the backend seam, filesystem class via volume resolution —
  fail closed per §33: local NTFS is the only OK verdict, while network
  shares, FAT/exFAT/ReFS and undeterminable storage classes are `FAIL` with
  a non-zero exit — configuration-level reparse pre-check beside the kernel's own
  refusal, a real policy-filtered root listing as the read probe, a
  non-mutating write-capability probe (Win32 `CreateFileW(FILE_ADD_FILE)` /
  POSIX `access`), tunnel-client presence/version and bootstrapped-copy
  discovery, and redacted proxy status with DNS/TCP reachability — secrets
  and the derived proxy URL never appear; exit 0/1/2 semantics; stderr-only;
- `serverfs bootstrap tunnel-client` (`tunnel_bootstrap.py`) per §25, pinned
  to the official v0.0.15 release with double-anchored SHA-256 verification
  (pinned manifest digest -> manifest entry -> archive re-hash),
  traversal-safe top-level extraction of the runtime member set, user-owned
  data directory, no PATH changes; `serverfs tunnel` now auto-discovers the
  bootstrapped client; real-network run on WorkPC installed and executed the
  verified binary;
- `serverfs bootstrap native-wheel --url --sha256`: verify-then-store
  channel for the published wheel, installation staying an explicit step;
- `.github/workflows/wheel-release.yml`: tag-triggered publication that
  builds BOTH release wheels (maturin native + hatchling product), gates
  each wheel's version against the tag by reading the authoritative
  ``Version:`` from each wheel's ``*.dist-info/METADATA`` (never filename
  segment positions, which misread ``cp312``/``py3``), runs a two-wheel
  clean-install acceptance in a fresh venv with no editable checkout and no
  PYTHONPATH (module-provenance assertion included), then attaches both
  wheels to the tag's GitHub Release and rewrites its notes through the
  paired-marker managed block in `deployment/native/wheel_release.py`:
  maintainer text before and after the block survives, re-runs replace the
  block in place, an absent pair appends exactly one, and any orphan or
  duplicate marker fails closed without touching the release. The helper's
  version normalization and full notes matrix are unit-tested in
  `tests/test_wheel_release.py`, executed by the Linux gate on every push;
  the actual publication run belongs to the maintainer's release decision;
- clean-install closure evidence on WorkPC: fresh `uv venv` (CPython 3.13)
  with only the two release-shaped wheels installed -> `serverfs doctor`
  exit 0 and a full stdio MCP session (create -> stat -> edit -> stat,
  exact CRLF bytes, consistent revision chain). This doubles as the abi3
  cross-minor evidence item (install/import/smoke on 3.13);
- README "Windows Native Deployment", bootstrap/upgrade guidance and the
  Windows native development section.

Deliberate uv-dependency shape: a `[tool.uv.sources]` URL entry cannot land
before the release asset exists, because `uv.lock` is universal — a
not-yet-downloadable URL would break Linux `uv sync --frozen` and the
Windows CI job's resolution. The post-publication switch to a URL-pinned
source (or a release-time `uv add`) remains a release-checklist step; the
two-command `uv pip install`/bootstrap path is the shipped closure.

Objective: make the implementation installable and usable without a developer toolchain.

Work:

- prebuilt Windows x64 wheel (E1; publication channel landed by E2, run pending release tag);
- uv install closure (E2: two-wheel clean-venv path + verified native-wheel bootstrap);
- `serverfs.toml.example` (done);
- full `serverfs doctor` root/backend/filesystem/tunnel/proxy health checks (done);
- pinned project-local tunnel-client bootstrap/profile (done);
- clean install and upgrade guidance (done);
- Windows documentation (done; website mirror may follow).

Exit criteria:

- clean Windows machine/environment needs no Docker/WSL/Rust/MSVC SDK (demonstrated in the WorkPC clean-venv closure);
- the usable native backend installs from a prebuilt wheel via `uv` (URL-source lockfile wiring follows publication);
- ChatGPT tunnel E2E succeeds over stdio (Phase F live-network acceptance);
- ServerFS child does not receive the Control Plane API key value;
- no localhost MCP listener exists in the default native profile.

### Phase F — Windows acceptance and release closure

**Status: IN PROGRESS — executed evidence lives in
`docs/phase-f-acceptance-2026-10.md` (clean dual-wheel installs on CPython
3.12.10 and 3.13.3, long/deep/Unicode suite, proxy items 1–10 plus a real
control-plane 401 through the operator's HTTP proxy, sanitized child-env
probe, and the acceptance-found launcher fix for the health bind on
127.0.0.1:8080). Remaining: maintainer live ChatGPT E2E per the runbook in
that document, then the version bump and publication sequence in
`docs/phase-f-release-checklist.md`.**

Objective: decide whether v0.10.0 may claim Windows support.

Required acceptance:

- Windows 11 x64;
- local NTFS;
- read-only and writable workdirs;
- reparse/junction attack matrix;
- concurrent host edit/rename matrix;
- metadata cases;
- large/deep/Unicode paths;
- clean-install wheel;
- tunnel E2E;
- restart/recovery of tunnel-created MCP child;
- full proxy acceptance over real network paths: (1) direct/no-proxy, (2) HTTP proxy
  without authentication, (3) HTTP proxy with username/password, (4) proxy credentials
  containing URL-reserved characters (percent-encoding), (5) invalid credentials
  producing a redacted actionable error, (6) unreachable proxy, (7) proxy dropping an
  established connection with recovery/restart, (8) proof the ServerFS child/container
  environment contains neither proxy credentials nor the Control Plane API key value,
  (9) proof logs, doctor output and MCP errors contain no proxy or tunnel secret
  values, (10) proxy-disabled deployment behaviorally identical to the direct path;
- Python minor-version evidence for the abi3 claim: install/import/smoke the same
  `cp312-abi3` wheel on at least one newer CPython minor (3.13+), or explicitly bound
  v0.10 Windows native support to 3.12 in the release documentation. E2 development
  evidence already shows the wheel installing and serving a full stdio MCP session
  (doctor + create/stat/edit chain) on CPython 3.13.3 in a clean uv venv; Phase F must
  repeat this against the exact release-built wheel and record it.
- documentation and website alignment.

Release only after all required acceptance evidence is recorded.

## 32. Public compatibility and additive changes

Expected public filesystem tool count remains unchanged by default.

Additive public changes may include:

- `type="reparse_point"` in entry/stat output;
- a new recoverable Windows-specific error only if required for correct recovery.

Configuration/CLI changes are deployment-facing, not agent-facing.

v0.10.0 must not alter Agent MCP tools or Bridge RPC merely to prepare for future Windows Agent support.

## 33. Supported and unsupported Windows storage

### GA target

- Windows 11 x64;
- local NTFS;
- ordinary current-user accessible files/directories.

### Must be detected/tested before claiming support

- ReFS / Dev Drive;
- SMB/UNC network shares;
- mapped network drives;
- OneDrive/cloud placeholders;
- filesystem virtualization/filter products;
- removable filesystems;
- FAT/exFAT.

A filesystem being mountable/openable is not enough. It is supported only after its identity, reparse, atomic rename, flush and revision semantics pass the acceptance suite.

Unknown storage must fail clearly or run only in an explicitly documented reduced mode; it must never silently inherit the NTFS security claim.

## 34. Performance principles

Security correctness wins over micro-optimization.

Still, the native backend should avoid unnecessary crossings:

- root/session handles retained;
- high-level read/list/find/search calls cross Python/Rust once per operation rather than once per path component;
- native search streams/limits internally;
- directory handles are bounded by traversal depth/algorithm;
- no persistent content cache or index;
- no hidden background scanner.

Performance acceptance should record representative:

- read latency;
- large directory listing;
- recursive find;
- literal search;
- create/edit latency;

but no release requirement is expressed as an arbitrary throughput target until a real baseline is measured.

## 35. Security/threat-model statement for Windows

Windows Native ServerFS protects against:

- untrusted MCP path input;
- traversal;
- symlink/junction/reparse redirection;
- hidden/credential policy bypass;
- stale revision writes;
- partial publication;
- unsupported file types;
- accidental metadata loss within the declared mutation contract;
- secret/path leakage through normal MCP errors/audit logs.

It does not claim to protect against:

- arbitrary native code execution inside the ServerFS process;
- a malicious process running as the same Windows user with unrestricted access to the same workdir;
- administrator/kernel compromise;
- filesystem/filter behavior outside the tested support matrix.

This distinction must remain explicit in documentation.

## 36. Future compatibility

The architectural output of v0.10.0 should make later work simpler without implementing it early.

### Windows Agent Bridge, future release

Later work can add platform-specific:

- local IPC;
- Agent process lifecycle;
- writer leases;
- Job Objects;
- native runtime adapters.

It must build on the native workdir identity/model rather than restoring fixed Docker slots as a core concept.

### macOS native backend, future release

macOS should be evaluated as a native platform, not implemented through Docker by default.

Likely shared ideas:

- Python product layer;
- native TOML config;
- stdio tunnel profile;
- backend-owned revision;
- retained root capability;
- platform-specific filesystem implementation.

Darwin may reuse POSIX concepts such as `openat` but must be validated independently for APFS/macOS semantics. Shared contract does not imply identical implementation.

## 37. External facts verified for this design

The following external capabilities were verified against current primary/official sources before freezing this plan:

1. Microsoft documents user-mode `NtCreateFile` and relative naming through `OBJECT_ATTRIBUTES.RootDirectory`:
   https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntcreatefile

2. Microsoft documents reparse-point handling and `FILE_FLAG_OPEN_REPARSE_POINT` behavior:
   https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points-and-file-operations

3. Microsoft documents `FILE_ID_INFO` with `VolumeSerialNumber` + `FILE_ID_128` for file identity:
   https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info

4. PyO3 documents prebuilt extension distribution, Maturin and stable ABI/`abi3` wheels:
   https://pyo3.rs/main/building-and-distribution

5. OpenAI tunnel-client documents `--mcp.command` / `MCP_COMMAND` stdio MCP children and the one-active-instance-per-tunnel stdio limitation:
   https://github.com/openai/tunnel-client/blob/master/docs/configuration.md

6. OpenAI tunnel-client currently publishes Windows amd64/arm64 artifacts with checksum manifests:
   https://github.com/openai/tunnel-client/releases

7. The current tunnel-client stdio implementation uses Go `exec.Command` without replacing the child environment, which is why v0.10 native deployment must not place the Control Plane API key value in an environment inherited by ServerFS:
   https://github.com/openai/tunnel-client/blob/master/pkg/mcpclient/stdio_command.go

These references establish available primitives, not implementation acceptance. Real Windows tests remain authoritative.

## 38. Completion contract for v0.10.0

v0.10.0 is complete only when all of the following are true:

1. Linux v0.9 filesystem behavior and deployment remain regression-green.
2. Windows 11 x64 runs ServerFS natively without Docker/WSL.
3. Windows filesystem access is rooted in retained native handles and request-controlled traversal is HANDLE-relative.
4. Reparse-point parents never resolve through to targets.
5. Windows read/list/find/search/stat pass MCP-level tests.
6. Writable Windows workdirs satisfy revision, atomic publication and metadata fail-closed contracts on real NTFS.
7. Windows search requires no ripgrep installation.
8. End users install no Rust/MSVC/Windows SDK toolchain.
9. The native default MCP transport is stdio through the official tunnel-client.
10. Tunnel control-plane secret values are not inherited by the ServerFS MCP child in the supported profile.
11. Native fileParams ingress remains disabled until an equivalent isolation design exists.
12. Windows documentation names the exact tested support matrix and does not imply untested ReFS/SMB/cloud support.
13. Full Linux and Windows gates are actually executed and recorded.
14. Repository docs/site are aligned to v0.10.0 before tag/release.

Until those conditions hold, Windows Native remains development/preview functionality rather than a released support claim.
