# v0.26.0 pre-split public contract

Product source and planning baseline: `fd1019498b281cdeb8c5805242f1a71222f5eebd` (main after the approved documentation-only commit). Product version 0.26.0; Python >=3.12; MCP SDK 2.3.0. This is the checked-out source baseline, not an assertion of GitHub publication, every deployment version, or fresh live acceptance. Historical acceptance has its own [ledger](../../docs/releases/v0.26.0-evidence.md) and [checklist](../../docs/v0.26.0-release-checklist.md).

## Public capability groups

| Group | Tool names | Admission |
| --- | --- | --- |
| Filesystem (11) | list_workdirs, list_directory, find_files, search_text, read_text_file, stat_file, create_text_file, edit_text_file, delete_file, create_directory, delete_directory | Registered base surface; actual mutations require explicit workdir write policy |
| Binary (2) | download_binary_file, upload_binary_file | At least one binary-enabled workdir; enforce selected alias and transfer limits |
| Agent (11) | list_agent_runtimes, list_agent_models, submit_agent_task, list_agent_tasks, get_agent_task, read_agent_task_events, read_agent_task_result, respond_agent_approval, answer_agent_question, send_agent_message, cancel_agent_task | Explicit Bridge and Agent-enabled workdir; actual supported operations depend on attachment/runtime/grants |
| Repo Ops (4) | inspect_repo, run_repo_action, commit_repo_changes, push_repo | Independent explicit workdir grant and matching Bridge; actions remain operator-configured |

Base = 11; combinations add 2/11/4 independently. Max registration = 28. `list_agent_tasks` on supported signed-v2 attachments requires task.list + task.get; task discovery is not continuation or idempotency. v0.26.0 signed-v2 rejects explicit non-null continuation/idempotency. The v0.27.0 plan extends this only after the split.

## Protocol and behavior

- stdio and Streamable HTTP; modern 2026-07-28 per-request metadata and legacy 2025-11-25 initialization/session on the same HTTP app, as covered by `tests/test_mcp_http_wire_compat.py`.
- Read-only text Resource template `serverfs://{workdir}/{path}`, MIME text/plain, full UTF-8 result or explicit budget error; no silent truncation. Binary embedded resource and structured metadata remain coupled.
- Keep the public MCP schema, annotation hints, errors, policy admission, revision-guarded atomic mutation, read pagination, binary ingress/limits, deterministic Repo Ops, OAuth/static-key boundary, task/event/result/approval semantics and lifecycle identity as defined by the [maintainer invariants](../../docs/maintainers/invariants.md). Optional capabilities and platform/provider limits follow the [v0.26.0 release notes](../../docs/releases/v0.26.0.md).
- Native security and lifecycle cannot be established by schema capture alone. Run applicable real platform tests; preserve negative checks and the historical evidence scope.
- Actual registered tool definitions were captured in [mcp-tools.json](mcp-tools.json) with the one-off [capture utility](../../docs/diagnostics/v0270-capture-v026-contract.py): MCP 2.3.0, 12 variants, 28 tools, 85,820 bytes, SHA-256 `e4aff42ebcf40a587912b9d0832a74e263dc5f2ee28322feee296639a96aea4d`. Independent JSON readback validated references and input/output schemas/annotations; upload has two definitions depending on file ingress. Synthetic registration evidence is not native runtime acceptance.

No v0.27.0 behavior is implied by this baseline.
