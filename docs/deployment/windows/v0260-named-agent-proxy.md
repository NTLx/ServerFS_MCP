# Windows — Named Bridge runtime proxy

This v0.26.0 runbook covers Windows native named `owned` / `shared` Bridge proxy policy. Use matching wheels and the create-only provisioning procedure in the [Windows native operations guide](v0250-native-operations.md). Preserve existing working services when preparing a new instance.

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

For a **fresh, isolated named Bridge** after the version-matched v0.26.0 private runtime installation and Core policy preflight, use the existing create-only helper with the explicit proxy input:

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

Before migrating a real deployment, test a disposable isolated named Core+Bridge pair with an actual local network proxy observer: prove opt-in provider traffic traverses that endpoint, opt-out provider traffic does not, and proxy outage does not leak direct traffic. Repeat through Task Scheduler/Job Object restart, verify provider child environment and stdin bootstrap, and check protected file DACL/owner, Core isolation, two workdirs with Repo Ops, foreign task denial and existing legacy/default Bridge behavior.

Native regression covers three controlled loopback synthetic-client routes. In the real Claude rejection fixture, a non-forwarding proxy observer rejected seven CONNECT requests, and the task reached its configured 45-second Bridge deadline with `AGENT_TASK_TIMED_OUT`, without harness cancellation. Process and fixture cleanup passed; this verifies the scoped deadline and observed rejection, not immediate provider failure, successful real-provider proxy/direct routing or global absence of network traffic. See the [release evidence ledger](../../releases/v0.26.0-evidence.md) for exact source and acceptance scope. Provision a fresh instance rather than editing an installed manifest in place. No production secrets or credential-bearing URLs belong in tests or Git.
