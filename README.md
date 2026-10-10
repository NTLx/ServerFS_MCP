# ServerFS MCP

[![Release](https://img.shields.io/github/v/release/NTLx/ServerFS_MCP)](https://github.com/NTLx/ServerFS_MCP/releases/latest)
[![Linux CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/container.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/container.yml)
[![Windows CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/windows-native.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/windows-native.yml)
[![macOS CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/macos-native.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/macos-native.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Secure, scoped filesystem access and optional native Agent delegation for ChatGPT and other MCP clients.**

Expose only the directories you choose. Keep them read-only by default. Opt into narrow, revision-guarded file mutations, bounded binary transfer, or structured Codex / Claude Code / Qoder delegation only where you need them.

**Current stable release: v0.13.0**

[Website](https://ntlx.github.io/ServerFS_MCP/) ·
[Documentation](https://ntlx.github.io/ServerFS_MCP/docs/) ·
[Getting started](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/) ·
[Security model](https://ntlx.github.io/ServerFS_MCP/docs/security/) ·
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
| **Windows** | Windows 11 x64, local NTFS | Native HANDLE-relative Rust kernel over MCP stdio | Codex, Claude Code, Qoder |
| **macOS** | Apple M-series, native arm64, macOS 27 Golden Gate | Native descriptor-relative Darwin backend over MCP stdio | Codex, Claude Code, Qoder |

The macOS v0.13 support boundary is intentionally strict: Intel Macs, Rosetta/x86_64 execution, macOS 26 or older, and macOS 28+ are not claimed as supported.

For native setup, use the dedicated guides:

- [Windows Native](https://ntlx.github.io/ServerFS_MCP/docs/windows-native/)
- [macOS Native](https://ntlx.github.io/ServerFS_MCP/docs/macos-native/)

## Quick start

The shortest path is the Linux Docker deployment.

### Prerequisites

- Docker + Docker Compose
- An OpenAI Secure MCP Tunnel
- A tunnel Runtime API Key
- At least one directory you want to expose

### 1. Clone and configure

```bash
git clone https://github.com/NTLx/ServerFS_MCP.git
cd ServerFS_MCP
cp .env.example .env
chmod 600 .env
```

Edit `.env` and configure the tunnel plus at least one workdir:

```env
CONTROL_PLANE_TUNNEL_ID=tunnel_...
CONTROL_PLANE_API_KEY=rtk_...

WORKDIR_01_ALIAS=projects
WORKDIR_01_PATH=/srv/projects
WORKDIR_01_DESCRIPTION="Projects"
WORKDIR_01_READ_ONLY=true
```

### 2. Start ServerFS

```bash
docker compose pull
docker compose up -d
docker compose ps
```

The base deployment exposes the filesystem surface and keeps workdirs read-only unless a slot explicitly sets `WORKDIR_XX_READ_ONLY=false`.

For Agent delegation, binary transfer, file ingress, proxy routing, or production hardening, continue with the [Getting Started](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/) and [Configuration](https://ntlx.github.io/ServerFS_MCP/docs/configuration/) guides instead of expanding the base Compose command blindly.

## How it works

```text
ChatGPT / MCP client
        |
        | OpenAI Secure MCP Tunnel
        v
+---------------------------+
|        ServerFS MCP       |
|                           |
|  scoped filesystem tools  |------> configured workdirs
|  optional binary tools    |
|  optional Agent tools     |------> host Agent Bridge
+---------------------------+            |
                                         +--> Codex
                                         +--> Claude Code
                                         +--> Qoder
```

On Linux, the MCP service stays inside internal Docker networks with no published port or general Internet egress; the tunnel and optional file-ingress sidecar have separate network responsibilities. On Windows and macOS, ServerFS runs natively over stdio and keeps the Agent Bridge in a separate host process boundary.

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
| Binary transfer / file ingress | [Binary Transfer](https://ntlx.github.io/ServerFS_MCP/docs/binary-transfer/) |
| Agent delegation | [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/) |
| Optional Jev advice | [Jev Advisors](https://ntlx.github.io/ServerFS_MCP/docs/jev-advisors/) |
| Windows native | [Windows Native](https://ntlx.github.io/ServerFS_MCP/docs/windows-native/) |
| macOS native | [macOS Native](https://ntlx.github.io/ServerFS_MCP/docs/macos-native/) |

Release history and upgrade-specific notes belong in [GitHub Releases](https://github.com/NTLx/ServerFS_MCP/releases), not in this README.

## Releases

The current stable release is **v0.13.0**.

- Linux images: `ghcr.io/ntlx/serverfs_mcp`
- Stable release tag: `v0.13.0`
- Production deployments should pin an exact release rather than `latest` or `edge`.
- Windows release assets include the product, Agent Bridge, and native kernel wheels.
- macOS uses the product and Agent Bridge Python distributions directly; v0.13 has no separate macOS native wheel.

See the [latest release](https://github.com/NTLx/ServerFS_MCP/releases/latest) for artifacts, checksums, and version-specific notes.

## Development

For the main Python/Linux path:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose config
```

Platform-native development has additional gates in the Windows and macOS workflows. Maintainer invariants, validation rules, and release discipline live in [AGENTS.md](AGENTS.md) and [docs/maintainers/](docs/maintainers/).

## License

ServerFS MCP is released under the [MIT License](LICENSE).
