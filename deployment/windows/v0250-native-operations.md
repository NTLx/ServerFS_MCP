# Windows v0.25.0 independent component operations

This is the named-instance operator contract, **not** the legacy v0.14/v0.24
`serverfs service` watchdog CLI. The examples below are intended for an
operator-controlled fresh instance after the exact release artifacts and
private inputs are available. They are not a production migration command.

## Architecture

Windows Task Scheduler owns four independent component lifecycles: Core,
Agent Bridge owner, Cloudflare Connector and OpenAI Connector. Each runs
with the current user's interactive token, least privilege and a windowless
Python launcher. No WSL, VM, SCM service, administrator or central service
manager is required for the named-instance lifecycle.

- Core owns its MCP HTTP listener on 127.0.0.1 and enforces HTTP authorization.
- Bridge is optional; file-only Core needs no Bridge binding. Repo Ops and Agent
  delegation require a valid signed workdir grant. Owned Bridge uses the
  Core's instance ID; shared Bridge has its own distinct ID and individual
  Core keys.
- Each Connector is optional and separately registered. A Connector belongs
  to a specific Core and must match that Core's configured hostname and port.
  Core/Bridge do not depend on a Connector to start.
- Independent Bridge owners must not claim overlapping writable workdir roots.
  Multiple Cores can instead use the same explicitly provisioned shared Bridge.

## Private installation prerequisites

Use a trusted installed Core interpreter (not an editable source checkout)
with the corresponding Windows Native Wheel installed. Obtain the released
artifact hashes and verify the three distribution versions and hashes.
The named-instance layout is:

```text
%LOCALAPPDATA%\ServerFS\v25\instances\<id>\
  envs\core\Scripts\pythonw.exe
  envs\bridge\Scripts\pythonw.exe
  envs\agent-bridge\Scripts\python.exe
  envs\cloudflare\Scripts\pythonw.exe
  envs\openai\Scripts\pythonw.exe
  services\core\service.json
  services\bridge\bridge.json
  services\bridge\state\core-grants.json
  services\bridge\locks\
  services\core\bridge-binding.json
  services\cloudflare\connectors\<connector-id>\service.json
  services\openai\connectors\<connector-id>\service.json
```

Only provision components you intend to use. The Core, Bridge owner and
Connector venvs require installed Core + matching Windows Native Wheels.
The separate worker venv requires the Agent Bridge Wheel. `prepare` creates
or validates private directories, not venvs or credentials. For fresh named
instances use the offline runtime installer in the next section. Do not repair
existing foreign ACLs or bypass reparse point checks. The product's `storage.check_private` and
`windows_private_artifacts.protect_new_file` implement the private-path
contract; the latter is for newly created artifacts only. Existing paths must
be checked, not blindly re-ACL'd.

All credentials, binding files, grant files and runtime environments must
remain private and outside all exposed workdir roots. Do not put tokens in
command-line values, source control or diagnostics. `configure` publishes
a new manifest exclusively; it is not an overwrite/update operation.
`uninstall` removes only the verified offline task, not private configuration.

## Offline runtime provisioning

Run `install-v25-runtime.py` from a trusted installed 0.25.0 Core + Native
controller venv using the ordinary current user. The Wheel directory must
contain the release-named Core, Native and (only for Bridge) Agent Bridge
Wheels. Supply their published lowercase SHA256 digests as variables;
never derive trust from an unverified downloaded file alone. The x64 native
Wheel name is fixed to the accepted release's cp312-abi3-win_amd64 artifact.
A trusted `uv` on PATH and an already populated offline dependency cache
are prerequisites. Set `UV_CACHE_DIR` to the absolute existing cache path
if using a non-default cache. The installer keeps that variable, forces
copy mode and offline resolution, ignores uv project configuration and
never forwards provider credentials or remote index environment overrides.
The three product Wheels are hash-pinned; transitive dependencies are
resolved from the trusted local cache, not individually hash-pinned here.

```powershell
# $ctl, $wheelDir and SHA256 variables supplied by the operator.
& $ctl -I -B .\deployment\windows\install-v25-runtime.py --instance $instance --component core --wheel-dir $wheelDir --core-sha256 $coreSha --native-sha256 $nativeSha
# Optional Bridge owner AND its separate worker (same instance for owned mode):
& $ctl -I -B .\deployment\windows\install-v25-runtime.py --instance $bridgeId --component bridge --wheel-dir $wheelDir --core-sha256 $coreSha --native-sha256 $nativeSha --bridge-sha256 $bridgeSha
```

Use `--component cloudflare` or `--component openai` separately for the
corresponding ingress runtime. Connector IDs for the same component/instance
share that component's venv; do not reinstall it for each connector. Stop on
any nonzero exit. This installer prepares directories, creates only fresh
venvs, validates installed bytes and protects newly installed files. It does
not publish `service.json`, generate grants, register tasks or start services.
A one-shot `runtime-install-attempt` directory contains pinned Wheel copies
and the terminal redacted audit when reached; a failed attempt is retained.
Never overwrite a partially installed environment; investigate and plan
explicit recovery separately. No automatic in-place upgrade is provided.

## Initial private configuration (recommended for fresh installations)

After offline runtime installation, use `configure-v25-private.py` with
absolute operator-authored TOML paths. It generates new independent HTTP
bearer keys and, where applicable, signed Bridge grants and Core bindings
inside the private instance homes. It publishes the verified component
manifests but **does not register or start any Task Scheduler task**.

```powershell
# File-only: TOML must disable both Agent delegation and Repo Ops.
& $ctl -I -B .\deployment\windows\configure-v25-private.py --core $instance $port $hostname $config

# Owned: install the Core and Bridge runtimes under the same ID first.
& $ctl -I -B .\deployment\windows\configure-v25-private.py --core $instance $port $hostname $config --bridge-id $instance

# Shared: install a distinct Bridge owner and all Core runtimes first.
# $bridgePolicy defines the complete owner workdir/runtime policy.
& $ctl -I -B .\deployment\windows\configure-v25-private.py --core $coreA $portA $hostA $policyA --core $coreB $portB $hostB $policyB --bridge-id $bridgeId --bridge-policy $bridgePolicy
```

Each Core has an exact subset of the shared Bridge's workdir policies;
matching aliases must have identical effective access, action and runtime
configuration. Up to 16 Core IDs per initial shared configuration are
accepted. The Bridge worker derives its authoritative policy digests.
Each Core gets distinct credentials. The helper refuses existing manifests,
tasks, occupied ports, reused private files or nonfresh Bridge state.
It also rejects exposed private ServerFS roots and checks existing Core and
Bridge workdir policies against the new private paths.

Stop on nonzero exit. Coordinate provisioning serially with other operators;
the preflight is not a global cross-instance port reservation. A failed
publication may leave private files, manifests and one-shot
`private-config-attempt` markers; do not rerun or blindly delete them.
Current scope: new instances only, bearer auth, explicit nonproxy runtime
policies, no live grant append or rotation. For OAuth or specialized
provider/proxy settings, retain the separate operator-managed configuration
path; these modes are not generated by this helper. Never print the
generated `http.env` or signed key files into chat, diagnostics or a
shared workdir.

After successful configuration, use the component `install` and `start`
actions below (skip their manual `prepare` and `configure` examples).
For Bridge-backed Core instances start the Bridge first, then each Core.
Do not register unused connectors.

## Operator command surface (manual configuration and lifecycle)

In PowerShell, set `$ctl` to the existing trusted installed controller
python.exe path, and define the new unique instance ID and intended private
configuration paths. These are operator-supplied values, not fixture paths.

```powershell
$instance = 'my-core'
# Instance storage is derived from the current user's private LocalAppData.

# Prepare a file-only instance; no Bridge or Tunnel required.
& $ctl -I -B -m serverfs_mcp.windows_v25.cli core --instance $instance prepare

# STOP HERE until the offline runtime installer and private inputs are ready.
# $config: absolute serverfs.toml path
# $auth: absolute private HTTP environment file path
# $port / $hostname: unique Core loopback port and configured origin identity
& $ctl -I -B -m serverfs_mcp.windows_v25.cli core --instance $instance configure --config $config --auth-file $auth --port $port --hostname $hostname
& $ctl -I -B -m serverfs_mcp.windows_v25.cli core --instance $instance install
& $ctl -I -B -m serverfs_mcp.windows_v25.cli core --instance $instance status
& $ctl -I -B -m serverfs_mcp.windows_v25.cli core --instance $instance start
& $ctl -I -B -m serverfs_mcp.windows_v25.cli core --instance $instance status
```

Check `$LASTEXITCODE` after every invocation and stop on any nonzero exit.
A file-only configuration disables Agent delegation AND Repo Ops in every
workdir; the absence of a running Bridge is not itself a file-only policy.
Use `serverfs.toml.example` for the native workdir schema. A private HTTP env
must select exactly one authentication mode: HTTP bearer API key or configured
OAuth. For v25 the exact Host allowlist comprises the configured hostname
and `127.0.0.1:<port>`. Do not disable HTTP authentication. The HTTP listener
is loopback-only; direct local MCP does not imply a public network listener.
Any external reverse proxy is an independently managed deployment concern.

## Bridge-enabled Core

For a named owned Bridge, use the same ID as the Core; for a shared
Bridge, use a distinct nondefault ID. Prepare the Bridge component first.
Install its owner and worker environments; provision a private bridge.json
with the exact instance resources from `WindowsBridgeResources`, explicit
Windows `core_grants_enabled: true`, the current SID, allowed peer SID and
matching workdir policy. The worker's grant writer is
`serverfs_agent_bridge.core_grants_private`; policy revisions must be obtained
from the actual installed Bridge policy using
`core_workdir_policy_digest`. The shared-owner issuance helper is
`windows_v25.shared_core_grants.issue_shared_core_capabilities`.
Do not reuse another Core's secret or print returned secret-bearing objects.

Provision the Bridge's protected grant document (including signed health
capability) and each Core's protected binding before configuration/activation.
The Core binding must match its instance ID, bridge_mode, bridge_id, key ID,
private secret and exact workdir policy revisions. For named Bridge owners,
the private config and worker paths are fixed to the owner layout above.
Start Bridge before a Bridge-dependent smoke test. Do not reuse a legacy
default Bridge's SID-global pipe or state.

```powershell
& $ctl -I -B -m serverfs_mcp.windows_v25.cli bridge --instance $bridgeId prepare
# After private venvs, config, grants and bindings have been provisioned:
& $ctl -I -B -m serverfs_mcp.windows_v25.cli bridge --instance $bridgeId configure --config $bridgeConfig --bridge-python $workerPython
& $ctl -I -B -m serverfs_mcp.windows_v25.cli bridge --instance $bridgeId install
& $ctl -I -B -m serverfs_mcp.windows_v25.cli bridge --instance $bridgeId start
```

Configure and install the associated Core only once the Bridge manifest and
its verified binding are in place. For fresh named instances the initial
private configuration helper above performs the signed provisioning and
manifest publication; the lifecycle CLI remains independent. Existing
I-series fixture scripts are test evidence, not general-purpose installers.

## Optional ingress

After configuring the named Core, install each required Connector component
runtime once with `install-v25-runtime.py`. Multiple Connector IDs of the
same provider and instance share that component runtime. Every Connector
has its own private home, credentials, manifest and Scheduler task.

Use `configure-v25-connector.py` for each fresh Connector ID. Inputs are
absolute paths to trusted, privately owned credential files and Windows
executables with independently verified SHA256 pins. Never put credentials
themselves in the command line. The tool copies private inputs, inherits
the exact configured Core hostname and port, and publishes only the
manifest: it does not register or start a task.

```powershell
& $ctl -I -B .\deployment\windows\configure-v25-connector.py --instance $instance --connector-id edge-cf --component cloudflare --cloudflared $cloudflared --cloudflared-sha256 $cfSha --credential-file $tokenFile
& $ctl -I -B -m serverfs_mcp.windows_v25.cli cloudflare --instance $instance --connector-id edge-cf install
# Only after the external Tunnel route and Core readiness are verified:
& $ctl -I -B -m serverfs_mcp.windows_v25.cli cloudflare --instance $instance --connector-id edge-cf start
```

Repeat configuration for another fresh Connector ID with its own distinct
Tunnel. A Cloudflare token envelope is checked locally; this is not proof
of Cloudflare account ownership or route correctness. Duplicate identities
are refused for generic-helper audits, but legacy/manual Cloudflare
manifests have no such identity record. Check the reported
`manual_cloudflare_peers_unchecked` count and verify those remote Tunnel
identities independently before activation.

```powershell
& $ctl -I -B .\deployment\windows\configure-v25-connector.py --instance $instance --connector-id edge-oa --component openai --cloudflared $bundledCloudflared --cloudflared-sha256 $bundledSha --tunnel-client $tunnelClient --tunnel-client-sha256 $clientSha --credential-file $keyFile --tunnel-id $tunnelId
& $ctl -I -B -m serverfs_mcp.windows_v25.cli openai --instance $instance --connector-id edge-oa install
# Only after the external Tunnel and Core readiness are verified:
& $ctl -I -B -m serverfs_mcp.windows_v25.cli openai --instance $instance --connector-id edge-oa start
```

OpenAI requires the matching bundled cloudflared executable and a
Bearer-enabled Core. Its Core Authorization header is generated from the
actual private Core configuration, not supplied as a command-line secret.
Cloudflare and OpenAI external Tunnel provisioning, credential issuance,
route mapping and public endpoint verification remain operator concerns.
This helper is create-only, not a credential rotation or in-place upgrade
tool. Run provisioning serially; local collision checks are not a global
cross-process reservation. Failed partial homes are retained for diagnosis.

The manual lifecycle CLI prepare/configure path remains available for
operator-managed inputs. Connectors require the verified Core listener
before startup. Owner-alive status alone is not proof of public
reachability or authenticated MCP. Validate unauthorized/authorized HTTP
and real MCP initialization separately.

## Stop, restart, uninstall

Use the same component/instance/connector identity in every operation.
`stop` disables future starts before ending that exact task; `restart` affects
only that component. `uninstall` requires a verified disabled/offline task.
For retirement ordinarily stop ingress first, Core second and Bridge last
(only if no other Core uses it). Do not use legacy service stop/uninstall
as substitutes for v25 component commands. Do not manually terminate
processes by executable name or delete task names from another instance.

## Acceptance and limitations

See [refreshed installed Wheel native lifecycle and I17 acceptance](../../docs/diagnostics/v0250-windows-refreshed-native-lifecycle-2026-11-20.md)
and [ACP Job containment and refreshed release regression](../../docs/diagnostics/v0250-windows-acp-job-and-refreshed-release-2026-11-20.md).
The exact refreshed 0.25.0 Core/Bridge/native artifacts passed 17 native
supplemental cases and isolated controlled Bridge stop, fail-closed
Repo Ops while files remained available, independent Bridge restart,
abnormal Bridge owner exit and automatic Scheduler recovery with the
same Core process. Protected task state/generation and test cleanup
were verified. The default installed Core/native and Bridge suites
separately passed 1,864 and 772 cases, respectively (134 total skips).
The [refreshed public Cloudflare acceptance](../../docs/diagnostics/v0250-windows-refreshed-public-cloudflare-2026-11-20.md)
verified two independent real Cloudflare owners against a fresh file-only
Core on the exact refreshed Core/Native Wheels, with authenticated concurrent
public MCP, negative authentication, independent stop/restart, exact cleanup
and control-plane route restoration. Earlier generic provisioning and
upgrade/rollback proofs retain their original Wheel hashes.
The [refreshed I11 native isolation and OpenAI readiness](../../docs/diagnostics/v0250-windows-refreshed-i11-no-model-and-openai-readiness-2026-11-21.md)
gate verifies two independent Claude-capable owned pairs, signed discovery,
foreign credential denial and independent Bridge stop/restart without model
execution. Real-provider output fidelity remains open. Public OpenAI Tunnel
acceptance requires a separately verified nonproduction Tunnel identity,
private credential file and trusted binary pins; the configured inventory
contains only a production-associated identity and known synthetic fixture.
Other public topologies remain separate gates. These results are not a
production cutover.
A production rollout requires coordinated release integration, private
provisioning and an explicit cutover/rollback plan. Do not deploy this
candidate over established services merely because the test gates passed.
