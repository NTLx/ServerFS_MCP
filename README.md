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
docker compose up -d
docker compose ps       # serverfs-mcp should become (healthy)
docker compose logs -f openai-tunnel
```

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
| Path resolution | Every path is normalized and confined to the workdir root. `..`, absolute paths, NUL bytes rejected. Symlinks anywhere on the path are rejected (`SYMLINK_NOT_ALLOWED`), including links pointing inside the same workdir. |
| Special files | FIFOs, sockets, device files rejected before open (`UNSUPPORTED_FILE_TYPE`) — reads can never block. |
| Hidden files | Dot-prefixed path components are denied everywhere (list/find/search/read/stat), not just hidden from listings. |
| Credential deny rules | `.env`, `.env.*`, `*.env`, `*.pem`, `*.key`, `id_rsa`, `id_ed25519`, `.ssh/`, `.aws/`, `.gnupg/`, `.kube/` are denied on every channel — even with `SERVERFS_ALLOW_HIDDEN=true`. Append your own patterns via `SERVERFS_EXTRA_DENY_GLOBS` (e.g. `*.sqlite,internal/**`); those apply unconditionally. The built-in set can be released with `SERVERFS_DISABLE_DEFAULT_DENY=true` — see the warning below. |
| Read limits | `SERVERFS_MAX_READ_LINES` (500) and `SERVERFS_MAX_READ_BYTES` (512 KiB); a single line over the byte budget returns `LINE_TOO_LARGE` rather than a truncated line. |
| Search limits | rg subprocess with argument-array invocation (no shell, no string concatenation), 15 s timeout, 50 MiB per-file ceiling, result caps, walk-entry caps with early stop. |
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
| `find_files` | Recursive filename glob search (early-stop, walk caps) |
| `search_text` | Literal (non-regex) content search via ripgrep |
| `read_text_file` | UTF-8 reading with line pagination and byte caps |
| `stat_file` | type / size / mtime (RFC 3339 UTC) / best-effort MIME |

A `serverfs://{workdir}/{path}` resource template is also exposed; it goes through the exact same validation as `read_text_file`.

## Operations

```bash
docker compose up -d
docker compose down
docker compose ps
docker compose logs -f
```

## Upgrade

Dependency versions are pinned: `mcp==2.2.0` in `pyproject.toml`/`uv.lock`, and the tunnel image `ghcr.io/openai/tunnel-client:v0.0.14` in `.env.example`. Upgrade deliberately by changing those pins, then `docker compose build && docker compose up -d`. Avoid `latest`.

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
