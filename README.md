# ServerFS MCP

[![Release](https://img.shields.io/github/v/release/NTLx/ServerFS_MCP)](https://github.com/NTLx/ServerFS_MCP/releases/latest)
[![Linux CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/container.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/container.yml)
[![Windows CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/windows-native.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/windows-native.yml)
[![macOS CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/macos-native.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/macos-native.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A secure, standards-first MCP filesystem server with scoped access and optional native Agent delegation.**

Expose only the directories you choose. Keep them read-only by default. Opt into narrow, revision-guarded file mutations, bounded binary transfer, or structured Codex / Claude Code / Qoder delegation only where you need them.

**Current stable release: v0.18.0**

[Website](https://ntlx.github.io/ServerFS_MCP/) ·
[Documentation](https://ntlx.github.io/ServerFS_MCP/docs/) ·
[Getting started](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/) ·
[Security model](https://ntlx.github.io/ServerFS_MCP/docs/security/) ·
[Static API Key](https://ntlx.github.io/ServerFS_MCP/docs/api-key/) ·
[OAuth 2.1](https://ntlx.github.io/ServerFS_MCP/docs/oauth/) ·
[Releases](https://github.com/NTLx/ServerFS_MCP/releases)

---

## Why ServerFS

Giving an AI agent broad filesystem or shell access is easy. Giving it **only the capabilities required for the task** is harder.

ServerFS is built around that narrower boundary:

- **Explicit workdirs** — only administrator-configured directories are visible, addressed by aliases rather than host paths.
- **Read-only by default** — write access is an explicit per-workdir decision, not a global default.
- **Narrow file operations** — no shell, generic command executor, recursive delete, or unguarded overwrite.
- **Concurrency-aware mutations** — destructive operations use opaque revisions; text edits are exact-match and atomic.
- **Defense in depth** — path confinement, symlink rejection, credential-path deny rules, bounded I/O, audit events, and platform-specific filesystem safety.
- **Optional capabilities stay optional** — binary transfer, ChatGPT file ingress, Agent delegation, and Jev advice are independently gated.
- **Linux, Windows, and macOS** — one public capability model with platform-native containment and deployment boundaries.

## Supported deployments

| Platform | Supported shape | Filesystem boundary | Agent delegation |
| --- | --- | --- | --- |
| **Linux** | Docker Compose, `linux/amd64` and `linux/arm64` | Bind-mounted workdirs; container read-only by default | Optional host Agent Bridge |
| **Windows** | Windows 11 x64, local NTFS, native stdio; current-user Task Scheduler persistence since v0.14.0 | Native HANDLE-relative Rust kernel over MCP stdio | Codex, Claude Code, Qoder |
| **macOS** | Apple M-series, native arm64, macOS 27 Golden Gate | Native descriptor-relative Darwin backend over MCP stdio | Codex, Claude Code, Qoder |

The macOS v0.13 support boundary is intentionally strict: Intel Macs, Rosetta/x86_64 execution, macOS 26 or older, and macOS 28+ are not claimed as supported. **v0.13.2** adds Apple-native `SMAppService` persistence for the Tunnel and Agent Bridge, so the managed stack no longer depends on terminal/session lifetime. `SMAppService` owns user-login persistence; live reboot/re-login recovery validation is deferred beyond v0.13.2.

**v0.14.0** brings persistent outer-lifecycle reliability to Windows without introducing administrator requirements. Windows Native uses a current-user, least-privilege Task Scheduler 2.0 task that owns a windowless watchdog; the watchdog restarts the managed runner, and the runner restarts Tunnel generations. A logon trigger plus an indefinite one-minute time-trigger heartbeat re-establishes the outer owner while `IgnoreNew` preserves one healthy instance. `serverfs service install/start/stop/restart/status/uninstall` is the supported operator surface; installation, runtime, recovery and maintenance remain non-elevated and current-user scoped. Release acceptance verified Tunnel-, runner- and Task-Scheduler-level recovery, intentional stop semantics, explicit start recovery, Agent Bridge recovery, windowless operation and exactly-one Tunnel ownership; a physical reboot/re-login cycle was not part of that acceptance.

For native setup, use the dedicated guides:

- [Windows Native](https://ntlx.github.io/ServerFS_MCP/docs/windows-native/)
- [macOS Native](https://ntlx.github.io/ServerFS_MCP/docs/macos-native/)

## Agent Skills (v0.18.0)

The repository now carries task-oriented [ServerFS Agent Skills](skills/README.md) for installation, configuration, upgrade, diagnosis, OpenAI Tunnel, HTTP authentication, and Agent Bridge/runtime operations. They are a thin workflow layer over the current CLI, configuration templates, Compose/deployment files, and product documentation rather than a second configuration system.

The repository root also contains a portable skills-only [`plugin.json`](plugin.json). The v0.18.0 stable tag builds one `serverfs-plugin-v0.18.0.zip` GitHub Release asset containing the seven runtime skills and their packaged references. The Plugin does not bundle a ServerFS MCP URL, credentials, or provider-private configuration.

## Quick start

The shortest path is the Linux Docker deployment.

### Prerequisites

- Docker + Docker Compose
- At least one directory you want to expose

An OpenAI Secure MCP Tunnel is optional and only required for the ChatGPT Tunnel ingress path.

### 1. Clone and configure

```bash
git clone https://github.com/NTLx/ServerFS_MCP.git
cd ServerFS_MCP
cp .env.example .env
chmod 600 .env
```

Edit `.env` and configure at least one workdir:

```env
WORKDIR_01_ALIAS=projects
WORKDIR_01_PATH=/srv/projects
WORKDIR_01_DESCRIPTION="Projects"
WORKDIR_01_READ_ONLY=true
```

### 2. Start the standard Streamable HTTP endpoint

```bash
docker compose pull
docker compose -f compose.yml -f compose.http.yml up -d serverfs-mcp
docker compose -f compose.yml -f compose.http.yml ps
```

The generic HTTP override publishes ServerFS on `127.0.0.1:8000/mcp` by default and does not start the OpenAI Tunnel. Configure `SERVERFS_HTTP_ALLOWED_HOSTS` and place an authenticated TLS ingress in front of ServerFS before any non-loopback/public deployment.

For ChatGPT through the existing OpenAI Secure MCP Tunnel, configure `CONTROL_PLANE_TUNNEL_ID` and `CONTROL_PLANE_API_KEY` and use the base Compose deployment. For Agent delegation, binary transfer, file ingress, proxy routing, or production hardening, continue with the [Getting Started](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/) and [Configuration](https://ntlx.github.io/ServerFS_MCP/docs/configuration/) guides.

### Optional static API Key for remote HTTP

v0.17.0 adds a lightweight built-in authentication mode for operators who do not need OAuth identity flows. Set one high-entropy secret in `.env`:

```env
SERVERFS_HTTP_API_KEY=<long-random-secret>
```

Clients should preferably send `Authorization: Bearer <key>`. A DingTalk-style one-line URL such as `https://mcp.example.com/mcp?key=<key>` is also supported, but URL credentials can be captured by reverse-proxy or infrastructure logs. Public endpoints still require HTTPS, and API Key mode is mutually exclusive with `SERVERFS_OAUTH_ENABLED=true`. See [Static API Key](https://ntlx.github.io/ServerFS_MCP/docs/api-key/).

### Optional OAuth 2.1 for remote HTTP

v0.16.0 adds an opt-in OAuth 2.1 deployment for generic remote Streamable HTTP. OAuth stays **disabled by default**, applies only when `SERVERFS_OAUTH_ENABLED=true` and `compose.oauth.yml` is included, and does not change native Windows/macOS stdio.

```bash
mkdir -p .serverfs-auth-state
chmod 700 .serverfs-auth-state

docker compose --env-file .env \
  -f compose.yml \
  -f compose.http.yml \
  -f compose.oauth.yml \
  up -d
```

The recommended public shape uses separate HTTPS MCP and authorization-server origins behind a reverse proxy; `serverfs-auth` alone owns provider/CIMD egress. Supported identity providers are GitHub, DingTalk, Feishu/Lark and WeChat Open Platform. See [OAuth 2.1](https://ntlx.github.io/ServerFS_MCP/docs/oauth/) for callbacks, stable-principal admission, CIMD/DCR, resource binding, backup/revocation, proxy configuration and the current OpenAI Tunnel OAuth limitation.

## How it works

```text
Claude Code / Codex / Qoder / MCP hosts
             |               |
           stdio      Streamable HTTP
             |               |
             +-------+-------+
                     v
              +-------------+
              | ServerFS MCP |------> configured workdirs
              | Core        |------> optional host Agent Bridge
              +-------------+                |
                     ^                       +--> Codex
                     |                       +--> Claude Code
          OpenAI Secure MCP Tunnel           +--> Qoder
                     |
                  ChatGPT
```

The MCP core is host-neutral. Standard clients use stdio or Streamable HTTP; the OpenAI Secure MCP Tunnel remains an optional authenticated ChatGPT ingress adapter. On Linux, generic HTTP is opt-in and loopback-only by default, while the historical Tunnel deployment remains supported. v0.17 adds an optional one-secret API Key boundary directly around Streamable HTTP; v0.16 adds the isolated `serverfs-auth` OAuth 2.1 broker for identity-aware deployments. Neither mode changes the existing workdir authorization model or native stdio. The optional file-ingress sidecar retains separate egress responsibilities. On Windows and macOS, ServerFS runs natively over stdio and keeps the Agent Bridge in a separate host process boundary. Since v0.14.0, Windows uses the current user's Task Scheduler as the persistent OS owner and keeps the managed process tree windowless; no SCM/LocalSystem service or UAC elevation is required. On macOS v0.13.2+, a small headless ServiceManagement companion registers the persistent per-user Tunnel and Agent Bridge through `SMAppService`; no root LaunchDaemon or hand-installed LaunchAgent is required.

See [Architecture](https://ntlx.github.io/ServerFS_MCP/docs/architecture/) for the complete deployment and trust-boundary model.

## Capability surface

ServerFS exposes capabilities in layers rather than enabling everything at once.

| Surface | Tools | What it adds |
| --- | ---: | --- |
| Filesystem | 11 | 6 read tools + 5 controlled mutation tools |
| Filesystem + binary | 13 | Bounded whole-file download/upload |
| Filesystem + Agent | 21 | 10 structured Agent Bridge tools |
| Full capability | 23 | Filesystem + binary + Agent |

### Filesystem tools

**Read everywhere:** `list_workdirs`, `list_directory`, `find_files`, `search_text`, `read_text_file`, `stat_file`

**Read-write workdirs only:** `create_text_file`, `edit_text_file`, `delete_file`, `create_directory`, `delete_directory`

**Optional binary transfer:** `download_binary_file`, `upload_binary_file`

Agent tools submit and observe structured provider tasks; they are **not** a shell or arbitrary command-execution API. See [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/).

## Security model

ServerFS assumes file contents are untrusted data and uses independent enforcement layers rather than a single permission check.

Key properties include:

- workdir confinement with host paths hidden from MCP results;
- read-only-by-default policy, reinforced by the deployment/filesystem layer;
- descriptor- or handle-relative traversal with symlink/reparse-point defenses;
- revision-guarded destructive operations and atomic publication;
- metadata preservation for replacement writes;
- reserved internal names and built-in credential-path deny rules;
- bounded reads, writes, searches, binary transfers, task results, and interaction lifetimes;
- no recursive delete, force flag, generic `write_file`, shell, or arbitrary command executor;
- isolated file ingress instead of giving the MCP service general download egress;
- allowlisted Agent runtimes, writer leases, recovery guards, explicit provider approvals, and advisory-only Jev integration.

For the actual trust boundaries and security contract, read the [Security Model](https://ntlx.github.io/ServerFS_MCP/docs/security/) and [Architecture](https://ntlx.github.io/ServerFS_MCP/docs/architecture/) pages.

## Optional Agent Bridge

ServerFS can delegate bounded tasks to native **Codex**, **Claude Code**, and **Qoder** runtimes through a separate host-side Agent Bridge.

The Bridge adds structured task submission, status/events, cancellation, approvals/questions, model discovery where the provider exposes it, request-scoped model selection, result spooling, idempotent retries, and recovery semantics. Provider capabilities are reported per runtime rather than assumed to be uniform.

Jev integration is optional and advisory only: it may provide task preflight, runtime/model advice, and approval context, but it never expands permissions or automatically overrides the caller's selected runtime/model/approval decision.

- [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/)
- [Jev Advisors](https://ntlx.github.io/ServerFS_MCP/docs/jev-advisors/)

## Binary transfer and ChatGPT file ingress

Binary transfer is a separate opt-in capability with explicit byte limits. Uploads to existing files remain revision-guarded and still require a read-write workdir.

ChatGPT file parameters use an optional isolated ingress helper rather than giving the main MCP service unrestricted Internet access.

See [Binary Transfer](https://ntlx.github.io/ServerFS_MCP/docs/binary-transfer/) for the supported sources, limits, and network boundary.

## Documentation

The documentation site is the authoritative user and operator reference. The README intentionally stays at project-entry level.

| Topic | Documentation |
| --- | --- |
| Overview | [ServerFS MCP docs](https://ntlx.github.io/ServerFS_MCP/docs/) |
| First deployment | [Getting Started](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/) |
| Workdirs and policy | [Configuration](https://ntlx.github.io/ServerFS_MCP/docs/configuration/) |
| Architecture and trust boundaries | [Architecture](https://ntlx.github.io/ServerFS_MCP/docs/architecture/) |
| Security contract | [Security Model](https://ntlx.github.io/ServerFS_MCP/docs/security/) |
| Static API Key remote HTTP | [Static API Key](https://ntlx.github.io/ServerFS_MCP/docs/api-key/) |
| OAuth 2.1 remote HTTP | [OAuth 2.1](https://ntlx.github.io/ServerFS_MCP/docs/oauth/) |
| Binary transfer / file ingress | [Binary Transfer](https://ntlx.github.io/ServerFS_MCP/docs/binary-transfer/) |
| Agent delegation | [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/) |
| Optional Jev advice | [Jev Advisors](https://ntlx.github.io/ServerFS_MCP/docs/jev-advisors/) |
| OpenAI Tunnel troubleshooting | [Tunnel Diagnostics](https://ntlx.github.io/ServerFS_MCP/docs/tunnel-diagnostics/) |
| Read-only host status publication | [Host Observability](https://ntlx.github.io/ServerFS_MCP/docs/host-observability/) |
| Restricted-network Linux ARM64 | [Restricted-network ARM64](https://ntlx.github.io/ServerFS_MCP/docs/restricted-network-arm64/) |
| Windows native | [Windows Native](https://ntlx.github.io/ServerFS_MCP/docs/windows-native/) |
| macOS native | [macOS Native](https://ntlx.github.io/ServerFS_MCP/docs/macos-native/) |

Release history and upgrade-specific notes belong in [GitHub Releases](https://github.com/NTLx/ServerFS_MCP/releases), not in this README.

## Releases

The current stable release is **v0.18.0**.

- Linux images: `ghcr.io/ntlx/serverfs_mcp`
- Stable release tag: `v0.18.0`
- Production deployments should pin an exact release rather than `latest` or `edge`.
- Portable Agent Plugin: one skills-only `serverfs-plugin-v0.18.0.zip` asset.
- Windows release assets include the product, Agent Bridge, and native kernel wheels.
- macOS uses the product and Agent Bridge Python distributions directly; v0.13 has no separate macOS native wheel. v0.13.2 locally builds the small bundled ServiceManagement companion used for persistent per-user services.

See the [latest release](https://github.com/NTLx/ServerFS_MCP/releases/latest) for artifacts, checksums, and version-specific notes.

## Development

For the main Python/Linux path:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose config
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
```

Platform-native development has additional gates in the Windows and macOS workflows. Maintainer invariants, validation rules, and release discipline live in [AGENTS.md](AGENTS.md) and [docs/maintainers/](docs/maintainers/).

## License

ServerFS MCP is released under the [MIT License](LICENSE).
