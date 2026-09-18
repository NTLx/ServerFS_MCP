# ServerFS MCP

> ServerFS MCP is a secure, read-only MCP server that exposes selected Linux filesystem directories as workdirs to AI agents in real time.

v0.1 exposes your directories to ChatGPT / OpenAI agents through the **OpenAI Secure MCP Tunnel**. Agents can list, find, search, read and stat files — but never write, execute, or escape the directories you configure.

```text
服务器上有哪些 workdir？        → list_workdirs
看一下 projects 根目录有什么    → list_directory
找所有 docker compose 配置     → find_files
搜索哪里配置了 DATABASE_URL    → search_text
打开对应配置文件               → read_text_file / stat_file
```

---

## Architecture

```text
Linux filesystem
   → Docker read-only bind mounts (/workdirs/01..16)
   → ServerFS MCP (read-only tools, streamable-http on :8000, internal network only)
   → OpenAI Secure MCP Tunnel (official tunnel-client container, outbound-only)
   → ChatGPT
```

The MCP server container has **no Internet egress** and no published ports. Only the tunnel container can reach it, over a Docker-internal network.

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

Building from source instead of pulling: run `docker compose build` before `docker compose up -d`.

## Docker Image & Release Channels

Images are published to GitHub Container Registry by GitHub Actions:

| Channel | Tag | Updated by |
|---|---|---|
| Stable | `ghcr.io/ntlx/serverfs_mcp:latest` | newest `vX.Y.Z` tag |
| Pinned release | `ghcr.io/ntlx/serverfs_mcp:0.1.0` | `v0.1.0` |
| Pinned minor | `ghcr.io/ntlx/serverfs_mcp:0.1` | newest `v0.1.x` |
| Development | `ghcr.io/ntlx/serverfs_mcp:edge` | every push to `main` |

Every image is multi-arch: `linux/amd64` and `linux/arm64`.

Release automation — the image version comes from the **Git tag**, never from a GitHub Release event:

```text
push to main   →  edge
tag vX.Y.Z     →  X.Y.Z  +  X.Y  +  latest
```

For example `v0.1.2` publishes `0.1.2`, `0.1` and `latest`. `latest` always points at the newest stable release; `main` never updates it (only `edge`).

## Workdir Configuration

Up to 16 slots. Each slot maps a host directory to an alias the agent sees:

```env
WORKDIR_01_ALIAS=projects
WORKDIR_01_PATH=/srv/projects
WORKDIR_01_DESCRIPTION="Projects"

WORKDIR_02_ALIAS=logs
WORKDIR_02_PATH=/var/log/myapp
WORKDIR_02_DESCRIPTION="Application logs"
```

Rules:

- `ALIAS`: starts with a letter, then letters/digits/`_`/`-`, max 32 chars, unique, case-sensitive. Startup fails on duplicates.
- `PATH`: must already exist — Docker is configured with `create_host_path: false`, so a typo fails loudly instead of silently creating an empty directory.
- Leave both `ALIAS` and `PATH` empty to disable a slot.
- Host paths are never sent to the MCP container (only aliases are); the mapping exists only in Docker bind mounts.

### What the agent sees

Agents address files as `workdir + relative path` (`{"workdir": "projects", "path": "PandaWiki/docker-compose.yml"}`). Host paths like `/srv/projects` are never exposed in tool results, error messages, or logs.

## Linux Permissions

The container runs as UID/GID `10001` by default (`SERVERFS_UID` / `SERVERFS_GID`).

**Docker's `read_only` bind mount does not bypass Linux file permissions.** The UID must be able to *read* the host directories. If a directory is not readable, you get `Permission denied` — that is correct behavior, not a bug. Do not `chmod 777` or `chown` host trees to work around it; instead grant read access to the specific UID/GID (e.g. via a group).

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
| Read-only MCP tools | Only 6 tools exist; there is no write/execute capability and no configuration to enable one. Read-only is a product property, not an option. |
| Tool annotations | All tools advertise `readOnlyHint=true`, `openWorldHint=false`. (Hints, not a security mechanism.) |
| Path resolution | Every path is normalized and confined to the workdir root. `..`, absolute paths, NUL bytes rejected. |
| FD-based traversal | All filesystem access walks components with `openat(2)` + `O_NOFOLLOW` on directory file descriptors — component identity and symlink rejection are atomic at open time, so there is no lstat→open TOCTOU window. A symlink as a *parent* component is rejected (`SYMLINK_NOT_ALLOWED`) including links pointing inside the same workdir; a symlink as the *final* component is reported by `stat_file` as `type: "symlink"` (target never revealed) and rejected by `read_text_file`/`list_directory`. rg runs rooted at a pre-validated directory FD (`/proc/self/fd`) with symlink following never enabled. |
| Special files | FIFOs, sockets and device files appear in `list_directory`/`stat_file` as `type: "other"` but are rejected before any content read (`UNSUPPORTED_FILE_TYPE`) — reads can never block. |
| Hidden files | Dot-prefixed path components are denied everywhere (list/find/search/read/stat/resource), not just hidden from listings. With `SERVERFS_ALLOW_HIDDEN=true` they become visible on *every* channel, still subject to the deny rules. |
| Credential deny rules | `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`, `.ssh/`, `.aws/`, `.gnupg/`, `.kube/` are denied on every channel — even with `SERVERFS_ALLOW_HIDDEN=true`. Append your own patterns via `SERVERFS_EXTRA_DENY_GLOBS` (e.g. `*.sqlite,internal/**`); those apply unconditionally. The built-in set can be released with `SERVERFS_DISABLE_DEFAULT_DENY=true` — see the warning below. |
| Read limits | `SERVERFS_MAX_READ_LINES` (500) and `SERVERFS_MAX_READ_BYTES` (512 KiB); a single line over the byte budget returns `LINE_TOO_LARGE` rather than a truncated line. |
| Search limits | rg subprocess with argument-array invocation (no shell, no string concatenation), streamed `--json` output, wall-clock 15 s deadline (terminate → grace → kill, no orphan processes), 50 MiB per-file ceiling, and a true *global* result limit: rg is terminated as soon as `limit + 1` policy-valid matches exist, instead of scanning the whole tree. Result paths are re-checked against hidden/deny policy. |
| Audit log | Every tool call emits a structured `tool_call` event (tool, workdir, relative path, duration, success, `error_code`, plus per-tool counts). File contents, search queries and host/container paths are never logged. The `startup` event records the effective security mode (`allow_hidden`, `default_deny_enabled`, `extra_deny_rule_count`). |
| Docker | Read-only bind mounts (`create_host_path: false`), read-only container root filesystem, tmpfs `/tmp`, non-root UID 10001, `cap_drop: ALL`, `no-new-privileges`. |
| Network | MCP container is on an `internal: true` network only — no Internet egress, no published ports. Only the tunnel container bridges to the outside. |
| Secrets | `CONTROL_PLANE_*` never enters the MCP container (verified with `docker compose exec serverfs-mcp env`). |

File contents are treated as **untrusted data** — ServerFS only returns them as text and never acts on anything inside them.

Error messages are short, agent-recoverable codes (`PATH_NOT_FOUND`, `SYMLINK_NOT_ALLOWED`, …) and never contain internal container paths or host paths.

> **Warning — `SERVERFS_DISABLE_DEFAULT_DENY=true`**: this releases only the *built-in* credential rules (`.env`, `*.pem`, `id_rsa`, `.ssh/**`, …), letting the agent read credential material inside your workdirs. `SERVERFS_EXTRA_DENY_GLOBS` still applies and is the intended place for compensating rules. Hidden-path filtering (`SERVERFS_ALLOW_HIDDEN`) is a separate, independent switch. Use this option only when a workdir legitimately contains files matching the built-in patterns and you have reviewed the exposure.

## Tools

| Tool | Purpose |
|---|---|
| `list_workdirs` | Discover configured workdirs |
| `list_directory` | Sorted directory listing with offset/limit pagination |
| `find_files` | Recursive filename glob search; `truncated=true` whenever the scan stopped early at the match limit or the walk-entry cap |
| `search_text` | Literal (non-regex) content search via ripgrep, global streamed result limit with early-stop |
| `read_text_file` | UTF-8 reading with line pagination and byte caps |
| `stat_file` | type (`file`/`directory`/`symlink`/`other`) / size / mtime (RFC 3339 UTC) / best-effort MIME |

A `serverfs://{workdir}/{path}` resource template is also exposed; it goes through the exact same validation as `read_text_file`. Resources are all-or-nothing: a file that exceeds the read budget returns `RESOURCE_TOO_LARGE` instead of a silently truncated body — use `read_text_file` for paginated access.

## Operations

```bash
docker compose up -d
docker compose down
docker compose ps
docker compose logs -f
```

## Upgrade

Dependency versions are pinned: `mcp==2.2.0` in `pyproject.toml`/`uv.lock`, the builder image `ghcr.io/astral-sh/uv:0.12.15` in the `Dockerfile`, and the tunnel image `ghcr.io/openai/tunnel-client:v0.0.14` in `.env.example`. Upgrade deliberately by changing those pins, then `docker compose build && docker compose up -d`. Avoid `latest`.

For **production**, pin `SERVERFS_IMAGE` to an exact release instead of `latest`:

```env
SERVERFS_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.1.0
```

Pinned deploys are reproducible, upgrades are explicit (`docker compose pull && docker compose up -d` after editing the version), and rollback is a one-line change back to the previous version. `latest` is convenient for a first look, not for a long-lived deployment.

## Development

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose config
docker compose build
```

## Not in v0.1 (by design)

No write support, shell/command execution, OAuth/SSO, web UI, database, indexing, file watching, PDF/Office parsing, multi-user ACL, or client support beyond the OpenAI tunnel.
