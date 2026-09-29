# ServerFS v0.8.0 Development Plan — Qoder Native Runtime

Status: implementation and validation complete; release/deployment pending
Baseline: v0.7.3 / main
Scope: add Qoder as the third production Agent runtime without changing the public Bridge RPC protocol.

## 1. Goal

Add a native `qoder` runtime beside `codex` and `claude` by using Qoder's official Python Agent SDK and the existing system-managed Qoder CLI login/configuration.

The integration must preserve the current ServerFS architecture:

- ServerFS MCP remains provider-neutral.
- Agent Bridge remains the only provider integration layer.
- Qoder owns its native model/runtime/tool/settings behavior.
- ServerFS owns workdir authorization, writer lease, task lifecycle, durable interaction state and normalized events.
- No generic shell/argv/environment RPC is added.
- No provider credential is moved into the MCP container.

## 2. Non-goals

This phase does not add:

- a public `model` parameter to `submit_agent_task`;
- Qoder-specific MCP tools;
- an Agent Bridge schema migration solely for Qoder;
- in-flight process reattachment after Bridge restart;
- ServerFS-owned Qoder permission rules or persistent permission mutation;
- ServerFS-owned model routing.

Production Qoder sessions inherit the user's native Qoder configuration. A model override is allowed only in the disposable live-smoke script so validation can pin a known test model.

## 3. Provider contract

Add `QoderAdapter` implementing the existing `AgentAdapter` contract.

Expected normalized runtime capabilities:

- `name="qoder"`
- `persistent_session=True`
- `interactive_approval=True`
- `interactive_question=True`
- `in_flight_recovery="session-resume"`
- `live_steer=False`: the 2026-09-29 installed-SDK/CLI probe proved `priority="now"` first terminates the current `receive_response()` with `error_during_execution`, while the steered success arrives from a second `receive_response()`; the existing one-task/one-terminal-Result Bridge contract must not expose that as live steer.

The adapter uses:

- `QoderSDKClient`
- `QoderAgentOptions`
- `qodercli_auth()`
- `cwd=context.cwd`
- `cli_path=<configured qodercli path>`
- `setting_sources=["user", "project", "local"]`
- no explicit `permission_mode` override; Qoder's native user/project/local permission settings remain authoritative
- `resume=<native_session_id>` for continuation
- `can_use_tool` for approval and `AskUserQuestion` when the provider requires interactive input
- `interrupt()` for cancellation

No production `model` option is supplied.

## 4. Lifecycle and recovery

A Qoder SDK session is process-backed. Session persistence/resume does not prove that an old in-flight provider process is still active or can be reattached.

Therefore:

- a Qoder native session ID is persisted as soon as the SDK init message exposes it;
- terminal tasks may continue through a new ServerFS task using `resume`;
- Bridge restart reconciliation never reports `REATTACHED` solely from a persisted session ID;
- when the local SDK client is known to have disconnected, reconciliation may report `SESSION_RESUMABLE` and `provider_active=False`;
- otherwise prior in-flight process state remains unknown and the existing recovery guard stays fail-closed;
- systemd `KillMode=control-group` continues to provide host-side child-process cleanup on service stop.

## 5. Approval and question mapping

Ordinary Qoder permission requests map to the existing ServerFS decisions:

- `approve_once` -> `PermissionResultAllow(updated_input=tool_input)`
- `approve_session` -> allow plus only provider-supplied permission suggestions whose `destination` is exactly `session`
- `deny` -> `PermissionResultDeny(interrupt=False)`
- `cancel_task` -> `PermissionResultDeny(interrupt=True)`

ServerFS never fabricates or persists provider permission updates.

`AskUserQuestion` maps into the existing normalized question payload. Returned answers use Qoder's required full-question-text keys, option labels joined with `, ` for multi-select, or explicit free text.

## 6. Event mapping

First implementation normalizes typed SDK messages:

- text -> `agent.message`
- Bash tool use -> `command.started`
- Write/Edit/NotebookEdit -> `file_change.started`
- other tool use -> `tool.started`
- provider/result errors -> normalized Bridge provider failure

Hooks are not required for the first integration because they would expand scope. They may be added later if complete tool-call audit coverage is needed.

## 7. Configuration and deployment

Add strict `qoder` config:

```json
"qoder": {
  "enabled": false,
  "qoder_bin": "qodercli",
  "probe_timeout_seconds": 5,
  "event_idle_timeout_seconds": null
}
```

Production rendering adds:

- `SERVERFS_QODER_BIN`
- `SERVERFS_QODER_PROBE_TIMEOUT_SECONDS`

The runtime remains opt-in per workdir through existing `WORKDIR_XX_AGENT_RUNTIMES`.

Native Qoder mode requires `workspace-write`, matching Codex and Claude, because ServerFS must hold the workdir writer lease.

## 8. Dependencies

Pin the official global Qoder Python SDK:

- `qoder-agent-sdk==1.0.15`

Use the system `qodercli` executable rather than the wheel-bundled runtime in production so the Bridge delegates to the same native Qoder installation/login/configuration used by the server user.

## 9. Runtime Router

Extend the optional Jev route vocabulary with `qoder`.

Jev remains advisory only. It may recommend Qoder when:

- the task explicitly requests Qoder/Qoder CLI;
- the task depends on Qoder-specific sessions/settings/skills/plugins;
- otherwise provider selection remains a recommendation and never overrides the explicitly requested runtime.

## 10. Test strategy

### Deterministic tests

Add/extend tests for:

- strict Qoder config parsing;
- allowlist requires `qoder.enabled=true` and `workspace-write`;
- runtime discovery and public allowlists;
- Qoder new-session and continuation;
- Qoder approval and session-suggestion mapping;
- Qoder `AskUserQuestion` round-trip;
- cancellation/interrupt and reconciliation evidence;
- Qoder event normalization;
- Jev route vocabulary;
- deployment config rendering/host verification.

### Real live smoke

The live smoke must explicitly pin:

`Qwen3.8-Flash`

This model override exists only in the smoke script and is never exposed through ServerFS MCP or normal production config.

Before the live test, verify the server account exposes the exact model identifier via the native CLI. The current server preflight confirmed `Qwen3.8-Flash`.

The smoke should prove, in a disposable directory:

1. runtime probe;
2. a new Qoder session;
3. native session ID persistence;
4. terminal session continuation;
5. a real file write and cleanup;
6. `AskUserQuestion` if reliable under the installed model/runtime;
7. live steer boundary behavior through a separate real SDK probe.

The 2026-09-29 probe accepted `priority="now"`, but the first `receive_response()` ended with an `error_during_execution` Result and the steered success arrived only from a second `receive_response()`. Therefore v0.8.0 deliberately keeps `live_steer=False`; supporting it would require provider-specific multi-Result orchestration and is outside this runtime integration scope. See `docs/qoder-runtime-validation-2026-09-29.md`.

## 11. Acceptance criteria

The implementation is accepted when:

- no public Bridge RPC shape changed;
- no model parameter was added to public MCP Agent tools;
- `qoder` is accepted everywhere the public native runtime allowlist is validated;
- deterministic root and Agent Bridge test suites pass;
- lint/format checks pass;
- Qoder probe and real smoke use the installed native CLI;
- real smoke explicitly uses `Qwen3.8-Flash`;
- any unsupported recovery/steer behavior is reported conservatively rather than emulated;
- the real validation evidence is recorded in `docs/qoder-runtime-validation-2026-09-29.md`;
- repository changes are limited to the Qoder runtime feature, its tests, deployment/config documentation and the v0.8.0 development plan.
