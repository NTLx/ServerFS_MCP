# Windows v0.26.0 — Named Bridge runtime proxy (development candidate)

> Scope: Windows native named `owned` / `shared` Bridge only. This is a feature-branch runbook; do not interpret it as installed v0.26.0 release acceptance. Preserve the current working default/v0.25.0 services and create-only private provisioning rules.

## 1. Separate proxy trust domains

The Agent runtime proxy applies **only** when an individual native provider or ACP profile explicitly sets `use_proxy = true` in the native Agent policy. It is independent of OpenAI/Cloudflare Connector egress and the Jev advisory proxy. An Agent provider with `use_proxy = false` must remain genuinely proxy-free, including when Windows has ambient proxy environment variables.

The named Bridge owner uses an existing **private stdin bootstrap frame** to transfer the validated endpoint to the Bridge process. The proxy URL must never enter the Bridge parent environment, provider credentials, `bridge.json`, Task Scheduler arguments, event logs or public MCP payloads.

## 2. Initial private proxy source

Create a small owner-private file outside every exposed ServerFS workdir and outside the project checkout. Use an absolute canonical path, a current-user/SYSTEM/Admins protected DACL and **no reparse traversal**. Example contents (substitute your actual credentialless forward proxy):

```dotenv
SERVERFS_AGENT_PROXY_URL=http://127.0.0.1:20170
SERVERFS_AGENT_NO_PROXY=example.internal
```

Both names are dedicated to Agent egress. The endpoint must use an explicit-port `http://` or `https://` URL without username/password, whitespace or fragment. Loopback addresses `127.0.0.1,localhost,::1` are always merged into `NO_PROXY`; neither a SOCKS URL nor an authenticated proxy is supported. Maximum source length: 4096 bytes. The provisioning helper requires exactly this source when any selected runtime/ACP profile enables `use_proxy=true`; it must be absent for proxy-free Bridge policies.

For a **fresh, isolated named Bridge** after the normal v0.25.0 private runtime installation and Core policy preflight, use the existing create-only helper with one new argument:

```powershell
$proxySource = 'C:\ServerFS-private\agent-proxy.env'
Invoke-SF .\deployment\windows\configure-v25-private.py `
  --core blue-owned 18782 mcp-owned.example.invalid 'C:\ServerFS-private\owned.toml' `
  --bridge-id blue-owned `
  --agent-proxy-env-file $proxySource
```

A shared owner additionally needs its existing `--bridge-policy` and Core topology options; `--agent-proxy-env-file` belongs to **the Bridge owner**, not each Core or Connector. The helper verifies the source DACL, length, allowed fields, policy opt-in parity and exclusion from the exposed root set, then copies only its normalized endpoint and NO_PROXY fields to the Bridge's user-private `agent-proxy.env` using create-only protected publication. The source is **not** persisted in `bridge.json`.

The named native owner validates its own private `bridge.json` and proxy file again at every startup, then passes the endpoint in memory through `bridge_child_environment` and `bootstrap_frame_bytes`. Missing/invalid proxy with an opted-in runtime causes a startup refusal; **never** silently fall back to direct provider egress. `use_proxy=false` runtimes remain unproxied.

## 3. Acceptance and limits

Before public acceptance, test a disposable isolated named Core+Bridge pair with an actual local network proxy observer: prove opt-in provider traffic traverses that endpoint, opt-out provider traffic does not, and proxy outage does not leak direct traffic. Repeat through Task Scheduler/Job Object restart, verify provider child environment and stdin bootstrap, and check protected file DACL/owner, Core isolation, two workdirs with Repo Ops, foreign task denial and existing legacy/default Bridge behavior.

The v0.26.0 candidate has source-level and synthetic native lifecycle regression tests; **real proxy packet traffic and live public MCP acceptance are not yet verified.** A fresh provisioned instance, not an in-place edit of an installed v0.25.0 manifest, is required for the acceptance fixture. No production secrets or credential-bearing URLs belong in tests or Git.
