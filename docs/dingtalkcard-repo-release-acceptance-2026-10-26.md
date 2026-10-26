# DingtalkCard deterministic release acceptance — 2026-10-26

## Authorized scope and outcome

Configure deterministic ServerFS Repo Ops release actions for `DingtalkCard` /
`NTLx/n8n-nodes-dingtalk-card`, then create/push `v1.1.0-beta.3` and dispatch its
existing `Publish to npm` workflow. No target business/workflow files were edited.
No n8n service, database, container or configuration was changed.

The public ChatGPT MCP tools executed both actions directly. No server-side AI
Agent task was submitted. No task was created after the pre-deployment snapshot;
the retained count decreased from 717 to 693 during lifecycle retention cleanup.

## Implementation and operator configuration

- Preserve the four-tool Repo Ops surface. Extend `run_repo_action` with the optional
  structured `release_tag`; reject unknown fields at the MCP and private RPC boundaries.
- Add opt-in built-in policy kinds `release_tag` and `publish_npm`. They cannot carry
  command argv; the administrator fixes the repository and optional absolute `github_cli`.
- Keep command actions backward compatible and retain mandatory integrity pins,
  bounded subprocess-group cleanup, and the existing exclusive writer lease.
- Add `repo_release.py`, policy/config/renderer changes, public/RPC/service wiring,
  disposable Git integration tests and protocol/configuration tests.
- Update `.env.example`, the maintainer invariant and both language versions of the
  Repo Ops guide. Git/workflow command escape hatches remain outside this API.
- The private `.env` changes only `WORKDIR_06_REPO_ACTIONS_JSON` and `SERVERFS_IMAGE`.
  The latter now retains the tested `serverfs-mcp:repo-release-dev` local development
  artifact, avoiding a later Compose recreation reverting MCP to the old stable image.
- Both actions pin reviewed `package.json`, `package-lock.json`,
  `.github/workflows/publish.yml`, and `scripts/verify-release-ref.cjs`. Pins were
  calculated from unchanged target files; future changes require separate operator review.

## Final public actions discovery

`inspect_repo(workdir="DingtalkCard", view="actions")` returned:

| name | kind | argument | mutates | repository | head_only | force | workflow |
| --- | --- | --- | --- | --- | --- | --- | --- |
| publish_npm | publish_npm | release_tag | true | NTLx/n8n-nodes-dingtalk-card | true | false | publish.yml |
| tag_release | release_tag | release_tag | true | NTLx/n8n-nodes-dingtalk-card | true | false | null |

Each entry also exposes `tag_pattern` (full-match semantics, 128-character maximum):

```text
v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-beta\.(?:0|[1-9][0-9]*))?
```

Scope: clean HEAD on `main` tracking `origin/main`, matching HEAD package version,
actual remote main equal to HEAD, one approved GitHub fetch/push URL each, exact
HEAD tag only, conflict rejection. Tag creation is lightweight compare-and-create;
no existing ref is replaced. Remote/local annotated tags at the same commit are
accepted. Dispatch is fixed to `publish.yml`, has no arbitrary inputs and is not
idempotent. Unknown outcome must be checked on GitHub before retrying dispatch.

## Executed validation

- Root suite: **1793 passed, 320 skipped**; one upstream Starlette deprecation warning.
- Bridge suite: **535 passed, 15 skipped**, no warnings.
- Root and Bridge Ruff lint and format: **PASS**.
- Frozen dependency synchronization: **PASS**.
- Existing-overlay/file-ingress and generic HTTP Compose quiet renders: **PASS**.
- Isolated development MCP image build: **PASS**.
- English/Simplified Chinese site build: **PASS**, 39 pages; no visual layout change.
- `git diff --check`: **PASS**.
- Host user-scoped Bridge/IPC verification and existing Claude/Codex/Qoder readiness:
  **PASS**; readiness did not submit a provider task.
- Public MCP rejected non-semver `1.1.0`, `../../xxx`, `main`, `refs/heads/main`,
  whitespace/option and shell-bearing tags. Public extra `force`, `commit`, and
  `workflow` fields were rejected before execution.
- Disposable local Git remote tests covered exact single-tag push, local/remote
  different-commit conflicts, same-HEAD annotated-tag idempotency, dirty/staged/
  untracked files, ahead HEAD, wrong branch/version/remote, integrity mismatch,
  fixed workflow dispatch and administrator-only CLI selection. No destructive
  remote safety test was performed against the real repository.

## Live release evidence

Before mutation, local HEAD, tracking `origin/main`, actual remote `main`, and
successful CI run `40275078480` all matched:

```text
3038e3c55de2f3efb0d3ac8eda6917b189c620aa
```

The working tree/index was clean and the target remote tag was absent.

Public `run_repo_action(action="tag_release", release_tag="v1.1.0-beta.3")` returned
`pushed=true`, that commit SHA and `already_present=false`. A repeat returned
`already_present=true`, proving live idempotency. Independent `git ls-remote`
confirmed `refs/tags/v1.1.0-beta.3` at the exact SHA above, with remote `main`
unchanged. The target worktree remained clean.

The first publish attempt failed before process creation with `REPO_TOOL_UNAVAILABLE`:
the user-scoped service PATH did not include the existing GitHub CLI installation.
No dispatch occurred. An administrator-only absolute `github_cli` path fixed this
without changing provider PATH or authentication. Full tests were rerun after the
fix; the Bridge was updated again through the existing installer.

The subsequent public `publish_npm` action returned `dispatched=true` for `publish.yml`
and the exact tag/SHA. GitHub reported:

- [Publish to npm run 40277071611](https://github.com/NTLx/n8n-nodes-dingtalk-card/actions/runs/40277071611)
- event: `workflow_dispatch`
- ref: `v1.1.0-beta.3`
- HEAD: `3038e3c55de2f3efb0d3ac8eda6917b189c620aa`
- observed state: **in_progress** at acceptance, created `2026-10-26T05:57:39Z`

Acceptance stops at successful workflow start. npm publication outcome was deliberately
not evaluated; the maintainer/ChatGPT will verify it independently. The existing Git tag
→ GitHub Actions → Trusted Publisher/OIDC → npm path was preserved, without new secrets.
The existing workflow tag allowlist still governs which future versions it publishes.

## Applied lifecycle

The supported current-user Bridge installer updated/restarted only the user Bridge
(two activations, including the CLI path correction). MCP alone was recreated from
the tested development artifact with `compose.yml`, `compose.agent.yml`, and the
existing file-ingress profile retained. Tunnel/file-ingress containers remained healthy;
n8n containers retained their original IDs and uptimes. ServerFS source changes were
left uncommitted; no ServerFS Git commit/tag/push or release publication was performed.

No capability blocker remains for the configured current release. Clients that cache
tool schemas must refresh discovery to see `release_tag`; the actual ChatGPT public
MCP calls in this acceptance successfully carried the parameter.
