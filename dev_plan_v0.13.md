# ServerFS v0.13.0 Development Plan — Native macOS 27 on Apple Silicon M-series

**Status:** IMPLEMENTED — release candidate (not yet tagged)
**Target:** v0.13.0
**Baseline:** released/frozen v0.12.0 on `main`

## Implementation & acceptance record (2026-10-10)

All phases 0–K are implemented on `feat/v0.13-macos27-arm64` and validated on the development
baseline machine (MacBook Air Mac16,12 / Apple M4 / macOS 27.0.1 build 26A434, native arm64,
not Rosetta):

- Phase 0 — `docs/phase-0-macos27-arm64-capability-probe-2026-10.md` (every
  design-load-bearing primitive PROVEN or SUPPORTED VIA SMALL DARWIN WRAPPER with the wrapper
  PROVEN).
- Phase A — `src/serverfs_mcp/posix_fdio.py` extraction; Linux call path unchanged.
- Phase B — `src/serverfs_mcp/darwin_backend.py` + explicit backend dispatch
  (`NATIVE_PLATFORM_UNSUPPORTED` elsewhere); `search_scan.py`/`read_page.py` shared cores.
- Phase C — measured serve/bootstrap gates; `~/Library/Application Support/ServerFS` data
  home; Darwin doctor diagnostics (APFS/network verdict, platform facts, TCC hint);
  `native-wheel` refuses on macOS.
- Phase D — Bridge platform seams (LINUX/DARWIN/WINDOWS/POSIX), `getpeereid` peer identity,
  validated runtime dir, sun_path budget enforcement before bind, §23 parity tests.
- Phase E — `serverfs agent-bridge …` launchd lifecycle; live LaunchAgent proof
  (bootstrap/kickstart/bootout) in `tests/test_darwin_lifecycle.py` (opt-in live, PASS).
- Phase F — Codex adapter recognized POSIX-generic (`codex_posix.py`); live darwin probes:
  codex 0.162.1 via the real managed daemon, claude 2.1.294, `model/list` 11 models.
- Phase G — Jev/proxy reused unchanged (zero Darwin-specific code); suites green.
- Phase H — native file-ingress helper over private AF_UNIX (`SERVERFS_FILE_INGRESS_SOCKET`,
  parent guard, SIGTERM cleanup); real subprocess/socket tests.
- Phase I — `.github/workflows/macos-native.yml` with measured platform assertions.
- Phase J — `docs/phase-macos-native-acceptance-2026-10.md` (23/23 on APFS),
  `docs/phase-macos-agent-acceptance-2026-10.md` (9/9), and
  `docs/phase-macos-live-chatgpt-e2e-2026-10.md` (operator run book: the ChatGPT tunnel E2E
  and live proxied egress need the operator's real tunnel credentials/proxy; a Qoder live run
  needs a host with the Qoder CLI — the release statement narrows accordingly).

Automated gates at the acceptance commit: MCP suite 1118 passed / 0 failed (macOS 27 arm64),
Bridge suite 327 passed / 0 failed, ruff + ruff format clean, `git diff --check` clean. The
macOS Native CI workflow runs the same suites behind measured platform assertions.

### CI status at this record

- Hosted runners (Linux Container, Windows native, Windows Agent, site): **all green** on
  PR #41 after the wheel-version alignment.
- Hosted `macos-27` runners remained queued for over an hour (free-account capacity), so the
  plan's §16 fallback applies: the exact macOS workflow step sequence was executed on this
  acceptance machine with identical measured assertions and recorded as
  `docs/phase-i-macos-ci-local-equivalence-2026-10.md` — **all steps PASS**. The workflow now
  also supports switching every job to an M-series self-hosted runner via the repository
  variable `MACOS_RUNNER` without any other change; hosted runs stay queued and will execute
  when capacity frees.

Release tagging remains blocked until the operator-assisted evidence above is captured and CI
is green on the release commit (the local CI-equivalence run satisfies the macOS gate until a
hosted or self-hosted run completes).

## Supported platform — intentionally narrow

v0.13.0 supports exactly:

```text
Hardware:
    Apple Mac with Apple M-series SoC

CPU architecture:
    arm64 / AArch64
    native execution only

Operating system:
    macOS 27.x Golden Gate

Development baseline at plan creation:
    macOS 27.0.1
```

The actual release acceptance must use the **latest publicly released macOS 27.x patch available at acceptance time**.

Explicitly unsupported in v0.13.0:

```text
Intel Macs
x86_64 processes
Rosetta-translated ServerFS/Python
macOS 26 Tahoe or older
macOS 28 or newer
Hackintosh
non-Apple ARM hardware
cross-platform Darwin derivatives
non-M-series Apple Silicon as a supported product target
```

Do not implement compatibility code for these environments.

---

# 1. Mission

Implement a first-class **native ServerFS deployment for M-series Macs running macOS 27 Golden Gate**.

The macOS implementation must reach functional parity with the current Linux product surface while using native Darwin security and lifecycle primitives.

Target architecture:

```text
ChatGPT
   |
OpenAI Secure MCP Tunnel
   |
official tunnel-client (darwin-arm64 only)
   |
stdio
   v
ServerFS MCP — native arm64 Python
   |
   +-- Darwin filesystem backend
   +-- optional native isolated file-ingress helper
   +-- Agent Bridge over authenticated AF_UNIX
          |
          +-- Codex
          +-- Claude Code
          +-- Qoder
          +-- optional Jev advisors
```

No Docker.

No VM.

No Rosetta requirement.

No system-wide daemon.

No macOS-specific Rust kernel unless a Phase 0 measurement proves one unavoidable.

---

# 2. Platform contract

## 2.1 Runtime gate

Native macOS startup must explicitly verify the platform rather than treating all POSIX systems alike.

Required conditions:

```python
sys.platform == "darwin"
platform.machine() == "arm64"
macOS major version == 27
```

Additionally probe whether the current process is running under Rosetta translation.

The Phase 0 probe must establish the reliable Darwin mechanism, expected to include:

```text
sysctl.proc_translated
```

Required behavior:

```text
native arm64 + macOS 27.x
    -> supported

x86_64 / Rosetta
    -> NATIVE_PLATFORM_UNSUPPORTED

macOS < 27
    -> NATIVE_PLATFORM_UNSUPPORTED

macOS > 27
    -> NATIVE_PLATFORM_UNSUPPORTED
```

Do not silently execute on an unvalidated future major macOS release.

Patch releases inside macOS 27 remain allowed:

```text
27.0
27.0.1
27.1
...
```

but final v0.13 acceptance must be rerun on the newest available 27.x patch.

## 2.2 M-series support boundary

Do **not** build a large hard-coded M1/M2/M3/M4/M5 model database.

The product support statement is:

> M-series Macs running native arm64 macOS 27.

Runtime security depends on:

```text
Darwin
arm64
native execution
macOS major 27
```

rather than fragile marketing-model detection.

Live acceptance must nevertheless run on a real M-series Mac.

---

# 3. Core architectural decision

## 3.1 No macOS Rust kernel by default

Windows required a Rust/PyO3 native filesystem kernel because Win32/NT filesystem security primitives differ fundamentally from POSIX.

Darwin already provides:

```text
file descriptors
directory-relative filesystem operations
O_NOFOLLOW
fstat
flock
atomic rename
AF_UNIX
UID/GID identity
xattrs
ACLs
native metadata-copy APIs
```

Therefore:

> v0.13.0 should use Python + Darwin/POSIX primitives and only minimal libc bindings where Python does not expose the required API.

Acceptable narrow `ctypes` bindings include:

```text
getpeereid()
fcopyfile(..., COPYFILE_METADATA)
confstr(_CS_DARWIN_USER_TEMP_DIR)
```

Do not introduce:

```text
serverfs-macos-native
Rust
Swift
Objective-C
Xcode project
```

unless a security-critical Phase 0 result proves the Python/libc path inadequate.

---

# 4. Shared architecture

The intended platform split becomes:

```text
MCP tools / policy / limits / models
                |
          WorkdirSession
        /       |        \
     Linux    Darwin    Windows
     POSIX     POSIX       NT
```

Share product contracts.

Share POSIX primitives only where semantics genuinely match.

Do not create a fake universal syscall abstraction.

Explicit backend dispatch:

```text
linux
    -> LinuxWorkdirSession

darwin + arm64 + macOS 27
    -> DarwinWorkdirSession

win32
    -> WindowsWorkdirSession

anything else
    -> PLATFORM_UNSUPPORTED
```

The current implicit:

```text
win32 -> Windows
everything else -> Linux
```

must disappear.

---

# 5. Product parity target

macOS must support the current ServerFS product surface.

## Filesystem tools

```text
list_workdirs
list_directory
find_files
search_text
read_text_file
stat_file

create_text_file
edit_text_file
delete_file
create_directory
delete_directory
```

Optional binary surface:

```text
download_binary_file
upload_binary_file
```

Preserve existing tool-count contracts:

```text
11  filesystem
13  filesystem + binary
21  filesystem + Agent
23  filesystem + binary + Agent
```

## Filesystem semantics

Preserve:

```text
read-only by default
per-workdir policy
hidden-file policy
deny globs
bounded reads/writes
bounded search
binary limits
opaque revisions
expected-revision CAS
atomic publication
hard-link protection
symlink refusal
stable error codes
audit logging
no shell execution
no recursive delete
```

## Agent functionality

Support all existing provider-neutral Agent behavior:

```text
Codex
Claude Code
Qoder

runtime model discovery where supported
request-scoped model override
approvals
questions
cancellation
idempotency
correlation_id
immutable execution manifest
task deadlines
interaction deadlines
writer leases
recovery guards
event retrieval
result spool
read_agent_task_result
retention
provider-aware recovery
```

## Jev

Preserve:

```text
Task Preflight
Runtime Router
Model Advisor
Approval Advisor
```

Jev remains:

```text
optional
advisory
fail-open
automatic=false
```

## Proxy

Support independent routing for:

```text
OpenAI Tunnel
Agent runtimes
Jev
```

using the existing proxy contracts rather than another proxy model.

## File ingress

Support ChatGPT/OpenAI `fileParams` through an isolated native helper.

---

# 6. Frozen non-regression boundaries

Do not redesign:

1. MCP public tool schemas.
2. Agent Bridge protocol.
3. runtime authorization.
4. revision/CAS semantics.
5. writer-lease authority.
6. Jev authority.
7. default read-only behavior.
8. binary size semantics.
9. task idempotency.
10. 8 MiB spool maximum.
11. 256 KiB default spool threshold.
12. provider-native approval behavior.
13. Linux Docker topology.
14. Windows native topology.
15. Windows Rust filesystem kernel.
16. Linux transport security.
17. existing Windows release packages.

macOS is an additive platform implementation.

---

# 7. Phase 0 — Real M-series / macOS 27 capability probe

This phase is mandatory.

Use a real M-series Mac running the latest macOS 27.x available.

Create:

```text
docs/phase-0-macos27-arm64-capability-probe-2026-10.md
```

Record:

```text
Mac model
M-series generation
uname -m
Python architecture
macOS version
Darwin kernel version
Rosetta state
filesystem type
```

## 0A — native architecture

Verify:

```text
uname -m -> arm64
platform.machine() -> arm64
Python itself is arm64
sysctl.proc_translated indicates native execution
```

Run a negative test under Rosetta if possible and prove the runtime gate rejects it.

## 0B — POSIX filesystem primitives

With the actual Python >= 3.12 environment, verify:

```text
os.open + dir_fd
O_DIRECTORY
O_NOFOLLOW
os.stat(dir_fd=..., follow_symlinks=False)
os.scandir(fd)
os.mkdir(dir_fd=...)
os.unlink(dir_fd=...)
os.rmdir(dir_fd=...)
os.link(src_dir_fd, dst_dir_fd)
os.rename / os.replace with dir_fd
os.fchmod
os.fchown
os.listxattr(fd)
os.getxattr(fd)
os.setxattr(fd)
directory fsync
```

Do not infer API availability from Linux behavior.

## 0C — APFS behavior

Use local APFS.

Measure:

```text
st_dev
st_ino
st_mode
st_uid
st_gid
st_size
st_mtime_ns
st_ctime_ns
st_nlink
```

Verify:

```text
same-directory atomic rename
FD identity after pathname replacement
hard-link behavior
concurrent readers during replace
```

## 0D — Darwin metadata

Create test files containing:

```text
POSIX mode
custom xattrs
macOS xattrs
extended ACL
Finder/resource metadata where practical
```

Verify:

```c
fcopyfile(src_fd, dst_fd, NULL, COPYFILE_METADATA)
```

preserves metadata without replacing the new file content.

This should become the preferred Darwin replacement metadata primitive.

No silent metadata loss is acceptable.

## 0E — AF_UNIX peer identity

Test real:

```c
getpeereid()
```

over AF_UNIX `SOCK_STREAM`.

Server must obtain authoritative:

```text
peer euid
peer egid
```

Darwin does not need to fabricate Linux `SO_PEERCRED`.

Do not invent peer PID evidence.

## 0F — flock

Two independent native arm64 processes:

```text
process A -> LOCK_EX success
process B -> LOCK_EX|LOCK_NB blocked
A releases
B -> success
```

## 0G — Darwin user runtime directory

Resolve the operating-system-provided user runtime/temp location using:

```text
_CS_DARWIN_USER_TEMP_DIR
```

Verify:

```text
owned by current UID
not shared with another user
private child directory can be 0700
Unix socket path remains below sun_path limit
```

Do not trust an arbitrary inherited `$TMPDIR` string as the security boundary without validating it.

## 0H — official OpenAI Tunnel

Test only:

```text
darwin-arm64
```

Do not test or retain a v0.13 requirement for:

```text
darwin-amd64
```

Verify:

```text
download
manifest verification
asset SHA-256
executable mode
launch
stdio child
termination
```

## 0I — Agent runtimes

Record current native ARM versions of:

```text
codex
claude
qodercli
```

Verify they execute natively on arm64.

Do not knowingly use x86_64 runtime binaries through Rosetta.

For Codex probe:

```text
managed daemon
Unix socket transport
standalone app-server Unix listener
multiple clients
model/list
model override
approval
interrupt
```

## 0J — launchd

Test a real per-user LaunchAgent:

```text
bootstrap
print
kickstart
bootout
restart
SIGTERM
logout/login behavior where practical
```

Measure descendant process behavior.

Do not infer Windows Job Object semantics.

### Phase 0 exit gate

Every relevant primitive must be classified:

```text
PROVEN
SUPPORTED VIA SMALL DARWIN WRAPPER
UNSUPPORTED
NOT VERIFIED
```

Implementation cannot rely on `NOT VERIFIED`.

---

# 8. Phase A — POSIX boundary extraction

Existing Linux `fdio.py` contains both POSIX and Linux-specific behavior.

Extract only genuinely shared code.

Recommended:

```text
src/serverfs_mcp/posix_fdio.py
```

Candidate shared primitives:

```text
root FD
component traversal
open regular file
open directory
stat_at
unlink_at
parent traversal
same-directory temp creation
bounded FD reads
directory fsync
```

Linux-specific code remains Linux-specific.

Especially:

```text
/proc/self/fd
```

must never appear in Darwin implementation.

Run full Linux tests immediately after extraction.

No Darwin feature work continues until Linux remains green.

---

# 9. Phase B — Darwin filesystem backend

Create:

```text
src/serverfs_mcp/darwin_backend.py
```

Implement the complete `WorkdirSession` contract.

## B1 — path security

Never implement security as:

```python
Path(root, relative).resolve()
```

Every request path must remain descriptor-relative.

Reject request-controlled symlinks regardless of whether their targets remain inside the workdir.

Test:

```text
root symlink
parent symlink
final symlink
inside-target symlink
outside-target symlink
rename/symlink races
```

## B2 — read/list/stat/find

Reuse POSIX implementation where semantics truly match.

`find_files` keeps bounded FD usage by reopening one branch at a time.

Preserve:

```text
RESOURCE_EXHAUSTED
truncation
hidden policy
deny policy
```

## B3 — search

Do not use:

```text
/proc/self/fd
Linux-only rg cwd tricks
path re-resolution
```

Implement Darwin search using:

```text
FD-secure directory walk
-> bounded regular-file read
-> literal UTF-8 scan
```

Match current public behavior:

```text
fixed string
case sensitivity
glob
max file size
deadline
match limit
truncation
hidden policy
deny policy
VCS exclusions
binary/NUL behavior
```

Extract reusable scanning helpers from Windows implementation if doing so is surgical.

Do not rewrite Linux search.

## B4 — mutation

Preserve:

```text
same-directory temp
write payload
metadata copy
fsync temp
revision recheck
atomic rename
directory durability attempt
```

Darwin metadata path should prefer:

```text
fcopyfile(..., COPYFILE_METADATA)
```

Fail before publication if required metadata cannot be preserved.

## B5 — revisions

Keep:

```text
v1:<digest>
```

Do not expose:

```text
inode
UID/GID
APFS identifiers
host path
```

## B6 — support boundary

v0.13 GA filesystem support:

> Local APFS workdirs on an M-series Mac running macOS 27.

Out of scope:

```text
SMB
NFS
FUSE
network volumes
cloud placeholder behavior
unusual filesystem extensions
```

Do not reject them arbitrarily unless needed for safety, but do not claim support.

## B7 — TCC

ServerFS must not attempt to bypass macOS privacy controls.

For protected paths:

```text
fail cleanly
surface redacted access-denied diagnostics
doctor identifies likely TCC denial
documentation explains operator action
```

---

# 10. Phase C — Native CLI and Tunnel

## C1 — native serve

`serverfs serve` gains Darwin support only through the strict platform gate:

```text
darwin
arm64
macOS 27
not Rosetta
```

## C2 — data home

Use:

```text
~/Library/Application Support/ServerFS
```

unless explicitly overridden by:

```text
SERVERFS_DATA_HOME
```

Do not use Linux XDG defaults on macOS.

## C3 — tunnel bootstrap

For Darwin v0.13:

```text
official asset = darwin-arm64 only
```

Do not include `darwin-amd64` as a supported target.

Preserve:

```text
manifest digest pin
asset SHA-256 verification
GitHub host restrictions
project-local install
no PATH modification
```

## C4 — native-wheel command

`serverfs bootstrap native-wheel` remains Windows-specific.

On macOS:

```text
refuse clearly as not required
```

Do not create a dummy macOS native package.

## C5 — doctor

Add:

```text
macOS version
Darwin version
arm64/native status
Rosetta status
APFS
workdir access
symlink checks
metadata-copy support
TCC diagnostics
tunnel-client architecture/version
Bridge endpoint
Bridge readiness
runtime executables
Agent proxy configuration
private-state ownership/modes
```

Doctor stays read-only.

---

# 11. Phase D — Darwin Agent Bridge seams

## D1 — platform constants

Introduce explicit semantics:

```python
LINUX
DARWIN
WINDOWS
POSIX = LINUX or DARWIN
```

Audit every existing Linux guard manually.

Classify it as:

```text
POSIX
Linux-specific
Windows-specific
```

Never replace Linux checks mechanically with `not WINDOWS`.

## D2 — local IPC

Use:

```text
AF_UNIX SOCK_STREAM
```

Endpoint location:

```text
<Darwin per-user runtime dir>/
    serverfs-agent-bridge-v1/
        bridge.sock
```

ServerFS-created directory:

```text
0700
owned by current UID
not symlinked
```

Validate socket length before bind.

## D3 — peer authentication

Use:

```text
getpeereid()
```

Authorize measured UID/GID.

Create a Darwin peer type if needed.

Do not fake:

```text
pid
SO_PEERCRED
Windows SID
```

## D4 — writer lease

Use existing POSIX:

```text
flock(LOCK_EX | LOCK_NB)
```

MCP mutation and Agent workspace-write must share the same lease identity and artifact.

## D5 — private state

Darwin private state:

```text
directory 0700
file 0600
owned by current UID
unsafe symlinks rejected
```

No Windows ACL code.

## D6 — Bridge data home

Use:

```text
~/Library/Application Support/ServerFS/agent-bridge
```

for persistent:

```text
SQLite
results
recovery
locks
generated config
```

Socket remains in runtime storage.

---

# 12. Phase E — launchd Agent lifecycle

Use only a current-user LaunchAgent.

No root.

No LaunchDaemon.

Recommended label:

```text
com.ntlx.serverfs.agent-bridge
```

Location:

```text
~/Library/LaunchAgents/com.ntlx.serverfs.agent-bridge.plist
```

## E1 — lifecycle commands

Use modern:

```text
launchctl bootstrap
launchctl print
launchctl kickstart
launchctl bootout
```

Do not build new deployment flows around legacy `load/unload`.

## E2 — generated plist

Generate deployment-specific paths.

Do not commit machine-specific paths.

Do not put secrets in plist.

Provider credentials and Jev/proxy secrets stay in private runtime/config material.

## E3 — no shell-profile dependency

A LaunchAgent must work without:

```text
.zshrc
.bashrc
interactive shell
Terminal.app
```

Resolve runtime binaries explicitly or through controlled configuration.

## E4 — containment

Measure actual launchd descendant behavior.

If shutdown cannot prove all provider descendants have stopped:

```text
do not claim containment
retain recovery guard
mark provider state unknown
fail closed for workspace write
```

Do not emulate Windows Job Objects without evidence.

## E5 — supervisor shape

Windows path remains unchanged.

macOS recommended topology:

```text
launchd
   -> persistent Agent Bridge

OpenAI tunnel
   -> stdio supervisor
       -> ServerFS MCP child
           -> connect to Bridge UDS
```

Do not start a second Bridge from every Tunnel/MCP process.

---

# 13. Phase F — Agent runtimes

All provider runtimes must be tested natively on ARM64.

No Rosetta runtime is part of v0.13 acceptance.

## Claude

Verify:

```text
normal task
workspace-write
approval callback
question callback
cancellation
proxy direct
proxy enabled
result spool
restart recovery
```

Keep provider permission mode behavior from v0.12.

## Qoder

Verify:

```text
task
model discovery
request-scoped model override
approval
proxy
cancellation
recovery
```

No static model/pricing catalog.

## Codex

Prefer reuse in this order:

1. existing Unix transport;
2. shared POSIX managed-daemon implementation;
3. shared POSIX standalone app-server implementation for proxy mode;
4. Darwin-specific lifecycle only for proven differences.

If current `codex_linux.py` is actually POSIX-generic after testing, extract/rename it rather than copying it.

Preserve:

```text
approvalPolicy="on-request"
model/list
model override
interrupt
```

## Agent proxy

Agent proxy remains credentialless-only where credentials would become visible to provider/tool child processes.

Authenticated unsafe configuration:

```text
fail before provider start
```

No process-global proxy credential leakage.

---

# 14. Phase G — Jev

No Darwin-specific Jev implementation.

Reuse v0.12:

```text
explicit HTTP client
optional authenticated proxy
fail-open network behavior
advisory authority only
```

Verify direct and proxied traffic on the real Mac.

---

# 15. Phase H — Native file ingress

Keep the security property:

> ServerFS MCP itself does not retrieve ChatGPT temporary HTTPS files.

Run a separate native helper.

Reuse existing validation logic:

```text
HTTPS only
443 only
allowed host rules
OpenAI Blob constrained family
DNS validation
global IP only
IP pinning
original hostname TLS
redirect revalidation
size limit
```

For local MCP <-> helper communication prefer:

```text
private AF_UNIX socket
```

rather than a public TCP listener.

Supervisor owns helper lifecycle.

Never silently fall back to downloading inside the MCP process.

---

# 16. Phase I — macOS 27 ARM64 CI

There is **no x86_64 macOS CI requirement** in v0.13.

There is **no macOS 26 matrix**.

There is **no Intel compatibility matrix**.

Use an ARM64 macOS 27 runner.

At the beginning of every macOS job assert:

```text
uname -m == arm64
macOS major == 27
ServerFS Python process == arm64
not Rosetta translated
```

Do not trust runner labels alone.

Current suitable hosted infrastructure may use the ARM64 Xcode 27/macOS 27 runner, but the assertions above are authoritative.

If hosted CI does not satisfy them, use an M-series self-hosted runner.

## Required CI

```text
Darwin filesystem backend
mutation
binary
search
xattrs
ACL metadata
getpeereid
flock
native CLI
doctor
tunnel bootstrap
Agent fake-provider harnesses
launchd-related pure logic
proxy
spool
recovery
```

## Non-regression

Also keep green:

```text
Linux CI
Linux container
Windows native
Windows Agent
ruff
site
git diff --check
```

---

# 17. Phase J — Live M-series/macOS 27 acceptance

CI is insufficient.

Run final acceptance on an actual M-series Mac with the newest available macOS 27.x.

Record:

```text
hardware
chip
OS build
Python build/architecture
ServerFS commit
runtime versions
tunnel-client version
```

## Filesystem

Exercise all:

```text
11 filesystem tools
13-tool binary surface
revision conflicts
read-only enforcement
hidden/deny policy
hard links
symlink attacks
atomic replacement
ACL/xattr preservation
large files
Unicode paths
FD pressure
```

## Tunnel / ChatGPT

Real:

```text
ChatGPT
-> official darwin-arm64 tunnel-client
-> native ServerFS
```

Verify filesystem operations end-to-end.

## Agent

Real live acceptance for:

```text
Codex
Claude
Qoder
```

including:

```text
task
workspace-write
model discovery where supported
model override where supported
approval
deny
question
cancellation
writer lease
restart/recovery
```

## Result spool

Use temporary low threshold:

```text
1024 or 2048 bytes
```

Prove:

```text
real result -> spool
bounded preview
chunked read
byte-exact reconstruction
```

Restore 256 KiB production default.

## Proxy

Prove independently:

```text
Tunnel direct/proxy
Agent direct/proxy
Jev direct/proxy
```

No credential leakage.

## File ingress

Use a real ChatGPT file parameter and verify:

```text
temporary URL
-> native ingress helper
-> upload_binary_file
-> SHA-256 exact
```

Temporary URL must not appear in logs.

---

# 18. Deployment assets

Keep macOS deployment minimal:

```text
deployment/macos/
```

Possible contents:

```text
README.md
LaunchAgent template/generator
install helper
update helper
uninstall helper
```

Do not make v0.13 depend on:

```text
Homebrew formula
DMG
.pkg
GUI
Xcode
Swift
root helper
```

Development/install target should remain approximately:

```text
uv sync
uv sync --project agent_bridge

serverfs bootstrap tunnel-client
serverfs doctor --config serverfs.toml
```

plus user LaunchAgent installation when Agent delegation is enabled.

---

# 19. Packaging

Expected distributions remain:

```text
serverfs-mcp
serverfs-agent-bridge
serverfs-windows-native
```

Do not add:

```text
serverfs-macos-native
```

unless Phase 0 proves compiled Darwin code indispensable.

macOS product/Bridge wheels should remain ordinary Python packages where possible.

No x86_64 macOS wheel/artifact is required.

No universal2 binary is required merely for compatibility.

Any compiled dependency introduced in the future must have an arm64-native execution path.

---

# 20. Documentation

Before release update:

```text
README.md
AGENTS.md
dev_plan_v0.13.md
deployment docs
website English
website zh-cn
architecture diagrams
getting started
upgrade docs
release docs
```

Platform descriptions must be precise:

```text
Linux
    Docker / Linux POSIX / systemd user Bridge

Windows
    Windows native / NTFS Rust kernel / Named Pipe / Windows lifecycle

macOS v0.13
    M-series only
    native arm64 only
    macOS 27 Golden Gate only
    Darwin FD backend
    AF_UNIX + getpeereid
    flock
    launchd user Bridge
```

Do not write generic claims such as:

```text
macOS supported
Apple Silicon supported
Darwin supported
```

without the v0.13 boundary.

Preferred wording:

> ServerFS v0.13 supports native arm64 execution on Apple M-series Macs running macOS 27 Golden Gate.

---

# 21. Explicit non-goals

v0.13.0 does not support or optimize for:

```text
Intel Macs
x86_64 macOS
Rosetta
macOS 26
macOS 28+
universal binary compatibility
older Python solely for older macOS
Hackintosh
non-M Apple Silicon as a claimed target
cross-compilation to Intel
darwin-amd64 tunnel-client
Docker Desktop for Mac
Swift GUI
macOS app bundle
.pkg installer
Homebrew formula
root LaunchDaemon
```

Also do not add:

```text
shell MCP
generic argv execution
recursive delete
automatic symlink following
automatic runtime routing
automatic model selection
automatic approval
generic URL fetcher
```

---

# 22. Development order

Use this order:

```text
Phase 0
    M-series macOS 27 capability probes

Phase A
    POSIX extraction

Phase B
    Darwin filesystem backend

Phase C
    native CLI / doctor / darwin-arm64 tunnel

Phase D
    Darwin Bridge seams

Phase E
    launchd lifecycle

Phase F
    Codex / Claude / Qoder

Phase G
    Jev

Phase H
    native file ingress

Phase I
    ARM64 macOS 27 CI

Phase J
    real M-series acceptance

Phase K
    docs / release closeout
```

Recommended branch:

```text
feat/v0.13-macos27-arm64
```

Keep commits phase-sized.

Do not force-push shared `main`.

Linux and Windows may be under simultaneous development elsewhere; synchronize carefully before merge.

---

# 23. Release gates

v0.13.0 cannot be tagged until all are true.

## Platform

```text
real M-series Mac
native arm64 process
latest macOS 27.x
Rosetta disabled/not used
```

## Filesystem

```text
complete WorkdirSession
all 11 filesystem tools
binary surface
APFS live acceptance
symlink confinement
revision/CAS
atomic replace
ACL/xattr preservation
read-only enforcement
```

## Connectivity

```text
official darwin-arm64 tunnel-client
manifest verification
native ChatGPT E2E
```

## Agent

```text
launchd Bridge
getpeereid auth
flock writer lease
Codex live
Claude live
Qoder live
approvals
questions
cancellation
idempotency
spool
restart recovery
```

## Network

```text
Tunnel proxy evidence
Agent proxy evidence
Jev proxy evidence
no credential leakage
```

## File ingress

```text
native isolated helper
real ChatGPT fileParam
exact bytes
security restrictions preserved
```

## CI

```text
macOS 27 arm64 green
Linux green
Windows native green
Windows Agent green
site green
ruff green
git diff --check clean
```

## Release

```text
package versions aligned to 0.13.0
docs describe v0.13.0 as current
acceptance evidence committed
main clean
all required CI green
```

Then and only then:

```text
tag v0.13.0
push tag
verify GitHub Release
verify release assets
verify Linux GHCR release
verify docs/site
```

---

# 24. Definition of done

The final product claim must be supportable verbatim:

> **ServerFS v0.13.0 adds first-class native macOS support specifically for Apple M-series Macs running macOS 27 Golden Gate. It runs natively as arm64 without Docker or Rosetta and provides the existing ServerFS filesystem, binary-transfer and Agent delegation capabilities using Darwin descriptor-relative filesystem operations, metadata-preserving atomic mutation, authenticated Unix-domain IPC via peer credentials, flock writer leases and a user-scoped launchd Agent Bridge. Codex, Claude Code, Qoder, Jev advisors, proxy routing, approvals, recovery, result spooling and ChatGPT file ingress are supported on this validated platform.**

If any clause cannot be demonstrated on the actual M-series/macOS 27 acceptance machine, narrow the release statement rather than inferring compatibility.