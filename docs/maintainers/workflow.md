# Maintainer Workflow

Use the smallest workflow that fully validates the boundary you changed. Executed evidence matters; an unrun check is `Not verified`.

## 1. Before editing

1. State the concrete problem or requested outcome.
2. Inspect the current implementation, tests, configuration and relevant docs before changing them.
3. Separate current-state claims from historical records. Do not mechanically replace old version numbers inside release histories, plans or acceptance evidence.
4. Identify the narrowest affected boundary: filesystem/MCP, Agent Bridge, deployment/platform, website/docs, release metadata or Git-only work.
5. Keep unrelated cleanup out of the change.

For delegated Agent work, specify one atomic objective, allowed mutation scope, stop conditions and required evidence. A verification-only task does not gain permission to edit when it finds a failure.

Never disguise, fragment or relocate instructions to evade a provider safety check. Narrow legitimate work at a real capability boundary instead.

## 2. During the change

- Make every changed line trace to the requested outcome.
- Match existing architecture and naming before introducing a new abstraction.
- Preserve public schemas and behavior unless the requested change explicitly alters them.
- Runtime/filesystem/security behavior fixes should add a regression test at the public surface where the defect was observable when practical.
- Do not claim support for a platform/provider/version from source inspection alone when the project requires measured acceptance evidence.

## 3. Validation gates

### Root runtime / filesystem / security / base deployment

Use the root Development gate documented in `README.md`:

```bash
uv sync --frozen
ruff check
ruff format --check
pytest
docker compose config
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
```

Run all six when the change crosses that boundary. The scratch image tag is mandatory: Compose builds to the image name it is given, so building under a published production tag can shadow that release locally.

### Agent Bridge

The root pytest configuration does not collect `agent_bridge/tests/`. For any `agent_bridge/` change, also run from `agent_bridge/`:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

For the two-process MCP E2E harness, install/sync the Bridge environment and invoke the harness from the repository root using the root environment as documented by `agent_bridge/README.md`.

When Linux Phase E shell scripts change, additionally run:

```bash
bash -n deployment/agent-bridge/*.sh
```

Platform-native Windows/macOS changes must also run their platform-specific tests/acceptance path; a green Linux root suite is not a substitute.

### Website / docs-only changes

`site/` is an Astro + Starlight static GitHub Pages site. For site changes use:

```bash
cd site
npm ci
npm run build
```

Then run from the repository root:

```bash
git diff --check
```

If dependencies are already present and the task explicitly forbids installation/update, use the existing declared build script without mutating dependencies or lockfiles; otherwise report the missing prerequisite instead of silently changing it.

A static build proves routes/syntax, not rendered geometry. Visual CSS/layout changes require browser evidence. If no browser check was executed, report visual rendering as `Not verified`.

### Cross-boundary changes

Run every applicable gate. Do not run unrelated heavyweight gates merely because they exist, but do not let one green layer substitute for another affected layer.

## 4. Website / GitHub Pages rules

- Keep `site/` a pure static Pages artifact: no SSR, database, serverless runtime or required third-party runtime CDN.
- Project base remains `/ServerFS_MCP/`.
- Custom Astro page URLs use `import.meta.env.BASE_URL`; Starlight-owned base-relative assets remain in the form expected by Starlight. Do not double-prefix the project base.
- English is the root locale; Simplified Chinese lives under `/zh-cn/`.
- The landing page remains one shared `HomePage.astro` with locale-specific copy rather than duplicated layouts.
- Translated docs mirror English slugs under `src/content/docs/zh-cn/`; keep i18n loader/schema enabled.
- Simplified-Chinese font delivery remains deterministic and self-hosted where already configured.
- Let Starlight own semantic light/dark palette foundations. Scope brand overrides by theme rather than globally replacing base gray/black/white variables.
- Prefer content-driven Grid/Flex for semantic diagrams over fixed coordinates that break under localization/responsive widths.

When current platform/release copy changes, update English and Chinese together and scan the built/source site for stale current-state wording. Historical timeline/version landmarks may remain unchanged when they accurately describe the older release.

## 5. Documentation status hygiene

Release-facing docs must describe the released product, not the development state that preceded it.

After a release-state cleanup, scan managed docs/site for stale concepts such as:

- `release candidate` / `not yet tagged`;
- an older version described as `current stable`;
- a platform described as planned after it has shipped;
- a runtime list that omits a now-supported provider;
- a historical platform limitation presented as a current product limitation.

Exclude generated/cache/vendor trees and historical development memory such as `.git`, `.workbuddy`, `node_modules` and site build output. Classify every hit: current-state defect vs legitimate historical evidence.

Do not make a search result disappear by falsifying history.

## 6. Release workflow

Use Semantic Versioning and the repository's established version surfaces. Before tagging, reconcile current version/status claims across the release-facing README/docs/site, package metadata/version gates, deployment examples and release notes/checklists that are intended to describe the upcoming/current release.

Git tags drive published images:

- `main` publishes `:edge`;
- stable `vX.Y.Z` tags publish `X.Y.Z`, `X.Y` and `latest`;
- `latest` comes from a stable tag, not from a GitHub Release event;
- production deployments pin exact versions; `edge`/`latest` are not production pins.

A published stable tag is immutable. Post-release documentation/site/metadata corrections land on `main`; never move/recreate the stable tag to absorb them.

Use supported GitHub CLI/API operations (`gh`) for GitHub actions when requested. If a required operation has no supported CLI/API path, report that exception rather than inventing an endpoint.

Do **not** commit, push, create/move tags or publish a release unless the user explicitly asks for that Git operation. Documentation cleanup alone does not imply Git publication authority.

## 7. Deployment discipline

- Build is not deploy; verify the running artifact separately when deployment is requested.
- Linux Agent-enabled recreation must include the Agent overlay, not only base `compose.yml`.
- Restart/re-establish the Tunnel when the documented deployment procedure requires it after ServerFS recreation; readiness alone may not prove the active MCP session points at the new container.
- Use user-scoped service lifecycle for the established Linux Agent deployment; do not introduce root/system-wide service requirements as a shortcut.
- Windows development/deployment work belongs on the Windows-native workflow; macOS work belongs on the macOS-native workflow. Do not use one host to infer another host's live state.

## 8. Reporting completion

Report only evidence actually obtained:

- files changed and why;
- tests/build/checks actually run, with pass/fail status and relevant counts where available;
- whether the change was source-only, built, deployed and/or live-E2E verified;
- residual limitations, skipped checks and unrelated/pre-existing dirty state.

Do not say `should pass` for an unexecuted check. Use `Not verified`.

End-to-end verification through ChatGPT belongs to the maintainer/orchestrator layer; a delegated local Agent cannot claim it merely because its local MCP surface passed.

## 9. Language

Repository code, comments, docstrings, tests, commits, pull requests and maintainer instruction files are English unless the artifact is explicitly a localized surface. Communicate with the repository maintainer in Chinese.
