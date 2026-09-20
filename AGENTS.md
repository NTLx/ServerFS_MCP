# AGENTS.md

Read `README.md` for the currently released behaviour, security model, deployment and
release contract. Read `dev_plan_v0.3.md` for the next-version design baseline: the
optional Agent Bridge, provider-neutral long-running task model, Codex App Server mapping,
Claude Agent SDK mapping, human approvals/questions and cross-process workdir leases.
`dev_plan_v0.3.md` is design intent, not shipped behaviour: do not rewrite README or
existing v0.2 code as though Agent Bridge features already exist. Read `dev_plan_v0.2.md`
for the released v0.2 filesystem-mutation baseline, and `dev_plan.md` for the original
v0.1 baseline. Where current behaviour disagrees with a historical plan, README, tests
and implementation win; where new v0.3 work is being implemented, `dev_plan_v0.3.md`
defines the intended new contract unless the maintainer explicitly revises it.

The `agent_bridge/` directory is **v0.3 development code**, not released behavior and
not wired into production Compose/systemd. Phases A (provider-neutral core), B (Codex
native-mode adapter), C (Claude Code native-mode adapter) and D (Agent MCP surface) are
frozen. Phases A–C are merged into `main`; Phase D is committed on its own branch and
awaits merge.

**Phase D is frozen.** It added the eight provider-neutral Agent MCP tools, a thin
stdlib Unix-socket Bridge client, fail-closed global/per-workdir Agent configuration,
audit records, and the shared cross-process writer lease consumed by existing mutation
tools. Do not modify its MCP public surface, UDS protocol, local authorization model or
shared writer-lease contract except to fix a demonstrated defect. Phase D kept
`SERVERFS_AGENT_BRIDGE_ENABLED=false` as the default, so an upgrade retains the 11-tool
v0.2 surface unless the administrator explicitly enables Agent delegation. Agent tools
talk only to the Bridge RPC contract; they never import provider adapters or provider
SDKs into `serverfs-mcp`.

Phase E is the next active development phase after Phase D is merged.

Phase D's Bridge changes were limited to the socket/lock ownership mechanics the future
container-to-host local trust boundary needs. The target deployment is a dedicated shared
group: socket dir 0750/socket 0660 and pre-created lock dir 0750/lock files 0640. The MCP
container is to consume both directories through read-only bind mounts and must never
create host lock files. Production Compose/systemd wiring, host group creation, real
peer-ID measurement and ChatGPT E2E remain **Phase E** and are absent from Phase D.

Do not add a generic shell/argv/env MCP tool. Do not replace the eight tools with the MCP
Tasks extension yet: as of 2026-09-20 the official Python SDK still lists
`io.modelcontextprotocol/tasks` as not implemented. Keep the backend compatible with a
future Tasks adapter instead.
`SERVERFS_DISABLE_DEFAULT_DENY` is one rule this project deliberately reversed, and v0.1's
"read-only is a product property, not an option" was superseded by v0.2's per-workdir
opt-in. This file carries what none of them does: the reasons behind the design, the traps
that already cost debugging time here, and how work gets verified in this repository.

## Change protocol

Start from a written problem statement — a task-book section, a review issue, an
observed misbehaviour. Restate the issue first, then make every changed line trace
to it. This codebase has been hardened by successive review passes; unsolicited
rewrites and drive-by cleanups discard decisions that are not visible from the code.

Work in steps: modify, run the targeted test, then run the full suite. Reaching the
end of an edit is not a milestone; a passing targeted test is.

Every fix lands with a regression test, exercised through the MCP surface where the
bug was observable — a test that calls an internal helper proves less than one that
calls the tool.

Before declaring anything done, the full gate in `README.md` → Development passes:
`uv sync --frozen`, `ruff check`, `ruff format --check`, `pytest`,
`docker compose config`, `SERVERFS_IMAGE=serverfs-mcp:dev docker compose build`.
All six, actually executed. The scratch tag is not decoration: `image` doubles as
the tag Compose builds to, so an untagged build repoints whatever `SERVERFS_IMAGE`
names — see Traps.

Then report: files changed, how each issue was fixed, regression tests added, pytest
counts, and residual limitations. Anything not executed is `Not verified` — never
"should pass" or "theoretically fine".

## Channels

Every way a path is reached — read, stat, list, find, search, resource — is a
**channel**. The central invariant of this project is that **all channels filter
identically**.

A `DenyPolicy` is built once per call and travels on `ResolvedPath`; each channel
reads the policy from the resolved path rather than re-deriving rules. A new channel
or a new filter routes through that same object — a second matcher implementation is
a defect, not a shortcut.

`allow_hidden` and the credential deny rules are independent axes; all four
combinations are legal configurations and are covered by tests. Keep them uncoupled.
The reserved names are a third axis that is **not** configurable: they ride inside
`DenyPolicy.is_denied` so entry filtering and path resolution cannot drift apart, and
`resolve_workdir_path` raises `RESERVED_PATH` ahead of the deny check so the agent gets
the precise code. Two names are reserved — `.serverfs-tmp-*` (`RESERVED_TEMP_PREFIX`)
and the workdir registry's disabled-slot sentinel `.serverfs-disabled`
(`workdirs.DISABLED_SENTINEL`, enforced centrally by `paths.is_reserved_component`).
Adding a third internal name means adding it *there*, in `RESERVED_RG_EXCLUDES` and in
the reserved-channel tests — not at a call site.

Tracing a deny bypass means following the *full* workdir-relative path on every
channel. A policy decision made against a search root's own relative path is a
partial path, and partial paths are how bypasses ship.

## Mutation contract (v0.2)

Five tools, no general-purpose write. `create_text_file` and `create_directory` require
the target to be absent; `edit_text_file` requires it to exist and to be a UTF-8 regular
file; `delete_file` takes any regular file; `delete_directory` takes an empty directory.
No `force`, `recursive` or `overwrite` flag exists anywhere, and none may be added: the
agent-facing contract is "the precondition is the error message".

The mutation pipeline in `mutations.py` is `authorize (workdir read-write) → resolve
(policy) → root/parent FD walk → act on the final NAME with dir_fd=parent_fd`. Every
mutation takes the process-wide `mutation_lock()`; reads never do.

- **create** publishes with `os.link` from a reserved same-directory temp file, which
  cannot overwrite anything and leaves no check-then-create window. `create_text_file`
  deliberately advertises `idempotentHint=false`: even a repeat that ultimately fails
  with `PATH_ALREADY_EXISTS` creates and removes that temp entry first, so the parent
  directory's metadata/revision may change. `create_directory` remains idempotent.
- **edit** reads and verifies the target by FD, applies exact-match edits in memory,
  writes a temp file, copies mode/ownership/xattrs onto it, re-checks the revision, and
  publishes with `os.replace`. Nothing is written before every edit validates.
- **delete** re-checks the revision, then `unlinkat`/`rmdirat` by name while still
  holding the verified FD. `delete_file` refuses a file the process cannot read —
  directory write permission alone must not delete it.

Fatal versus logged: everything before the commit (temp `fsync`, metadata copy, the
final revision re-check) aborts the mutation; the directory `fsync` after the commit is
logged as `directory_fsync_failed` and the call still reports the mutation, because the
entry is already visible. The same reasoning applies to the text-file contract: the NUL
gate lives on both sides (`content` for create, `old_text`/`new_text` for edit) so no
mutation channel can produce a file every read channel then refuses.

Revision tokens are `v1:<16 hex>` of a SHA-256 over the stat tuple. Two rules make them
usable: compute them from the same object the caller will later stat, and derive them
from the whole tuple (size, mtime_ns, ctime_ns, nlink) so metadata changes are visible.
`read_text_file` fstats before and after reading and reports
`FILE_CHANGED_DURING_READ` rather than returning content that does not match its
revision. `edit`/`delete` verify `expected_revision` inside the lock *and* immediately
before the commit.

## Filesystem access

Request-derived traversal is FD-based: each component is opened relative to an
already-open directory descriptor — `dir_fd` plus `O_NOFOLLOW`, and `O_DIRECTORY` for
directories — and the final descriptor is `fstat`ed. `fdio.py` holds the shared
primitives and is the security boundary. No request-derived path travels as
`lstat`-then-`open(path)`: that gap is the TOCTOU window this design closes.

The workdir root is the one path opened by name — the trusted anchor from
configuration, carrying no request input, which is why the walk starts there.
Every root open goes through `fdio.open_root` / its context-managed wrapper
`fdio.root_fd`: `tools.py`, `filesystem.py` and `mutations.py` each keep a thin
`_root_fd` helper that delegates there, and `find_files` calls `open_root`
directly only because it owns the descriptor across a whole walk. Do not add a
fourth root-open implementation, and do not bypass the error mapping in `fdio`.

## Search

`rg` runs with `shell=False` and an argument array, `--json` output, streamed through
`selectors`, stopping at `limit + 1` policy-valid matches. Its cwd is the validated
directory FD via `/proc/self/fd/<fd>`, and result paths are re-checked against the
hidden and deny policy — validating the search root is one gate, not the only one.
`-L`/`--follow` are never passed; rg follows no symlinks.

## Logs

Structured JSON, one event per tool call. File contents, search queries, host and
container paths, and credentials stay out of the log, including error text.
Agent-facing errors are `CODE: short message`; internal paths are for DEBUG logs at
most.

## Intentional decisions

`SERVERFS_ALLOW_HIDDEN`, `SERVERFS_DISABLE_DEFAULT_DENY` and
`SERVERFS_EXTRA_DENY_GLOBS` are product features, not oversights: safe by default,
explicitly releasable by the administrator. `EXTRA_DENY_GLOBS` applies
unconditionally and survives `DISABLE_DEFAULT_DENY=true` by design — and so does the
reserved temp namespace.

`WORKDIR_XX_READ_ONLY` (default `true`) is the v0.2 write switch, and it is one variable
driving two layers on purpose: ServerFS's own authorization and the Compose bind-mount
flag. A read-only workdir must refuse mutations even if the mount is accidentally
writable — application authorization is checked before the path policy, so
`WORKDIR_READ_ONLY` wins over `HIDDEN_PATH_NOT_ALLOWED`. Parsing is strict
(`true/false/1/0/yes/no/on/off`, empty = read-only) and an unknown value is a startup
error: this switch must never fail open, and an upgrade from v0.1 must not gain write
access by omission.

The deployment shape — `serverfs-mcp` + `openai-tunnel`, internal-only network, no
published ports, no OAuth, 16 workdir slots — is fixed for v0.2. Shell execution,
indexing, a web UI, rename/move/copy, recursive mkdir/rmdir, binary editing, chmod/chown
and non-OpenAI clients are out of scope for v0.2, not pending work.

## Traps

- `O_NOFOLLOW|O_DIRECTORY` reports a symlink as `ENOTDIR`, not `ELOOP`. Classify it
  with a supplementary `lstat` after the open has already failed — the access itself
  is still refused by the kernel, so this adds no race.
- An empty component set means the workdir root: `os.dup` that descriptor.
  `/proc/self/fd/N` is a symlink in its own right and `O_NOFOLLOW` rejects it.
- `linkat`, `unlinkat` and `renameat` all update the *moved* inode's ctime and nlink.
  A revision computed before publication therefore does not match the next `stat_file`
  of the same file, and an agent that creates then immediately edits gets a spurious
  `REVISION_CONFLICT`. Compute the revision after the commit — both create and edit
  fstat the temp FD again once the name is in its final place.
- `os.scandir(fd)` does not close the descriptor it was given, so the FD walk helpers
  can keep owning and closing it. Do not "fix" the double-looking close.
- `Path.read_text()` translates CRLF to LF. Any test asserting byte fidelity of created
  or edited content must compare `read_bytes()`, or it fails on correct output.
- `docker compose config` refuses `1`/`0` for a boolean field
  (`failed to cast to expected type: invalid boolean: 1`) and merely warns on
  `yes/no/on/off` under YAML 1.2. `WORKDIR_XX_READ_ONLY` must be documented as
  `true`/`false`; the parsing differences between Compose and `parse_read_only` are
  fail-closed by construction, because an ambiguous value stops the deployment.
- A test helper that opens a different root than production hides real bugs. The
  scoped-search path collapse (`foo/foo/`) survived a full green suite because the
  helper passed the workdir root where production passes the search root. Keep
  helpers on the production call path.
- `docker compose config` interpolates `.env` and prints real secrets. Never paste
  its raw output; select the field you need, e.g.
  `docker compose config --format json | jq -r '.services["serverfs-mcp"].image'`.
- A bare `GET /mcp` against a running container can terminate the tunnel's active
  MCP session, leaving the deployment idle rather than visibly broken. Probe the
  tunnel's `/readyz` instead.
- The running deployment is live and serves a real tunnel. Exercise new behaviour on
  a throwaway stack under a separate compose project name rather than against it.
- Rebuilding the image is not deploying it: the running container keeps the old
  image until `docker compose up -d` recreates it.
- Recreating `serverfs-mcp` strands the tunnel, and `/readyz` will not tell you:
  `docker compose up -d` recreates only the service whose image changed, so the
  tunnel keeps running with its MCP session and connections belonging to a container
  that no longer exists — ready, quiet, idle. Restart it in the same breath
  (`docker compose restart openai-tunnel`). Its `mcp session initialized` line then
  reports the running `server_version`, which is the cheapest proof of what the
  deployment actually serves.
- `docker compose build` tags the result `SERVERFS_IMAGE`, which in a production
  `.env` is a pinned release (`ghcr.io/ntlx/serverfs_mcp:0.1.1`). A bare build
  therefore shadows that release locally: the running container is unaffected,
  but the next `up -d` starts v0.2 code under a v0.1.1 tag. Always build under a
  scratch tag (`SERVERFS_IMAGE=serverfs-mcp:dev docker compose build`). Upgrading
  a deployment is `pull` + `up -d`, never `build`.

## Release

Use `gh` for GitHub operations (`gh api`, `gh run`, `gh release`) rather than curl or
the web UI. When GitHub exposes no supported CLI or API operation for what you need —
GHCR package visibility is the known case — use the UI and report the exception
explicitly instead of reaching for an undocumented endpoint.

Tags drive images: `main` publishes `:edge`; a `vX.Y.Z` tag publishes `X.Y.Z`, `X.Y`
and `latest`. `latest` comes only from a stable tag, and the image version comes from
the Git tag — never from a GitHub Release event. Workflows pin every Action to a full
commit SHA and authenticate with `GITHUB_TOKEN` alone; pull-request builds never log
in, never push, and never write the shared build cache. Production deployments pin
`SERVERFS_IMAGE` to an exact version; `:edge` and `latest` are for trying things out.

End-to-end acceptance through ChatGPT belongs to the maintainer: an agent's reach
ends at the container's MCP surface. Say so rather than implying it was verified.

## Language

Code, comments, docstrings, tests, commits, pull requests and this file are English.
Talk to the maintainer in Chinese.