# Phase 0A — Windows writer lease on `LockFileEx` (measured 2026-10-04)

Status: **GATE PASS — `LockFileEx` is frozen as the v0.11 Windows writer-lease primitive.**

This is a Phase 0 experiment record for `dev_plan_v0.11.md` §5 and §15/Phase 0A. It contains
only measurements executed on a real Windows 11 x64 host with local NTFS. No product code was
changed by this experiment.

## 1. Question

`dev_plan_v0.11.md` §5.1 assumes native `LockFileEx` can carry the Linux `flock` writer-lease
contract. The frozen Linux contract (`agent_bridge/.../leases.py`, `src/serverfs_mcp/agent_leases.py`,
`agent_bridge/.../recovery.py`, `tests/test_agent_leases.py`, `agent_bridge/tests/test_leases.py`)
requires:

| ID | Linux requirement (source) |
| --- | --- |
| C1 | Bridge pre-creates the lock artifact; the MCP mutation reader opens an existing artifact only and never creates one; an absent artifact fails closed (`agent_leases.py:37-42`, `tests/test_agent_leases.py:140-149`) |
| C2 | The reader participates with **read access only**: it opens `O_RDONLY` and the artifact may be mode `0444` (`agent_leases.py:38`, `tests/test_agent_leases.py:171-199`) |
| C3 | A busy acquisition attempt returns immediately (`LOCK_NB`) → `WORKDIR_BUSY` (`agent_leases.py:48-50`) |
| C4 | Contention is bidirectional: Agent blocks mutation and mutation blocks Agent (`leases.py:106`, `agent_leases.py:48`) |
| C5 | A second independent handle in the same process conflicts (`agent_bridge/tests/test_leases.py:12-18`); same-process mutations are serialized by `mutation_lock()` instead (`tests/test_agent_leases.py:61-78`) |
| C6 | The OS releases the lock on handle close **and** on abnormal owner-process death, while the persistent guard survives → `WORKDIR_RECOVERY_REQUIRED` (`agent_leases.py:52-64`, `recovery.py`) |
| C7 | The artifact must be verifiable as a real regular file, reparse points detectable (`leases.py:103-105`, `recovery.py:105`) |
| C8 | Alias-derived artifact names must not be merged by NTFS case folding (`dev_plan_v0.11.md` §5.3) |

`v0.11_windows_agent_bridge_discovery.md` §23.4 listed the highest-risk unknown as **C2** — whether a
lock can be taken on a read-only handle — because a "no" would silently break the
"reader never creates host lock files" invariant.

## 2. Method

Throwaway `ctypes` harness under `%TEMP%\serverfs-phase0a\` (`probe2`, `probe3`, `probe4`, `probe6`,
`probe7` and the raw `run*.log` / `work*\matrix.json` outputs). It is never committed; it is kept
under `%TEMP%` while the Phase 0 review is open and discarded afterwards. Contention is measured between **real separate processes**: a holder child
acquires and is then killed with `TerminateProcess` (no cleanup runs), and contender children report
each stage (`open`, `lock`, `read`, `write`, `rename`, `delete`) with the Win32 error code. Every
contending call has a hard
deadline, so a *blocking* call is recorded as `BLOCKED@<stage>` rather than hanging the run — the
first harness revision proved this distinction is essential, because a blocked busy-probe would
have been an immediate gate failure.

Host: Windows 11 Pro 10.0.26200, `C:` = NTFS (`FILE_CASE_PRESERVED_NAMES`,
`FILE_PERSISTENT_ACLS`, `FILE_SUPPORTS_REPARSE_POINTS`, `FILE_NAMED_STREAMS`), current user SID
`S-1-5-21-1903659250-1886163475-1572399001-1001`, CPython 3.13.3 x64. Artifacts are 0-byte files.

## 3. Measured matrix

`LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY` with a full-file range
(`0xFFFFFFFF:0xFFFFFFFF`); share mode `FILE_SHARE_READ|WRITE|DELETE` unless stated.

### 3.1 Access-mask matrix, uncontended (`0`-byte artifact)

| Handle access | exclusive `LockFileEx` | shared `LockFileEx` |
| --- | --- | --- |
| `GENERIC_READ` | **ok** | ok |
| `FILE_READ_DATA` | **ok** | ok |
| `GENERIC_READ \| FILE_WRITE_ATTRIBUTES` | ok | — |
| `GENERIC_WRITE` | ok | — |
| `GENERIC_READ \| GENERIC_WRITE` | ok | ok |
| `MAXIMUM_ALLOWED` | ok | — |
| `FILE_READ_ATTRIBUTES` | fail `5 ERROR_ACCESS_DENIED` | fail `5` |
| `FILE_READ_ATTRIBUTES \| SYNCHRONIZE` | fail `5` | — |

Exclusive `LockFileEx` **does succeed on a read-only handle** (documented requirement of
`GENERIC_WRITE` does not hold in practice on this OS build — and, more importantly, is verified
not to be needed). An attribute-only handle cannot lock at all, so the verification handle and the
locking handle must be the same `GENERIC_READ` handle.

### 3.2 C2 under a real NTFS read-only DACL (decisive)

Artifact DACL replaced with an explicit, non-inherited read grant for the identity
(`icacls <artifact> /inheritance:r /grant:r <who>:(RX)`), then:

| Measurement | Result |
| --- | --- |
| `GENERIC_READ` open + exclusive lease | **ok** (`LE7-RONLY-DACL-GR-EX`, `LE4-GR-EX`) |
| `GENERIC_READ \| GENERIC_WRITE` open | fail `5 ERROR_ACCESS_DENIED` (`LE7-RONLY-DACL-GW`, `LE4-GW-DENIED`) |
| second reader `GENERIC_READ` + exclusive lease while held | fail `33 ERROR_LOCK_VIOLATION` (`LE7-RONLY-DACL-CONTEND`, `LE4-GR-CONTEND`) |

So the lease does not need write access anywhere, and the artifact can be kept read-only for the
ServerFS mutation side exactly as in Phase E Linux (`0640` + read-only bind mount).

### 3.3 C3/C4 contention, direction by direction

| Holder | Contender | Result |
| --- | --- | --- |
| `GR\|GW` handle, exclusive lock, share-all | other process, `GENERIC_READ`, exclusive | fail `33` in `0 ms` |
| `GR\|GW` handle, exclusive lock, share-all | other process, `GENERIC_READ`, shared | fail `33` |
| **`GENERIC_READ` handle, exclusive lock** | other process, `GENERIC_READ`, exclusive | fail `33` |
| `GENERIC_READ` handle, exclusive lock | other process, `GENERIC_READ\|GENERIC_WRITE`, exclusive | fail `33` |
| `GENERIC_READ` handle, exclusive lock | other process, `FILE_READ_DATA`, exclusive | fail `33` |
| any exclusive holder | other process `CreateFileW` with `GENERIC_READ`, `GENERIC_WRITE`, `RW`, `FILE_READ_ATTRIBUTES`, `READ_CONTROL`, `DELETE`, `MAXIMUM_ALLOWED` | **open itself succeeds in `0 ms`** for every mask |
| same process, second independent handle | exclusive lock request | fail `33` (matches `flock`) |
| reader holds shared lock on a `GENERIC_READ` handle | agent exclusive request, any mask | fail `33` |

`CREATE_ALWAYS`/`OPEN_ALWAYS` are never used, so this row is not a hole: the important part is that
**a conflicting `CreateFileW` never blocks**, it only fails to lock. A busy probe therefore cannot
hang the mutation path, which was the main structural risk of porting `flock` to Windows.

Range granularity is real and must be respected: a 1-byte request against a full-range holder
conflicts (`33`), and an uncontended 1-byte lock on a 0-byte artifact succeeds. Production uses one
full-file range on a per-alias artifact; no artifact is ever shared by two leases.

### 3.4 C6 death and close semantics

| Scenario | Next acquisition |
| --- | --- |
| holder calls `UnlockFileEx` + `CloseHandle` | ok |
| holder re-opens after closing (same process) | ok |
| holder exits via `os._exit()` with the handle and lock deliberately left open | ok |
| holder killed by `TerminateProcess` (no user-mode cleanup at all) | ok |
| reader with `GENERIC_READ` handle + exclusive lock, holder killed | ok |

The OS always reclaims the lock with the handle. A crashed Bridge therefore never permanently
deadlocks a workdir, which is exactly the property `recovery.py`'s persistent guard depends on
(guard survives, live lock does not → `WORKDIR_RECOVERY_REQUIRED`).

### 3.5 Enforcement surface, rename and delete

`LockFileEx` is **OS-enforced, not advisory**: while an exclusive lock is held, another process's
content `open(..., "rb")` and `open(..., "ab")` on the same artifact both fail with
`PermissionError(13)` (`LE3-CONTENT`). Rename-by-name and delete-by-name of the locked artifact
from another process **succeed** (`LE3-RENAME`, `LE-BUSY-DELETE`), and after a delete the next
reader open fails with `2 ERROR_FILE_NOT_FOUND` → fail closed (`C1-NOCREATE-*`,
`tests/test_agent_leases.py:140-149` equivalent). Both match `flock` on Linux.

### 3.6 C7 artifact verification

On the same `GENERIC_READ` locking handle, `GetFileInformationByHandle`,
`GetFileInformationByHandleEx(FileAttributeTagInfo)` and `FILE_ID_INFO` all succeed and report
attributes, reparse tag and a stable 128-bit file id plus volume serial
(`SM-*-ATTR-ID`, `C7-verify`). Python-emulated `st_ino` is **not** used; native file identity is.

Reparse detection (`LE4-RPARSE`, `SETUP-JUNCTION`): a default `CreateFileW` **silently follows** a
reparse point, while `FILE_FLAG_OPEN_REPARSE_POINT` + `FILE_READ_ATTRIBUTES` returns the object
itself with `FILE_ATTRIBUTE_REPARSE_POINT` and tag `0xA0000003`. A *file* symbolic link could not
be created on this host (`os.symlink` → `OSError(22)` — the account lacks
`SeCreateSymbolicLinkPrivilege` and Developer Mode is off), so file-level reparse rejection is
measured only against a directory reparse point.

Hard links (`LE3-HARDLINK`): two names for one file object share `FILE_ID_INFO`, and the exclusive
lock taken through one name is honored when contending through the other name — lease identity is
the file object, not the name.

### 3.7 C8 alias-derived naming

| Measurement | Result |
| --- | --- |
| `Foo.lock` and `foo.lock` in one directory | **same file object** (identical `FILE_ID_INFO`), second open returns the first's file |
| `sha256(alias)`-derived names for `Foo`, `foo`, `FOO`, `con`, `prn`, `nul`, a 200-char alias, `中文 ` with ZWSP, `foo bar`, `..`, a 260-char alias | 11 distinct artifacts, all distinct `FILE_ID_INFO`, name length bounded at 45 chars |
| concurrent exclusive leases on the `Foo` and `foo` artifacts | both succeed independently (`LE4-PERALIAS`) |

Naive alias→filename mapping is therefore **rejected**: NTFS case folding would silently merge two
ServerFS workdirs into one lease. A hash of the exact alias string is **required and sufficient**.

### 3.8 Alternative primitives (for completeness)

| Primitive | Measured |
| --- | --- |
| `msvcrt.locking` | Holder blocks a `LockFileEx` exclusive request with `33` — it is the same underlying primitive, not an independent option |
| `CreateFileW(dwShareMode = 0)` handle-holding | Works: contending opens fail **immediately** with `32 ERROR_SHARING_VIOLATION` for every data-access mask, while `FILE_READ_ATTRIBUTES` / `READ_CONTROL` opens still succeed; released by process teardown; also blocks rename/delete of the held artifact |
| Named mutex | `WaitForSingleObject` from another process → `WAIT_TIMEOUT` while the owner lives, `WAIT_ABANDONED` after `TerminateProcess`; no filesystem artifact, so it cannot express "reader never creates", per-alias artifacts or the guard/live-lock precedence |

`dwShareMode = 0` is a viable fallback, but it is strictly weaker for operability (nothing, not
even a diagnostic read of the artifact, can open it while held) and cannot express a shared vs
exclusive state. The named mutex is not a file-lock substitute at all. Neither is needed.

## 4. Decision

`LockFileEx` is frozen as the Windows writer-lease primitive for v0.11 (plan §5.1 confirmed by
measurement). The production shape Phase C must implement:

1. Artifact per workdir, named from the **exact alias**: `<sha256("alias:<exact alias>")[:40]>.lock`
   inside the private `locks\` directory. Never `NN.lock`, never alias text in the filename.
2. Both sides acquire identically:
   `CreateFileW(artifact, GENERIC_READ, FILE_SHARE_READ|FILE_SHARE_WRITE|FILE_SHARE_DELETE, OPEN_EXISTING)`
   then `LockFileEx(LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY, full range)`.
   * `33 ERROR_LOCK_VIOLATION` → `WORKDIR_BUSY`
   * `2 ERROR_FILE_NOT_FOUND` → `AGENT_LOCK_UNAVAILABLE` (fail closed, nothing was created)
   * `5 ERROR_ACCESS_DENIED` on the open → `LOCK_PATH_UNSAFE` (fail closed)
3. The ServerFS mutation reader uses `GENERIC_READ` only — **no write access and no `CREATE`**, and
   the artifact DACL may grant the reader identity read-only access, exactly as Phase E's
   read-only `0640` bind mount does on Linux. Verified in §3.2.
4. Validate on the **same** locking handle (`FILE_READ_ATTRIBUTES` alone cannot lock): regular file,
   no `FILE_ATTRIBUTE_REPARSE_POINT`, and keep `FILE_ID_INFO` for diagnostics.
5. Release = `UnlockFileEx` + `CloseHandle`; a crashed Bridge needs no cleanup — the OS reclaims the
   lock while `active\<alias-hash>` guard JSON survives, preserving live-lease-beats-guard and
   `WORKDIR_RECOVERY_REQUIRED`.
6. The `mutation_lock()` process-wide serialization stays exactly as on Linux: same-process lease
   contention is real (§3.3), so two concurrent ServerFS mutations must not both reach the lease.
7. `LockFileEx` is OS-enforced (§3.5), so the primitive is legal **only** on lease artifacts, never
   on a workdir file or on any path the Agent runtime may read or write.

Two caveats Phase B/C must carry, both measured here:

* On this profile, replacing a **parent directory** DACL also emptied the child artifact's inherited
  ACEs, so a write-forbidden directory silently denied the reader's open (§4 setup rows, `probe4` vs
  `probe6`). Private-state security must set and verify an **explicit, non-inherited DACL per
  artifact**, never rely on inheritance from the parent directory.
* Windows-native ServerFS and the Bridge run as the **same login user** (v0.10 model, plan §7), so
  "reader never creates" cannot be enforced by a DACL identity split the way Phase E's container
  group + read-only mount does. On Windows it is enforced by construction (`OPEN_EXISTING`, never
  `CREATE`; Bridge pre-creates; `serverfs doctor` verifies the artifacts exist and are
  non-reparse). This is recorded as an explicit platform difference, not a silent weakening.

## 5. Not verified

* File-level (non-directory) reparse rejection on the lease artifact itself — requires
  `SeCreateSymbolicLinkPrivilege` or Developer Mode; the detection API and the follow-instead-of-detect
  failure mode are verified on a directory reparse point.
* Contention across **different** Windows accounts or integrity levels (a second security context
  does not exist on this user-scoped host); deferred to the Phase 0B Named Pipe peer-identity
  experiment, which can construct a restricted-token client.
* ReFS, SMB/redirected drives and Roaming/OneDrive-mapped paths: out of v0.11 scope by plan §17.
* Live ChatGPT → Tunnel → Bridge lease behavior: belongs to Phase E/F/G acceptance.
