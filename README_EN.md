# ServerFS MCP

**English** · [简体中文](README.md)

[![Release](https://img.shields.io/github/v/release/NTLx/ServerFS_MCP)](https://github.com/NTLx/ServerFS_MCP/releases/latest)
[![Linux CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/container.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/container.yml)
[![Windows CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/windows-native.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/windows-native.yml)
[![macOS CI](https://github.com/NTLx/ServerFS_MCP/actions/workflows/macos-native.yml/badge.svg)](https://github.com/NTLx/ServerFS_MCP/actions/workflows/macos-native.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**A secure, standards-first MCP filesystem server with scoped access and optional Agent delegation.**

Expose only the directories you choose. Keep them read-only by default. Opt into narrow, revision-guarded file mutations, bounded binary transfer, or structured delegation to native Codex / Claude Code / Qoder runtimes and operator-configured ACP Agents only where you need them. Deterministic Repo Ops shipped on Linux Host Bridge in v0.23.0, expanded to validated native Apple Silicon macOS in v0.23.1, and now extends to validated native Windows 11 x64 in v0.23.2 for deterministic repository completion without Agent delegation.

**Current stable release: v0.23.3**

[Website](https://ntlx.github.io/ServerFS_MCP/) ·
[Documentation](https://ntlx.github.io/ServerFS_MCP/docs/) ·
[Getting started](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/) ·
[Security model](https://ntlx.github.io/ServerFS_MCP/docs/security/) ·
[Repo Ops](https://ntlx.github.io/ServerFS_MCP/docs/repo-ops/) ·
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
- **Optional capabilities stay optional** — binary transfer, ChatGPT file ingress, Linux / native macOS / native Windows Repo Ops, Agent delegation, and Jev advice are independently gated.
- **Linux, Windows, and macOS** — one public capability model with platform-native containment and deployment boundaries.

## Supported deployments

| Platform | Supported shape | Filesystem boundary | Repo Ops | Agent delegation |
| --- | --- | --- | --- | --- |
| **Linux** | Docker Compose, `linux/amd64` and `linux/arm64` | Bind-mounted workdirs; container read-only by default | v0.23.0+ | Native Codex, Claude Code, Qoder; configurable ACP v1 profiles |
| **Windows** | Windows 11 x64, local NTFS, native stdio | Native HANDLE-relative Rust kernel over MCP stdio | v0.23.2 validated | Native Codex, Claude Code, Qoder; configurable ACP v1 profiles |
| **macOS** | Apple M-series, native arm64, macOS 27 Golden Gate, local APFS | Native descriptor-relative Darwin backend over MCP stdio | v0.23.1+ validated | Native Codex, Claude Code, Qoder; configurable ACP v1 profiles |

macOS support is limited to Apple M-series, native arm64, macOS 27.x and local APFS; Intel, Rosetta and other macOS major versions are not claimed. Persistent Tunnel and Agent Bridge services use current-user `SMAppService` LaunchAgents. Windows uses a current-user, non-elevated Task Scheduler 2.0 task with a windowless watchdog that restarts the runner, which restarts the Tunnel. Both platforms manage persistent services through `serverfs service`; see the platform guides for setup, recovery boundaries and acceptance limits.

For native setup, use the dedicated guides:

- [Windows Native](https://ntlx.github.io/ServerFS_MCP/docs/windows-native/)
- [macOS Native](https://ntlx.github.io/ServerFS_MCP/docs/macos-native/)

## Agent Skills

The repository carries task-oriented [ServerFS Agent Skills](skills/README.md) for installation, configuration, upgrade, diagnosis, OpenAI Tunnel, HTTP authentication, and Agent Bridge/runtime operations. They are a thin workflow layer over the current CLI, configuration templates, Compose/deployment files, and product documentation rather than a second configuration system.

The repository root also contains a portable skills-only [`plugin.json`](plugin.json). The published v0.23.3 GitHub Release includes `serverfs-plugin-v0.23.3.zip`, containing the seven runtime skills and their packaged references. The Plugin does not bundle a ServerFS MCP URL, credentials, or provider-private configuration.

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

Edit `.env` to pin the image, allow the local client Host values, and configure at least one workdir:

# Current stable release example (v0.23.3):
```env
SERVERFS_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.23.3
SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp:8000,127.0.0.1:8000,localhost:8000
WORKDIR_01_ALIAS=projects
WORKDIR_01_PATH=/srv/projects
WORKDIR_01_DESCRIPTION="Projects"
WORKDIR_01_READ_ONLY=true
```

The host workdir must already exist and permit reads and directory traversal by the container's `SERVERFS_UID:SERVERFS_GID` (default `10001:10001`); a read-only mount does not bypass host permissions. Native Windows/macOS deployments use [`serverfs.toml.example`](serverfs.toml.example); follow the platform guides.

### 2. Start the standard Streamable HTTP endpoint

```bash
docker compose -f compose.yml -f compose.http.yml pull serverfs-mcp
docker compose -f compose.yml -f compose.http.yml up -d serverfs-mcp
docker compose -f compose.yml -f compose.http.yml ps
```

The generic HTTP override publishes ServerFS on `http://127.0.0.1:8000/mcp` by default and does not start the OpenAI Tunnel. Before any non-loopback/public deployment, add the actual external Host to `SERVERFS_HTTP_ALLOWED_HOSTS` and configure HTTPS and authentication (built-in API Key/OAuth or an upstream gateway).

For ChatGPT through the existing OpenAI Secure MCP Tunnel, configure `CONTROL_PLANE_TUNNEL_ID` and `CONTROL_PLANE_API_KEY` and use the base Compose deployment. For Agent delegation, binary transfer, file ingress, proxy routing, or production hardening, continue with the [Getting Started](https://ntlx.github.io/ServerFS_MCP/docs/getting-started/) and [Configuration](https://ntlx.github.io/ServerFS_MCP/docs/configuration/) guides.

### Optional static API Key for remote HTTP

An optional lightweight built-in authentication mode is available for operators who do not need OAuth identity flows. Set one high-entropy secret in `.env`:

```env
SERVERFS_HTTP_API_KEY=<long-random-secret>
```

Clients should preferably send `Authorization: Bearer <key>`. A DingTalk-style one-line URL such as `https://mcp.example.com/mcp?key=<key>` is also supported, but URL credentials can be captured by reverse-proxy or infrastructure logs. Public endpoints still require HTTPS, and API Key mode is mutually exclusive with `SERVERFS_OAUTH_ENABLED=true`. See [Static API Key](https://ntlx.github.io/ServerFS_MCP/docs/api-key/).

### Optional OAuth 2.1 for remote HTTP

OAuth 2.1 is available for remote Streamable HTTP on Linux Docker, is **disabled by default**, and does not affect native Windows/macOS stdio. Enable `SERVERFS_OAUTH_ENABLED=true`, configure the public MCP/issuer URLs, introspection secret, identity provider and exact principal allowlist, and include both `compose.http.yml` and `compose.oauth.yml`. The state directory must already exist and be writable by `SERVERFS_UID:SERVERFS_GID`.

The recommended public shape places separate MCP and authorization-server origins behind an HTTPS reverse proxy; only `serverfs-auth` owns identity-provider/CIMD egress. Supported providers are GitHub, DingTalk, Feishu/Lark and WeChat Open Platform. See [OAuth 2.1](https://ntlx.github.io/ServerFS_MCP/docs/oauth/) for complete commands, callbacks, state-directory permissions and the OpenAI Tunnel OAuth compatibility boundary.

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
              | Core        |------> optional host Bridge (Agent / Repo Ops)
              +-------------+                |
                     ^                       +--> Repo Ops (Linux + native macOS + native Windows)
                     |                       +--> Codex
          OpenAI Secure MCP Tunnel           +--> Claude Code
                     |                       +--> Qoder
                     |                       +--> ACP profiles
                     |
                  ChatGPT
```

The MCP core is host-neutral. Standard clients use stdio or Streamable HTTP; the OpenAI Secure MCP Tunnel remains an optional authenticated ChatGPT ingress adapter. On Linux, generic HTTP publication is opt-in and binds to host loopback by default, while the historical Tunnel deployment remains supported. Streamable HTTP supports optional one-secret API Key authentication or the isolated `serverfs-auth` OAuth 2.1 broker for identity-aware deployments. Neither mode changes the existing workdir authorization model or native stdio. The optional file-ingress sidecar retains separate egress responsibilities. On Windows and macOS, ServerFS runs natively over stdio and keeps the Agent Bridge in a separate host process boundary. Repo Ops debuted on Linux in v0.23.0, expanded to validated native Apple Silicon macOS in v0.23.1, and adds validated native Windows 11 x64 support in v0.23.2.

See [Architecture](https://ntlx.github.io/ServerFS_MCP/docs/architecture/) for the complete deployment and trust-boundary model.

## Capability surface

These are registered MCP tool counts. The default surface includes all 5 mutation tools, but read-only workdirs reject mutations. Binary tools require binary policy on at least one workdir; Agent tools require both the Bridge switch and an enabled workdir Agent policy. Stable v0.23.3 keeps the existing 11 / 13 / 21 / 23 filesystem, binary and Agent combinations and can independently add four Deterministic Repo Ops tools on Linux Host Bridge, validated native Apple Silicon macOS, and validated native Windows 11 x64.

| Surface | Tools | What it adds |
| --- | ---: | --- |
| Filesystem | 11 | 6 read tools + 5 controlled mutation tools |
| Filesystem + binary | 13 | Bounded whole-file download/upload |
| Filesystem + Repo Ops | 15 | 4 deterministic Git / named-action tools; v0.23.3 Linux Host Bridge / native macOS / native Windows |
| Filesystem + binary + Repo Ops | 17 | Binary + Repo Ops |
| Filesystem + Agent | 21 | 10 structured Agent Bridge tools |
| Filesystem + binary + Agent | 23 | Binary + Agent |
| Filesystem + Agent + Repo Ops | 25 | Agent and Repo Ops independently enabled |
| Full v0.23.3 surface | 27 | Filesystem + binary + Agent + Repo Ops |

### Filesystem tools

**Read everywhere:** `list_workdirs`, `list_directory`, `find_files`, `search_text`, `read_text_file`, `stat_file`

**Read-write workdirs only:** `create_text_file`, `edit_text_file`, `delete_file`, `create_directory`, `delete_directory`

**Optional binary transfer:** `download_binary_file`, `upload_binary_file`

**Optional v0.23.3 Linux Host Bridge, validated native macOS, and validated native Windows Repo Ops:** `inspect_repo`, `run_repo_action`, `commit_repo_changes`, `push_repo`. Repo Ops uses fixed Git semantics and operator-defined, SHA-256 integrity-pinned named actions. The caller cannot supply executable, argv, environment, remote, ref, force, or shell fragments, and Repo Ops never creates an Agent task. The v0.23.0 Linux acceptance remains the baseline; v0.23.1 adds real native Apple Silicon macOS Repo Ops E2E; v0.23.2 adds Windows 11 x64 process-tree containment, reparse/junction fail-closed behavior, and public MCP live acceptance.

Agent tools submit and observe structured provider tasks; they are **not** a shell or arbitrary command-execution API. Repo Ops and Agent delegation are authorized independently: a workdir may enable Repo Ops while Agent mode remains disabled. See [Deterministic Repo Ops](https://ntlx.github.io/ServerFS_MCP/docs/repo-ops/) and [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/).

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
- no recursive delete, force flag, generic `write_file`, shell, or arbitrary command executor; Repo Ops also exposes no caller-controlled Git force / remote / ref / argv surface;
- v0.23 Repo Ops named actions use operator-fixed argv, mandatory SHA-256 integrity pins, a narrow child environment, bounded execution, and the same writer lease as filesystem/Agent writers;
- isolated file ingress instead of giving the MCP service general download egress;
- allowlisted Agent runtimes, writer leases, recovery guards, explicit provider approvals, and advisory-only Jev integration.

For the actual trust boundaries and security contract, read the [Security Model](https://ntlx.github.io/ServerFS_MCP/docs/security/) and [Architecture](https://ntlx.github.io/ServerFS_MCP/docs/architecture/) pages.

## Optional Agent Bridge

ServerFS can delegate bounded tasks through a separate host-side Agent Bridge to native **Codex**, **Claude Code**, and **Qoder** runtimes, plus generic ACP v1 profiles with runtime IDs such as `acp-<profile>`. Operators configure each ACP profile with an absolute executable and fixed argv; these are not new native provider integrations.

The Bridge adds structured task submission, status/events, cancellation, approvals/questions, model discovery where the provider exposes it, request-scoped model selection, result spooling, idempotent retries, and recovery semantics. Provider capabilities are reported per runtime rather than assumed to be uniform. ACP runtimes do not support model discovery or override, elicitation, ACP client filesystem, or terminal capabilities; remaining capabilities are reported conservatively from measured behavior.

Every enabled Agent runtime requires a writable workdir and `workspace-write` policy. Tasks cannot override ACP command, argv or environment; provider authentication stays provider-owned, and ServerFS does not auto-install or trust registry Agents. OpenCode 2.1.2 with `opencode/glm-5.3-flash-free` passed Linux, macOS and Windows end-to-end acceptance; this does not establish compatibility for every ACP implementation.

v0.22.0 Correlated Agent Diagnostics query bounded, Bridge-process-local state through `serverfs diagnostics snapshot` and `serverfs diagnostics task <task-id>`, with `--format human|json`. They reuse existing private RPC and add no MCP tool, network listener, persistent trace store, OpenTelemetry dependency or telemetry egress. On Linux, set `SERVERFS_AGENT_BRIDGE_SOCKET` when using a custom socket path. See the [v0.22 plan](dev_plan_v0.22.md) for implementation and three-platform acceptance links.

Jev integration is optional and advisory only: it may provide task preflight, runtime/model advice, and approval context, but it never expands permissions or automatically overrides the caller's selected runtime/model/approval decision.

- [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/)
- [ACP Guide](https://ntlx.github.io/ServerFS_MCP/docs/acp/)
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
| Deterministic repository operations (v0.23.3 Linux / native macOS / native Windows) | [Deterministic Repo Ops](https://ntlx.github.io/ServerFS_MCP/docs/repo-ops/) |
| Agent delegation | [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/) |
| ACP profile configuration and operation | [ACP Guide](https://ntlx.github.io/ServerFS_MCP/docs/acp/) |
| Optional Jev advice | [Jev Advisors](https://ntlx.github.io/ServerFS_MCP/docs/jev-advisors/) |
| OpenAI Tunnel troubleshooting | [Tunnel Diagnostics](https://ntlx.github.io/ServerFS_MCP/docs/tunnel-diagnostics/) |
| Read-only host status publication | [Host Observability](https://ntlx.github.io/ServerFS_MCP/docs/host-observability/) |
| Restricted-network Linux ARM64 | [Restricted-network ARM64](https://ntlx.github.io/ServerFS_MCP/docs/restricted-network-arm64/) |
| Windows native | [Windows Native](https://ntlx.github.io/ServerFS_MCP/docs/windows-native/) |
| macOS native | [macOS Native](https://ntlx.github.io/ServerFS_MCP/docs/macos-native/) |

Release history and upgrade-specific notes belong in [GitHub Releases](https://github.com/NTLx/ServerFS_MCP/releases), not in this README.

## Releases

- Linux images: `ghcr.io/ntlx/serverfs_mcp`; current stable release: `v0.23.3`
- Current stable release tag: `v0.23.3`
- Production deployments should pin an exact release rather than `latest` or `edge`.
- Portable Agent Plugin: skills-only `serverfs-plugin-v0.23.3.zip` asset in the v0.23.3 GitHub Release.
- Windows release assets include the product, Agent Bridge, and native kernel wheels.
- macOS uses the product and Agent Bridge Python distributions directly; there is no separate macOS native wheel. Installation locally builds the small bundled ServiceManagement companion used for persistent per-user services.

See the [latest release](https://github.com/NTLx/ServerFS_MCP/releases/latest) for artifacts, checksums, and version-specific notes.

## Development

For the main Python/Linux path:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose config --quiet
docker compose -f compose.yml -f compose.http.yml config --quiet
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
```

Platform-native development has additional gates in the Windows and macOS workflows. Maintainer invariants, validation rules, and release discipline live in [AGENTS.md](AGENTS.md) and [docs/maintainers/](docs/maintainers/).

## License

ServerFS MCP is released under the [MIT License](LICENSE).
