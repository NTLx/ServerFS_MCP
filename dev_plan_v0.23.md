# ServerFS v0.23 Development Plan — Deterministic Repo Ops

Status: **v0.23.0 PRE-TAG RELEASE CANDIDATE PASS.** Implementation, Linux bootstrap/internal E2E, public ChatGPT MCP E2E, refreshed full root/Agent Bridge validation, MCP conformance, Windows/macOS regression, and post-freeze site build/deploy are PASS. Release-facing version/documentation closeout is complete and is being revalidated on the final release commit before the immutable tag is created. Executed acceptance evidence is recorded in [`docs/v0.23.0-linux-repo-ops-bootstrap-acceptance-2026-10-26.md`](docs/v0.23.0-linux-repo-ops-bootstrap-acceptance-2026-10-26.md) and the [`v0.23.0 release checklist`](docs/v0.23.0-release-checklist-2026-10-26.md).

## 1. Problem

ChatGPT can already read and revision-guardedly mutate explicitly configured ServerFS workdirs, but a repository workflow can still stop at deterministic completion gates such as repository validation, Git inspection, commit, and push. Today the only host-side execution plane is the optional Agent Bridge. Using `submit_agent_task` merely to run these mechanical steps would add an unnecessary AI Agent hop and violate the desired architecture: reasoning stays in ChatGPT; deterministic host actions stay deterministic.

The requested outcome is a narrow non-Agent repository operations surface that lets ChatGPT finish repository-local work without gaining a generic shell.

## 2. Product boundary

v0.23 adds an optional **Repo Ops** capability implemented through the existing host-side Bridge transport, but with independent RPC methods that never select a model, create an Agent task, call an adapter, or enter provider runtime code.

The host Bridge is reused only because it already owns:

- authenticated local IPC;
- host workdir paths;
- writer-lease coordination with ServerFS mutations and Agent tasks;
- the host user's Git/network credential context.

Repo Ops is therefore a deterministic host capability, not Agent delegation.

### Non-goals

- no generic shell, argv, env, cwd, executable, remote, ref, or force parameters from an MCP caller;
- no generic caller-selectable pull/fetch/checkout/reset/clean/rebase/merge/tag/release operations. The later accepted v0.23.0 release-action extension is deliberately narrower: an operator must configure the built-in `release_tag` or `publish_npm` action kind, and the caller may supply only a validated `release_tag`; repository, HEAD semantics and fixed workflow remain operator/product controlled. A fixed internal unstage rollback is allowed only to restore commit staging after a failed Repo Ops commit attempt;
- no automatic Agent fallback;
- no provider runtime/model/session/task creation;
- no credential transport through MCP or Bridge RPC payloads;
- no repository-controlled executable policy file;
- no execution of mutable repository-controlled code/config unless every relevant repository input is pinned by operator-owned SHA-256 integrity metadata. Host executables, the installed runtime environment, credential helpers and user-level Git/SSH configuration remain operator-owned trust boundaries and are never request-controlled.

## 3. Public MCP surface

Register Repo Ops tools only when at least one workdir explicitly enables Repo Ops.

### `inspect_repo(workdir, view, paths=[])`

Read-only. `view` is one of:

- `status` — bounded Git status/current HEAD/upstream evidence;
- `diff` — bounded unstaged diff;
- `staged_diff` — bounded staged diff;
- `actions` — names and metadata for administrator-configured named actions.

Path arguments, when accepted, are validated through the same ServerFS workdir path policy as filesystem tools.

### `run_repo_action(workdir, action, path=None, release_tag=None)`

Runs one administrator-configured named action. Command actions accept only the action name and, when their profile explicitly declares a path argument, one workdir-relative path. The executable and base argv are fixed in host-side configuration and no shell is used. The later v0.23.0 built-in release-action extension may instead accept one validated `release_tag` for an operator-configured `release_tag` or `publish_npm` action; `path` and `release_tag` are mutually exclusive, and repository/workflow/HEAD semantics remain fixed rather than caller-controlled.

A command action profile contains:

- `kind`: `command` (default);
- `argv`: fixed non-empty argv array;
- `mutates`: boolean;
- `argument`: `none`, `path`, or `basename`; `basename` still requires the caller to submit a full policy-valid workdir path, but only its final filename is appended to the fixed argv;
- `integrity`: non-empty map of workdir-relative files to lowercase SHA-256 digests.

The accepted release-action extension adds `kind=release_tag` and `kind=publish_npm`. These built-ins do not accept command argv. Operator policy fixes the GitHub repository and, for npm dispatch, the fixed `publish.yml` workflow plus an optional administrator-only absolute GitHub CLI path. The public caller supplies only a full-match `vMAJOR.MINOR.PATCH` or `vMAJOR.MINOR.PATCH-beta.N` release tag. Both actions require a clean `main` tracking `origin/main`, matching package version, remote `main == HEAD`, reviewed integrity pins, approved GitHub fetch/push URLs and the same exclusive writer lease. Tag creation is exact-HEAD compare-and-create with conflict rejection; npm publication remains Git tag → fixed GitHub Actions → Trusted Publisher/OIDC and never adds an npm token to ServerFS.

Before any named action runs, the host Bridge independently validates every integrity input as a confined regular file and verifies its digest. This is mandatory because a fixed argv such as `python tools/wiki-lint.py` is not safe if the caller can first modify `tools/wiki-lint.py`; without integrity pinning it would be an indirect arbitrary-code execution primitive. Integrity mismatch fails closed before process creation.

All named actions require a read-write workdir and take the exclusive writer lease, even when an action is declared `mutates=false`. `mutates` is descriptive/audit metadata, not an enforcement boundary: executing repository code cannot be proven side-effect-free at the OS level. Serializing every named action also closes the integrity-check/exec TOCTOU window against concurrent ServerFS or Agent writers.

### `commit_repo_changes(workdir, paths, message)`

Mutating, explicit, bounded Git commit operation.

- validates every requested path through ServerFS path policy;
- refuses pre-existing staged changes;
- stages only the requested paths;
- refuses an empty staged result;
- commits with the repository/user's existing Git configuration;
- returns the resulting commit SHA;
- does not push.

No caller-controlled Git options are accepted.

### `push_repo(workdir)`

Mutating/open-world. Resolves the current branch's configured upstream inside the Bridge, validates that it is a normal remote branch, and pushes exactly `HEAD:<upstream-branch>` to that derived remote.

- no caller-selected remote/ref; repository `push.default` and `remote.*.push` cannot widen the ref set because Repo Ops always supplies one exact refspec;
- no force flag;
- no credential payload;
- non-interactive (`GIT_TERMINAL_PROMPT=0`);
- child environment is allowlisted rather than inheriting Bridge/provider secrets wholesale;
- hooks are disabled and SSH/protocol behavior is constrained by fixed Git config overrides plus `GIT_ALLOW_PROTOCOL=https:ssh:file`; arbitrary/custom remote-helper protocols remain denied even if repository/user Git config attempts to allow them;
- exclusive writer lease;
- bounded timeout/output; on Linux, timeout/output overflow kills the whole Repo Ops subprocess group before the lease is released.

## 4. Configuration

Repo Ops is opt-in per workdir and defaults off.

Linux/Compose environment:

```dotenv
WORKDIR_03_REPO_OPS_ENABLED=true
WORKDIR_03_REPO_ACTIONS_JSON={"wiki_lint":{"argv":["uv","run","--frozen","--no-sync","python","-I","tools/wiki-lint.py"],"mutates":false,"argument":"none","integrity":{"tools/wiki-lint.py":"059c75aed32ea6af02def2554473c143368e2d8ab428bb3a11f1a5490b2ff9be","tools/compile_registry.py":"936f0da02936acb8c541a9c5a836dcaf38846e66db2d2641308fe25a16df8c96","pyproject.toml":"d000277a4df531e25f4247ac10c8c979cfb31bd3b9d6b20c1f67c73fd755ac21","uv.lock":"ce8a1112e37b3dd5f47f6ee986f485a52126c6c23e05dd5a52383af6d24deee8",".python-version":"49a506dd32096b010d75205acf3430c9ae6c40351888129499e5a5e487126c93"}}}
```

`WORKDIR_XX_REPO_ACTIONS_JSON` is operator-owned configuration, never read from the repository. It is parsed and validated by the Linux Bridge config renderer and persisted only in the private Bridge policy document.

Initial v0.23 implementation scope is the established Linux host-Bridge deployment used by the VPS. Native Windows/macOS enablement is not claimed until their operator-config renderers gain the same policy surface and measured acceptance exists. The MCP/native defaults remain Repo Ops disabled.

## 5. Security invariants

1. Repo Ops never exposes a generic command executor.
2. The MCP caller cannot choose executable, argv, env, cwd, remote, ref, shell syntax, or force semantics.
3. All caller path inputs pass ServerFS path/deny policy before Bridge RPC submission.
4. The Bridge independently confines work to the configured host workdir root.
5. Repo mutations and every named action acquire the same exclusive writer lease used by Agent workspace-write tasks.
6. A recovery guard is reconciled before a commit, push, or named action may acquire the lease.
7. Output and runtime are bounded; timeout/output overflow fails closed and terminates the whole Linux subprocess group before releasing the writer lease.
8. Repo Ops child processes receive a narrow allowlisted environment; ambient provider/API secrets are not forwarded. Git additionally receives only the minimal host context needed for normal user credentials such as `HOME`/`SSH_AUTH_SOCK`.
9. `git push` uses host-native credentials only and never returns credential material. Git hooks are disabled, repository `core.sshCommand`/askpass overrides are neutralized, and an environment-level protocol allowlist denies unapproved remote-helper protocols even if Git config tries to re-enable them.
10. Deterministic commit staging rejects any requested path whose Git attributes select an external content filter, and commit signing is forcibly disabled for the Repo Ops commit path.
11. Repo Ops errors/logs do not expose absolute host paths.
12. No Repo Ops code imports or calls provider adapters.
13. Named actions must verify operator-pinned integrity metadata before process creation; repository code changes cannot silently widen executable behavior.
14. Git status never returns raw content and is filtered by the ServerFS deny policy; Git diff requires explicit caller paths that already passed the same deny policy.

## 6. LLM_WiKi target policy

For the VPS `LLM_WiKi` workdir, enable Repo Ops with exactly these named actions:

- `wiki_lint` → `uv run --frozen --no-sync python -I tools/wiki-lint.py` (`mutates=false`)
- `wiki_lint_fix_index` → `uv run --frozen --no-sync python -I tools/wiki-lint.py --fix-index` (`mutates=true`)
- `registry_ensure` → `uv run --frozen --no-sync python -I tools/compile_registry.py ensure <basename>` (`mutates=true`, `argument=basename`); the caller submits the real source path such as `raw/foo.md`, the Bridge validates that path as an existing regular file, then passes only `foo.md` because the existing registry CLI stores raw-file keys as basenames.

All three actions pin the current LLM_WiKi execution-chain inputs. Current measured SHA-256 values on 2026-10-25 are:

- `tools/wiki-lint.py` → `059c75aed32ea6af02def2554473c143368e2d8ab428bb3a11f1a5490b2ff9be`
- `tools/compile_registry.py` → `936f0da02936acb8c541a9c5a836dcaf38846e66db2d2641308fe25a16df8c96`
- `pyproject.toml` → `d000277a4df531e25f4247ac10c8c979cfb31bd3b9d6b20c1f67c73fd755ac21`
- `uv.lock` → `ce8a1112e37b3dd5f47f6ee986f485a52126c6c23e05dd5a52383af6d24deee8`
- `.python-version` → `49a506dd32096b010d75205acf3430c9ae6c40351888129499e5a5e487126c93`

If any pinned file intentionally changes, Repo Ops must refuse that named action until an operator reviews the change and explicitly updates the configured digest. ChatGPT must not self-update these digests as part of the same repository task.

The LLM_WiKi Python actions use isolated mode (`python -I`) so a caller cannot add a repository-local `yaml.py`, `hashlib.py`, or similar module and hijack imports after the pinned entrypoint passes integrity verification. The installed Python/uv environment and site-packages remain operator-owned runtime dependencies.

ChatGPT continues to perform source reading, clipping, synthesis, compilation, provenance decisions, and file edits itself. Repo Ops only performs deterministic repository gates and Git publication.

## 7. Tests

### Root MCP tests

- Repo tools absent when no workdir enables Repo Ops.
- Repo tools present when at least one workdir enables Repo Ops and Bridge transport is enabled.
- workdir authorization and read-only enforcement.
- path-policy rejection before RPC for hidden/denied/outside paths.
- malformed action/message/path inputs rejected.
- Bridge unavailable/remote errors normalized without Agent-task wording.
- public schemas contain no executable/argv/env/remote/ref/force fields.

### Agent Bridge tests

- config rejects unknown Repo Ops fields, malformed JSON/action profiles, duplicate/invalid action names, empty argv, unsupported argument kinds, and any named action on read-only workdirs.
- `repo.*` RPC methods never call Agent adapters.
- read operations are bounded.
- commits, pushes, and every named action honor the writer lease and recovery guard; read-only workdirs cannot configure or run named actions.
- timeout/output-limit cancellation kills the Linux Repo Ops subprocess group so child processes cannot outlive the lease.
- named actions do not inherit arbitrary Bridge environment variables/provider secrets.
- commit refuses pre-existing staged state, rejects external Git clean/process filters, disables commit signing/hooks, and stages only requested paths.
- push derives the current upstream, validates the remote/ref, supplies exactly one `HEAD:<upstream-branch>` refspec (ignoring wider `push.default` / `remote.*.push` behavior), is non-interactive, disables hooks/askpass/repository SSH-command overrides, restricts Git transport protocols, and has no request-controlled remote/ref/force.
- named actions execute fixed argv with `shell=False`; optional path is appended as one argv element after independent confinement validation;
- named actions fail before process creation when any pinned integrity file is missing, symlinked, oversized, or digest-mismatched;
- status/diff cannot bypass hidden/credential path policy.

### Linux renderer/deployment tests

- `.env` Repo Ops fields render exactly into private Bridge config.
- disabled slots cannot enable Repo Ops.
- Bridge may run with Repo Ops enabled even when no Agent runtime is enabled.
- existing Agent-only configurations remain unchanged.

## 8. Validation gates

Because this change crosses root MCP + Agent Bridge + Linux deployment rendering:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest

cd agent_bridge
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest

cd ..
bash -n deployment/agent-bridge/*.sh
```

Also run focused Repo Ops tests before the full suites.

### Current implementation evidence — 2026-10-26

Source-only validation completed from exact VPS worktree snapshots without invoking a ServerFS Agent runtime:

- Agent Bridge Repo Ops focused suite: **38/38 passed** across `test_repo_ops.py`, `test_repo_ops_config.py`, `test_repo_ops_service.py`, and `test_repo_protocol.py`. This covers path confinement, fixed/integrity-pinned named actions, environment isolation, bounded process-tree cleanup, external-filter rejection, exact-upstream push confinement, Agent-disabled operation, RPC dispatch, and shared writer-lease exclusion;
- root Linux policy/renderer/Compose wiring focused suite: **8/8 passed** across `test_repo_ops_deployment.py` and `test_repo_ops_workdirs.py`; the Compose regression test proves all 16 `WORKDIR_XX_REPO_OPS_ENABLED` values reach the MCP container while private `REPO_ACTIONS_JSON` never does;
- total dynamic focused validation: **46/46 passed**;
- the first dynamic run exposed a real regression in the final `git check-attr` hardening: `check-attr` consumes pathnames rather than Git pathspec magic, so wrapping the path with `:(literal)` caused four false `REPO_STAGE_FAILED` results. The implementation was corrected to pass the already-confined pathname to `check-attr`, while `diff` / `add` / rollback staging operations retain literal pathspecs; the same focused suites then passed completely;
- current changed Python snapshots pass AST parsing / `compileall` and the repository's 100-column source limit check;
- the isolated environment does not contain the root MCP `mcp` dependency or Ruff, so the repository-wide root MCP tool-surface tests and Ruff gates have **not** been claimed as executed.

The source-focused results above were followed by the real user-scoped Linux bootstrap on the VPS. After fixing the bootstrap to use the project-level uv Python environment, resolving Ruff/format gates, and fixing asyncio subprocess transport cleanup exposed by the first full run, the final bootstrap on 2026-10-26 is **CLOSED-PASS**:

- root project: `uv run --frozen --no-sync ruff check .` PASS; format gate PASS; **1780 passed, 320 skipped, 1 warning**. The only warning is the existing upstream Starlette `anyio.abc.BlockingPortal` deprecation warning;
- Agent Bridge project: Ruff PASS; format PASS; **497 passed, 15 skipped, 0 warnings**. The earlier `PytestUnraisableExceptionWarning` is gone after `run_bounded_command()` was hardened to let stdout/stderr pipe readers settle before the event loop closes;
- docs/site at bootstrap time: `npm ci` + Astro build PASS, **37 pages built**. The existing unsupported `env` / `cron` expressive-code language warnings fall back to `txt` and do not fail the build. This remains historical bootstrap evidence; the later post-freeze Pages build/deploy also passed before release closeout;
- Compose shapes PASS; scratch image `serverfs-mcp:dev` built successfully. The host lacks Docker buildx, so Compose used the default builder; the image build still completed successfully;
- all 16 writer leases were acquired for the maintenance cutover;
- the dirty user-scoped Host Bridge was staged with `--no-start`, explicitly restarted, and `verify_host.py --require-runtimes` returned `bridge_rpc=PASS`, `enabled_runtimes=PASS:claude,codex,qoder`, and `user_scoped_deployment=PASS`;
- the scratch MCP container was recreated with the temporary Repo Ops policy and the Tunnel restarted;
- in-container acceptance returned **`MCP Repo Ops surface: PASS`** and **`Host Repo Ops actions: PASS`**;
- `list_workdirs` from ChatGPT already observes `LLM_WiKi.repo_ops=true`, proving the live v0.23 policy/schema path is active on the service side.

The public ChatGPT MCP acceptance completed on 2026-10-26 after the connector refresh. ChatGPT used the actual public Repo Ops tools against `LLM_WiKi`: `inspect_repo(actions)` exposed exactly `registry_ensure`, `wiki_lint`, and `wiki_lint_fix_index`; `inspect_repo(status)` showed only the two intended documentation changes; `run_repo_action(wiki_lint)` returned code 0 with `阻断问题: 0`; explicit diff inspection matched the intended architecture change; `commit_repo_changes` created `b3621bc5342e834990c170720de149d846d7de34`; `push_repo` pushed that exact commit to the current `origin/main`; final status was clean. No `submit_agent_task` or provider task was used. The first commit invocation exposed a transient ChatGPT connector catalog-cache mismatch (`tool was not present in the inspected catalog`); reloading the connector catalog resolved it and the same public tool then succeeded, so this is recorded as client/catalog synchronization evidence rather than a Repo Ops implementation failure.

A follow-up 2026-10-26 operational-Skills audit aligned all seven packaged Skills with the v0.23 boundary: Repo Ops policy remains under `serverfs-configure`, Host Bridge lifecycle under `serverfs-agent-bridge`, install/upgrade/diagnose/tunnel/auth preserve the Host Bridge without inferring Agent enablement, native Windows/macOS remain Repo Ops-unclaimed, and deterministic Repo Ops must never fall back to Agent submission. `tests/test_agent_skills.py` now carries a semantic regression test for these contracts. Because that test/source changed after the bootstrap suite, the bootstrap's 1780-pass root result is historical evidence rather than final-source validation.

The post-Skills-audit and post-documentation-freeze gates subsequently completed successfully: Container run `40273205154`, Windows Native `40273205186`, Windows Agent `40273205178`, macOS Native `40273205159`, and Pages `40273290743` all concluded success; the Container run also passed the refreshed Agent Skills/Plugin, Registry, Compose, Ruff/format, complete root/Agent Bridge tests, and MCP conformance gates. The only remaining pre-tag step is revalidating the final v0.23.0 version/release-facing metadata commit. Publication and post-release deployment evidence are tracked in the release checklist. The n8n/GitHub bootstrap alternatives remain unnecessary and must not be used as a substitute for the public MCP path.

## 9. Acceptance criteria

PASS requires all of the following:

1. ChatGPT can inspect LLM_WiKi Git status/diff without Agent submission.
2. ChatGPT can run `wiki_lint`, `wiki_lint_fix_index`, and `registry_ensure` only through fixed named actions.
3. ChatGPT can commit explicit changed paths and push the current upstream without arbitrary Git arguments.
4. `submit_agent_task` is never called during Repo Ops E2E.
5. concurrent Agent/filesystem writers fail closed via the shared lease.
6. no generic shell/argv/env MCP surface exists.
7. source tests and Linux live E2E pass before release/deployment is declared complete.

## 10. Rollout

1. Implement source + deterministic tests.
2. Validate root, Bridge, and Linux renderer gates.
3. Configure Repo Ops only for `LLM_WiKi` on the VPS with the three named actions above.
4. Rebuild/update the user-scoped ServerFS deployment through the normal documented lifecycle.
5. From ChatGPT, verify `actions → status → wiki_lint → explicit commit → push` without any Agent task. **PASS on 2026-10-26**; public commit `b3621bc5342e834990c170720de149d846d7de34` was pushed to `LLM_WiKi` `origin/main`.
6. Re-run root Ruff/format and Agent Skills validation/tests after the Skills audit; refresh the complete root suite. **PASS** in Container run `40273205154`.
7. Run the fresh post-documentation-freeze site build/diff gate and cross-platform regression workflows. **PASS**: Pages `40273290743`, Windows Native `40273205186`, Windows Agent `40273205178`, macOS Native `40273205159`.
8. Align authoritative versions and all current release-facing documentation to v0.23.0, re-run the final release-commit gates, then create immutable tag `v0.23.0` and let the tag workflow publish the GitHub Release/assets.
9. Replace the temporary VPS bootstrap/dev artifact with the official v0.23.0 release deployment, preserve permanent Repo Ops policy, refresh the client schema, and perform final live verification without Agent fallback.
