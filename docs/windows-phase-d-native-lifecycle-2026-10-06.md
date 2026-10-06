# Windows native Agent lifecycle — Phase D evidence

Date: 2026-10-06
Branch: `v0.11-phase-d-native-config-lifecycle`
Base: `6038d4b` (post-Phase-C merge of `main`)

Phase D makes Agent delegation configurable and startable on a Windows native deployment, and closes
with one accepted lifecycle. **No real Codex, Qoder or Claude integration is included** — the provider
adapter at the end of the chain is a deterministic test double, and the real transports are Phase E, F
and G.

## What was built

| Item | Where | Substance |
|---|---|---|
| **D1** native `[agent]` TOML | `src/serverfs_mcp/native_config.py` | `[agent]`, per-runtime sections, `[agent.proxy]`, per-workdir `agent_mode` / `agent_runtimes`, startup cross-validation. No `[agent]` section, or `enabled = false`, parses to the identical filesystem-only v0.10 result. |
| **D2** private Bridge config renderer | `agent_bridge/src/serverfs_agent_bridge/render_config.py` | Own CLI entry point in the Bridge package, so §23/§70 independence holds and the generated file is private state (`private_state` / `windows_security` verbatim). `lease_key` is always `alias`; a native workdir never carries a slot. No secret is ever written. |
| **D3** runtime egress policy | `src/serverfs_mcp/agent_proxy.py` | Bridge environment scrubbed of proxy variables and the Tunnel / Control Plane namespaces; per-runtime policy applied strictly downward. `use_proxy=false` yields a genuinely proxy-free child. `userinfo` is refused outright. |
| **D4** bootstrap channel | `agent_bridge/src/serverfs_agent_bridge/bootstrap.py` | One bounded JSON frame on stdin carrying runtime-only material; stdin EOF is the shutdown trigger. Not the MCP protocol; `PROTOCOL_VERSION` is untouched and a test asserts the public transport never imports the module. |
| **D5/D7** supervisor lifecycle | `src/serverfs_mcp/supervisor.py`, `agent_lifecycle.py` | The frozen 12-step startup order as written. Readiness is a real authenticated `runtime.list` over the Named Pipe, never a sleep. Any failure terminates the tree through the Job Object and never starts the stdio child. Shutdown is the reverse and bounded. |
| **D6** Job Object | `src/serverfs_mcp/windows_job.py` | `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` over the Bridge tree only — the tunnel-client and the stdio child are deliberately excluded so a job can never take down the operator's own MCP session. Assignment failure fails closed. |
| **D8** doctor | `src/serverfs_mcp/agent_doctor.py`, `agent_bridge/.../inspect_state.py` | Read-only and static. Disabled Agent is one informational line. Proxy diagnostics use a fixed vocabulary and never emit host, port, URL, userinfo or the raw value. Private-state safety is delegated to a Bridge-side inspector rather than reimplemented. |
| **D9** acceptance | `tests/test_d9_native_lifecycle.py`, `tests/e2e/d9_*.py` | One real lifecycle, 25 tests, no stubs between the steps. |

## Accepted lifecycle (D9)

```
fake tunnel-client → serverfs tunnel → native_tunnel → supervisor
  → private config renderer → Job Object → real Bridge process → real Named Pipe
  → native ServerFS stdio → published MCP Agent surface → provider adapter
```

Only the provider adapter is a test double, injected as a `sitecustomize` so the repository gains no
test-only operator surface. The public runtime name stays `codex`.

| Property | Evidence |
|---|---|
| Launcher chain | The stand-in client receives `--mcp.command` and decodes it with the inverse of the production encoder; `argv[1:3] == ["-m", "serverfs_mcp.supervisor"]`. |
| Launcher scrub | The client records what it actually received: `leaked_prefixes == []`, `leaked_proxy_names == []`. |
| Rendered config | `lease_key: "alias"`, `codex: {enabled, codex_bin, use_proxy}`, live SID, and it loads through the real `BridgeConfig.load`. |
| Secret scan | Every planted marker is absent from `bridge.json`, and the Bridge argv is scanned with the process asserted findable first. |
| Published surface | 21 tools — the 11 filesystem tools plus exactly the 10 frozen Agent tools, pinned by name and by count. |
| Disabled parity | The same chain with Agent disabled publishes 11 tools, creates no Bridge config, starts no Bridge, works without any Bridge interpreter, and serves filesystem reads and writes. |
| Task lifecycle | `submit_agent_task` → `status == "succeeded"` (strictly), read back through `get_agent_task`, `read_agent_task_events` (`task.started`, `turn.started`, `turn.completed`, `task.completed`) and `read_agent_task_result`. |
| Workspace mutation | The adapter wrote `phase-d-native-lifecycle.txt` into the Bridge-resolved `context.cwd`; the exact bytes are asserted on disk and through `read_text_file`. |
| Writer lease | A live turn refuses an MCP mutation with `WORKDIR_BUSY`; both the cancellation and the normal-completion paths release the lease and clear the guard. |
| Proxy isolation | The provider child — a **real** spawned process reading its own `os.environ` — sees `HTTPS_PROXY` and `NO_PROXY` only, with the mandatory loopback bypass merged with the operator's entries; zero namespace leaks; provider-native names survive. With `use_proxy=false` it is genuinely proxy-free. |
| Graceful shutdown | Closing stdin stops the chain with exit 0 and no forced kill; no Bridge or supervisor process survives. |
| Abnormal containment | Terminating the supervisor kills the Bridge tree; an unrelated bystander process survives. |
| Startup failure | A missing Bridge interpreter gives a non-zero exit, a redacted message with no traceback and no interpreter path, no orphan Bridge and no lease. |

## Security fix found by the acceptance run

With a Bridge interpreter that does not exist, the Agent path built the correct redacted message —
`the Bridge configuration could not be rendered (FileNotFoundError)` — and then `AgentLifecycleError`
escaped `main()` uncaught. The operator saw a Python stack trace containing the interpreter path those
messages exist to withhold. `main()` now translates it into stderr plus exit 2, with a last-resort net
in the `__main__` guard.

## Recorded, not fixed

A malformed `serverfs.toml` or a non-existent workdir root is refused by `run_native_tunnel` *before*
the supervisor exists, and that path emits an unredacted traceback. This is a real gap in
launcher-level diagnostics and is follow-up work. The D9 failure case is deliberately injected after
the supervisor starts, so acceptance does not silently depend on the fix.

## Not tested here

- Real Codex, Qoder or Claude Windows transports (Phase E / F / G).
- Provider authentication, model catalogues or inference.
- The Jev advisory layer beyond its measured conclusion: installed `typesafe-sdk==0.7.1` exposes no
  explicit proxy parameter, so Jev stays direct under its existing fail-open semantics.

## Gates

Recorded in the PR body for the final head. Windows root and Bridge suites, `ruff check` and
`ruff format --check` in both packages.