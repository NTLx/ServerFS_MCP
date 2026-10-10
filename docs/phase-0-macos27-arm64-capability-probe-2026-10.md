# Phase 0 — macOS 27 / Apple Silicon M-series Capability Probe

**Date:** 2026-10-10
**Machine:** MacBook Air (Mac16,12), Apple M4, 24 GB RAM
**OS:** macOS 27.0.1 (build 26A434), Darwin kernel 27.0.0 (`xnu-13432.1.9~1/RELEASE_ARM64_T8132`)
**Filesystem:** local APFS (root volume), `f_bsize=1048576`
**Probe interpreter:** CPython 3.12.12 (`uv`-managed, Mach-O 64-bit executable arm64) and
CPython 3.13.12 (WorkBuddy managed, Mach-O arm64) — both native
**Probe scripts:** ad-hoc (round 1: `/tmp/phase0_probe.py`, round 2: `/tmp/phase0_probe2.py`
plus targeted follow-ups); evidence quoted inline below.

Classification legend (per dev_plan_v0.13.md §7):

```text
PROVEN
SUPPORTED VIA SMALL DARWIN WRAPPER
UNSUPPORTED
NOT VERIFIED
```

Implementation must not rely on `NOT VERIFIED`.

---

## Environment classification

| Item | Result | Classification |
| --- | --- | --- |
| `uname -m` | `arm64` | PROVEN |
| `platform.machine()` | `arm64` (both Pythons) | PROVEN |
| Python binary architecture | `Mach-O 64-bit executable arm64` | PROVEN |
| Rosetta translation state | `sysctl -n sysctl.proc_translated` → `0` (native) | PROVEN (positive) |
| Rosetta negative test | Rosetta is **not installed** on this machine: `arch -x86_64 /usr/bin/true` → `posix_spawnp: ... Bad CPU type in executable`. A live translated-process negative test is therefore impossible here. The runtime gate's rejection branch is covered by unit tests instead. | NOT VERIFIED (live); gate logic unit-tested |
| macOS version | 27.0.1 (26A434) — major == 27 | PROVEN |

## 0B — POSIX filesystem primitives (Python-visible)

| Primitive | Result | Classification |
| --- | --- | --- |
| `os.open` with `O_RDONLY\|O_DIRECTORY\|O_NOFOLLOW\|O_CLOEXEC` | PASS | PROVEN |
| `O_NOFOLLOW` symlink rejection | `ELOOP` raised at open time | PROVEN |
| `os.scandir(fd)` | lists entries by directory FD | PROVEN |
| `os.mkdir(..., dir_fd=)` | PASS | PROVEN |
| `os.open(..., dir_fd=)` (file create, `O_EXCL\|O_NOFOLLOW`) | PASS | PROVEN |
| `os.link(src_dir_fd=, dst_dir_fd=)` | PASS | PROVEN |
| `os.rename` / `os.replace` with `src_dir_fd`/`dst_dir_fd` | PASS | PROVEN |
| `os.unlink(..., dir_fd=)` / `os.rmdir(..., dir_fd=)` | PASS | PROVEN |
| `os.stat(..., dir_fd=, follow_symlinks=False)` | PASS | PROVEN |
| `os.fchmod` / `os.fchown` / `os.fstat` | PASS (mode verified 0o640) | PROVEN |
| `os.listxattr` / `os.getxattr` / `os.setxattr` | **`os.setxattr` does not exist on macOS CPython** (`AttributeError`). FD-based xattr is exposed by Darwin libc instead: `fsetxattr` / `fgetxattr` / `flistxattr` verified working via `ctypes` on a file FD (set `user.probe.meta=KEEP`, read back, listed). | UNSUPPORTED in `os` module; SUPPORTED VIA SMALL DARWIN WRAPPER — wrapper PROVEN |
| directory `fsync(fd)` | PASS | PROVEN |

## 0C — APFS behavior

| Item | Result | Classification |
| --- | --- | --- |
| `st_dev/st_ino/st_mode/st_uid/st_gid/st_size/st_mtime_ns/st_ctime_ns/st_nlink` | all populated (`st_dev=16777227`) | PROVEN |
| same-directory atomic rename / replace with `dir_fd` | PASS | PROVEN |
| **FD identity across pathname replace** | FD opened before `os.replace` still reads the ORIGINAL content (`b"original"`) after the name was atomically replaced with a different file — confirmed with an `O_RDWR` FD | PROVEN |
| hard-link behavior | `st_nlink` 2 → 1 after unlinking one name | PROVEN |
| concurrent readers during replace | covered by FD-identity result above (reader keeps reading the old object) | PROVEN |

## 0D — Darwin metadata (fcopyfile)

| Item | Result | Classification |
| --- | --- | --- |
| ACL creation for testing | `chmod +a "<username> allow read,write" <file>` works on macOS 27 (mode and entry must be **separate argv items**; UID-form fails with "Unable to translate ... to a UUID" — use the username) | PROVEN |
| `fcopyfile(src_fd, dst_fd, NULL, COPYFILE_METADATA)` via ctypes (`libSystem`) | `COPYFILE_METADATA = COPYFILE_STAT\|COPYFILE_ACL\|COPYFILE_XATTR` (0b111, flags `(1<<0)\|(1<<1)\|(1<<2)`). Verified: POSIX mode (`0o604`) copied, user xattr (`user.probe.meta=KEEP`) copied, extended ACL (`0: user:lx allow read,write`) copied, **destination content byte-identical and untouched** (`b"dst-content-must-remain"` preserved), `rc==0`. | SUPPORTED VIA SMALL DARWIN WRAPPER — PROVEN (preferred Darwin metadata-copy primitive) |
| Finder/resource metadata | covered by COPYFILE_XATTR path (macOS metadata is xattr-carried); not separately asserted beyond xattr/ACL/mode | PROVEN for the xattr/ACL carrier; fine-grained Finder attributes not separately asserted |

## 0E — AF_UNIX peer identity

| Item | Result | Classification |
| --- | --- | --- |
| `getpeereid()` via ctypes over AF_UNIX `SOCK_STREAM` | server accepts connection, `getpeereid(conn_fd)` returns `euid=501 egid=20` matching the connecting process (`rc==0`) | SUPPORTED VIA SMALL DARWIN WRAPPER — PROVEN |
| Fabricated peer PID / `SO_PEERCRED` | not needed, not used (Darwin-native euid/egid is authoritative) | n/a by design |

## 0F — flock

| Item | Result | Classification |
| --- | --- | --- |
| cross-process `fcntl.flock(LOCK_EX)` hold, then `LOCK_EX\|LOCK_NB` from an independent native arm64 process | second process gets `BlockingIOError` while held; after release, acquisition succeeds | PROVEN |

## 0G — Darwin user runtime directory

| Item | Result | Classification |
| --- | --- | --- |
| `confstr(_CS_DARWIN_USER_TEMP_DIR)` via ctypes | `/var/folders/0j/wbbc4kpd7j383n2swkl9fhp40000gn/0/`, `st_uid` == current UID | SUPPORTED VIA SMALL DARWIN WRAPPER — PROVEN |
| private 0700 child directory | created and verified `0o700` | PROVEN |
| AF_UNIX bind below runtime dir | socket path of length 98 (`.../serverfs-agent-bridge-v1/bridge.sock`) binds successfully; Darwin `sun_path` limit is 104 bytes → path length MUST be validated before bind | PROVEN (with required length check) |
| trust boundary | the confstr value is OS-provided per-user; ServerFS must still lstat/validate ownership before use and never trust an inherited `$TMPDIR` string blindly (implemented in Phase D) | design note |

## 0H — official OpenAI tunnel-client (darwin-arm64 only)

Real bootstrap executed with `SERVERFS_DATA_HOME` sandboxed:

| Item | Result | Classification |
| --- | --- | --- |
| platform tag detection | `detect_platform_tag()` → `darwin-arm64` | PROVEN |
| download + double-anchor verification (pinned manifest SHA-256 → asset digest) | tunnel-client **v0.0.15** installed at `<data>/bin/tunnel-client-v0.0.15-darwin-arm64/tunnel-client`; archive contents `tunnel-client`, `cloudflared`, `cloudflared-manifest.json`, LICENSE, NOTICE | PROVEN |
| binary architecture | `Mach-O 64-bit executable arm64` | PROVEN |
| launch + termination | `tunnel-client --version` → `0.0.15+a390c168ff1b...` (rc 0) | PROVEN |
| stdio child behavior | exercised in Phase C/J acceptance (transport contract is unchanged from v0.12 stdio supervisor) | deferred (not a platform assumption) |

## 0I — Agent runtimes (native ARM64)

| Runtime | Result | Classification |
| --- | --- | --- |
| Codex | `~/.local/bin/codex` → `Mach-O 64-bit executable arm64`, `codex-cli 0.162.0`. Managed daemon **already running natively on this Mac**: `codex app-server daemon version` → `{"status":"running","managedCodexVersion":"0.162.1","socketPath":"/Users/lx/.codex/app-server-control/app-server-control.sock", ...}`. The control socket exists as an AF_UNIX path (symlink into `/private/tmp/codex-501/...`). | PROVEN (CLI, managed daemon, UDS transport present). Full `model/list` / approval / interrupt probes are exercised by the existing adapter test-suite in Phase F |
| Claude Code | `claude` → `Mach-O 64-bit executable arm64`, version `2.1.294` | PROVEN (binary); live task behavior in Phase F/J |
| Qoder | No system `qodercli` binary; Qoder ships as IDE app + official Python `qoder_agent_sdk` (adapter-driven). SDK-driven live verification deferred to Phase F on this Mac | NOT VERIFIED (live SDK run); no platform assumption depends on it before Phase F |

## 0J — launchd lifecycle (real per-user LaunchAgent)

Real LaunchAgent (`KeepAlive` + `RunAtLoad`, `/bin/sh -c` loop) tested in `gui/501`:

| Item | Result | Classification |
| --- | --- | --- |
| `launchctl bootstrap gui/501 <plist>` | OK; job reaches `state = running` with a real `pid` | PROVEN |
| `launchctl print gui/501/<label>` | exposes `state`, `pid` | PROVEN |
| `launchctl kickstart -k` | OK; process replaced (old pid 90378 → new pid 90406) | PROVEN (restart) |
| `launchctl bootout` | OK; process gone afterwards (`ps -p` finds nothing) | PROVEN |
| SIGTERM delivery on bootout | bootout stops the direct agent process; whether ALL provider descendants are reaped is **not proven** — per plan §E4 no containment claim, recovery guard retained, workspace-write fails closed when provider state is unknown | PROVEN for direct process; descendant containment NOT VERIFIED (by plan, not claimed) |
| logout/login persistence | standard LaunchAgent semantics (plist in `~/Library/LaunchAgents`, RunAtLoad) — not separately exercised in Phase 0 | NOT VERIFIED (deferred to Phase E live acceptance) |

---

## Phase 0 exit gate — conclusion

All primitives the v0.13 design depends on are **PROVEN** or
**SUPPORTED VIA SMALL DARWIN WRAPPER (wrapper PROVEN)** on this machine:

1. Descriptor-relative POSIX traversal and mutation: **PROVEN** — the Linux `fdio.py`
   model transfers to Darwin without semantic changes.
2. Metadata preservation on replace: **fcopyfile(COPYFILE_METADATA) via ctypes — PROVEN**,
   covers mode + xattr + ACL without touching content.
3. xattr access: small libc wrapper (`fsetxattr`/`fgetxattr`/`flistxattr`) — **PROVEN**
   (Python `os.*xattr` absent on macOS — do not assume Linux `os` surface).
4. Peer identity: `getpeereid()` ctypes — **PROVEN**; no PID fabrication.
5. Writer lease: `flock` cross-process — **PROVEN**.
6. Runtime dir / socket path: `_CS_DARWIN_USER_TEMP_DIR` + 0700 child + bind within
   `sun_path` 104 — **PROVEN** with mandatory path-length validation.
7. Tunnel: official `darwin-arm64` asset — **PROVEN** (v0.0.15 pinned flow works unchanged).
8. Agent runtimes: Codex + Claude native arm64 — **PROVEN**; Qoder via SDK — deferred to
   Phase F (no earlier phase depends on it).
9. launchd: modern bootstrap/kickstart/bootout — **PROVEN**; containment not claimed.

Items left NOT VERIFIED have no downstream dependency before their own phase:
Rosetta live negative test (unit-tested gate), Qoder SDK live run (Phase F),
launchd logout/login (Phase E), descendant containment (never claimed).
