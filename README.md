# ServerFS MCP

> ServerFS MCP is a secure MCP server that exposes explicitly configured Linux directories as controlled workdirs to AI agents, **read-only by default** with opt-in per-workdir file mutation.

Agents reach your directories through the **OpenAI Secure MCP Tunnel**. They can list, find, search, read and stat files anywhere you mount; optionally transfer bounded whole binary files; and, in workdirs you explicitly mark read-write, create, edit, delete or revision-guarded replace files through narrow tools. Nothing else: no shell, no command execution, no unguarded overwrite, no recursive delete, no escape from the directories you configure.

```text
服务器上有哪些 workdir？        → list_workdirs
看一下 projects 根目录有什么    → list_directory
找所有 docker compose 配置     → find_files
搜索哪里配置了 DATABASE_URL    → search_text
打开对应配置文件               → read_text_file / stat_file
下载 PNG / ZIP 等原始字节       → download_binary_file（可选）
上传或受控替换二进制文件         → upload_binary_file（可选）
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
        optional binary path: download / upload / revision-guarded replace
        optional ChatGPT file ingress → isolated sidecar → temporary HTTPS file URL
        optional Agent path: eight Agent tools → host Agent Bridge → Codex/Claude
   → OpenAI Secure MCP Tunnel (official tunnel-client container, outbound-only)
   → ChatGPT
```

The MCP server container has **no Internet egress** and no published ports. The tunnel reaches it over a Docker-internal network. v0.5.0 adds an optional, separately isolated `serverfs-file-ingress` sidecar for ChatGPT file parameters; only that sidecar receives file-download egress, it has no workdir mounts or OpenAI credentials, and the main MCP container remains internal-only. The container root filesystem stays read-only regardless of any workdir setting.

The default `compose.yml` exposes the original 11 filesystem tools. Binary transfer is opt-in: when at least one workdir enables it, `download_binary_file` and `upload_binary_file` are added, producing a 13-tool filesystem surface. When the administrator also configures Agent policy and uses `compose.agent.yml`, the overlay adds eight structured Agent tools. The four supported surfaces are therefore 11 / 13 / 19 / 21 tools for filesystem-only / filesystem+binary / filesystem+Agent / filesystem+binary+Agent. Agent tools broker structured tasks through the host-side Bridge; they are not a shell, argv, or generic command executor. Delegated tasks should therefore stay objective-level and capability-bounded: one authorized goal, explicit mutation scope/stop conditions, and only the context/evidence needed for that goal. This improves clarity and reduces accidental ambiguity; it is not intended to bypass provider safety checks.

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

The default Quick Start remains the base 11-tool deployment. For the optional
Agent deployment, see [the Phase E guide](deployment/agent-bridge/README.md)
and [compose.agent.yml](compose.agent.yml); it is an explicit overlay.

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
| Pinned release | `ghcr.io/ntlx/serverfs_mcp:0.5.0` | `v0.5.0` |
| Pinned minor | `ghcr.io/ntlx/serverfs_mcp:0.5` | newest `v0.5.x` tag |
| Development | `ghcr.io/ntlx/serverfs_mcp:edge` | every push to `main` |

Every image is multi-arch: `linux/amd64` and `linux/arm64`.

Release automation — the image version comes from the **Git tag**, never from a GitHub Release event:

```text
push to main   →  edge
tag vX.Y.Z     →  X.Y.Z  +  X.Y  +  latest
```

`v0.5.0` is published: the tag produced `0.5.0`, `0.5` and `latest`. `latest` always points at the newest published stable release; `main` never updates it. See [ServerFS MCP v0.5.0 on GitHub](https://github.com/NTLx/ServerFS_MCP/releases/tag/v0.5.0).

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

### Global defaults and workdir overrides

v0.4 resolves one immutable effective policy for every enabled workdir at startup. Global `SERVERFS_*` values are defaults; an explicit `WORKDIR_XX_*` scalar override wins for that slot, while an empty workdir value inherits the global default. This applies to hidden-file policy, read/write limits, binary transfer, and Agent policy. `EXTRA_DENY_GLOBS` is intentionally stricter: global and workdir deny globs are **unioned**, so a workdir can add restrictions but cannot remove the global deny floor.

Binary transfer is disabled by default. Enable it globally with `SERVERFS_BINARY_TRANSFER_ENABLED=true` or for one slot with `WORKDIR_XX_BINARY_TRANSFER_ENABLED=true`. `SERVERFS_MAX_BINARY_TRANSFER_BYTES` / `WORKDIR_XX_MAX_BINARY_TRANSFER_BYTES` bound both upload and download; the default is 8 MiB. Enabling binary transfer does not release write authorization: uploads still require `WORKDIR_XX_READ_ONLY=false`.

v0.5.0 optionally accepts a ChatGPT/OpenAI file parameter as the upload source. This path is separately disabled by default. To enable it, set `SERVERFS_FILE_INGRESS_ENABLED=true` and start Compose with `--profile file-ingress`. The sidecar requires a narrow host policy: exact hosts may be listed in `SERVERFS_FILE_INGRESS_ALLOWED_HOSTS`; for real ChatGPT fileParams, set `SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=true` to admit only the measured `oaisdmntpr<Azure-storage-account-suffix>.blob.core.windows.net` family. Generic `*.blob.core.windows.net` wildcards remain unsupported. `upload_binary_file` then advertises `_meta["openai/fileParams"] = ["file"]` and accepts exactly one of `data_base64` or `file`. The client-supplied `file_name`, `file_id` and temporary URL never select the ServerFS destination; the explicit `path` argument remains authoritative.

Agent policy follows the same inheritance model via `SERVERFS_AGENT_MODE` / `SERVERFS_AGENT_RUNTIMES` and the workdir overrides. `SERVERFS_AGENT_BRIDGE_ENABLED` remains the separate infrastructure master gate.

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
| No recursive delete, no unguarded force | `delete_directory` removes an empty directory only; anything inside it — hidden, denied or a leftover temp file — yields `DIRECTORY_NOT_EMPTY` and the interior is never named in the error. `create_directory` is not recursive. There is no `force` or `recursive` mode. The only overwrite path is `upload_binary_file(overwrite=true)`, and it requires the caller's `expected_revision` for one existing regular file. |
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
| Network | The MCP container remains on `internal: true` networks only — no Internet egress and no published ports. The tunnel has its own egress network. v0.5.0's optional file-ingress sidecar has a separate egress network but no workdir mounts or OpenAI credentials; MCP can reach it only over a dedicated internal network, and the tunnel is not attached to that network. The sidecar accepts HTTPS/443 only, requires either exact allowlisted hosts or the explicit constrained OpenAI Azure-Blob account family (`oaisdmntpr` prefix, Azure account-name length/charset, fixed `.blob.core.windows.net` suffix), rejects non-global DNS answers, pins connections to validated IPs while verifying the original TLS hostname, and revalidates every redirect. Generic host wildcards remain unsupported. Streamable HTTP DNS-rebinding protection introduced in v0.4.0 remains unchanged in v0.5.0: only `serverfs-mcp:8000` is accepted and no non-empty Origin is allowed. |
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

Optional binary tools (registered only when at least one workdir enables binary transfer):

| Tool | Contract |
|---|---|
| `download_binary_file` | Return exact raw bytes as an MCP `BlobResourceContents`, plus size / MIME / SHA-256 / revision metadata. Enforces the effective binary size limit and rejects files that change during the read. |
| `upload_binary_file` | Whole-file upload from exactly one source: strict RFC 4648 `data_base64`, or (when optional file ingress is enabled) a ChatGPT/OpenAI `file` parameter. Default `overwrite=false` creates only. `overwrite=true` requires `expected_revision`, replaces one existing regular file atomically, preserves metadata, and rejects stale revisions or multi-hardlink targets. |

Mutation tools (read-write workdirs only; all require the path's parent to exist):

| Tool | Contract |
|---|---|
| `create_text_file` | Create a **new** UTF-8 text file. Any existing object at the path → `PATH_ALREADY_EXISTS`. Never overwrites. Atomic. |
| `edit_text_file` | Exact-match replacement in an **existing** UTF-8 text file, guarded by `expected_revision`. Never creates. All-or-nothing across the call's edits. |
| `delete_file` | Delete one regular file (binary included), guarded by `expected_revision`. Permanent. |
| `create_directory` | Create **one** directory; the parent must exist (no `mkdir -p`). `PATH_ALREADY_EXISTS` if anything is already there. |
| `delete_directory` | Delete **one empty** directory, guarded by `expected_revision`. Never recursive. |

A `serverfs://{workdir}/{path}` resource template is also exposed; it goes through the exact same validation as `read_text_file` and is **read-only** — mutations are available as tools only. Resources are all-or-nothing: a file that exceeds the read budget returns `RESOURCE_TOO_LARGE` instead of a silently truncated body — use `read_text_file` for paginated access.

Common error codes: `WORKDIR_READ_ONLY`, `BINARY_TRANSFER_DISABLED`, `BINARY_FILE_TOO_LARGE`, `BINARY_PAYLOAD_TOO_LARGE`, `BINARY_SOURCE_REQUIRED`, `BINARY_SOURCE_CONFLICT`, `INVALID_BASE64`, `FILE_INGRESS_DISABLED`, `FILE_INGRESS_UNAVAILABLE`, `FILE_INGRESS_FAILED`, `FILE_INGRESS_URL_NOT_ALLOWED`, `FILE_INGRESS_HOST_NOT_ALLOWED`, `FILE_INGRESS_ADDRESS_NOT_ALLOWED`, `FILE_INGRESS_DNS_FAILED`, `FILE_INGRESS_TOO_MANY_REDIRECTS`, `FILE_INGRESS_UPSTREAM_FAILED`, `PATH_ALREADY_EXISTS`, `PARENT_NOT_FOUND`, `ROOT_MUTATION_NOT_ALLOWED`, `REVISION_REQUIRED`, `REVISION_CONFLICT`, `EDIT_CONFLICT`, `TOO_MANY_EDITS`, `WRITE_TOO_LARGE`, `BINARY_CONTENT_NOT_ALLOWED`, `BINARY_FILE`, `DIRECTORY_NOT_EMPTY`, `MULTIPLE_HARDLINKS_NOT_SUPPORTED`, `METADATA_PRESERVATION_FAILED`, `RESERVED_PATH`, plus the read-channel codes (`PATH_NOT_FOUND`, `SYMLINK_NOT_ALLOWED`, `DENIED_PATH`, `HIDDEN_PATH_NOT_ALLOWED`, `UNSUPPORTED_FILE_TYPE`, …).

## Operations

```bash
docker compose up -d
docker compose down
docker compose ps
docker compose logs -f
```

## Upgrade

Two distinct paths — do not mix them.

Upgrading a **deployed instance** uses the published image: edit `SERVERFS_IMAGE`, then preserve the same deployment surface when recreating services.

Base filesystem-only / binary deployment:

```bash
docker compose pull
docker compose up -d
docker compose restart openai-tunnel
```

Agent-enabled deployment — **always keep the Agent overlay**:

```bash
docker compose -f compose.yml -f compose.agent.yml pull
docker compose -f compose.yml -f compose.agent.yml up -d
docker compose -f compose.yml -f compose.agent.yml restart openai-tunnel
```

If v0.5.0 ChatGPT file ingress is also enabled, add `--profile file-ingress` to the same Compose invocation; do not replace the Agent overlay with the profile. Recreating `serverfs-mcp` with only the base file removes the Agent socket/lock mounts and makes the runtime surface Agent-disabled even when the `.env` still contains valid Agent policy.

Building the **source** yourself (dependency pins, local changes) uses a scratch tag, so a pinned release tag is never repointed at local code:

```bash
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
SERVERFS_IMAGE=serverfs-mcp:dev docker compose up -d
```

Dependency versions are pinned: `mcp==2.2.0` in `pyproject.toml`/`uv.lock`, the builder image `ghcr.io/astral-sh/uv:0.12.15` in the `Dockerfile`, and the tunnel image `ghcr.io/openai/tunnel-client:v0.0.14` in `.env.example`. Upgrade deliberately by changing those pins and rebuilding along the source path. Avoid `latest`.

For **production**, pin `SERVERFS_IMAGE` to an exact published release instead of `latest`:

```env
SERVERFS_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.5.0
```

Pinned deploys are reproducible, upgrades are explicit, and rollback is a one-line change back to the previous version. `latest` is convenient for a first look, not for a long-lived deployment.

### Upgrading to v0.5.0

The upgrade is backward-compatible by default: `SERVERFS_FILE_INGRESS_ENABLED=false`, `SERVERFS_FILE_INGRESS_ALLOW_OPENAI_BLOB_HOSTS=false`, the ingress sidecar is not started unless the `file-ingress` profile is selected, and existing Base64 binary transfer continues to work. For current ChatGPT fileParams, set both booleans to `true` and add `--profile file-ingress` to the same Compose command you already use. Exact additional hosts can still be supplied through `SERVERFS_FILE_INGRESS_ALLOWED_HOSTS`; generic wildcards are rejected.

Rollback is equally narrow: set `SERVERFS_FILE_INGRESS_ENABLED=false`, stop/remove the optional `serverfs-file-ingress` profile service if it was running, pin `SERVERFS_IMAGE` back to the previous release, then `pull` + `up -d` using the same base/Agent overlay shape as before and restart `openai-tunnel`. No workdir data migration is involved.

### Upgrading from v0.1

Nothing to do beyond bumping `SERVERFS_IMAGE`: the new tool surface is additive, every existing workdir stays read-only (no `WORKDIR_XX_READ_ONLY` in a v0.1 `.env` means `true`), and the deny/hidden policy is unchanged. Preserve the deployment surface shown above when recreating the container.

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

## Still not in v0.5.0 (by design)

No generic URL downloader, no rename/move/copy, no recursive mkdir or delete, no in-place binary editing API, no chmod/chown tools, no symlink or hardlink creation, no chunked/resumable transfer sessions, no shell or command execution, no Git operations, no automatic backup or trash, no database/index/RAG, no ACL management, no cross-workdir move, no OAuth/SSO, no web UI, and no file watching. Binary transfer remains bounded whole-file transfer; the optional ChatGPT file-ingress sidecar is a narrow, policy-checked HTTPS ingress capability that accepts only exact administrator hosts or the explicit constrained OpenAI Blob family rather than acting as a general proxy.
