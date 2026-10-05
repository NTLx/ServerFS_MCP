# Phase C C0 — Windows revision correctness experiment (2026-10-05)

Scope: `dev_plan_v0.11.md` §15 "Recorded prerequisites" and the Phase C C0 gate. This file records
what was measured and what it decides. All abstract: no volume serial, no raw `FILE_ID`, no absolute
host path, no SID, no user name. Everything ran on scratch files under `%TEMP%` on WorkPC (Windows 11
x64, local NTFS, ordinary non-elevated login), and the experiment modified no product code.

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
4. **The USN route is disqualified as written.** A per-file change sequence number is readable only
   through the volume journal (`FSCTL_READ_USN_JOURNAL` on a volume handle), which an ordinary
   non-elevated login cannot open, and it is not a per-handle O(1) query. Phase C §7 requires
   "ordinary current-user usable, no admin-only assumption", so USN cannot be selected here without a
   separate, larger decision.
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

## 6. Closed by this phase independently of that decision

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

## 7. Not tested here

- ReFS, SMB/remote shares and non-NTFS volumes: out of scope, v0.11 claims local NTFS only.
- Elevated/admin USN journal reads (not selected; the ordinary-user requirement disqualifies them).
- File-symlink reparse artifacts for the lease paths (needs Developer Mode; belongs to the lease work).
- Anything beyond single-machine warm-cache medians in item 5 — no performance claim is being made.
