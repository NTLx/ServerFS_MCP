# Jev Agent Task Preflight — live validation 2026-09-22

Branch: `experiment/jev-agent-preflight`

Implementation commit under test:

```text
a91140c181361d6d016c02c03d2766e664a2b654
experiment: add Jev agent task preflight
```

This note records the first live validation with a real TypeSafe API key. No API key or
private configuration value is recorded here.

## Activation

The repository `.env` contained a non-empty `SERVERFS_JEV_API_KEY` and remained ignored
by Git.

The Agent Bridge was first installed with the normal user-scoped installer using
`--no-start`. The new private Bridge config was a regular current-user-owned file with
mode `0600`. The installed release loaded that config as:

```text
config.jev.enabled = True
config.jev.api_key is not None = True
serverfs-agent-bridge = 0.4.0
typesafe-sdk = 0.7.1
```

Before activation, the Bridge SQLite state database was checked read-only. The only
non-terminal task was the activation task itself. The user service was then restarted with
exactly:

```text
systemctl --user restart serverfs-agent-bridge.service
```

The activation task was consequently persisted as:

```text
status = interrupted
error_code = BRIDGE_SHUTDOWN
error_message = bridge stopped while task was active
```

This was expected. After restart, ServerFS MCP reconnected successfully and both native
runtimes were available again.

## First post-restart request

The first real `submit_agent_task` after restart produced:

```json
{"status":"unavailable"}
```

for its preflight. The already-authorized Agent task still ran and succeeded, demonstrating
the intended fail-open advisory behavior. The unavailable result was also persisted as the
task's `task.preflight` event.

No root cause was recoverable from that event because the current experiment intentionally
collapses Jev exceptions to the single `unavailable` state.

## Subsequent connectivity

The next four real Agent submissions all received successful Bridge-side Jev preflights.
A separate direct call through the installed `JevTaskPreflight` also succeeded using the
same installed config, SDK, model, and API key.

The direct synthetic task:

```text
Read one file and report its title. Do not modify anything.
```

returned:

```text
single_objective               0.95
mutation_boundary_explicit     0.95
stop_condition_explicit        0.67
verification_evidence_explicit 0.76
execution_fit                  structured_serverfs
structured_serverfs probability 0.86
native_agent probability        0.00
unclear probability             0.14
```

This confirms that the installed `typesafe-sdk==0.7.1`, pinned `jev-1.13.0`, network
path, API key, request schema, and response parser are interoperating successfully.

## Three representative Agent tasks

### 1. Bounded file read

Task:

```text
Read-only bounded check. Read only the first line of AGENTS.md and report that exact line.
Do not run tests, Git commands, network requests, or any mutation. Stop immediately after
reporting the first line.
```

Preflight:

```text
single_objective               0.97
mutation_boundary_explicit     0.97
stop_condition_explicit        0.95
verification_evidence_explicit 0.95
execution_fit                  structured_serverfs
structured_serverfs probability 1.00
native_agent probability        0.00
unclear probability             0.00
```

The delegated Codex task succeeded and returned `# AGENTS.md`.

This is the desired signal: the task was submitted through the Agent surface, but Jev
correctly identified that bounded ServerFS primitives were sufficient.

### 2. Git + pytest verification

Task:

```text
Verification-only native-agent check. Run exactly git diff --check and then
uv run pytest tests/test_agent_deployment.py -q. Do not modify files, do not run a
formatter, do not commit/push/change branches, and do not run network requests. Stop at
the first failure; otherwise report both exit statuses and the pytest pass count.
```

Preflight:

```text
single_objective               0.57
mutation_boundary_explicit     0.97
stop_condition_explicit        0.96
verification_evidence_explicit 0.98
execution_fit                  native_agent
structured_serverfs probability 0.00
native_agent probability        1.00
unclear probability             0.00
```

The task succeeded: `git diff --check` exited 0 and 46 deployment tests passed.

The execution-fit result is correct. The lower `single_objective` value is also important:
a single verification objective containing two explicit commands can look partially
multi-objective to the model. This is evidence against using a high atomicity threshold as
an automatic blocker without calibration.

### 3. Broad, weakly specified review

Task:

```text
Take a broad look around this repository and tell me what you think is worth doing next.
Do not modify anything.
```

Preflight:

```text
single_objective               0.40
mutation_boundary_explicit     0.94
stop_condition_explicit        0.45
verification_evidence_explicit 0.09
execution_fit                  native_agent
execution_fit confidence       0.34
structured_serverfs probability 0.44
native_agent probability        0.55
unclear probability             0.01
```

The task was cancelled immediately after preflight so it would not perform unnecessary
broad repository work.

The four quality signals behave usefully here: atomicity, stop condition, and especially
verification evidence fall sharply, while the explicit read-only mutation boundary remains
high. The `execution_fit` Choice itself is low-confidence rather than selecting
`unclear`; therefore `unclear` should not be treated as the sole ambiguity detector.

## Current conclusions

The live PoC is operational.

The evidence supports using Jev as an advisory measurement layer for delegated task quality
and execution fit. It does not yet support making Jev authoritative.

In particular:

- keep deterministic ServerFS authorization, workdir policy, runtime allowlists, approvals,
  and writer leases unchanged;
- keep Jev fail-open;
- do not block a task using any current Noul threshold;
- do not automatically reroute tasks yet;
- evaluate the individual quality signals separately from `execution_fit`;
- calibrate on a larger corpus of real historical ServerFS tasks before choosing thresholds;
- include both Chinese and English prompts in that corpus;
- measure preflight availability as well as classification quality, because the first live
  post-restart request was unavailable while subsequent calls succeeded.

The current experiment is ready for corpus-level evaluation.
