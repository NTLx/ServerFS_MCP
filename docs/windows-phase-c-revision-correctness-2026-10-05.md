# Phase C C0 — Windows revision correctness experiment (2026-10-05)

Scope: `dev_plan_v0.11.md` §15 "Recorded prerequisites" and the Phase C C0 gate. This file records
what was measured and what it decides. All abstract: no volume serial, no raw `FILE_ID`, no absolute
host path, no SID, no user name. Everything ran on scratch files under `%TEMP%` on WorkPC (Windows 11
x64, local NTFS, ordinary non-elevated login), and the experiment modified no product code.

Outcome: **C0 CLOSED — CONTRACT DECISION B** (§9). §4–§6 preserve the measurement and the conclusion
that was live while the gate was open.

## 1. Why this gate exists

Phase A measured that a rapid **same-size external rewrite** of the same object left the published
Windows revision token unchanged in 17 of 20 trials. The current public material is volume serial +
128-bit file identity + attributes + size + link count + `LastWriteTime`, so the only member of the
tuple a content rewrite can move is a timestamp. That is a correctness question, not a test flake:
`edit_text_file` and `upload_binary_file(overwrite)` are revision-guarded replacements, and a guard
that cannot see a write is not a guard.

Phase A's `settle_file_time()` only waits for the clock to tick. It cannot be the answer.

## 2. Method

One read handle is held open for the whole scenario (the way the native kernel holds a handle per
object) and queried with `GetFileInformationByHandleEx`: `FileBasicInfo` (creation / last-access /
last-write / **change** times, attributes), `FileStandardInfo` (end-of-file, **allocation size**, link
count, delete-pending) and `FileIdInfo` (volume serial + 128-bit id). `FILE_INFO_BY_HANDLE_CLASS` is
zero-based; mis-numbering it returns `ERROR_INVALID_PARAMETER`/`ERROR_MORE_DATA`, which is worth
recording because that is the kind of bug this experiment would otherwise hide.

Each trial records the snapshot before and after one event and reports, per signal, how often the
signal **stayed equal while the bytes changed** — a miss. Two derived tokens are tracked as well: the
shipped material, and the same material with `ChangeTime` added. No sleep is inserted anywhere in the
miss-counting loops.

Events: same-size rewrite measured at three durability points (writer handle still open, after
`FlushFileBuffers`, after the writer closed); different-size rewrite; DOS attribute toggle; rename
away and back; same-name replacement. Plus a granularity sweep that inserts an explicit delay between
the recorded revision and the rewrite, and a head-to-head of which of `LastWriteTime` / `ChangeTime`
moved.

## 3. Results

Miss counts (of the trials shown). The after-flush and after-close rows vary between runs of the same
probe because they depend on how fast the loop turns over relative to the clock tick; the ranges are
the observed spread over repeated executions. The same-size rows are the ones that matter, and they
are identical for `lastWrite` and `change` in every run.

| event | size | allocation | attributes | links | lastWrite | **change** | fileId |
| --- | --- | --- | --- | --- | --- | --- | --- |
| same-size rewrite, writer handle open (n=120) | 120 | 120 | 120 | 120 | 112 | **112** | 120 |
| same-size rewrite, after flush (n=120) | 120 | 120 | 120 | 120 | 59–82 | **59–82** | 120 |
| same-size rewrite, after close (n=120) | 120 | 120 | 120 | 120 | 94–109 | **94–109** | 120 |
| different-size rewrite (n=20) | **0** | 19 | 20 | 20 | 10 | 10 | 20 |
| DOS attribute toggle (n=10) | 10 | 10 | **0** | 10 | 10 | 9–10 | 10 |
| rename away and back (n=10) | 10 | — | 10 | 10 | 10 | 8 | 10 |
| same-name replacement (n=10) | 9 | — | 10 | 10 | 8 | 8 | **0** |

Head-to-head, same-size rewrite with durability at flush (n=120):

```text
only ChangeTime moved        0
only LastWriteTime moved     0
both moved                  37
neither moved               83
```

Granularity sweep — external same-size rewrite separated from the recorded revision by a fixed delay,
40 trials each, with a byte comparison confirming the rewrite really changed the content:

| delay | `LastWriteTime` still equal | content identical (trial invalid) |
| --- | --- | --- |
| 0 ms | 3 / 40 | 0 / 40 |
| 0.25 ms | 0 / 40 | 0 / 40 |
| 0.5 / 1 / 2 / 5 / 15 / 50 ms | 0 / 40 | 0 / 40 |

## 4. What that decides

1. **`ChangeTime` is not the answer.** It moved in exactly the same trials `LastWriteTime` moved
   (`only ChangeTime moved = 0` across 120 rapid rewrites), so it adds no detection. It also moved on
   2 of 10 rename-away-and-back trials, i.e. it costs rename stability — which is why the shipped
   kernel excludes it from the *public* tuple. It stays where it already is: in the private
   replacement fingerprint, which publishes no token and where rename sensitivity is harmless.
2. **No other O(1) metadata signal detects a same-size content rewrite.** Size, allocation size,
   attributes, link count and file identity are unchanged in 120/120 trials.
3. **The residual window is a clock tick, not unbounded staleness.** Once the rewrite is separated
   from the recorded revision by ≥0.25 ms, detection is 40/40. The failing case is a rewrite that
   lands inside the same ~0.25 ms tick as the revision the caller was given.
4. **`ChangeTime` is available and costs rename stability, and it adds no detection.** See the
   head-to-head above: `only ChangeTime moved = 0`, so this is not a material worth trading
   rename-stability for — independently of the question answered in §8 below.
5. **A bounded content read is cheap.** One 128 KiB window read measured median ≈0.101 ms,
   p95 ≈0.122 ms on warm cache (4 KiB ≈0.006 ms, 64 KiB ≈0.052 ms). A *mutation-gate* content
   comparison therefore costs about a tenth of a millisecond per mutation, while the same work in
   `stat_file`/`list_directory` would put it on every metadata call — which §8 forbids doing here.
6. **The kernel already closes the concurrency half of the problem.** The mutation window opens the
   target with `FILE_SHARE_READ` (replacement additionally `FILE_SHARE_DELETE`), so a new external
   write open is refused for the whole snapshot → final gate → atomic relative rename interval; the
   v0.10 test `replacement_window_blocks_external_write_opens` still passes. What is **not** covered is
   a rewrite that completed *before* the window opened, in the same tick as the caller's revision,
   because the content an edit is derived from is read before that window.

## 5. Consequence for the C0 gate

Of Phase C §15's seven conditions, condition 1 ("rapid same-size rewrite no longer yields a stale
revision") and condition 2 ("the fix must not depend on sleep") cannot be met by any metadata
material: the token is a function of metadata, and every O(1) metadata field reachable from a
non-elevated handle is provably unchanged in that case. The writer-lease implementation is therefore
**not started** — §15 makes entering it contingent on this gate, and §8 makes a content-derived
revision a formal design decision rather than something this phase may adopt on its own.

The two candidate answers, with what each does and does not satisfy:

- **Structural (hold before read).** Move the content read inside the mutation window: open the
  target with the existing restricted share mask *before* its bytes are read, so no external writer
  can be active across read → gate → publish and the edit is always derived from bytes sampled under
  that hold. Cost: none beyond a window the kernel already takes; no hashing, no public token change,
  `stat_file`/`list_directory` stay O(1) metadata. It removes the lost-update hazard, but the
  published token still does not change for a same-tick earlier rewrite, so §10's literal regression
  ("old revision → `REVISION_CONFLICT`") is not satisfied by it.
- **Content-derived gate.** Add a bounded content digest (for example head + tail windows plus size)
  to the material the *mutation* final gate compares, keeping the published `v1:<16 hex>` shape and
  metadata-only `stat_file`. It satisfies §10 literally at ≈0.1 ms per mutation, and needs an explicit
  decision about the read-size contract for large files: bounded windows leave content outside them
  undetected, and a full-content digest is exactly what §8 rules out as a default.

They are not equivalent: the first removes the race, the second also makes the token observably
change. Choosing between them — or accepting the tick-sized window together with the share-mask hold as
the documented boundary — changes a frozen contract, so it is the maintainer's call per §7/§8.

Update after §6: the per-file USN route was then measured and failed on this platform, which removes
the "cheap exact signal" possibility. What is left is the contract-level choice, and only these two:

- **A — full content-derived identity in the published revision.** `stat_file`, `read_text_file` and
  the mutation guard would carry a content component, so an external same-size rewrite is always
  visible. Cost: the read/perf contract of the metadata channels changes, which is exactly what §8 of
  the Phase C instruction forbids doing by default; it needs an explicit decision.
- **B — formally lower the external-writer guarantee.** Keep metadata material, document the tick-sized
  blind window plus the share-mask hold (which does make an *active* writer and a transaction
  mutually exclusive), and state that Windows revision is compare-against-metadata, not
  compare-against-content.

No third option survives measurement: `ChangeTime` adds nothing (§4.1), no other O(1) field moves
(§4.2), and per-file USN is unreachable as a change signal here (§6).

*Decision since recorded: the maintainer selected **B** — see §9. §4–§6 are kept as the state of the
record while the gate was open, including §6's then-blocking "C0 stays blocked" conclusion.*

## 6. C0b — per-file USN follow-up (correction to the first-round USN claim)

The first round concluded from the *volume-journal* APIs that "USN requires a volume handle and
elevation", and used that to disqualify the route. That conclusion was wrong in its premise, and the
route had to be measured separately: **`FSCTL_READ_FILE_USN_DATA` (`0x000900EB`) is a distinct
per-file / per-directory query**, documented against "a specified file or directory" and returning
the most recent change-journal record for the object behind the handle. Volume-wide enumeration
needs the volume-oriented APIs; this does not.

Second experiment, same discipline: `%TEMP%` scratch files, no elevation, no journal creation, no
`fsutil`, no machine setting touched, no raw USN recorded.

Measured:

| question | result |
| --- | --- |
| does the call work on an ordinary non-elevated **file** handle | **yes** — `ERROR_SUCCESS`, a `USN_RECORD_V2` is returned, USN nonzero (input may be `NULL`; the `READ_FILE_USN_DATA` variants are accepted too) |
| does it work on a **directory** handle | **yes**, when the handle is opened with `FILE_FLAG_BACKUP_SEMANTICS` (without it the open itself fails with `ERROR_ACCESS_DENIED`, which is the directory-open rule, not a USN limitation) |
| second local fixed NTFS volume (read-only repeat) | same: query succeeds, record returned |
| completed same-size rewrite (writer flushed and closed), 200 rounds, no sleep | USN **unchanged in 200/200**; the record's own timestamp unchanged in 200/200; `Reason` zero |
| writer still open / writer flushed but still open | USN unchanged (acceptable on its own — see the sharing result below) |
| different-size rewrite | USN unchanged |
| DOS attribute toggle | USN unchanged |
| rename away and back | USN unchanged |
| same-name replacement | USN unchanged (queried through the still-held handle of the old object) |
| **ServerFS's own kernel replacement** (`open_workdir(...).replace_bytes`) | USN unchanged |
| directory: child created and removed | USN unchanged |
| pure read / stat-only on the held handle | unchanged, as required |

On these volumes the per-file query returns a static record with `Reason = 0` and a timestamp that
never moves, so it is not an available change signal here even though the API itself is reachable
without elevation. §5's gate is `unchanged = 0`; measured `unchanged = 200/200`. **C0b therefore
FAILS**, and per §16 the response is not to fall back to a bounded content digest: C0 stays blocked
and the remaining choice is the contract-level one.

Independently confirmed while testing §4A: with an external writer still holding `WRITE` access, the
ServerFS restricted-share target open is **refused** (`ERROR_SHARING_VIOLATION`, mapped internally and
never surfaced raw), and the same open succeeds once that writer closes. So the sharing half of the
combined model — an active writer cannot coexist with a mutation transaction — holds on this
platform, exactly as the v0.10 kernel's own test asserts; what sharing cannot do is reveal a write
that finished earlier. Those two halves have to be described together: neither USN nor share mask
alone gives compare-and-swap against a non-cooperating writer.

## 7. Closed by this phase independently of that decision

**C0.7 error precedence.** The native kernel checked the revision guard before the target type, so
`edit_text_file`/`delete_file` on a directory with a stale `expected_revision` answered
`REVISION_CONFLICT` where Linux answers `NOT_A_FILE`. `native/windows/src/mutation.rs` now runs a
`check_file_target` gate (type first, then revision) on the file-target channels — replacement and
`delete_file`, at both the initial and the final gate — while `delete_directory` keeps its revision
guard as-is. `cargo test` on this machine: 10/10 lib tests and the NTFS integration targets green,
including the new `directory_target_is_refused_before_the_revision_guard`, which asserts
`IsADirectory` for a directory target with both a stale and a current token. The MCP-surface
equivalent (retiring the Phase A `windows_difference` xfail) needs the rebuilt wheel and is recorded
in the Phase C status entry.

## 8. Not tested here

- ReFS, SMB/remote shares and non-NTFS volumes: out of scope, v0.11 claims local NTFS only.
- Elevated/admin USN journal reads (not selected; the ordinary-user requirement disqualifies them).
- File-symlink reparse artifacts for the lease paths (needs Developer Mode; belongs to the lease work).
- Anything beyond single-machine warm-cache medians in item 5 — no performance claim is being made.

## 9. C0 decision (maintainer, 2026-10-05): Option B — contract decision, not a fix

The A/B choice in §5 was resolved as **Option B**. C0 is therefore **CLOSED — CONTRACT DECISION B**.
It is explicitly *not* recorded as `PASS because the blind window was fixed`: the window was measured,
every candidate strong signal was measured, none qualified, and the residual was converged into an
accepted and documented product boundary. The experiment (§2–§5) and the USN follow-up (§6) stand as
written; nothing here re-reads them.

### 9.1 Frozen Windows revision semantics

- The public revision stays `v1:<16 hex>`, computed from object identity plus the relevant observable
  NTFS metadata. It is an **opaque optimistic-concurrency token**.
- It is **not** a content hash, **not** a cryptographic content identity, and **not** an atomic
  compare-and-swap token against arbitrary same-user processes. No code, test or document in this
  phase may claim otherwise.

### 9.2 The accepted boundary, in product wording

On Windows 11 x64 with local NTFS, a non-ServerFS-coordinated external writer that completes a
same-object, same-size in-place rewrite **within one filesystem timestamp tick** may leave the public
revision unchanged. The §3.3 figure (separations of ≥ ~0.25 ms detected 40/40 on WorkPC) is recorded as
**measured evidence about that machine**, never as a normative or guaranteed maximum window, and no
product text, test or error message may quote 0.25 ms as the size of the window.

### 9.3 Why Option A was rejected

A full content-derived component turns `stat_file` — and every other channel that must produce a real
revision — from an O(1) metadata query into an O(file-size) content scan against a size that has no
natural bound. That is a published-performance regression, hidden I/O on a read channel, added latency,
and a new resource-exhaustion surface, all to close one narrow non-cooperating-writer alias. A strong
content-identity revision mode, if it is ever wanted, is a separate product design decision and is not
part of v0.11.

### 9.4 The three-layer concurrency model this closes

1. **ServerFS-coordinated writers** (MCP mutation versus Agent `workspace-write`) are serialized by the
   Phase C writer lease. This is the layer the phase is actually built for.
2. **An active external writer** is excluded by the frozen restricted-share strategy on the mutation
   target: while another process still holds `WRITE` access, the ServerFS open is refused
   (`ERROR_SHARING_VIOLATION` internally, normalized, never surfaced raw). Measured in §6.
3. **A completed external writer** is detected through object replacement (file identity change), size
   change, and any observable metadata change. The single accepted blind spot is the same-tick same-size
   in-place rewrite of §9.2.

### 9.5 Replacement C0 completion contract

The old gate condition — "a rapid same-size external rewrite must always change the revision" — is no
longer a release gate. C0 is complete when all of the following hold:

1. metadata and USN capability are fully measured (§2–§6);
2. no ordinary-user O(1) strong change signal exists (§4, §6);
3. the blind window is precisely documented (§9.2);
4. an active external writer is excluded by the restricted-share transaction (§9.4 layer 2);
5. `edit_text_file`'s read → commit runs entirely inside one target hold;
6. ServerFS-coordinated writers are serialized by the Phase C lease;
7. the public revision makes no content-identity or arbitrary-process-CAS claim (§9.1).

### 9.6 Item 5 is a requirement, and it is not a revision fix

The Windows edit channel currently reads the source bytes *before* the replacement transaction opens
the target, so an external writer can enter between those two opens. Phase C must make it one
transaction: acquire the restricted-share target hold → validate regular-file type → validate
`expected_revision` → read the source bytes **from that same held object** → validate text/edit
semantics → build the replacement → final identity/revision/fingerprint gate → atomic handle-relative
replacement, with the hold kept throughout.

This must be described as narrowing the **active-writer** race. It does not close the historical
same-tick token alias, and must never be presented as if it did — that alias is what §9.2 accepts.

`upload_binary_file(overwrite)` and `delete_file` have no pre-read content problem, but the same
acquire-hold → validate → commit shape applies: the restricted-share target handle stays held across
type validation, revision validation and the final commit/delete, rather than being released and
reacquired.

Read channels (`read_text_file`, `download_binary_file`, `stat_file`) must **not** gain long-lived
writer-excluding sharing: plain reads still have to coexist with Agent `workspace-write`, and the
existing before/after metadata check stays as it is.

### 9.7 Consequences for the phase

`C1` (platform-neutral lease identity) is unblocked, and C1→C5 proceed on the frozen §5.5 shape. Signal
research is closed: `ChangeTime`, per-file USN, bounded digests and full-content hashes are all
settled by this decision and are not to be re-opened inside Phase C. `AGENTS.md` carries a narrow
Windows revision clarification so the token's guarantee level is readable without reading this
document; `dev_plan_v0.10.md` is history and is not rewritten.

## 10. Phase C implementation evidence (2026-10-06)

What §9 committed to is now implemented, and the parts of it that needed a measurement got one.
This section records only those; §3–§6 remain the experiment as it happened.

**The accepted blind window, measured through the published surface.** A probe drives 200 tight
same-object same-size external rewrites and reads the revision back through `stat_file` on the real
MCP surface: **75 unchanged, 125 detected** on WorkPC (Windows 11 x64, local NTFS `C:`, ordinary
non-elevated login, `%TEMP%` scratch files, no `settle` wait). It is run on demand, not as a release
gate:

```text
SERVERFS_MEASURE_BLIND_WINDOW=1 uv run pytest tests/test_revision.py -k same_tick -s
```

That is the §9.2 boundary restated as a count instead of a claim. It is deliberately **not** an
`xfail`: the suite must not carry a permanent expected-failure for behaviour the product now documents
as out of scope for the token. The guarantees decision B *does* make are asserted normally, in
`tests/test_revision.py::TestWindowsAcceptedRevisionBoundary`: an explicit timestamp move, a size
change and a same-name replacement all move the revision.

**Item 5, held across read and commit.** `edit_text_file`'s source read now happens on the object the
replacement is holding, inside one kernel transaction. The seam is deterministic and measured rather
than argued: with the transaction live, an external `O_WRONLY` open of the target is refused and a read
of the same object still succeeds, and after the transaction both succeed
(`tests/test_native_windows.py::test_source_transaction_reads_from_the_object_it_holds`). Kernel-level
cases cover the ordering (kind and revision answered before any read, bounded read before the build,
no publication when the build raises). As §9.6 requires, this is described as narrowing the
**active-writer** race — it does not close the same-tick alias above.

**A LockFileEx implementation fact Phase 0A did not state.** §5.5 item 2 describes
`LockFileEx(LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY)` over the full file range. While
implementing it: `LockFileEx` reads `Overlapped.Offset` for the range start **even for a handle opened
without `FILE_FLAG_OVERLAPPED`**, and passing a NULL `lpOverlapped` faults on this OS build — every
call with `None` raised an access violation at offset `0x10`, the `Offset` member, while the identical
call with a real zeroed `OVERLAPPED` succeeded, with and without `use_last_error`. Both lease backends
therefore pass a fresh zeroed structure per call. This is a clarification of the frozen shape, not a
change to it: the acquisition, share mask, access mask, dispositions and error mapping are exactly as
§5.5 and `docs/windows-phase-0a-lockfileex-2026-10-04.md` §4 specify.

**Layers 1 and 2 of §9.4 are now observed behaviour, not design intent.** Layer 1 (ServerFS-coordinated
writers) is proven both directions on the real Named Pipe and two processes: an Agent
`workspace-write` turn blocks an MCP mutation with `WORKDIR_BUSY`, and a lease held by a separate
process makes the Bridge refuse a `workspace-write` submission with `WORKDIR_BUSY`. Layer 2 (active
external writer) is the sharing refusal measured in §6 and re-asserted by the transaction seam above.
Layer 3 (completed external writer) is the revision, with §9.2 as its documented limit.

**Crash and reconciliation.** `TerminateProcess` leaves the guard and releases the lock, the published
surface answers `WORKDIR_RECOVERY_REQUIRED`, and a Bridge restarted on the same state and lock trees
reconciles the task and clears the guard so the workdir is writable again. The lifecycle evidence is
the Linux suites running against the Windows lease rather than a second implementation of it
(43 previously-skipped Bridge cases), which is what makes the parity claim checkable.
