# Windows native deployment

Long-term deployment layout for the native Windows stack (v0.10.x GA, single-active
ChatGPT connector tunnel). This replaces the temporary external Phase F acceptance
layout with the repository-centered deployment layout below.

## Layout contract

| What | Where | In Git? |
|---|---|---|
| Native deployment config (source of truth) | repo root `serverfs.toml` | no (git-ignored; machine-specific paths) |
| Committed template for the above | repo root `serverfs.toml.example` | yes |
| Local environment config + proxy settings | repo root `.env` (sibling of `serverfs.toml`, auto-discovered by `serverfs tunnel`) | no |
| Ops scripts | `deployment\windows\` | yes |
| Python env with the installed abi3 wheel | `%LOCALAPPDATA%\ServerFS\connector-env` | never |
| Pinned tunnel-client binary | `%LOCALAPPDATA%\ServerFS\bin\tunnel-client-v<ver>-windows-amd64\` (default data home) | never |
| Verified native wheels | `%LOCALAPPDATA%\ServerFS\wheels` | never |
| Launcher logs | `%LOCALAPPDATA%\ServerFS\logs` | never |
| Control Plane API key | `%USERPROFILE%\.config\serverfs\api-key` | never — product-enforced: it must live outside every configured workdir (`native_tunnel.py` refuses otherwise) |

Principle: the workdir holds development assets plus deployment config as the source of
truth; `%LOCALAPPDATA%\ServerFS` holds installed/runtime artifacts only; credential files
stay outside every workdir because the MCP surface can otherwise read them.

## Start

```powershell
powershell -ExecutionPolicy Bypass -File deployment\windows\start-native.ps1
```

The launcher reads `CONTROL_PLANE_TUNNEL_ID` from the repo `.env`, starts
`serverfs tunnel` hidden, and writes logs to `%LOCALAPPDATA%\ServerFS\logs\`.
One active tunnel per tunnel ID: never start a second tree while one runs.

## Check / stop

```powershell
& "$env:LOCALAPPDATA\ServerFS\connector-env\Scripts\serverfs.exe" doctor --config .\serverfs.toml
taskkill /PID <launcher PID> /T /F     # kills launcher -> tunnel-client -> supervisor -> serve
```

`serverfs tunnel` supervises the tree: tunnel-client spawns `serverfs_mcp.supervisor`
(env sanitizer) which spawns the `serverfs serve` MCP stdio child. A config change in
`serverfs.toml` or the sibling `.env` takes effect only after such a full restart.

## One-time prerequisites (per machine)

1. `connector-env` venv with the published `serverfs-windows-native` wheel
   (`serverfs bootstrap native-wheel --url ... --sha256 ...`, then `pip install`).
2. Pinned tunnel-client into the default data home:
   `serverfs bootstrap tunnel-client`
3. `%USERPROFILE%\.config\serverfs\api-key` holding the Control Plane key
   (a scoped `rtk_` Runtime key is preferred over a full `sk-` key).
4. Repo root `serverfs.toml` + `.env` with `CONTROL_PLANE_TUNNEL_ID` and
   `SERVERFS_PROXY_*` (HTTP-only four-field proxy contract).
