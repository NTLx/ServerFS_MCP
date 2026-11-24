---
title: Deterministic Repo Ops
description: Deterministic repository operations through an explicitly authorized host Bridge, without Agent delegation.
---

Deterministic Repo Ops is an optional non-Agent capability providing bounded repository operations through an explicitly authorized host Bridge, never a shell or Agent fallback.

Repo Ops is enabled per workdir and must be admitted by the selected Bridge attachment. Named shared Core grants are independent; macOS owned uses private native authorization. Named macOS shared Core does not support Repo Ops.

## Product boundary

The host Bridge owns repository paths, Git credentials and writer leases. Repo Ops does not select a model, create a provider task or fall back to `submit_agent_task`. It accepts neither a shell command nor caller-selected executable, argv, environment, cwd, remote, ref or force flag. Agent mode may remain disabled.

## Public MCP tools

Repo Ops adds exactly four tools when at least one workdir explicitly enables it:

| Tool | Purpose | Mutation |
| --- | --- | --- |
| `inspect_repo` | Inspect bounded Git status/diff evidence or list configured named actions | No |
| `run_repo_action` | Run one administrator-defined fixed repository action | Serialized writer operation |
| `commit_repo_changes` | Commit only explicitly requested paths with a fixed hardened Git path | Yes |
| `push_repo` | Push the current branch to its configured upstream with one exact refspec | Yes |

Start with `inspect_repo(workdir="project", view="status")` and `inspect_repo(workdir="project", view="actions")`, substituting your authorized alias. Neither accepts paths; `diff` and `staged_diff` require 1–64 unique, policy-valid relative paths. If no action is configured, do not invent one or fall back to Agent execution.

`commit_repo_changes` accepts 1–64 unique, explicit file paths (not directories) and a single-line message. It refuses pre-existing staged state, rejects external Git content filters, disables hooks and commit signing, and returns the commit SHA. Check status after commit; it never pushes automatically.

`push_repo` derives the current branch/upstream inside the host Bridge and pushes exactly `HEAD:<upstream-branch>` to the derived remote. The caller cannot choose a remote/ref or request force semantics. Git is non-interactive and provider/API secrets are not forwarded into the child process environment.

## Per-workdir configuration

Repo Ops defaults off. Linux named deployment enables the Core slot and the matching private owner JSON `workdirs` entry (`repo_ops_enabled: true`); the signed grant must allow the required `repo.*` methods. Follow the [Linux runbook](https://github.com/NTLx/ServerFS_MCP/blob/main/deployment/linux/README.md). The v0.25.0 signed overlay binds slot 01 only; the v0.26.0 candidate accepts 1–16 explicitly granted slots, including Repo Ops-only slots with Agent disabled. The Core-side settings for a writable repository are:

```text
WORKDIR_01_ALIAS=project
WORKDIR_01_PATH=/srv/project
WORKDIR_01_READ_ONLY=false
WORKDIR_01_REPO_OPS_ENABLED=true
```

Define named actions under the authorized owner workdir's private JSON `repo_actions`. Only the legacy Linux singleton renderer uses `WORKDIR_XX_REPO_ACTIONS_JSON`; it does not forward that policy to the Core container. Native TOML uses `repo_ops_enabled = true` and per-workdir `repo_actions` tables; follow the applicable [macOS](../macos-native/) or [Windows](../windows-native/) runbook to provision the matching owner policy/attachment.

Inspection works on an authorized read-only repository without named actions. Commit, push and every named action require writable policy and the writer lease. No Agent provider is required.

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

Replace each placeholder with a reviewed lowercase 64-character SHA-256 digest. The policy permits 1–32 argv entries (each at most 4096 UTF-8 bytes) and 1–32 integrity pins. The example is a policy illustration, not a ready-to-run action.

Integrity pinning is mandatory because a fixed argv that executes mutable repository code would otherwise become an indirect arbitrary-code execution primitive. Before process creation the Bridge independently verifies that every pinned input is confined, regular, within size limits, and digest-identical. A mismatch fails closed.

Do not automatically update integrity pins in the same task that changed the pinned repository code. Treat a pin update as an operator review decision.

## Release tags and fixed npm workflow dispatch

For an npm/`package.json` release workdir, the operator may configure two built-in action kinds. Use the private owner policy described above. These use the same tools, integrity checks and writer lease; the repository is fixed by the operator. The caller supplies only an action name and `release_tag` (no argv). The following Linux example is the narrow npm release contract, not a generic release interface or proof of acceptance on other platforms.

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

Discover profiles with `inspect_repo(workdir="project", view="actions")`. Only when tag creation and workflow dispatch are separately authorized, call the configured actions:

```text
run_repo_action(workdir="project", action="tag_release", release_tag="v1.1.0-beta.3")
run_repo_action(workdir="project", action="publish_npm", release_tag="v1.1.0-beta.3")
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

After an authorized upgrade or policy change, follow the exact platform lifecycle and grant provisioning procedure. Named owner provisioning is not a live policy update mechanism. Verify the selected owner, Core, action discovery and current `release_tag` schema; refresh the client tool schema if stale. Never recreate unrelated components or substitute Agent tasks.

## Writer lease and process lifecycle

Repo Ops commit, push, and **all named actions** acquire the same exclusive writer lease used by ServerFS filesystem mutations and Agent workspace-write tasks. Named actions take the lease even when `mutates=false`; that flag cannot prove side-effect freedom and must not create a TOCTOU gap between integrity verification and execution.

Repo Ops child execution is bounded by time and output on every supported platform. On POSIX hosts, timeout, output overflow, or cancellation terminates the subprocess group and drains its pipes before the lease is released. On Windows, Repo Ops creates the child suspended, assigns it to a Bridge-owned Job Object, resumes it only after containment succeeds, and terminates/drains the full descendant tree before the writer lease is released. If cleanup cannot be proven, the operation fails closed and retains the lease in that Bridge generation. Reconcile the actual process state and follow the platform recovery procedure; never restart blindly merely to release a lease.

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

## Capability counts

Repo Ops adds four independently gated tools:

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

Acceptance must be verified for the target release and selected independent platform components.
