# Windows native deployment

Long-term deployment layout for the native Windows stack. Since **v0.14.0**, the normal outer
lifecycle is persistent, current-user and non-elevated: Windows Task Scheduler 2.0
owns a windowless ServerFS watchdog, the watchdog owns a managed runner, and the runner
owns one Tunnel generation at a time. No Windows Service/SCM, LocalSystem account, HKLM
registration, stored password or administrator/UAC step is part of the intended supported path.

## Layout contract

| What | Where | In Git? |
|---|---|---|
| Native deployment config (source of truth) | repo root `serverfs.toml` | no (git-ignored; machine-specific paths) |
| Committed template for the above | repo root `serverfs.toml.example` | yes |
| Local environment config + proxy settings | repo root `.env` | no |
| Ops / compatibility scripts | `deployment\windows\` | yes |
| Python env with the installed abi3 wheel | `%LOCALAPPDATA%\ServerFS\connector-env` | never |
| Versioned tunnel-client binary | `%LOCALAPPDATA%\ServerFS\bin\tunnel-client-v<ver>-windows-amd64\` | never |
| Verified native wheels | `%LOCALAPPDATA%\ServerFS\wheels` | never |
| v0.14 lifecycle manifest/state/logs | `%LOCALAPPDATA%\ServerFS\service-manager\windows\` | never |
| Control Plane API key | `%USERPROFILE%\.config\serverfs\api-key` | never — product-enforced: it must live outside every configured workdir (`native_tunnel.py` refuses otherwise) |

Principle: the workdir holds development assets plus deployment config as the source of
truth; `%LOCALAPPDATA%\ServerFS` holds installed/runtime artifacts only; credential files
stay outside every workdir because the MCP surface can otherwise read them.

## Install and start the persistent current-user service

```powershell
$serverfs = "$env:LOCALAPPDATA\ServerFS\connector-env\Scripts\serverfs.exe"

& $serverfs doctor --config .\serverfs.toml
& $serverfs service install --config .\serverfs.toml --env-file .\.env
& $serverfs service start
& $serverfs service status
```

A **fresh install is registered disabled**: `install` writes and verifies the lifecycle
contract but does not silently start it. `service start` enables the task and starts the
watchdog. Reinstalling/updating an existing managed task preserves its enabled/disabled
operator intent. The v0.14 installer also rejects persistent-service installation from any
Python environment other than `%LOCALAPPDATA%\ServerFS\connector-env`, and verifies that
`serverfs-mcp` and `serverfs-windows-native` are both present there at the same version before
registering persistence. This prevents an editable repository `.venv` from becoming the
long-lived Task Scheduler runtime.

The Task Scheduler contract is bound to the current user with `InteractiveToken` and
`LeastPrivilege`. A user logon trigger starts the service after sign-in, while an indefinite
one-minute time-trigger heartbeat re-establishes the watchdog if the outer Task action is
lost. `MultipleInstancesPolicy=IgnoreNew` prevents duplicate healthy instances. Normal
recovery is layered:

```text
Task Scheduler
  -> windowless watchdog          (OS owner; logon + 1-minute heartbeat)
       -> managed runner          (Job Object containment; short bounded restart backoff)
            -> tunnel-client      (Tunnel-generation bounded restart backoff)
                 -> supervisor -> serve / Agent Bridge
```

The native current-user ownership mutex prevents a managed lifecycle and a direct/manual
`serverfs tunnel` lifecycle from running side by side. v0.14 installation can also perform a
controlled hand-over from the legacy v0.13.x manual launcher using frozen process identity,
rather than killing processes by name alone.

## Operate the service

```powershell
& $serverfs service status
& $serverfs service restart
& $serverfs service stop
& $serverfs service start
& $serverfs service uninstall
```

In v0.22.0, the default `service status` detail/JSON is unchanged. For a bounded, sanitized common projection, explicitly select `--format common-json` or `--format common-human`; it omits paths, PIDs, task names and private IDs, and reports `unknown`/`not-checked` rather than inferring health. Use `serverfs doctor` for deeper platform/configuration diagnostics. Phase F acceptance limits: [v0.21.0 native acceptance](../../docs/v0.21.0-phase-f-native-acceptance-2026-10-22.md).

`service stop` disables the scheduled task **before** ending the current action, so the
heartbeat cannot resurrect an intentional stop. `service uninstall` stops and unregisters
the persistent task and removes its local lifecycle artifacts.

Configuration changes in `serverfs.toml` or `.env` take effect after `service restart`.

## Optional native Cloudflare Tunnel (v0.24.2 release candidate)

**Not yet a published Windows feature.** The v0.24.2 candidate passed public ChatGPT 401/200, tools, cross-ingress read/write/edit/delete on the same writable workdirs, and Codex/Claude through independent same-user owners. Final exact-SHA CI and >60-second SSE/Connector-only restart evidence remain pending. See the [Windows Cloudflare runbook](../../docs/windows-cloudflare-tunnel.md) for the accepted owner topology, private-ACL contract, commands, rollback and remaining gates.

Each owner selects one provider. The accepted topology keeps the OpenAI native default and a separate `ServerFS Cloudflare WorkPC` owner (`service cloudflare-parallel`) under the same Windows user. Each owner has its own venv, state, watchdog and runner; both may use the same writable workdirs and share one Agent Bridge/Repo Ops service. The existing single-owner `service cloudflare` path still refuses to replace an active OpenAI provider in that owner. That guard does not prohibit the separate parallel owner, and there is no universal automated provider handover. Retiring OpenAI must not retire the shared Agent Bridge while Cloudflare depends on it. No Windows SCM service or administrator is introduced. Qoder's provider error predated the acceptance; Python urllib's default User-Agent received edge WAF 1010. These are scoped provider/edge caveats. Do not claim long-lived SSE or Connector-only restart behavior before the remaining evidence is recorded.

The command example below uses the single-owner `service cloudflare` path. It is not the accepted parallel-owner path and will refuse to replace an active OpenAI provider in that same owner. The accepted WorkPC setup keeps OpenAI active and uses a separate `ServerFS Cloudflare WorkPC` same-user owner through `service cloudflare-parallel`; consult the matching CLI help for its lifecycle syntax. Keep that owner's venv/state/watchdog/runner separate and preserve the shared Agent Bridge if Cloudflare depends on it after OpenAI retirement.

```powershell
$serverfs = "$env:LOCALAPPDATA\ServerFS\connector-env\Scripts\serverfs.exe"
& $serverfs bootstrap cloudflare-client   # pinned cloudflared 2026.10.7 with SHA-256 + signature
& $serverfs service cloudflare install --config 'C:\stage\serverfs.toml' --cloudflared "$env:LOCALAPPDATA\ServerFS\bin\cloudflared-2026.10.7-windows-amd64\cloudflared.exe" --token-file 'C:\private\cloudflare.token' --env-file 'C:\private\origin.env' --http-port 18765 --hostname 'mcp.example.invalid'
& $serverfs service cloudflare status
# After verifying the operator intentionally chose this dedicated service:
& $serverfs service cloudflare start
& $serverfs service cloudflare status
```

`origin.env` must configure **exactly one** HTTP API Key or OAuth and set `SERVERFS_HTTP_ALLOWED_HOSTS` to the exact external hostname. Both private input files must be owned by the current user with protected Windows DACL and be outside the checkout/all workdirs. This path is not the existing OpenAI `.env`; the raw Connector Token must never appear in command arguments or Git. A successful `enabled` status alone does not mean the connector is connected or that ChatGPT MCP works.

## Compatibility / emergency launcher

`deployment\windows\start-native.ps1` is retained for bounded compatibility and emergency
recovery work. It is **not** the intended normal v0.14 deployment path and must not be run in parallel
with a managed `serverfs service` instance.

## One-time prerequisites (per machine)

1. `connector-env` venv with the published `serverfs-windows-native` wheel
   (`serverfs bootstrap native-wheel --url ... --sha256 ...`, then `pip install`).
2. Official tunnel-client into the default data home. The command defaults to the ServerFS-tested release; another explicit release may be selected with `--version` plus its manifest SHA-256:
   `serverfs bootstrap tunnel-client`
3. `%USERPROFILE%\.config\serverfs\api-key` holding the Control Plane key
   (a scoped `rtk_` Runtime key is preferred over a full `sk-` key).
4. Repo root `serverfs.toml` + `.env` with `CONTROL_PLANE_TUNNEL_ID` and
   `SERVERFS_PROXY_*` (HTTP-only four-field proxy contract).


ACP profile configuration and operation: [ACP Guide](https://ntlx.github.io/ServerFS_MCP/docs/acp/).
