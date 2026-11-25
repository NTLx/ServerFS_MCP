# Windows native component operations

This runbook covers fresh named instances (the v0.25.0 architecture) on Windows 11 x64 and local NTFS. Commands in this checkout target the unreleased v0.26.0 candidate. For v0.25.0 installations use the tagged source and matching artifacts. Do not mix versions. Core, Bridge owner and each Connector run through current-user Task Scheduler with windowless launchers. No WSL, VM, SCM service or elevated administrator process is needed.

## 1. Choose your deployment

| Need | Deployment |
| --- | --- |
| Local MCP client only | Native stdio; no persistent registration |
| Persistent HTTP file service | File-only Core |
| Dedicated Agent/Repo Ops execution | Owned Core/Bridge (same instance ID) |
| Multiple Cores sharing one Bridge | Shared Bridge (distinct owner ID, per-Core signed grants) |
| Optional public ingress | Independent Cloudflare or OpenAI Connector(s) |

The Core listens on loopback and owns caller authentication. A Connector targets one Core; the Bridge owns host execution and task state. Independent Bridge owners require disjoint writable roots. A Shared Core never operates its owner.

## 2. Prepare verified artifacts and private inputs

Use an ordinary, non-elevated PowerShell session. Prepare a trusted installed controller virtual environment containing the matching Core and Native Wheels, not an editable checkout. Python must be native x64, 3.12 or later. Use a trusted uv executable on PATH and a prepopulated offline dependency cache containing the runtime dependencies from the matching checkout's `uv.lock` and, when using a Bridge, `agent_bridge/uv.lock`. Metadata-only resolution is not proof that the required archives are cached. For an alternate cache, set `UV_CACHE_DIR` to its existing absolute path.

Obtain the artifacts and independently trusted lowercase SHA256 pins for the intended version. The v0.26.0 candidate installer expects:

| Distribution | Wheel |
| --- | --- |
| Core | `serverfs_mcp-0.26.0-py3-none-any.whl` |
| Windows Native | `serverfs_windows_native-0.26.0-cp312-abi3-win_amd64.whl` |
| Agent Bridge (only when used) | `serverfs_agent_bridge-0.26.0-py3-none-any.whl` |

Do not assume candidate artifacts are a published stable release. Verify the original trusted digest before installing the controller itself; a digest calculated from an untrusted download is not proof of publisher identity.

From the matching checkout root, use these example PowerShell variables. Replace paths and pins with real local values; keep credentials out of commands.

```powershell
$ctl = 'C:\ServerFS-private\controller\Scripts\python.exe'
$wheelDir = 'C:\ServerFS-private\wheels'
$coreSha = '<trusted-core-sha256>'
$nativeSha = '<trusted-native-sha256>'
$bridgeSha = '<trusted-bridge-sha256>'

function Invoke-SF {
    & $ctl -I -B @args
    if ($LASTEXITCODE -ne 0) { throw "ServerFS command refused: $LASTEXITCODE" }
}
```

The examples use this function to stop on failure. If you already have a verified installed controller, skip the bootstrap below. Otherwise, use a trusted absolute native Python executable, a fresh controller target under a private parent, and the same offline cache:

```powershell
$basePython = 'C:\Path\To\Python312\python.exe'
$controllerDir = 'C:\ServerFS-private\controller'
$coreWheel = Join-Path $wheelDir 'serverfs_mcp-0.26.0-py3-none-any.whl'
$nativeWheel = Join-Path $wheelDir 'serverfs_windows_native-0.26.0-cp312-abi3-win_amd64.whl'

if (Test-Path -LiteralPath $controllerDir) { throw 'Controller target already exists' }
if ((Get-FileHash -LiteralPath $coreWheel -Algorithm SHA256).Hash.ToLowerInvariant() -ne $coreSha) { throw 'Core Wheel hash mismatch' }
if ((Get-FileHash -LiteralPath $nativeWheel -Algorithm SHA256).Hash.ToLowerInvariant() -ne $nativeSha) { throw 'Native Wheel hash mismatch' }
uv --no-config venv --offline --python $basePython $controllerDir
if ($LASTEXITCODE -ne 0) { throw 'Controller venv creation failed' }
$controllerConstraints = Join-Path $controllerDir 'locked-constraints.txt'
uv --no-config export --offline --frozen --no-dev --no-emit-project --no-hashes --format requirements-txt --output-file $controllerConstraints
if ($LASTEXITCODE -ne 0) { throw 'Core runtime lock export failed' }
uv --no-config pip install --offline --python $ctl --constraint $controllerConstraints $coreWheel $nativeWheel
if ($LASTEXITCODE -ne 0) { throw 'Controller installation failed' }
uv --no-config pip check --python $ctl
if ($LASTEXITCODE -ne 0) { throw 'Controller dependency check failed' }
```

Confirm the installed packages report 0.26.0 and load from the private controller environment, not the checkout:

```powershell
Invoke-SF -c 'import importlib.metadata as m; import serverfs_mcp, serverfs_windows_native; print(m.version("serverfs-mcp"), m.version("serverfs-windows-native")); print(serverfs_mcp.__file__, serverfs_windows_native.__file__)'
```

Keep the controller and its base Python trusted and outside exposed workdirs.

Save the following minimal policy as `C:\ServerFS-private\blue.toml` (replace the path with a real readable NTFS directory):

```toml
[[workdirs]]
alias = "docs"
path = 'D:\ServerFS-data\docs'
read_only = true
```

The controller, input files, private credentials and component environments must not be exposed through any workdir. Do not bypass protected DACL or reparse-point checks. Existing foreign ACLs require separate diagnosis, not blanket permission changes.

For local stdio, configure the client to launch the installed Core via `serverfs serve --config <absolute TOML path>`; no named tasks or HTTP credential are needed.

## 3. Install and configure a file-only HTTP Core

Choose a unique non-default ID, unused loopback port and hostname. A native ID is 1–32 lowercase ASCII letters/digits, with optional internal hyphens. The following is the recommended new-instance route:

```powershell
$instance = 'blue'
$port = 18781
$hostname = 'mcp.example.invalid'
$config = 'C:\ServerFS-private\blue.toml'

Invoke-SF .\deployment\windows\install-v25-runtime.py --instance $instance --component core --wheel-dir $wheelDir --core-sha256 $coreSha --native-sha256 $nativeSha
Invoke-SF .\deployment\windows\configure-v25-private.py --core $instance $port $hostname $config
Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $instance install
Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $instance start
Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $instance status
```

Each stage has a distinct purpose:

- **Install runtime:** creates a fresh private venv from pinned Wheels, using version constraints exported offline from the trusted matching checkout's frozen runtime locks. Core, Connector and Bridge owner use `uv.lock`; the separate Agent Bridge worker uses `agent_bridge/uv.lock`. The private installation audit records the constraint file SHA256 values and verifies the installed dependency closure. A missing or incompatible cached archive causes a refusal; prepare the cache separately, then use a fresh instance for a new attempt. Constraints pin dependency versions, not the individual third-party archive hashes; retain a trusted cache and independently verified product Wheel pins.
- **Configure:** copies the policy into private state, generates a unique HTTP bearer key and publishes the manifest. It does not register a task.
- **Install/start:** registers then explicitly starts the selected Scheduler task. No Connector or Bridge is started for a file-only Core.

The generated credential is in the instance's private `services\core\http.env` under `%LOCALAPPDATA%\ServerFS\v25\instances\blue\`. Access it locally to configure the MCP client's private bearer header; do not print or upload it into chat, logs or shared workdirs. The exact Host allowlist is the configured hostname followed by the loopback authority (`mcp.example.invalid,127.0.0.1:18781` in this example).

Connect to `http://127.0.0.1:18781/mcp`. Verify anonymous/wrong-key denial, authenticated initialize, tools/list, list_workdirs and safe read/denied write. Expect 11 tools with every optional capability disabled. Task registration or enabled status is not proof of a working HTTP listener or authenticated MCP. Direct public HTTPS exposure and any reverse proxy are independently managed.

## 4. Optional: deploy with an owned or shared Bridge

Choose this path **instead of** file-only configuration. Once a manifest exists, the initial helper is not an upgrade command. Use a native TOML that explicitly enables the appropriate per-workdir policy. Repo Ops alone can be enabled with `repo_ops_enabled = true`; Agent delegation also needs configured native runtime policy (see [Agent Bridge](../../agent_bridge/README.md)). Do not expose provider credentials or use a fake-runtime fixture.

For an owned pair, use one ID for Core and Bridge. Install both environments before initial configuration. The Bridge runtime install additionally provisions its separate Agent Bridge worker venv.

```powershell
$instance = 'blue-owned'
$port = 18782
$hostname = 'mcp-owned.example.invalid'
$config = 'C:\ServerFS-private\owned.toml'

Invoke-SF .\deployment\windows\install-v25-runtime.py --instance $instance --component core --wheel-dir $wheelDir --core-sha256 $coreSha --native-sha256 $nativeSha
Invoke-SF .\deployment\windows\install-v25-runtime.py --instance $instance --component bridge --wheel-dir $wheelDir --core-sha256 $coreSha --native-sha256 $nativeSha --bridge-sha256 $bridgeSha
Invoke-SF .\deployment\windows\configure-v25-private.py --core $instance $port $hostname $config --bridge-id $instance
Invoke-SF -m serverfs_mcp.windows_v25.cli bridge --instance $instance install
Invoke-SF -m serverfs_mcp.windows_v25.cli bridge --instance $instance start
Invoke-SF -m serverfs_mcp.windows_v25.cli bridge --instance $instance status
Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $instance install
Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $instance start
Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $instance status
```

For a shared Bridge, install every Core runtime and one Bridge runtime under a distinct owner ID. Prepare separate policies and unused ports for each Core and an explicit owner policy containing their effective Agent/Repo Ops workdir policies. For example, after those runtime installs:

```powershell
Invoke-SF .\deployment\windows\configure-v25-private.py --core core-a 18801 mcp-a.example.invalid 'C:\ServerFS-private\a.toml' --core core-b 18802 mcp-b.example.invalid 'C:\ServerFS-private\b.toml' --bridge-id owner-blue --bridge-policy 'C:\ServerFS-private\owner.toml'
Invoke-SF -m serverfs_mcp.windows_v25.cli bridge --instance owner-blue install
Invoke-SF -m serverfs_mcp.windows_v25.cli bridge --instance owner-blue start
Invoke-SF -m serverfs_mcp.windows_v25.cli bridge --instance owner-blue status

foreach ($id in @('core-a', 'core-b')) {
    Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $id install
    Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $id start
    Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $id status
}
```

The initial helper accepts up to 16 Cores per shared owner and generates distinct HTTP keys and signed Core grants/bindings; each Core's enabled Bridge policies must be an exact subset of the owner's effective workdir/action/runtime policy. It checks existing deployments, private storage exposure and overlapping writable roots, including legacy Bridge ownership. These are preflight checks, not a global port reservation. Serialize provisioning with other operators.

Verify authorized runtime discovery or Repo Ops, depending on enabled capabilities, and denial of foreign tasks/workdirs. Keep private owner state intact. The v0.25.0 helper covers initial bearer authentication and explicit nonproxy runtime policies only; no live grant append/rotation or automatic update. For OAuth or specialized policies, use the advanced manual path below.

**v0.26.0 candidate only — named Agent proxy:** with matching candidate source and artifacts, explicitly set `use_proxy: true` on the selected enabled runtime or ACP profile in the owner request. Supply `--agent-proxy-env-file` only when at least one runtime opts in. This input is a UTF-8, maximum 4096-byte dedicated file containing `SERVERFS_AGENT_PROXY_URL` and optionally `SERVERFS_AGENT_NO_PROXY` (no empty values, credentials, duplicate keys, dotenv expansion or other fields). Keep it at an absolute ACL-protected path outside the checkout and all exposed workdir roots, with no reparse components. The create-only helper publishes protected `agent-proxy.env` in the owner home; the independent Bridge reads it and passes proxy policy through its private bootstrap. Disabled or direct runtimes do not inherit the opt-in policy. Verify proxy, direct and unavailable-proxy behavior with a disposable named owner before migration. Tunnel, Jev and provider authentication are independent. Do not use this flag with v0.25.0 artifacts or edit installed private state in place.

## 5. Optional: add independent Connectors

Install each required Connector component runtime once per Core instance; additional IDs of that provider share its environment but have separate manifests, credentials and tasks. Set `$instance` to the verified target Core ID (for example, `core-a` for the shared topology), not a previously used ID. The external Tunnel identity and route must be new or otherwise independently authorized; never reuse unrelated credentials.

**Cloudflare.** Prepare a verified cloudflared.exe, its SHA256 pin and a private token file. Configure the external route to the selected Core's loopback port, using the Core's configured Host authority.

```powershell
Invoke-SF .\deployment\windows\install-v25-runtime.py --instance $instance --component cloudflare --wheel-dir $wheelDir --core-sha256 $coreSha --native-sha256 $nativeSha
Invoke-SF .\deployment\windows\configure-v25-connector.py --instance $instance --connector-id edge-cf --component cloudflare --cloudflared $cloudflared --cloudflared-sha256 $cfSha --credential-file $tokenFile
Invoke-SF -m serverfs_mcp.windows_v25.cli cloudflare --instance $instance --connector-id edge-cf install
Invoke-SF -m serverfs_mcp.windows_v25.cli cloudflare --instance $instance --connector-id edge-cf start
Invoke-SF -m serverfs_mcp.windows_v25.cli cloudflare --instance $instance --connector-id edge-cf status
```

The variables denote absolute trusted executable/credential paths and the independently verified digest. Local token-envelope checks are not proof of remote route or ownership. The helper reports `manual_cloudflare_peers_unchecked` for legacy/manual peers without recorded Tunnel identity; check those remotely before activation.

**OpenAI.** Prepare a verified tunnel-client.exe and its matching bundled cloudflared.exe, their separate SHA256 pins, the Tunnel ID and a private control-plane key file. The helper derives the Authorization header from the actual Core bearer configuration; this Connector requires supported static bearer authentication, not OAuth-only.

```powershell
Invoke-SF .\deployment\windows\install-v25-runtime.py --instance $instance --component openai --wheel-dir $wheelDir --core-sha256 $coreSha --native-sha256 $nativeSha
Invoke-SF .\deployment\windows\configure-v25-connector.py --instance $instance --connector-id edge-oa --component openai --cloudflared $bundledCloudflared --cloudflared-sha256 $bundledSha --tunnel-client $tunnelClient --tunnel-client-sha256 $clientSha --credential-file $keyFile --tunnel-id $tunnelId
Invoke-SF -m serverfs_mcp.windows_v25.cli openai --instance $instance --connector-id edge-oa install
Invoke-SF -m serverfs_mcp.windows_v25.cli openai --instance $instance --connector-id edge-oa start
Invoke-SF -m serverfs_mcp.windows_v25.cli openai --instance $instance --connector-id edge-oa status
```

Both configuration helpers copy private inputs and publish manifests, without registering or starting tasks. Connector startup requires the intended Core to be ready. Verify external registration, routing, TLS and authenticated MCP independently; owner-alive status is not public acceptance.

## 6. Operate, update or retire

The controller interface is `python -I -B -m serverfs_mcp.windows_v25.cli`. Select the exact component, instance and Connector ID (where applicable).

| Action | Meaning |
| --- | --- |
| status | Inspect the selected task; distinguish enabled, registered and running |
| start / stop | Operate only the selected component |
| restart | Stop and start that component; verify readiness again |
| uninstall | Remove the verified disabled/offline task, retaining private configuration |

For example, restart only one Cloudflare Connector:

```powershell
Invoke-SF -m serverfs_mcp.windows_v25.cli cloudflare --instance $instance --connector-id edge-cf restart
Invoke-SF -m serverfs_mcp.windows_v25.cli cloudflare --instance $instance --connector-id edge-cf status
```

Confirm unrelated Core/Bridge/Connector identity remains unchanged, then establish a fresh public MCP session. Do not assume old SSE session replay. For retirement, stop ingress first, then Core, and only then an owner Bridge after accounting for all attached Cores and active tasks/leases. Never terminate processes by executable name or use legacy group commands on named instances.

The installer and configuration helpers are create-only. They leave one-shot `runtime-install-attempt` and `private-config-attempt` records; failed attempts retain evidence and may have partial private state. Do not blindly rerun, overwrite, re-ACL or remove that state. Plan explicit recovery or migration with preserved versions, configuration and state; no automatic in-place rollback or credential rotation is provided.

## 7. Advanced: manual configuration and private layout

The manual `prepare` / `configure` path is for operator-managed inputs not covered by the initial helper. Do not repeat it after successful helper configuration. Prepare creates/validates private directories, not environments or credentials. The offline runtime installer remains the recommended way to populate fresh component venvs.

For a file-only Core with operator-managed HTTP auth (including an independently configured OAuth service, when required):

```powershell
Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $instance prepare
# After runtime installation and protected inputs are ready:
Invoke-SF -m serverfs_mcp.windows_v25.cli core --instance $instance configure --config $config --auth-file $auth --port $port --hostname $hostname
```

The mode controls the TOML policy, not merely whether a Bridge process is running. Auth file and configuration paths are absolute; exactly one HTTP authentication mode is required. For named Bridge owners, configure the private policy and worker interpreter:

```powershell
Invoke-SF -m serverfs_mcp.windows_v25.cli bridge --instance $bridgeId prepare
# After owner/worker runtime, private config, grants and bindings exist:
Invoke-SF -m serverfs_mcp.windows_v25.cli bridge --instance $bridgeId configure --config $bridgeConfig --bridge-python $workerPython
```

Manual Bridge inputs must use the exact resources from `WindowsBridgeResources`, current SID, `core_grants_enabled: true`, the owner policy and signed health/workdir method grants. Use the installed worker's `core_workdir_policy_digest` and `serverfs_agent_bridge.core_grants_private` writer; `windows_v25.shared_core_grants.issue_shared_core_capabilities` handles shared issuance. Supply a matching private Core binding before its configure/start. Do not reuse the default Bridge's SID-global pipe or state. For manual Connector inputs, consult the selected component's `--help` and ensure the origin identity matches the Core. Configure publishes a new manifest exclusively; use install/start as in the main flow.

The private layout is derived from the current user's LocalAppData:

```text
%LOCALAPPDATA%\ServerFS\v25\instances\<id>\
  envs\<component>\Scripts\pythonw.exe
  envs\agent-bridge\Scripts\python.exe          # Bridge worker only
  services\core\{service.json,serverfs.toml,http.env,bridge-binding.json}
  services\bridge\{service.json,bridge.json,state,locks}
  services\<provider>\connectors\<connector-id>\service.json
```

Only selected components exist; bindings are not needed in file-only mode. The protected owner grant file is `services\bridge\state\core-grants.json`. The Core, Bridge owner and Connector environments contain matching Core/Native distributions; the worker contains the separate Agent Bridge distribution. Preserve the protected DACL and reject reparse-point traversal. `storage.check_private` verifies the private contract; `windows_private_artifacts.protect_new_file` is for newly created inputs only.

## Compatibility and release evidence

For an existing default installation, use its version-matched scoped compatibility commands and a deliberate migration plan. The [integration baseline](https://github.com/NTLx/ServerFS_MCP/blob/fffc0adb8341edb30ac7b03e72a017ca95b8c83d/deployment/windows/README.md) preserves earlier deployment details. The I-series fixtures are acceptance evidence, not general installers.

See the [release checklist](../../docs/v0.25.0-release-checklist-2026-11-21.md) for open real-provider I11-B and OpenAI public ingress gates. Local registration, simulated tests and historical candidate results do not establish final release artifact acceptance.
