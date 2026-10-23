---
name: serverfs-agent-bridge
description: Enable workdir Agent delegation; operate Bridge lifecycle, native Codex/Claude/Qoder or operator-configured ACP profiles, proxy routing, Jev advice, or task/HITL failures.
---

# Operate ServerFS Agent Bridge

Agent delegation is an explicit optional capability. Preserve the two-process security boundary, per-workdir authorization, provider-native ownership and current-user lifecycle.

The current published stable is v0.22.0. Generic ACP v1 profiles were introduced in v0.19.0 and remain distinct from native `codex`, `claude` and `qoder` adapters. See the [ACP Guide](https://ntlx.github.io/ServerFS_MCP/docs/acp/) for complete configuration and operations.

## When to use

Use this skill when the user explicitly wants to:

- enable/disable Agent delegation for selected workdirs;
- install/update/verify the Linux host Agent Bridge;
- configure native Windows/macOS Agent policy and Bridge lifecycle;
- allow Codex, Claude or Qoder runtimes;
- configure an operator-owned ACP v1 profile (`acp-<id>`);
- configure Agent-specific proxy routing or optional Jev advisory features;
- diagnose missing Agent tools, unavailable runtimes, Bridge IPC/identity errors, task lifecycle/HITL problems or provider parity issues.

## When NOT to use

Do not use this skill to:

- grant filesystem writes unless the user explicitly authorizes the workdir write policy;
- alter provider authentication/default permissions/sandbox policy merely to make a task run;
- upgrade provider CLIs outside the requested ServerFS/Agent operational objective;
- configure OpenAI Tunnel or HTTP Auth except where verifying their independence;
- diagnose an unknown whole-system outage before the Agent boundary is identified (`serverfs-diagnose`).

Jev is advisory only. It is not an Agent runtime, approval authority or fallback executor.

## Preflight

First locate the target ServerFS checkout or deployed installation and establish its version, active configuration and lifecycle owner. In imported/standalone mode, if no authoritative checkout/deployment source is accessible, stop at bounded guidance; do not mutate or infer target commands/settings.

Paths below are relative to the located **ServerFS checkout root**, never to this skill or the Plugin ZIP. Resolve repository paths only after locating that checkout. For an installed deployment without a checkout, use its version-matched deployment documentation, installed CLI help and active configuration; obtain matching sources before a step that needs missing repository material.

1. Identify platform and deployment family.
2. Read current workdir read-only and Agent policy before changing it.
3. Identify enabled runtimes and their actual provider binaries/native environment.
4. Locate provider credentials/private state and ensure they remain outside exposed workdirs and the MCP container.
5. Determine current Bridge/service owner and whether active Agent tasks/writer leases exist before any restart/update.
6. On Linux, measure/verify the real container peer identity; never assume `id -u/-g` is equivalent to the socket peer measurement.
7. Keep Tunnel, Agent and Jev proxy switches independent.

If provider policy or credential ownership is unclear, stop rather than copying interactive shell state wholesale or changing provider defaults.

## Authoritative sources

- `deployment/agent-bridge/README.md`
- `site/src/content/docs/docs/agent-bridge.md`
- `site/src/content/docs/docs/jev-advisors.md`
- `site/src/content/docs/docs/windows-native.md`
- `site/src/content/docs/docs/macos-native.md`
- `serverfs.toml.example`
- `.env.example`
- current `serverfs agent-bridge`, Agent task/runtime tools and Bridge verification behavior.

The Linux deployment guide is the authoritative step-by-step source for peer identity, user-owned paths, provider env, installer and rollback behavior. Do not duplicate its complete variable table here.

## Procedure

1. **Define the authorization boundary first.** Agent delegation is enabled only for selected workdirs/runtimes. ACP profiles always require `workspace-write`; reject them on read-only workdirs and never widen workdir policy silently. Record explicit authorization before enablement.
2. **Establish the provider-native baseline.** Confirm the selected provider CLI works for the same login user before blaming ServerFS. Preserve provider-owned auth and permission configuration. Proceed after direct CLI availability/version is observed for the same user.
3. **Configure only the required ServerFS policy.** Use Linux `.env` or native TOML as documented; keep provider secrets/private env in their dedicated user-owned boundary. Review the local policy diff and pass the applicable parser/doctor or Compose gate.
4. **Install/configure the Bridge through supported current-user lifecycle.** Do not create system users, root services or administrator services. Confirm status identifies the supported current-user owner.
5. **Verify IPC/identity before Agent tasks.** Prove Bridge status/runtime discovery and container/native connectivity. Proceed only after status, runtime discovery and applicable peer/RPC checks pass.
6. **Verify provider parity.** Compare direct provider version/availability with normalized ServerFS runtime evidence. Treat stale managed provider daemons using the provider's official lifecycle, not arbitrary kill/delete operations. Record direct and Bridge-reported runtime evidence; resolve unexplained differences before task testing.
7. **Exercise a disposable Agent task only after structural checks pass.** Use a safe workdir and verify poll/result/cancel/HITL as required by the user's objective. Record a terminal result and lease release; when cancel/HITL is exercised, verify those outcomes too.
8. **Preserve writer-lease semantics.** Never bypass a legitimate `WORKDIR_BUSY` by deleting locks or launching an uncontrolled parallel writer. Report busy/recovery state and use the documented lifecycle before retrying.

## ACP profile workflow (v0.19.0)

ACP is the standards-based path for compatible Agents. Follow standard-protocol-first: do not add a provider-specific native runtime unless the Native runtime admission gate in `docs/maintainers/invariants.md` is satisfied, including an explicit maintainer `PASS` recorded after review. An Agent must stop before native-adapter source implementation without that approval.

1. Treat each `acp-<id>` as an operator-configured profile, separate from native Codex/Claude/Qoder. Configure an absolute executable path and fixed bounded argv (`SERVERFS_ACP_PROFILES_JSON` on Linux; `[[agent.acp_profiles]]` on native platforms). Task callers cannot supply command, argv or environment. Never turn an ACP task into command execution.
2. Confirm the selected workdir is explicitly `workspace-write`. ACP's native sandbox is not controlled by ServerFS; never enable it by silently converting a read-only workdir. Keep provider authentication and credentials provider-owned.
3. Treat ACP Registry as discovery/reference metadata only. Never auto-install, trust or authorize a Registry entry. Runtime listing/probe may inspect local executable/config and cached measured capabilities, but must not launch an Agent solely to discover capabilities.
4. Use only capability evidence measured for that configured profile. ACP model discovery/override, elicitation, client filesystem and terminal are unsupported; do not invent or infer them. For permission requests, show the offered options and require the human to select the exact offered option ID; never synthesize, broaden or auto-select one.
5. Cancellation is terminal-first, but protocol cancel or a final response alone does not prove the child stopped. Require platform task-tree stop evidence before releasing the writer lease/clearing recovery state. If stop is uncertain, fail closed and retain the recovery guard; never blindly rerun an in-flight task after restart. Session resumability is not old-process stop evidence.

## Platform branches

Load only the matching branch after identifying the deployment and requested mode:

- Linux / Docker + host Bridge: read [linux branch](references/linux.md) before platform/mode-specific changes.
- Windows native: read [windows branch](references/windows.md) before platform/mode-specific changes.
- macOS native: read [macos branch](references/macos.md) before platform/mode-specific changes.

## Correlated Agent Diagnostics (v0.22+ / feature-detect)

These diagnostics are available only in versions that implement the v0.22 feature; they are not a v0.21 capability. First check the installed version or inspect `serverfs diagnostics --help` (or the equivalent CLI capability listing). Proceed only when the commands are advertised by the target installation.

When supported, use `serverfs diagnostics snapshot --format json` for a bounded Bridge snapshot and `serverfs diagnostics task <task-id> --format json` for one retained task. Omitting `--format json` selects human rendering. The CLI uses the existing private Bridge RPC; this does not add a public MCP tool or listener. Diagnostic state is bounded and process-local. Task disposition distinguishes `available`, `unknown_task`, `not_retained`, and CLI-level `bridge_unavailable`. A Bridge restart does not fabricate pre-restart history; unavailable prior state remains unavailable.

Treat these outputs as diagnostic evidence, not persistent task history. If capability detection fails, use the target version's supported status/runtime surfaces and continue with the matching diagnostic workflow; do not invoke commands absent from its help.

## Safety / stop conditions

Stop when:

- Agent enablement would require widening a workdir from read-only without explicit user approval;
- Linux measured peer identity differs from the current user;
- provider secrets would enter `.env`/TOML/workdir/container contrary to the current documented boundary;
- provider authentication/permission settings would have to be silently changed;
- a Bridge restart/update would interrupt active tasks without authorization;
- an active writer lease exists and someone proposes deleting/bypassing it;
- a stale provider daemon/process cannot be proven and safely handed over through its official lifecycle;
- Agent/Tunnel/Jev proxy settings are being conflated;
- root/Admin ownership is proposed as a fix.
- Do not extend recorded ACP acceptance evidence into a compatibility guarantee for all Agents or versions.

Never manually delete a live control socket, lock or Bridge state as a first-line repair. Use documented lifecycle/rollback mechanisms.

## Verification

At the minimum level appropriate to the task, prove:

- intended workdir Agent mode/runtime allowlist is the effective policy;
- Bridge/service is owned by the current user and IPC identity checks pass;
- selected provider runtimes report the expected availability;
- direct provider and ServerFS-reported runtime versions/availability are consistent enough for the supported provider contract;
- a disposable task can be submitted/polled to terminal state when execution verification is requested;
- cancellation/HITL paths work when they are part of the changed boundary;
- writer lease is acquired/released correctly;
- Jev advice, if enabled, remains advisory and absence/failure does not gain execution authority;
- no provider credential appears in MCP container/config/log/task events.

Linux release/upgrade acceptance should use `verify_host.py --require-runtimes` and prove the container-to-Bridge RPC through the documented path.

## Success criteria

The task is complete when Agent delegation is enabled only where requested, uses the supported current-user Bridge/lifecycle, exposes only the intended runtime set, preserves provider-owned security configuration, and passes structural plus runtime verification appropriate to the change.

## Failure handling

If the Bridge is healthy but a provider runtime is unavailable, diagnose the provider-native lifecycle/environment for the same user; do not alter ServerFS filesystem security to compensate.

If an update fails on Linux, use the documented `deployment/agent-bridge/rollback_app.sh` path rather than manually swapping releases. If a provider managed daemon is stale, use its official update/bootstrap/hand-over flow after proving active work is quiescent.

Report whether failure is policy, IPC identity, Bridge lifecycle, provider availability, task lifecycle, proxy, Jev or writer-lease related. Do not collapse these boundaries into a generic "Agent failed" diagnosis.
