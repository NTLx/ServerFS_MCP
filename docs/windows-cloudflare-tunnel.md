# Native Windows Cloudflare Tunnel — v0.24.2 operator and acceptance runbook

> **Release state:** v0.24.2 development/acceptance. This runbook describes the implementation under test, **not** a published Windows Cloudflare release. Do not report it as released until the exact-SHA Windows, cross-platform, public MCP and Release workflow gates pass. v0.24.1 remains the published stable baseline.

## Scope and ownership

Windows 11 x64, local NTFS, current-user non-elevated native ServerFS. The optional Cloudflare ingress is outbound-only: Cloudflare HTTPS edge → cloudflared → `http://127.0.0.1:<port>/mcp` → ServerFS HTTP caller authentication (exactly one of API Key or OAuth) → unchanged Workdir policy. A Tunnel Token authenticates **only** the Cloudflare connector, never MCP clients.

The implementation selects **one provider per existing managed Windows service**. Both providers use the existing current-user Task Scheduler 2.0 → watchdog → managed runner → kill-on-close Windows Job Object lifecycle. Installing Cloudflare on a Windows service currently running OpenAI Tunnel is **refused**. No second persistent watchdog, SCM/LocalSystem service, administrator/UAC, PATH modification, or automatic provider takeover is supported. This differs from the macOS implementation's separate SMAppService opt-in pair.

**Production isolation gate:** Do not run `service cloudflare install/start/stop/uninstall` against the continuously used WorkPC OpenAI Tunnel. All v0.24.2 source acceptance on that host uses disposable read-only test fixtures, an unrelated loopback port, a freshly built Windows Rust extension, and bounded child processes. A live Cloudflare service acceptance needs a dedicated **separate Windows installation/user/device** with its own user-scoped service owner and a distinct test hostname and token. Do not claim a simulated integration test is public ChatGPT acceptance.

## Preconditions

- Clean, version-matched `serverfs-mcp` + `serverfs-windows-native` installation in the supported per-user `%LOCALAPPDATA%\ServerFS\connector-env`; `serverfs service install` refuses a checkout `.venv` and native/core version mismatch.
- User-created `serverfs.toml` with **read-only** test workdirs, all real directories and no symlink/junction/reparse components.
- Official fixed `cloudflared-windows-amd64.exe`, version `2026.10.7`, verified against release SHA-256 `2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343` **and** a valid Cloudflare Authenticode signer. A local artifact is allowed only after the exact same checks; never bypass verification after download failure. A previously installed executable with an invalid digest is rejected instead of overwritten.
- Distinct Cloudflare remotely managed Tunnel and public hostname owned by the operator, with its HTTP origin configured as `http://127.0.0.1:<port>` **on the Windows host** (not the Linux Docker hostname). No public inbound host port.
- Tunnel Token file and HTTP environment file are separate files, absolute paths **outside the checkout and all configured ServerFS workdirs**, owned by the current Windows user. Their DACL must be **protected against inheritance**, contain no inherited ACEs or foreign allowed SID, and allow only the current-user SID, LOCAL SYSTEM and BUILTIN Administrators. The CLI validates them and fails closed; it does not repair pre-existing ACLs.
- The local port is free; `SERVERFS_HTTP_ALLOWED_HOSTS` is exactly the external hostname. Avoid disabling HTTP Host/Origin checking.

## Configuration (placeholders only)

Create a dedicated native `serverfs.toml` in a disposable test deployment directory with the intended readonly fixture. Store the Connector Token in a user-private file. A second private file for the HTTP origin contains:

```dotenv
SERVERFS_HTTP_API_KEY=<independently-generated-long-random-key>
SERVERFS_HTTP_ALLOWED_HOSTS=mcp.example.invalid
SERVERFS_OAUTH_ENABLED=false
```

Alternatively use the documented OAuth settings with a verified token introspection secret and `SERVERFS_OAUTH_ENABLED=true`, **without** setting an HTTP API Key. Never reuse the Tunnel Token for HTTP authentication or include its value in CLI args, task XML, Git, terminal history, logs, support requests, or a ChatGPT conversation.

```powershell
$serverfs = "$env:LOCALAPPDATA\ServerFS\connector-env\Scripts\serverfs.exe"
& $serverfs --version
& $serverfs bootstrap cloudflare-client
# Or: & $serverfs bootstrap cloudflare-client --artifact 'C:\path\to\verified\cloudflared-windows-amd64.exe'

$cloudflared = "$env:LOCALAPPDATA\ServerFS\bin\cloudflared-2026.10.7-windows-amd64\cloudflared.exe"
# All paths below are placeholders for a DEDICATED, non-production install:
$config = 'C:\serverfs-cloudflare-staging\serverfs.toml'
$token = 'C:\Users\example\private\cloudflare.token'
$originEnv = 'C:\Users\example\private\http-origin.env'

& $serverfs service cloudflare install --config $config --cloudflared $cloudflared --token-file $token --env-file $originEnv --http-port 18765 --hostname mcp.example.invalid
& $serverfs service cloudflare status
# Install must not silently start the service.
& $serverfs service cloudflare start
& $serverfs service cloudflare status
```

The command is **not a production migration instruction**; stop here if the existing OpenAI provider is enabled, watchdog/runner is alive, or any task ownership cannot be established. `service cloudflare install` is transactional with task/manifest rollback on failure and preserves existing disabled/enabled state only after checking that the prior provider is stopped. Task XML stores credential **paths**, never raw tokens. The runner revalidates pinned binary, config, token and HTTP environment on startup.

## Native lifecycle and recovery

- `serverfs service cloudflare status` reports the current single-service provider plus frozen-identity liveness for HTTP origin and Connector, if available. Do not equate `enabled=true` with healthy processes.
- `serverfs service cloudflare stop` **disables Task Scheduler first**, then stops the owned task and managed process tree; heartbeat must not resurrect an intentional stop.
- `serverfs service cloudflare restart` is a stop/start of the same provider. An HTTP Origin failure, failed readiness probe or Connector exit terminates the current Job generation and is subject to bounded runner restart backoff.
- `serverfs service cloudflare uninstall` is guarded by the provider manifest and must never uninstall an active OpenAI provider. It removes only current service-manager artifacts; external operator credential files remain operator-owned.
- To return a dedicated test machine to OpenAI mode, first stop/uninstall the Cloudflare provider and then explicitly reinstall the OpenAI provider with its original `serverfs.toml` and `.env` files. Revalidate its native service state. An operator must retain those original configuration paths and secret files in advance; automatic provider-history rollback is not implemented.

## Layered release acceptance

1. **Build:** Windows x64 `cargo build --locked --release --features pyo3`, current build staged into a **fresh** `serverfs_windows_native.pyd` directory, and Python confirms `__file__` points there. A stale `.pyd` is not acceptable evidence.
2. **Security:** actual current-user SID/protected DACL inspection, junction/reparse rejection, token not present in argv/logs/manifest, exact pinned cloudflared SHA-256 + Authenticode + version. Negative ACL, credentials-inside-workdir and bad-binary cases must fail closed.
3. **Native HTTP:** no authentication → HTTP 401; valid API Key/OAuth → authenticated `initialize` HTTP 200, tools/list + read-only fixture read. Validate Host/Origin rejection and forbidden hidden/traversal/write paths.
4. **Lifecycle:** separate test owner, non-admin installation, clean start/stop, two-child Job containment, no orphan processes, bounded restart/backoff and deliberate-stop semantics. Verify no interference with existing production OpenAI Tunnel / Agent Bridge; do not simulate an actual user logout/login or machine restart solely for this gate.
5. **Cloudflare public ingress:** distinct test hostname with valid DNS/TLS and outbound Connector, unauthenticated public 401, authorized 200 and direct ChatGPT tool calls. Verify SSE remains live >60 seconds and distinguish fresh-session recovery vs older-session behavior after **Connector-only** restart; do not claim lossless in-flight SSE replay.
6. **Release:** exact commit SHA; Windows Native + Windows Agent, Linux, macOS and website CI; seven Skills, deterministic Plugin ZIP, bilingual README/site/docs, native wheel and version surfaces. Run release workflow `workflow_dispatch` on `main` only **after** ordinary CI. The workflow must create the immutable tag only after all gates pass.

## Known test boundaries

Local Python/Rust pytest, mocked Task Scheduler semantics and disposable loopback HTTP smoke tests **do not establish** real Cloudflare connectivity, Authenticode acceptance of an actual downloaded artifact, private ACL behavior for actual deployment secrets, public ChatGPT interoperability or long-lived SSE recovery. Do not mark those gates `pass` without corresponding real evidence.

References: [Windows deployment](../deployment/windows/README.md), [Windows native site guide](../site/src/content/docs/docs/windows-native.md), [Cloudflare Linux](cloudflare-tunnel.md), [v0.24.2 implementation plan](../dev_plan_v0.24.2.md), [maintainer release workflow](maintainers/workflow.md).
