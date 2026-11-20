# Linux v0.25.0 Compose ingress source milestone

This Linux-only source layout composes the existing Core definition with
optional ingress overlays. It does not change or migrate an existing Compose
project. Run commands from the repository root and use an isolated private env
file such as `/absolute/path/to/new-instance.env`; do not reuse the existing
production `.env`. Give the new project a unique name, use host ports that do
not collide with existing instances, and configure distinct workdir roots.

The common files are `compose.yml` plus `compose.http.yml`. For published HTTP,
add `deployment/linux/compose.publish-http.yml`, which attaches a dedicated
non-internal per-project ingress bridge without changing the existing internal
connector networks. Docker may omit host port mappings when Core is attached
only to internal networks. The extra bridge permits normal Docker network
egress; select it only for host publication. Published HTTP binds to
`127.0.0.1` by default; explicitly set `SERVERFS_HTTP_PUBLISH_HOST` for wider
access. For an internal-only Core or tunnel-only deployment, instead add
`deployment/linux/compose.internal.yml` (Compose v2.24+). Do not combine the
publish and internal overlays. Include `compose.cloudflare.yml` plus
`deployment/linux/compose.cloudflare-profile.yml` and select `--profile
cloudflare` only when enabling Cloudflare. Include
`deployment/linux/compose.openai-http.yml` and select `--profile openai-http`
only when enabling OpenAI direct HTTP. The profiles can be selected together.
The root `openai-tunnel` service is profile-gated by `compose.http.yml`, so its
legacy default does not start.

The common Core/direct-HTTP selection is:

```sh
docker compose -p serverfs-v0250 --env-file /absolute/path/to/new-instance.env \
  -f compose.yml -f compose.http.yml \
  -f deployment/linux/compose.publish-http.yml \
  -f deployment/linux/compose.bridge-disabled.yml config --quiet
```

Internal-only:

```sh
docker compose -p serverfs-v0250 --env-file /absolute/path/to/new-instance.env \
  -f compose.yml -f compose.http.yml \
  -f deployment/linux/compose.internal.yml \
  -f deployment/linux/compose.bridge-disabled.yml config --quiet
```

The OpenAI connector uses the explicit `--mcp.server-url` HTTP mode, following
the [v0.0.17 configuration reference](https://github.com/openai/tunnel-client/blob/v0.0.17/docs/configuration.md).
The legacy Linux proxy launcher forwards these arguments verbatim, preserving
the existing control-plane-only proxy setting. Its no-argument behavior is
unchanged. The container's health listener and health probe use the same
loopback address and fixed port 8080; no host port is published. Containers
have independent network namespaces, so the health port does not conflict
across projects. The CLI/file resolution and live tunnel behavior still
require isolated runtime acceptance.
Provide two separate external files: the control-plane API key, and the exact
single-line `Bearer <Core API key>` header value (no trailing newline). Keep
both outside the checkout and every exposed workdir, with owner-only access
readable by the connector's effective container identity. Both file paths and
the Tunnel ID are mandatory in this overlay: there is no /dev/null fallback.
Allow the upstream authority `serverfs-mcp:<HTTP port>` in the Core's exact
Host allowlist, together with the approved public/loopback authorities as needed.
Core-only/direct HTTP and Cloudflare-only do not require OpenAI control-plane
credentials because its overlay is absent from those selections. The OpenAI
overlay is included only when enabling that connector. Cloudflare requires
only its own external token file.

Signed-v2 attachment is selected explicitly by replacing the disabled overlay
in the command with `deployment/linux/compose.bridge-v2.yml`:

```sh
docker compose -p serverfs-v0250-blue --env-file /absolute/path/to/blue.env \
  -f compose.yml -f compose.http.yml \
  -f deployment/linux/compose.publish-http.yml \
  -f deployment/linux/compose.bridge-v2.yml config --quiet
```

Use one private env file per instance for non-secret IDs/host paths and normal
workdir policy settings. It must not contain grant credentials.

### Read-only signed attachment preflight

After provisioning and before starting an attached Core, validate the complete
resolved Compose model as the Bridge owner. In a Bash shell from the repository
root, with the exact selected files and profiles (example direct HTTP):

```sh
set -o pipefail
docker compose -p serverfs-v0250-blue --env-file /absolute/path/to/blue.env \
  -f compose.yml -f compose.http.yml \
  -f deployment/linux/compose.publish-http.yml \
  -f deployment/linux/compose.bridge-v2.yml config --format json |
  uv run --project agent_bridge --no-sync python -m serverfs_agent_bridge.linux_v0250_preflight \
    --bridge-id bridge-blue --core-id core-blue --project serverfs-v0250-blue
```

Keep this pipeline private: do not print, tee, upload, or log the resolved model
(which can contain HTTP and connector credentials). The validator prints only
a pass marker or fixed refusal stage; it does not write files or open the task
database. Use matching explicit IDs and the same project, env file and overlays
for the subsequent deployment.

The check compares the actual bind sources and modes, owner UID/GID, exact
Bridge/ Core IDs and socket/lock/binding mounts, private grant and policy
digest, slot-01 alias, read-only policy, runtime set and Repo Ops flag. It rejects
privileged/container-namespace overrides, unexpected mounts, additional Agent/
Repo-enabled slots, writable disabled placeholders, overlapping active roots
and mounts exposing the local Bridge's private directories. Extra disjoint
file-only workdirs are permitted. Global Agent defaults must stay disabled;
specify the granted policy in slot 01. This is a deliberately static check of
the supported overlay layout, not an assertion of daemon UID mapping, socket
liveness, other projects' mounts, or protection against concurrent edits.
Keep independent writable roots disjoint across all deployments and run the
signed Ping/runtime checks after startup. Do not treat an omitted false-valued
bind option in Compose's normalized JSON as an actual mount existence check;
the validator verifies that each bind source already exists.

VPS verification: the provisioning test module passed 49 tests with one
explicitly opt-in systemd case skipped; the root Bridge deployment gate
passed five tests. The preflight cases used actual Docker Compose JSON
rendering with synthetic credentials and private provisioned fixtures.
They covered the passing model, unchanged private-tree file sizes/mtimes,
configuration drift, a mismatched grant secret, and a disjoint file-only
slot. Ruff and Git diff checks passed. This did not start containers or
revalidate the live Connector/provider gates.

`deployment/linux/compose.bridge-disabled.yml` makes the **default activation**
file-only (no Bridge) and mounts no socket. When Agent capabilities are explicitly
enabled, the preferred Linux Bridge implementation is host-native, not a Bridge
container. Core operation with `bridge_mode=disabled` needs
no host Bridge, socket, Agent runtime or Agent credentials. `owned` means one
Bridge lifecycle and state tree belong to exactly one Core; `shared` means
multiple Cores use a separately owned Bridge and requires per-Core authorization.
The optional `deployment/linux/compose.bridge-v2.yml` overlay attaches one
operator-configured host Bridge to a non-default Core through signed v2 startup
binding. It is separate from `compose.agent.yml`; Agent and Repo Ops remain
independently enabled through normal per-workdir environment variables. The
overlay explicitly passes global `SERVERFS_AGENT_MODE`/`SERVERFS_AGENT_RUNTIMES`
and workdir 01's `WORKDIR_01_AGENT_MODE`, `WORKDIR_01_AGENT_RUNTIMES`, and
`WORKDIR_01_REPO_OPS_ENABLED` into the Core container. The binding authorizes
exactly one enabled workdir, which must be workdir 01 for this overlay.

Before rendering, explicitly set `SERVERFS_UID` and `SERVERFS_GID` to the
nonroot host Bridge owner's UID/GID (`id -u` and `id -g` on the host). This
overlay rejects absent values; the legacy file-only Compose default is unchanged.
Create a private host binding directory owned by that UID, mode 0700,
containing the owner-provisioned
fixed `core-bridge-binding.json` (mode 0400 or 0600, one link). Set
`SERVERFS_LINUX_CORE_BINDING_DIR` to this directory. Set
`SERVERFS_LINUX_BRIDGE_SOCKET_DIR` and `SERVERFS_LINUX_BRIDGE_LOCK_DIR` to
dedicated existing host directories. All three mounts are read-only and use
`create_host_path: false`. The socket directory must contain `bridge.sock`;
the binding must match that socket, `SERVERFS_LINUX_INSTANCE_ID`, and
`SERVERFS_LINUX_BRIDGE_ID`. Required values fail Compose rendering when absent.
The binding directory is mounted at `/run/serverfs-core-bridge-binding`, with
the fixed binding file path supplied to Core. No grant credential is embedded
in Compose or an env file. The overlay does not install/start a Bridge, Agent
runtime or tunnel.

The signed-v2 Core runs as the explicitly selected nonroot
`${SERVERFS_UID}:${SERVERFS_GID}` identity. The host must observe the same peer
UID/GID on the socket; user namespace mappings must be verified, not assumed. Do not add a Docker
socket or privileged mode. Each instance needs a unique Compose project, port,
instance/Bridge IDs, socket/lock/state directories, and policy/workdir roots.
BridgeConfig supports independent `socket_path`, `lock_dir`, and `state_dir`;
provision one Bridge config and state root per owned instance. Writable workdir
roots assigned to different Bridges must not overlap because same-user
ownership does not provide cross-Bridge write/lease coordination. Never treat
same-user v1 shared mode as acceptable.

The established Linux single-Core topology uses RPC v1 within its measured
same-user trust boundary. That does not authorize multiple Cores to share a v1
socket: shared mode remains gated, and there is no fallback to v1. Linux v2
Core startup IPC and deployment attachment are source-verified; the signed
`CoreBridgeClientV2` was validated against temporary AF_UNIX sockets with
synthetic Core grants, identity/policy denials, peer checks and durable replay
checks. When
`SERVERFS_LINUX_V25_BRIDGE_BINDING_FILE` points to an owner-provisioned
`core-bridge-binding.json`, Linux Core opens that file without following any
path component, requires its parent directory to be current-user-owned mode
0700, and requires the regular file to be current-user-owned mode 0400/0600
with one link and bounded exact-schema JSON. The grant binds one non-default
`SERVERFS_LINUX_INSTANCE_ID`, `SERVERFS_LINUX_BRIDGE_ID`, configured socket,
and exactly one Agent/Repo-enabled workdir alias/root. Missing, invalid,
mismatched or disabled bindings abort startup. A bounded signed `bridge.ping`
probe rejects explicit authorization failures and invalid responses; transport
unavailability instead emits `linux_bridge_unavailable_at_startup` and allows
Core HTTP/file reads to start independently. Agent/Repo requests still require
signed Bridge authorization, and file mutations still require the existing
alias lease and recovery-guard checks. There is no v1 fallback. The
environment contains only the private file path and non-secret identities;
grant credentials remain in the file. The setting is Linux-only and unset by
default. The overlay supplies source-only attachment wiring but does not
provision grants or configure/start the host Bridge. The installed Linux user
unit and installer still manage one fixed config/socket/lock/state root. The
optional `deployment/linux/serverfs-agent-bridge@.service` user-unit template
invokes the existing installed executable with an instance-qualified config
path; operators must separately provision that config and unique socket/lock/
state/policy roots before explicitly enabling the unit. The template selects its own executable under
`~/.local/share/serverfs-agent-bridge/instances/<bridge_id>/current/`, not the
legacy singleton's current version. The template does not install itself, copy
provider credentials, or select/start a runtime. Startup
IPC and Compose wiring do not establish owner provisioning, independent Bridge
lifecycle, isolated policies/leases/workdir roots, live Agent/Repo Ops, or live
multi-Core acceptance.

For source-level Compose isolation checks, use an empty temporary env file and
an allowlisted subprocess environment. Use a unique project (`-p`), loopback
port and workdir path per Core. For v2 render checks, use synthetic absolute
socket/lock/binding paths and non-default identities. Never use the repository
`.env`. Config checks do not establish live Bridge authorization, Agent
operation or multi-instance acceptance.

Connector services do not own Core lifecycle. Use the complete selected
overlay set, project name, private env file and applicable profile on every
command. Start or recreate just the named connector with
`up -d --no-deps --no-build <connector>`; stop it with `stop <connector>`.
Do not include `serverfs-mcp`, another connector, or the host Bridge in that
operation. The inherited depends_on readiness condition is useful for initial
combined startup; explicit `--no-deps` is required for connector-only up.
There are no fixed container names or published connector ports. No
`docker compose up` is part of source validation.

The native dual-Core HTTP smoke has verified MCP initialization, discovery,
file reads, fake Agent submit/get and cross-Core denial. It uses an injected
fixture registry root, not Docker mount paths; it does not establish live
Docker, Tunnel, real Agent, rollback or production acceptance.

The isolated file-only Docker acceptance additionally runs the actual
Bridge-disabled Core with a private temporary workdir and synthetic bearer
key. It verifies loopback direct HTTP authentication denial, MCP initialization
and tool discovery, file read and write. It then stops that container and
starts the same Core with `compose.internal.yml`, verifies all attached
networks are internal, performs MCP discovery/read from inside the Core
container, and confirms Docker created no host port bindings. The test mounts
the current source read-only over a cached runtime image. It uses a unique
Compose project; `finally` runs `down --remove-orphans` and asserts no
containers remain with that project label.

Reproduction from the repository root with the locally cached v0.24.2 image:

```sh
SERVERFS_LINUX_DOCKER_TEST_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.24.2 \
  uv run pytest tests/test_linux_v0250_file_only_docker.py -q
```

Result: `1 passed`. Linux v0.25 Compose rendering and file-only Docker
acceptance passed together; the full `tests/test_linux_v0250_*.py` regression
also passed. Ruff lint, Ruff format check and `git diff --check` passed. No
Tunnel connector was started, so connector stop/restart independence has not
been measured against a running fixture. Public Tunnel, provider behavior,
v0.25 built-image, Bridge-enabled and production acceptance remain separate
gates.

## Isolated Docker acceptance

The first derived-image Docker smoke failed at Core binding startup with the
old generic error. The opt-in test now separates in-container binding validation
from signed Ping and HTTP MCP acceptance. Startup errors include only a fixed
local stage (for example `private-directory`, `private-file`, `workdir-binding`,
`ping-unavailable`, or `ping-rejected`), not private paths, credentials, or
Bridge error text. Checks still fail closed without v1 fallback. The second
smoke passed the in-container signed Ping but failed host HTTP readiness; the
published-HTTP overlay addresses the internal-only network topology. A VPS
probe on a disposable internal network also reported that no public port was
published. After selecting the new overlay, the opt-in source-overlay Docker
E2E passed (`1 passed in 21.95s`). It asserted the actual published loopback
endpoint, signed Ping, authenticated MCP file/Agent calls, wrong-key denial,
real alias writer-lock contention and successful writes, Core-only restart,
Bridge listener-only restart with Core identity unchanged, file reads during
Bridge outage, and removal of the test project's containers. The fake Bridge
listener ran in the test process, not as an installed systemd service. This
is not a canonical release-image build, real Agent, two-container shared
Bridge, or public Tunnel acceptance.

## Isolated dual-connector lifecycle acceptance

The opt-in dual-connector Docker test selects both current Linux connector
overlays, then replaces the provider executables with two different local
stand-ins using the cached ServerFS runtime image: Cloudflare's service uses
the MCP SDK client, and OpenAI's service uses a standard-library JSON-RPC
client. Both authenticate to the disposable Core through Compose service DNS,
discover tools, and read a fixture file. Their actual service networks remain
`serverfs_internal` and `tunnel_egress`; the test-only overlay marks both
networks internal, preventing public egress. It uses synthetic credential
files and does not use public Tunnel/provider endpoints.

With the Core running internal-only, the test confirms the actual Docker
container has no host port bindings and inspects the project-labeled Docker
networks to verify both are internal. It then stops, starts with documented
`up -d --no-deps --no-build`, and force-recreates each connector in turn.
After every operation it compares the Core and sibling connector container ID
and `StartedAt`; the sibling must also log a later authenticated MCP tool call.
After stop/start, the target keeps the same container ID with a new `StartedAt`;
after forced recreation, it receives a new container ID. Both connectors must
log fresh authenticated calls after each operation.
The Bridge is explicitly disabled, so host Bridge listener identity/Ping is
outside this acceptance. The test's `finally` runs project-scoped Compose down
and asserts no containers or networks remain with the unique Compose project
label.

Reproduction from the repository root with the cached runtime image:

```sh
SERVERFS_LINUX_DOCKER_TEST_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.24.2 \
  uv run pytest tests/test_linux_v0250_dual_connector_lifecycle.py -q
```

Result: `1 passed`. The complete Linux v0.25 test group also passed with the
current source mounted over that cached image, including the existing signed
Bridge Docker cases:

```sh
SERVERFS_LINUX_DOCKER_TEST_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.24.2 \
SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE=1 \
  uv run pytest tests/test_linux_v0250_*.py -q
```

Ruff lint, Ruff format check and `git diff --check` passed. This validates
local connector process lifecycle and internal service-DNS transport only; it
does not validate real Cloudflare/OpenAI clients, external ingress, provider
credentials, public tunnels, Bridge lifecycle, production startup ordering,
or a v0.25 built image.

## Explicit real-Agent Docker acceptance

`tests/test_linux_v0250_real_agent_docker.py` is disabled unless both a local
image and `SERVERFS_LINUX_REAL_AGENT_ACCEPT=claude` are explicitly selected.
It uses the cached v0.24.2 runtime image with the current Core source mounted
read-only, one temporary Bridge config/grant/binding, one isolated workdir and
one Core. A separate host process runs the real Bridge CLI and Claude adapter;
the existing user service/unit is not stopped, changed or reused. Core startup
performs signed v2 Ping, and `list_agent_runtimes` confirms Claude availability
over the signed Bridge RPC before the single task is submitted through MCP.
The test polls `get_agent_task` until `succeeded` and asserts the exact synthetic
marker response plus unchanged fixture bytes and directory entries. No fake
adapter is installed in this path, and the task contains no project/company
data. The existing host Claude authentication/settings are used normally; the
test never reads, logs, writes or mounts provider credentials.

The task uses native Claude's required `workspace-write` profile and a
workdir configured writable for that contract. The prompt asks for a read-only
fixture lookup and the test confirms no changes occurred, but this is **not an
enforced read-only sandbox**. A pending approval/question is canceled without
approval and fails the test. The single-Core case does not test cross-Core task
visibility. The existing installed Bridge unit was observed active with the
same MainPID before and after the test.

Reproduction from the repository root, using the locally cached image and the
host user's current Claude authentication:

```sh
SERVERFS_LINUX_DOCKER_TEST_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.24.2 \
SERVERFS_LINUX_REAL_AGENT_ACCEPT=claude \
  uv run pytest tests/test_linux_v0250_real_agent_docker.py -q
```

Result: `1 passed`; the returned task was terminal `succeeded` and its final
response exactly matched the generated fixture marker. The focused coordinator
dual-connector test passed (`1 passed`). The full Linux v0.25 group, with
real-Agent opt-in explicitly empty and current Core source overlay enabled,
passed `59 passed, 1 skipped in 178.26s`:

```sh
SERVERFS_LINUX_DOCKER_TEST_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.24.2 \
SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE=1 \
SERVERFS_LINUX_REAL_AGENT_ACCEPT= \
  uv run pytest -o addopts='' tests/test_linux_v0250_*.py -q
```

The opt-in test stores synthetic grants and task state only under its private
pytest temp root. The runtime acceptance completed before automatic temp-root
cleanup was added; postflight found and removed that exact leftover scratch
directory. The test now registers root removal as a finalizer. Project-scoped
Docker cleanup assertions and postflight queries found no acceptance containers
or networks. No real Tunnel or existing user unit was touched. Ruff lint,
format and diff checks passed. This is one successful Claude smoke, not a
general provider guarantee, sustained session test, enforced sandbox proof,
cross-Core visibility proof, or production acceptance.

Run against a local candidate image that already exists, under a nonroot
operator on Linux with local Docker daemon access:

```sh
SERVERFS_LINUX_DOCKER_TEST_IMAGE=serverfs-mcp:dev \
  uv run --no-sync pytest -q tests/test_linux_v0250_docker_e2e.py
```

An offline developer fallback can use a compatible cached image and set
`SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE=1` to mount a temporary copy of the
current Core source read-only. This tests current source and real mount/IPC
behavior, not canonical release-image build or dependency packaging. The test
uses its own project, synthetic API key and grants, local fake runtime, and
loopback port, with cleanup restricted to its own project and fixture. It does
not pull or build images, install services, or start real Agents. The acceptance
path is opt-in; a default skipped test is not a runtime pass. The same test
is parameterized for one Core and two separately named Compose projects sharing
one Bridge. It includes independent HTTP credentials, per-Core workdir/grant
and task checks, read-only Repo Ops status against temporary Git repositories,
denial of ungranted commits, alias-lock independence, and Core restart while
the Bridge listener is absent. Both owned and shared parameters passed on the
cached source-overlay runtime. The shared case completed in 39.60 seconds;
actual UID/GID was 1000:1000, binding directory 0700, file 0600, socket 0660,
and lease directory 0750. A subsequent project-label check found no remaining
`serverfs-v25-test-*` containers. This result uses a fake runtime and listener
restart, not an installed systemd unit or canonical release-image build.

## Linux shared-Bridge v2 source acceptance

Two explicitly granted Core identities with separate workdir aliases and roots
have passed isolated Linux AF_UNIX v2 tests against one temporary Bridge using
only the fake runtime. Tests cover signed ping, task submission/lookup, cross-Core
task denial, method and alias denial, and nonce replay rejection after restart.
This IPC evidence is supplemented by the native two-Core HTTP smoke and
source-level multi-Core provisioning tests and actual two-container shared
Bridge E2E. Both containers retained separate HTTP credentials, workdirs,
leases and tasks through isolated lifecycle checks. The Bridge listener is a
temporary host test process; systemd lifecycle, real Agent execution and
public MCP E2E remain unverified.

## v0.25.0 Linux owned-Bridge status

The legacy installation remains a singleton. New instance-qualified
provisioning and signed attachment are separate from that legacy installer:

- `deployment/agent-bridge/serverfs-agent-bridge.service` is one fixed user
  service. It reads one fixed `~/.config/serverfs-agent-bridge/config.json`
  and the installer stages one fixed socket/lock root and one fixed state root.
  The legacy installer does not install the new instance template or provision
  its named configurations.
- BridgeConfig accepts independent absolute socket, lock and state paths, but
  does not bind those paths to an instance identity. RPC v1 carries no Core
  identity, and its peer check is only same-UID/GID authentication. The new
  Linux Core path can use signed v2 per-Core grants at startup and the v2 IPC
  endpoint. Both one- and two-container topologies passed isolated fake-runtime
  Bridge listener tests. A separate test now covers one installed release
  selection and rollback under a transient systemd user unit; persistent
  template-managed and simultaneous owned/shared service topologies remain
  unverified.
- `LeaseManager` uses the configured lock directory and stable workdir lease
  names. Initial provisioning creates separate socket, lock **and** state
  paths and checks explicitly declared workdir conflicts; it does not discover
  undeclared deployments.
- The established Linux `compose.agent.yml` overlay uses fixed in-container
  socket/lock paths. A workdir Bridge policy must name the host path backing that Core's
  `/workdirs/NN` mount. The existing Phase E renderer checks configured Core
  and peer uid/gid against the current host user. The new owner-side preflight
  below additionally checks the rendered Compose project, host/container
  mapping, private binding, grants and Bridge policy. It does not establish
  the identity of a live socket peer; signed runtime Ping does that.

The installed legacy unit remains a singleton, while the optional source unit
template supplies instance-qualified config selection only. Do not enable
`compose.agent.yml` for a new owned instance: it targets the singleton deployment's fixed Bridge roots. The new
`compose.bridge-v2.yml` uses signed attachment wiring; the operator must
provision a separate Bridge config and socket, lock and state roots. Do not
treat same-user v1 shared mode as acceptable. Initial owner provisioning is
source-tested; one installed release's transient systemd lifecycle is now
covered as recorded below. Persistent template-managed startup, simultaneous
independent Bridge instances, real Agent/Repo Ops, and
live multi-Core acceptance remain unverified. Low-level IPC, Compose rendering and
one-container fake-runtime tests do not satisfy those gates.

The source-gated tests in `tests/test_linux_v0250_bridge_gate.py` pin the
disabled default and deployment boundary. They are not runtime or multi-instance
acceptance evidence.

## Initial owner provisioning and lifecycle acceptance

`uv run python -m serverfs_agent_bridge.linux_v0250_provision MANIFEST.json`
from `agent_bridge/` creates one new owner-private Bridge and 1–32 Core bindings. The
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

The legacy single-`core` manifest remains supported. The `cores` array is
mutually exclusive with it and supports 1–32 unique nondefault Core IDs distinct
from the Bridge ID. Current Linux v2 Core and Bridge use the existing alias-based
writer lease identity, including when Core's Docker workdir is mapped from
legacy slot 01.
Independent Bridges must still use disjoint writable roots. Revocation is not
live: remove the Core entry through the established owner-private grant
provisioner and restart that Bridge to load the changed grant file. Existing
Core binding secrets are not automatically rotated, and this command does not
implement reauthorization or rotation.

### Explicit per-instance application and systemd deployment

After provisioning, use the Linux-only release installer from the repository
root. The release label is chosen by the operator (use a distinct label for
each candidate, ideally including its version and commit). For example:

```sh
repo="$(pwd)"
uv_bin="$(command -v uv)"
uv run --project agent_bridge --no-sync python -m serverfs_agent_bridge.linux_v0250_install \
  stage bridge-blue v0.25.0-candidate1 --source "$repo/agent_bridge" --uv "$uv_bin"
uv run --project agent_bridge --no-sync python -m serverfs_agent_bridge.linux_v0250_install \
  select bridge-blue v0.25.0-candidate1
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

An isolated CLI lifecycle regression uses a temporary owner home, actual
provisioned credentials and a fake provider. To additionally exercise a real
transient systemd user unit (not an installed template), run from
`agent_bridge/`:

```sh
SERVERFS_LINUX_SYSTEMD_TEST=1 uv run --no-sync pytest -q tests/test_linux_v0250_provision.py
```

The test never accesses production provider credentials or units. It checks
signed Ping, task completion, restart, persisted task lookup, directory inode
stability and cleanup. On the VPS, the default Bridge regression group passed
(34 passed, 2 skipped), including the actual subprocess CLI lifecycle test.
The opt-in transient systemd-user case also passed (1 passed, 25 deselected):
its assertions included a working user manager, distinct MainPID after restart,
unchanged socket directory inode, successful task lookup across the restart,
unit stop/inactive and socket removal. The root Bridge deployment gates passed
(5 passed). This evidence is for temporary provisioned configuration and a
fake runtime, not real provider execution. The pytest command did not
separately record a postflight unit inventory.

The release installer default regression passed 57 tests with two opt-in
cases skipped, and the root deployment gate passed five tests. To exercise a
real locked non-editable application installation, select one packaging mode
from `agent_bridge/`:

```sh
SERVERFS_LINUX_INSTALL_TEST=offline uv run --no-sync pytest -q tests/test_linux_v0250_provision.py -k actual_release_packaging
# Explicitly permit pinned public dependency downloads if the cache is incomplete:
SERVERFS_LINUX_INSTALL_TEST=online uv run --no-sync pytest -q tests/test_linux_v0250_provision.py -k actual_release_packaging
```

On the VPS the offline attempt failed because the uv cache lacked the locked
`claude-agent-sdk==0.2.156` wheel. A separate isolated package-sync reproduction
confirmed the cause. The online acceptance then passed (1 passed, 58
deselected in 9.77s). It exercised the actual installed non-editable CLI,
initial atomic release selection with the real user-manager stopped-unit
check, template publication to a temporary home, signed IPC and fake-runtime
task completion, graceful process stop and socket removal. Upgrade and
rollback pointer paths in that earlier test were exercised with controlled
subprocess fixtures. A separate two-release systemd acceptance is recorded
below. No production unit was installed or activated, no dependency lock was
changed, and real provider execution and public ingress remain separate gates.

### Two-release installed systemd selection and rollback acceptance

The additional opt-in test packages labels `fixture-release-a` and
`fixture-release-b` independently from the current `agent_bridge/` source with
the checked-in `uv.lock`; both labels intentionally contain the same source.
It therefore measures non-editable packaging, the installer's real atomic
`select`, and executable/persistent-state lifecycle rollback. It does not test
cross-version task-schema or database compatibility.

The test uses the fixture's private temporary HOME for provisioned config,
grants, release trees, and the Bridge process. It invokes the real
`serverfs-agent-bridge@<unique-id>.service` identity via a transient
`systemd --user` unit and supplies the selected release's absolute executable
and fixture config in that transient unit. This exercises the actual user
manager, unit identity, MainPID, executable selection, signed v2 socket IPC,
graceful stop/start, and task persistence. It deliberately does not install or
replace the persistent `serverfs-agent-bridge@.service` template, reload the
manager, enable a service, or prove template expansion/boot activation.

Run from `agent_bridge/`:

```sh
SERVERFS_LINUX_INSTALL_TEST=offline SERVERFS_LINUX_INSTALL_SYSTEMD_ACCEPT=1 \
  uv run --no-sync pytest -o addopts='' -s -q tests/test_linux_v0250_provision.py \
  -k actual_release_systemd_upgrade_rollback
SERVERFS_LINUX_INSTALL_TEST=online SERVERFS_LINUX_INSTALL_SYSTEMD_ACCEPT=1 \
  uv run --no-sync pytest -o addopts='' -s -q tests/test_linux_v0250_provision.py \
  -k actual_release_systemd_upgrade_rollback
```

The offline attempt stopped at installer stage `uv-sync` with the installer's
generic `InstallError` (`1 failed, 59 deselected in 0.93s`); subprocess
diagnostics are suppressed, so the missing offline artifact was not identified
in this run. The failed stage removed its reserved release directory and
created no systemd unit. The online retry used the locked dependencies and
passed on the final test revision:
`1 passed, 59 deselected in 20.94s`. Evidence
included signed Ping and deterministic fake task A under release A; selection
of B while A was active failed with `unit-not-stopped` and left the pointer on
A; after stopping A, B became the selected absolute `ExecStart` with a
different MainPID; B retrieved A's task and completed task B; rollback selected
the original A executable with another distinct MainPID and retrieved both
terminal task results. State-directory and socket-directory device/inode
identities remained unchanged across both transitions. Final stop required an
inactive unit with `MainPID=0` and removed the socket. The test reported fixture
`r29acf552` and unit `serverfs-agent-bridge@r29acf552.service`; postflight showed
that exact unit as `LoadState=not-found`, `ActiveState=inactive`, `MainPID=0`,
and absent from the user-unit inventory. The private HOME is managed by the
fixture's `TemporaryDirectory` teardown.

Read-only production baseline before and after acceptance:
`serverfs-agent-bridge.service` remained `loaded/active/running`,
`MainPID=2039243`, with fragment
`/home/lx/.config/systemd/user/serverfs-agent-bridge.service`. The acceptance
did not query or alter its configuration, and did not create an instance of
the installed template.

The read-only postflight queries were:

```sh
systemctl --user show serverfs-agent-bridge@r29acf552.service \
  -p LoadState -p ActiveState -p SubState -p MainPID --no-pager
systemctl --user show serverfs-agent-bridge.service \
  -p LoadState -p ActiveState -p SubState -p MainPID -p FragmentPath --no-pager
systemctl --user list-units --type=service --all --no-legend --plain
```

The first returned `LoadState=not-found`, `ActiveState=inactive`,
`SubState=dead`, `MainPID=0`. The production query returned
`LoadState=loaded`, `ActiveState=active`, `SubState=running`,
`MainPID=2039243`, and the fragment path above. The unit inventory contained
the legacy production service and no `serverfs-agent-bridge@*.service` instance.

Default Bridge regression command from `agent_bridge/`:

```sh
uv run --no-sync pytest -o addopts='' -q tests/test_linux_v0250_provision.py \
  tests/test_linux_v0250_core_grants_ipc.py tests/test_linux_v0250_two_core_isolation.py
```

Result: `61 passed, 3 skipped in 12.63s`. Root signed-client/Bridge gates also
passed (`29 passed in 2.14s`):

```sh
uv run pytest -o addopts='' -q tests/test_linux_v0250_bridge_adapter.py \
  tests/test_linux_v0250_bridge_bootstrap.py tests/test_linux_v0250_bridge_gate.py
```

Ruff and tracked-diff checks from the repository root were:

```sh
uv run --project agent_bridge --no-sync ruff check agent_bridge/tests/test_linux_v0250_provision.py
uv run --project agent_bridge --no-sync ruff format --check agent_bridge/tests/test_linux_v0250_provision.py
git diff --check
```

Ruff check, format check, and Git whitespace checks passed. The transient
process used only the fake runtime and synthetic grant; no provider, tunnel,
production credential, or production Bridge unit was used.

## Locally packaged Linux source candidate image

On 2026-11-20, the current dirty Linux branch was packaged with the root
`Dockerfile` as a local **source candidate**, not a final v0.25.0 release.
`pyproject.toml` remains at version `0.24.2`; neither package metadata nor lock
files were changed. The unique image tag is
`serverfs-mcp:linux-v0250-candidate-20261120-53112a212491`, image ID
`sha256:6a3692cff58a1a93a4364b3aa00c9816e5bfaacabc9b5c4c8d0354fe7596123c`
(`linux/amd64`, 159,357,699 bytes). The tag was verified absent before build
and remains locally available for later isolated ingress checks.

The standard Dockerfile build used a private temporary context with exactly
these top-level inputs: `Dockerfile`, `.dockerignore`, `pyproject.toml`,
`uv.lock`, `README.md`, `src/`, and `native/macos/service_manager/`. Its
recursive scan rejected symlinks and secret-like files. The context contained
241 files / 3,563,053 bytes, SHA-256
`f6f0222d1393619f0bedac81494cd56acf27f9b06dae940a9885b84391376626`; no
`.env`, `.git`, tests, workdirs, credentials, or unrelated repository content
was included. The temporary context was removed after the build.

The executed Docker argv was equivalent to:

```sh
docker --host unix:///var/run/docker.sock build \
  --tag serverfs-mcp:linux-v0250-candidate-20261120-53112a212491 \
  --file "$ALLOWLIST_CONTEXT/Dockerfile" "$ALLOWLIST_CONTEXT"
```

Docker 29.1.3 completed the root multi-stage Dockerfile build and pulled the
normal Python/uv base images and locked dependencies. The legacy builder emitted
its deprecation warning; the build still completed successfully. At the start,
`/var/lib/docker` had about 15 GB free and the host had 3.1 GiB available
memory. No prune, daemon configuration change, existing image retag, or push
was performed.

The packaged module probe ran in a disposable `--network none` container. It
reported package version `0.24.2`, origin
`/app/.venv/lib/python3.12/site-packages/serverfs_mcp/__init__.py`,
`linux_v0250_core_binding.py` and `linux_v0250_bridge_adapter.py` under that
same installed package directory, and `editable=false` in package metadata.

The probe command was:

```sh
docker --host unix:///var/run/docker.sock run --rm --network none \
  --entrypoint /app/.venv/bin/python \
  serverfs-mcp:linux-v0250-candidate-20261120-53112a212491 -c '
import importlib.metadata as m, importlib.util as u, json, pathlib, serverfs_mcp
d = m.distribution("serverfs-mcp")
mods = {n: u.find_spec(n) for n in ("serverfs_mcp.linux_v0250_core_binding", "serverfs_mcp.linux_v0250_bridge_adapter")}
base = pathlib.Path(serverfs_mcp.__file__).resolve().parents[1]
assert all(spec and pathlib.Path(spec.origin).resolve().is_relative_to(base) for spec in mods.values())
direct = d.read_text("direct_url.json")
assert not direct or not json.loads(direct).get("dir_info", {}).get("editable", False)
print(m.version("serverfs-mcp"), serverfs_mcp.__file__, "editable=false")
'
```

The actual candidate-image acceptance command was run from the repository root
with `SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE` unset:

```sh
env -u SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE \
  SERVERFS_LINUX_DOCKER_TEST_IMAGE=serverfs-mcp:linux-v0250-candidate-20261120-53112a212491 \
  uv run pytest -o addopts='' -q \
  tests/test_linux_v0250_file_only_docker.py \
  tests/test_linux_v0250_docker_e2e.py \
  tests/test_linux_v0250_dual_connector_lifecycle.py
```

Result: `4 passed in 175.94s` (file-only, signed Bridge owned and shared
parameters, and dual-connector lifecycle). The tests assert rendered Compose
and running Core container config contain no candidate `PYTHONPATH` or source
bind mount. File-only covers published loopback HTTP and internal-only HTTP
with no published port. Signed owned/shared cases use the real candidate Core
image with the temporary host Bridge and fake adapter. The dual case uses two
local Python stand-ins on internal networks; it does not start real Cloudflare
or OpenAI connectors and performs no provider egress. Each test cleans only its
unique Compose project. Postflight Docker container/network inventory showed
no `serverfs-v25-test-*` or `serverfs-v0250-dual-*` acceptance resources.

The explicit source-overlay fallback remains available and passed separately
against the cached v0.24.2 image (`2 passed in 115.84s`):

```sh
SERVERFS_LINUX_DOCKER_TEST_IMAGE=ghcr.io/ntlx/serverfs_mcp:0.24.2 \
SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE=1 \
  uv run pytest -o addopts='' -q \
  tests/test_linux_v0250_file_only_docker.py \
  tests/test_linux_v0250_dual_connector_lifecycle.py
```

The default Linux v0.25.0 regression then passed (`55 passed, 5 skipped in
12.62s`):

```sh
env -u SERVERFS_LINUX_DOCKER_TEST_IMAGE \
  -u SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE \
  -u SERVERFS_LINUX_REAL_AGENT_ACCEPT \
  -u SERVERFS_LINUX_INSTALL_TEST \
  -u SERVERFS_LINUX_INSTALL_SYSTEMD_ACCEPT \
  -u SERVERFS_LINUX_SYSTEMD_TEST \
  uv run pytest -o addopts='' -q tests/test_linux_v0250_*.py
```

Ruff check, Ruff format check, `python3 -m py_compile` for the three changed
Docker tests, and `git diff --check` passed. The production Docker project
`serverfs_cf_prod` remained running its existing v0.24.2 Core and file-ingress
containers; the read-only production Bridge user unit remained active at
MainPID `2039243`. No production container, user unit, Tunnel, or credential
was changed. This candidate is Linux source packaging and local runtime
evidence only; it is not release metadata, cross-platform packaging evidence,
public Tunnel/provider acceptance, or final v0.25.0 release acceptance.

The read-only production baseline used these queries:

```sh
docker --host unix:///var/run/docker.sock ps \
  --format '{{.Names}} {{.Image}} {{.Label "com.docker.compose.project"}}'
systemctl --user show serverfs-agent-bridge.service \
  -p LoadState -p ActiveState -p SubState -p MainPID -p FragmentPath --no-pager
```

The Docker output kept `serverfs_cf_prod-cloudflared-1`,
`serverfs_cf_prod-serverfs-mcp-1`, and
`serverfs_cf_prod-serverfs-file-ingress-1` running in project `serverfs_cf_prod`;
the two ServerFS containers remained on `ghcr.io/ntlx/serverfs_mcp:0.24.2`.
The user unit remained active/running at MainPID `2039243` with its existing
fragment `/home/lx/.config/systemd/user/serverfs-agent-bridge.service`.

## Tunnel client runtime compatibility and ingress acceptance

The opt-in offline check uses the actual local client images, never stand-ins
and never a provider network. Read-only inventory on 2026-11-21:

| Component | Local image | Image ID / runtime evidence |
| --- | --- | --- |
| Candidate Core | `serverfs-mcp:linux-v0250-candidate-20261120-53112a212491` | `sha256:6a3692cff58a1a93a4364b3aa00c9816e5bfaacabc9b5c4c8d0354fe7596123c` |
| Cloudflare | `cloudflare/cloudflared:2026.11.0` | `sha256:41c38225ed72eb65a0c644f02c522f7a8c5e34f2a71568f0b763573ad47fd9f4`; `cloudflared version 2026.11.0`, tested Compose UID/GID override `1000:1000` |
| OpenAI Secure MCP Tunnel | `ghcr.io/openai/tunnel-client:v0.0.17` | `sha256:052e3607211e3730a1f3dea5ea8d90112f6fdabd0e7ec67d301ff606cdf9565f`; `run version 0.0.17+a248b7f725fc989c1eb26680cf4351ee33421f61`, image entrypoint `/usr/bin/tunnel-client run`, default UID 0 |

The repository Cloudflare Compose default remains `cloudflare/cloudflared:2026.10.7`.
That image was not present locally (`docker image inspect` reported `No such
image`), so this gate did not run that default or pull it. The offline runtime
test explicitly overrides only its isolated client to locally available
`cloudflare/cloudflared:2026.11.0`; it does not change repository defaults.
The actual `tunnel run --help` exposes `--token-file`; the Compose command
`tunnel --no-autoupdate run --token-file` is accepted.
The test creates a synthetic token locally in cloudflared's documented
Base64-encoded JSON shape (`a`, `s`, `t`), with a synthetic account tag,
secret bytes and UUID, based on the [v2026.11.0 token implementation](https://github.com/cloudflare/cloudflared/blob/2026.11.0/connection/connection.go).
It writes the token to a private 0600 file owned by the current nonroot UID/GID,
renders Compose with `CLOUDFLARED_UID/GID` set to that owner, and runs the real
image with explicit local daemon `unix:///var/run/docker.sock`, `--pull=never`,
and `--network none`. The log proves parsing reached `Starting tunnel
tunnelID=01234567-89ab-cdef-0123-456789abcdef`, then failed DNS edge discovery
with `network is unreachable`; invalid-token and permission errors are
asserted absent. This is local token-shape/config parsing evidence, not proof
that Cloudflare would authorize the synthetic credentials.

OpenAI's actual `run --help` documents `--control-plane.api-key` as `file:`
capable, `--mcp.server-url url=...`, both MCP extra-header options as accepting
`file:` values, and `--health.listen-addr`. The local HTTP overlay forwards
those flags through the real `deployment/tunnel/tunnel-launcher.sh`. Its Core
bearer file contains exactly `Bearer synthetic-core-key`; the `Authorization:`
name belongs in the CLI argument (`Authorization: file:/...`), not in the
referenced file. This follows the [pinned v0.0.17 configuration reference](https://github.com/openai/tunnel-client/blob/v0.0.17/docs/configuration.md).
The offline test verifies argument/reference parsing and local file contents;
it does not prove that an HTTP request reached Core or that Core accepted the
forwarded authorization header.

Run the focused compatibility acceptance from the repository root:

```sh
SERVERFS_LINUX_TUNNEL_RUNTIME_ACCEPT=1 \
  uv run pytest -o addopts='' -q tests/test_linux_v0250_tunnel_runtime.py
```

Result: `2 passed in 6.70s`. Both Docker clients ran with `--network none`
and synthetic-only files; tests removed
uniquely labelled containers and assert no labeled container remains. OpenAI's
real launcher forwarded the HTTP URL, valid synthetic Tunnel ID, control-plane
key file and Core bearer header file. It reached connection attempts against
the deliberately unreachable loopback port; `/healthz` returned 200 while
`/readyz` returned 503 because offline startup could not complete. This proves
listener startup, not tunnel readiness. The `health` subcommand returned 2
for that expected unready result. cloudflared ran as the explicit Compose UID
override and read the mode-0600 synthetic token file; logs showed edge DNS
discovery blocked by the network namespace. No real Tunnel was contacted.

Re-run the suite without opt-in to confirm provider calls remain disabled by
default:

```sh
uv run pytest -o addopts='' -q tests/test_linux_v0250_tunnel_runtime.py
```

The dedicated Linux v0.25 Cloudflare test Tunnel and proxied hostname
`vps-serverfs-v25-test.ntlx.top`; the ingress maps to `http://serverfs-mcp:8000`
with an HTTP 404 fallback. This is resource provisioning evidence, not runtime
acceptance. Production `vps-serverfs` and the macbook/workpc-v25a/b resources
are not authorized for reuse. Cloudflare public acceptance passed as recorded
below. OpenAI uses a separate control-plane connection model; public OpenAI
registration/discovery and dual-provider public E2E are optional future
integration work, not release blockers. Existing offline checks cover the real
OpenAI client CLI, HTTP upstream/header file references, health listener and
unready behavior, plus local connector lifecycle and Compose selection. They do
not establish live OpenAI registration, discovery, or remote tool calls; no
such claim is made and no OpenAI test resources are required or provisioned.

### Public ingress smoke prerequisites and procedure

#### Cloudflare-only public smoke

The reusable runner is
`tests/test_linux_v0250_cloudflare_public.py::test_cloudflare_only_public_candidate_acceptance`.
It is skipped unless `SERVERFS_LINUX_CLOUDFLARE_PUBLIC_ACCEPT=1`; it also
requires explicit matching hostname approvals, the exact packaged candidate
image, an explicit locally available Cloudflare image, and a token **path**.
It checks token metadata only, never opens the token contents, and passes only
an allowlisted environment to Docker/Compose. It generates a private synthetic
API key and fixture, validates the loopback origin before starting cloudflared,
then checks TLS/MCP/auth, stop/start, force-recreate, and unique project cleanup.

The owner reported that the one-time handoff was unsealed successfully and the
one-time private key was removed. An earlier attempt failed before Docker
startup because the private-env writer received an unsupported `project`
keyword; that call was corrected and the shared preparation path passed the
local candidate Core regression. The authorized public acceptance then passed
through this ServerFS Agent task. It used the dedicated test hostname and
candidate images, performed public auth denial and HTTPS MCP initialize/list/
read checks, and verified connector stop/start/recreate without changing Core
identity or `StartedAt`. The token contents were never read or copied. After
the successful run and cleanup verification, only the exact temporary token
file and its empty private parent directory were removed.

```sh
SERVERFS_LINUX_CLOUDFLARE_PUBLIC_ACCEPT=1 \
APPROVED_CLOUDFLARE_TEST_HOSTNAME=vps-serverfs-v25-test.ntlx.top \
CLOUDFLARE_TEST_HOSTNAME=vps-serverfs-v25-test.ntlx.top \
SERVERFS_IMAGE=serverfs-mcp:linux-v0250-candidate-20261120-53112a212491 \
CLOUDFLARED_IMAGE=cloudflare/cloudflared:2026.11.0 \
CLOUDFLARE_TUNNEL_TOKEN_FILE=/path/to/private/cloudflare-token \
  uv run --no-sync pytest -o addopts='' -q --tb=short --basetemp=/tmp/serverfs-v0250-cf-acceptance-UNIQUE \
  tests/test_linux_v0250_cloudflare_public.py::test_cloudflare_only_public_candidate_acceptance
```

Acceptance result: `1 passed in 27.24s`. The run used a unique mode-0700
pytest temp directory and kept its output log outside that directory; both were
removed after the result was recorded. The test confirmed the public endpoint
returned HTTP 401 for missing and incorrect synthetic keys, and completed MCP
initialize/list/read over HTTPS with the synthetic fixture marker. Stopping the
connector removed it from the active service listing; ordinary restart kept
its ID and changed `StartedAt`; force-recreate changed connector identity and
start time. Core service lookup, ID, and `StartedAt` remained unchanged at each
lifecycle checkpoint. The unique Compose project reported zero residual
containers or networks. Postflight checks confirmed the three production
`serverfs_cf_prod` containers' IDs/`StartedAt` and the Bridge `MainPID` matched
their read-only preflight baseline. The exact token file and its now-empty
private parent directory were removed after checking regular-file/directory
types, uid/gid 1000, file mode 0600, directory mode 0700, and the parent's exact
contents. No OpenAI connector, production credential, or production service
was used.

The local-only harness checks are safe to run now. The offline test invokes
the same preparation helper as the public runner, starts only the packaged
Core candidate with `--pull never`, verifies unauthenticated and wrong-key
HTTP 401 responses, and performs MCP initialize/list/read against a synthetic
read-only marker fixture. It confirms no Cloudflare connector was started,
then tears down and checks the unique project labels. It makes no external
requests and does not access the handoff path:

```sh
env -u SERVERFS_LINUX_CLOUDFLARE_PUBLIC_ACCEPT \
  uv run pytest -o addopts='' -q tests/test_linux_v0250_cloudflare_public.py
```

The dedicated remotely managed Linux test Tunnel, hostname/route, synthetic
MCP reads, and cleanup are owner-authorized. Do not use `vps-serverfs`, the
macbook or workpc resources, or any other existing Tunnel. The authorized
dedicated hostname for the current acceptance is
`vps-serverfs-v25-test.ntlx.top`.
Require explicit nonempty values for
`CLOUDFLARE_TEST_HOSTNAME`, `CLOUDFLARE_TUNNEL_TOKEN_FILE`, and
`SERVERFS_IMAGE`; reject a token path inside the checkout/workdir or an image
other than `serverfs-mcp:linux-v0250-candidate-20261120-53112a212491`. Never
source production `.env`. Store a synthetic Core API key and a private env
file under a new temporary directory. Create one read-only workdir with one
marker file, allow only `serverfs-mcp`, loopback authority and the dedicated
hostname in the Core Host allowlist, use a new Compose project and loopback
port, and set `CLOUDFLARED_UID/GID` to the token file's 0600 owner. The
Cloudflare image must also be explicitly selected and already available before
using `--pull never`; the repo default 2026.10.7 was unavailable locally,
while local 2026.11.0 was used only by the offline test override.

Use a private, mode-0600 env file with these values (replace angle-bracket
items locally; never print the file):

```dotenv
SERVERFS_IMAGE=serverfs-mcp:linux-v0250-candidate-20261120-53112a212491
SERVERFS_UID=<workdir-owner-uid>
SERVERFS_GID=<workdir-owner-gid>
SERVERFS_HTTP_API_KEY=<new-synthetic-random-key>
SERVERFS_HTTP_ALLOWED_HOSTS=serverfs-mcp,127.0.0.1:<loopback-port>,<dedicated-hostname>
SERVERFS_HTTP_PUBLISH_HOST=127.0.0.1
SERVERFS_HTTP_PUBLISH_PORT=<unused-loopback-port>
WORKDIR_01_ALIAS=fixture
WORKDIR_01_PATH=<private-synthetic-read-only-workdir>
WORKDIR_01_READ_ONLY=true
CLOUDFLARE_TUNNEL_TOKEN_FILE=<dedicated-token-file-outside-workdir>
CLOUDFLARED_IMAGE=cloudflare/cloudflared:2026.11.0
CLOUDFLARED_UID=<token-owner-uid>
CLOUDFLARED_GID=<token-owner-gid>
CLOUDFLARE_TEST_HOSTNAME=<dedicated-approved-hostname>
APPROVED_CLOUDFLARE_TEST_HOSTNAME=<separately-confirmed-hostname>
```

Before Compose, check only metadata: hostname equals the explicitly approved
test hostname; token file is regular, mode 0600, owned by the configured UID/GID
and outside checkout/workdir; candidate Core and selected cloudflared images
are already present. Do not read or print the token. Use a unique project and
the exact isolated Compose file set below:

```sh
set -eu
set -a
. /absolute/private/test.env
set +a
: "${APPROVED_CLOUDFLARE_TEST_HOSTNAME:?confirm owner-approved dedicated hostname}"
: "${CLOUDFLARE_TEST_HOSTNAME:?supply the explicitly approved dedicated hostname}"
: "${CLOUDFLARE_TUNNEL_TOKEN_FILE:?supply the dedicated token file path}"
: "${SERVERFS_IMAGE:?supply the packaged candidate image}"
test "$CLOUDFLARE_TEST_HOSTNAME" = "$APPROVED_CLOUDFLARE_TEST_HOSTNAME"
test "$SERVERFS_IMAGE" = serverfs-mcp:linux-v0250-candidate-20261120-53112a212491
test -f "$CLOUDFLARE_TUNNEL_TOKEN_FILE"
test "$(stat -c '%a:%u:%g' "$CLOUDFLARE_TUNNEL_TOKEN_FILE")" = \
  "600:${CLOUDFLARED_UID}:${CLOUDFLARED_GID}"
token_real=$(realpath -e "$CLOUDFLARE_TUNNEL_TOKEN_FILE")
repo_real=$(realpath -e .)
workdir_real=$(realpath -e "$WORKDIR_01_PATH")
case "$token_real" in
  "$repo_real"/*|"$workdir_real"/*) exit 1 ;;
esac
docker --host unix:///var/run/docker.sock image inspect "$SERVERFS_IMAGE" >/dev/null
docker --host unix:///var/run/docker.sock image inspect "$CLOUDFLARED_IMAGE" >/dev/null
PROJECT="serverfs-v0250-cf-$(python3 -c 'import uuid; print(uuid.uuid4().hex[:12])')"
COMPOSE=(docker --host unix:///var/run/docker.sock compose -p "$PROJECT" \
  --env-file /absolute/private/test.env \
  -f compose.yml -f compose.http.yml \
  -f deployment/linux/compose.publish-http.yml \
  -f deployment/linux/compose.bridge-disabled.yml \
  -f compose.cloudflare.yml -f deployment/linux/compose.cloudflare-profile.yml)
"${COMPOSE[@]}" --profile cloudflare config --quiet
"${COMPOSE[@]}" --profile cloudflare up -d --no-deps --no-build --pull never serverfs-mcp
```

Before enabling `cloudflared`, verify the direct loopback origin returns 401
without authorization:

```sh
test "$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' \
  -H 'Host: <dedicated-hostname>' \
  "http://127.0.0.1:<loopback-port>/mcp")" = 401
"${COMPOSE[@]}" --profile cloudflare up -d --no-deps --no-build --pull never cloudflared
```

Using the existing MCP SDK client pattern in
`tests/test_linux_v0250_http_e2e.py` (`httpx2`, `mcp.Client`, and
`streamable_http_client`), connect to the HTTPS endpoint using a local script
that reads the synthetic key from the private env file without printing it.
For example, after loading this operator-created **test-only** env file into
the shell (never load a production `.env`):

```sh
set -a
. /absolute/private/test.env
set +a
uv run python - <<'PY'
import asyncio
import os
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def smoke():
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {os.environ['SERVERFS_HTTP_API_KEY']}"},
        timeout=15,
        trust_env=False,
    ) as http:
        transport = streamable_http_client(
            f"https://{os.environ['CLOUDFLARE_TEST_HOSTNAME']}/mcp",
            http_client=http,
            terminate_on_close=False,
        )
        async with Client(transport, mode="2026-07-28") as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert "read_text_file" in tools
            result = await client.call_tool(
                "read_text_file", {"workdir": "fixture", "path": "marker.txt"}
            )
            assert not result.is_error
            assert [item.text for item in result.content] == ["<exact-synthetic-marker>"]

asyncio.run(smoke())
PY
```

Replace `<exact-synthetic-marker>` in that local script with the marker file's
synthetic contents. Entering `Client` performs MCP initialize. Check wrong-key
denial through HTTPS with:

```sh
test "$(curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' \
  -H 'Authorization: Bearer deliberately-wrong-synthetic-key' \
  "https://<dedicated-hostname>/mcp")" = 401
```

Record the Core ID and `StartedAt`, stop/restart only `cloudflared` with the
same project/env/overlay set, and prove the Core ID and `StartedAt` are
unchanged. Repeat the authenticated MCP read through HTTPS. This is a
Cloudflare-only gate and does not require OpenAI resources.

For the lifecycle assertion, record and compare the actual service identities:

```sh
DOCKER=(docker --host unix:///var/run/docker.sock)
core_id=$("${COMPOSE[@]}" ps -q serverfs-mcp)
cloudflared_id=$("${COMPOSE[@]}" ps -q cloudflared)
core_before=$("${DOCKER[@]}" inspect --format '{{.Id}} {{.State.StartedAt}}' "$core_id")
cloudflared_before=$("${DOCKER[@]}" inspect --format '{{.Id}} {{.State.StartedAt}}' "$cloudflared_id")
"${COMPOSE[@]}" --profile cloudflare stop cloudflared
"${COMPOSE[@]}" --profile cloudflare up -d --no-deps --no-build --pull never cloudflared
test "$core_before" = "$("${DOCKER[@]}" inspect --format '{{.Id}} {{.State.StartedAt}}' "$core_id")"
cloudflared_after_id=$("${COMPOSE[@]}" ps -q cloudflared)
cloudflared_after=$("${DOCKER[@]}" inspect --format '{{.Id}} {{.State.StartedAt}}' "$cloudflared_after_id")
test "$cloudflared_id" = "$cloudflared_after_id"
test "$cloudflared_before" != "$cloudflared_after"
```

Repeat the HTTPS MCP checks, then tear down with
`"${COMPOSE[@]}" --profile cloudflare down --remove-orphans` and verify exact
project cleanup:

```sh
test -z "$("${DOCKER[@]}" ps -aq --filter "label=com.docker.compose.project=$PROJECT")"
test -z "$("${DOCKER[@]}" network ls -q --filter "label=com.docker.compose.project=$PROJECT")"
```

On failure, stop the test connector and tear down only the unique project. For
cleanup run the same Compose selection with `--profile cloudflare down
--remove-orphans`, then
assert no containers or networks remain with
`com.docker.compose.project=<unique-project>`. Delete only the dedicated test
Tunnel/hostname and temporary files using the resource owner's procedure.
Never roll back or modify production. Keep the candidate image tag unchanged.

#### OpenAI control-plane integration scope

The release gate retains the offline checks above for the actual pinned
OpenAI client, explicit HTTP upstream and bearer-header file references,
health/liveness behavior, Compose selection, and local independent connector
lifecycle. Public OpenAI registration/discovery and combined public-provider
E2E are optional future integration, not pending release acceptance. No live
OpenAI registration, discovery, or remote MCP call has been verified. Do not
request or provision OpenAI test resources for this release gate.

Final regression and hygiene commands for this change:

```sh
env -u SERVERFS_LINUX_DOCKER_TEST_IMAGE \
  -u SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE \
  -u SERVERFS_LINUX_REAL_AGENT_ACCEPT \
  -u SERVERFS_LINUX_INSTALL_TEST \
  -u SERVERFS_LINUX_INSTALL_SYSTEMD_ACCEPT \
  -u SERVERFS_LINUX_SYSTEMD_TEST \
  -u SERVERFS_LINUX_TUNNEL_RUNTIME_ACCEPT \
  -u SERVERFS_LINUX_CLOUDFLARE_PUBLIC_ACCEPT \
  uv run pytest -o addopts='' -q tests/test_linux_v0250_*.py
uv run pytest -o addopts='' -q tests/test_tunnel_proxy_deployment.py
SERVERFS_LINUX_TUNNEL_RUNTIME_ACCEPT=1 uv run pytest -o addopts='' -q tests/test_linux_v0250_tunnel_runtime.py
uv run ruff check tests/test_linux_v0250_cloudflare_public.py tests/test_linux_v0250_tunnel_runtime.py
uv run ruff format --check tests/test_linux_v0250_cloudflare_public.py tests/test_linux_v0250_tunnel_runtime.py
git diff --check
```

Results for the runner regression: the local harness command reported `4
passed, 1 skipped in 9.22s`; the public test is the skipped test. The actual
candidate Core container returned HTTP 401 without credentials and with a wrong
key, then passed authenticated MCP initialize/list/read against the synthetic
read-only marker. The offline project teardown assertion found no remaining
containers or networks with its unique Compose project label. The separate
offline real-client test reported `2 passed in 5.94s`. The full Linux v0.25
regression reported `59 passed, 8 skipped in 25.07s`; Tunnel launcher
regression reported `22 passed in 0.24s`. Ruff lint, Ruff format and
`git diff --check` passed. Formatted Docker image inspection confirmed Core
image ID `sha256:6a3692cff58a1a93a4364b3aa00c9816e5bfaacabc9b5c4c8d0354fe7596123c`
and cloudflared image ID
`sha256:41c38225ed72eb65a0c644f02c522f7a8c5e34f2a71568f0b763573ad47fd9f4`.
That local harness regression did not access the token handoff path, enable the
public runner, make public requests, or start cloudflared. The later authorized
Cloudflare public acceptance is recorded above; its dedicated token and temp
files were removed after successful cleanup verification.

## Linux v0.25.0 release integration readiness

Read-only Git inventory on 2026-11-21:

- Current branch: `linux/v0.25.0-docker`, HEAD
  `01d3e81dc56637df461857cf8fa2da06ef66790f`.
- Its configured upstream is incorrectly `origin/main`; that remote currently
  points at the same HEAD. The remote exposes only `main` and
  `macos/v0.25.0-checkpoint-20261117`, both at that same SHA; there is no remote
  Linux v0.25 branch.
- Local `main` is three commits behind `origin/main`. The Linux branch has no
  committed delta against current `origin/main` or the macOS checkpoint. The
  local-main-to-HEAD history touches 231 paths and intersects the dirty Linux
  work in five shared paths: Bridge `config.py`, `main.py`, `protocol.py`,
  `tests/test_core_grants_startup.py`, and Core `core_bridge_client_v2.py`.
- No fetch, scratch checkout, merge, rebase, or ref update was performed. The
  remote tips already resolve to the checked-out object; the dirty worktree,
  index, branch and HEAD were left in place.

Shared compatibility review:

- Linux extends the shared Core `core_bridge_client_v2.py` POSIX gate and adds
  Linux binding selection in `config.py`/`main.py`; `tools.py` switches Linux
  signed-binding mutation leases to alias identity. Darwin remains enabled by
  the same Core v2 client and its existing Darwin bootstrap path.
- Bridge `config.py`/`main.py` enable v2 on Linux and enforce alias leases;
  `protocol.py` keeps Linux v1 socket behavior while applying the Linux v2
  socket-path bound. These files are shared with the Darwin Bridge and require
  native Darwin acceptance before cross-platform integration.
- `deployment/tunnel/tunnel-launcher.sh` now forwards explicit argv in both
  proxy modes, while its no-argument behavior remains unchanged. The local
  OpenAI client test covers argument forwarding and offline startup only.
- Full root pytest exposed a stale common test expectation:
  `tests/test_core_bridge_client_v2.py::test_non_darwin_is_rejected` expected
  Linux rejection, while this Linux feature intentionally enables the client
  on Linux. The test now parametrizes Linux/macOS acceptance and Windows/
  unsupported-platform rejection without changing runtime behavior. Focused
  root validation passed all eight tests in that module.
- The other 38 root errors are setup failures in Darwin-only modules that use
  `/private/tmp`, which does not exist on this Linux host:
  `tests/test_darwin_v0250_shared_binding.py` (10 cases),
  `tests/test_darwin_v0250_shared_binding_private.py` (24 cases), and
  `tests/test_darwin_v0250_shared_readiness.py` (4 cases). No macOS tests or
  source were changed to mask these host-specific failures.

Regression commands and results (provider/live opt-ins explicitly unset):

```sh
env -u SERVERFS_LINUX_DOCKER_TEST_IMAGE \
  -u SERVERFS_LINUX_DOCKER_TEST_USE_SOURCE \
  -u SERVERFS_LINUX_REAL_AGENT_ACCEPT \
  -u SERVERFS_LINUX_INSTALL_TEST \
  -u SERVERFS_LINUX_INSTALL_SYSTEMD_ACCEPT \
  -u SERVERFS_LINUX_SYSTEMD_TEST \
  -u SERVERFS_LINUX_TUNNEL_RUNTIME_ACCEPT \
  -u SERVERFS_LINUX_CLOUDFLARE_PUBLIC_ACCEPT \
  -u SERVERFS_RUN_LOCAL_HTTP_SESSION_SSE_ACCEPTANCE \
  -u SERVERFS_MEASURE_BLIND_WINDOW \
  uv run --no-sync pytest -o addopts='' -q
(cd agent_bridge && env -u SERVERFS_LINUX_INSTALL_TEST \
  -u SERVERFS_LINUX_INSTALL_SYSTEMD_ACCEPT -u SERVERFS_LINUX_SYSTEMD_TEST \
  uv run --no-sync pytest -o addopts='' -q)
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
(cd agent_bridge && uv run --no-sync ruff check .)
(cd agent_bridge && uv run --no-sync ruff format --check .)
git diff --check
```

Results: root pytest `2683 passed, 416 skipped, 1 failed, 38 errors in
114.32s` was measured before the focused test correction; its single failed
assertion is now covered by the passing focused run below. The same 38 errors
remain the known Linux-host `/private/tmp` setup limitation in the three Darwin
modules above. Agent Bridge pytest `666 passed, 70 skipped in 89.84s`. The initial Bridge
invocation from the repository root accidentally loaded the root conftest and
exited at collection (`serverfs_mcp` unavailable); the corrected command above
runs from `agent_bridge/` and passed. Root Ruff lint passed and 746 files were
formatted; Agent Bridge Ruff lint passed and 126 files were formatted. The
focused root command `uv run --no-sync pytest -o addopts='' -q
tests/test_core_bridge_client_v2.py` reported `8 passed in 4.35s`. The Linux
v0.25 command with Docker, Agent, install/systemd, tunnel-runtime, and public
Cloudflare opt-ins unset reported `59 passed, 8 skipped in 22.47s`. No
provider opt-in ran in those Linux regressions.

Proposed isolated Linux branch scope (explicit allowlist; no credentials,
`.env`, production state, or generated artifacts were present in the worktree
path inventory):

- Shared files requiring integration review: `agent_bridge/src/serverfs_agent_bridge/config.py`, `agent_bridge/src/serverfs_agent_bridge/main.py`, `agent_bridge/src/serverfs_agent_bridge/protocol.py`, `agent_bridge/tests/test_core_grants_startup.py`, `src/serverfs_mcp/config.py`, `src/serverfs_mcp/core_bridge_client_v2.py`, `src/serverfs_mcp/main.py`, `src/serverfs_mcp/tools.py`, `deployment/tunnel/tunnel-launcher.sh`, `tests/test_tunnel_proxy_deployment.py`, `tests/test_core_bridge_client_v2.py` (platform contract test only).
- Linux-only Bridge implementation/tests: `agent_bridge/src/serverfs_agent_bridge/linux_v0250_install.py`, `agent_bridge/src/serverfs_agent_bridge/linux_v0250_preflight.py`, `agent_bridge/src/serverfs_agent_bridge/linux_v0250_provision.py`, `agent_bridge/tests/test_linux_v0250_core_grants_ipc.py`, `agent_bridge/tests/test_linux_v0250_provision.py`, `agent_bridge/tests/test_linux_v0250_two_core_isolation.py`.
- Linux-only Core implementation/tests: `src/serverfs_mcp/linux_v0250_bridge_adapter.py`, `src/serverfs_mcp/linux_v0250_core_binding.py`, `tests/test_linux_v0250_bridge_adapter.py`, `tests/test_linux_v0250_bridge_bootstrap.py`, `tests/test_linux_v0250_bridge_gate.py`, `tests/test_linux_v0250_cloudflare_public.py`, `tests/test_linux_v0250_compose.py`, `tests/test_linux_v0250_docker_e2e.py`, `tests/test_linux_v0250_dual_connector_lifecycle.py`, `tests/test_linux_v0250_file_only_docker.py`, `tests/test_linux_v0250_http_e2e.py`, `tests/test_linux_v0250_real_agent_docker.py`, `tests/test_linux_v0250_tunnel_runtime.py`, `tests/test_linux_v0250_writer_lease.py`.
- Linux deployment/design: `deployment/linux/README.md`, `deployment/linux/compose.bridge-disabled.yml`, `deployment/linux/compose.bridge-v2.yml`, `deployment/linux/compose.cloudflare-profile.yml`, `deployment/linux/compose.internal.yml`, `deployment/linux/compose.openai-http.yml`, `deployment/linux/compose.publish-http.yml`, `deployment/linux/serverfs-agent-bridge@.service`, `docs/architecture/v0.25.0-linux-ingress-host-bridge.md`.

Before any later cross-platform merge, run the macOS-owned acceptance on
macOS; the Linux-only CI result does not replace it. OpenAI public
registration/discovery remains optional future work, not a release blocker;
Cloudflare public acceptance passed, and its dedicated test Tunnel remains
allocated but inactive. No OpenAI resources or credentials are needed for this
release gate.
