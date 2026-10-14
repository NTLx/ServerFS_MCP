# ServerFS v0.14.0 Development Plan

Status: implementation and Windows release acceptance complete; release closeout approved.

## Goal

Make the Windows native deployment persistent without ever requiring administrator privileges for installation, runtime, upgrade, inspection, recovery, or uninstall.

The current Windows deployment is manually launched by `deployment/windows/start-native.ps1`. Its internal lifecycle is already strong (sanitizer supervisor, per-user Agent lifecycle lease, Named Pipe identity, Job Object containment), but the outer `serverfs tunnel` process has no OS-owned persistent lifecycle. When that top-level tree exits, recovery currently requires a human to run the PowerShell launcher again.

v0.14.0 closes that outer lifecycle gap.

## Non-negotiable platform invariant

Windows remains entirely **current-user scoped and non-elevated**:

- no SCM/Windows Service registration;
- no `HKLM` service template or machine-wide installer;
- no UAC elevation, `runas`, administrator token, system account, stored account password, or `LogonType=Password`;
- no root/system-wide maintenance step;
- all lifecycle state stays under the current user's profile/application-data boundary;
- provider CLIs continue to run in the interactive user's security context and keep their existing user-owned configuration/authentication.

This is a project-wide deployment invariant, not an implementation convenience.

## Architecture decision

Use **Windows Task Scheduler 2.0** as the OS persistence backend for Windows native ServerFS.

The registered task is deliberately constrained to:

- current user SID;
- `InteractiveToken` logon type;
- `LeastPrivilege` run level;
- current-user `LogonTrigger` for sign-in startup;
- an indefinite one-minute `TimeTrigger` heartbeat for deterministic outer ownership recovery;
- `MultipleInstancesPolicy=IgnoreNew` so healthy ownership remains single-instance;
- no idle/network/battery gating;
- unlimited execution time;
- Task Scheduler `RestartOnFailure` retained as an additional safety net rather than the only recovery path.

The final implementation uses three explicit recovery layers. Task Scheduler owns a windowless ServerFS watchdog; the watchdog owns one managed runner in a kill-on-close Job Object and restarts it with a short bounded backoff; the runner owns one Tunnel generation at a time and restarts Tunnel generations with its own bounded backoff.

Normal topology:

```text
Windows Task Scheduler 2.0
  current user · InteractiveToken · LeastPrivilege
  LogonTrigger + 1-minute repeating TimeTrigger
  MultipleInstancesPolicy = IgnoreNew
  -> windowless ServerFS watchdog
       -> managed runner
            -> official tunnel-client
                 -> sanitizer supervisor
                      -> serverfs serve
                      -> Agent Bridge
                           -> Codex / Claude / Qoder
```

`serverfs-windows-native` remains the narrow Rust/NTFS kernel wheel. Service/persistence logic belongs to the Python product/deployment layer and must not be added to the filesystem kernel crate.

## Public CLI

Keep one cross-platform operator surface:

```text
serverfs service install --config <serverfs.toml> [--env-file <.env>]
serverfs service status
serverfs service start
serverfs service stop
serverfs service restart
serverfs service uninstall
```

`serverfs service ...` dispatches to:

- macOS: existing `SMAppService` implementation;
- Windows: Task Scheduler implementation;
- other platforms: fail closed with a clear unsupported-platform error.

Windows `install` registers/updates the persistent current-user task and private manifest. It does not require elevation. `start` enables and starts it; `stop` disables then ends the running task, so an intentional stop cannot be mistaken for a failure and restarted. `restart` performs stop/start. `uninstall` stops and removes only v0.14.0-owned current-user artifacts.

A hidden internal runner command may be used by the scheduled task, but it is not a public operational interface.

## Runtime configuration boundary

The task stores only absolute non-secret paths and identifiers. Secrets must never appear in Task Scheduler XML, argv, task names, logs, or status output.

The runner reads the existing untracked sibling `.env` and preserves the already-supported Windows launcher environment contract, including the dedicated Agent proxy/Bridge interpreter values needed before the sanitizer supervisor starts. Control-plane credentials continue to use the existing external per-user key file boundary.

## Reliability behavior

Recovery is layered and independently bounded:

1. the runner starts and contains one Tunnel generation, records its redacted exit/runtime evidence and restarts it with a 5s→60s bounded backoff;
2. the windowless watchdog contains the runner in a kill-on-close Job Object and restarts it with a 2s→30s bounded backoff;
3. Task Scheduler uses the current-user logon trigger plus an indefinite one-minute TimeTrigger heartbeat to recreate a missing outer Task action; `RestartOnFailure` remains an additional native safety net;
4. `MultipleInstancesPolicy=IgnoreNew` prevents the heartbeat from duplicating a healthy watchdog;
5. `service stop` disables the task before ending the action, so neither heartbeat nor failure recovery can resurrect an intentional stop;
6. a native current-user ownership mutex prevents managed and direct/manual Tunnel trees from coexisting.

A configuration/startup failure therefore retries slowly rather than crash-looping, while operator stop remains authoritative.

## Status / observability

`serverfs service status` on Windows must report a structured JSON snapshot including at least:

- platform/backend (`windows-task-scheduler`);
- registered/enabled state;
- whether the registered task still matches the v0.14.0 current-user/least-privilege contract;
- watchdog PID/liveness and runner PID/liveness when available;
- last runner restart evidence and last Tunnel generation start/exit evidence;
- watchdog→runner, runner→Tunnel and Task Scheduler restart/heartbeat policy evidence;
- log directory and lifecycle state paths;
- no credentials or proxy endpoints.

Runtime logs live under the current user's ServerFS application-data directory.

## Implementation slices

### A. Windows lifecycle product layer

Add `src/serverfs_mcp/windows_service.py`:

- Task Scheduler XML generation and validation;
- current-user SID binding;
- safe `schtasks.exe` invocation through the Windows system directory;
- install/start/stop/restart/status/uninstall;
- private manifest/runtime-state/log paths;
- lifecycle runner with bounded restart backoff;
- process/job cleanup on runner termination.

### B. Tunnel child containment

Extend the internal native Tunnel launcher narrowly so a caller may receive/contain the spawned `tunnel-client` process and redirect its stdout/stderr. Existing direct `serverfs tunnel` behavior must remain unchanged when these optional hooks are absent.

### C. CLI dispatch

Generalize `serverfs service ...` from darwin-only to explicit platform dispatch while preserving the existing macOS behavior.

### D. Tests

Add regression coverage for:

- Task XML binds exactly the current user SID;
- `InteractiveToken` + `LeastPrivilege` are mandatory;
- no password/elevation/SCM/HKLM contract appears;
- logon trigger and restart-on-failure settings are present;
- secrets are absent from task XML/argv;
- install uses only the current-user Task Scheduler path;
- stop disables before ending, preventing automatic relaunch;
- start enables then runs;
- restart ordering;
- uninstall removes only owned task/artifacts;
- status validation rejects/makes visible a drifted task definition;
- runner backoff and healthy-run reset;
- Tunnel child containment/cleanup on error;
- macOS `serverfs service` behavior remains unchanged.

### E. Windows live acceptance

On the WorkPC Windows 11 native environment, verify without elevation:

1. install/update the task;
2. inspect the registered XML and current-user principal;
3. start and reach live ChatGPT connectivity;
4. kill `tunnel-client` and observe runner-level recovery without manual launcher use;
5. kill the managed runner and observe watchdog-level recovery;
6. kill the Task Scheduler Action launcher and observe native heartbeat recovery;
7. `service stop` remains stopped and `service start` restores connectivity;
8. Agent delegation still works after managed restart;
9. the managed tree exposes no visible top-level terminal windows;
10. task remains configured to start at the next logon.

Earlier development evidence recorded Tunnel recovery ≈5.20s, runner recovery ≈3.09s, outer Task Action recovery ≈52.39s, and explicit start recovery ≈1.04s. The WorkPC service state was later disturbed during development, so those historical timings are not treated as fresh release evidence.

Final WorkPC release acceptance on 2026-10-14 ran from an observer outside the managed ServerFS tree under the non-elevated current user and revalidated every layered recovery boundary. Tunnel-generation recovery completed in about 6.4s while watchdog/runner identities remained stable; runner recovery completed in about 2.73s while the watchdog stayed stable; terminating the outer watchdog was recovered by the Task Scheduler heartbeat in about 18.5s; intentional `service stop` remained stopped after a 70s observation window; explicit `service start` restored the managed tree in about 1.1s; Agent Bridge/MCP recovered with Claude/Codex/Qoder available; a fresh workspace-write probe completed and released its writer lease/guard; the final tree was windowless, exactly-one, current-user and least-privilege. No repository/config/package/runtime-environment mutation or administrator path was used during acceptance. A physical reboot/logout-login cycle was not executed, so only the presence of the configured LogonTrigger is claimed, not measured reboot/re-login recovery. Full release evidence is recorded in `docs/v0.14.0-windows-live-acceptance-2026-10-14.md`.

## Release closeout

Before tagging v0.14.0:

- bump all release version surfaces to 0.14.0;
- update README, Windows deployment docs, maintainer invariants/workflow, release-facing English/Chinese site content;
- describe v0.14.0 as already released in current-state copy only at the final release-facing cleanup stage;
- keep historical plans/evidence historically accurate;
- run root lint/format/test gates plus Windows-native tests and live acceptance;
- run site build and stale current-state wording scan;
- commit/push final main;
- tag immutable `v0.14.0` on the verified commit;
- publish the GitHub Release from that tag;
- verify tag/release/CI state and the live WorkPC managed service.
