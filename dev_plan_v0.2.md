# ServerFS MCP v0.2 — design baseline

This is the **current** design and implementation contract. `dev_plan.md` describes the
original v0.1 read-only release and is historical context: v0.1's "read-only is a product
property, not an option" was deliberately superseded here by a per-workdir opt-in.
`README.md` describes the user-facing behaviour; the tests and the implementation remain
the executable truth.

> A secure MCP server that exposes explicitly configured Linux directories as controlled
> workdirs to AI agents, read-only by default with opt-in per-workdir file mutation.

## 1. What v0.2 adds

v0.1 exposed read channels only. v0.2 keeps every one of them unchanged and adds exactly
five mutation tools, available only in workdirs an administrator explicitly marks
read-write:

```
create_text_file   edit_text_file   delete_file   create_directory   delete_directory
```

Tool surface is fixed at **11 tools** (6 read + 5 mutation). There is deliberately no
`write_file`, `create_path`, `delete_path` or `filesystem_operation`.

### Out of scope (not "pending")

Binary create/edit, rename/move/copy, recursive mkdir (`-p`), recursive delete (`rm -rf`),
deleting non-empty directories, chmod/chown, symlink and hardlink creation, file upload,
shell/command execution, Git tools, automatic backup or trash, database/index/RAG, ACL
management, cross-workdir moves, resources with write semantics.

## 2. Access model

Each of the 16 slots has `WORKDIR_XX_READ_ONLY`, default `true`:

```env
WORKDIR_01_READ_ONLY=true    # default: read-only
WORKDIR_02_READ_ONLY=false   # explicit opt-in to mutation
```

- Exactly one variable, controlling **both** the ServerFS authorization and the Docker
  bind-mount flag. A second access variable would allow the two layers to drift.
- Parsing is strict: `true/false/1/0/yes/no/on/off` (case-insensitive), empty = `true`,
  anything else is a startup `CONFIGURATION_ERROR`. A security switch must never fail
  open. Compose itself only accepts `true`/`false` (`1`/`0` abort the deployment), so
  that is the documented form.
- A disabled slot (empty alias + `.serverfs-disabled` sentinel) with `READ_ONLY=false` is
  a configuration error rather than a silently ignored setting.
- `list_workdirs` reports `access: "read-only" | "read-write"` so the agent learns its
  reach in one call. The value is derived from `read_only`; there is no second source.
- Authorization is the **first** gate: a read-only workdir answers `WORKDIR_READ_ONLY`
  even when the path would also fail the hidden/deny policy, and even if a human has
  mistakenly mounted the host directory writable.

Container root stays read-only, non-root, `cap_drop: ALL`, `no-new-privileges`, no
Docker socket. Only `/workdirs/XX` can become writable, and only from configuration.

## 3. Tool contracts

### create_text_file — the target must not exist

Success requires: read-write workdir, non-root path, existing parent directory, a target
that does not exist, policy-valid path, content ≤ `SERVERFS_MAX_WRITE_BYTES`, no NUL.

Any existing object (file, directory, symlink, FIFO, socket, device) → `PATH_ALREADY_EXISTS`.
There is no overwrite, force or upsert mode, by design: an agent that guesses a path wrong
must be told, not obeyed.

Content is written exactly as supplied — no newline normalization, no trimming, no
appended final newline. UTF-8 encode failure or an embedded `U+0000` →
`BINARY_CONTENT_NOT_ALLOWED`. New files are `0o666 & ~umask` (typically `0644`).

### edit_text_file — the target must exist, and be text

Requires `expected_revision` from a previous `read_text_file`/`stat_file`. The target must
exist (`PATH_NOT_FOUND`, never implicit creation — a mistyped path must not become a new
file), be a regular file (`NOT_A_FILE`), not a symlink (`SYMLINK_NOT_ALLOWED`), valid
UTF-8 (`UNSUPPORTED_TEXT_ENCODING`), NUL-free (`BINARY_FILE`), at most one hard link
(`MULTIPLE_HARDLINKS_NOT_SUPPORTED` — an atomic replace would split the link), and within
the write limit.

Edits are exact-match only, never regex or fuzzy. Each edit declares `expected_count`
(default 1) and fails with `EDIT_CONFLICT` if the actual count differs, or if the text is
absent. `old_text=""` is legal only to fill a completely empty file. Edits apply in order
to an in-memory working text; if any fails, **nothing** is written (all-or-nothing).

### delete_file — regular files, any content

Binary files can be deleted even though only text can be edited. Requires
`expected_revision`. Directories → `NOT_A_FILE`, symlinks → `SYMLINK_NOT_ALLOWED`, other
types → `UNSUPPORTED_FILE_TYPE`. A file the process cannot *read* is refused with
`ACCESS_DENIED`: holding directory write permission is not a licence to unlink a file you
cannot open. No size limit; deletion is permanent.

### create_directory — one level, parent must exist

No recursive creation. `PATH_ALREADY_EXISTS` for anything already at the path, including
an existing directory. Mode `0o777 & ~umask`.

### delete_directory — empty only, never recursive

Requires `expected_revision`. Emptiness is **physical**: hidden, denied and reserved
entries count, and the error never names them. A non-empty directory → `DIRECTORY_NOT_EMPTY`
(also enforced by `rmdirat`'s `ENOTEMPTY` as a second gate). The documented cleanup path
is `list_directory` → `delete_file` per entry → `delete_directory`.

### The workdir root is never mutable

`create_*`, `edit_*` and `delete_*` with `path=""` (or `.`, `./`, `sub/..`) →
`ROOT_MUTATION_NOT_ALLOWED`. No exception.

## 4. One policy, eleven channels

Every channel — `list`, `find`, `search`, `read`, `stat`, `resource`, `create`, `edit`,
`delete file`, `mkdir`, `rmdir` — resolves through the same `ResolvedPath` and consults
the same `DenyPolicy`. A new channel that re-derives rules instead of reading the policy
off the resolved path is a defect.

For a *would-be* path (create/mkdir), the rule is: if the path came into existence, it
would have to be a path the agent may reach. So `create_text_file(".env")`,
`create_directory(".ssh")` and `create_text_file("private.pem")` are refused before
anything is created, and `SERVERFS_EXTRA_DENY_GLOBS` applies to them identically.

`SERVERFS_DISABLE_DEFAULT_DENY=true` semantics are unchanged: it releases the built-in
credential rules for reads *and* mutations. No mutation-only deny policy was added —
`EXTRA_DENY_GLOBS` remains the compensating control and always applies.

### Reserved internal names

Atomic create/edit needs a same-directory temp file. Those files are named
`.serverfs-tmp-<random>`; the prefix `.serverfs-tmp-` is a **hard reserved namespace**.
The workdir registry's disabled-slot marker `.serverfs-disabled` is reserved alongside
it: `WorkdirRegistry` reads that name from the host layout at startup and refuses to
start when it finds it where it does not expect it, so a channel that could create it
would turn a file write into a startup failure. (Deleting it is not reachable — a
disabled slot has no alias, so no channel can address it — but the marker is ServerFS's
own and is reserved in both directions rather than only where it bites.) No enabled
workdir can legally contain it — the registry refuses to start on that layout — so the
reservation costs no configuration anything.

Both names:

- not listable, findable, searchable, readable, stat-able, creatable, editable or
  deletable — in every configuration;
- *not* released by `SERVERFS_ALLOW_HIDDEN`, `SERVERFS_DISABLE_DEFAULT_DENY` or
  `SERVERFS_EXTRA_DENY_GLOBS`;
- direct access answers `RESERVED_PATH`; listings simply omit them;
- `search_text` passes exclusions to ripgrep (`paths.RESERVED_RG_EXCLUDES`) so
  intermediate temp files are never read at all, with the result-path policy re-check
  kept as defense in depth.

If the process is `SIGKILL`ed mid-mutation a temp file can survive on disk. It stays
invisible to the agent forever; v0.2 ships no startup scavenger and no recursive cleanup,
so an operator removes it on the host. This is the one accepted debris mode.

## 5. Revision and concurrency

`read_text_file` and `stat_file` return `revision`: `v1:` + 16 hex chars of a SHA-256 over
`(st_dev, st_ino, st_mode, st_uid, st_gid, st_size, st_mtime_ns, st_ctime_ns, st_nlink)`.
The token is opaque — inode numbers, device numbers and UIDs never reach the agent — and
it changes for content *and* metadata changes. Every page of a paginated read carries the
same revision, so an agent can prove it read one consistent version.

`read_text_file` fstats the FD before and after reading; if the revision moved it returns
`FILE_CHANGED_DURING_READ` instead of content that does not match the revision it reports.

Mutation concurrency:

1. **One process-wide lock** (`mutations.mutation_lock()`) serializes every mutation.
   Reads never take it. Mutation traffic is low; a per-path lock table would be
   premature.
2. **Revision re-check inside the lock**, and again immediately before the commit.
3. **Atomic publication** means a concurrent reader sees the complete old or the complete
   new content, never a partial file.

Resulting guarantee: two callers holding the same revision cannot both commit — exactly
one wins, the other gets `REVISION_CONFLICT`. Idempotent retries are safe: a repeated
create yields `PATH_ALREADY_EXISTS`, a repeated edit with the same revision yields
`REVISION_CONFLICT`, a repeated delete yields `PATH_NOT_FOUND` — in every case without a
second change to the filesystem. That is why all five mutation tools advertise
`idempotentHint=true`.

**Documented limitation.** This is compare-and-swap against *this process* plus a
last-moment re-check, not a linearizable filesystem. POSIX has no atomic "compare inode
and mutate pathname" primitive, so a non-cooperating external writer — a host user, an
IDE, another container — can still rename a pathname between the final check and the
`renameat`. ServerFS does not claim otherwise; a read-write workdir should be treated as
shared space.

## 6. Implementation notes

`mutations.py` owns revision, the lock, atomic create/edit, delete, mkdir/rmdir and
metadata preservation. `fdio.py` remains the FD-based security boundary and gained
`open_regular_at`, `open_dir_at`, `stat_at`, `create_temp_at`, `unlink_at`,
`fsync_directory` plus `open_root`/`root_fd` — the single implementation of "open the
workdir root", which `tools.py`, `filesystem.py` and `mutations.py` all delegate to.

Every mutation walks the parent chain with `O_DIRECTORY|O_NOFOLLOW` and holds the parent
FD; the final name is only ever passed to `dir_fd=`-relative calls (`linkat`, `renameat`,
`unlinkat`, `mkdirat`, `rmdirat`, `stat`). No request-derived path is ever reassembled
into a pathname.

Publication:

```
create:  temp(O_EXCL) → write → fsync → linkat(temp, target)   # EEXIST = no overwrite
                                        → unlinkat(temp) → fsync(dir)
edit:    temp(O_EXCL) → write → preserve owner/mode/xattrs → fsync
                                        → re-check revision → renameat(temp, target)
                                        → fsync(dir)
```

Metadata preservation happens **before** the rename and failure is fatal: ownership is
copied only when it differs (a foreign owner cannot be reproduced by a non-root
container), mode is always copied, and xattrs are copied wholesale; any unreproducible
piece raises `METADATA_PRESERVATION_FAILED` with the original file untouched. Losing a
mode bit, an ACL or a SELinux label silently would be worse than a failed edit.

The order — **ownership, then mode, then xattrs** — is load-bearing, not cosmetic.
`chown(2)` clears `S_ISUID`/`S_ISGID` on the file it touches, so applying the mode first
lets a successful `fchown` strip setuid/setgid from the published inode while the edit
still reports success (measured: `0o2755` published as `0o755`). Copying xattrs last
keeps the replacement holding what the *original* held rather than what the ownership
change left behind, since ownership changes can disturb `security.*` metadata. Both
properties are pinned by tests (`TestEditMetadata`).

Durability has two different failure semantics on purpose. The temp file's own `fsync`
runs *before* publication, so a failure there aborts the mutation with `MUTATION_IO_ERROR`
and nothing is published. The directory `fsync` runs *after* publication, when the entry
is already visible to every reader: a failure there means "not durable across a crash",
not "nothing happened", so it is logged as `directory_fsync_failed` for the operator and
the tool still reports the mutation it actually performed. Reporting a failed call for a
committed change would contradict the filesystem and invite a pointless retry.

The text-file contract is enforced on the *result* too, not only on the source:
`edit_text_file` rejects `old_text`/`new_text` containing `U+0000`
(`BINARY_CONTENT_NOT_ALLOWED`) exactly as `create_text_file` rejects it in `content`, so
no mutation channel can turn a text file into one that every channel then refuses to read.

Audit: every mutation emits one `tool_call` event with tool, workdir, relative path,
duration, success, `error_code`, and per-tool counters (`bytes_written`, `edit_count`,
`bytes_before`/`bytes_after`, `bytes_deleted`, `revision`). Content, `old_text`/`new_text`
and any host/container path are never logged.

## 7. Configuration added in v0.2

| Variable | Default | Meaning |
|---|---|---|
| `WORKDIR_XX_READ_ONLY` | `true` | Read-only unless explicitly `false`; drives app authorization and the bind mount |
| `SERVERFS_MAX_WRITE_BYTES` | `1048576` | Create content, edited source, edit result, and the summed `old_text`+`new_text` of one call |
| `SERVERFS_MAX_EDITS_PER_CALL` | `50` | Edits accepted in one `edit_text_file` call |

## 8. Error codes added in v0.2

```
WORKDIR_READ_ONLY              PATH_ALREADY_EXISTS        PARENT_NOT_FOUND
ROOT_MUTATION_NOT_ALLOWED      REVISION_CONFLICT          FILE_CHANGED_DURING_READ
EDIT_CONFLICT                  TOO_MANY_EDITS             WRITE_TOO_LARGE
BINARY_CONTENT_NOT_ALLOWED     MULTIPLE_HARDLINKS_NOT_SUPPORTED
METADATA_PRESERVATION_FAILED   DIRECTORY_NOT_EMPTY        RESERVED_PATH
MUTATION_IO_ERROR
```

Reused unchanged: `WORKDIR_NOT_FOUND`, `PATH_NOT_FOUND`, `PATH_OUTSIDE_WORKDIR`,
`HIDDEN_PATH_NOT_ALLOWED`, `DENIED_PATH`, `SYMLINK_NOT_ALLOWED`, `NOT_A_FILE`,
`NOT_A_DIRECTORY`, `BINARY_FILE`, `UNSUPPORTED_TEXT_ENCODING`, `UNSUPPORTED_FILE_TYPE`,
`ACCESS_DENIED`. All remain `CODE: short message`, built from the agent's own inputs, with
no host path, container path, temp name, inode or UID.

## 9. Verification

The v0.2 gate is the README's Development block (all six commands actually executed) plus
a mutation smoke test on a **throwaway** compose stack: the production deployment serves a
live tunnel and is never used as a mutation test target.

Coverage summary: tool surface and annotations; RO/RW authorization on all five tools;
the hidden × default-deny policy matrix on all five mutation channels; the reserved
namespace on all eleven channels including a ripgrep-level check; create/edit/delete
semantics (conflicts, types, limits, atomicity, metadata, hardlinks, BOM/CRLF/UTF-8
fidelity); revision stability, opacity and change detection; deterministic concurrency
(two creates, two edits on one revision, edit vs delete, read-while-edit); audit content
exclusion; and error-leak checks over the whole failing surface.

Known residual limitations (accepted, not fixed in v0.2):

- external renames can still race the final check (see §5);
- a crash can leave a `.serverfs-tmp-*` file on the host;
- a file owned by another user cannot be edited by a non-root container
  (`METADATA_PRESERVATION_FAILED`), and the same applies to metadata the process cannot
  reproduce (e.g. a `security.*` xattr without privileges);
- the process-local lock does not coordinate a second ServerFS instance or a host editor.