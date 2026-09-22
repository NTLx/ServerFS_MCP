# Jev Runtime Router + Preflight — live validation 2026-09-22

Branch: `experiment/jev-agent-preflight`

Runtime Router implementation commit:

```text
c232e0917a9f51594fc309bd1caa8601dd86daf8
experiment: add Jev runtime router advisor
```

This note records the first joint live evaluation of the Jev Runtime Router and the
existing Agent Task Preflight. The real TypeSafe API key remained only in the ignored
repository `.env` / private Bridge config and is not recorded here.

## Verification before live evaluation

The updated Agent Bridge passed:

```text
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
97 passed
```

The root repository regression gate passed:

```text
git diff --check
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
803 passed
docker compose --env-file .env -f compose.yml -f compose.agent.yml config --quiet
```

The running Bridge returned both `preflight` and `routing_advice`, and persisted separate
`task.preflight` and `task.routing_advice` events.

## Joint live corpus

### A. Chinese bounded file read

Task intent: read only the first line of `AGENTS.md`, with shell/Git/tests/network and
mutations explicitly forbidden.

Expected route: `direct_serverfs_tool`.

Result:

```text
single_objective               0.97
mutation_boundary_explicit     0.97
stop_condition_explicit        0.92
verification_evidence_explicit 0.86
execution_fit                  structured_serverfs
structured_serverfs            1.00

route                         direct_serverfs_tool
direct_serverfs_tool          0.96
codex                         0.04
claude                        0.00
human_review                  0.00
matches requested codex       false
```

Result direction: correct. The explicit Agent submission was more powerful than necessary,
and the Router detected that.

### B. English Git + pytest verification

Task intent: run exactly `git diff --check` and one pytest target, with no mutation.

Expected route: `codex`.

Result:

```text
single_objective               0.57
mutation_boundary_explicit     0.97
stop_condition_explicit        0.96
verification_evidence_explicit 0.98
execution_fit                  native_agent
native_agent                   1.00

route                         codex
codex                         1.00
matches requested codex       true
```

The task completed successfully: `git diff --check` exited 0 and 46 tests passed.

Result direction: route correct. The pre-existing atomicity caveat is reproduced: one
verification objective containing two explicit commands receives only 0.57
`single_objective`. This confirms that no high atomicity cutoff should currently block
execution.

### C. Claude request on a dual-runtime workdir

The production `ServerFS` workdir currently rejects Claude deterministically before Jev
because Claude is not allowed there. This is correct: the Router cannot override runtime
policy.

The Claude sample therefore used the dedicated `agent-e2e` workdir, where both providers
are allowed.

A lightweight task explicitly asked to use Claude Code but was otherwise simple enough for
direct inspection:

```text
execution_fit                  structured_serverfs
structured_serverfs            0.66
native_agent                   0.32

route                         claude
direct_serverfs_tool          0.48
claude                        0.51
route confidence              0.35
matches requested claude      true
```

This is a useful conflict signal: explicit provider intent slightly wins, but the Router
correctly expresses low confidence because a less powerful direct route could perform the
actual work.

### D. Explicit Claude-native requirement submitted as Codex

Task intent: inspect Claude Code user/project/local setting sources visible to a native
Claude session. The task explicitly requires Claude-native state, but was deliberately
submitted with `runtime=codex`.

Expected route: `claude`; expected mismatch: true.

Result:

```text
single_objective               0.94
mutation_boundary_explicit     0.97
stop_condition_explicit        0.90
verification_evidence_explicit 0.75
execution_fit                  native_agent
native_agent                   0.67

route                         claude
direct_serverfs_tool          0.01
codex                         0.00
claude                        0.99
human_review                  0.00
route confidence              0.98
matches requested codex       false
```

The Agent task was cancelled immediately after the advisory result.

Result direction: correct. This is the strongest evidence that the Router can identify a
provider-selection mismatch while leaving the explicit runtime untouched.

### E. Chinese maintainer release decision

Task intent: decide whether an experimental feature should be merged and formally released,
explicitly framing the decision as one requiring risk and maintenance-policy judgment.

Expected route: `human_review`.

Result:

```text
single_objective               0.89
mutation_boundary_explicit     0.96
stop_condition_explicit        0.78
verification_evidence_explicit 0.22
execution_fit                  unclear
structured_serverfs            0.26
native_agent                   0.36
unclear                        0.38
execution-fit confidence       0.07

route                         human_review
direct_serverfs_tool          0.03
codex                         0.03
claude                        0.00
human_review                  0.94
route confidence              0.92
matches requested codex       false
```

The task was cancelled immediately after advisory evaluation.

Result direction: correct. The Router identifies the human decision boundary much more
clearly than the older coarse `execution_fit` Choice.

### F. English broad / underspecified repository review

Task:

```text
Take a broad look around this workdir and tell me what you think is worth doing next.
Do not modify anything.
```

Result:

```text
single_objective               0.51
mutation_boundary_explicit     0.93
stop_condition_explicit        0.45
verification_evidence_explicit 0.09
execution_fit                  structured_serverfs
structured_serverfs            0.53
native_agent                   0.46

route                         direct_serverfs_tool
direct_serverfs_tool          0.73
codex                         0.25
claude                        0.01
human_review                  0.01
route confidence              0.65
```

The task was cancelled immediately after advisory evaluation.

This was the most important negative result in the first live pass. The Router alone did
**not** initially treat this vague task as `human_review`; it chose a technically capable
execution path instead. The existing Preflight still exposed the weakness through
verification evidence at 0.09 and stop-condition clarity at 0.45.

The Router criteria were then tightened in commit `f34455514f2fdb55fa63c2f99d9cff83c4d69740`
to explicitly treat open-ended prioritization requests without decision criteria as a
human-review case. After staging and activating that release, the exact same prompt was
retested:

```text
single_objective               0.14
mutation_boundary_explicit     0.93
stop_condition_explicit        0.47
verification_evidence_explicit 0.10
execution_fit                  structured_serverfs
structured_serverfs            0.57
native_agent                   0.42

route                         human_review
direct_serverfs_tool          0.11
codex                         0.04
claude                        0.00
human_review                  0.85
route confidence              0.80
matches requested codex       false
```

The task was cancelled immediately after advisory evaluation. This retest demonstrates that
the Router can be calibrated against concrete false-routing evidence while the independent
Preflight quality signals remain stable. Runtime Router and Task Preflight should still be
interpreted jointly.

### G. Chinese deliberately bundled task

The prompt bundled five independent objectives: list files, check Git, run all tests,
summarize architecture, and propose three future changes.

Result:

```text
single_objective               0.03
mutation_boundary_explicit     0.96
stop_condition_explicit        0.78
verification_evidence_explicit 0.88
execution_fit                  native_agent
native_agent                   1.00

route                         codex
codex                         0.99
```

The task was cancelled immediately after advisory evaluation.

Result direction: excellent for the existing Preflight. `single_objective=0.03` sharply
separates an intentionally bundled request from the bounded examples.

## Persistence verification

For both the provider-mismatch and human-review samples, SQLite-backed event retrieval
confirmed that the Bridge persisted:

```text
task.preflight
task.routing_advice
```

before the native Agent execution events. Cancelling the task afterward did not remove the
advisory evidence.

## Findings

The Runtime Router is operational and useful as an advisory feature.

All four route labels were exercised successfully:

```text
direct_serverfs_tool  -> bounded filesystem read
codex                 -> Git + pytest / general coding-agent work
claude                -> explicit Claude-native requirement
human_review          -> maintainer release decision
```

The strongest positive result is provider mismatch detection: a task submitted to Codex but
requiring Claude-native state received `claude=0.99` and
`matches_requested_runtime=false`.

The most useful negative result was the first vague-repository-review pass: the Router
recommended `direct_serverfs_tool=0.73` even though the task was poorly specified. That
failure directly produced the `f344555` criteria refinement. After activation, the exact
same prompt moved to `human_review=0.85`, while the original Preflight continued to flag
weak task quality (`verification_evidence=0.10`, `stop_condition=0.47`).

This establishes a practical separation of concerns:

- Runtime Router answers **where/capability-wise the task appears to fit**.
- Task Preflight answers **whether the task itself is sufficiently bounded and verifiable**.
- Deterministic ServerFS policy answers **whether the requested operation is authorized**.

None of these probabilistic results should replace deterministic authorization.

## Current recommendation

Keep both features advisory-only.

Do not add `runtime=auto` yet. Do not automatically redirect a submitted task. Do not
automatically reject a task based on a single Preflight threshold.

For a future larger corpus, evaluate a composite policy rather than a single score. For
example, an automatic-routing experiment should only be considered when:

- route recommendation confidence is high;
- task-quality signals are not materially weak;
- the recommended route is deterministically allowed for the workdir;
- false-routing cost has been measured;
- Chinese and English behavior are both calibrated.

The live evidence now supports proceeding to corpus-level calibration, but not production
automatic routing.
