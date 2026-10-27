---
title: Windows Native
description: Native Windows 11 x64 + NTFS deployment — prebuilt Rust kernel, v0.23.2 Deterministic Repo Ops, native Codex/Claude/Qoder and configurable ACP delegation, and the current-user Task Scheduler lifecycle.
---

Since v0.10.0 ServerFS runs natively on **Windows 11 x64 over local NTFS** as an MCP **stdio** service. Nothing listens on a port: the official OpenAI `tunnel-client` connects out to the control plane and speaks MCP through the child process's stdin/stdout, so the default native profile has no localhost listener. Linux Docker deployments are unchanged and remain the supported Linux shape.

v0.11 adds **Agent delegation** to this deployment for Codex, Claude Code and Qoder through the same ten Agent tools over a Named-Pipe Bridge, accepted through the live ChatGPT tunnel E2E. v0.23.2 adds validated native Windows 11 x64 **Deterministic Repo Ops**, using the same independently gated, no-Agent-fallback Bridge transport already used by Linux and validated native Apple Silicon macOS.

v0.14.0 adds the persistent outer lifecycle without changing that MCP/Agent surface. Its owner is a **current-user, least-privilege Task Scheduler 2.0 task** — not an SCM/LocalSystem Windows Service. Normal install, runtime, recovery, maintenance and removal remain non-elevated and store no account password.

The runtime chain:

```text
Task Scheduler 2.0                    (InteractiveToken · LeastPrivilege)
  -> windowless watchdog              (logon + 1-minute heartbeat owner)
       -> managed runner              (kill-on-close Job Object; bounded restart)
            -> official tunnel-client (ServerFS-tested release by default)
                 -> sanitizer supervisor
                      -> Agent Bridge (optional, separate venv + lifecycle lease)
                           -> provider CLI (codex / claude / qodercli)
                      -> serverfs serve (MCP stdio child; no secrets)
                           -> serverfs-windows-native wheel (HANDLE-relative NTFS kernel)
```

Windows users consume prebuilt wheels from the GitHub Release and never need Rust, Cargo, MSVC Build Tools or the Windows SDK. The kernel wheel is `cp312-abi3-win_amd64` (stable ABI, Python >= 3.12).

## Install

The product and the Agent Bridge are **independent distributions in two isolated virtual environments**, connected by a frozen process boundary: the supervisor launches the Bridge through `SERVERFS_BRIDGE_PYTHON`. They are never installed into one shared dependency graph (measured: `serverfs-mcp` freezes `mcp==2.2.0` while the pinned Qoder SDK declares `mcp<2.0.0`).

1. Install [uv](https://docs.astral.sh/uv/) (or any Python >= 3.12).
2. Install the **ServerFS environment** (product + native wheels) and the **Agent Bridge environment** (bridge wheel) into the two stable per-user runtime locations:

```powershell
$serverfsRoot = "$env:LOCALAPPDATA\ServerFS\connector-env"
$bridgeRoot = "$env:LOCALAPPDATA\ServerFS\bridge-env"

uv venv --python 3.12 $serverfsRoot
uv pip install --python "$serverfsRoot\Scripts\python.exe" serverfs_mcp-<ver>-py3-none-any.whl serverfs_windows_native-<ver>-cp312-abi3-win_amd64.whl

uv venv --python 3.12 $bridgeRoot
uv pip install --python "$bridgeRoot\Scripts\python.exe" serverfs_agent_bridge-<ver>-py3-none-any.whl
```

3. Point the supervisor at the Bridge venv by setting `SERVERFS_BRIDGE_PYTHON` in the `.env` next to `serverfs.toml` to the absolute path of `%LOCALAPPDATA%\ServerFS\bridge-env\Scripts\python.exe` for the current user (the tunnel launcher injects it into the serverfs process environment). The Bridge environment never needs the product package, and the product environment never needs the Bridge package.
4. Copy `serverfs.toml.example` to `serverfs.toml` and configure workdirs. Paths are operator configuration — agents only ever see aliases. `read_only = true` is the default and the kernel refuses mutations unless a workdir opts in. No secrets belong in `serverfs.toml`. Agent delegation is **disabled unless you add an `[agent]` section with `enabled = true`** plus per-workdir `agent_mode`/`agent_runtimes` policy; a config without `[agent]` behaves exactly like v0.10.
5. Run the health report until it exits 0:

```powershell
$serverfs = "$env:LOCALAPPDATA\ServerFS\connector-env\Scripts\serverfs.exe"
& $serverfs doctor --config serverfs.toml
```

`serverfs doctor` probes the native backend import/version, each workdir root open, the filesystem class, reparse topology, a real policy-filtered listing, a non-mutating write-capability check, the tunnel-client version and the project-managed HTTP proxy reachability — proxy and tunnel credentials are never displayed. Windows GA is local NTFS only: network shares, FAT/exFAT, ReFS and any storage whose class cannot be measured are reported as `FAIL` (fail closed, never a silent blessing).

With `[agent]` enabled, doctor additionally reports — read-only, never starting a Bridge or a provider — the per-workdir Agent policy, each runtime's enabled state and proxy routing, the Agent data home, the private-state safety of whatever exists, the Agent proxy endpoint wiring and reachability (the endpoint itself is never displayed), the user identity derivation, and that the Bridge package is importable by the configured `SERVERFS_BRIDGE_PYTHON` interpreter.

## Deterministic Repo Ops on Windows (v0.23.2+)

Repo Ops is configured independently per workdir and may be enabled while Agent mode remains disabled. The public MCP surface is the same four-tool contract as other supported platforms: `inspect_repo`, `run_repo_action`, `commit_repo_changes`, and `push_repo`. It never starts a provider runtime and has no Agent fallback.

Windows-specific containment is fail closed:

- Repo paths reject NTFS reparse points/junctions before repository execution, matching the native filesystem boundary.
- Repo Ops children are created suspended, assigned to a Bridge-owned Job Object, and resumed only after containment succeeds.
- Timeout, cancellation, or output overflow terminates and drains the descendant tree before the shared writer lease is released; if cleanup cannot be proven, the operation preserves the lease until an external Bridge restart.
- Hardened Git-for-Windows execution remains non-interactive, uses a narrow environment, disables hooks/signing/askpass/SSH overrides, and uses the native `NUL` hooks sink where required.

The v0.23.2 Windows acceptance exercised the installed 0.23.2 stdio MCP server through the managed 0.23.2 Bridge with a disposable local Git repository and `file://` bare upstream: all 27 tools were present, Repo Ops worked while Agent mode was disabled, an Agent submission was rejected with `AGENT_DISABLED`, and create → inspect → explicit-file commit → exact upstream push → final clean status completed without Agent fallback. See the [v0.23.2 Windows Repo Ops acceptance](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.23.2-windows-repo-ops-acceptance-2026-10-28.md).

## Persistent current-user lifecycle (v0.14.0+)

After `doctor` passes, install and start the managed lifecycle from the stable ServerFS `connector-env`:

```powershell
$serverfs = "$env:LOCALAPPDATA\ServerFS\connector-env\Scripts\serverfs.exe"
& $serverfs service install --config serverfs.toml --env-file .env
& $serverfs service start
& $serverfs service status
```

A fresh `service install` registers the current-user task **disabled**. It writes and re-reads the Task Scheduler contract but does not silently start ServerFS. `service start` enables the task and starts its windowless watchdog. Reinstalling an existing task preserves the operator's enabled/disabled intent. The v0.14 installer rejects persistent-service installation from any other Python environment and verifies that `serverfs-mcp` and `serverfs-windows-native` are installed in `connector-env` at exactly the same version before registering persistence; a repository `.venv` is intentionally not a valid persistent runtime.

The scheduled task has two native triggers: a user-logon trigger and an indefinite one-minute `TimeTrigger` heartbeat. `MultipleInstancesPolicy=IgnoreNew` keeps a healthy watchdog single-instance; if the outer Task action disappears, a later heartbeat recreates it. The watchdog independently restarts the runner with a short bounded backoff, and the runner independently restarts Tunnel generations. The Windows ownership mutex rejects a second direct/manual native Tunnel while the managed lifecycle owns the slot.

Intentional lifecycle operations are explicit:

```powershell
& $serverfs service restart
& $serverfs service stop
& $serverfs service start
& $serverfs service uninstall
```

### Common status formats (v0.22.0)

The stable v0.23.0 release retains this behavior. The default `service status` detail/JSON is unchanged. Operators may explicitly run `serverfs service status --format common-json` or `serverfs service status --format common-human` for a bounded, sanitized projection. It omits paths, PIDs, task names and private IDs, and uses `unknown`/`not-checked` rather than inventing health. Use `serverfs doctor` for deeper platform/configuration diagnostics. See [Phase F acceptance and limits](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.22.0-phase-f-windows-acceptance-2026-10-22.md).

`service stop` disables the scheduled task before ending its current action, so the heartbeat cannot resurrect an intentional stop. v0.14.0 also performs a controlled hand-over from the legacy v0.13.x manual launcher when it can freeze and revalidate the exact legacy process identities; it never kills a tree by executable name alone.

## Agent runtimes on Windows

v0.19.0 ships configured generic `acp-<id>` profiles. OpenCode 2.1.2 with `opencode/glm-5.3-flash-free` passed Windows end-to-end acceptance, including prompt, persisted-session continuation, and active-tool cancellation/cleanup. The containment implementation creates the child with `CREATE_SUSPENDED`, assigns it to the existing supervisor Job Object, then resumes it; task-level stop terminates the ACP process tree without creating a second Job Object.

Each runtime is a real provider process; capabilities are recorded per runtime, not advertised uniformly:

| Runtime | Model discovery | Live steering | Proxy routing |
|---|---|---|---|
| Codex | catalog + request-scoped override | yes (its own loopback control channel is bypassed from proxy policy) | `use_proxy = true` on hosts without a direct provider route |
| Claude Code | unsupported (`model_discovery: unsupported`) | no (`live_steer = false`) | `use_proxy = true` routes provider egress through the injected proxy (measured: external CONNECTs attributed to the claude child, 0 loopback); `false` for direct hosts |
| Qoder | catalog (pricing state read live, never hard-coded) | no (`live_steer = false`) | same injection model as Claude |

The Agent egress proxy is a **credentialless HTTP proxy** supplied by the operator through `SERVERFS_AGENT_PROXY_URL`; an authenticated upstream proxy is not part of the native support surface. The Claude/Qoder provider children consume the injected `HTTPS_PROXY`/`NO_PROXY` overlay; Codex's SDK-to-CLI control traffic stays on stdio and its loopback endpoints are exempt from proxy policy.

## Tunnel bootstrap and connectivity

```powershell
& $serverfs bootstrap tunnel-client   # defaults to the ServerFS-tested official release
```

The bootstrap stores the client under the user-owned ServerFS data directory and never modifies the machine PATH. The current ServerFS-tested baseline is `v0.0.17`; it is a tested default, not a hard compatibility lock. Another official release can be selected with `--version`, and non-default versions must also provide `--manifest-sha256` for that release's official `SHA256SUMS.txt`, preserving the same double-anchor verification chain before the downloaded archive is accepted. The v0.14 managed runner auto-discovers the bootstrapped client, binds tunnel-client's health server to an ephemeral loopback port, passes the control-plane key only as a `file:` reference (the key file must live outside every workdir), and strips all `CONTROL_PLANE_*`, `TUNNEL_CLIENT_*`, `OPENAI_*`, `MCP_*`, `SERVERFS_PROXY_*` and proxy variables before spawning the ServerFS child. Proxy configuration uses the same four `SERVERFS_PROXY_*` fields in `.env` as Linux (HTTP only; SOCKS is not supported and not claimed).

Direct `serverfs tunnel` remains useful for bounded diagnostics and compatibility work, but it is not the intended normal v0.14+ persistent deployment path. Multiple active tunnel-client instances sharing one tunnel ID are unsupported upstream, and the current-user ownership guard refuses a managed/direct double-owner shape.

## What stays true on Windows

Every channel — read, list, stat, find, search and all five mutation tools — behaves as on Linux: handle-relative traversal with reparse points never followed, revision-guarded atomic publication, metadata preservation with fail-closed semantics, policy filtering identical across channels, and content-free structured logging. Two platform-visible differences are documented: a symlink rejection surfaces as `REPARSE_POINT_NOT_ALLOWED` (Linux: `SYMLINK_NOT_ALLOWED`), and Windows directory revisions may or may not move with children depending on volume settings, so `delete_directory` relies on a physical emptiness scan. The Windows `revision` remains a metadata-derived optimistic concurrency token — a same-tick, same-size blind write window exists by design, and ServerFS does not claim arbitrary-process compare-and-swap.

For complete ACP setup, validation, task lifecycle, and troubleshooting, see the [ACP Guide](../acp/).
