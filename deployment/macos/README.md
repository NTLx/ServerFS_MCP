# macOS native deployment

Use Apple Silicon, native arm64, macOS 27.x and local APFS. ServerFS runs as the current user and respects macOS filesystem permissions and privacy controls. Persistent services use signed SMAppService / LaunchAgents, without Docker, Rosetta or a root LaunchDaemon.

Use a version-matched checkout and installed runtimes. This runbook covers **new named instances**, not migration of an existing default installation.

## 1. Choose your deployment

| Need | Path |
| --- | --- |
| Local MCP client only | Native stdio: `serverfs serve` |
| Persistent HTTP file service | File-only Core (`disabled`) |
| Agent delegation and/or Repo Ops with its own Bridge | Owned Core/Bridge pair (`owned`) |
| Multiple Cores using an existing v2 Bridge owner | Shared Core (`shared`): 1–16 owner-bound Agent workdirs per Core (v2 private binding), no Repo Ops |
| Optional OpenAI or Cloudflare ingress | Stage Connectors on a file-only or owned Core |

The named Shared Core has no Connector staging path. It may expose its authenticated loopback HTTP endpoint to an independently managed client or proxy. Do not assume every architecture topology is implemented by the same native commands. In particular, the owned Bridge uses its private native socket and policy checks; the shared path additionally requires owner-issued v2 grants.

## 2. Prepare a minimal Core

Install uv. In the version-matched checkout, prepare the Core environment:

```bash
uv sync --frozen
PY="$(pwd -P)/.venv/bin/python"
```

Keep the Core interpreter available at its original absolute path. Prepare a private configuration directory outside the checkout and all exposed workdirs.

Save this minimal policy as `/absolute/private/blue.toml`, replacing the example with a real accessible APFS directory:

```toml
[[workdirs]]
alias = "docs"
path = "/Users/operator/ServerFS-data/docs"
read_only = true
```

For local stdio, this is sufficient: configure the MCP client to run the version-matched `serverfs serve --config /absolute/private/blue.toml`. No persistent HTTP service or Tunnel credential is needed.

For persistent HTTP only, install Apple's native developer tools and build a signed template App in a fresh, private location. The build script replaces an App at the selected output path; never point it at an installed service App.

```bash
BUILD=/absolute/private/new-build
bash native/macos/service_manager/build.sh "$BUILD"
APP="$BUILD/ServerFSServiceManager.app"
```

Named staging copies and signs the locally ad-hoc-signed template. Prepare an existing current-user-owned service base at `~/Library/Application Support/ServerFS` (mode 0700 for a new installation). Do not change ownership or permissions on an existing installation as a shortcut.

Create a separate current-user-owned mode-0600 file `/absolute/private/blue.env` with only supported HTTP admission settings:

```text
SERVERFS_HTTP_ALLOWED_HOSTS=mcp.example.invalid,127.0.0.1:18781
SERVERFS_HTTP_API_KEY=<generate-a-unique-secret-locally>
```

Replace the example hostname and generate a unique high-entropy key locally. The Host allowlist must be **exactly** the configured hostname followed by the loopback authority, in that order. Choose an unused port. Do not put provider settings or arbitrary process environment into this file. OAuth is an alternative to the static API key, not an additional mode; it requires an independently configured authorization service. See [API Key](../../docs/api-key.md) and [OAuth](../../docs/oauth.md).

## 3. Stage and start a file-only Core

The examples below use Bash and the absolute `PY` and `APP` values from step 2. Stop on any nonzero exit. The instance ID is non-default, unique and stable (1–32 lowercase ASCII letters/digits with optional internal hyphens).

```bash
blue=("$PY" -m serverfs_mcp.darwin_instance_cli --instance blue)
"${blue[@]}" stage --config /absolute/private/blue.toml \
  --env-file /absolute/private/blue.env --http-port 18781 \
  --hostname mcp.example.invalid --template-app "$APP"
"${blue[@]}" status
"${blue[@]}" start --component core
"${blue[@]}" status
```

Staging creates an inactive instance, including a signed App and pinned configuration under `~/Library/Application Support/ServerFS/instances/blue/service-manager/`. Starting registers the Core's Login Item. If macOS requires approval, use System Settings → General → Login Items; do not bypass it.

Connect your Streamable HTTP client to `http://127.0.0.1:18781/mcp` with the private bearer credential. Verify unauthorized requests are denied, authenticated initialize and tools/list work, and the expected workdir is readable but not writable. The basic surface is 11 tools when all optional capabilities are disabled. Named persistent Cores disable shared file-parameter ingress; do not assume the Linux sidecar path is available here.

## 4. Optional: owned Bridge

Choose this path **instead of** file-only staging when a Core needs its own native Agent or Repo Ops. A fresh owned instance uses the same ID for Core and Bridge. Prepare disjoint writable roots and a separate locked Bridge environment:

```bash
uv sync --project agent_bridge --frozen
BRIDGE_PY="$(pwd -P)/agent_bridge/.venv/bin/python"
```

Prepare a separate TOML enabling the desired per-workdir policy. For example, repository inspection without Agent execution:

```toml
[[workdirs]]
alias = "repo"
path = "/Users/operator/ServerFS-data/repo"
read_only = true
repo_ops_enabled = true
```

For Agent delegation, also configure `[agent]`, provider executables and the matching runtime allowlist using the native TOML schema in [Agent Bridge](../../agent_bridge/README.md). Keep provider credentials outside workdirs and in provider-native configuration. Do not copy fake-runtime test configurations.

Prepare a separate `owned.env` with a fresh key and exact hosts `mcp-owned.example.invalid,127.0.0.1:18782`.

```bash
owned=("$PY" -m serverfs_mcp.darwin_instance_cli --instance blue-owned)
"${owned[@]}" owned-preflight --config /absolute/private/owned.toml
"${owned[@]}" owned-stage --config /absolute/private/owned.toml \
  --env-file /absolute/private/owned.env --http-port 18782 \
  --hostname mcp-owned.example.invalid --template-app "$APP" \
  --bridge-python "$BRIDGE_PY"
"${owned[@]}" owned-policy-check --config /absolute/private/owned.toml \
  --bridge-python "$BRIDGE_PY"
"${owned[@]}" owned-start --config /absolute/private/owned.toml --http-port 18782
"${owned[@]}" owned-status
```

The preflight checks owner/writable-root isolation; staging derives the private Bridge policy from the TOML. Start registers Bridge then Core, not Connectors. Verify Core MCP and only the configured Agent/Repo Ops capabilities. The named Bridge uses a short, derived Unix socket in the user's native runtime directory; do not hardcode another instance's socket or modify its private state.

## 5. Optional: shared Bridge

Use this advanced path only when an independent same-user v2 Bridge owner is already configured. The shared CLI does not provision or supervise that owner. Each Core needs a distinct owner-issued grant and private binding; owning a socket or running under the same UID is not sufficient.

v0.26.0 candidate scope: one to sixteen Agent-enabled workdirs per Shared Core (additional file-only workdirs are possible), **Repo Ops disabled**, no named Connector staging. The owner must independently maintain the matching per-alias workdir policies, task ownership and signed grants. The owner's private grant writer is `serverfs_agent_bridge.core_grants_private.provision_private_core_grants`; compute each `core_workdir_policy_digest` from the actual Bridge owner policy (never a TOML file hash). Existing `schema_version: 1` mode-0600 bindings containing `workdir_alias`, `workdir_path` and `policy_revision` remain supported for a single alias. For multiple Agent-enabled roots the owner provides a mode-0600 `schema_version: 2` binding with `bridge_id`, `core_id`, `key_id`, `secret_hex`, `bridge_config_path`, `socket_path`, and a `workdirs` list of 2–16 `{alias, path, policy_revision}` entries. Each path is a canonical host path, each policy revision matches its independent owner-side grant, and the shared Core accepts *exactly* the aliases in its native Agent-enabled workdir TOML. At startup the Core authenticates every alias against the running Bridge, and task-only follow-ups route by signed task ownership rather than untrusted task IDs. Do not generate a pretend binding, reuse another Core's credential, or modify an existing private binding in place. No general-purpose shared-owner installer is provided by this CLI; create new disposable instances for acceptance.

Prepare a private runtime parent (0700) outside the checkout and workdirs and a nonexistent target. Install the pinned Core runtime from the matching source with the required offline dependency cache:

```bash
RUNTIME=/absolute/private/runtimes/shared-blue
"$PY" -m serverfs_mcp.darwin_shared_runtime \
  --target "$RUNTIME" --project-root "$(pwd -P)" \
  --source-python "$PY" --uv "$(command -v uv)"
"$PY" -m serverfs_mcp.darwin_shared_runtime --verify-only \
  --target "$RUNTIME" --project-root "$(pwd -P)"
```

For an exact committed release, the runtime installer also accepts `--release-commit` and `--release-version` to pin the archived Git source and wheel metadata; supply both when verifying that release.

Prepare the matching Agent policy TOML, private env with exact hosts `mcp-shared.example.invalid,127.0.0.1:18868`, and owner-issued binding. The owner can be offline during staging, but must be ready and authorize the Core before activation.

```bash
shared=("$PY" -m serverfs_mcp.darwin_instance_cli --instance shared-blue)
"${shared[@]}" shared-stage --bridge-id owner-bridge \
  --config /absolute/private/shared.toml --env-file /absolute/private/shared.env \
  --binding-file /absolute/private/core-binding.json \
  --http-port 18868 --hostname mcp-shared.example.invalid \
  --template-app "$APP" --core-python "$RUNTIME/bin/serverfs-core-python"
"${shared[@]}" shared-preflight --bridge-id owner-bridge \
  --config /absolute/private/shared.toml --http-port 18868
"${shared[@]}" shared-start --bridge-id owner-bridge \
  --config /absolute/private/shared.toml --http-port 18868
"${shared[@]}" shared-status --bridge-id owner-bridge
```

Preflight is read-only; it checks the pinned runtime, signed App, private manifest, port and live grant. Verify each Core's admission, granted Agent behavior and rejection of foreign tasks. Owner restart/revocation is an owner operation, not a Shared Core lifecycle action.

## 6. Optional: add a Connector

Use this section for file-only or owned instances after checking Core readiness. The external Tunnel route and credentials are operator-managed. Target the Core's loopback port; preserve or set its exact configured Host authority. The Tunnel credential does not authenticate MCP callers.

A base Connector per provider requires no additional ID. For more than one Connector of the same provider, predeclare IDs at Core staging with repeated `--cloudflare-connector-id` / `--openai-connector-id` flags. The signed App cannot acquire undeclared named jobs in place. All subsequent Connector operations for an additional ID use `--connector-id <id>`. When targeting an owned pair, insert `--owned-connectors` before the subcommand, for example `"${owned[@]}" --owned-connectors cloudflare-stage ...`.

**Cloudflare** — obtain the verified executable and an independent current-user private token file outside workdirs and checkout. Configure the external route to `http://127.0.0.1:18781` for the file-only example.

```bash
"${blue[@]}" cloudflare-stage --cloudflared /absolute/bin/cloudflared \
  --token-file /absolute/private/cloudflare-token
"${blue[@]}" start --component cloudflare
```

**OpenAI** — use a verified tunnel-client supporting HTTP upstream and file-based credentials. Provide its independent Tunnel ID, private control-plane key file and Core bearer header file containing exactly `Bearer <Core API key>` without a trailing newline. This named Connector requires static API-key admission.

```bash
"${blue[@]}" openai-stage --tunnel-client /absolute/bin/tunnel-client \
  --tunnel-id "$TUNNEL_ID" --control-plane-key-file /absolute/private/control-plane.key \
  --core-bearer-file /absolute/private/core-auth-header
"${blue[@]}" start --component openai
```

Use distinct Tunnel resources for separate Connectors. Verify the external registration, public HTTPS, authorized MCP and denied requests independently. Local job status does not prove public reachability.

## 7. Operate, update or retire

| Scope | Status | Stop |
| --- | --- | --- |
| File-only | `status` | `stop --component core` |
| Owned | `owned-status` | `owned-stop` |
| Shared | `shared-status --bridge-id <id>` | `shared-stop --bridge-id <id>` |
| Connector | Instance status (file-only/owned) | `stop --component cloudflare` or `stop --component openai`, with the exact Connector ID if used |

Use the same instance identity and ownership selector for each command. To restart a Connector, stop and start only that Connector, then check unrelated PIDs and establish a fresh authenticated MCP session. There is no guarantee of old SSE session replay. The native stop guard refuses to stop a Core/owned pair while its Connectors remain active.

To retire a named instance, stop ingress first, unstage the selected Connector if no longer needed, then stop its Core or owned pair. The corresponding `unstage`, `owned-unstage` or `shared-unstage --bridge-id <id>` removes only eligible inactive state; retained task/log/runtime evidence can cause refusal. Do not force deletion or clear writer leases. A Shared Core never stops or uninstalls the owner. Before updates, account for active tasks and preserve the current source, runtime, configuration and rollback path. Staging is create-only and not an in-place upgrade or credential-rotation command.

## Compatibility and release evidence

For existing default installations use the version-matched scoped `serverfs service` compatibility commands; do not apply the named workflow to them. The [integration baseline](https://github.com/NTLx/ServerFS_MCP/blob/fffc0adb8341edb30ac7b03e72a017ca95b8c83d/deployment/macos/README.md) retains their commands and historical native acceptance recipes.

Current acceptance and remaining release gates are recorded in the [release checklist](../../docs/v0.25.0-release-checklist-2026-11-21.md). Historical signed native, public SSE and recovery tests do not establish that the final release artifact has passed every live combination.
