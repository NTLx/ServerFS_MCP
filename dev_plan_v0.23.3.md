# ServerFS v0.23.3 Plan — Release-Gate Closure for Native Windows Repo Ops

## Objective

v0.23.3 is the release-hygiene patch that closes the publication defect exposed by v0.23.2 and makes the Windows Repo Ops adaptation a fully gated stable release rather than a release with a post-publication exception.

The product behavior delivered in v0.23.2 remains unchanged: native Windows 11 x64 Deterministic Repo Ops stays independently gated from Agent delegation, current-user/non-elevated, NTFS-local, and fail-closed. v0.23.3 changes the release process so this support cannot be published from a commit that has not passed the complete release matrix.

## Defect being fixed

The v0.23.2 release commit contained two root Ruff E501 violations. The release commit therefore produced failures in Container, macOS Native, and Windows Native CI, while the independent tag-triggered Windows wheel workflow still created the GitHub Release and uploaded release assets. The tag-triggered Container publication was skipped because its test job failed.

This violated the repository's intended release discipline: a stable tag and published release must be downstream of successful validation, not parallel to it.

## v0.23.3 release contract

1. There is one release orchestration entry point: the repository Release workflow started with `workflow_dispatch` on `main` and an explicit `release_tag` input.
2. The workflow validates that the requested tag matches every release version surface and that the dispatched commit is the current `origin/main`.
3. Before a tag exists, the exact release SHA must pass all reusable gates:
   - Linux root/Container + Agent Bridge + MCP conformance;
   - Windows Native;
   - Windows Agent;
   - macOS Native;
   - documentation site build.
4. After the gates finish, the workflow fetches `origin/main` again. If main advanced during validation, release fails closed and no tag is created.
5. Only then may the workflow create the lightweight stable tag at the exact validated SHA. Existing conflicting local/remote tags are never moved or force-updated.
6. Release artifacts are downstream of tag creation:
   - product wheel;
   - Agent Bridge wheel;
   - Windows native abi3 wheel;
   - ServerFS Agent Plugin ZIP;
   - GitHub Release;
   - stable GHCR tags (`X.Y.Z`, `X.Y`, `latest`).
7. Ordinary CI workflows no longer publish stable-tag artifacts independently. Stable publication is owned by the Release workflow.
8. `main` continues to publish only `:edge` through the normal Container workflow.

## Implementation scope

- Make Container, Windows Native, Windows Agent, macOS Native, and Pages workflows reusable through `workflow_call` while preserving their existing push/PR behavior.
- Make Pages deployment side effects explicit so a reusable release-gate call builds the site without deploying it.
- Extract container publication into a reusable workflow used by both `main` edge publication and stable release publication.
- Convert `.github/workflows/wheel-release.yml` from a tag-triggered wheel publisher into the single manual Release orchestrator.
- Add regression coverage for the release workflow contract.
- Bump all release version surfaces to 0.23.3 and update current-state docs/site/release notes without rewriting historical v0.23.0-v0.23.2 evidence.

## Validation before publication

Local/Linux source gate:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run python deployment/plugin/package_plugin.py validate
cd agent_bridge && uv sync --frozen && uv run ruff check . && uv run ruff format --check . && uv run pytest
cd ../site && npm ci && npm run build
```

GitHub release-candidate gate on the final `main` commit:

- Container: green, including root tests, Agent Bridge tests, and MCP conformance.
- Windows Native: green, including Rust/native boundary, Python collection/test set, and native wheel acceptance.
- Windows Agent: green, including three-wheel clean-install acceptance and Bridge/runtime tests.
- macOS Native: green on native arm64 macOS 27.
- Pages build: green.

No `v0.23.3` tag may be created until the above exact-SHA gate is green.

## Publication acceptance

After the Release workflow completes:

- `refs/tags/v0.23.3` points to the exact gated release commit.
- GitHub Release `ServerFS v0.23.3` is published, not draft/prerelease.
- Exactly four expected Release assets are attached and their SHA-256 digests are recorded.
- GHCR `0.23.3`, `0.23`, and `latest` resolve to the same multi-arch OCI index with linux/amd64 and linux/arm64 manifests.
- No failed or in-progress release-gate job remains for the release run.
- Windows production acceptance remains valid: current-user Task Scheduler lifecycle, three 0.23.3 runtime distributions, Repo Ops enabled independently of Agent mode, and public MCP Repo Ops smoke/E2E evidence.

## Non-goals

- No expansion of the Repo Ops public tool surface.
- No generic shell/argv/env/remote/ref/force capability.
- No SCM/LocalSystem or administrator-dependent Windows lifecycle.
- No retagging or rewriting v0.23.2; its publication exception remains historical evidence.
