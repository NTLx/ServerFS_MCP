# Windows Phase E — Codex runtime

Status: **OPEN** (not CLOSED-PASS). The implementation and the deterministic Windows suite are
landed, and the real runtime probe and model discovery both pass against the installed CLI.
Closure is blocked because **the Codex CLI cannot complete a provider round trip on this host at
all** — demonstrated with every ServerFS component removed, so it is not a ServerFS defect. That
distinction is the whole content of this document, so it is stated first and not softened at the end.

- Branch: `v0.11-phase-e-windows-codex`
- Base: `bc3500fce28453c77118c265c51aa0d84b5580d2` (post-Phase-D main)
- Codex CLI measured on this host: **`codex-cli 0.159.2`**

## 1. What is landed

| Piece | Commit | What it does |
| --- | --- | --- |
| Authenticated loopback transport | `105646a` | `CodexConnection` takes a `CodexEndpoint`; the RPC engine stays single |
| Bridge-owned app-server lifecycle | `477a485` | endpoint, child process, capability token |
| Adapter wiring | `0162ebf` | every entry point acquires the platform endpoint |
| Deterministic Windows suite | `98798c4` | the adapter suite runs on Windows for the first time |
| Codex doctor diagnostics | `f52fab6` | read-only CLI checks |

No Qoder and no Claude Windows provider integration is included. That is Phase F and Phase G.

## 2. Protocol-drift preflight (§4) — PASS

`codex app-server --help` on 0.159.2 still carries, with unchanged meaning:

```
--listen <URL>            ws://IP:PORT among the supported transports
--ws-auth <MODE>          capability-token | signed-bearer-token
--ws-token-file <PATH>    absolute path to the capability-token file
--ws-token-sha256 <HEX>   still mutually exclusive with the above, as Phase 0C recorded
```

No drift. No STOP condition from §64 was triggered by the transport flags.

## 3. Port strategy (§5) — the plan's fallback was unnecessary

The plan anticipated that port 0 might be rejected and prescribed: reserve an OS ephemeral port,
close the reservation, spawn, then retry a narrow bind race up to three times. **Measurement found
an official channel that makes all of that unnecessary**, and the difference matters because the
fallback would have invented a race the provider does not have:

| Question | Measurement |
| --- | --- |
| Is `--listen ws://127.0.0.1:0` accepted? | **Yes.** The provider binds an OS-assigned loopback port |
| Is the bound port officially obtainable? | **Yes.** The CLI prints it on stderr |
| Anonymous client | refused, HTTP 401 |
| `Authorization: Bearer <token>` | accepted |
| Token in argv / in child output | no / no |
| Teardown | listener released, no port left LISTENING (3/3 runs) |

The announcement, verbatim:

```
codex app-server (WebSockets)
  listening on: ws://127.0.0.1:<port>
  readyz: http://127.0.0.1:<port>/readyz
  healthz: http://127.0.0.1:<port>/healthz
  note: binds localhost only (use SSH port-forwarding for remote access)
```

The announced port was cross-checked against an OS socket diff on every run and always agreed. The
implementation therefore parses the child's own stderr, and additionally re-checks the host in the
URL rather than trusting the printed note.

### Listener latency: 0.26–0.28 s, not the 24.4 s Phase 0C recorded

Four independent spawns: 0.28 / 0.26 / 0.27 / 0.19 s from spawn to a successful authenticated
handshake, against Phase 0C's 24.36 / 24.37 / 24.38 s. The discrepancy is recorded rather than
absorbed and **its cause is not established**; no claim is made about why. The 60 s readiness bound
from §21 is kept: it is now over-provisioned rather than wrong, and a bounded wait costs nothing
when readiness is real.

### `/readyz` is not a readiness signal

`/readyz` and `/healthz` answer **200 on loopback without the capability token**, and are already
200 at the instant the port accepts a connection. They gate nothing. Readiness remains an
authenticated WebSocket connect plus `initialize`, which is also the only check that proves the
token file was accepted. Using the unauthenticated 200 would have been strictly weaker.

## 4. Deterministic coverage

`agent_bridge/tests/test_codex_windows_runtime.py`, no real Codex and no inference: the endpoint
refuses anything but literal `127.0.0.1` with an explicit port; the token never appears in `repr`;
an authenticated round trip routes RPC, events and a server request; missing and wrong bearers are
refused; connection loss fails pending requests and reports transport closed; `proxy=None` and
`compression=None` are asserted on the actual client call rather than inferred; and for the
lifecycle — single-flight startup, a fresh token per child, no token in argv, a planted unsafe token
target refused, cleanup on the failure and normal paths, early child exit failing immediately rather
than after the budget, and recovery from an unexpected child death.

### The suite that was skipped is the part that mattered

Before this phase every Codex adapter case was skipped on Windows: the double served an AF_UNIX
socket and one `require_linux_kernel` gated the whole module. The Windows runtime therefore had
**zero** provider-semantics coverage. The double now selects its transport by platform, so the
existing model-list, thread, turn, steer, interrupt, approval, permission, question, event, result,
reconcile and model-override cases all run on Windows against the loopback endpoint.

| | Phase D baseline | Phase E |
| --- | --- | --- |
| Bridge tests | 369 passed / 54 skipped | **440 tests, 0 failures, 40 skipped** |
| Windows Codex cases | 0 | **32** |
| Windows root | 1268 passed / 130 skipped | 1398 tests, 0 failures, 130 skipped |

The skip count **fell**. The new coverage was not bought with skips.

Two cases are now explicitly Linux-contract and say why in their marker: probe never autostarts
(the managed daemon is the Linux transport, and on Windows probe must start the child to answer at
all), and reconcile after a control-socket failure before thread start (that failure is identified
by the Linux daemon's exact message). The Windows equivalents are covered by the new file.

Two real defects surfaced while making this run, both fixed: a permission assertion hardcoded
`"/generated"`, and one call site that passed `event_idle_timeout_seconds` and so missed the
endpoint rewrite — which is why the bulk edit needed a per-call audit rather than trust.

## 5. Real provider acceptance — what passed, and where it stops

### Passed

Driven through the product's own `WindowsCodexAppServer` and `CodexAdapter` against the **real**
`codex` CLI:

| Check | Result |
| --- | --- |
| Runtime probe | `available = true`, version `0.159.2`, **0.23 s** |
| Model discovery | `status = ok`, `source = codex_app_server`, **11 models**, 0.01 s |
| Managed daemon untouched | the operator's 0.160.1 daemon was never seized, stopped or reconfigured |
| Token file lifetime | present while the child ran, deleted by `close()` |

Dated catalog snapshot (2026-10-06), recorded as evidence and **not as a contract**:

```
gpt-6.1-sol (default), gpt-6-astra, gpt-6-sol, gpt-6-luna, gpt-5.6-sol,
gpt-5.6-terra, gpt-5.6-luna, gpt-daybreak-blue-latest, gpt-daybreak-red-latest,
gpt-5.5, codex-auto-review
```

Phase 0C recorded 8 models on 2026-10-04. The catalog has since changed, which is exactly why
§29 forbids hardcoding model ids: `list_agent_models` reads the live catalog, and the only fixed
value anywhere is the *shape* of the response.

### Blocked: the Codex CLI cannot reach the provider from this host, with or without a proxy

The Agent Runtime proxy is now **explicitly configured** (operator action, §2 below), and the proxy
path itself is proven good: TCP to the proxy connects, and an HTTPS request through it reaches the
provider and returns an unauthenticated 401 — the expected answer, and proof that the whole path
works **without any proxy credential reaching the provider child**. All local calls still succeed
against the same child: `probe` and `model/list` are unchanged above.

The provider round trip does not. What made the cause unambiguous is removing every ServerFS
component from the path: `codex app-server --stdio` speaks the same JSON-RPC over stdin/stdout, so
there is no listener, no port, no capability token, no Bridge and no WebSocket. Two arms, same
prompt, same CLI:

| Arm | Proxy variables | Thread started | Turn |
| --- | --- | --- | --- |
| `with_proxy` | `HTTPS_PROXY` + `NO_PROXY` set | yes | **failed** — `Reconnecting... 1/5 … 3/5` |
| `without_proxy` | none | yes | **failed** — `Reconnecting... 2/5 … 4/5` |

Both arms stall identically with ServerFS entirely out of the path. So the CLI is not failing because
of the transport, the token, the Bridge, the listener, or the proxy configuration — this host's Codex
CLI cannot complete provider inference at all. That is an environment or account fact, not a v0.11
proxy-security boundary: nothing anywhere required a proxy credential, and none was injected.

### Proxy isolation, measured on the real child

Despite the inference stall, the four isolation properties the runtime must hold are demonstrated:

| Property | Measurement |
| --- | --- |
| Child receives the dedicated Agent proxy | `HTTPS_PROXY` present, non-loopback host, explicit port |
| Child receives nothing else | no `SERVERFS_AGENT_*`, no `SERVERFS_PROXY_*`/`TUNNEL*`, no `CONTROL_PLANE_*` in the child env |
| Mandatory loopback bypass survives | child `NO_PROXY` = `127.0.0.1, localhost, ::1` (complete) |
| Bridge's own process stays proxy-free | Bridge env has **no** `HTTPS_PROXY` at all |
| Control channel stays local | endpoint shape `ws://loopback:<ephemeral>/`, dialled with `proxy=None` |

`SERVERFS_AGENT_NO_PROXY` was deliberately **not** set: the product merges the mandatory loopback
bypass itself, so an operator value would only add a bypass this host does not need.

### A real defect found, and honestly ruled out as the cause

`build_runtime_environment` forwards `CODEBUDDY_SERVICE_PROXY_URL` — a **loopback** proxy under a
third-party name — into the provider child, because `_is_proxy_variable()` only recognises the
standard proxy names and the `FORWARD_FORBIDDEN_PREFIXES` cover the `SERVERFS_PROXY_*` family by
prefix. A third-party `*_PROXY_URL` naming matches neither. The child therefore sees two proxy
configurations.

Before touching security-relevant code I isolated it: the same real turn was run twice, once with
the policy environment exactly as produced and once with only the recognised proxy variables.
**Both arms failed identically**, so the stray variable is not the cause and **no product change was
made**. Recording this because "found a plausible defect" is not "found the defect"; widening
`_is_proxy_variable` on this evidence would have been an unjustified change to a security boundary.
It is reported as a residual for the maintainer instead.

### Not run, not claimed

Because no real turn completes, none of these were run and none is claimed: workspace-write (§40),
native id persistence (§41), continuation (§42), question (§43), approval (§44), cancellation
(§45), model override (§46), restart reconciliation (§47), lease/guard cleanup (§49), and real Job
containment (§13 of the acceptance list). Running any of them against the fake provider would prove
nothing about the runtime — the mistake Phase D's D9 harness already made once, and one this project
has explicitly retracted before.

## 5b. Operator proxy configuration (§2 of the ruling)

The maintainer confirmed that `TUNNEL_HTTPS_PROXY` on this host is a **credentialless HTTP(S)
absolute endpoint with an explicit port and no userinfo**, which satisfies the v0.11 Agent Runtime
proxy contract, and that the same endpoint could be reused.

- `SERVERFS_AGENT_PROXY_URL` was **explicitly configured** by hand in the operator's private env
  file, after re-checking the contract (absolute http(s), explicit host and port, no userinfo).
- `.env` is **not tracked by git**; the worktree stayed clean apart from the two allowed untracked
  paths. No endpoint value was written to any tracked file, commit, test, evidence document, report
  or log.
- **No product logic was added** to map `TUNNEL_HTTPS_PROXY` or `SERVERFS_PROXY_*` into
  `SERVERFS_AGENT_*`. The two remain independent configuration domains, and the copy was a human
  configuration action.
- `SERVERFS_PROXY_HOST` + `SERVERFS_PROXY_PORT` were correctly rejected as the Agent proxy source:
  a bare `host:port` is not the absolute URL the contract requires.

The raw endpoint value was never printed by any script, in any output, in any commit message or in
this document.

## 6. Doctor

End-to-end against the installed CLI:

```
agent codex: enabled, use_proxy=false
codex cli: OK -- codex-cli 0.159.2
codex app-server flags: OK -- loopback listener and token flags available
codex authentication: OK -- signed in with ChatGPT
```

Three bounded, read-only checks, only when Codex is enabled. Nothing starts an app-server or runs a
turn. `login status` output is reduced to a fixed vocabulary before it reaches a report line.

**A real defect the end-to-end run caught and the unit tests had hidden.** `codex login status`
writes its result to **stderr**; stdout stays empty. The first version read stdout only and so
reported every correctly signed-in deployment as "sign-in state not recognised". A mock that put
the line on stdout agreed with the implementation that assumed stdout — the shape of bug a fixture
written to fit the code cannot find. Both streams are read now, and a regression pins the measured
behaviour against the installed CLI.

## 7. Secrets

Never written to any evidence, log, argv, config or report: the capability token, the Agent proxy
endpoint, any Tunnel or Control Plane credential, any provider credential or account identifier.
The token is generated per child, passed by file, excluded from `repr`, and absent from the
fixed per-platform refusal message. The app-server argv carries `--ws-token-file <private path>`
and `ws://127.0.0.1:<ephemeral>`, and nothing else sensitive.

## 8. Not verified

- Everything in §5's not-run list: workspace-write, native id persistence, continuation, question,
  approval, cancellation, model override, restart reconciliation, lease/guard cleanup.
- Job Object containment of the Bridge-owned child against an abnormal Bridge death. The design
  relies on the Phase D supervisor Job and standard child inheritance, and the deterministic suite
  covers teardown, but the abnormal-termination case was not driven end to end this phase.
- **Residual for the maintainer:** `build_runtime_environment` forwards a third-party loopback proxy
  variable (`CODEBUDDY_SERVICE_PROXY_URL` on this host) into the provider child, because the scrub
  recognises standard proxy names and `SERVERFS_PROXY_*` prefixes but not arbitrary `*_PROXY_URL`
  naming. Measured to be **not** the cause of the stall above. It is still a real narrowing of the
  §7.1 property — the child's proxy configuration is decided partly by an unrelated ambient variable
  — and it should be decided on its own merits rather than fixed under acceptance pressure.
- Behaviour when the operator's managed daemon is mid-flight.
- Linux CI is not run from this host; the Linux gate must confirm that the shared mock and the
  lazy `codex_windows` import produce no collection error and that the Linux Codex UDS cases still
  execute rather than skip.

## 9. Phase F readiness

Qoder is unaffected by this phase: it has its own adapter and its own transport, and nothing here
changed its code path. The transferable result is the pattern — an endpoint type per transport, a
platform-selected connection factory, and a provider double that serves whichever transport the
platform actually uses, so the next runtime inherits Windows coverage instead of starting at zero.