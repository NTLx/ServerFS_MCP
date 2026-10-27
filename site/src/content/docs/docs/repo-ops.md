---
title: Deterministic Repo Ops
description: ServerFS v0.23.2 Deterministic Repo Ops for bounded Git inspection, integrity-pinned repository actions, explicit-file commits, exact current-upstream pushes, and fixed npm release actions without Agent delegation on Linux, validated native macOS, and validated native Windows.
---

Deterministic Repo Ops is an optional **non-Agent repository capability** released on Linux Host Bridge in **v0.23.0**, extended to validated native Apple Silicon macOS in **v0.23.1**, and extended again to validated native Windows 11 x64 in **v0.23.2**. It lets an MCP caller finish mechanical repository steps without receiving a shell and without delegating those steps to a server-side AI Agent.

The current stable release is **v0.23.2**. Repo Ops is supported on Linux Host Bridge, validated native Apple Silicon macOS, and validated native Windows 11 x64.

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

Repo Ops defaults off and is enabled independently for each workdir. Linux/Compose uses environment variables such as:

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

A command action profile (`kind: "command"`, the default) contains:

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

## Release tags and fixed npm workflow dispatch

v0.23.0 Linux deployments can opt an npm/`package.json` release workdir into two built-in
action kinds through `WORKDIR_XX_REPO_ACTIONS_JSON`. These retain the same four public
tools and exclusive writer lease. The administrator fixes the repository; the caller supplies
only an action name and `release_tag`. Built-ins accept no executable or argv configuration.
The generic Repo Ops surface is repository-generic; these two built-ins intentionally implement
a narrower npm release contract and are not a universal release abstraction for every repository type.

```json
{
  "tag_release": {
    "kind": "release_tag",
    "repository": "example/project",
    "argument": "release_tag",
    "mutates": true,
    "integrity": {
      "package.json": "<reviewed-lowercase-sha256>",
      ".github/workflows/publish.yml": "<reviewed-lowercase-sha256>"
    }
  },
  "publish_npm": {
    "kind": "publish_npm",
    "repository": "example/project",
    "argument": "release_tag",
    "mutates": true,
    "integrity": {
      "package.json": "<reviewed-lowercase-sha256>",
      ".github/workflows/publish.yml": "<reviewed-lowercase-sha256>"
    }
  }
}
```

Replace the repository and placeholders after reviewing those inputs. Both pins are
mandatory; add pins for other reviewed release inputs when appropriate. Changes to pinned
files require a separate operator review and pin update. No automatic pin refresh occurs.
This policy does not edit the target repository or its workflow.

Discover profiles with `inspect_repo(workdir="DingtalkCard", view="actions")`, then call:

```text
run_repo_action(workdir="DingtalkCard", action="tag_release", release_tag="v1.1.0-beta.3")
run_repo_action(workdir="DingtalkCard", action="publish_npm", release_tag="v1.1.0-beta.3")
```

The tag must fully match
`v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-beta\.(0|[1-9][0-9]*))?`
and fit in 128 characters. Leading zeroes, paths, branch names, whitespace, shell
syntax and arbitrary prerelease channels fail closed. `path` and `release_tag` are mutually
exclusive. Unknown public action arguments, including `force`, `commit`, `remote`, `ref`
and `workflow`, are rejected rather than ignored.

Both actions require the workdir to be writable, its Git root/metadata confined, a clean
working tree/index, `main` tracking `origin/main`, the root `package.json` version in HEAD
to match the tag, and the actual remote `main` to equal HEAD. Fetch/push URLs must each resolve to
one standard HTTPS/SSH URL for the configured GitHub repository. Git hooks, SSH-command
and askpass overrides, mirror/follow-tags/submodule pushes and custom transport helpers
are disabled. Children use the existing bounded execution and narrow environment.

`tag_release` creates a lightweight tag using compare-and-create, pointing only to the
captured current HEAD. It pushes only that object to the exact `refs/tags/<release_tag>`.
Existing local/remote tags are peeled to commits: the same HEAD is idempotent success;
a different commit is `REPO_TAG_CONFLICT` and is never replaced. It verifies the remote
tag after push. A failed push can leave the newly created local tag; retry the same action
after resolving the reported error. It never changes a branch or forces a ref.

`publish_npm` requires the remote release tag already to resolve to current HEAD. An optional administrator-only
`github_cli` absolute executable path selects an existing GitHub CLI when the Bridge's
PATH does not contain `gh`; the default is `gh`. It never appears in caller input or
changes provider PATH/authentication. It uses
the current user's existing GitHub CLI authentication and dispatches only `publish.yml`
in the fixed repository, with only the validated tag as ref and no workflow inputs.
It returns dispatch acceptance, not npm publication success. Dispatch is **not idempotent**:
an uncertain network response requires checking GitHub runs before retrying. It does not
automatically dispatch after tagging, preventing accidental duplicate publication requests.
The existing workflow's tag allowlist still applies; accepting a semver tag here does not
imply that the workflow allows publishing that version.

Git tag → existing GitHub Actions → npm Trusted Publisher/OIDC remains the only formal
npm publication path for this setup. ServerFS never invokes npm or adds a PAT/npm token.
Arbitrary Git/workflow commands would permit branch rewrites, ref expansion and credential
misuse, so they are outside this API. ChatGPT should use these deterministic Repo Ops
actions for normal releases instead of submitting server-side AI Agent tasks.

After updating the user-scoped Bridge and MCP artifact, re-render the private configuration
and recreate MCP with all existing overlays/profiles. Verify actions discovery and the
`release_tag` tool schema on the running server. Clients with a cached tool schema must
refresh their connector before using the new parameter; do not substitute Agent delegation.

## Writer lease and process lifecycle

Repo Ops commit, push, and **all named actions** acquire the same exclusive writer lease used by ServerFS filesystem mutations and Agent workspace-write tasks. Named actions take the lease even when `mutates=false`; that flag cannot prove side-effect freedom and must not create a TOCTOU gap between integrity verification and execution.

Repo Ops child execution is bounded by time and output on every supported platform. On POSIX hosts, timeout, output overflow, or cancellation terminates the subprocess group and drains its pipes before the lease is released. On Windows, Repo Ops creates the child suspended, assigns it to a Bridge-owned Job Object, resumes it only after containment succeeds, and terminates/drains the full descendant tree before the writer lease is released. If cleanup cannot be proven, the operation fails closed and the lease is preserved until the Bridge generation is externally restarted.

## Security invariants

Repo Ops keeps the existing ServerFS trust model:

- all caller paths pass MCP-side path/deny policy before Bridge RPC;
- the Bridge independently confines host paths again;
- native Windows rejects reparse points/junctions in the Repo Ops path before repository execution; this matches the native filesystem boundary instead of treating Windows paths as POSIX symlinks;
- hidden/credential-denied paths are filtered from status and cannot be requested for diff/commit;
- child environments are allowlisted instead of inheriting Bridge/provider secrets wholesale;
- Git hooks, askpass overrides, repository SSH-command overrides, external content filters, and commit signing are neutralized on hardened deterministic paths; Windows uses the native `NUL` hooks sink where required by Git-for-Windows;
- Git transport is restricted to approved protocols and custom remote helpers are not caller-selectable;
- errors and results do not reveal absolute host workdir paths;
- Repo Ops code does not import or call Agent provider adapters.

See [Security Model](../security/) and [Architecture](../architecture/) for the adjacent trust boundaries.

## Capability counts in v0.23.2

Stable v0.23.2 keeps the existing 11 / 13 / 21 / 23 filesystem, binary, and Agent combinations and adds four independently gated Repo Ops tools on Linux Host Bridge, validated native Apple Silicon macOS, and validated native Windows 11 x64:

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

## v0.23.0 Linux release acceptance

The Linux bootstrap/internal E2E is **CLOSED-PASS** and the public ChatGPT MCP E2E is **PASS**:

- root suite: 1780 passed, 320 skipped; one upstream Starlette deprecation warning;
- Agent Bridge suite: 497 passed, 15 skipped, no warnings;
- Ruff and format gates: PASS;
- pre-documentation-freeze site build: PASS (37 pages in the executed bootstrap); post-freeze site build/deploy: PASS;
- scratch MCP image build: PASS;
- user-scoped Host Bridge verification: PASS;
- internal MCP Repo Ops surface verification: PASS;
- configured Host Repo Ops action-set verification: PASS;
- public ChatGPT MCP E2E: PASS — `actions → status → wiki_lint → explicit diff → explicit-file commit → current-upstream push`, no Agent task; commit `b3621bc5342e834990c170720de149d846d7de34` pushed to `LLM_WiKi` `origin/main` and final status clean.

The executed Linux/public-MCP evidence is recorded in [`docs/v0.23.0-linux-repo-ops-bootstrap-acceptance-2026-10-26.md`](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.23.0-linux-repo-ops-bootstrap-acceptance-2026-10-26.md). Native Apple Silicon macOS evidence is recorded in [`docs/v0.23.1-macos-repo-ops-acceptance-2026-10-27.md`](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.23.1-macos-repo-ops-acceptance-2026-10-27.md). Native Windows 11 x64 evidence is recorded in [`docs/v0.23.2-windows-repo-ops-acceptance-2026-10-28.md`](https://github.com/NTLx/ServerFS_MCP/blob/main/docs/v0.23.2-windows-repo-ops-acceptance-2026-10-28.md). See the versioned release checklists for final packaging and publication gates.
