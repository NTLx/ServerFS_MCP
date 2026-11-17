# macOS Native Deployment (v0.24.1 stable)

ServerFS **v0.24.2 is the current stable release**. Native macOS Cloudflare Tunnel shipped in v0.24.1; it is optional and does not replace the existing OpenAI Secure MCP Tunnel, stdio, Agent Bridge or their lifecycle. Native Windows Cloudflare Tunnel was added in v0.24.2. The macOS deployment shape introduced
in v0.13.2 remains pure Python over POSIX/Darwin primitives (`fcopyfile`, `getpeereid`, `confstr`
via narrow ctypes bindings); v0.13.2 adds a small headless Swift ServiceManagement companion solely for persistent
per-user service registration. There is still no `serverfs-macos-native` wheel, Docker/VM,
LaunchDaemon, root helper or Rosetta requirement. Deterministic Repo Ops shipped on Linux Host Bridge in v0.23.0, validated native Apple Silicon macOS in v0.23.1, and validated native Windows 11 x64 in v0.23.2. The existing native persistent Tunnel remains the OpenAI adapter unless the operator separately opts into the Cloudflare pair below.

## v0.25.0 named-instance candidate (unreleased)

The v0.25.0 working tree adds independent, current-user macOS `SMAppService` Core instances with unique `instance_id`, signed app identity, launchd labels, 0600 private manifest/HTTP environment, data home and logs. The existing production `default` instance names and services are unchanged. **Both `bridge_mode=disabled` (file-only) and `bridge_mode=owned` (independent private Bridge) have passed real native lifecycle acceptance.** A named Core can stage one independent Cloudflare and one independent direct-HTTP OpenAI Connector; their four-component owned combination has passed native lifecycle/MCP acceptance with **no-network simulated Connectors only**. `bridge_mode=shared` now has real macOS `SMAppService` acceptance for two separately registered signed Shared Cores against one independent Bridge v2 owner, including owner restart, per-Core task isolation, owner grant revocation and restoration of the original grants without Core restart, and verified cleanup; these tests use isolated synthetic owner/provider resources, not a production Shared Bridge deployment. Multiple `connector_id` values per provider have passed real signed `SMAppService` lifecycle acceptance in isolated Owned and file-only instances with no-network simulated Connectors. The existing Cloudflare production hostname has passed ChatGPT-connected MCP filesystem CRUD and binary transfer after the independent Core/Connector cutover; the public hostname has now also passed authenticated MCP `initialize` and one continuous **75.640-second** SSE GET with **5 keepalives**, without proxy/WAF changes; controlled production Connector-only restart with a fresh authenticated public MCP session and >75-second SSE hold has since passed; unclean-exit and QUIC/public-network fault recovery remain unverified. The CLI is intentionally separate from legacy `serverfs service` to avoid Windows parallel-development conflicts.

To prepare a **new, inactive** Core, first build an unregistered template bundle and choose absolute paths, a unique lowercase instance ID, an unused local port, a private 0600 Core env file outside all workdirs, and an exact `SERVERFS_HTTP_ALLOWED_HOSTS=hostname,127.0.0.1:port` setting:

```bash
bash native/macos/service_manager/build.sh /absolute/private/build
python -m serverfs_mcp.darwin_instance_cli --instance dev-core stage \
  --config /absolute/path/serverfs.toml \
  --env-file /absolute/private/core.env \
  --http-port 18781 --hostname mcp-dev.example.invalid \
  --template-app /absolute/private/build/ServerFSServiceManager.app
python -m serverfs_mcp.darwin_instance_cli --instance dev-core status
python -m serverfs_mcp.darwin_instance_cli --instance dev-core start --component core
python -m serverfs_mcp.darwin_instance_cli --instance dev-core stop --component core
```

The Core serves on `127.0.0.1` with API Key or configured OAuth admission, and has no Agent Bridge/Repo Ops. An optional independent Cloudflare Connector may be staged with `cloudflare-stage --cloudflared /absolute/binary --token-file /absolute/private/token` and explicitly started/stopped via `--component cloudflare`; public Tunnel/DNS resources and caller authentication remain operator-owned, never auto-provisioned. Do not reuse the default production Connector Token file. `unstage`/`cloudflare-unstage` refuse running jobs and retained or unexpected private data/logs rather than force-delete them.

Actual native E2E evidence is in `docs/diagnostics/v0250-macos-named-instance-runtime-2026-11-13.md`, `docs/diagnostics/v0250-macos-owned-native-acceptance-2026-11-14.md`, `docs/diagnostics/v0250-macos-owned-dual-connector-acceptance-2026-11-14.md`, and `docs/diagnostics/v0250-macos-connector-native-acceptance-2026-11-14.md`. Independently named file-only Cores, owned Core/Bridge pairs, and multiple `connector_id`-qualified same-provider services have passed real signed `SMAppService` lifecycle and local MCP tests without changing four default production PIDs. Synthetic Connectors do **not** verify real Cloudflare/OpenAI public HTTPS, SSE or session behavior. Shared Bridge v2 has now passed real two-Core macOS `SMAppService` registration, owner crash/restart recovery, grant revocation, cross-Core task isolation, and cleanup with unchanged production service PIDs. The existing Cloudflare hostname has passed authenticated ChatGPT MCP file/binary E2E; a new isolated loopback stateful HTTP session test separately verified one GET SSE for >65 seconds, periodic keepalive, old-session rejection after Core restart and fresh-session establishment. Fresh dirty-source Core/Bridge `0.24.2`-metadata wheels were also installed into **two new independent Python 3.12.12 environments** from public PyPI hash-locked `uv.lock` exports, passed isolated `-I -B` installed-site-packages import checks, and completed a real **one Bridge + two Core** HTTP/IPC loopback acceptance (**1 passed, exit 0**). This is current working-tree packaging evidence, **not** a release-matched v0.25.0 artifact. The existing public Cloudflare hostname subsequently passed direct no-proxy authenticated `initialize` (HTTP 200) and one SSE GET kept open for >75 seconds with 5 keepalives. **Production Connector unclean-exit/QUIC recovery**, release-matched v0.25.0 artifacts and three-platform same-SHA acceptance remain incomplete (the bounded controlled Connector-only restart and fresh authenticated public MCP/SSE checks passed separately); see `docs/diagnostics/v0250-macos-public-cloudflare-sse-acceptance-2026-11-16.md`. See `docs/diagnostics/v0250-macos-current-wheel-fresh-venv-install-2026-11-16.md` and `docs/diagnostics/v0250-macos-fresh-wheel-three-process-e2e-2026-11-16.md`. See `docs/diagnostics/v0250-macos-release-readiness-2026-11-16.md` and `docs/diagnostics/v0250-macos-local-http-session-sse-acceptance-2026-11-16.md`.

For a **new isolated owned instance**, use `owned-stage` (Core + private Bridge preparation), `owned-start` (register Bridge then Core), `owned-status`, `owned-stop` (Core then Bridge), and `owned-unstage` (only a completely pristine, inactive candidate). The isolated CLI requires `--instance <non-default-id>`; `owned-stage` additionally requires `--bridge-python` pointing to the independent Bridge virtualenv interpreter, alongside the Core config/env/port/hostname and signed template bundle. For Connector staging, start/stop or unstage on that owned Core, use `--owned-connectors` before the Connector subcommand. For example:

```bash
# Placeholder paths only; stage an owned instance with private, non-production inputs.
python -m serverfs_mcp.darwin_instance_cli --instance dev-owned owned-stage \
  --config /absolute/private/serverfs.toml --env-file /absolute/private/core.env \
  --http-port 18782 --hostname mcp-dev.example.invalid \
  --template-app /absolute/private/build/ServerFSServiceManager.app \
  --bridge-python /absolute/agent_bridge/.venv/bin/python
python -m serverfs_mcp.darwin_instance_cli --instance dev-owned owned-start \
  --config /absolute/private/serverfs.toml --http-port 18782
python -m serverfs_mcp.darwin_instance_cli --instance dev-owned owned-status
# Stage the isolated Connector separately before attempting its start:
python -m serverfs_mcp.darwin_instance_cli --instance dev-owned \
  --owned-connectors cloudflare-stage --cloudflared /absolute/cloudflared \
  --token-file /absolute/private/cloudflare-token
python -m serverfs_mcp.darwin_instance_cli --instance dev-owned \
  --owned-connectors start --component cloudflare
# Stop Connectors first, then owned Core + Bridge; do not touch default services.
```

### v0.25.0 reviewed-source candidate gate (unreleased)

The macOS development checkout may contain uncommitted source and generated test artifacts. Before considering an integration commit, use the explicit local source inventory rather than bulk staging or packaging the entire mutable working tree. The reviewed overlay allowlist is `deployment/macos/v0250_candidate_source_paths.txt` and the fail-closed checker is `deployment/macos/v0250_candidate_source_gate.py`. It requires an exact match to current modified/untracked source paths, rejects any staged changes and unsafe paths, and excludes only the known `deployment/macos/.serverfs-v0250-*` scratch prefix. Root and Bridge `pyproject.toml`/`uv.lock` plus root `plugin.json` are the only explicitly allowed version-metadata TOML/lock/JSON paths; arbitrary configuration and credential files remain forbidden. The root Ruff configuration separately excludes this same generated scratch prefix from lint/format traversal, without excluding actual application source.

```bash
# Check only: no Git writes, build, registration or network access.
.venv/bin/python -B deployment/macos/v0250_candidate_source_gate.py

# Optional development drill: provide a fresh, randomized 12-hex task-owned
# scratch name under deployment/macos; the script creates it itself.
.venv/bin/python -B deployment/macos/v0250_candidate_source_gate.py \
  --snapshot "$(pwd)/deployment/macos/.serverfs-v0250-candidate-0123456789ab"
```

The second command illustrates syntax only: use a **new random suffix** and never reuse an existing directory. It materializes local Git HEAD plus exactly the reviewed overlay files, producing an independent candidate source tree without staging or committing. Build Root and Bridge Wheels offline from that tree, run the Wheel archive integrity gate, and clean only the verified task-owned scratch. A passing source drill is **not** an exact release SHA: Root, Bridge, Plugin and locks must first be coordinated to `0.25.0` in a reviewed clean integration commit; both Wheel metadata and all three platform acceptance gates must be tied to that single SHA. Keep production OpenAI/Cloudflare services untouched.

See `docs/diagnostics/v0250-macos-reviewed-source-snapshot-dryrun-2026-11-16.md` for the measured 192-file source reconstruction and offline Root/Bridge build evidence. A subsequent **isolated, unreleased** `0.25.0` metadata drill also passed offline Root/Bridge lock consistency, unchanged third-party dependency graphs, both 0.25.0 Wheel archive checks and Plugin validation; see `docs/diagnostics/v0250-macos-version-metadata-isolated-dryrun-2026-11-16.md`. Neither drill alters live project versions or constitutes an exact-SHA release.

For the opt-in installed-Wheel **one Bridge + two Core** local acceptance, keep the Wheel/venv storage directory separate from the **short** test runtime directory. Darwin's Unix-domain socket `sun_path` permits at most **103 pathname bytes** (excluding its NUL terminator); the test now checks the exact UTF-8 byte length of `<runtime-scratch>/case/owner.sock` before starting Bridge. A nested runtime directory beneath a long `.serverfs-v0250-candidate-*` Wheelhouse can exceed that limit even when all packages install correctly. Use a new private direct child of `deployment/macos`, for example `.serverfs-v0250-e2e-<8 random hex>`, and pass its absolute path in `SERVERFS_V0250_WHEEL_SCRATCH`; keep the separate `SERVERFS_V0250_WHEELHOUSE` pointed at the actual installed Core/Bridge `0.25.0` venvs. Do not set the public-hold option for a loopback-only test. A measured corrected `0.25.0` installed-Wheel test passed on macOS (1 Bridge, 2 Cores, 93-byte socket, authentication/isolation/Bridge restart, exit 0); see `docs/diagnostics/v0250-macos-versioned-wheel-live-e2e-2026-11-16.md`. This does not substitute for a clean-SHA or production service/public ingress acceptance.

The separately opt-in **signed native** two-Shared-Core test can also validate the installed Core and Bridge candidates rather than the live source checkout. After independently creating a reviewed, owner-private `0.25.0` source snapshot with Root/Bridge locks checked offline, and a separate independently installed `0.25.0` Bridge virtual environment, use both test-only pins:

```bash
# All paths must already exist and meet the test's strict owner/version checks.
# This registers two RANDOM test-only signed SMAppService Core Login Items;
# do not run on a host with unknown ServerFS launchd jobs.
SERVERFS_ENABLE_SHARED_SMAPP_LIVE_TEST=1 \
SERVERFS_V0250_NATIVE_CORE_SOURCE="$ABS_PRIVATE_0250_SOURCE_SNAPSHOT" \
SERVERFS_V0250_NATIVE_BRIDGE_PYTHON="$ABS_PRIVATE_0250_BRIDGE_VENV/bin/python" \
  .venv/bin/python -B -m pytest -q -s -p no:cacheprovider \
  tests/test_darwin_v0250_shared_smappservice_live_macos.py
```

The Core pin must be a canonical `.serverfs-v0250-candidate-<12 lowercase hex>` directory owned by the current user with mode `0700`, Root metadata `0.25.0` and a lockfile. The Bridge pin must be an installed candidate `bridge-venv/bin/python` whose own site-packages contains `serverfs-agent-bridge==0.25.0` and private-grants modules. **Both pins must point to the same candidate source directory:** the test now refuses any mixed Core/Bridge candidate or a versioned Bridge with a non-versioned Core before native registration. It builds the signed ServiceManager from that same candidate directory, uses no implicit `PYTHONPATH` for the installed owner, requires the protected production launchd labels/PIDs to remain unchanged, and refuses uncertain native cleanup. The previous mixed-source acceptance is retained only as development history. A corrected one-source candidate A native test passed with **numeric pytest exit 0** and independently verified JUnit XML (**1 test, 0 failures/errors/skips**), two signed native `0.25.0` Cores, and an installed `0.25.0` Bridge. The private task-owned JUnit evidence is retained for inspection; see `docs/diagnostics/v0250-macos-signed-0250-shared-cores-2026-11-16.md`. This development candidate is still **not an immutable same-SHA release**, nor production Cloudflare/public ingress acceptance.

## Supported platform

```text
Apple M-series Mac, native arm64 (no Rosetta), macOS 27 Golden Gate, local APFS
```

## Install dependencies

```bash
uv sync --frozen                           # ServerFS product environment
uv sync --project agent_bridge --frozen    # Agent Bridge environment
serverfs bootstrap tunnel-client           # defaults to the ServerFS-tested official release
serverfs doctor --config serverfs.toml
```

The persistent-service installer builds `ServerFSServiceManager.app` locally with the Apple command
line toolchain (`xcrun swiftc`, `codesign`, `plutil`), validates it, and installs it under the
private per-user ServerFS Application Support tree. The app contains two app-associated
LaunchAgents and uses Apple's `SMAppService` API; normal operation does not copy a plist into
`~/Library/LaunchAgents` or ask the operator to run raw `launchctl` commands.

## Persistent Tunnel + Agent Bridge

With tunnel credentials and any Agent/Jev/proxy settings in the private `.env`:

```bash
serverfs service install --config serverfs.toml --env-file .env
serverfs service status
```

`install` materializes private 0600 lifecycle state, builds/validates the native companion, registers
`com.ntlx.serverfs.tunnel` and (when Agent delegation is enabled) `com.ntlx.serverfs.bridge`,
and verifies the managed processes. Both are current-user LaunchAgents with `RunAtLoad` and
`KeepAlive`: they survive terminal/Agent-session exit. `SMAppService` owns user-login persistence;
live reboot/re-login recovery validation is deferred beyond v0.13.2.

macOS may require explicit approval under **System Settings > General > Login Items**. ServerFS
reports that condition as `requires-approval` and never bypasses the user's setting:

```bash
serverfs service open-settings
```

Lifecycle commands are:

```bash
serverfs service status
serverfs service start
serverfs service restart
serverfs service stop
serverfs service uninstall
```

When the installed manifest includes Agent Bridge, `service restart` uses a product-owned, one-shot launchd hand-over so unregistering the Bridge cannot terminate the process that must finish the restart. The parent command reports that the hand-over was scheduled; reconnect and run `serverfs service status` to verify the native `handover` evidence reaches `complete=true`, `ok=true`, `job_loaded=false` and `plist_exists=false`. Tunnel-only restart remains synchronous. `serverfs agent-bridge restart` uses the same bounded hand-over after any requested policy/private-state synchronization and restarts only the Bridge.

The default native `service status` JSON includes this bounded hand-over evidence. For a sanitized common projection, explicitly select `--format common-json` or `--format common-human`; it omits paths, PIDs, task names, private IDs and the native hand-over detail, and reports `unknown`/`not-checked` rather than inferring health. Use `serverfs doctor` for deeper platform/configuration diagnostics. Phase F acceptance limits: [v0.21.0 native acceptance](../../docs/v0.21.0-phase-f-native-acceptance-2026-10-22.md).

`uninstall` removes only v0.13.2-owned registration/runtime artifacts; operator configuration and
normal ServerFS data are preserved.

## Optional Cloudflare Tunnel (v0.24.1 release)

v0.24.1 added a separate native Cloudflare Connector and private loopback Streamable HTTP origin. It does not change the existing OpenAI Tunnel or Agent Bridge. Use a version-matched native build on supported Apple Silicon/macOS 27. The **unreleased v0.24.3 candidate** extends installer admission to explicitly writable workdirs; v0.24.1/v0.24.2 remain limited to read-only Cloudflare workdirs. It does not create or edit Cloudflare Tunnels, DNS routes, or public hostname configuration; configure the named remotely managed Tunnel and route through Cloudflare's operator controls first.

The bootstrap command installs the pinned official Cloudflare `cloudflared` macOS arm64 release, verifying its archive and binary digests, code signature and version. With no flag it downloads the pinned artifact; `--artifact` accepts a local archive for offline installation:

```bash
serverfs bootstrap cloudflare-client
serverfs bootstrap cloudflare-client --artifact /absolute/path/to/cloudflared-darwin-arm64.tgz
```

Prepare two current-user regular files with mode `0600`, stored outside the checkout and every configured workdir:

- a connector Token file, containing only the remotely managed Tunnel token;
- an HTTP-origin environment file containing only supported settings. Use either a non-empty `SERVERFS_HTTP_API_KEY` with OAuth disabled, or OAuth enabled with its required public resource/issuer/introspection settings and secret. Do not combine API Key and OAuth. Set `SERVERFS_HTTP_ALLOWED_HOSTS` to exactly the public hostname passed to install. The Token authenticates only the Connector; it does not authenticate MCP callers.

Use absolute paths for all install paths. `--http-port` has no default and must be available for the loopback origin. Configure `serverfs.toml` with at least one workdir. **Published v0.24.1/v0.24.2 installers require all Cloudflare workdirs to be read-only. The v0.24.3 candidate permits explicit `read_only = false` while retaining the native default of read-only and the existing per-workdir Agent/Repo Ops permissions.** Replace the path and hostname placeholders below with the operator's own values; never put secret values in command arguments:

```bash
serverfs service cloudflare install \
  --config /absolute/path/to/serverfs.toml \
  --cloudflared /absolute/path/to/cloudflared \
  --token-file /absolute/path/outside-workdirs/cloudflare-token \
  --env-file /absolute/path/outside-workdirs/cloudflare-origin.env \
  --http-port 18765 \
  --hostname mcp.example.invalid
serverfs service cloudflare status
```

The installer registers an HTTP-origin/Connector pair through the same current-user, app-associated `SMAppService` LaunchAgent mechanism as the existing native services. The origin binds to `127.0.0.1`; public TLS terminates at Cloudflare. The hostname must match exactly in ServerFS Host admission. Verify both service statuses, then verify public HTTPS rejects missing/wrong caller credentials with 401 and accepts the intended API Key/OAuth identity with 200. Continue with MCP tools and a persistent SSE connection longer than 60 seconds. After a controlled Connector-only restart, establish a **fresh MCP session** and verify tools; the previously established session may return 404 (the observed staging result was 8/8 successful fresh-session checks). Do not claim old-session continuity or SSE event replay. This paragraph records the original v0.24.1 isolated read-only staging acceptance; it does **not** establish production writable-workdir or Agent acceptance. Follow the additional v0.24.3 candidate gate below before using writable production workdirs.

If registration fails, the installer attempts to unregister the services it registered and restore its manifest state. If rollback is reported incomplete, stop and preserve the recovery state; do not layer another install over it. The explicit lifecycle commands are:

```bash
serverfs service cloudflare start
serverfs service cloudflare stop
serverfs service cloudflare restart
serverfs service cloudflare uninstall
```

`uninstall` unregisters this Cloudflare service pair and removes its private origin-environment snapshot; it preserves the operator's source Token/env files, `serverfs.toml`, workdirs, existing OpenAI Tunnel and Agent Bridge. Cloudflare service registration can require user approval in System Settings > General > Login Items; respect that decision and do not bypass it.

### v0.24.3 candidate: production workdir parity (not yet released)

Only a **version-matched v0.24.3 candidate** can install a Cloudflare HTTP origin with an explicitly writable workdir. For intentional OpenAI/Cloudflare parity, point Cloudflare's `--config` to the **same canonical native `serverfs.toml` actually used by the OpenAI Tunnel**; do not maintain a second, drifting production workdir policy. This shares the configured filesystem permissions, optional binary transfer, Agent runtimes and independently gated Repo Ops. The separate HTTP caller API Key/OAuth still authorizes incoming clients; a Connector Token alone never grants MCP access. An authenticated Cloudflare client inherits all capabilities enabled by the shared policy, so protect the caller credential accordingly.

The macOS HTTP origin and OpenAI Tunnel may address the **same** Agent Bridge and writer-lease identity. Before re-registering the Cloudflare pair, verify the live Bridge policy is in sync with the canonical TOML and preserve the existing OpenAI Tunnel/Bridge processes. The native Cloudflare service pair is singleton: do **not** register a second unmanaged pair or run a general `serverfs service restart`. Prepare a separately verified read-only Cloudflare config for rollback; keep existing private token and origin-auth env outside every workdir. Use `serverfs service cloudflare uninstall` followed by the supported `serverfs service cloudflare install --config <canonical-toml> --cloudflared <verified-binary> --token-file <private-token-file> --env-file <private-origin-env> --http-port <available-loopback-port> --hostname <exact-public-host>`; those angle-bracket values are placeholders, not literal argv. This re-registration affects only the Cloudflare Connector/HTTP Origin pair.

**Release acceptance gate:** on a disposable test file only, verify public HTTPS authentication failures (401), authenticated MCP initialize and workdir/tool discovery (200), read/write/edit/delete with revision guards, read-only/deny-path rejection, optional binary transfer as configured, available Agent runtimes through the existing Bridge, cross-ingress writer-lease behavior, a >60-second SSE connection and fresh-session behavior. Recheck that the OpenAI Tunnel and Bridge remain running. On failure, re-register only the previous read-only Cloudflare pair using its preserved private inputs. Until these runtime checks pass, do not claim production write/Agent parity or instruct operators to retire OpenAI Tunnel.

For v0.24.1 lifecycle acceptance, architectural equivalence to the previously verified app-associated `SMAppService`/LaunchAgent mechanism is accepted. An additional real logout/login or whole-Mac reboot is not required for this release and must not be claimed as performed. This exception applies only while the lifecycle architecture remains the same.

### v0.25.0 development: independently staged HTTP Core and Cloudflare Connector

This section documents **unreleased source-only interfaces**. It is not a production
migration procedure and does not supersede the v0.24.1/v0.24.3 rollback path.
On a host with an already registered Cloudflare HTTP-origin/Connector pair,
**both stage operations refuse to replace it**. Do not stop or uninstall that pair
just to test these commands; prepare a separately accepted isolated instance.

The independent Core and Connector reuse the existing current-user
`SMAppService` identities `http-origin` and `cloudflare`, with distinct service
owners. Explicit `stage` only writes the private mode-0600 manifest; it does
**not** register or launch any service. Core stage first checks that the TOML is
the same canonical file as an existing OpenAI Tunnel manifest. Its HTTP
environment must include exactly
`SERVERFS_HTTP_ALLOWED_HOSTS=<public-host>,127.0.0.1:<core-port>` with valid
API-Key-or-OAuth authentication. The Cloudflare Connector's private token stays
outside the repository and all exposed workdirs; only its path is written to the
manifest. The public hostname and Core port must match that exact Core snapshot.

On an **isolated, independently accepted macOS test installation only**, after
providing real existing private inputs at the placeholder paths:

```bash
serverfs service core stage \
  --config /absolute/path/to/serverfs.toml \
  --env-file /private/path/core-http.env \
  --http-port 18766 \
  --hostname mcp.example.invalid
serverfs service cloudflare stage \
  --config /absolute/path/to/serverfs.toml \
  --cloudflared /absolute/path/to/cloudflared \
  --token-file /private/path/cloudflare-token \
  --hostname mcp.example.invalid
serverfs service core status
# Registration is a separate explicit operator-controlled test step:
serverfs service start --component core
serverfs service start --component cloudflare
# A Connector-only restart must not stop the Core or Agent Bridge:
serverfs service restart --component cloudflare
```

For a safe cleanup of **only those isolated staged services**, explicitly stop
the connector, verify it is no longer registered or running, and use
`serverfs service cloudflare unstage`. Stop the Core and verify its unregistered
state before `serverfs service core unstage`. Never unstage the Core while the
Connector manifest remains. The source credentials are not deleted by these
commands.

**Compatibility warning:** on unmarked legacy manifests, bare
`serverfs service start/stop/restart/uninstall/install` retains historical
group behavior. When **any independently owned** v0.25.0 Core/Connector
manifest entry is present, the same legacy group operations now fail closed
*before* registering, unregistering or replacing the bundle. Use only explicit
component-scoped commands. Legacy `service cloudflare stop/restart/uninstall`
still operates the original paired origin/connector and rejects an
independently owned Core. Public OpenAI/Cloudflare, workdir/Bridge and rollback
acceptance remain required.

### v0.25.0 development: OpenAI Tunnel as a direct HTTP Connector

**Source-only candidate; do not run on the production Mac.** The new
`serverfs service openai stage` path uses the existing app-owned
`tunnel` LaunchAgent identity but launches `tunnel-client run` directly.
It passes `--mcp.server-url url=http://127.0.0.1:<core-port>/mcp`, not the old
`--mcp.command` supervisor that owned Core/Bridge child processes. The Core
must already have an `origin_owner=independent-core` entry and the identical
canonical `serverfs.toml`. Existing OpenAI Tunnel manifests, registered
Tunnel jobs and unmanaged instances are rejected; no in-place upgrade is
performed during stage.

The operator must provide two **different** current-user mode-0600 files,
stored outside every exposed workdir and checkout: a control-plane runtime
API key file and a separate Core HTTP header-value file. For this initial
static-key candidate, the second file must contain the exact single-line
`Bearer <core-http-api-key>` value (without a trailing newline), matching
the Core's protected environment snapshot. OAuth-only Core configurations
are not supported by this candidate and fail closed. The files' contents
are never written into the service manifest, command line or logs; only
`file:` paths are passed to the official tunnel-client's separate MCP
runtime and discovery headers.

On an **isolated test host/identity only**, after independent Core staging:

```bash
serverfs service openai stage \
  --config /absolute/path/to/serverfs.toml \
  --tunnel-client /absolute/path/to/verified-tunnel-client \
  --tunnel-id tunnel_0123456789abcdef0123456789abcdef \
  --control-plane-key-file /private/path/control-plane-runtime.key \
  --core-bearer-file /private/path/core-auth-header
# Only after deliberate isolated acceptance:
serverfs service start --component tunnel
# Scoped shutdown and safe removal of the unregistered staged connector:
serverfs service stop --component tunnel
serverfs service openai unstage
```

The actually installed Mac `tunnel-client 0.0.17` has been checked using
`--version` and `run --help`: the direct HTTP, static runtime/discovery
headers, health-listen and `file:` reference **options exist**. The
`url=...` HTTP-target syntax is consistent with this binary's help.
**Actual `file:` resolution, proxy authentication, startup readiness,
hosted MCP POST/SSE, cross-ingress sessions and Agent/Repo calls are NOT
verified.** An isolated `dev proxy` smoke attempt failed before protocol
evidence; see
`docs/diagnostics/v0250-macos-m1-native-capability-and-safety-2026-11-12.md`.
The new stage does not upgrade the existing production OpenAI Tunnel,
Bridge or Cloudflare setup.

## Agent policy updates

The private `bridge.json` remains derived/private state outside every exposed workdir, not a second
operator configuration. Workdir/runtime/lifecycle policy is owned by `serverfs.toml`:

```bash
serverfs agent-bridge configure --config serverfs.toml --env-file .env
serverfs agent-bridge restart --config serverfs.toml
# if private Jev/proxy values also changed:
serverfs agent-bridge restart --config serverfs.toml --env-file .env
```

The legacy `serverfs agent-bridge install` path is retired in v0.13.2. Upgrading from v0.13.0 or
v0.13.1 detects the old `~/Library/LaunchAgents/com.ntlx.serverfs.agent-bridge.plist`, performs a
bounded hand-over to the app-associated LaunchAgent, health-checks the new Bridge, and removes the
legacy plist only after the replacement is healthy. An unmanaged v0.13.1 tunnel is detected and
must be stopped before installation, preventing duplicate control-plane clients.

## Manual/debug operation

Direct commands remain useful for diagnostics, but they are not the persistent production path:

```bash
serverfs serve --config serverfs.toml
serverfs tunnel --config serverfs.toml --tunnel-id tunnel_... --api-key-file KEY
```

When `SERVERFS_FILE_INGRESS_ENABLED=true`, `serve` owns a native file-ingress helper over a private
AF_UNIX socket. The v0.13.2 managed tunnel also forwards termination to tunnel-client so the
supervisor/serve/helper tree shuts down coherently rather than becoming orphaned.

No root, no LaunchDaemon and no shell-profile dependency are introduced. launchd remains a service
manager rather than a containment kernel; the Agent Bridge recovery guard still keeps
workspace-write failed closed whenever provider state is unknown.


ACP profile configuration and operation: [ACP Guide](https://ntlx.github.io/ServerFS_MCP/docs/acp/).

## v0.25.0 pre-release — independent Shared Core Python runtime

This section is **development-only** for the unreleased macOS `bridge_mode=shared`
architecture. It does not authorize replacement of existing production services.

Choose a new physical absolute parent directory owned by the current user
with mode `0700`, outside all exposed workdirs and the repository. The runtime
destination **must not exist**. With an already populated uv cache:

```bash
# RUNTIME_PARENT must already exist and be private (0700), user-owned.
RUNTIME_PARENT="/absolute/private/user/path/serverfs-v25-runtimes"
RUNTIME_TARGET="$RUNTIME_PARENT/shared-core-01"
.venv/bin/python -m serverfs_mcp.darwin_shared_runtime \
  --target "$RUNTIME_TARGET" \
  --project-root "$(pwd -P)" \
  --source-python "$(pwd -P)/.venv/bin/python" \
  --uv "$(command -v uv)"
```

The installer uses `uv export --locked --offline` to reject a stale
`pyproject.toml`/`uv.lock` combination, builds the ServerFS wheel offline and
installs locked dependencies in a distinct venv. It pins a physical non-symlink
`bin/serverfs-core-python` with matching `libpython`, probes real `-I -B`
imports, records lock/project/requirements/wheel/Python/libpython SHA-256
provenance, and publishes atomically with Darwin `RENAME_EXCL`.
It never replaces an existing runtime and cannot register services or
provision/revoke owner Bridge grants.

You may independently revalidate the installed runtime without modifying it:

```bash
.venv/bin/python -m serverfs_mcp.darwin_shared_runtime \
  --verify-only \
  --target "$RUNTIME_TARGET" \
  --project-root "$(pwd -P)"
```

This read-only verifier checks private root and manifest permissions, no-follow
provenance JSON schema, pinned Python and libpython hashes, current lock and
project metadata hashes, and isolated imports. It **does not** hash every file
in `site-packages`, sign or activate services, or independently attest that the
checkout corresponds to a Git release tag.

Use the exact installed `bin/serverfs-core-python` for the named Shared Core
`shared-stage --core-python` input. The separate Bridge owner must provision
private v2 grants/binding. The official managed interpreter is integrity-checked
at staging **and again** before registration; non-managed development
interpreters do not receive this provenance assurance. The signed native App
still pins the exact physical Core interpreter independently of the management
CLI interpreter.

With that Core already staged and the owner Bridge v2 active, explicitly check
the entire registration gate **without registering** the native ServiceManagement
Login Item:

```bash
.venv/bin/python -m serverfs_mcp.darwin_instance_cli \
  --instance shared-core-01 shared-preflight \
  --bridge-id owner-bridge-01 \
  --config /absolute/path/to/core.toml \
  --http-port 18868
```

Success reports `ready-to-register` and `bridge_authenticated=true` after
signed App, provenance, manifest, live grant, status/PID and port checks.
This is not a ServiceManagement registration or Login Items approval. The live
Bridge may record anti-replay authentication nonces. Only explicit
`shared-start` subsequently registers that Core, never its owner Bridge.

**Validated:** locked-offline runtime install and read-only verification;
signed native App preflight; two standalone Core HTTP MCP processes sharing
one Bridge; three-process Bridge restart, grant rotation and revocation;
and the additional two signed Core Apps + live independent Bridge owner
`shared-preflight` with simulated owner outage/restart/revocation and damaged
runtime provenance. All run with the actual installed runtime, without native
ServiceManagement registration; temporary signed candidates were un-staged.
**Not validated:** two live SMAppService registered shared Cores, production
placement/exact-SHA build, public Cloudflare/ChatGPT E2E or release approval.
Do not enable the native login-item opt-in test without approved execution access.

Evidence: `docs/diagnostics/v0250-macos-shared-offline-runtime-native-preflight-2026-11-15.md`
and `docs/diagnostics/v0250-macos-shared-preflight-provenance-2026-11-15.md`.
