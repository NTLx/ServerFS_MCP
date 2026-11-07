# Optional Windows Cloudflare ingress — v0.24.2

**Release gate:** As long as v0.24.2 has not passed published exact-SHA CI and real public MCP E2E, use this reference for development/acceptance only. The published stable v0.24.1 does not support the Windows Cloudflare lifecycle.

Read the target version's `docs/windows-cloudflare-tunnel.md` and `deployment/windows/README.md` before acting.

## Owner selection

The Windows lifecycle is **one** current-user Task Scheduler 2.0 owner. OpenAI Tunnel remains the default provider. Cloudflare uses the same Task Scheduler → watchdog → runner owner and one kill-on-close Windows Job Object for HTTP Origin and cloudflared; it is **not** a separate persistent service and cannot run alongside the existing managed OpenAI Tunnel for the same Windows login.

Before any change check `& $serverfs service status` and inspect `enabled`, `watchdog_alive`, `runner_alive`, and `provider`. Never switch an active owner. This WorkPC's live OpenAI Tunnel must not be stopped or repurposed for Cloudflare testing. Use a dedicated user/device/installation for the real lifecycle gate; isolated source tests may use disposable loopback processes and fixtures without touching the scheduled task.

## Private credentials

Only use the official pinned Windows x64 cloudflared from `serverfs bootstrap cloudflare-client` after SHA-256, Authenticode signer and version checks. Keep the connector token and the HTTP Origin env file in **separate current-user-private files outside all ServerFS workdirs and the checkout**. Verify ownership by Windows SID, protected noninherited DACL and no unauthorized allow ACE. Do not run `icacls` against user credentials as an automatic workaround or allow an unsigned/changed binary. Never place raw Token in argv, logs, task XML, Git, a message, or a ServerFS-visible directory.

The HTTP environment must choose exactly one `SERVERFS_HTTP_API_KEY` or OAuth; the Cloudflare connector token is not HTTP caller authentication. Use the exact public hostname in `SERVERFS_HTTP_ALLOWED_HOSTS` and a free loopback port.

## CLI (only after a released, version-matched Windows install)

```powershell
& $serverfs bootstrap cloudflare-client
& $serverfs service cloudflare install --config '<private-staging-config>' --cloudflared '<pinned-user-cloudflared.exe>' --token-file '<private-token-file>' --env-file '<private-http-env-file>' --http-port 18765 --hostname 'mcp.example.invalid'
& $serverfs service cloudflare status
& $serverfs service cloudflare start
& $serverfs service cloudflare status
# Intentional stop/uninstall on the matching Cloudflare provider only:
& $serverfs service cloudflare stop
& $serverfs service cloudflare uninstall
```

No generic `serverfs service restart` during an unrelated task. Stop/uninstall must be provider-guarded; on intentional stop the task is disabled first so heartbeat cannot resurrect it. Do not claim provider handover is seamless, or delete the original OpenAI configuration/credentials.

## Acceptance evidence

Verify actual current-user ACL and the signed binary (not only mocked tests), isolated HTTP 401/200, DNS rebinding Host/Origin denial, legitimate MCP tools and read-only/hidden/traversal/write rejection, two-child Job identity/cleanup and bounded recovery. For **public** Cloudflare readiness, confirm dedicated connector healthy, DNS/TLS, unauthenticated 401, authorized initialize/tools/read via ChatGPT, >60-second SSE and reconnect semantics after a connector-only restart. No claims of zero-loss SSE replay or Windows sign-out/reboot testing are required.

If Cloudflare credentials, dedicated test ownership, exact SHA gates, or public E2E evidence are missing, report **not release-ready**; do not activate a production provider.
