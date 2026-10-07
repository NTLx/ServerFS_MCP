# Windows Phase E — Codex runtime

Status: **OPEN** (not CLOSED-PASS). The transport, the Bridge-owned app-server lifecycle, the
adapter wiring and the deterministic Windows suite are landed. The real runtime probe, real model
discovery, real workspace-routing readiness and **a real inference over the WebSocket listener** all
pass against the installed CLI. What remains unrun is everything gated behind that: workspace-write,
continuation, question, approval, cancellation, model override, restart reconciliation and lease
cleanup. Three harness defects got here, each of which had produced a confident false conclusion, so
they are recorded first rather than tidied away.

- Branch: `v0.11-phase-e-windows-codex`
- Base: `bc3500fce28453c77118c265c51aa0d84b5580d2` (post-Phase-D main)
- Codex CLI measured on this host: **`codex-cli 0.159.2`**

## 0. Corrections to earlier revisions of this document

| Defect | False conclusion it produced | Actual cause |
| --- | --- | --- |
| `CODEX_HOME` pointed at an empty directory | "this host's Codex CLI cannot complete provider inference at all" | A ChatGPT-auth CLI keeps its tokens in `<codex_home>/auth.json`; an empty home silently de-authenticates it, it falls back to `api.openai.com`, and every turn returns 401 |
| `CodexSettings(use_proxy=...)` never set, so it defaulted to `False` | "the WebSocket listener cannot complete a turn while stdio can" | The WS child got a **proxy-free** environment, so routing discovery had no egress and hit its 15 s bound every time. The stdio arm built its environment with `use_proxy=True` and did have egress |
| turn budget shorter than a real turn on this host | "the turn never completes" | a real turn needs ~45–90 s here |

The second is the one that mattered. A paired stdio-vs-WebSocket matrix run under it produced stdio
**9/10** success against WebSocket **0/10**, every WebSocket sample hitting the bound at exactly
15.00–15.01 s — a textbook "transport causality" result, and entirely an artefact of the WebSocket
arm never being given a proxy. The upstream 15 s bound is real and is what made the artefact look
like a transport property.

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

### Measured: real inference over the WebSocket listener succeeds

With the proxy actually injected into the child, the product's own Windows shape completes a real
turn. Three independent runs, each a fresh Bridge-owned app-server:

| run | probe | model/list | account/read | routing | turn |
| --- | --- | --- | --- | --- | --- |
| 1 | available, 0.159.2 | 11 | success, 1.01 s | present | **`ready`** |
| 2 | available, 0.159.2 | 11 | success, 1.29 s | present | **`ready`** |
| 3 | available, 0.159.2 | 11 | success, 1.33 s | present | **`ready`** |

Zero provider errors across all three. This is the §4 minimal gate: probe available, non-empty
catalog, turn completes, final response exactly `ready`.

### `account/read` is a valid routing-readiness probe, and it is not required as a gate

`account/read {"refreshToken": false}` exists in the official protocol (confirmed from
`codex app-server generate-json-schema`, which is the authoritative source for method names). It
answers in 1–3 s with `workspaceRouting` **present**, and under the proxy-free defect it reproduced
the upstream 15 s bound at 15.01 s — so it measures exactly the thing the maintainer's hypothesis was
about.

But a separate three-run matrix **with the gate skipped** also returned `ready` 3/3. So on this path
`initialize` is sufficient readiness and the extra `account/read` wait is optional hardening, not a
required fix. Recorded as measured rather than assumed either way.

### Transport causality: NOT established, and the evidence points away from it

The maintainer's upstream citation (openai/codex#49827) reports this same routing failure on stdio
in a matching environment at different times, which is correct and was the right thing to check. The
paired matrix run here was **confounded** and has been discarded rather than reported: its WebSocket
arm never received a proxy, which is a sufficient explanation for a deterministic 15.00 s bound on
every sample.

A follow-up control isolates the remaining harness asymmetry, with the proxy correctly injected:

| arm | connections | result |
| --- | --- | --- |
| single | one | success, ~3.4 s, routing present |
| sequential | first opened then closed, then a second | success, ~2.0 s, routing present |
| concurrent | two live at once | success, ~2.0 s, routing present |

Concurrent success matters on its own terms: the frozen §10 design requires one shared app-server
serving the probe, the model list, every task and reconciliation at once, and that shape is measured
to work.

## 5a. Real-provider acceptance (§40–§49): started, provider behaviour confirmed, gates unproven

The acceptance harness is landed (`3bb2603`): the D9 chain with the fake adapter removed, so the
pass evidence comes from the public MCP surface only -- `serverfs tunnel` CLI -> native supervisor ->
real Bridge -> real Named Pipe -> the published Agent tools -> the real `CodexAdapter` -> a
Bridge-owned `codex app-server` child -> the real provider. Its preflight asserts, before any work
starts, the three conditions whose absence produced this phase's earlier false conclusions.

**What is confirmed, from the TaskStore of a real run** (read-only; no id values are recorded here):

| Observation | Evidence |
| --- | --- |
| The chain launches and publishes the Agent surface | 10 agent tools reachable over real MCP stdio |
| A **real** turn runs against the provider | `turn.started`, `item.started`/`item.completed` for reasoning and an agent message |
| **Native ids persist** (§41) | `native_session_id` and `native_turn_id` both present on the task row |
| The provider **asks for approval** (§8/§44) | `approval.requested` with a pending `command` request, reason *"May I write the requested file in the current workspace?"*, `available_decisions: [approve_once, cancel_task]` |
| The interaction has a bounded life | the request carries a 5-minute `expires_at` |

So the workspace-write gate and the approval gate are **the same interaction** on this provider: a
real Codex will not write a file without asking. That is genuine provider-originated behaviour, not
a harness artefact, and it is recorded as such.

### The approval round trip passes, and so does §40 with it

A one-shot probe drove the real chain against the real provider and answered the request once
through the public tool. Result:

| Step | Outcome |
| --- | --- |
| `task.get` while waiting | `waiting_for_approval`; top-level id **and** nested object both present and **equal**; nested `task_id` points back; `kind=approval`; `status=pending`; offered `[approve_once, cancel_task]` |
| `respond_agent_approval` (`approve_once`) | success, **0.03 s**, `resolved: true`, ids matched |
| task after the answer | `waiting_for_approval` → **`running`** |
| request row | **`resolved`** |
| events | `approval.requested` **and** `approval.resolved` both present |
| terminal state | **`succeeded`** |
| artifact | `phase-e-approval-probe.txt` written by the real Codex, contents exactly `approval-roundtrip-ok` |

So the frozen pending-request contract holds, the public tool reaches Bridge, Bridge commits the
resolution, the provider continues, and the file lands. On this provider §40 and §44 are the same
real turn, and both are now evidenced: **workspace-write and approval both PASS.**

### What the probe exposed instead: the public MCP surface stalls while an approval is pending

Reproducible, and the reason every earlier run looked like a slow provider. While a task sits in
`waiting_for_approval`, `get_agent_task` over the public MCP surface does not answer for roughly
15 seconds — measured directly against `serverfs_mcp.cli serve`: the first two calls returned
nothing inside 15 s and the third arrived, i.e. replies are delayed in a queue rather than lost.

The delay is not the Bridge. Straight onto the same Named Pipe, `task.get` for the same waiting
task returns in **0.02 s**, repeatedly, with both pending ids present and correct. And 15 s is
exactly `AgentBridgeClient`'s default `timeout_seconds`. So the stall is in the MCP layer above the
Bridge, correlated with a pending interaction — most plausibly the pipe exchange saturating on the
same deadline while the task's interaction waiter is live. Not yet isolated to a line of product
code, and not claimed as fixed.

This also explains the earlier "the harness never answers" symptom without needing a harness bug to
carry it: the harness polls through the same affected path, so it frequently could not observe the
approval inside the interaction's five-minute life.

### Harness faults, all now fixed (`56f547c`)

Each of these turned a stuck MCP layer into "the provider is slow":

- **the bounded read was not bounded.** `request()` checked its deadline only between messages while
  its body was a blocking `readline()`, so an unanswered chain parked the harness indefinitely;
- **fixing that exposed a second fault I introduced:** one reader thread per request, all calling
  `readline()` on the same pipe, so replies went to whichever thread won. There is now one reader
  for the client's life, feeding a queue, and replies to abandoned requests are recorded so a slow
  response is distinguishable from a stuck layer;
- **the pending read tolerated two shapes**, so a missing id was indistinguishable from a present
  one. One legal shape now, asserted; disagreement raises instead of falling back;
- **`respond_agent_approval` failures were swallowed**, making "could not answer" identical to
  "never saw the request".

Recorded as **unrun and unproven**, with no approximation by the fake provider:

- §42 continuation, §46 model override, §43 question, §45 cancellation, §47 restart reconciliation,
  §48 post-restart continuation, §49 lease/guard matrix, and real Job containment.

§40 and §44 moved to PASS on the evidence above. Everything else is untouched, and the MCP-surface
stall above must be understood before those gates can be trusted, because they all poll through it.

### Harness faults found by running it

Each of these was a silent failure that made a wrong outcome look like a slow provider:

- teardown killed only the launcher, so a Bridge survived in its Job Object holding the writer lease
  and the next run failed `WORKDIR_BUSY` against a lease no operator can see or release;
- a failing `respond_agent_approval` was swallowed by a bare `except`, making "could not answer"
  identical to "never saw the request";
- the MCP read loop was unbounded, so a chain that stopped answering hung until the outer timeout
  instead of naming the outstanding call;
- `tail`-buffered output made a harness that had submitted its task look like one that had not.

A separate process-cleanup mistake of mine deserves recording: while clearing leftovers I killed by a
pattern broad enough to also stop the operator's own processes. The Codex **managed daemon survived**
(verified by PID and creation time), but two orphaned `codex app-server` processes from 2026-10-04
were also stopped. They had no live parent and were not the managed daemon, so no contract was
broken, but the filter was wider than the intent and the intent should have been narrower.

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

### Proxy isolation, measured on the real child

| Property | Measurement |
| --- | --- |
| Child receives the dedicated Agent proxy | `HTTPS_PROXY` present, non-loopback host, explicit port |
| Child receives nothing else | no `SERVERFS_AGENT_*`, no `SERVERFS_PROXY_*`/`TUNNEL*`, no `CONTROL_PLANE_*` in the child env |
| Mandatory loopback bypass survives | child `NO_PROXY` = `127.0.0.1, localhost, ::1` (complete) |
| Bridge's own process stays proxy-free | Bridge env has **no** `HTTPS_PROXY` at all |
| Control channel stays local | endpoint shape `ws://loopback:<ephemeral>/`, dialled with `proxy=None` |
| No harmful proxy variable | `ALL_PROXY` absent; `codex doctor` shows it breaks TLS when present |

`SERVERFS_AGENT_NO_PROXY` was deliberately **not** set: the product merges the mandatory loopback
bypass itself, so an operator value would only add a bypass this host does not need.

### A real trust-boundary defect, found by measurement and since fixed

`build_runtime_environment` forwarded `CODEBUDDY_SERVICE_PROXY_URL` — a **loopback** proxy under a
third-party name — into the provider child, because both implementations of `_is_proxy_variable()`
recognised only the four standard proxy names and the forbidden-prefix list covers only the
`SERVERFS_PROXY_*` family. A third-party `*_PROXY_URL` naming matched neither, so the child's egress
was partly chosen by an unrelated ambient variable, which is exactly what §7.1 forbids.

It was first isolated by measurement — the same turn was run with and without it and behaved
identically — so it was **not** the cause of the routing failure and no change was made at that
point. That was the right call for the wrong reason: the defect is real regardless of whether it
caused this particular symptom, and it was fixed in its own commit (`a3c4f30`) before the Phase E
PR, as an independent correctness and security corrective.

The rule added is a case-insensitive `_PROXY_URL` **suffix**, implemented independently in both
packages as §23/§70 require. Deliberately not `"proxy" in name.lower()`: a substring rule would also
swallow `PROXY_PROTOCOL_VERSION` and `PROXY_MODE`, which are provider configuration rather than
endpoints, and deleting configuration nobody measured is its own silent regression. A test asserts
that distinction, and non-vacuity is proven in both directions — disabling the suffix reproduces the
leak, and a substring rule is shown to capture the leak while destroying the names that must
survive.

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
- **Whether `account/read` should be added to product readiness.** Measured to be optional on this
  path (turns succeed 3/3 without it), so it is a maintainer decision rather than an outstanding
  defect. Not implemented.
- Job Object containment of the Bridge-owned child against an abnormal Bridge death. The design
  relies on the Phase D supervisor Job and standard child inheritance, and the deterministic suite
  covers teardown, but the abnormal-termination case was not driven end to end this phase.
- Behaviour when the operator's managed daemon is mid-flight.
- Linux CI is not run from this host; the Linux gate must confirm that the shared mock and the
  lazy `codex_windows` import produce no collection error and that the Linux Codex UDS cases still
  execute rather than skip.

## 8b. Process notes worth keeping

- `asyncio.start_reading` no longer exists on Python 3.13. Framing a child's stdout needs a
  background pump into a queue.
- `asyncio.StreamReader.readline()` takes no timeout; bound it with `wait_for`.
- Pointing `CODEX_HOME` at a fresh directory silently de-authenticates a ChatGPT-signed-in CLI. Any
  acceptance run must use the real Codex home or place `auth.json` deliberately.
- `codex doctor` and `codex debug app-server send-message-v2` are the provider's own diagnostics and
  answer most connectivity questions in seconds. They were available the whole time; reaching for
  them first would have saved two rounds of harness archaeology.

## 9. Phase F readiness

Qoder is unaffected by this phase: it has its own adapter and its own transport, and nothing here
changed its code path. The transferable result is the pattern — an endpoint type per transport, a
platform-selected connection factory, and a provider double that serves whichever transport the
platform actually uses, so the next runtime inherits Windows coverage instead of starting at zero.