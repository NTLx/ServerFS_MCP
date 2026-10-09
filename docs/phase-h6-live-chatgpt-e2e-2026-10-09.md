# Phase H6 — Live ChatGPT Tunnel E2E acceptance (2026-10-09)

Status: **PASS**

This acceptance was executed from a live ChatGPT conversation through the installed WorkPC ServerFS MCP plugin. It therefore exercises the release topology that H6 exists to prove:

`ChatGPT -> OpenAI Secure MCP Tunnel -> native ServerFS -> Windows Named Pipe Agent Bridge -> provider runtime`

Repository state before recording evidence:

- branch: `phase-h-release-closure`
- HEAD: `fbc52c50dd6da7d4bd0d69a35ebc5bdfd5ccc29e`
- no tracked modified or staged files
- pre-existing untracked paths only: `.claude/`, `.workbuddy/`, `debug.log`

## Live observations

1. `list_agent_runtimes` returned all three claimed Windows runtimes available through the live MCP connection:
   - Codex `0.159.2`
   - Claude Code `2.1.285`
   - Qoder `1.1.65`

2. `list_agent_models(runtime="qoder")` succeeded through the live connection and reported `qfmodel` / Qwen3.8-Flash as enabled and free.

3. Codex completion probe:
   - task: `agt_a89029dd59651025ec350385`
   - correlation id: `h6-live-e2e-codex-2026-10-09`
   - terminal state: `succeeded`
   - response: `This H6 live ChatGPT Tunnel E2E completion probe reached Codex successfully.`

4. Claude completion probe:
   - task: `agt_0118ed6b9d4fc17be2023715`
   - correlation id: `h6-live-e2e-claude-2026-10-09`
   - terminal state: `succeeded`
   - provider confirmed the probe reached Claude; no tools or file mutations were used.

5. Qoder exact-output completion probe:
   - task: `agt_d27bb22c2ced249541f8a141`
   - model override: `qfmodel`
   - correlation id: `h6-live-e2e-qoder-exact-2026-10-09`
   - terminal state: `succeeded`
   - exact response: `H6_OK`

6. Live interaction/cancellation path was also exercised with Qoder during H6 recovery inspection:
   - task: `agt_b467a5c78ed5d7071021c6b3`
   - provider emitted an `approval.requested` event for a read-only Git command
   - ChatGPT answered it with `approve_once`
   - the same live task was then cancelled successfully

7. Event and task-state polling were exercised from ChatGPT with `get_agent_task` and `read_agent_task_events`; correlation identifiers and runtime manifests remained visible and consistent end to end.

## Notes

An earlier Qoder probe (`agt_a176cf2a75d240e74a6d9693`) succeeded but returned `H6_OK` instead of the longer literal string requested. Because that is provider instruction-following behavior rather than transport correctness, it was not used as the exact-output assertion; the later `agt_d27bb22c2ced249541f8a141` probe supplied the clean exact-output evidence.

Two temporary read-only recovery-inspection Agent tasks were cancelled after they had served their purpose. They made no repository changes.

## Acceptance conclusion

H6 is **CLOSED-PASS**. A live ChatGPT session successfully traversed the Secure MCP Tunnel to the native Windows ServerFS/Named-Pipe Agent Bridge and reached all three release-claimed provider runtimes. Completion responses, runtime/model discovery, normalized task/event polling, an approval round trip, and cancellation were all observed through the public MCP surface.

This closed release acceptance item 26 (`Live ChatGPT E2E succeeds before the stable tag`). H7–H9 have since closed and **v0.11.0 was tagged and published 2026-10-09** — the final post-refresh live manifest re-verification (three runtimes, `bridge_version: 0.11.0`) restored this phase to CLOSED-PASS after the `bridge_version` drift fix (`46794b0`).
