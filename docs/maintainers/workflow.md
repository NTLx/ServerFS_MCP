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
uv run ruff check .
uv run ruff format --check .
uv run pytest
docker compose config --quiet
docker compose -f compose.yml -f compose.http.yml config --quiet
SERVERFS_IMAGE=serverfs-mcp:dev docker compose build
```

Run all applicable gates when the change crosses that boundary. The generic HTTP Compose render is mandatory for ingress/deployment changes; use `--quiet` when only validity is required so expanded secrets are never printed. The scratch image tag is mandatory: Compose builds to the image name it is given, so building under a published production tag can shadow that release locally.

### Agent Skills / portable Plugin

Repository operational skills live under `skills/` and the portable skills-only Agent Plugin manifest is root `plugin.json`. These files are a workflow/distribution layer over existing product interfaces, not a second configuration schema.

For any `skills/**`, `plugin.json`, Plugin packaging, or Plugin release-workflow change, run:

```bash
uv run python deployment/plugin/package_plugin.py validate
uv run pytest tests/test_agent_skills.py
```

For changes to packaged content or packaging, also build the archive into a disposable directory and inspect its file list:

```bash
uv run python deployment/plugin/package_plugin.py build --output-dir /tmp/serverfs-plugin-check
```

The archive must contain exactly one top-level `serverfs/` Plugin, its `plugin.json`, and the seven runtime Skill directories with Markdown references. Verify tag-matched archive contents; no packaged link escapes its Skill directory. It must not contain `skills/README.md`, local configuration, source/VCS state, `mcp.json`, credentials, or a parallel `.codex-plugin/plugin.json`.

`plugin.json` version must equal the root project version. On a release tag, the packaging command also receives `--tag vX.Y.Z` and fails closed unless tag, project and Plugin versions agree. Agent Skill instructions must keep platform/source-of-truth links current; do not copy the whole `.env`/TOML schema into skill prose merely to satisfy a test.

### Release metadata and Registry

From repository root run `uv run python deployment/native/release_metadata.py --root . --tag vX.Y.Z` (substitute the intended tag), `uv run python deployment/registry/validate.py`, and focused release metadata/version-gate tests. Check both Python locks with `uv lock --check --offline` from their respective roots; verify the native locked Cargo build on Windows. Version-match the Core, Bridge, native wheel, OCI tag and Plugin; site package version is independent. Registry is a loopback-only read-only Docker entry without built-in caller authentication, not the full deployment. Do not add secrets or broaden its binding to satisfy validation.

### Agent Bridge

The root pytest configuration does not collect `agent_bridge/tests/`. For any `agent_bridge/` change, also run from `agent_bridge/`:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

For the two-process MCP E2E harness, install/sync the Bridge environment and invoke the harness from the repository root using the root environment as documented by `agent_bridge/README.md`.

When Linux host-Bridge shell scripts change, additionally run:

```bash
bash -n deployment/agent-bridge/*.sh
```

Platform-native Windows/macOS changes must also run their platform-specific tests/acceptance path; a green Linux root suite is not a substitute.

For Windows lifecycle changes, run native source/wheel tests plus isolated current-user acceptance for the selected named Core, Connector and Bridge owner. Verify least-privilege task registration, signed IPC, Job containment, explicit stop/start, crash recovery and independence of unrelated components. Hosted CI must not mutate persistent operator tasks.

For Windows pre-upgrade acceptance, do not assume the installed Bridge supports checkout-new private RPCs. Only the exact legacy `METHOD_NOT_FOUND` for a newly introduced probe permits narrowly scoped read-only proof from installed durable state; different or ambiguous failure fails closed. PowerShell 5.1 may promote native stderr to terminating `NativeCommandError` under `$ErrorActionPreference = 'Stop'` even when exit succeeds: temporarily use non-terminating handling, capture streams, restore preference, use `$LASTEXITCODE` and explicit output validation. Do not pass multiline/quote-sensitive source via native argv (`python -c $PythonCode`); use stdin or controlled temporary file with ordinary data as separate args.

For macOS lifecycle changes, additionally build and validate the ServiceManagement companion on the supported Mac and verify the wheel carries its build sources:

```bash
bash native/macos/service_manager/build.sh /tmp/serverfs-service-manager-check
uv build --wheel --out-dir /tmp/serverfs-wheel-check
```

Real `SMAppService` registration, Login Items approval and reboot/login recovery are local operator acceptance steps; hosted CI must not mutate a runner's Login Items.

Prior release-specific waivers are historical, not automatic acceptance for named components. Record isolated native registration, identity, restart/recovery and public ingress evidence separately. Do not reboot a production host solely to obtain evidence.

### Deterministic Repo Ops

Repo Ops crosses the root MCP surface, host Bridge service, Linux deployment rendering, Git/process execution, writer-lease coordination and release-facing documentation. Run every affected root + `agent_bridge/` gate, plus Linux Compose/render checks and `bash -n deployment/agent-bridge/*.sh` when deployment scripts/config rendering change.

Before release, prove the applicable Repo Ops gates on supported deployments; Linux public acceptance requires all of the following:

1. at least one workdir can enable Repo Ops while Agent mode remains disabled;
2. the public MCP surface exposes exactly `inspect_repo`, `run_repo_action`, `commit_repo_changes`, and `push_repo` in addition to the applicable existing tools;
3. named actions execute only administrator-fixed argv with mandatory reviewed SHA-256 integrity pins and fail closed on pin/path violations;
4. `inspect_repo` status/diff output respects ServerFS hidden/credential deny policy;
5. commit refuses pre-existing staged state and stages only explicit requested paths;
6. push derives the current upstream and supplies one exact refspec, with no caller-selected remote/ref/force path;
7. timeout/output-overflow/cancellation process-tree cleanup completes before the writer lease is released;
8. public ChatGPT MCP E2E completes `actions -> status -> named validation -> explicit diff/commit -> current-upstream push` as applicable, and evidence shows that no `submit_agent_task` / provider task was created.

Internal Bridge RPC/in-container checks are required implementation evidence but do **not** replace the public MCP E2E release gate. Do not use Agent delegation as a fallback when deterministic Repo Ops is unavailable or fail-closed; report the capability gap instead.

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

A static build proves generated routes/syntax, not rendered geometry. Use Node >=22.22.0 per `site/package.json` and locked dependencies; check bilingual route/link parity, base-path assets, both themes, narrow widths, keyboard and reduced motion. Visual CSS/layout changes require browser evidence. If none was executed report visual rendering as `Not verified`.

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

Release-facing docs must distinguish released product from the current candidate. Before publication, label the target pending; after successful publication, update stable-state copy on main without moving the released tag. Track current-file review, historical classification and check evidence separately.

After a release-state cleanup, scan managed docs/site for stale concepts such as:

- `release candidate` / `not yet tagged`;
- an older version described as `current stable`;
- a platform described as planned after it has shipped;
- a runtime list that omits a now-supported provider;
- a historical platform limitation presented as a current product limitation.

Exclude generated/cache/vendor trees and historical development memory such as `.git`, `.workbuddy`, `node_modules` and site build output. Classify every hit: current-state defect vs legitimate historical evidence.

Do not make a search result disappear by falsifying history.

## 6. Release workflow

Use Semantic Versioning and the repository's established version surfaces. Before publication, reconcile current version/status claims across the release-facing README/docs/site, package metadata/version gates, deployment examples and release notes/checklists that are intended to describe the upcoming/current release. For a release that introduces a new public MCP capability such as Repo Ops, public client E2E acceptance remains a release gate; internal/private RPC evidence alone is insufficient.

### Stable publication contract

Stable publication is **dispatch-first, not tag-first**. Do not manually push a stable tag and then wait to see whether CI passes.

1. Push the intended release commit to `main` and wait for its ordinary CI to complete.
2. Start `.github/workflows/wheel-release.yml` (`Release`) with `workflow_dispatch` on `main`, supplying the exact `vMAJOR.MINOR.PATCH` release tag.
3. The workflow validates that the requested tag matches all release version surfaces and that the dispatched SHA is still the current remote `main`.
4. The exact SHA must then pass the reusable Linux/Container, Windows Native, Windows Agent, macOS Native, and Pages build gates.
5. After those gates complete, the workflow fetches `origin/main` again. If `main` advanced, it fails closed and creates no tag.
6. Only the fully gated SHA may receive the lightweight stable tag. Existing conflicting tags are never moved or force-updated.
7. Wheels, Agent Plugin, GitHub Release, and stable GHCR tags are downstream of that tag creation. A failed publication step may be retried against the same immutable tag, but a failed validation gate cannot create the tag in the first place.

Publication channels:

- `main` publishes only `:edge` through the ordinary Container workflow;
- a successful stable Release workflow publishes `X.Y.Z`, `X.Y` and `latest` to GHCR;
- the same release run builds the product wheel, Agent Bridge wheel, Windows native abi3 wheel, and `serverfs-plugin-vX.Y.Z.zip`, then attaches the four assets to the GitHub Release;
- production deployments pin exact versions; `edge`/`latest` are not production pins.

A published stable tag is immutable. Post-release documentation/site/metadata corrections land on `main`; never move/recreate the stable tag to absorb them. The repository `skills/` tree remains canonical; do not maintain seven independent skill ZIPs, publish a private ChatGPT Plugin, submit to the Plugin Directory, or create a second stable-release publisher.

Use supported GitHub CLI/API operations (`gh`) for GitHub actions when requested. If a required operation has no supported CLI/API path, report that exception rather than inventing an endpoint.

Do **not** commit, push, create/move tags or publish a release unless the user explicitly asks for that Git operation. Documentation cleanup alone does not imply Git publication authority.

## 7. Deployment discipline

- Build is not deploy; verify the running artifact separately when deployment is requested.
- Linux named recreation must retain the exact project, env, exposure/auth/file-ingress/Bridge and Connector overlays/profiles. Operate only selected services with documented `--no-deps --no-build`; no generic project restart/down.
- After Core recreation establish fresh authenticated MCP; operate affected ingress separately only when required. No old SSE replay guarantee. Stop ingress before Core where native guards require it; shared Core never stops its owner.
- Linux host Bridge uses user-scoped named systemd and explicit release stage/select; rollback selects an executable label, not a database/config rollback. Native named staging/helpers are create-only: preserve attempt markers and private state; unsupported updates require explicit migration planning.
- Windows development/deployment work belongs on the Windows-native workflow; macOS work belongs on the macOS-native workflow. Do not use one host to infer another host's live state.
- Windows uses the named component CLI documented in `deployment/windows/v0250-native-operations.md`; never replace current-user Task Scheduler with SCM/LocalSystem.
- macOS uses named SMAppService lifecycle documented in `deployment/macos/README.md`; respect Login Items approval, do not mutate raw launchd state.
- Never use a persistent or `KeepAlive` `launchctl submit` job as a one-shot delayed maintenance handoff. Temporary macOS maintenance actions must be bounded, and completion evidence must include verification that the temporary launchd job/artifact no longer exists. When ServerFS reports a restart hand-over was scheduled, reconnect and verify the native `service status` hand-over result rather than treating scheduling as completion.

## 8. Reporting completion

Report only evidence actually obtained. If deterministic Repo Ops actions or a direct executor are unavailable, record unexecuted gates rather than silently substituting Agent execution or reporting historical evidence as new:

- files changed and why;
- tests/build/checks actually run, with pass/fail status and relevant counts where available;
- whether the change was source-only, built, deployed and/or live-E2E verified;
- residual limitations, skipped checks and unrelated/pre-existing dirty state.

Do not say `should pass` for an unexecuted check. Use `Not verified`.

End-to-end verification through ChatGPT belongs to the maintainer/orchestrator layer; a delegated local Agent cannot claim it merely because its local MCP surface passed.

## 9. Language

Repository code, comments, docstrings, tests, commits, pull requests and maintainer instruction files are English unless the artifact is explicitly a localized surface. Communicate with the repository maintainer in Chinese.
