# AGENTS.md

Read `dev_plan_v0.7.md` first for the current v0.7.0 release line: Runtime Reliability & Observability, schema-versioned event envelopes, opaque correlation IDs, immutable execution manifests, task deadline/retention, provider-aware restart reconciliation, persistent active-slot recovery guards, and bounded large-result spooling/retrieval. For v0.7.0 work, that plan plus executed tests and implementation are authoritative over older Agent Bridge assumptions.

Read `dev_plan_v0.5.md` for the released/frozen v0.5.0 line: ChatGPT/OpenAI file-parameter ingress, the isolated file-ingress sidecar, MCP request-body sizing, and release closure. For every v0.5.0 change, that plan plus executed tests and implementation are authoritative over older binary-transfer assumptions.

Read `README.md` for the current v0.7.0 behaviour, security model,
deployment and release contract. Read `dev_plan_v0.4.md` for the frozen v0.4 design and
acceptance baseline for hierarchical workdir policy, binary file transfer and the Issue #10
transport-security fix.
Read `dev_plan_v0.3.md` for the frozen v0.3 Agent Bridge contract: provider-neutral
long-running tasks, Codex App Server mapping, Claude Agent SDK mapping, human
approvals/questions, cross-process workdir leases and Phase E deployment. The final v0.3
acceptance evidence is recorded in `docs/phase-e-acceptance-2026-09-20.md`. Read
`dev_plan_v0.2.md` for the historical v0.2 filesystem-mutation baseline, and `dev_plan.md`
for the original v0.1 baseline. Where v0.4 explicitly extends an older rule,
`dev_plan_v0.4.md` wins for v0.4 work; otherwise README, tests, implementation and the
accepted v0.3 contracts remain authoritative.

The `agent_bridge/` directory carries the **released/frozen v0.3 provider-neutral Agent
Bridge contract** forward. v0.6.0 added an optional Jev advisory layer; v0.7.0 adds
reliability/evidence features without changing runtime authorization, provider approval
semantics, or writer-lease authority. The Bridge package version is 0.7.0 in this release.
Phases A (provider-neutral core), B (Codex native-mode adapter), C (Claude Code native-mode
adapter), D (Agent MCP surface) and E (production deployment) are complete and frozen on
`main`. Production Agent delegation remains opt-in through `compose.agent.yml`; the base
`compose.yml` intentionally preserves the 11-tool filesystem-only surface.

The Jev advisory suite is an explicitly opt-in experimental capability on `main` over
that frozen baseline. It adds advisory task-quality Preflight, Runtime Router, and Approval
Advisor results inside the host Bridge, but it must not change the MCP tool surface, Bridge
RPC, runtime allowlists, provider adapters, authorization, approval/question decision
semantics, or writer-lease semantics. Jev is not an Agent runtime or a safety authority.
With no `SERVERFS_JEV_API_KEY`, ServerFS must construct no Jev client, make no Jev request,
emit no advisor result, and preserve baseline behavior. With a key, all Jev features remain
fail-open and advisory only. The router must not add `runtime=auto`, invoke a different
runtime/tool, or override the explicitly requested runtime. Approval Advisor may make one
extra Jev request only after a provider creates an approval request; it must not auto-approve,
auto-deny, alter available decisions, grant permission IDs, or bypass the existing
`respond_agent_approval` validation path.

**Phase D is frozen except for the explicit v0.7.0 additive extension.** It originally added eight provider-neutral Agent MCP tools, a thin
stdlib Unix-socket Bridge client, fail-closed global/per-workdir Agent configuration,
audit records, and the shared cross-process writer lease consumed by existing mutation
tools. v0.7.0 adds exactly one read-only public tool, `read_agent_task_result`, plus additive protocol fields/RPC needed for correlation metadata and spooled-result retrieval. Do not otherwise modify the MCP public surface, UDS protocol, local authorization model or shared writer-lease contract except to fix a demonstrated defect. Phase D kept
`SERVERFS_AGENT_BRIDGE_ENABLED=false` as the default, so an upgrade retains the 11-tool
v0.2 surface unless the administrator explicitly enables Agent delegation. Agent tools
talk only to the Bridge RPC contract; they never import provider adapters or provider
SDKs into `serverfs-mcp`.

**Phase E deployment contracts are frozen after acceptance.** They define the production
deployment layer around the frozen A–D contracts: an opt-in Compose overlay, host-side
systemd lifecycle, measured SO_PEERCRED identity, shared runtime-directory permissions,
deployment config rendering, provider-environment documentation, container-to-Bridge
verification, ChatGPT/Tunnel E2E and rollback/release documentation. After v0.3.0
acceptance, do not redesign the MCP tool surface, Bridge RPC, provider adapters or lease
semantics unless a real deployment test demonstrates a defect.

Phase E deployment is **user-scoped only**. Do not require sudo/root, create system
users/groups, write to /etc, /opt or /var/lib, or install a system-level service. The
Bridge runs as the current login user whose native Codex/Claude environment is reused;
lifecycle uses `systemctl --user`. Application/config/state live below
`~/.local/share`, `~/.config` and `~/.local/state`; the bind-mounted socket/lock
runtime directories are persistent user-owned paths below
`~/.local/share/serverfs-agent-bridge/runtime` so their inode identity survives Bridge restarts.

Agent-enabled deployment requires the `serverfs-mcp` container to request the same
UID/GID as the current login user. The Bridge still measures the real host-kernel
SO_PEERCRED identity. That equality is a post-measurement assertion, never a shortcut:
do not synthesize `SERVERFS_AGENT_PEER_UID/GID` from `id -u` / `id -g`; leave them unset
until the real container peer probe measures them. If rootless Docker/userns-remap makes
the actual peer differ from that user, fail closed and report that the default user-scoped
deployment is incompatible; do not propose privileged ownership/group changes as a
workaround.

Socket dir 0750/socket 0660 and pre-created lock dir 0750/lock files 0640 use the user's
existing primary group only. The MCP container consumes both directories through read-only
bind mounts and must never create host lock files. Keep base `compose.yml` Agent-unaware;
Phase E uses explicit `compose.agent.yml`.

The repository-root `.env` is the single deployment configuration source for both base
ServerFS and Phase E. Do not reintroduce `.env.agent`, a second env-file precedence layer,
or installer-generated deployment env files. `.env.example` documents the complete
configuration surface. Provider secrets/shell-only variables remain outside the repository
in `~/.config/serverfs-agent-bridge/provider.env` except for the opt-in Jev advisor suite:
`SERVERFS_JEV_API_KEY` lives in the existing untracked repository `.env`, is omitted when
empty, and when configured is rendered only into the private 0600 Bridge config. It must
never be passed into the MCP container, logs, RPC responses, or task events.

**Streamable HTTP transport security is a v0.4 security invariant (GitHub #10).** The
production endpoint remains `http://serverfs-mcp:8000/mcp`; `MCPServer.run()` must receive
an explicit `TransportSecuritySettings` with DNS-rebinding protection enabled, exact
`allowed_hosts=["serverfs-mcp:8000"]`, and no allowed non-empty Origins. In
`mcp==2.2.0`, absent Origin is accepted; invalid/missing Host fails with HTTP 421 and a
non-empty disallowed Origin fails with HTTP 403 before MCP dispatch. Do not broaden the
Host/Origin allowlist, make it wildcard-configurable, or remove the startup wiring merely
to accommodate a different development topology. Any legitimate topology change must be
measured and regression-tested first. Evidence is in
`docs/transport-security-audit-2026-09-21.md`.

**ChatGPT file ingress is a v0.5 security boundary.** `serverfs-mcp` must retain no Internet egress. Its client is hard-wired to the internal `serverfs-file-ingress:8081/fetch` endpoint through plain `HTTPConnection` and must not gain configurable destination URLs or redirect following. The optional `serverfs-file-ingress` sidecar has no workdir mounts, no tunnel/OpenAI credentials and no published port. It accepts HTTPS/443 only and requires either exact administrator-configured hosts or the explicit constrained OpenAI Azure Blob family measured from real fileParams: storage-account label starts `oaisdmntpr`, uses only lowercase ASCII letters/digits, fits Azure's 24-character account limit, and ends with exactly `.blob.core.windows.net`. Generic `*.blob.core.windows.net` or other wildcards remain forbidden. Every accepted hostname still goes through all-global DNS validation, IP-pinned connection, original-host TLS verification, and per-redirect revalidation. Never turn it into a generic URL fetcher, broad wildcard proxy, workdir-aware service, or credential-bearing component. Network retrieval happens before the existing mutation lock/writer lease; publication still uses the frozen binary mutation primitives. See `dev_plan_v0.5.md`.

OpenAI file-parameter schema is part of the public tool contract: `upload_binary_file` advertises `_meta["openai/fileParams"]=["file"]` only when ingress is enabled; the file object declares string properties `download_url`, `file_id`, `mime_type`, `file_name`, with only `download_url` and `file_id` required. The upload accepts exactly one payload source (`data_base64` XOR `file`). Never derive the destination path from `file_name` or log the URL/file ID.

systemd user mode is a lifecycle manager only: do not add provider sandbox/hardening that
changes the native provider capability model frozen in Phases B/C. Do not auto-enable
login lingering; whether the user's systemd manager persists after logout is an
environment/administrator policy outside this project.

Do not add a generic shell/argv/env MCP tool. Do not replace the nine tools with the MCP
Tasks extension yet: as of 2026-09-20 the official Python SDK still lists
`io.modelcontextprotocol/tasks` as not implemented. Keep the backend compatible with a
future Tasks adapter instead.
`SERVERFS_DISABLE_DEFAULT_DENY` is one rule this project deliberately reversed, and v0.1's
"read-only is a product property, not an option" was superseded by v0.2's per-workdir
opt-in. This file carries what none of them does: the reasons behind the design, the traps
that already cost debugging time here, and how work gets verified in this repository.

## Website / GitHub Pages

`site/` is a first-class but isolated static website. It is Astro + Starlight and MUST
remain deployable as a pure GitHub Pages artifact: no SSR, database, serverless runtime
or required third-party runtime CDN. The project-site base is `/ServerFS_MCP/`. Custom
Astro pages build internal URLs from `import.meta.env.BASE_URL`; Starlight-owned asset
options such as `favicon` stay base-relative (for example `/favicon.svg`) because
Astro/Starlight applies the configured base. Hard-coding `/ServerFS_MCP/` into both
layers creates a double-base URL.

English is the root locale and Simplified Chinese is `/zh-cn/`. The custom landing page
uses one shared `HomePage.astro` with locale-specific copy; do not fork the page layout
per language. Translated Starlight docs mirror the English slugs under
`src/content/docs/zh-cn/`, and `i18nLoader()` / `i18nSchema()` remain configured so
locales are a real content contract rather than a suppressed warning. The Chinese landing
page self-hosts Noto Sans SC Variable; keep Simplified-Chinese glyph rendering
deterministic and locally served rather than depending on platform CJK fallback or an
external font CDN.

Let Starlight own its semantic light/dark palette. Brand accent overrides may be scoped
to `data-theme="dark"` / `data-theme="light"`, but do not globally redefine
Starlight's base white/gray/black variables or add an unconditional dark body background.
Verify Auto, Light and Dark when Docs theme CSS changes.

For semantic architecture/topology diagrams, prefer content-driven Grid/Flex layout over
absolute coordinates. Localization and responsive widths make coordinate layouts brittle.
A successful static build proves routes and syntax, not rendered geometry: visual changes
need real browser evidence. If no browser is available, report the rendering check as
`Not verified` and use maintainer screenshots rather than inferring visual correctness
from source alone.

A site-only change uses the site gate: `cd site && npm ci && npm run build`, then
`git diff --check`. It does not require the Python/Docker root gate unless it crosses
into runtime/deployment code. Pure `site/**` changes should trigger the Pages workflow
only; preserve the Container workflow's site-path exclusions so website edits do not
publish a new `:edge` image.

## Change protocol

Start from a written problem statement — a task-book section, a review issue, an
observed misbehaviour. Restate the issue first, then make every changed line trace
to it. This codebase has been hardened by successive review passes; unsolicited
rewrites and drive-by cleanups discard decisions that are not visible from the code.

Delegated Agent tasks are capability-bounded. A verification-only, Git-only,
deployment-only or metadata-only task does not acquire permission to edit source when it
finds a failure: stop at the declared boundary and return exact evidence to the
orchestrator. Prefer atomic delegated tasks with one objective and an explicit
allowed-mutation set.

For normal authorized engineering and operations, write delegated prompts to minimize
ambiguity rather than to defeat external safety checks. State the user-owned workdir or
existing deployment being operated on, one concrete objective, the allowed mutation set,
stop conditions, and the evidence to return. Include shell commands, network details,
credentials terminology or security-analysis context only when they are actually required
for that atomic objective; do not bundle unrelated implementation, deployment, probing and
Git work into one task. Never encode, disguise, fragment or relocate instructions merely
to evade a provider safety classifier. When a legitimate task is blocked, narrow the task
at a real capability boundary or use an existing structured ServerFS primitive; do not
weaken authorization, audit, writer-lease or network-isolation controls.

Work in steps: modify, run the targeted test, then run every gate relevant to the changed
boundary. Reaching the end of an edit is not a milestone; executed verification is.

Backend/MCP behaviour fixes land with a regression test, exercised through the MCP
surface where the bug was observable — a test that calls an internal helper proves less
than one that calls the tool. Website-only visual/content fixes follow the independent
site gate and rendered-evidence rule in Website / GitHub Pages.

For runtime, filesystem, security or base deployment changes, the full root gate in
`README.md` → Development passes: `uv sync --frozen`, `ruff check`,
`ruff format --check`, `pytest`, `docker compose config`,
`SERVERFS_IMAGE=serverfs-mcp:dev docker compose build`. All six, actually executed.
The scratch tag is not decoration: `image` doubles as the tag Compose builds to, so an
untagged build repoints whatever `SERVERFS_IMAGE` names — see Traps.

The root pytest configuration collects only `tests/`; it does **not** collect
`agent_bridge/tests/`. Whenever a change touches `agent_bridge/`, also execute its
independent gate from that directory: `uv sync --frozen`, `uv run ruff check .`,
`uv run ruff format --check .`, and `uv run pytest`. Whenever Phase E shell scripts
change, run `bash -n deployment/agent-bridge/*.sh` from the repository root as an
additional syntax gate. A green root suite never substitutes for either of these.
Cross-boundary changes run every applicable gate; a site-only change uses the site gate
instead of the unrelated Python/Docker gate.

Then report: files changed, how each issue was fixed, regression tests added, pytest
counts, and residual limitations. Anything not executed is `Not verified` — never
"should pass" or "theoretically fine".

## Channels

Every way a path is reached — read, stat, list, find, search, resource — is a
**channel**. The central invariant of this project is that **all channels filter
identically**.

A `DenyPolicy` is built once per call and travels on `ResolvedPath`; each channel
reads the policy from the resolved path rather than re-deriving rules. A new channel
or a new filter routes through that same object — a second matcher implementation is
a defect, not a shortcut.

`allow_hidden` and the credential deny rules are independent axes; all four
combinations are legal configurations and are covered by tests. Keep them uncoupled.
The reserved names are a third axis that is **not** configurable: they ride inside
`DenyPolicy.is_denied` so entry filtering and path resolution cannot drift apart, and
`resolve_workdir_path` raises `RESERVED_PATH` ahead of the deny check so the agent gets
the precise code. Two names are reserved — `.serverfs-tmp-*` (`RESERVED_TEMP_PREFIX`)
and the workdir registry's disabled-slot sentinel `.serverfs-disabled`
(`workdirs.DISABLED_SENTINEL`, enforced centrally by `paths.is_reserved_component`).
Adding a third internal name means adding it *there*, in `RESERVED_RG_EXCLUDES` and in
the reserved-channel tests — not at a call site.

Tracing a deny bypass means following the *full* workdir-relative path on every
channel. A policy decision made against a search root's own relative path is a
partial path, and partial paths are how bypasses ship.

## MCP result schemas

The public MCP schema is part of the tool contract, not decoration. A tool that returns
structured metadata must expose a matching `outputSchema` so clients can understand the
result without reverse-engineering prose. For mixed results such as
`download_binary_file`, preserve the binary resource content block and
`structuredContent`; expose the metadata model through the return annotation rather
than flattening the response or dropping the resource block. Regression tests should
assert the exposed schema against the model's JSON schema as well as testing the runtime
payload.

## Mutation contract (v0.2 baseline, extended by v0.4 binary transfer)

Five v0.2 tools remain narrow, with no general-purpose write. `create_text_file` and
`create_directory` require the target to be absent; `edit_text_file` requires it to exist
and to be a UTF-8 regular file; `delete_file` takes any regular file;
`delete_directory` takes an empty directory. Their public contracts remain unchanged.

v0.4 adds one deliberate exception to the historical "no overwrite" rule:
`upload_binary_file` may use `overwrite=true` **only** for an existing regular file and
**only** with a required `expected_revision`. It is revision-guarded replacement, not a
`force` operation and not create-or-replace. There is still no recursive mutation and no
unguarded overwrite path. See `dev_plan_v0.4.md` sections 9 and 19.

The mutation pipeline in `mutations.py` is `authorize (workdir read-write) → resolve
(policy) → root/parent FD walk → act on the final NAME with dir_fd=parent_fd`. Every
mutation takes the process-wide `mutation_lock()`; reads never do.

- **create** publishes with `os.link` from a reserved same-directory temp file, which
  cannot overwrite anything and leaves no check-then-create window. `create_text_file`
  deliberately advertises `idempotentHint=false`: even a repeat that ultimately fails
  with `PATH_ALREADY_EXISTS` creates and removes that temp entry first, so the parent
  directory's metadata/revision may change. `create_directory` remains idempotent.
- **edit** reads and verifies the target by FD, applies exact-match edits in memory,
  writes a temp file, copies mode/ownership/xattrs onto it, re-checks the revision, and
  publishes with `os.replace`. Nothing is written before every edit validates.
- **delete** re-checks the revision, then `unlinkat`/`rmdirat` by name while still
  holding the verified FD. `delete_file` refuses a file the process cannot read —
  directory write permission alone must not delete it.

Fatal versus logged: everything before the commit (temp `fsync`, metadata copy, the
final revision re-check) aborts the mutation; the directory `fsync` after the commit is
logged as `directory_fsync_failed` and the call still reports the mutation, because the
entry is already visible. The same reasoning applies to the text-file contract: the NUL
gate lives on both sides (`content` for create, `old_text`/`new_text` for edit) so no
mutation channel can produce a file every read channel then refuses.

Revision tokens are `v1:<16 hex>` of a SHA-256 over the stat tuple. Two rules make them
usable: compute them from the same object the caller will later stat, and derive them
from the whole tuple (size, mtime_ns, ctime_ns, nlink) so metadata changes are visible.
`read_text_file` fstats before and after reading and reports
`FILE_CHANGED_DURING_READ` rather than returning content that does not match its
revision. `edit`/`delete` verify `expected_revision` inside the lock *and* immediately
before the commit.

## Streamable HTTP transport security (v0.4)

GitHub Issue #10 established that Docker `internal: true` networking and an unpublished
port are not sufficient as the only DNS-rebinding boundary. The v0.4 Streamable HTTP
entry point therefore MUST pass an explicit `TransportSecuritySettings` to the pinned
`mcp==2.2.0` server runtime. The accepted production policy is intentionally narrow:
DNS-rebinding protection enabled, `allowed_hosts=["serverfs-mcp:8000"]`, and no allowed
non-empty Origin. In this SDK, an absent Origin is accepted as same-origin; unexpected or
missing Host is rejected with HTTP 421 and a non-empty unapproved Origin with HTTP 403,
before MCP dispatch.

Do not replace the exact Host with a wildcard, add a configurable broad allowlist, or
remove `transport_security=` from the production `mcp.run("streamable-http", ...)` call
without new measured deployment evidence. Keep both regression layers: the real ASGI
Host/Origin tests in `tests/test_security.py` and the startup-wiring assertion in
`tests/test_main.py`. Real Tunnel compatibility evidence is recorded in
`docs/transport-security-audit-2026-09-21.md`.

## Filesystem access

Request-derived traversal is FD-based: each component is opened relative to an
already-open directory descriptor — `dir_fd` plus `O_NOFOLLOW`, and `O_DIRECTORY` for
directories — and the final descriptor is `fstat`ed. `fdio.py` holds the shared
primitives and is the security boundary. No request-derived path travels as
`lstat`-then-`open(path)`: that gap is the TOCTOU window this design closes.

The workdir root is the one path opened by name — the trusted anchor from
configuration, carrying no request input, which is why the walk starts there.
Every root open goes through `fdio.open_root` / its context-managed wrapper
`fdio.root_fd`: `tools.py`, `filesystem.py` and `mutations.py` each keep a thin
`_root_fd` helper that delegates there, and `find_files` calls `open_root`
directly only because it owns the descriptor across a whole walk. Do not add a
fourth root-open implementation, and do not bypass the error mapping in `fdio`.

## Search

`rg` runs with `shell=False` and an argument array, `--json` output, streamed through
`selectors`, stopping at `limit + 1` policy-valid matches. Its cwd is the validated
directory FD via `/proc/self/fd/<fd>`, and result paths are re-checked against the
hidden and deny policy — validating the search root is one gate, not the only one.
`-L`/`--follow` are never passed; rg follows no symlinks.

## Logs

Structured JSON, one event per tool call. File contents, search queries, host and
container paths, and credentials stay out of the log, including error text.
Agent-facing errors are `CODE: short message`; internal paths are for DEBUG logs at
most.

## Intentional decisions

`SERVERFS_ALLOW_HIDDEN`, `SERVERFS_DISABLE_DEFAULT_DENY` and
`SERVERFS_EXTRA_DENY_GLOBS` are product features, not oversights: safe by default,
explicitly releasable by the administrator. In v0.4 they become global policy defaults
with per-workdir overrides. Scalar workdir policy wins over the global default, while
`EXTRA_DENY_GLOBS` is intentionally additive: global deny globs remain a security floor
and workdir globs can only add restrictions. Reserved internal paths remain
non-configurable and denied everywhere.

`WORKDIR_XX_READ_ONLY` (default `true`) is the v0.2 write switch, and it is one variable
driving two layers on purpose: ServerFS's own authorization and the Compose bind-mount
flag. A read-only workdir must refuse mutations even if the mount is accidentally
writable — application authorization is checked before the path policy, so
`WORKDIR_READ_ONLY` wins over `HIDDEN_PATH_NOT_ALLOWED`. Parsing is strict
(`true/false/1/0/yes/no/on/off`, empty = read-only) and an unknown value is a startup
error: this switch must never fail open, and an upgrade from v0.1 must not gain write
access by omission.

The deployment shape — `serverfs-mcp` + `openai-tunnel`, internal-only network, no
published ports, no OAuth, 16 workdir slots — is fixed for v0.2. Shell execution,
indexing, a web UI, rename/move/copy, recursive mkdir/rmdir, binary editing, chmod/chown
and non-OpenAI clients are out of scope for v0.2, not pending work.

## Traps

- `O_NOFOLLOW|O_DIRECTORY` reports a symlink as `ENOTDIR`, not `ELOOP`. Classify it
  with a supplementary `lstat` after the open has already failed — the access itself
  is still refused by the kernel, so this adds no race.
- An empty component set means the workdir root: `os.dup` that descriptor.
  `/proc/self/fd/N` is a symlink in its own right and `O_NOFOLLOW` rejects it.
- `linkat`, `unlinkat` and `renameat` all update the *moved* inode's ctime and nlink.
  A revision computed before publication therefore does not match the next `stat_file`
  of the same file, and an agent that creates then immediately edits gets a spurious
  `REVISION_CONFLICT`. Compute the revision after the commit — both create and edit
  fstat the temp FD again once the name is in its final place.
- `os.scandir(fd)` does not close the descriptor it was given, so the FD walk helpers
  can keep owning and closing it. Do not "fix" the double-looking close.
- `Path.read_text()` translates CRLF to LF. Any test asserting byte fidelity of created
  or edited content must compare `read_bytes()`, or it fails on correct output.
- Do not diagnose localization from a screenshot alone. Compare source text, built HTML
  and the raw live HTTP response before attributing a problem to content, deployment or
  rendering. Different Unicode text is a content/transform issue; identical raw text with
  different visual output is a client/font/rendering issue. Preserve this evidence order
  before changing translations.
- `docker compose config` refuses `1`/`0` for a boolean field
  (`failed to cast to expected type: invalid boolean: 1`) and merely warns on
  `yes/no/on/off` under YAML 1.2. `WORKDIR_XX_READ_ONLY` must be documented as
  `true`/`false`; the parsing differences between Compose and `parse_read_only` are
  fail-closed by construction, because an ambiguous value stops the deployment.
- A test helper that opens a different root than production hides real bugs. The
  scoped-search path collapse (`foo/foo/`) survived a full green suite because the
  helper passed the workdir root where production passes the search root. Keep
  helpers on the production call path.
- `docker compose config` interpolates `.env` and prints real secrets. Never paste
  its raw output; select the field you need, e.g.
  `docker compose config --format json | jq -r '.services["serverfs-mcp"].image'`.
- A bare `GET /mcp` against a running container can terminate the tunnel's active
  MCP session, leaving the deployment idle rather than visibly broken. Probe the
  tunnel's `/readyz` instead.
- The running deployment is live and serves a real tunnel. Exercise new behaviour on
  a throwaway stack under a separate compose project name rather than against it.
- Rebuilding the image is not deploying it: the running container keeps the old
  image until `docker compose up -d` recreates it.
- On an Agent-enabled deployment, recreating `serverfs-mcp` with only the base
  `compose.yml` silently drops the Agent overlay even if `.env` still contains valid
  Agent configuration: the socket/lock mounts and Agent environment disappear and the
  tool surface falls back to Agent-disabled. Every Agent-enabled recreate/upgrade must
  use both `compose.yml` and `compose.agent.yml`.
- Recreating `serverfs-mcp` strands the tunnel, and `/readyz` will not tell you:
  `docker compose up -d` recreates only the service whose image changed, so the
  tunnel keeps running with its MCP session and connections belonging to a container
  that no longer exists — ready, quiet, idle. Restart it in the same breath
  (`docker compose restart openai-tunnel`). Its `mcp session initialized` line then
  reports the running `server_version`, which is the cheapest proof of what the
  deployment actually serves.
- `docker compose build` tags the result `SERVERFS_IMAGE`, which in a production
  `.env` is a pinned release (`ghcr.io/ntlx/serverfs_mcp:0.7.0`). A bare build
  therefore shadows that release locally: the running container is unaffected,
  but the next `up -d` starts local code under a release tag. Always build under a
  scratch tag (`SERVERFS_IMAGE=serverfs-mcp:dev docker compose build`). Upgrading
  a deployment is `pull` + `up -d`, never `build`.

## Release

Use `gh` for GitHub operations (`gh api`, `gh run`, `gh release`) rather than curl or
the web UI. When GitHub exposes no supported CLI or API operation for what you need —
GHCR package visibility is the known case — use the UI and report the exception
explicitly instead of reaching for an undocumented endpoint.

Tags drive images: `main` publishes `:edge`; a `vX.Y.Z` tag publishes `X.Y.Z`, `X.Y`
and `latest`. `latest` comes only from a stable tag, and the image version comes from
the Git tag — never from a GitHub Release event. Workflows pin every Action to a full
commit SHA and authenticate with `GITHUB_TOKEN` alone; pull-request builds never log
in, never push, and never write the shared build cache. Production deployments pin
`SERVERFS_IMAGE` to an exact version; `:edge` and `latest` are for trying things out.

A published stable tag is immutable. Post-release documentation, website and repository
metadata fixes land on `main`; never move or recreate the stable tag to absorb them.
Current operational docs may be updated to reflect the released state, while historical
plans, acceptance records and audits preserve what was true at the time unless correcting
a factual error.

End-to-end acceptance through ChatGPT belongs to the maintainer: an agent's reach
ends at the container's MCP surface. Say so rather than implying it was verified.

## Language

Code, comments, docstrings, tests, commits, pull requests and this file are English.
Talk to the maintainer in Chinese.
