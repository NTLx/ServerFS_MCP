# ServerFS v0.18.0 Development Plan

Status: released — v0.18.0 release contract complete. The release commit carries the final Agent Skills, portable Plugin packaging, validation gates, version metadata and release-facing documentation before the immutable `v0.18.0` tag is created. Publication follows the repository rule that hosted CI must be green on that exact release commit before tagging. Plan frozen from the 2026-10-20 Decision Grilling session.

## Implementation checkpoint — 2026-10-20

Completed in the VPS `ServerFS` development checkout:

- added repository-native `skills/` with `skills/README.md` plus the seven frozen task-oriented skills: install, configure, upgrade, diagnose, Tunnel, Auth and Agent Bridge;
- every skill implements the shared execution contract: explicit use/non-use boundary, preflight, authoritative-source lookup, platform branches, safety/stop conditions, verification, success criteria and failure handling;
- added a thin root `AGENTS.md` Skills Index so repository Agents load only the smallest relevant skill instead of carrying all operational procedures in base context;
- added root portable `plugin.json` using the Agent Plugins 1.0.0 schema identity (`serverfs` / `ServerFS`), with no `mcp.json`, fixed MCP URL, credentials or parallel `.codex-plugin/plugin.json`;
- added `deployment/plugin/package_plugin.py`, a pure-stdlib validator/packager that enforces the exact seven-skill set, front matter and required workflow sections, repository-relative links, obvious secret-token rejection, Plugin/project/tag version agreement, and a bounded deterministic ZIP layout under one top-level `serverfs/` directory;
- added `tests/test_agent_skills.py`, covering successful validation, fail-closed tag mismatch and repeat-build archive determinism/minimality;
- integrated normal validation into `.github/workflows/container.yml` and tag-time Plugin ZIP build/upload into the existing `.github/workflows/wheel-release.yml` rather than creating a second GitHub Release publisher;
- added README, maintainer-workflow and bilingual site documentation for the Agent Skills/Plugin layer; release-facing current-state copy is now converged to v0.18.0;
- no new runtime/configuration framework or skill-local deployment script was needed: the seven skills can route through existing CLI, doctor, Compose and deployment interfaces without a v0.18 product-interface expansion so far.

Initial implementation gate evidence (recorded before the Agent Writing pass and version convergence; full regression/site gates are not rerun in this convergence task):

- initial Plugin validator — PASS; superseded by the v0.18.0 validation below;
- initial focused `tests/test_agent_skills.py` — **3 passed**; superseded by the **24 passed** quality-pass gate below;
- focused packaging code Ruff lint/format — PASS;
- `uv lock --check` — PASS;
- full root Ruff lint/format — PASS;
- full root pytest — **1590 passed / 316 skipped / 0 failed** (1 warning);
- initial Plugin build — PASS; superseded by the release-candidate artifact below;
- initial archive inspection — PASS for the original manifest + seven-entry layout; the quality pass expanded the packaged runtime with references as recorded below;
- `git diff --check` — PASS;
- site `npm ci` — PASS, with 10 pre-existing moderate dependency advisories;
- site production build — PASS, **35 pages**; existing `host-observability` `env`/`cron` syntax-highlighting fallback warnings remain unrelated to this work; browser visual verification is not required because no layout/CSS was changed;
- bounded unfamiliar-Agent smoke review across eight representative scenarios — **8/8 PASS**, with no blocker/major/minor finding: Linux install, Agent-enabled Linux upgrade preservation, Windows native lifecycle, macOS Login Items approval, API Key/OAuth selection, Tunnel control-plane diagnosis, Linux Agent Bridge/Jev boundaries and unknown-failure routing.

## Agent Writing quality pass and release-candidate convergence — 2026-10-20

Implementation and the Agent Writing quality pass are complete:

- all **7 skills remain model-invoked**;
- the **4 heavy skills** use **13 packaged `references/` files** for progressive disclosure;
- standalone source resolution is fixed: imported skills locate an authoritative checkout or installed release before using checkout-only paths, and packaged Markdown links resolve inside each skill;
- focused `uv run pytest tests/test_agent_skills.py` — **24 passed** after version convergence;
- `uv run python deployment/plugin/package_plugin.py validate --tag v0.18.0` — PASS; this checks a prospective tag string and does not create or prove an existing tag;
- root/Bridge project versions and generated uv locks, Windows Cargo manifest/generated lock, and portable `plugin.json` converge to **0.18.0**; the Windows wheel dynamically inherits Cargo's version, and the isolated private site package keeps its independent version;
- `.env.example`, current-state repository/deployment docs and bilingual site copy converge to v0.18.0; historical milestones remain intact;
- release-candidate Plugin build and inspection — PASS: `serverfs-plugin-v0.18.0.zip`, **21 files total** = manifest + **7 entry `SKILL.md`** + **13 references**; no repository index, configuration, credentials, source tree or extra manifest;
- release-candidate archive SHA-256: `04cee59d8d63320a2909198fd0f8fc4acc8b5b48f2a6c32d37d3dc36482c1f8c`;
- lock consistency, focused version parity, residual current-state scan and `git diff --check` — PASS;
- final root release-candidate gate — `uv sync --frozen`, Ruff lint/format, five Compose render shapes, scratch Docker build and full pytest **1611 passed / 316 skipped / 0 failed**;
- final Agent Bridge gate — frozen sync, Ruff lint/format and full pytest **364 passed / 12 skipped / 0 failed**;
- Windows native release-metadata smoke from Linux — `cargo metadata --locked` PASS with package version **0.18.0**; Windows live behavior remains owned by hosted/native Windows acceptance rather than inferred from Linux;
- final site gate — `npm ci` PASS and production build **35 pages**; the existing 10 moderate npm advisories and four `env`/`cron` syntax-highlighting fallback warnings are unchanged and unrelated to v0.18;
- final pre-commit audit — PASS: Git status is stable across validation; the 19 pre-existing untracked `tmp_tunnel_client_*` files remain untouched; no validation step introduced an unexpected tracked mutation;
- final Plugin artifact audit — `serverfs-plugin-v0.18.0.zip`, **35,172 bytes**, **21 files**, SHA-256 `04cee59d8d63320a2909198fd0f8fc4acc8b5b48f2a6c32d37d3dc36482c1f8c`.

Release publication contract:

- hosted GitHub CI must be green on the exact release commit before the immutable `v0.18.0` tag is created;
- tag workflows publish the normal container/wheel artifacts and the single `serverfs-plugin-v0.18.0.zip` GitHub Release asset;
- no private ChatGPT Plugin is auto-created and no Plugin Directory submission is performed; the ZIP is the platform-distribution boundary for v0.18.0.

## 1. Goal

ServerFS v0.18.0 adds a repository-level `skills/` capability layer so an AI Agent can reliably install, configure, upgrade and diagnose ServerFS without inventing platform-specific procedures or turning documentation into a second configuration source.

The feature is intentionally a **thin operational workflow layer** over the existing product interfaces:

```text
User objective
    |
    v
Agent selects one ServerFS Skill
    |
    v
Skill discovers platform + deployment shape + current state
    |
    v
Existing authoritative interfaces
    +-- serverfs CLI / doctor
    +-- .env.example / serverfs.toml.example
    +-- Compose overlays
    +-- deployment/* helpers
    +-- current product/deployment documentation
    +-- implementation/schema/tests when evidence is needed
    |
    v
Minimal, bounded change
    |
    v
Existing verification surface proves result
```

The new skills are not a new deployment framework, not a new configuration schema and not a replacement for current documentation.

## 2. Release definition

v0.18.0 introduces seven task-oriented Agent Skills:

```text
skills/
  README.md
  serverfs-install/SKILL.md
  serverfs-configure/SKILL.md
  serverfs-upgrade/SKILL.md
  serverfs-diagnose/SKILL.md
  serverfs-tunnel/SKILL.md
  serverfs-auth/SKILL.md
  serverfs-agent-bridge/SKILL.md
```

The same repository also becomes the canonical source for one portable skills-only Agent Plugin package:

```text
plugin.json
skills/
  <the seven skill directories>
```

A stable release tag produces one release asset:

```text
serverfs-plugin-vX.Y.Z.zip
```

The ZIP contains one top-level `serverfs/` plugin directory with the portable `plugin.json` and the seven skill directories required at runtime. It does not contain the source repository, `.env`, `serverfs.toml`, MCP instance URLs, credentials, `mcp.json`, provider-private configuration or a second compatibility manifest.

## 3. Product principles

### 3.1 One source of truth

Skills may describe workflow and decision logic, but must not become an independently maintained copy of facts that already exist in ServerFS.

Authoritative facts remain owned by:

- product CLI and parser behavior;
- `.env.example` and `serverfs.toml.example`;
- Compose files;
- `deployment/*` implementation and platform guides;
- current website/docs;
- executable tests and implementation when documentation is ambiguous.

Skills should point to those sources and read the current checkout before acting.

### 3.2 Instruction-first

Skills default to instructions only. They should call existing ServerFS interfaces instead of introducing skill-local deployment scripts.

If skill implementation exposes a real operability gap — for example, a required step can only be completed by guessing a path, parsing unstable text, or reimplementing configuration rules in shell — v0.18.0 may add the smallest stable product CLI/doctor/config interface needed to close that gap. Such a change must be directly required by a skill and covered by tests. It is not permission for unrelated refactoring.

### 3.3 Current-user operation

Skills preserve the existing ServerFS installation/runtime/maintenance model:

- no root on Linux for ServerFS-specific lifecycle where the product already uses current-user ownership;
- no Administrator/SCM/LocalSystem path on Windows;
- no root LaunchDaemon or unmanaged replacement for the supported macOS lifecycle;
- no silent provider-policy changes.

### 3.4 Surgical configuration changes

Every configuration-changing workflow follows this contract:

1. identify platform and deployment family;
2. identify the current deployment/configuration source;
3. read current state before editing;
4. merge or surgically edit only keys required by the objective;
5. never replace a populated `.env` or `serverfs.toml` wholesale with an example file;
6. never change unrelated authentication, workdir, proxy, runtime or service settings;
7. never echo secrets or move them into tracked/workdir-visible files;
8. validate with an existing parser/doctor/Compose/service status interface;
9. stop on validation failure rather than layering more speculative edits.

### 3.5 Platform boundaries are explicit

The seven skills are organized by user objective, not duplicated per operating system. Each skill branches internally into the supported deployment families:

- Linux Docker Compose;
- Windows native;
- macOS native.

A skill must never generalize Linux shell/Compose assumptions to native Windows/macOS, or infer one platform's live state from another platform.

## 4. Skill contract

Every `SKILL.md` must use Agent Skills front matter with a stable task-oriented name and activation description. The body must explicitly cover the following semantics, although heading wording may vary where clarity improves:

- When to use
- When NOT to use
- Preflight
- Authoritative sources
- Procedure
- Platform branches
- Safety / stop conditions
- Verification
- Success criteria
- Failure handling

Each skill must be usable in two contexts.

### 4.1 Repo-aware context

The Agent is operating in a ServerFS checkout and can inspect the current project files. The skill should prefer live repository evidence and direct official interfaces.

### 4.2 Standalone/imported context

The skill has been loaded independently into ChatGPT/Codex/another compatible Agent. The Agent must first locate a ServerFS checkout or an authoritative deployed installation/configuration source. If it cannot obtain that evidence, it may diagnose or explain but must not invent current configuration values, paths, supported platform details or commands.

## 5. Skill boundaries

### 5.1 `serverfs-install`

Purpose: new installation / first deployment.

Owns:

- choosing the supported platform deployment family;
- prerequisites and source checkout/package acquisition boundary;
- first configuration creation from examples only when no existing operator configuration exists;
- initial current-user service/container start;
- initial health/status verification;
- routing optional Tunnel/Auth/Agent work to their dedicated skills rather than embedding every optional feature.

Does not own:

- upgrades of existing installations;
- OAuth/API-key design decisions beyond identifying them as optional follow-up;
- deep Agent Bridge setup;
- destructive migration from an unknown older installation.

### 5.2 `serverfs-configure`

Purpose: ordinary ServerFS configuration changes.

Owns:

- workdir aliases/paths/descriptions/read-only and related bounded capabilities;
- native TOML vs Linux `.env` routing;
- binary/file-ingress and ordinary non-auth, non-Tunnel, non-Agent policy settings;
- syntax/parser/doctor/Compose validation after modification.

Does not own dedicated Auth, Tunnel or Agent Bridge internals except to route to their skills.

### 5.3 `serverfs-upgrade`

Purpose: update an existing deployment to the requested/current ServerFS release while preserving operator configuration and current-user ownership.

Owns:

- identifying source checkout vs deployed artifact;
- checking for local/uncommitted operator or development changes before source synchronization;
- preserving `.env`, `serverfs.toml`, credential/state directories and provider-private configuration;
- using the supported update/rebuild/bootstrap/service path for the detected deployment family;
- post-upgrade version/status/doctor verification;
- optional subsystem continuity checks for Tunnel and Agent Bridge when they were already enabled.

Does not silently enable new optional features introduced by a release.

### 5.4 `serverfs-diagnose`

Purpose: read-first troubleshooting and status verification.

Owns:

- `serverfs doctor` and structured status surfaces;
- Compose/service/container/platform state inspection;
- configuration validation without mutation;
- determining which subsystem-specific skill should handle a confirmed problem;
- separating observed evidence from hypotheses.

It is the preferred entry point when the user reports failure without an already-known cause.

### 5.5 `serverfs-tunnel`

Purpose: OpenAI Secure MCP Tunnel configuration, lifecycle and diagnostics.

Owns:

- detecting Linux/native Tunnel deployment shape;
- Tunnel bootstrap/configuration using current supported CLI/deployment interfaces;
- keeping control-plane credentials separate from ServerFS HTTP API Key/OAuth;
- proxy routing specific to Tunnel;
- restart/re-establish rules and Tunnel health/ownership checks.

Does not redefine generic HTTP authentication or Agent provider proxy behavior.

### 5.6 `serverfs-auth`

Purpose: remote Streamable HTTP ingress authentication.

Owns:

- choosing between no built-in auth, static API Key and OAuth 2.1 based on explicit operator intent;
- API Key/OAuth mutual exclusion;
- required Compose overlay and external HTTPS/public URL/provider boundary for OAuth;
- safe secret placement and validation;
- preventing native stdio deployments from being incorrectly treated as HTTP-auth deployments.

Does not auto-select or enable an authentication mode without user intent.

### 5.7 `serverfs-agent-bridge`

Purpose: Agent delegation configuration and validation.

Owns:

- Linux Agent overlay/host Bridge/current-user service path;
- native Agent policy in TOML and platform-specific Bridge lifecycle;
- provider runtimes, workdir Agent mode/runtime allowlists and Jev advisory configuration;
- peer identity/IPC validation where required;
- keeping provider credentials/private state out of ServerFS workdirs;
- explicit verification of the two-process boundary.

Does not alter provider authentication/default permission policy merely to make an Agent task pass.

## 6. Repository discovery and documentation

### 6.1 `AGENTS.md`

Add a compact Skills Index only. It should map recognizable operator objectives to `skills/<name>/SKILL.md` and instruct repository Agents to load the smallest matching skill. It must not copy skill procedures into `AGENTS.md`.

### 6.2 `skills/README.md`

Human/Agent entry point containing:

- what ServerFS skills are and are not;
- list of seven skills and selection guidance;
- repo-aware vs imported use;
- how to package/use the skills-only Plugin artifact;
- source-of-truth and safety principles.

It is not an umbrella skill and contains no deploy procedure.

### 6.3 README/site

Current release-facing documentation will gain a short Agent Skills section and link to the repository skill index. English/Simplified-Chinese site copy must remain semantically aligned if a new website page or navigation entry is added.

Release-facing wording is prepared as v0.18.0 current-state copy during this authorized convergence pass. The status/checkpoints above remain the authority for actual publication: hosted CI, tag and GitHub Release are pending.

## 7. Portable Agent Plugin

### 7.1 Manifest

Use the portable Agent Plugins manifest at repository root:

```text
plugin.json
```

Required identity contract:

- `$schema`: `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`;
- `name`: `serverfs`;
- display name: `ServerFS` through `extensions.com.openai.interface`;
- semantic `version`: exactly equal to the ServerFS package version for a releasable commit;
- description: deployment/configuration/upgrade/diagnostic operational workflows;
- repository/homepage/license metadata from the project;
- no `mcp.json`;
- no fixed MCP endpoint;
- no bundled credentials;
- no `.codex-plugin/plugin.json` compatibility manifest in v0.18.0.

Portable packages discover `skills/` automatically; do not duplicate a skills path field that belongs to the legacy/Codex compatibility format.

### 7.2 Release ZIP

Add one small packaging/validation utility under a deployment/release-oriented repository location. It should use the Python standard library where practical and must:

1. read the canonical project version from `pyproject.toml`;
2. load and validate `plugin.json` identity/schema/version;
3. validate the expected seven skill directories and `SKILL.md` front matter;
4. reject unexpected missing required skill contract sections;
5. verify packaged Markdown links resolve inside each skill, including packaged references; checkout-only sources are resolved after discovering an authoritative checkout or installed release;
6. create a deterministic/minimal ZIP containing exactly one top-level `serverfs/` directory, `serverfs/plugin.json`, and the seven runtime skill directories;
7. exclude `skills/README.md`, source code, VCS metadata, local config and other repository files from the release bundle;
8. support a validation-only mode used in normal CI;
9. fail closed on a version/tag mismatch when a release tag is supplied.

Target release filename:

```text
serverfs-plugin-vX.Y.Z.zip
```

The build utility is packaging infrastructure, not a runtime skill helper.

## 8. Automated validation

### 8.1 Static skill/plugin gate

Add focused tests and/or a validator that checks at least:

- exactly the intended seven skill packages are present;
- every skill has a valid `SKILL.md` front matter `name` and non-empty activation `description`;
- skill directory and front matter name match;
- required workflow contract sections are present;
- relative Markdown references resolve;
- no tracked skill text contains obvious secret material or operator-specific credential values;
- plugin manifest uses the portable schema and canonical identity;
- plugin version equals the root package version;
- release ZIP contains only the expected files and top-level directory;
- tag/version gate accepts a matching tag and rejects a mismatch.

The validator should not attempt to reimplement every `.env`/TOML schema value.

### 8.2 CI placement

The ordinary Linux/container CI is the preferred cross-platform-independent static gate because the new skill/manifest/package validation does not require Windows/macOS runtime state.

Path filters in Windows/macOS workflows must still be reviewed: if a skill or shared validation test change can affect platform acceptance expectations, the relevant workflow must not silently skip it. Do not force heavyweight platform CI for pure prose unless the workflow policy intentionally treats Skills as a platform contract.

### 8.3 Agent quality review

Before release, run representative Agent smoke/review tasks for at least:

- new Linux installation;
- existing Linux upgrade preserving `.env` and optional overlays;
- Windows native lifecycle selection;
- macOS native lifecycle selection;
- API Key vs OAuth routing;
- Tunnel vs generic HTTP distinction;
- Agent Bridge enablement with safe provider-secret handling;
- an ambiguous failure that should route through `serverfs-diagnose` and stop instead of guessing.

These reviews verify instruction quality. They do not substitute for platform product E2E where executable code changes occur.

## 9. Release workflow integration

The existing tag-driven wheel workflow already owns GitHub Release attachment behavior. v0.18.0 should extend the established release path rather than create a second independent release publisher.

Preferred design after repository audit:

- build/validate the Plugin ZIP from the same immutable tag commit;
- gate Plugin manifest version against tag version;
- attach the Plugin ZIP to the same GitHub Release as the existing wheel assets;
- preserve user-authored release notes;
- optionally add a bounded generated release-note block for the Plugin asset and SHA-256 only if this can reuse the existing managed-note mechanism without making wheel tooling semantically confusing; otherwise upload the asset without rewriting unrelated notes.

No release workflow may create a ChatGPT private plugin, submit to the Plugin Directory or publish platform-specific marketplace state.

## 10. Versioning

Skills do not have independent semantic versions.

```text
ServerFS release version = Plugin version = Skill content version
```

The release-candidate convergence pass updates current-state documentation and all established version surfaces to 0.18.0 before tagging. The plugin manifest and root package version remain mutually consistent at every CI checkpoint. Prepared release copy does not establish publication; actual hosted CI/tag/Release status is recorded above.

## 11. Implementation phases

### Phase A — architecture + plan

Deliverables:

- this `dev_plan_v0.18.0.md`;
- repository/CI audit identifying minimum release integration points;
- frozen seven-skill boundaries and plugin packaging contract.

Exit criteria:

- no unresolved product-boundary decision blocks implementation.

### Phase B — skills skeleton and canonical content

Deliverables:

- `skills/README.md`;
- seven `SKILL.md` files;
- no skill-local scripts unless a proven capability gap requires them.

Implementation order:

1. diagnose — establishes evidence/stop discipline used by all others;
2. configure — establishes safe mutation contract;
3. install;
4. upgrade;
5. tunnel;
6. auth;
7. agent-bridge.

Exit criteria:

- each skill meets the common contract;
- all commands/path references are checked against current repository sources;
- Linux/Windows/macOS branches are explicit where applicable.

### Phase C — repository discovery

Deliverables:

- thin Skills Index in `AGENTS.md`;
- README entry pointing users/Agents at `skills/README.md`.

Exit criteria:

- repository Agent can locate the correct skill without loading all skill bodies into base instructions.

### Phase D — portable Plugin + packaging

Deliverables:

- root `plugin.json` using Agent Plugins schema 1.0.0;
- packaging/validation utility;
- focused tests for manifest, skills and archive layout.

Exit criteria:

- local validation succeeds;
- generated ZIP contains only the allowed plugin files;
- manifest/package version and optional tag gate are enforced.

### Phase E — CI and release integration

Deliverables:

- static skills/plugin validation in normal CI;
- tag release job builds the same validated Plugin ZIP and uploads it to the existing GitHub Release;
- relevant workflow path filters include the new release/validation surfaces.

Exit criteria:

- CI fails on malformed skill/manifest/version/archive contract;
- tag workflow has one authoritative Plugin asset path and does not create a second Release publisher race.

### Phase F — docs/site

Deliverables:

- concise current-state Agent Skills documentation;
- English/Simplified-Chinese website parity if the site navigation/content surface changes;
- maintainer workflow additions describing skill/plugin gates and release artifact.

Exit criteria:

- no duplicated operational facts that should live only in canonical product docs;
- site build succeeds when site files are changed.

### Phase G — verification and operability audit

Run the narrowest relevant gates, expanding only if implementation reveals product-interface changes.

Minimum planned gates:

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run pytest
python <plugin-package-tool> validate
python <plugin-package-tool> build --output <temp-dir>
git diff --check
```

Also inspect the generated ZIP file list and hash.

If only docs/skills/packaging/tests/workflows change, no live ServerFS deployment is required to claim those files validated. Any runtime CLI/doctor/config implementation change triggers the corresponding root/platform gates from `docs/maintainers/workflow.md`.

Representative Agent smoke/review remains a release gate and should use clean, bounded scenarios rather than mutating production-like operator configurations.

### Phase H — release closeout (only when explicitly authorized)

Before a v0.18.0 tag:

- reconcile root package/locks/native/Agent Bridge version surfaces as required by the existing release contract;
- set `plugin.json` to the same `0.18.0` version;
- verify the prepared v0.18.0 current stable wording on the final release-facing commit;
- run stale-current-version scans while preserving historical `dev_plan_v*.md` evidence;
- execute all affected CI/platform gates;
- create immutable tag and GitHub Release only with explicit user authorization;
- verify the Release contains the existing wheel assets plus exactly one `serverfs-plugin-v0.18.0.zip` asset.

## 12. Expected file changes

Planned new files:

```text
dev_plan_v0.18.0.md
plugin.json
skills/README.md
skills/serverfs-install/SKILL.md
skills/serverfs-configure/SKILL.md
skills/serverfs-upgrade/SKILL.md
skills/serverfs-diagnose/SKILL.md
skills/serverfs-tunnel/SKILL.md
skills/serverfs-auth/SKILL.md
skills/serverfs-agent-bridge/SKILL.md
deployment/plugin/package_plugin.py
tests/test_agent_skills.py
```

The exact packaging utility/test filenames may change once the existing release tooling is inspected, but no parallel general-purpose framework should be introduced.

Expected modifications:

```text
AGENTS.md
README.md
docs/maintainers/workflow.md
.github/workflows/container.yml
.github/workflows/wheel-release.yml
```

Potential modifications only if required by the implementation:

```text
site/src/content/docs/**
site/src/** navigation/config
Windows/macOS workflow path filters
product CLI/doctor/config code + tests (only for a proven skill-blocking operability gap)
```

## 13. Non-goals

v0.18.0 does not add:

- a new ServerFS configuration format;
- `serverfs skills install/list/export` CLI commands;
- automatic copying into `~/.codex/skills`, Claude configuration or other provider-private directories;
- a ServerFS umbrella/router/setup skill;
- seven per-skill Release ZIPs;
- a bundled MCP endpoint or `mcp.json` in the Plugin;
- automatic Plugin Creator/private-plugin creation;
- automatic Plugin Directory submission;
- a legacy `.codex-plugin/plugin.json` maintained in parallel;
- an independent version lifecycle for skills;
- root/admin deployment shortcuts;
- silent opt-in to Tunnel/Auth/Agent capabilities during installation or upgrade.

## 14. Acceptance criteria

v0.18.0 is complete when all of the following are true:

1. A repository-aware Agent can identify the right one of seven skills from `AGENTS.md`/skill metadata without loading an umbrella procedure.
2. An imported skill can detect that it lacks authoritative ServerFS deployment/repository state and stops rather than guessing.
3. Install/configure/upgrade workflows branch correctly between Linux Compose, Windows native and macOS native deployment families.
4. Configuration-changing skills preserve existing operator settings by default and validate every mutation through supported product interfaces.
5. Tunnel, HTTP Auth and Agent Bridge remain separately gated optional capabilities and their credentials/policies are not conflated.
6. Skills contain no second complete copy of `.env.example`, `serverfs.toml.example` or equivalent schema facts.
7. All seven `SKILL.md` files and internal repository links pass the static gate.
8. `plugin.json` is a valid portable Agent Plugins manifest with canonical ServerFS identity and a version matching the project version.
9. One deterministic/minimal `serverfs-plugin-vX.Y.Z.zip` can be produced from the checkout and passes archive-layout validation.
10. The tag release workflow attaches that one Plugin ZIP to the existing GitHub Release without publishing a ChatGPT plugin or marketplace entry.
11. Relevant automated tests/CI gates are green and no unrelated platform or runtime boundary is weakened.
12. Representative Agent smoke/review demonstrates that an Agent unfamiliar with ServerFS can perform or safely stop the seven target classes of work using repository evidence and formal verification surfaces.

## 15. Success definition

The release succeeds when a qualified AI Agent with the necessary repository/machine access can receive one explicit ServerFS operational objective — install, configure, upgrade, diagnose, Tunnel, Auth or Agent Bridge — select the correct workflow, discover the real platform/deployment state, make only the minimal authorized change, and prove the outcome through supported ServerFS interfaces without using root/admin shortcuts, overwriting operator configuration, mixing platform procedures or exposing secrets.
