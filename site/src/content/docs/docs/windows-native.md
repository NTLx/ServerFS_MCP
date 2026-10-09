---
title: Windows Native
description: The native Windows deployment — MCP stdio over a prebuilt Rust/NTFS kernel wheel, no Docker or MSVC; Agent delegation for Codex, Claude and Qoder since v0.11.
---

Since v0.10.0 ServerFS runs natively on **Windows 11 x64 over local NTFS** as an MCP **stdio** service. Nothing listens on a port: the official OpenAI `tunnel-client` connects out to the control plane and speaks MCP through the child process's stdin/stdout, so the default native profile has no localhost listener. Linux Docker deployments are unchanged and remain the supported Linux shape.

v0.11 (in development) adds **Agent delegation** to this deployment for Codex, Claude Code and Qoder through the same ten Agent tools over a Named-Pipe Bridge, accepted through the live ChatGPT tunnel E2E.

The runtime chain:

```text
serverfs tunnel
  -> official pinned tunnel-client        (owns control-plane connectivity)
       -> sanitizer supervisor            (strips tunnel/proxy environment)
            -> serverfs serve             (MCP stdio child, sees no secrets)
                 -> serverfs-windows-native wheel (HANDLE-relative NTFS kernel)
       -> Agent Bridge                    (separate venv, per-user lifecycle lease)
            -> real provider CLI          (codex / claude / qodercli)
```

Windows users consume prebuilt wheels from the GitHub Release and never need Rust, Cargo, MSVC Build Tools or the Windows SDK. The kernel wheel is `cp312-abi3-win_amd64` (stable ABI, Python >= 3.12).

## Install

The product and the Agent Bridge are **independent distributions in two isolated virtual environments**, connected by a frozen process boundary: the supervisor launches the Bridge through `SERVERFS_BRIDGE_PYTHON`. They are never installed into one shared dependency graph (measured: `serverfs-mcp` freezes `mcp==2.2.0` while the pinned Qoder SDK declares `mcp<2.0.0`).

1. Install [uv](https://docs.astral.sh/uv/) (or any Python >= 3.12).
2. Install the **ServerFS environment** (product + native wheels) and the **Agent Bridge environment** (bridge wheel) into two separate venvs:

```powershell
uv venv --python 3.12 .venv-serverfs
uv pip install --python .venv-serverfs\Scripts\python.exe serverfs_mcp-<ver>-py3-none-any.whl serverfs_windows_native-<ver>-cp312-abi3-win_amd64.whl

uv venv --python 3.12 .venv-bridge
uv pip install --python .venv-bridge\Scripts\python.exe serverfs_agent_bridge-<ver>-py3-none-any.whl
```

3. Point the supervisor at the Bridge venv by setting `SERVERFS_BRIDGE_PYTHON=C:\path\to\.venv-bridge\Scripts\python.exe` in the `.env` next to `serverfs.toml` (the tunnel launcher injects it into the serverfs process environment). The Bridge environment never needs the product package, and the product environment never needs the Bridge package.
4. Copy `serverfs.toml.example` to `serverfs.toml` and configure workdirs. Paths are operator configuration — agents only ever see aliases. `read_only = true` is the default and the kernel refuses mutations unless a workdir opts in. No secrets belong in `serverfs.toml`. Agent delegation is **disabled unless you add an `[agent]` section with `enabled = true`** plus per-workdir `agent_mode`/`agent_runtimes` policy; a config without `[agent]` behaves exactly like v0.10.
5. Run the health report until it exits 0:

```powershell
.venv-serverfs\Scripts\serverfs.exe doctor --config serverfs.toml
```

`serverfs doctor` probes the native backend import/version, each workdir root open, the filesystem class, reparse topology, a real policy-filtered listing, a non-mutating write-capability check, the tunnel-client version and the project-managed HTTP proxy reachability — proxy and tunnel credentials are never displayed. Windows GA is local NTFS only: network shares, FAT/exFAT, ReFS and any storage whose class cannot be measured are reported as `FAIL` (fail closed, never a silent blessing).

With `[agent]` enabled, doctor additionally reports — read-only, never starting a Bridge or a provider — the per-workdir Agent policy, each runtime's enabled state and proxy routing, the Agent data home, the private-state safety of whatever exists, the Agent proxy endpoint wiring and reachability (the endpoint itself is never displayed), the user identity derivation, and that the Bridge package is importable by the configured `SERVERFS_BRIDGE_PYTHON` interpreter.

## Agent runtimes on Windows

Each runtime is a real provider process; capabilities are recorded per runtime, not advertised uniformly:

| Runtime | Model discovery | Live steering | Proxy routing |
|---|---|---|---|
| Codex | catalog + request-scoped override | yes (its own loopback control channel is bypassed from proxy policy) | `use_proxy = true` on hosts without a direct provider route |
| Claude Code | unsupported (`model_discovery: unsupported`) | no (`live_steer = false`) | `use_proxy = true` routes provider egress through the injected proxy (measured: external CONNECTs attributed to the claude child, 0 loopback); `false` for direct hosts |
| Qoder | catalog (pricing state read live, never hard-coded) | no (`live_steer = false`) | same injection model as Claude |

The Agent egress proxy is a **credentialless HTTP proxy** supplied by the operator through `SERVERFS_AGENT_PROXY_URL`; an authenticated upstream proxy is not part of the native support surface. The Claude/Qoder provider children consume the injected `HTTPS_PROXY`/`NO_PROXY` overlay; Codex's SDK-to-CLI control traffic stays on stdio and its loopback endpoints are exempt from proxy policy.

## Connectivity

```powershell
.venv-serverfs\Scripts\serverfs.exe bootstrap tunnel-client   # pinned official release, SHA-256 double-anchored
.venv-serverfs\Scripts\serverfs.exe tunnel --config serverfs.toml `
  --tunnel-id tunnel_... --api-key-file C:\Users\you\.config\serverfs\api-key
```

The bootstrap stores the client under the user-owned ServerFS data directory and never modifies the machine PATH; its verification chain pins the official `SHA256SUMS.txt` digest and re-hashes the downloaded archive against it. `serverfs tunnel` auto-discovers the bootstrapped client, binds tunnel-client's health server to an ephemeral loopback port, passes the control-plane key only as a `file:` reference (the key file must live outside every workdir), and strips all `CONTROL_PLANE_*`, `TUNNEL_CLIENT_*`, `OPENAI_*`, `MCP_*`, `SERVERFS_PROXY_*` and proxy variables before spawning the ServerFS child. Proxy configuration uses the same four `SERVERFS_PROXY_*` fields in `.env` as Linux (HTTP only; SOCKS is not supported and not claimed).

Multiple active tunnel-client instances sharing one tunnel ID are unsupported upstream — do not run the native profile twice.

## What stays true on Windows

Every channel — read, list, stat, find, search and all five mutation tools — behaves as on Linux: handle-relative traversal with reparse points never followed, revision-guarded atomic publication, metadata preservation with fail-closed semantics, policy filtering identical across channels, and content-free structured logging. Two platform-visible differences are documented: a symlink rejection surfaces as `REPARSE_POINT_NOT_ALLOWED` (Linux: `SYMLINK_NOT_ALLOWED`), and Windows directory revisions may or may not move with children depending on volume settings, so `delete_directory` relies on a physical emptiness scan. The Windows `revision` remains a metadata-derived optimistic concurrency token — a same-tick, same-size blind write window exists by design, and ServerFS does not claim arbitrary-process compare-and-swap.
