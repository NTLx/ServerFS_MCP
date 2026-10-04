# Phase 0F — Agent Runtime Egress Proxy (measured 2026-10-04)

Status: **GATE PASS** with one hard security boundary and one product requirement that did not exist
in the plan before this experiment (§4).

This is a Phase 0 experiment record for `dev_plan_v0.11.md` §7.1, §9, §10.2, §15/Phase 0F and the new
release acceptance items 27-32. Everything below was executed on WorkPC (Windows 11 Pro 10.0.26200).
**No model inference, no prompt, no turn.** No endpoint, host, port, URL, credential, token or account
identifier appears in this document by design: the probes classify rather than echo.

Harness: `%TEMP%\serverfs-phase0f\` (`proxy_discovery.py`, `net_health.py`, `codex_matrix.py`,
`observer.py`, `combined.py`, `lifecycle.py`, `no_proxy_honoured.py`, `sdk_inject.py`,
`claude_matrix.py`, `env_capture.py`, `cred_boundary.py`, plus a loopback fake proxy). Throwaway, not
committed.

## 1. Actual WorkPC proxy shape (redacted)

| Probe | Result |
| --- | --- |
| Process env, `HKCU\Environment`, `HKLM\...\Environment` | **no proxy variables at all** — the developer shell is not carrying a proxy, so ambient inheritance cannot be the product mechanism |
| WinINet | a proxy is *configured* but `ProxyEnable = 0` (disabled); provider tooling does not consume WinINet |
| WinHTTP | direct (no named proxy server) |
| Repo `.env` | the Tunnel namespace exists: `SERVERFS_PROXY_HOST` + `SERVERFS_PROXY_PORT` only — **no username/password keys** |
| Raw probe of that endpoint | HTTP `CONNECT` proxy on a **public-network** address (not loopback, not RFC1918), `CONNECT api.openai.com:443` → status `200` **without any credential**, then TLS 1.3 |

Classification: **HTTP (CONNECT) forward proxy, public-network endpoint, auth: none required.**

Provider reachability against that shape (this is the constraint the new product capability exists for):

| Destination | Direct | Through the proxy |
| --- | --- | --- |
| `api.openai.com:443` | **TIMEOUT** | CONNECT 200 + TLS 1.3 |
| `chatgpt.com:443` | **TIMEOUT** | CONNECT 200 + TLS 1.3 |
| `auth.openai.com:443` | connected | CONNECT 200 + TLS 1.3 |

So a true no-proxy baseline **is** constructible on this host (§8.1 of the task): with proxy variables
scrubbed, the provider's own health report fails. No "baseline isolation not possible" caveat needed.

## 2. Codex: health with and without the proxy, through the provider's own redacted report

`codex doctor --json` is a provider-supported, self-redacting health report. Check names and statuses
only:

| Configuration | rc | `network.provider_reachability` | `network.websocket_reachability` | `network.env` |
| --- | --- | --- | --- | --- |
| scrubbed (no proxy variables) | 1 | **fail** | warning | ok |
| `HTTPS_PROXY` + `HTTP_PROXY` = proxy | 0 | **ok** | **ok** | ok |
| `https_proxy` only (lower-case) | 0 | **ok** | **ok** | ok |
| derived from the dedicated Agent namespace (`SERVERFS_AGENT_PROXY_URL` mapped to `HTTPS_PROXY`) | 0 | **ok** | **ok** | ok |
| `HTTPS_PROXY` = a **local fake forwarder** that chains to the real proxy | 0 | **ok** | **ok** | ok |

Conclusions:
- The proxy is what makes Codex/OpenAI reachable here, and one variable is enough.
- Case-insensitive: upper-case and lower-case forms both work.
- `HTTP_PROXY` alone is **not sufficient** (1 tunnel attempt, provider check still `fail`); HTTPS
  destinations need `HTTPS_PROXY` (or `ALL_PROXY`, which also worked alone).
- The credentialless local-forwarder chain works end to end, which is the technical proof that the
  broker architecture (§5) is viable rather than speculative.

## 3. Which variables Codex actually consumes (observer proxy, no real egress needed)

A loopback observer proxy returning `502` was configured as the proxy, and Codex's health path was run
with a single variable at a time. "Tunnel requested" means the observer received a `CONNECT` for an
external target:

| Variable set alone | tunnel requested | note |
| --- | --- | --- |
| `HTTPS_PROXY` | yes (3 attempts) | sufficient |
| `https_proxy` | yes (3) | case-insensitive |
| `HTTP_PROXY` | yes (1) | insufficient for HTTPS destinations |
| `http_proxy` | yes (1) | ditto |
| `ALL_PROXY` | yes (3) | sufficient |
| `all_proxy` | yes (3) | sufficient |
| none | no | baseline |

**Frozen minimum for Codex: `HTTPS_PROXY` + `NO_PROXY`.** No other variable is required by the
installed 0.159.2 build; `HTTP_PROXY`/`ALL_PROXY` are honoured but not needed, and injecting more
than necessary would only widen the surface.

`NO_PROXY` is genuinely honoured by Codex: after excluding the provider domains the observer had
itself reported (no host names recorded in evidence), the observer saw **zero** tunnel requests and
the provider check returned `fail` again — i.e. Codex really bypassed the proxy for excluded
destinations rather than ignoring `NO_PROXY`.

## 4. Loopback control must never enter the egress proxy — measured in both directions

**Provider side.** Bridge-shaped app-server started with
`--listen ws://127.0.0.1:<ephemeral> --ws-auth capability-token --ws-token-file <private>`, with
`HTTPS_PROXY`/`HTTP_PROXY` pointing at the observer and `NO_PROXY` set to the mandatory local set:

- listening: yes; anonymous connection refused; `initialize` ok; `model/list` returned 8 models;
- observer saw 17 external tunnel attempts and **0 loopback-target attempts** ✓;
- `model/list` still succeeded through the *local* control path while the egress proxy was a broken
  502 sink, which also proves the model catalog on this path is served locally, not from the provider.

**Client side (`websockets`, the library the frozen transport uses).** Local WS server, observer proxy
configured, three loopback spellings (`127.0.0.1`, `localhost`, `[::1]`):

| Configuration | result |
| --- | --- |
| `proxy=None` (the shape `codex_transport.py` already uses) | connects **direct** for all three spellings; observer sees nothing |
| library default, **no** `NO_PROXY` | **the loopback destination is routed through the proxy** and fails (`InvalidProxyStatus: HTTP 502`) for all three spellings |
| library default + `NO_PROXY=127.0.0.1,localhost,::1` | connects direct; observer sees nothing |
| library default + `NO_PROXY=""` (empty) | routed through the proxy again |

This is the most dangerous assumption Phase 0F removed: a future code path that drops `proxy=None`, or
an operator-supplied empty `NO_PROXY`, silently breaks the Bridge→Codex control channel the moment the
Agent proxy is enabled. Two independent belts are therefore mandatory, not redundant:

1. the Bridge's own Codex client keeps `proxy=None` on the control connection;
2. the effective `NO_PROXY` given to runtimes always contains `127.0.0.1`, `localhost`, `::1`, merged
   with (never replaced by) the operator value.

Note also observed: `websockets==17.1` logs noisy asyncio `Fatal error` tracebacks when a proxy rejects
a tunnel; harmless for us (we never take that path), but it must not be mistaken for a transport bug.

## 5. Credential boundary — the reason authenticated proxy support is NOT shipped by env injection

| Measurement | Result |
| --- | --- |
| Does the provider child send credentials itself when the proxy URL contains userinfo? | **yes** — the observer recorded `Proxy-Authorization` present on 3 attempts (`CRED-userinfo-forwarded`) |
| Does an injected environment variable reach tool code executed by the runtime? | **yes** — a grandchild process saw the sentinel (`CRED-descendant-inherit`: child sees, grandchild sees) |
| Can an unrelated process of the same Windows user open a runtime process with `PROCESS_QUERY_INFORMATION \| PROCESS_VM_READ`? | **yes** (`CRED-same-user-inspect`: opened, no error) — the same rights a debugger needs |
| Actual WorkPC proxy | **auth: none**, so nothing above is currently triggered by the deployment |

Verdict: putting an authenticated proxy credential into a provider child's environment is **not** a
credential boundary — the secret is readable by agent-executed code and by any same-user process. The
task's stop condition (§13) applies to the authenticated case, and because the real proxy needs no
credential the gate can close without shipping that path.

The approved shape if an authenticated upstream is ever required is a **local credential broker**:
the provider child gets a credentialless `http://127.0.0.1:<ephemeral>` forwarder owned by ServerFS,
which chains to the authenticated upstream and holds the credential in its own process state only.
Measured viable (§2, `CHAIN-local-forwarder`); **not implemented** in v0.11 without a maintainer
decision, and it interacts with the Job Object and the §6 private-state rules.

## 6. Claude and Qoder: who owns the network, and where the proxy must go

**Trigger selection first, because it decides what can be claimed.** Claude's
`claude auth status` and `claude doctor` produced **zero** proxy events (they are local
credential/settings reads), so Claude's provider-credential path could not be observed without a real
request — and a real request is inference, which Phase 0F forbids. Qoder, by contrast, is fully
observable through `connect()` + `get_available_models()`.

| Runtime | Network-owning process | Minimum injection point | Measured |
| --- | --- | --- | --- |
| Codex | the Bridge-spawned `codex app-server` child (`codex.exe`) | the explicit env dict the Bridge passes when it spawns that child | 17 external tunnel attempts, 0 loopback, initialize/model-list ok |
| Qoder | the **`qodercli.exe` child**, not the Python SDK process | `QoderAgentOptions.env` (child-scoped) — and the parent env also reaches it | 37-39 tunnel requests attributed to `qodercli.exe` by the observer, in both parent-only and child-only cases; `get_available_models()` returned 17 models in every configuration |
| Claude | the `claude.exe` child (the SDK↔CLI channel is stdio only; the SDK made no provider connection during connect/control) | `ClaudeAgentOptions.env` | control path unaffected by proxy settings (connect, `get_server_info`, interrupt, disconnect all ok, 0 proxy events); provider egress consumption **not yet observable without a turn** → Phase G |

Two further Qoder facts: its provider endpoint is **directly reachable** on this host (baseline and
catalog both worked with no proxy), and when the configured proxy fails it **falls back to direct**
(the catalog still returned 17 models against a 502 sink after ~8 s). So `use_proxy` for Qoder is a
policy choice, not a connectivity requirement here, and a proxy failure is not fatal for Qoder.

## 7. The finding that changes Phase D's design: wholesale environment inheritance

The observer-style capture of the exact child environment each SDK builds (white-box: the SDK's
`anyio.open_process` call was inspected at the moment it constructed the child env, then the session
was aborted; no CLI ran, no network was used) gave:

| SDK | child environment | a Tunnel-namespace variable placed in the Bridge process | explicit removal available? |
| --- | --- | --- | --- |
| `claude-agent-sdk` 0.2.156 | inherited wholesale from `os.environ` (minus `CLAUDECODE`), then `CLAUDE_CODE_ENTRYPOINT`, then `options.env` overrides | **present in the child** in every case (91 keys from 88-89 parent keys) | **no** — `options.env` can add or overwrite, never unset |
| `qoder-agent-sdk` 1.0.15 | inherited wholesale (minus `QODER`), then `QODER_ENTRYPOINT`, then `options.env` | **present in the child** in every case (94 keys) | **yes** — `env={NAME: None}` deletes it (measured: child ended with 92 keys, no proxy variable, no namespace variable) |

Consequences, now frozen into the plan:

1. **The Bridge process environment must never contain Tunnel / Control Plane secrets.** With either
   SDK they would be copied into the provider CLI child, and from there into every tool/command the
   Agent runs. Scrubbing at spawn time is not sufficient for Claude, because it has no removal
   mechanism — the value must not be in the Bridge env in the first place. This is a supervisor
   requirement (the native `serverfs tunnel` chain already strips env for the tunnel-client; the same
   discipline must apply to the Bridge child it starts).
2. **The Bridge must not carry any proxy variable either.** A `use_proxy=false` runtime cannot be
   cleaned under Claude, so per-runtime proxy policy must be applied *downward* (child env only),
   never *upward* into the Bridge.
3. The environment builder must set proxy variables exclusively in the per-child env it constructs:
   `HTTPS_PROXY` + merged `NO_PROXY` for runtimes with `use_proxy=true`; for Qoder it may additionally
   pass `None` to defend against inheritance; for Codex the Bridge owns the spawn and controls env
   exactly.

## 8. Diagnostics redaction

| Measurement | Result |
| --- | --- |
| `codex doctor --json` with the proxy configured | endpoint host **absent**, port substring absent, `http://` absent (15 219-byte report) |
| `codex doctor --summary` | endpoint host absent (2 126 bytes) |

So a `serverfs doctor` proxy section can report provider-style status safely. Frozen doctor surface:
`Agent proxy: enabled/disabled`, `source: env`, `auth: none`, per-runtime
`enabled/reachable/not required`, `local bypass: OK` — and never the URL, host, port, user, password
or token, including in debug mode.

## 9. Lifecycle facts re-measured here

- The provider child does **not** delete the `--ws-token-file`; after `terminate()` the file was still
  present and had to be removed by the harness. Token-file deletion is Bridge-owned teardown work.
- The ephemeral loopback port was released on child termination (`port_released_after_terminate=True`).
- Spawn + terminate of the Bridge-shaped app-server left **no new** `codex.exe` process
  (`new_codex_processes=[]`). The four `codex.exe` processes present on this host are the
  provider-managed daemon (started Oct 2) and two pre-existing stdio app-servers (started 16:00) — all
  untouched by these experiments, as required.

## 10. Frozen contract (Phase 0F output)

```toml
[agent.proxy]
enabled = false        # default off; v0.11 supports exactly one source
source = "env"         # SERVERFS_AGENT_PROXY_URL (+ optional SERVERFS_AGENT_NO_PROXY)

[agent.codex]
use_proxy = true       # required on WorkPC: provider egress is unreachable direct

[agent.claude]
use_proxy = false      # not required here; decision must not be a global "always proxy"

[agent.qoder]
use_proxy = false      # measured directly reachable; proxy honoured but falls back to direct
```

- Dedicated variable names: `SERVERFS_AGENT_PROXY_URL` (required when enabled),
  `SERVERFS_AGENT_NO_PROXY` (optional). Endpoint never enters `serverfs.toml`.
- Validation at parse time: absolute `http://` (or `https://`) URL, host and port present, and
  **userinfo rejected** — a credential in the URL is not a supported product shape (§5).
- Mapping: `SERVERFS_AGENT_PROXY_URL` → child `HTTPS_PROXY`; mandatory local set
  `{127.0.0.1, localhost, ::1}` ∪ operator `SERVERFS_AGENT_NO_PROXY` → child `NO_PROXY`. The operator
  value merges and can never remove a mandatory entry, and an empty operator value must not disable
  the bypass (both measured).
- No bulk parent-env inheritance for provider children, and no provider gets `SERVERFS_AGENT_PROXY_*`
  itself (measured: providers do not consume those names; §2 `ISO-agent-namespace`).
- Tunnel `SERVERFS_PROXY_*` / Control Plane credentials are a separate trust domain and are not read
  by the Agent proxy path (measured: with only the Tunnel namespace set, Codex health stays `fail`).
- Bridge→Codex control connection keeps `proxy=None` **and** relies on the merged `NO_PROXY` (§4).
- Authenticated upstream: out of v0.11 scope as env-injected; only an external credentialless local
  forwarder/broker is an acceptable future shape (§5). Release docs must state
  "credentialless / local-broker runtime proxy supported" and must not claim authenticated support.

## 11. Gate checklist

| Required by the gate | Evidence | Result |
| --- | --- | --- |
| WorkPC proxy shape measured | §1 | PASS (HTTP CONNECT, public endpoint, auth none) |
| Codex health through the proxy | §2 | PASS (`provider_reachability=ok`, rc 0) |
| Codex no-proxy baseline | §1/§2 | PASS (`fail`, rc 1) — true baseline constructible |
| Local control + egress proxy coexist | §4 | PASS (0 loopback tunnel attempts, initialize/model-list ok) |
| localhost / `NO_PROXY` correctness | §3, §4 | PASS (provider honours `NO_PROXY`; client-side `proxy=None` + merge both proven) |
| Codex env variables frozen | §2, §3 | PASS (`HTTPS_PROXY` + `NO_PROXY`, upper/lower case accepted, `HTTP_PROXY` alone insufficient) |
| Claude injection point | §6, §7 | PASS for scope (`ClaudeAgentOptions.env`, child owns network); provider egress consumption deferred to Phase G with the reason recorded |
| Qoder injection point | §6, §7 | PASS (`QoderAgentOptions.env`, `qodercli.exe` owns the connections; `None` removal available) |
| Tunnel proxy vs Agent proxy independence | §2, §10 | PASS (name-separated, neither feeds the other) |
| Safe diagnostics redaction | §8 | PASS |
| Credential security conclusion | §5 | PASS with boundary: auth-less proxy supported; authenticated env-injected proxy **rejected by measurement**; broker is the only future shape |

## 12. Not verified / open

- Whether `claude.exe` itself honours standard proxy variables — no non-inference trigger exists on
  this host (§6). Must be settled during Phase G live smoke, and WorkPC's custom Claude endpoint
  configuration must not be generalized to standard Anthropic environments.
- Authenticated-proxy broker: measured technically viable (chain), **not designed or implemented**;
  needs a maintainer decision plus Job Object and §6 private-state interaction review.
- The Bridge's own advisory (Jev) HTTP client egress is a separate question from runtime egress and
  was not in 0F's scope; Phase D must decide whether Jev reuses the dedicated Agent proxy contract or
  stays direct.
- Machine-wide state: none was touched — WinINet/WinHTTP were read only, registry values never written,
  no provider persistent configuration changed.
