# Native Windows Cloudflare Tunnel — v0.24.2 operator and acceptance runbook

> **Release status:** v0.24.2 adds native Windows Cloudflare. Two independent same-user Task Scheduler owners passed the recorded public ChatGPT, file-operation and 75.0/70.0/65.0-second SSE acceptance. The operator waived full-generation restart/recovery testing on 2026-11-10; it was not tested and did not pass. Connector-only restart and lossless SSE replay are not claimed.

## Scope and ownership

Windows 11 x64, local NTFS, current-user non-elevated native ServerFS. The optional Cloudflare ingress is outbound-only: Cloudflare HTTPS edge → cloudflared → `http://127.0.0.1:<port>/mcp` → ServerFS HTTP caller authentication (exactly one of API Key or OAuth) → unchanged Workdir policy. A Tunnel Token authenticates **only** the Cloudflare connector, never MCP clients.

Each managed Windows service owner selects **one provider**. The accepted v0.24.2 deployment runs two independent same-user Task Scheduler 2.0 owners: the OpenAI native default and a separately named `ServerFS Cloudflare WorkPC` owner managed with `service cloudflare-parallel`. Each has its own venv, state, watchdog and runner; both can use the same writable workdirs and share one Agent Bridge and Repo Ops service. The older single-owner `service cloudflare` path still refuses a provider switch while the OpenAI owner is active; this is an owner-scoped guard, not a ban on two independent owners. There is no universal automated handover. If the OpenAI owner is retired, keep the shared Agent Bridge running while the Cloudflare owner depends on it. No SCM/LocalSystem service, administrator/UAC or PATH modification is introduced.

**Production isolation gate:** Do not run single-owner `service cloudflare install/start/stop/uninstall` against an active OpenAI owner. The accepted parallel deployment uses the distinct `service cloudflare-parallel` owner and separate private runtime/state; it does not perform a provider handover or replace the OpenAI owner. The public ChatGPT acceptance proved HTTPS 401/200, tool calls, cross-ingress read/write/edit/delete on the shared writable workdirs, and Codex/Claude. Qoder's provider error predated this acceptance. Python urllib's default User-Agent was rejected by the edge WAF (1010), an edge configuration caveat. The real public SSE gate passed: three public holds lasted 75.0, 70.0, and 65.0 seconds with approximately 15-second keepalives; same-session `tools/list` returned 200 with 27 tools and a new session returned 200. Windows HTTP origin and cloudflared share one kill-on-close Job generation. Full-generation restart/recovery was waived by the operator on 2026-11-10; it was not tested and did not pass. This is architecture-equivalent Windows acceptance, not a Connector-only restart pass. An older in-memory session is not guaranteed to survive or replay. Do not generalize acceptance to every provider or claim lossless SSE replay.

## Preconditions

- Clean, version-matched `serverfs-mcp` + `serverfs-windows-native` installation in the supported per-user `%LOCALAPPDATA%\ServerFS\connector-env`; `serverfs service install` refuses a checkout `.venv` and native/core version mismatch.
- User-created `serverfs.toml` with controlled test workdirs, all real directories and no symlink/junction/reparse components. Keep them read-only except for the explicitly authorized shared writable workdirs used in mutation acceptance.
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

The commands below use the existing single-owner `service cloudflare` path. That path refuses to replace an active OpenAI provider in that owner. The accepted parallel setup uses the separate `service cloudflare-parallel` owner; use its version-matched CLI help for the precise lifecycle syntax and keep its venv/state/watchdog/runner separate. Do not interpret the single-owner guard as support for automatic handover.

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

The single-owner command is **not a production migration instruction**; it refuses to replace OpenAI when that provider is active in the same owner. The accepted parallel setup uses its separate owner and leaves OpenAI untouched. `service cloudflare install` is transactional with task/manifest rollback on failure and preserves existing disabled/enabled state only after checking that the prior provider is stopped. Task XML stores credential **paths**, never raw tokens. The runner revalidates pinned binary, config, token and HTTP environment on startup.

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
4. **Lifecycle:** independent same-user Task Scheduler owner, non-admin installation, clean start/stop, two-child Job containment, no orphan processes, bounded restart/backoff and deliberate-stop semantics. Keep the OpenAI owner untouched; preserve the shared Agent Bridge while Cloudflare depends on it. Do not simulate an actual user logout/login or machine restart solely for this gate.
5. **Cloudflare public ingress:** distinct test hostname with valid DNS/TLS and outbound Connector, unauthenticated public 401, authorized 200 and direct ChatGPT tool calls. Verify a real public SSE stream remains live beyond 60 seconds. On Connector failure, verify bounded teardown/restart of the full HTTP origin + cloudflared generation and then verify fresh-session recovery. This architecture-equivalent Windows acceptance does not test or pass a Connector-only restart; the older in-memory session is not guaranteed to survive or replay.
6. **Release:** exact commit SHA; Windows Native + Windows Agent, Linux, macOS and website CI; seven Skills, deterministic Plugin ZIP, bilingual README/site/docs, native wheel and version surfaces. Run release workflow `workflow_dispatch` on `main` only **after** ordinary CI. The workflow must create the immutable tag only after all gates pass.

## Known test boundaries

Local Python/Rust pytest, mocked Task Scheduler semantics and disposable loopback HTTP smoke tests **do not establish** real Cloudflare connectivity, Authenticode acceptance of an actual downloaded artifact, private ACL behavior for actual deployment secrets, public ChatGPT interoperability or long-lived SSE recovery. Do not mark those gates `pass` without corresponding real evidence.

## Optional second, independently owned Cloudflare instance

The v0.24.2 `service cloudflare-parallel` command manages a separate **WorkPC-specific** same-user Windows instance. It never replaces the default `ServerFS Native Tunnel` owner, and it does not create a second Agent Bridge. This opt-in mode uses a separate `ServerFS Cloudflare WorkPC` Task Scheduler registration, `%LOCALAPPDATA%\\ServerFS\\instances\\workpc-cloudflare` environment/state/watchdog/runner, and a dedicated loopback HTTP origin on `127.0.0.1:18765`. Its ServerFS configuration may reference the **same explicitly authorized read-write workdirs**, Agent and Repo Ops policy as the default service. The remote Tunnel hostname and credentials remain operator-managed.

Use the **v0.24.2** binary installed in the parallel instance's own `connector-env` (not the default OpenAI venv), and retain the official pinned `cloudflared` and two private credential files outside all exposed workdirs:

```powershell
$parallel = "$env:LOCALAPPDATA\\ServerFS\\instances\\workpc-cloudflare\\connector-env\\Scripts\\serverfs.exe"
$config = 'C:\\path\\to\\existing\\serverfs.toml'
$cloudflared = "$env:LOCALAPPDATA\\ServerFS\\bin\\cloudflared-2026.10.7-windows-amd64\\cloudflared.exe"
$tokenFile = 'C:\\Users\\example\\private\\cloudflare.token'
$httpEnvFile = 'C:\\Users\\example\\private\\http-origin.env'
& $parallel service cloudflare-parallel install --config $config --cloudflared $cloudflared --token-file $tokenFile --env-file $httpEnvFile
& $parallel service cloudflare-parallel status
& $parallel service cloudflare-parallel start
& $parallel service cloudflare-parallel status
# Intentional stop / rollback of ONLY the isolated Cloudflare owner:
& $parallel service cloudflare-parallel stop
# & $parallel service cloudflare-parallel uninstall
```

The optional parallel installer registers its own task **disabled** and does not automatically start the Connector. `start` verifies the frozen installation contract; `stop` disables its own task before shutting down its managed tree. Only use this path when the instance identity, task and venv are correct. Never run the single-owner `service cloudflare install` against an active OpenAI owner. The shared Agent Bridge is still supervised through the existing service; do **not** retire that owner merely because the parallel HTTP ingress works. Validate independent Bridge supervision before any permanent provider cutover.

References: [Windows deployment](../deployment/windows/README.md), [Windows native site guide](../site/src/content/docs/docs/windows-native.md), [Cloudflare Linux](cloudflare-tunnel.md), [v0.24.2 implementation plan](../dev_plan_v0.24.2.md), [maintainer release workflow](maintainers/workflow.md).
