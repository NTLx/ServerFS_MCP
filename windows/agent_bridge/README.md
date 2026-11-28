# ServerFS Agent Bridge

The optional host-side Bridge owns Agent execution, task state, writer coordination and deterministic Repo Ops. Core and Connector lifecycles are separate. Choose `disabled` for file-only service, `owned` for a dedicated Bridge, or `shared` for attachment to an explicitly authorized owner. Named Windows attachments use signed per-Core grants and method/alias scope over Named Pipes. Shared Cores cannot manage the owner. Keep owner state and credentials private.

## Deployment

- [Windows native Task Scheduler and Named Pipe](../deployment/windows/native-operations.md)

This platform-local Bridge is part of the v0.27.0 candidate. For existing v0.26.0 deployments, use the tagged source and matching artifacts.

Repo Ops and Agent delegation are independently gated per workdir. Repo Ops never submits a provider task. Native Codex, Claude and Qoder plus fixed operator-configured ACP profiles are supported as documented; ACP needs workspace-write and must not infer unsupported capabilities. Provider authorization is provider-owned. Jev is advisory only. See the [Agent Bridge guide](https://ntlx.github.io/ServerFS_MCP/docs/agent-bridge/) for task lifecycle, model discovery, recovery, diagnostics and limits.

## Approval boundary and private audit

Public pending approvals and task events continue to redact absolute host paths outside
the selected workdir to `<outside-workdir>`. That placeholder is **not** an
authorization target. For structured native `file_change`/`file_changes` requests
and provider filesystem *write* grants, the Bridge checks the proposed target
against the task's trusted workdir root and effective task cwd (including
existing symlinks). Missing or unresolvable file targets, traversal and explicit
outside-root write targets receive `scope_status=outside_workdir_write_blocked`
in the public approval. Only `deny`/`cancel_task` may resolve that request;
an attempted positive decision fails with `PERMISSION_OUTSIDE_POLICY` without
consuming the pending request. Read-only grants are unaffected. ACP option-only
requests contain no structured write-root targets: their opaque provider-issued
option IDs retain native approval semantics rather than being misclassified as
missing file paths. This exception is not evidence that ACP can confine writes.

This is a **remote approval-admission rule**, not an Agent sandbox or a way to
parse arbitrary commands. Provider-native shell commands, MCP tools and free
text remain governed by their provider sandbox/permission system; this check
cannot promise confinement for those operations or prevent concurrent host
path changes. Grant legitimate access to another directory only through the
operator's explicit per-workdir policy and signed Core/Bridge grants, not by
approving an ambiguous redacted path. Jev advice never approves requests.

The Bridge persists only a keyed HMAC-SHA256 fingerprint of the **original**
provider request, action class, task/request identity, timestamp and decision
in its owner-private SQLite `approval_audit` table. The HMAC key is generated
inside the private database and survives restarts. Original absolute targets,
raw prompts and credentials are not persisted in this audit table. Deleting
retained tasks cascades through their audit rows. These records are not
exposed by public MCP APIs. A complete on-host security review must still
verify owner ACL/mode, task expiry and live provider behavior.

## Task discovery and runtime compatibility

On signed-v2 attachments with explicit `task.list` and `task.get` grants, `list_agent_tasks(workdir, status="active", limit=20, cursor=None)` discovers retained tasks for one authorized Agent alias. Use `active`, `terminal` or `all` and opaque pagination. Signed v2 does not support continuation or idempotency keys; rediscover rather than blindly retrying an uncertain submission. Legacy v1 retains its documented task-by-ID and continuation semantics.

Keep each native provider runtime compatible with its SDK. Use a verified executable explicitly where needed; do not overwrite the operator's CLI, configuration or local memory.

## Development

Run from this directory:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The Windows Core suite and external public contract gate must also remain green. Full lifecycle tests require an isolated test account.

## Named Pipe process acceptance

From the Windows platform root, with both platform-local environments installed,
run the isolated worker and Core process fixtures in a dedicated test account:

```powershell
uv run pytest tests/test_windows_mcp_agent_e2e.py tests/test_windows_v25_bridge_v2_real_worker.py tests/test_windows_v25_two_independent_bridge_workers.py tests/test_windows_v25_topology_c_real_core_processes.py
```

These use disposable Named Pipes, Jobs and private fixture state, not a public
Tunnel or a real provider. They do not replace current-user Scheduler or
real-provider acceptance. Legacy SID-global lifecycle tests must not run
against a production owner. Do not translate the old Unix-socket harness or
POSIX example JSON into Windows production configuration.

## Historical evidence

Previous provider smoke recipes, phase records and exact acceptance statements remain available in the [integration baseline](https://github.com/NTLx/ServerFS_MCP/blob/fffc0adb8341edb30ac7b03e72a017ca95b8c83d/agent_bridge/README.md) and repository diagnostics. Re-run applicable gates against the actual owner/artifacts before claiming current acceptance.
