---
title: Windows Native
description: The v0.10 native Windows deployment — MCP stdio over a prebuilt Rust/NTFS kernel wheel, no Docker or MSVC.
---

Since v0.10.0 ServerFS runs natively on **Windows 11 x64 over local NTFS** as an MCP **stdio** service. Nothing listens on a port: the official OpenAI `tunnel-client` connects out to the control plane and speaks MCP through the child process's stdin/stdout, so the default native profile has no localhost listener. Linux Docker deployments are unchanged and remain the supported Linux shape.

The runtime chain:

```text
serverfs tunnel
  -> official pinned tunnel-client        (owns control-plane connectivity)
       -> sanitizer supervisor            (strips tunnel/proxy environment)
            -> serverfs serve             (MCP stdio child, sees no secrets)
                 -> serverfs-windows-native wheel (HANDLE-relative NTFS kernel)
```

Windows users consume two prebuilt wheels from the GitHub Release and never need Rust, Cargo, MSVC Build Tools or the Windows SDK. The kernel wheel is `cp312-abi3-win_amd64` (stable ABI, Python >= 3.12).

## Install

1. Install [uv](https://docs.astral.sh/uv/) (or any Python >= 3.12).
2. Install both release wheels into a venv:

```powershell
uv venv --python 3.12
.venv\Scripts\activate
uv pip install serverfs_mcp-<ver>-py3-none-any.whl serverfs_windows_native-<ver>-cp312-abi3-win_amd64.whl
```

3. Copy `serverfs.toml.example` to `serverfs.toml` and configure workdirs. Paths are operator configuration — agents only ever see aliases. `read_only = true` is the default and the kernel refuses mutations unless a workdir opts in. No secrets belong in `serverfs.toml`.
4. Run the health report until it exits 0:

```powershell
serverfs doctor --config serverfs.toml
```

`serverfs doctor` probes the native backend import/version, each workdir root open, the filesystem class, reparse topology, a real policy-filtered listing, a non-mutating write-capability check, the tunnel-client version and the project-managed HTTP proxy reachability — proxy and tunnel credentials are never displayed. Windows GA is local NTFS only: network shares, FAT/exFAT, ReFS and any storage whose class cannot be measured are reported as `FAIL` (fail closed, never a silent blessing).

## Connectivity

```powershell
serverfs bootstrap tunnel-client   # pinned official release, SHA-256 double-anchored
serverfs tunnel --config serverfs.toml `
  --tunnel-id tunnel_... --api-key-file C:\Users\you\.config\serverfs\api-key
```

The bootstrap stores the client under the user-owned ServerFS data directory and never modifies the machine PATH; its verification chain pins the official `SHA256SUMS.txt` digest and re-hashes the downloaded archive against it. `serverfs tunnel` auto-discovers the bootstrapped client, binds tunnel-client's health server to an ephemeral loopback port, passes the control-plane key only as a `file:` reference (the key file must live outside every workdir), and strips all `CONTROL_PLANE_*`, `TUNNEL_CLIENT_*`, `OPENAI_*`, `MCP_*`, `SERVERFS_PROXY_*` and proxy variables before spawning the ServerFS child. Proxy configuration uses the same four `SERVERFS_PROXY_*` fields in `.env` as Linux (HTTP only; SOCKS is not supported and not claimed).

Multiple active tunnel-client instances sharing one tunnel ID are unsupported upstream — do not run the native profile twice.

## What stays true on Windows

Every channel — read, list, stat, find, search and all five mutation tools — behaves as on Linux: handle-relative traversal with reparse points never followed, revision-guarded atomic publication, metadata preservation with fail-closed semantics, policy filtering identical across channels, and content-free structured logging. Two platform-visible differences are documented: a symlink rejection surfaces as `REPARSE_POINT_NOT_ALLOWED` (Linux: `SYMLINK_NOT_ALLOWED`), and Windows directory revisions may or may not move with children depending on volume settings, so `delete_directory` relies on a physical emptiness scan.
