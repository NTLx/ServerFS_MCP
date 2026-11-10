# Optional Windows Cloudflare ingress — v0.24.2

**Release status:** Windows Cloudflare is included in the intended v0.24.2 stable release;  The public SSE gate passed with three holds of 75.0, 70.0, and 65.0 seconds, approximately 15-second keepalives, same-session `tools/list` 200 (27 tools), and new-session 200. Public HTTPS 401/200, ChatGPT tools, cross-ingress read/write/edit/delete on shared writable workdirs, and Codex/Claude passed through two independent same-user owners. The operator waived full-generation restart/recovery testing on 2026-11-10; it was not tested and did not pass. Connector-only restart and lossless SSE replay are not claimed.

Read the target version's `docs/windows-cloudflare-tunnel.md` and `deployment/windows/README.md` before acting.

## Owner selection

Each owner is a current-user Task Scheduler 2.0 lifecycle. The accepted topology runs the OpenAI default owner and a separate `ServerFS Cloudflare WorkPC` owner managed through `service cloudflare-parallel`. Each has its own venv, state, watchdog and runner; they may share intended writable workdirs and one Agent Bridge/Repo Ops service. The older `service cloudflare` path still refuses to replace an active OpenAI provider within that same owner. This is not a universal ban on independent owners and does not create a general automatic handover. If OpenAI is retired, keep the shared Agent Bridge running while Cloudflare depends on it.

Before any change inspect the status of the specific owner and its `enabled`, `watchdog_alive`, `runner_alive`, and `provider` state. Never switch an active owner. The accepted WorkPC setup leaves its OpenAI owner active and uses the distinct `ServerFS Cloudflare WorkPC` same-user owner for Cloudflare. Do not stop or repurpose the OpenAI owner. Keep the owners' venv/state/watchdog/runner separate; they may share approved workdirs and the Agent Bridge/Repo Ops service.

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

Verify actual current-user ACL and the signed binary (not only mocked tests), isolated HTTP 401/200, DNS rebinding Host/Origin denial, legitimate MCP tools and policy denials, two-child Job identity/cleanup and bounded recovery. Public Cloudflare DNS/TLS, 401/200, ChatGPT tools, cross-ingress read/write/edit/delete and Codex/Claude are accepted. The real public SSE beyond 60 seconds passed (75.0/70.0/65.0 seconds, approximately 15-second keepalives, same-session 27-tool `tools/list` 200, and new-session 200). Full-generation restart/recovery was waived by the operator on 2026-11-10; it was not tested and did not pass. This is architecture-equivalent Windows acceptance, not a Connector-only restart pass. An older in-memory session is not guaranteed to survive or replay; no zero-loss SSE replay or Windows sign-out/reboot testing is claimed. Qoder's provider error predated acceptance. Python urllib's default User-Agent received edge WAF 1010; treat that as an edge configuration caveat.

If Cloudflare credentials, the independent owner, exact-SHA release gates, or required public/SSE evidence are missing, report **not release-ready**. The accepted public tests do not authorize a general automated provider handover or replacement of the OpenAI owner.
