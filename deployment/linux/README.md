# Linux Docker deployment

This is the v0.25.0 operator runbook for Linux amd64/arm64. Use one version-matched checkout and image set. It describes fresh named deployments; it is not an instruction to migrate an existing installation or to change its live Tunnel resources.

## 1. Choose the topology

Each HTTP Core is a separate Docker Compose project. Direct HTTP is a Core publication option, not a Connector. The optional OpenAI and Cloudflare Connector services can target that Core independently. The optional Agent Bridge runs under a host login user through a named systemd user unit; the Core's container does not contain Agent executables or provider credentials.

| Decision | Select |
| --- | --- |
| Base | `compose.yml` + `compose.http.yml` (always for this layout) |
| Direct HTTP | `deployment/linux/compose.publish-http.yml` |
| Internal-only HTTP | `deployment/linux/compose.internal.yml` |
| No Bridge | `deployment/linux/compose.bridge-disabled.yml` |
| Authorized host Bridge | `deployment/linux/compose.bridge-v2.yml` |
| Cloudflare | `compose.cloudflare.yml` + `deployment/linux/compose.cloudflare-profile.yml`, profile `cloudflare` |
| OpenAI | `deployment/linux/compose.openai-http.yml`, profile `openai-http` |

Choose exactly one exposure overlay and one Bridge overlay. Append the OpenAI HTTP overlay *after* the Cloudflare overlays when using both providers. The supplied layout has one service per provider per project; it is not a per-Connector-ID native CLI. With no Tunnel profile and a published port, no Tunnel credential is required. With neither Tunnel nor published port, only explicitly attached networks can reach the Core.

The publication defaults to host loopback. It adds a non-internal ingress network because Docker may omit host publication when all attached networks are internal. Do not treat a published Core as having the same egress isolation as an internal-only Core. Reverse proxy, TLS certificates, external Tunnel resources and DNS remain operator-managed.

## 2. Deploy a new file-only Core

Prerequisites: Docker, Compose v2.24+ (for the internal overlay's reset tag), an actual version-matched ServerFS image or trusted locally built image, and an existing directory the configured nonroot container UID/GID can read. The checkout's `.empty/.serverfs-disabled` placeholder must be present for unused workdir slots. Do not publish the default example's `/srv/projects` automatically.

Create a private environment file (mode 0600) outside exposed workdirs and Git, using `.env.example` only as a starting reference. Do not overwrite an existing configuration. For a fresh read-only local example, the relevant settings are below. Replace example paths/UID/GID and the image tag with real verified values; a candidate build must use its actual scratch tag, not an assumed published v0.25.0 image. The example placeholder for the API key must be replaced locally with a unique high-entropy secret before use.

```dotenv
SERVERFS_IMAGE=<verified-version-matched-image>
SERVERFS_UID=1000
SERVERFS_GID=1000
SERVERFS_HTTP_HOST=0.0.0.0
SERVERFS_HTTP_PORT=8000
SERVERFS_HTTP_PATH=/mcp
SERVERFS_HTTP_PUBLISH_HOST=127.0.0.1
SERVERFS_HTTP_PUBLISH_PORT=18080
SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp:8000,127.0.0.1:18080
SERVERFS_HTTP_ALLOWED_ORIGINS=
SERVERFS_HTTP_API_KEY=<generate-a-unique-secret-locally>
SERVERFS_OAUTH_ENABLED=false
WORKDIR_01_ALIAS=docs
WORKDIR_01_PATH=/srv/serverfs-blue/docs
WORKDIR_01_READ_ONLY=true
SERVERFS_AGENT_BRIDGE_ENABLED=false
SERVERFS_AGENT_MODE=disabled
SERVERFS_AGENT_RUNTIMES=
WORKDIR_01_AGENT_MODE=disabled
WORKDIR_01_REPO_OPS_ENABLED=false
CONTROL_PLANE_API_KEY=
CONTROL_PLANE_TUNNEL_ID=
```

The container listens on port 8000 in this example; port 18080 is the host publication. List exact actual Host authorities, including any reverse proxy or public route; do not disable Host or Origin protection. Empty Origin allowlist rejects non-empty Origin headers. Leave unused slots' alias and path empty and their read-only flag true.

In Bash from the checkout root, retain the exact Compose argument array for every operation on this deployment:

```bash
ENV_FILE=/absolute/private/blue.env
core=(docker compose -p serverfs-blue --env-file "$ENV_FILE"
  -f compose.yml -f compose.http.yml
  -f deployment/linux/compose.publish-http.yml
  -f deployment/linux/compose.bridge-disabled.yml)

"${core[@]}" config --quiet
# For a published image not already present:
"${core[@]}" pull serverfs-mcp
"${core[@]}" up -d --no-deps --no-build serverfs-mcp
"${core[@]}" ps serverfs-mcp
```

Skip the pull when using an already built local image. If building from source, select a unique scratch image tag in the private env and build that image instead of pulling a published release. Never overwrite a published tag with a local build. Check every exit code; stop on error. Do not print expanded Compose configuration because it may contain credentials.

The healthcheck proves the listener accepts TCP connections; it does not prove authenticated MCP. Configure a Streamable HTTP client with `http://127.0.0.1:18080/mcp` and the private bearer key. Verify initialize, `tools/list` (11 tools with all optional capabilities disabled), `list_workdirs`, and a safe read of the intended workdir. Verify missing/wrong credential denial and rejected mutation in the read-only workdir. Use a new session for each transport test. No Agent task or Tunnel is needed.

For an internal-only deployment replace the publication overlay with the internal overlay. Test from a client on an authorized network. For OAuth use [the OAuth guide](../../docs/oauth.md) and the separate auth service; do not apply the static bearer example unchanged. For binary transfer/file ingress, enable the appropriate policies and optional sidecar independently. See [configuration](https://ntlx.github.io/ServerFS_MCP/docs/configuration/) and [API Key](../../docs/api-key.md).

## 3. Add or operate a Connector

Only after Core readiness, choose the exact external resource. Never reuse a production Tunnel or token for an isolated new instance. Keep private credential files outside exposed roots. When a deployment chooses new overlays, use the full final array for all subsequent operations; append only the new selection to the existing array, preserving every previously enabled auth, file-ingress and Bridge option. A change to Core settings (such as Host admission) may require a separate deliberate Core update before the Connector can work.

### Cloudflare

Configure an externally managed Tunnel with origin `http://serverfs-mcp:8000` (using the actual container port); ensure the public Host is admitted by the Core. Configure `CLOUDFLARE_TUNNEL_TOKEN_FILE` with the absolute path of a private 0600 file readable by the configured `CLOUDFLARED_UID/GID`. Select an independently verified `CLOUDFLARED_IMAGE`. The token authenticates cloudflared, not the MCP caller.

For the file-only direct-HTTP example above, select this array (for tunnel-only, use the internal-only exposure overlay instead):

```bash
cf=("${core[@]}" -f compose.cloudflare.yml
  -f deployment/linux/compose.cloudflare-profile.yml --profile cloudflare)
"${cf[@]}" config --quiet
"${cf[@]}" pull cloudflared
"${cf[@]}" up -d --no-deps --no-build cloudflared
"${cf[@]}" ps cloudflared
```

See [Cloudflare Connector](../../docs/cloudflare-tunnel.md) for public route and admission verification.

### OpenAI HTTP

The named HTTP path uses the actual `tunnel-client` with HTTP upstream mode. Select a verified version of `OPENAI_TUNNEL_IMAGE`; ensure it supports the file-based key and extra/discovery headers used by the overlay. Configure `CONTROL_PLANE_TUNNEL_ID`, `OPENAI_CONTROL_PLANE_KEY_FILE` and `SERVERFS_CORE_BEARER_FILE`. The latter two are distinct private external files. The Core bearer file contains the exact single-line `Bearer <Core API key>` header value, with no trailing newline. This path requires a static Core bearer key, not OAuth-only admission. Allow the actual upstream authority `serverfs-mcp:<HTTP port>` in the Core's Host allowlist.

```bash
oa=("${core[@]}" -f deployment/linux/compose.openai-http.yml
  --profile openai-http)
"${oa[@]}" config --quiet
"${oa[@]}" pull openai-tunnel
"${oa[@]}" up -d --no-deps --no-build openai-tunnel
"${oa[@]}" ps openai-tunnel
"${oa[@]}" exec openai-tunnel /usr/bin/tunnel-client health --port 8080 --require-control-plane-poll --json
```

For both providers, combine the base selection and both provider overlays, with the OpenAI overlay last; select both profiles. Use the complete final array for status, logs, start/stop and verification. Public registration and discovery must be checked separately; local upstream readiness is not public acceptance.

### Lifecycle and verification

Check external registration, DNS/TLS and exact route, then authenticated MCP initialize, tools/list and file access from the intended client. Test denied admission and separately record long-lived SSE behavior when required. Use `stop <selected-service>` and `up -d --no-deps --no-build <selected-service>` (after ensuring the intended image/config) to operate only the Connector. Verify unchanged unrelated Core/Bridge/Connector identities and establish a fresh authorized session after restart. Do not assume old-session SSE replay. No generic `up -d`, `down` or whole-project restart as a Connector-only action.

## 4. Optional host Bridge: owned or shared

A file-only Core needs no host Bridge or provider credentials. A dedicated owner or a shared owner uses the same signed attachment mechanism. Linux provisioning requires distinct non-default Core and Bridge IDs, including a dedicated owner; this differs from native macOS/Windows owned naming. Ownership is established by the deployment and explicit grants, not a magic Compose project name. A shared Core never operates its owner.

Only one Agent/Repo Ops-enabled workdir may attach through the supplied signed overlay, mapped to slot 01. Additional disjoint file-only roots are possible. Each Core receives its own grant and binding; independent Bridge owners must not claim overlapping writable roots. The provisioner is create-only, not live grant append or rotation.

### Prepare the owner policy

The Bridge config is private JSON, parsed by the installed Bridge version. Prepare an alias-keyed policy (`lease_key=alias`, no `slot` property) with `protocol_mode=v2`, the requested Bridge ID, exact canonical host root, actual owner UID/GID, fixed instance paths and explicit Agent/Repo Ops choices. Do not copy the standalone fake-runtime example into an operational owner. Provider credentials remain in the host-side native runtime environment.

For example, a read-only Repo Ops inspection policy (no Agent provider) can use the following structure. The example assumes the login user is `operator` with UID/GID 1000 and a real local Git repository at the indicated root. Substitute the actual canonical paths and numeric IDs.

```json
{
  "bridge_id": "bridge-blue",
  "protocol_mode": "v2",
  "lease_key": "alias",
  "socket_path": "/home/operator/.config/serverfs-agent-bridge/instances/bridge-blue/bridge/run/bridge.sock",
  "state_dir": "/home/operator/.config/serverfs-agent-bridge/instances/bridge-blue/bridge/state",
  "lock_dir": "/home/operator/.config/serverfs-agent-bridge/instances/bridge-blue/bridge/locks",
  "allowed_peer_uid": 1000,
  "allowed_peer_gid": 1000,
  "enable_fake_runtime": false,
  "workdirs": [
    {
      "alias": "repo",
      "host_path": "/srv/serverfs-blue/repo",
      "read_only": true,
      "agent_mode": "disabled",
      "agent_runtimes": [],
      "repo_ops_enabled": true
    }
  ]
}
```

Store it as an owner-private file, e.g. `/absolute/private/bridge-policy.json`. Prepare the separate nonsecret manifest with at least one explicit Core method list; for this example grant only health and repository inspection. Other methods are not implied by enabling Repo Ops.

```json
{
  "bridge_id": "bridge-blue",
  "root": "/home/operator/.config/serverfs-agent-bridge/instances/bridge-blue",
  "bridge_config": "/absolute/private/bridge-policy.json",
  "expected_uid": 1000,
  "expected_gid": 1000,
  "cores": [
    {
      "instance_id": "core-blue",
      "alias": "repo",
      "container_path": "/workdirs/01",
      "host_path": "/srv/serverfs-blue/repo",
      "methods": ["bridge.ping", "repo.inspect"]
    }
  ],
  "existing_writable_roots": []
}
```

The empty existing-roots list is appropriate only for a host with no other declared writable roots. Inventory existing deployments first; fill in actual paths. Add further Core entries only when their complete policies and method scopes are reviewed. An Agent-enabled owner additionally needs explicit provider/runtime policy and corresponding signed task/runtime methods; use [Agent Bridge](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/) and [Repo Ops](https://ntlx.github.io/ServerFS_MCP/docs/repo-ops/) for the relevant capability contract.

### Provision the owner and signed bindings

From the checkout root use the locked Bridge environment and a private absolute manifest path. The instance parent must already be owner-owned mode 0700. The target root must not exist.

```bash
uv sync --project agent_bridge --frozen
uv run --project agent_bridge --no-sync python -m serverfs_agent_bridge.linux_v0250_provision /absolute/private/manifest.json
```

Provisioning creates one new owner-private Bridge and 1–32 initial Core bindings. The
destination is fixed to
`~/.config/serverfs-agent-bridge/instances/<bridge_id>/`, matching
`serverfs-agent-bridge@<bridge_id>.service`; prepare the `instances` parent as
current-user-owned mode 0700 first. The manifest is a nonsecret JSON file and
contains exactly: `bridge_id`, `root`, `bridge_config`, `expected_uid`,
`expected_gid`, exactly one of legacy `core` or `cores`, and
`existing_writable_roots`. Each Core entry has `instance_id`, `alias`,
`container_path`, `host_path`, and explicit `methods`. The trusted Bridge config
must already name the new root's `bridge/state`, `bridge/locks`, and
`bridge/run/bridge.sock`, use v2 and the requested Bridge ID, and define only
alias-keyed policies. Every policy must be assigned to at least one Core; each
canonical host bind source must exactly match its policy. Core mapping is fixed
to `/workdirs/01`, the mapping used by `compose.bridge-v2.yml`.

The command reads bounded regular manifest/config files (1 MiB maximum),
validates all policies and Core mappings, derives each digest from the parsed
policy, generates distinct local 32-byte secrets/key IDs, then atomically
publishes the complete new directory without replacement. Each Core gets a
private binding directory and independent grant; same-alias sharing uses the
same policy digest and alias lease while retaining per-Core task/grant identity.
Distinct aliases with overlapping roots are rejected. Workdirs may not overlap
the instances parent or the new private root, including when read-only. Declared
`existing_writable_roots` are checked for overlap, but are operator-supplied and
are not global deployment discovery. Grant and binding files are private;
secret values are never emitted. Symlinked paths, invalid IDs/mappings/methods,
duplicate JSON keys, unused policies, overlaps, and existing destinations are
rejected. Paths are checked against the Linux AF_UNIX maximum of 107 encoded
bytes (excluding terminating NUL) before publication. A signed attachment
requires an Agent- or Repo Ops-enabled policy; use the disabled Compose overlay
for pure file service. Neither the instance config nor application directory
may overlap an exposed workdir. It does not
install or enable a unit, edit a Compose project, or modify an existing service.

### Install and select a Bridge release

After provisioning, use the Linux-only release installer from the repository
root. The release label is chosen by the operator (use a distinct label for
each installed release, ideally including its version and commit). For example:

```sh
repo="$(pwd)"
uv_bin="$(command -v uv)"
uv run --project agent_bridge --no-sync python -m serverfs_agent_bridge.linux_v0250_install \
  stage bridge-blue v0.25.0-blue --source "$repo/agent_bridge" --uv "$uv_bin"
uv run --project agent_bridge --no-sync python -m serverfs_agent_bridge.linux_v0250_install \
  select bridge-blue v0.25.0-blue
uv run --project agent_bridge --no-sync python -m serverfs_agent_bridge.linux_v0250_install \
  install-template --source "$repo/deployment/linux/serverfs-agent-bridge@.service"
```

The stage operation copies only the selected source and lock metadata under
`~/.local/share/serverfs-agent-bridge/instances/<bridge_id>/releases/<label>`,
then installs with `uv sync --frozen --no-dev --no-editable`. Add `--offline`
when the required cache is already present. It checks the installed candidate
against the private Bridge config and grants and verifies its CLI before
writing a completion marker. Failed builds never replace the current pointer.
A completed release cannot be overwritten in place. The select operation
checks the candidate again and atomically replaces only the selected Bridge's
`current` symlink; it refuses a running, failed or ambiguous user unit.
The previous release directory is retained; rollback is explicit selection
of its original label. Stage, select and template publication never start,
stop, enable or reload services. The installer does not modify the legacy
singleton or its version pointer.

Template publication is idempotent for the same trusted content and refuses
an existing differing file. After explicit operator review, run
`systemctl --user daemon-reload` and start only
`serverfs-agent-bridge@<bridge_id>.service`. Verify the authenticated socket
before attaching Core; enablement at login is a separate operator choice.
For updates, stage first, stop the named instance (including any manually
launched process using its socket), select the new label, then start the same
unit. On failure, stop it, select the previous label, and start it again.
This is executable-version rollback, not database downgrade or configuration
rollback; check schema compatibility before changing releases. Keep provider
settings explicit per instance and preserve the same run, state and lock
directories across restart. Serialize manual systemctl/config changes with
the installer; its lock only serializes installer operations on that instance.
The installer does not copy provider credentials into the release or Core
mounts.

### Attach a Core

Start the owner separately and confirm its user-unit status, then configure the Core's fresh private env, `/absolute/private/core-blue.env` in this example. Use the same documented HTTP settings as the file-only example, with a distinct project (`serverfs-core-blue`), unused publication port and independent caller credentials. For the inspection-only example replace slot 01 and Bridge settings as follows (adjust actual paths and identity):

```dotenv
SERVERFS_UID=1000
SERVERFS_GID=1000
SERVERFS_AGENT_MODE=disabled
SERVERFS_AGENT_RUNTIMES=
SERVERFS_LINUX_INSTANCE_ID=core-blue
SERVERFS_LINUX_BRIDGE_ID=bridge-blue
SERVERFS_LINUX_BRIDGE_SOCKET_DIR=/home/operator/.config/serverfs-agent-bridge/instances/bridge-blue/bridge/run
SERVERFS_LINUX_BRIDGE_LOCK_DIR=/home/operator/.config/serverfs-agent-bridge/instances/bridge-blue/bridge/locks
SERVERFS_LINUX_CORE_BINDING_DIR=/home/operator/.config/serverfs-agent-bridge/instances/bridge-blue/core-bindings/core-blue
WORKDIR_01_ALIAS=repo
WORKDIR_01_PATH=/srv/serverfs-blue/repo
WORKDIR_01_READ_ONLY=true
WORKDIR_01_AGENT_MODE=disabled
WORKDIR_01_AGENT_RUNTIMES=
WORKDIR_01_REPO_OPS_ENABLED=true
```

Select `deployment/linux/compose.bridge-v2.yml` *instead of* the disabled overlay. It explicitly enables the infrastructure and mounts only the three private directories read-only; it does not render host policy or copy provider credentials. Set the UID/GID to the actual nonroot owner and verify actual host/container peer identity; a nominal user mapping is not proof of the socket peer. The provisioned binding is fixed `core-bridge-binding.json` (0400 or 0600, one link); socket and lock directories must already exist; verify they belong to the correct owner. Keep global Agent defaults disabled and use slot-local policy. All unused placeholders remain read-only; additional active roots must be disjoint.

### Read-only signed attachment preflight

After provisioning and before starting an attached Core, validate the complete
resolved Compose model as the Bridge owner. In a Bash shell from the repository
root, with the exact selected files and profiles (example direct HTTP):

```bash
set -o pipefail
attached=(docker compose -p serverfs-core-blue
  --env-file /absolute/private/core-blue.env
  -f compose.yml -f compose.http.yml
  -f deployment/linux/compose.publish-http.yml
  -f deployment/linux/compose.bridge-v2.yml)
"${attached[@]}" config --format json |
  uv run --project agent_bridge --no-sync python -m serverfs_agent_bridge.linux_v0250_preflight \
    --bridge-id bridge-blue --core-id core-blue --project serverfs-core-blue
```

Keep this pipeline private: do not print, tee, upload, or log the resolved model
(which can contain HTTP and connector credentials). The validator prints only
a pass marker or fixed refusal stage; it does not write files or open the task
database. Use matching explicit IDs and the same project, env file and overlays
for the subsequent deployment.

The check compares the actual bind sources and modes, owner UID/GID, exact
Bridge/Core IDs and socket/lock/binding mounts, private grant and policy
digest, slot-01 alias, read-only policy, runtime set and Repo Ops flag. It rejects
privileged/container-namespace overrides, unexpected mounts, additional Agent/
Repo-enabled slots, writable disabled placeholders, overlapping active roots
and mounts exposing the local Bridge's private directories. Extra disjoint
file-only workdirs are permitted. Global Agent defaults must stay disabled;
specify the granted policy in slot 01. This is a deliberately static check of
the supported overlay layout, not an assertion of daemon UID mapping, socket
liveness, other projects' mounts, or protection against concurrent edits.
Keep independent writable roots disjoint across all deployments and run the
signed Ping/runtime checks after startup. Verify the actual bind sources exist.

Only after the preflight succeeds and the intended image is available, start the Core with `"${attached[@]}" up -d --no-deps --no-build serverfs-mcp` and inspect `"${attached[@]}" ps serverfs-mcp`. Check file MCP independently of Bridge readiness, then signed Agent/Repo Ops discovery and owner isolation. In the inspection-only example, verify `inspect_repo` succeeds for the authorized alias and that repository mutation methods and Agent delegation are not authorized. Keep Bridge state, socket and locks private and preserve the owner lifecycle when operating a Shared Core. A Bridge outage does not authorize requests or bypass leases.

## 5. Operation, update and retirement

Keep each Core's complete Compose selection, private env, image pin and host Bridge identity as deployment records. Diagnose by component: Core TCP and authenticated MCP first, external Connector registration/route next, signed Bridge attachment and runtime discovery last. Check actual task and writer-lease state before any Bridge update or stop.

- Core: use the exact project/overlays to render and operate `serverfs-mcp`. A Core restart terminates its active transport sessions; reinitialize. Do not restart its owner Bridge as a side effect.
- Connector: operate only its selected service with the full deployment selection. After an authorized change verify public MCP and unrelated identities.
- Bridge: stage/select versioned owner releases and use the named user unit. The same private state is retained across restart; check schema compatibility and use documented executable rollback. Never clear active locks or recovery guards to force progress. For shared ownership, determine every attached Core and coordinate with the actual owner before disrupting service.

For retirement, stop the selected ingress first, then its Core. Remove only the intended disabled resources after verifying scope. Stop or retire a Bridge only with owner authorization and after its attached Cores and active work are accounted for. Do not issue a whole-project removal while unrelated components must remain available. Retain private state and credentials according to an explicit retention plan; no implicit deletion or global cleanup.

## Compatibility and evidence

The previous single-owner `compose.agent.yml` / `deployment/agent-bridge/install.sh` path remains a compatibility option for existing deployments; it is not the named signed-Bridge installation path. Do not switch an existing deployment implicitly. Historical Linux acceptance, detailed test recipes and earlier source milestones are preserved in the [integration baseline runbook](https://github.com/NTLx/ServerFS_MCP/blob/fffc0adb8341edb30ac7b03e72a017ca95b8c83d/deployment/linux/README.md). The [release checklist](../../docs/v0.25.0-release-checklist-2026-11-21.md) records outstanding exact-artifact and public acceptance.

For live multi-Core acceptance evidence and its limits, consult the preserved integration-baseline record above and the current release checklist. Revalidate the actual Linux v2 deployment attachment, host/container identity and selected artifacts before making a release claim.
