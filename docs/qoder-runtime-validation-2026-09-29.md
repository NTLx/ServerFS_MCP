# Qoder Native Runtime Validation — 2026-09-29

Status: PASS for the v0.8.0 release

This record freezes the validation evidence for adding Qoder as the third
production Agent Bridge runtime. v0.8.0 is the current stable release; this record is the pre-release validation evidence used to accept the Qoder runtime.

## Scope

The validation covers the provider adapter, public runtime allowlists,
configuration/deployment rendering, Jev advisory routing, real Qoder CLI/SDK
behavior, and the model-selection boundary.

The public ServerFS contract remains provider-neutral:

- no new MCP Agent tool;
- no new Bridge RPC method;
- no public `model` parameter;
- production Qoder tasks inherit provider-native model selection;
- only the disposable live-smoke/probe may pin an explicit validation model.

## Environment

Measured on the ServerFS host:

- Qoder CLI: `1.1.64`
- system Qoder executable: `/home/lx/.qoder/entry/qoder`
- system qodercli executable: `/home/lx/.local/bin/qodercli`
- Qoder Agent SDK: `qoder-agent-sdk==1.0.15`
- validation model: `Qwen3.8-Flash`

Before any real Agent call, the native model list was queried and the exact
`Qwen3.8-Flash` identifier was present. No other model was used by the live
Qoder validation.

## Deterministic gates

With the Qoder SDK installed from the frozen `agent_bridge/uv.lock`:

- `cd agent_bridge && uv sync --frozen`: PASS
- `cd agent_bridge && uv run ruff check .`: PASS
- `cd agent_bridge && uv run ruff format --check .`: PASS
- `cd agent_bridge && uv run pytest`: **139 passed**
- repository-root `uv run pytest`: **819 passed**

The validation task made no additional tracked source changes.

## Real Qoder smoke

The live smoke used a private temporary Bridge config and the existing server
user's authenticated system `qodercli`. The temporary config did not copy the
Jev API key or provider secrets.

Command shape:

```bash
cd agent_bridge
uv run python scripts/qoder_live_smoke.py \
  --config /tmp/serverfs-qoder-live-config.json \
  --workdir ServerFS \
  --model 'Qwen3.8-Flash' \
  --timeout 300
```

Verified:

1. Qoder runtime probe: PASS
2. new native session: PASS
3. native session ID persistence: PASS
4. terminal session continuation through `resume`: PASS
5. `AskUserQuestion` Bridge round-trip: PASS
6. workspace write plus exact content verification: PASS
7. temporary workdir/config/state cleanup: PASS

The model override above is test-only. `QoderAdapter` constructed by normal
Bridge startup receives no model override.

## Live-steer probe

Qoder documents `client.query(..., priority="now")` as an immediate steering
mechanism. A separate real SDK probe tested whether that provider behavior can
map safely onto the current ServerFS one-task/one-terminal-Result contract.

Observed sequence:

- `priority="now"` was accepted successfully.
- The first `receive_response()` ended with a `ResultMessage` whose
  `subtype="error_during_execution"`, `is_error=true`, and `result=null`.
- The steered response did **not** appear in that first response iteration.
- A second `receive_response()` produced the expected
  `SERVERFS_QODER_STEER_OK` success Result.
- Both Results belonged to the same native session.

Therefore the existing ServerFS `_receive_result()` contract would treat the
first provider Result as terminal and finish the Bridge task before the actual
steered answer arrives.

Decision for v0.8.0:

- keep `live_steer=False` for Qoder;
- keep `send_agent_message` rejected for active Qoder tasks;
- do not add provider-specific multi-Result orchestration merely to expose this
  optional capability.

This is a measured provider/Bridge contract mismatch, not an untested feature.

## Recovery semantics

Real smoke proves native session persistence and continuation, but does not
provide evidence that a Bridge restart can reattach to an already-running
qodercli process. Qoder therefore remains:

```text
persistent_session = true
in_flight_recovery = session-resume
live_steer = false
```

A persisted native session may be continued by a new terminal-to-new-task
ServerFS continuation. It is not evidence for in-flight reattachment.

## Approval boundary

Qoder `can_use_tool` is bridged through the existing provider-neutral
approval/question records. `approve_session` is exposed only when the
provider supplied a permission suggestion whose destination is exactly
`session`. User/project-persistent suggestions are never echoed by the
Bridge as a session approval.

## Result

The Qoder runtime meets the v0.8.0 implementation acceptance criteria for
native execution, continuation, interactive approval/question brokerage,
cancellation, configuration/deployment validation and conservative recovery.

Publication/tagging and production rollout remain separate release/deployment
steps.
