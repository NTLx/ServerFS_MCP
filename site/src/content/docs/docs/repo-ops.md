---
title: Deterministic Repo Ops
description: v0.23 Linux preview for bounded Git inspection, integrity-pinned repository actions, explicit-file commits, and current-upstream pushes without Agent delegation.
---

Deterministic Repo Ops is an optional **non-Agent repository capability** introduced in the v0.23 development source. It lets an MCP caller finish mechanical repository steps without receiving a shell and without delegating those steps to a server-side AI Agent.

The current stable release remains **v0.22.1**. Repo Ops is a Linux-only v0.23 development capability until public MCP end-to-end acceptance and release publication are complete.

## Product boundary

Repo Ops reuses the authenticated host Bridge transport because that process already owns the host workdir path, the current user's Git/network credential context, and the shared writer lease. It does **not** reuse the Agent execution path.

Repo Ops never:

- selects a model or runtime;
- creates an Agent task or provider session;
- calls a provider adapter;
- exposes a shell or generic command executor;
- accepts caller-controlled executable, argv, environment, cwd, remote, ref, force flag, or shell syntax;
- automatically falls back to `submit_agent_task`.

A workdir may enable Repo Ops while Agent mode remains disabled.

## Public MCP tools

Repo Ops adds exactly four tools when at least one workdir explicitly enables it:

| Tool | Purpose | Mutation |
| --- | --- | --- |
| `inspect_repo` | Inspect bounded Git status/diff evidence or list configured named actions | No |
| `run_repo_action` | Run one administrator-defined fixed repository action | Serialized writer operation |
| `commit_repo_changes` | Commit only explicitly requested paths with a fixed hardened Git path | Yes |
| `push_repo` | Push the current branch to its configured upstream with one exact refspec | Yes |

`inspect_repo` supports `status`, `diff`, `staged_diff`, and `actions` views. Diff views require explicit workdir-relative paths that already passed ServerFS path/deny policy.

`commit_repo_changes` refuses pre-existing staged state, stages only requested paths, rejects external Git content filters, disables hooks and commit signing for this deterministic path, and returns the resulting commit SHA. It never pushes automatically.

`push_repo` derives the current branch/upstream inside the host Bridge and pushes exactly `HEAD:<upstream-branch>` to the derived remote. The caller cannot choose a remote/ref or request force semantics. Git is non-interactive and provider/API secrets are not forwarded into the child process environment.

## Per-workdir configuration

Repo Ops defaults off and is enabled independently for each Linux/Compose workdir:

```text
WORKDIR_01_ALIAS=project
WORKDIR_01_PATH=/srv/project
WORKDIR_01_READ_ONLY=false
WORKDIR_01_REPO_OPS_ENABLED=true
WORKDIR_01_REPO_ACTIONS_JSON={...}
```

The authenticated host Bridge transport must be enabled, but no Agent runtime needs to be enabled. `WORKDIR_XX_REPO_ACTIONS_JSON` is private host-side policy: it is rendered into the Bridge configuration and is intentionally not forwarded into the MCP container.

Repo Ops named actions require a read-write workdir. Git inspection can be used without a named action, while commit/push and every named action participate in the shared exclusive writer lease.

See [Configuration](../configuration/) for the workdir policy model and the versioned [`.env.example`](https://github.com/NTLx/ServerFS_MCP/blob/main/.env.example) for the complete Linux variable surface.

## Named actions

`run_repo_action` is a generic **policy mechanism**, not a project-specific command API. Each workdir may define its own administrator-owned action profiles. The caller chooses only the configured action name and, when the profile allows it, one workdir-relative path.

An action profile contains:

- `argv`: a fixed non-empty argv array owned by the operator;
- `mutates`: descriptive/audit metadata;
- `argument`: `none`, `path`, or `basename`;
- `integrity`: a non-empty mapping of workdir-relative files to lowercase SHA-256 digests.

Illustrative structure:

```json
{
  "quality_gate": {
    "argv": ["uv", "run", "--frozen", "--no-sync", "python", "-I", "tools/check_repo.py"],
    "mutates": false,
    "argument": "none",
    "integrity": {
      "tools/check_repo.py": "<reviewed-lowercase-sha256>",
      "pyproject.toml": "<reviewed-lowercase-sha256>",
      "uv.lock": "<reviewed-lowercase-sha256>"
    }
  }
}
```

Replace every placeholder with a real reviewed digest before deployment. The renderer rejects malformed action profiles.

Integrity pinning is mandatory because a fixed argv that executes mutable repository code would otherwise become an indirect arbitrary-code execution primitive. Before process creation the Bridge independently verifies that every pinned input is confined, regular, within size limits, and digest-identical. A mismatch fails closed.

Do not automatically update integrity pins in the same task that changed the pinned repository code. Treat a pin update as an operator review decision.

## Writer lease and process lifecycle

Repo Ops commit, push, and **all named actions** acquire the same exclusive writer lease used by ServerFS filesystem mutations and Agent workspace-write tasks. Named actions take the lease even when `mutates=false`; that flag cannot prove side-effect freedom and must not create a TOCTOU gap between integrity verification and execution.

Linux child execution is bounded by time and output. Timeout, output overflow, or cancellation terminates the entire Repo Ops subprocess group and drains its pipes before the lease is released.

## Security invariants

Repo Ops keeps the existing ServerFS trust model:

- all caller paths pass MCP-side path/deny policy before Bridge RPC;
- the Bridge independently confines host paths again;
- hidden/credential-denied paths are filtered from status and cannot be requested for diff/commit;
- child environments are allowlisted instead of inheriting Bridge/provider secrets wholesale;
- Git hooks, askpass overrides, repository SSH-command overrides, external content filters, and commit signing are neutralized on hardened deterministic paths;
- Git transport is restricted to approved protocols and custom remote helpers are not caller-selectable;
- errors and results do not reveal absolute host workdir paths;
- Repo Ops code does not import or call Agent provider adapters.

See [Security Model](../security/) and [Architecture](../architecture/) for the adjacent trust boundaries.

## Capability counts in v0.23 development source

The stable v0.22.1 release remains 11 / 13 / 21 / 23 tools for filesystem, binary, and Agent combinations. The v0.23 development source adds four independently gated Repo Ops tools:

| Surface | Tools |
| --- | ---: |
| Filesystem | 11 |
| Filesystem + binary | 13 |
| Filesystem + Repo Ops | 15 |
| Filesystem + binary + Repo Ops | 17 |
| Filesystem + Agent | 21 |
| Filesystem + binary + Agent | 23 |
| Filesystem + Agent + Repo Ops | 25 |
| Filesystem + binary + Agent + Repo Ops | 27 |

## Current v0.23 acceptance state

As of 2026-10-26, the Linux bootstrap/internal E2E is **CLOSED-PASS** and the public ChatGPT MCP E2E is **PASS**:

- root suite: 1780 passed, 320 skipped; one upstream Starlette deprecation warning;
- Agent Bridge suite: 497 passed, 15 skipped, no warnings;
- Ruff and format gates: PASS;
- pre-documentation-freeze site build: PASS (37 pages in the executed bootstrap); the post-freeze site rebuild is still pending;
- scratch MCP image build: PASS;
- user-scoped Host Bridge verification: PASS;
- internal MCP Repo Ops surface verification: PASS;
- configured Host Repo Ops action-set verification: PASS;
- public ChatGPT MCP E2E: PASS — `actions → status → wiki_lint → explicit diff → explicit-file commit → current-upstream push`, no Agent task; commit `b3621bc5342e834990c170720de149d846d7de34` pushed to `LLM_WiKi` `origin/main` and final status clean.

The executed evidence is recorded in [`docs/v0.23.0-linux-repo-ops-bootstrap-acceptance-2026-10-26.md`](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.23.0-linux-repo-ops-bootstrap-acceptance-2026-10-26.md).

A later operational-Skills audit changed `skills/**` and `tests/test_agent_skills.py`, so the bootstrap root-suite result no longer represents the final source state. The remaining release gates are refreshed root/Agent-Skills validation, the fresh post-documentation-freeze site build/diff check, and the normal versioning/publication workflow. Until those succeed and v0.23 is published, this page describes a development capability rather than a stable-release guarantee.
