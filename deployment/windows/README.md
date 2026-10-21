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

`service stop` disables the scheduled task **before** ending the current action, so the
heartbeat cannot resurrect an intentional stop. `service uninstall` stops and unregisters
the persistent task and removes its local lifecycle artifacts.

Configuration changes in `serverfs.toml` or `.env` take effect after `service restart`.

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
