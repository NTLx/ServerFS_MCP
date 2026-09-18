# ServerFS MCP

> ServerFS MCP is a secure MCP server that exposes explicitly configured Linux directories as controlled workdirs to AI agents, **read-only by default** with opt-in per-workdir file mutation.

Agents reach your directories through the **OpenAI Secure MCP Tunnel**. They can list, find, search, read and stat files anywhere you mount — and, in workdirs you explicitly mark read-write, create, edit and delete files through five narrow, revision-guarded tools. Nothing else: no shell, no command execution, no overwrite, no recursive delete, no escape from the directories you configure.

```text
服务器上有哪些 workdir？        → list_workdirs
看一下 projects 根目录有什么    → list_directory
找所有 docker compose 配置     → find_files
搜索哪里配置了 DATABASE_URL    → search_text
打开对应配置文件               → read_text_file / stat_file
新建一个 Markdown 设计文档      → create_text_file
把端口 8080 改成 8081          → edit_text_file
删掉过期的构建产物              → delete_file
建一个 docs/v0.2 目录           → create_directory
删掉空的临时目录                → delete_directory
```

---

## Architecture

```text
Linux filesystem
   → Docker bind mounts (/workdirs/01..16)
        read-only by default
        read-write only where WORKDIR_XX_READ_ONLY=false
   → ServerFS MCP (streamable-http on :8000, internal network only)
        read tools:  list / find / search / read / stat
        mutation tools: create / edit / delete (read-write workdirs only)
   → OpenAI Secure MCP Tunnel (official tunnel-client container, outbound-only)
   → ChatGPT
```

The MCP server container has **no Internet egress** and no published ports. Only the tunnel container can reach it, over a Docker-internal network. The container root filesystem stays read-only regardless of any workdir setting.

## Prerequisites

- Linux server
- Docker + Docker Compose
- An OpenAI Secure MCP Tunnel (created in the OpenAI dashboard)

## Quick Start

```bash
git clone <repo> && cd serverfs-mcp
cp .env.example .env

# Edit .env:
#  1. set WORKDIR_XX_ALIAS / WORKDIR_XX_PATH for each directory to expose
#  2. fill CONTROL_PLANE_TUNNEL_ID and CONTROL_PLANE_API_KEY

chmod 600 .env          # the file contains a runtime API key
docker compose pull     # fetch the published image from GHCR
docker compose up -d
docker compose ps       # serverfs-mcp should become (healthy)
docker compose logs -f openai-tunnel
```

Building from source instead of pulling — build under a scratch tag, never under the tag a production `.env` pins:

```bash
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
SERVERFS_IMAGE=serverfs-mcp:dev docker compose up -d
```

A bare `docker compose build` writes to whatever `SERVERFS_IMAGE` names, so with a pinned production `.env` it would silently repoint that release tag at local code. Upgrading a pinned deployment pulls the published image instead; see [Upgrade](#upgrade).

## Docker Image & Release Channels

Images are published to GitHub Container Registry by GitHub Actions:

| Channel | Tag | Updated by |
|---|---|---|
| Stable | `ghcr.io/ntlx/serverfs_mcp:latest` | newest `vX.Y.Z` tag |
| Pinned release | `ghcr.io/ntlx/serverfs_mcp:0.2.0` | `v0.2.0` |
| Pinned minor | `ghcr.io/ntlx/serverfs_mcp:0.2` | newest `v0.2.x` |
| Development | `ghcr.io/ntlx/serverfs_mcp:edge` | every push to `main` |

Every image is multi-arch: `linux/amd64` and `linux/arm64`.

Release automation — the image version comes from the **Git tag**, never from a GitHub Release event:

```text
push to main   →  edge
tag vX.Y.Z     →  X.Y.Z  +  X.Y  +  latest
```

For example `v0.2.0` publishes `0.2.0`, `0.2` and `latest`. `latest` always points at the newest stable release; `main` never updates it (only `edge`).

## Workdir Configuration

Up to 16 slots. Each slot maps a host directory to an alias the agent sees:

```env
WORKDIR_01_ALIAS=projects
WORKDIR_01_PATH=/srv/projects
WORKDIR_01_DESCRIPTION="Projects"
WORKDIR_01_READ_ONLY=true          # read-only (default)

WORKDIR_02_ALIAS=scratch
WORKDIR_02_PATH=/srv/scratch
WORKDIR_02_DESCRIPTION="Agent scratch space"
WORKDIR_02_READ_ONLY=false         # read-write: the agent may modify files here
```

Rules:

- `ALIAS`: starts with a letter, then letters/digits/`_`/`-`, max 32 chars, unique, case-sensitive. Startup fails on duplicates.
- `PATH`: must already exist — Docker is configured with `create_host_path: false`, so a typo fails loudly instead of silently creating an empty directory.
- `READ_ONLY`: `true` (default) or `false`. Write it as `true`/`false` — the value feeds both ServerFS's own authorization and the Docker bind mount flag, and Docker Compose rejects `1`/`0` for the latter. Any unrecognised value stops the container at startup (`CONFIGURATION_ERROR`) instead of guessing. **A typo can never grant write access.**
- Leave both `ALIAS` and `PATH` empty to disable a slot.
- Host paths are never sent to the MCP container (only aliases are); the mapping exists only in Docker bind mounts.

### Read-only by default, and after upgrades

`WORKDIR_XX_READ_ONLY` is **absent** from every v0.1 configuration. Upgrading to v0.2 therefore leaves all existing workdirs read-only: nothing becomes writable until you write `false` yourself. The startup log records how many workdirs are writable.

The variable controls two independent layers from one place:

| Layer | Effect |
|---|---|
| ServerFS authorization | A read-only workdir refuses every mutation with `WORKDIR_READ_ONLY`, even if the mount is writable |
| Docker bind mount | `read_only: true` on `/workdirs/XX` — the kernel refuses writes even if the application were compromised |

Both layers must be released for a mutation to reach the disk.

### What the agent sees

Agents address files as `workdir + relative path` (`{"workdir": "projects", "path": "PandaWiki/docker-compose.yml"}`) and call `list_workdirs` first to learn each workdir's `access` (`read-only` or `read-write`). Host paths like `/srv/projects` are never exposed in tool results, error messages, or logs.

## Linux Permissions

The container runs as UID/GID `10001` by default (`SERVERFS_UID` / `SERVERFS_GID`).

**Docker's `read_only` bind mount does not bypass Linux file permissions.** The UID must be able to *read* the host directories. If a directory is not readable, you get `Permission denied` — that is correct behavior, not a bug. Do not `chmod 777` or `chown` host trees to work around it; instead grant read access to the specific UID/GID (e.g. via a group).

A **read-write** workdir needs more:

- write permission on the directory itself (create, edit, delete all need it), plus the execute bit to traverse it;
- for editing, the UID must own the file or be able to replace it — ServerFS preserves the original mode, ownership and extended attributes, and **refuses the edit** (`METADATA_PRESERVATION_FAILED`) rather than silently changing an owner it cannot reproduce. Files owned by another user are therefore not editable by a non-root container.

Point the read-write workdir at a directory whose owner/group model already matches the ServerFS UID/GID — typically `chown -R 10001:10001` on a dedicated scratch directory, or a group the container is a member of. Never `chmod -R 777`.

> Never mount the host filesystem root, Docker socket (`/var/run/docker.sock`), SSH directories, credential stores, or other broad sensitive locations as a workdir.

## OpenAI Tunnel Setup

1. Create a tunnel in the OpenAI dashboard; note its **Tunnel ID**.
2. Create a **Runtime API Key** with `Tunnels Read` + `Tunnels Use` permissions (not an Admin Key — Admin Keys are only for tunnel CRUD, and this project never needs one).
3. Fill in `.env`:

```env
CONTROL_PLANE_TUNNEL_ID=tunnel_...
CONTROL_PLANE_API_KEY=rtk_...
```

The tunnel is **outbound-only**: no public domain, no TLS certificate, no inbound firewall rule, no reverse proxy. The container connects out to OpenAI's control plane and forwards MCP traffic to `http://serverfs-mcp:8000/mcp` over the internal Docker network.

To troubleshoot the tunnel, use the official client's own diagnostics (`tunnel-client doctor`, `/readyz`) rather than guessing.

## Security Model

Defense in depth — each layer is independent:

| Layer | Guarantee |
|---|---|
| Read-only by default | Six read tools work everywhere. Five mutation tools exist but refuse to act in any workdir that is not explicitly configured read-write — a write capability that has to be turned on per workdir, never a global switch. There is still no shell, no command execution and no generic `write_file`. |
| Per-workdir mutation opt-in | `WORKDIR_XX_READ_ONLY` (default `true`) drives both the ServerFS authorization check and the Docker bind mount. Application authorization is checked *first* and independently: a read-only workdir answers `WORKDIR_READ_ONLY` even if the mount is writable. |
| Create ≠ edit | `create_text_file` never overwrites (it fails with `PATH_ALREADY_EXISTS` on any existing file, directory or symlink) and `edit_text_file` never creates (a mistyped path fails with `PATH_NOT_FOUND` instead of silently becoming a new file). Both file and directory operations are separate tools — no `type: "file" \| "directory"` switch. |
| Optimistic concurrency | `read_text_file` and `stat_file` return an opaque `revision` (`v1:…`, a digest of the object's stat tuple — never a raw inode/UID/GID). `edit_text_file`, `delete_file` and `delete_directory` require the caller's `expected_revision` and fail with `REVISION_CONFLICT` if the object changed. |
| Mutation serialization | All mutations run under one process-wide lock, so two callers holding the same revision cannot both commit; exactly one wins and the other gets `REVISION_CONFLICT`. Reads never take the lock. |
| Exact-match edits | Edits replace literal text (never regex, never fuzzy), must match `expected_count` occurrences exactly, and apply in order as an all-or-nothing transaction. No edit touches the disk until every edit has been validated. |
| Atomic publication | `create_text_file` writes a reserved same-directory temp file and publishes it with `linkat(2)`, which cannot overwrite. `edit_text_file` writes a temp file and publishes with `renameat(2)`. A concurrent reader sees either the complete old or the complete new content — never a partial file, and never a truncated-then-rewritten file. |
| Metadata preservation | Editing replaces an inode, so ServerFS copies ownership, mode and extended attributes onto the replacement *before* the rename — in that order, because `chown(2)` clears setuid/setgid bits and can disturb `security.*` metadata — and fails with `METADATA_PRESERVATION_FAILED` if any of them cannot be reproduced. A file with multiple hard links is refused outright (`MULTIPLE_HARDLINKS_NOT_SUPPORTED` rather than silently splitting the link). |
| No recursive delete, no force | `delete_directory` removes an empty directory only; anything inside it — hidden, denied or a leftover temp file — yields `DIRECTORY_NOT_EMPTY` and the interior is never named in the error. `create_directory` is not recursive. No tool takes a `force`, `recursive` or `overwrite` argument. |
| Workdir root is immutable | The workdir root itself can never be created, edited or deleted (`ROOT_MUTATION_NOT_ALLOWED`). |
| Reserved names | Two internal names are hard-reserved: `.serverfs-tmp-*` (atomic-publication temp files) and `.serverfs-disabled` (the workdir registry's disabled-slot marker, which startup reads — an agent able to create it would break the next start). Neither is listable, findable, searchable, readable, stat-able or mutable, in every configuration: `SERVERFS_ALLOW_HIDDEN` and `SERVERFS_DISABLE_DEFAULT_DENY` do not release them, and ripgrep is told to skip those names outright. |
| Tool annotations | Read tools advertise `readOnlyHint=true`; mutation tools advertise `read_only=false`; `create_*` are non-destructive while `edit`/`delete_*` are destructive. `create_directory`, `edit_text_file` and `delete_*` advertise `idempotentHint=true`; `create_text_file` deliberately advertises `idempotentHint=false` because a failed repeat still creates and cleans up a same-directory temp entry, which can change parent-directory metadata/revision even though the target file is unchanged. All tools advertise `openWorldHint=false`. (Hints, not a security mechanism.) |
| Path resolution | Every path is normalized and confined to the workdir root. `..`, absolute paths, NUL bytes rejected. |
| FD-based traversal | All filesystem access — reads *and* mutations — walks components with `openat(2)` + `O_NOFOLLOW` on directory file descriptors, and mutations act on the final name through the parent FD (`mkdirat`, `linkat`, `renameat`, `unlinkat`, `rmdirat`). Component identity and symlink rejection are atomic at open time, so there is no lstat→open TOCTOU window. A symlink as a *parent* component is rejected (`SYMLINK_NOT_ALLOWED`) including links pointing inside the same workdir; a symlink as the *final* component is reported by `stat_file` as `type: "symlink"` (target never revealed), rejected by `read_text_file`/`list_directory`, and never followed or replaced by a mutation. rg runs rooted at a pre-validated directory FD (`/proc/self/fd`) with symlink following never enabled. |
| Special files | FIFOs, sockets and device files appear in `list_directory`/`stat_file` as `type: "other"` but are rejected before any content read (`UNSUPPORTED_FILE_TYPE`) — reads can never block. `delete_file` accepts regular files only: a FIFO or socket yields `UNSUPPORTED_FILE_TYPE`, a directory `NOT_A_FILE`. |
| Hidden files | Dot-prefixed path components are denied everywhere (list/find/search/read/stat/resource/create/edit/delete/mkdir/rmdir), not just hidden from listings. With `SERVERFS_ALLOW_HIDDEN=true` they become visible and mutable on *every* channel, still subject to the deny rules. |
| Credential deny rules | `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`, `.ssh/`, `.aws/`, `.gnupg/`, `.kube/` are denied on every channel — reads *and* mutations, so a would-be path (`create_text_file(".env")`) is refused before anything is created. Append your own patterns via `SERVERFS_EXTRA_DENY_GLOBS` (e.g. `*.sqlite,internal/**`); those apply unconditionally. The built-in set can be released with `SERVERFS_DISABLE_DEFAULT_DENY=true` — see the warning below. |
| Read limits | `SERVERFS_MAX_READ_LINES` (500) and `SERVERFS_MAX_READ_BYTES` (512 KiB); a single line over the byte budget returns `LINE_TOO_LARGE` rather than a truncated line. |
| Write limits | `SERVERFS_MAX_WRITE_BYTES` (1 MiB) bounds `create_text_file` content, an edited source file, an edit result and the combined size of one call's `old_text`+`new_text`. `SERVERFS_MAX_EDITS_PER_CALL` (50) bounds one call's edit list. Content containing NUL yields `BINARY_CONTENT_NOT_ALLOWED`; binary *files* cannot be edited (`BINARY_FILE`) but can be deleted. |
| Search limits | rg subprocess with argument-array invocation (no shell, no string concatenation), streamed `--json` output, wall-clock 15 s deadline (terminate → grace → kill, no orphan processes), 50 MiB per-file ceiling, and a true *global* result limit: rg is terminated as soon as `limit + 1` policy-valid matches exist, instead of scanning the whole tree. Result paths are re-checked against hidden/deny policy. |
| Audit log | Every tool call emits a structured `tool_call` event (tool, workdir, relative path, duration, success, `error_code`, plus per-tool counts and the new revision). File contents, `old_text`/`new_text`, search queries and host/container paths are never logged. The `startup` event records the effective security mode (`allow_hidden`, `default_deny_enabled`, `extra_deny_rule_count`, `read_write_workdirs`). |
| Docker | Read-only bind mounts by default (`create_host_path: false`), read-only container root filesystem, tmpfs `/tmp`, non-root UID 10001, `cap_drop: ALL`, `no-new-privileges`. `/workdirs/XX` becomes writable only when `WORKDIR_XX_READ_ONLY=false`; `/app`, the Python package and every system directory stay unwritable either way. |
| Network | MCP container is on an `internal: true` network only — no Internet egress, no published ports. Only the tunnel container bridges to the outside. |
| Secrets | `CONTROL_PLANE_*` never enters the MCP container (verified with `docker compose exec serverfs-mcp env`). |

File contents are treated as **untrusted data** — ServerFS only returns them as text and never acts on anything inside them.

Error messages are short, agent-recoverable codes (`PATH_NOT_FOUND`, `SYMLINK_NOT_ALLOWED`, …) and never contain internal container paths or host paths.

> **Warning — `SERVERFS_DISABLE_DEFAULT_DENY=true`**: this releases only the *built-in* credential rules (`.env`, `*.pem`, `id_rsa`, `.ssh/**`, …), letting the agent read **and modify** credential material inside your workdirs. `SERVERFS_EXTRA_DENY_GLOBS` still applies and is the intended place for compensating rules. Hidden-path filtering (`SERVERFS_ALLOW_HIDDEN`) is a separate, independent switch. Use this option only when a workdir legitimately contains files matching the built-in patterns and you have reviewed the exposure.

### Concurrent writes: what ServerFS does and does not guarantee

ServerFS serializes mutations *within its own process* and re-checks the revision immediately before committing, so two agents cannot both apply an edit to the same revision. It does **not** provide linearizable transactions against writers it does not control: a host user, an IDE or another container can still rename a pathname in the window between the final check and the `renameat(2)`. Treat a read-write workdir as shared, and prefer pointing it at scratch space rather than at a directory a human edits at the same time.

## Tools

Read tools (work in every workdir):

| Tool | Purpose |
|---|---|
| `list_workdirs` | Discover configured workdirs, including each one's `access` (`read-only` / `read-write`) |
| `list_directory` | Sorted directory listing with offset/limit pagination |
| `find_files` | Recursive filename glob search; `truncated=true` whenever the scan stopped early at the match limit or the walk-entry cap |
| `search_text` | Literal (non-regex) content search via ripgrep, global streamed result limit with early-stop |
| `read_text_file` | UTF-8 reading with line pagination, byte caps and a `revision` for later edits |
| `stat_file` | type (`file`/`directory`/`symlink`/`other`) / size / mtime (RFC 3339 UTC) / best-effort MIME / `revision` |

Mutation tools (read-write workdirs only; all require the path's parent to exist):

| Tool | Contract |
|---|---|
| `create_text_file` | Create a **new** UTF-8 text file. Any existing object at the path → `PATH_ALREADY_EXISTS`. Never overwrites. Atomic. |
| `edit_text_file` | Exact-match replacement in an **existing** UTF-8 text file, guarded by `expected_revision`. Never creates. All-or-nothing across the call's edits. |
| `delete_file` | Delete one regular file (binary included), guarded by `expected_revision`. Permanent. |
| `create_directory` | Create **one** directory; the parent must exist (no `mkdir -p`). `PATH_ALREADY_EXISTS` if anything is already there. |
| `delete_directory` | Delete **one empty** directory, guarded by `expected_revision`. Never recursive. |

A `serverfs://{workdir}/{path}` resource template is also exposed; it goes through the exact same validation as `read_text_file` and is **read-only** — mutations are available as tools only. Resources are all-or-nothing: a file that exceeds the read budget returns `RESOURCE_TOO_LARGE` instead of a silently truncated body — use `read_text_file` for paginated access.

Common error codes: `WORKDIR_READ_ONLY`, `PATH_ALREADY_EXISTS`, `PARENT_NOT_FOUND`, `ROOT_MUTATION_NOT_ALLOWED`, `REVISION_CONFLICT`, `EDIT_CONFLICT`, `TOO_MANY_EDITS`, `WRITE_TOO_LARGE`, `BINARY_CONTENT_NOT_ALLOWED`, `BINARY_FILE`, `DIRECTORY_NOT_EMPTY`, `MULTIPLE_HARDLINKS_NOT_SUPPORTED`, `METADATA_PRESERVATION_FAILED`, `RESERVED_PATH`, plus the read-channel codes (`PATH_NOT_FOUND`, `SYMLINK_NOT_ALLOWED`, `DENIED_PATH`, `HIDDEN_PATH_NOT_ALLOWED`, `UNSUPPORTED_FILE_TYPE`, …).

## Operations

```bash
docker compose up -d
docker compose down
docker compose ps
docker compose logs -f
```

## Upgrade

Two distinct paths — do not mix them.

Upgrading a **deployed instance** uses the published image: edit `SERVERFS_IMAGE`, then

```bash
docker compose pull
docker compose up -d
```

Building the **source** yourself (dependency pins, local changes) uses a scratch tag, so a pinned release tag is never repointed at local code:

```bash
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
SERVERFS_IMAGE=serverfs-mcp:dev docker compose up -d
```

Dependency versions are pinned: `mcp==2.2.0` in `pyproject.toml`/`uv.lock`, the builder image `ghcr.io/astral-sh/uv:0.12.15` in the `Dockerfile`, and the tunnel image `ghcr.io/openai/tunnel-client:v0.0.14` in `.env.example`. Upgrade deliberately by changing those pins and rebuilding along the source path. Avoid `latest`.

For **production**, pin `SERVERFS_IMAGE` to an exact release instead of `latest`:

```env
SERVERFS_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.2.0
```

Pinned deploys are reproducible, upgrades are explicit, and rollback is a one-line change back to the previous version. `latest` is convenient for a first look, not for a long-lived deployment.

### Upgrading from v0.1

Nothing to do beyond bumping `SERVERFS_IMAGE`: the new tool surface is additive, every existing workdir stays read-only (no `WORKDIR_XX_READ_ONLY` in a v0.1 `.env` means `true`), and the deny/hidden policy is unchanged. Run `docker compose pull && docker compose up -d` last — starting the container is what replaces the running one.

## Development

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose config
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
```

The scratch tag on the last line matters: `image` doubles as the tag Compose builds to, so an untagged build with a pinned production `.env` present would repoint that release tag at your working tree.

## Not in v0.2 (by design)

No rename/move/copy, no recursive mkdir or delete, no binary or non-UTF-8 file editing, no chmod/chown, no symlink or hardlink creation, no file upload, no shell or command execution, no Git operations, no automatic backup or trash, no database/index/RAG, no ACL management, no cross-workdir move, no OAuth/SSO, no web UI, no file watching, no client support beyond the OpenAI tunnel.
