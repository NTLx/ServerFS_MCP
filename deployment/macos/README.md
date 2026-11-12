# macOS Native Deployment (v0.24.1 stable)

ServerFS **v0.24.2 is the current stable release**. Native macOS Cloudflare Tunnel shipped in v0.24.1; it is optional and does not replace the existing OpenAI Secure MCP Tunnel, stdio, Agent Bridge or their lifecycle. Native Windows Cloudflare Tunnel was added in v0.24.2. The macOS deployment shape introduced
in v0.13.2 remains pure Python over POSIX/Darwin primitives (`fcopyfile`, `getpeereid`, `confstr`
via narrow ctypes bindings); v0.13.2 adds a small headless Swift ServiceManagement companion solely for persistent
per-user service registration. There is still no `serverfs-macos-native` wheel, Docker/VM,
LaunchDaemon, root helper or Rosetta requirement. Deterministic Repo Ops shipped on Linux Host Bridge in v0.23.0, validated native Apple Silicon macOS in v0.23.1, and validated native Windows 11 x64 in v0.23.2. The existing native persistent Tunnel remains the OpenAI adapter unless the operator separately opts into the Cloudflare pair below.

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
