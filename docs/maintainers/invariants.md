# Maintainer Invariants

These are durable contracts. Change them only when a demonstrated defect or an explicitly approved product change requires it, and update implementation, tests and release-facing documentation together.

## 1. Capability surface and authority

- ServerFS exposes narrow filesystem/Agent capabilities, never ambient host authority.
- The base filesystem surface has **11 tools**. Optional binary transfer expands it to **13**. Optional Agent delegation expands it to **21**. Enabling both yields **23**.
- Read-only remains the default per workdir. Mutation is an explicit workdir capability.
- Do not add a generic shell/argv/env MCP tool or another general-purpose executor.
- Public MCP schemas are contracts. Structured results must expose matching `outputSchema`; mixed binary results keep both the resource block and structured metadata.

## 2. Workdir and path confinement

Every path-bearing channel — read, stat, list, find, search, resource and mutation — must apply the same policy.

- A `DenyPolicy` is built once per call and travels with `ResolvedPath`; channels consume that policy instead of implementing their own matcher.
- Hidden-path policy and credential deny policy are independent axes.
- Reserved internal names are non-configurable and denied everywhere. The current reserved names are `.serverfs-tmp-*` and `.serverfs-disabled`; new reserved names must be added centrally and covered by channel tests.
- Request-derived POSIX traversal is descriptor-based: open each component relative to an already-open directory FD with no-follow semantics, then `fstat` the opened object. Do not reintroduce `lstat(path)` followed by `open(path)` for request-derived paths.
- The configured workdir root is the trusted named anchor. Root opening belongs to the shared FD helpers; do not add another independent root-open path.
- Search/list/find must filter the complete workdir-relative result path, not only the requested search root.
- Symlinked directories are never followed by discovery/search channels.

On native Windows, preserve the equivalent NTFS handle-based containment and the documented revision semantics instead of pretending POSIX details apply there.

## 3. Mutation and revision contract

The narrow mutation surface remains:

- `create_text_file` — create only; target must not exist.
- `create_directory` — create only; target must not exist.
- `edit_text_file` — existing UTF-8 regular file, exact-match edit, revision guarded.
- `delete_file` — existing regular file, revision guarded.
- `delete_directory` — existing empty directory, revision guarded.
- `upload_binary_file` — create-only by default; `overwrite=true` is allowed only for an existing regular file and requires `expected_revision`.

There is no recursive mutation, unguarded overwrite, force replace, rename/move/copy surface or generic binary editor.

On POSIX, the mutation pipeline is authorization → policy resolution → root/parent FD walk → operation on the final name relative to the verified parent FD. Mutations serialize through the process-wide mutation lock; reads do not.

Publication must remain atomic and race-resistant:

- create publishes from a same-directory reserved temp file without overwriting an existing entry;
- edit validates all exact-match edits in memory, writes/copies metadata to a temp file, re-checks revision, then atomically replaces;
- delete re-checks revision and removes the verified final name while holding the verified parent/object context;
- failures before commit abort; a directory `fsync` failure after a visible commit is logged rather than falsely reported as an uncommitted mutation.

Revision tokens are optimistic concurrency evidence, not content hashes. Compute the token from the same committed object the caller will later stat. POSIX revision derivation includes size, mtime, ctime and link-count evidence. `read_text_file` verifies the object did not change during the read.

Windows revisions are explicitly **metadata-derived optimistic-concurrency tokens**. They are not cryptographic content identity and cannot guarantee compare-and-swap against arbitrary same-user external rewrites. Preserve the documented restricted-share and writer-lease protections without overstating that boundary.

## 4. Binary transfer and ChatGPT file ingress

`SERVERFS_MAX_BINARY_TRANSFER_BYTES` is the public global binary/file-ingress ceiling. Per-workdir limits may tighten publication. The legacy file-ingress-specific variable is compatibility fallback only when the unified setting is absent.

The ChatGPT file-parameter ingress remains an isolated security boundary:

- `serverfs-mcp` keeps no Internet egress for ingress fetching;
- it talks only to the private `serverfs-file-ingress` endpoint;
- the ingress sidecar has no workdir mounts, Tunnel/OpenAI credentials or published port;
- accepted remote destinations remain narrowly constrained, DNS/IP validated and TLS verified on every redirect hop;
- never turn ingress into a generic URL fetcher or credential-bearing proxy;
- destination path never derives from `file_name`, and URL/file IDs are not logged;
- `upload_binary_file` accepts exactly one payload source: base64 bytes XOR ChatGPT `file` parameter.

The public file parameter schema remains explicit and must stay synchronized with tests.

## 5. Streamable HTTP and MCP protocol safety

The v0.4 transport-security fix remains a security invariant for every Linux Streamable HTTP ingress, including the v0.15 generic MCP path:

- DNS-rebinding protection stays enabled; v0.15 deliberately provides no casual disable switch;
- production Host/Origin policy remains explicit and narrow for the deployed topology;
- the compatibility default remains the Docker-internal OpenAI Tunnel authority, while generic HTTP deployments explicitly add the Host values they actually serve;
- the base Compose shape keeps the MCP listener unpublished; the generic HTTP override binds loopback by default and wider publication is an explicit operator decision;
- Internet-facing generic MCP endpoints terminate TLS and authentication at an explicit upstream gateway boundary unless a later ServerFS release adds an equally narrow built-in resource-server verifier;
- adding generic MCP connectivity must not grant the main Linux MCP process general Internet egress;
- do not broaden Host/Origin policy to wildcard configuration merely to accommodate an unmeasured development topology;
- keep real Host/Origin regression tests, startup-wiring tests, and both modern/legacy MCP-client compatibility tests.

ServerFS formally targets stdio and Streamable HTTP. Do not add WebSocket or a new legacy SSE deployment surface merely because a client happens to support them.

MCP/JSON-RPC protocol stdout must stay protocol-clean. Logs/diagnostics go to their intended logging channel, never into a stdio protocol stream.

Agent-facing errors use stable short error codes/messages. Internal paths, contents, credentials, search queries and other sensitive payloads do not belong in normal structured logs.

## 6. Agent Bridge contract

The Agent Bridge is optional and provider-neutral. Supported native runtime names are **Codex, Claude and Qoder**; `fake` remains development/test-only.

### Native runtime admission gate

**Default posture: standard protocol first; provider-specific native integration is an exception.** A new native runtime may be implemented only when the design record demonstrates every gate below. Popularity, convenience, lower latency, an available CLI, JSON output, or a provider name alone are not sufficient justification.

1. **Official stable control surface.** The integration must use a provider-maintained, documented and versioned SDK/API/daemon protocol intended for programmatic Agent control. TTY scraping, terminal automation, private reverse-engineered protocols and unstable internal interfaces do not qualify.
2. **Material advantage over standards.** The plan must compare the native surface with ACP and any other applicable stable standard. Native integration is justified only when the standard cannot preserve one or more ServerFS-critical semantics and the native surface provides a material, durable improvement. If the standard path is sufficient, use the standard adapter.
3. **Faithful Bridge mapping.** Every capability claimed by `RuntimeInfo` must be backed by measured behavior and map conservatively onto the provider-neutral contract: opaque session identity/continuation, structured events, human approval/question interaction when claimed, cancellation, restart reconciliation, and model discovery/override when claimed. Unsupported capabilities stay explicitly unsupported; never fabricate a catalog, recovery guarantee, live steering or interaction behavior for API symmetry.
4. **No authority expansion.** Native integration must preserve provider-owned authentication, default model, sandbox/permission policy, MCP servers, skills/plugins and shell environment. It must not introduce a generic shell/argv/env MCP executor, silently persist broader permissions, auto-approve requests, widen workdir roots, or bypass ServerFS authorization, writer leases or recovery guards.
5. **Fail-closed stop and recovery evidence.** Session resumability is not proof that an old in-flight execution has stopped. Cancellation/restart handling must preserve terminal-first ServerFS semantics, bounded provider interruption and the recovery guard whenever provider stop cannot be proven. A native adapter may declare reattachment or resumability only from provider evidence demonstrated by tests/live acceptance.
6. **Dependency and platform isolation.** Provider dependencies remain lazy/optional so an absent runtime cannot break the Bridge core or other runtimes. Platform support is claimed only where the executable/SDK, process containment, IPC, cancellation and recovery semantics have been measured on that platform; unsupported combinations fail closed rather than inheriting another platform's claim.
7. **Maintenance value and evidence.** The capability gain must justify long-term provider-specific API drift, security review and Linux/macOS/Windows regression cost. Before source implementation begins, a versioned development plan must record the native-vs-standard comparison, capability delta, configuration boundary, platform matrix, deterministic tests and required live acceptance evidence.

Every proposed new native runtime must contain a clearly labeled **Native runtime admission record** in its versioned design plan with, at minimum: candidate/runtime version; official control surface and stability evidence; ACP/other-standard comparison; material capability delta; authority/security impact; cancellation/recovery/stop evidence; dependency isolation; platform/test matrix; maintenance rationale; and a final `Admission decision: PASS | FAIL`. An AI Agent may perform research and draft this record, but **must not self-approve it**. Source implementation of a new native adapter may begin only after the maintainer/user explicitly approves `PASS` after reviewing that record. Without that approval, the Agent must stop at research/planning or use the applicable standard adapter.

If any hard gate above fails, the runtime must use the applicable standard adapter (ACP when suitable) or remain unsupported. Existing Codex, Claude and Qoder adapters are the established native baseline; adding another native runtime does not get precedent merely because those three exist. Re-evaluate an existing native adapter only through an explicit versioned design decision; do not migrate or remove it opportunistically.

Authorization is explicit and fail-closed:

- the Bridge must be enabled;
- the selected runtime must be enabled;
- the workdir must allow that runtime/profile;
- native provider modes currently use `workspace-write` so the shared writer lease is held;
- the Bridge does not convert that lease into a provider sandbox or permission system.

Provider-native configuration remains provider-owned. ServerFS selects the starting workdir/runtime and may pass an explicit request-scoped model when supported; it does not rewrite the user's provider authentication, native default model, sandbox, approval policy, MCP servers, skills/plugins or shell environment.

### Model selection

`model=None` means no ServerFS override. An explicit model ID is request-scoped, included in task/idempotency/manifest evidence and forwarded only through the provider-native API.

Do not add ServerFS provider-default model environment variables, automatic cross-provider fallback, fabricated static model catalogs or a cross-provider `effective_model` claim.

Model discovery is capability-based: Codex and Qoder expose native catalogs through their supported APIs; Claude may return `unsupported` when no stable native account enumeration API is available.

### Task lifecycle

Agent execution is asynchronous and normalized behind the Bridge contract. Preserve:

- durable tasks/events/pending approvals/questions;
- explicit terminal states;
- bounded deadlines/interaction lifetimes and retention;
- retry-safe submission via `idempotency_key` without duplicating a provider turn;
- optional opaque `correlation_id` as metadata only;
- bounded large-result spooling + `read_agent_task_result` rather than unbounded inline results;
- provider-aware restart reconciliation; never blindly rerun an in-flight provider task after Bridge restart;
- terminal-first cancellation semantics and bounded best-effort provider interrupt;
- shared writer lease/recovery guard behavior when provider stop cannot be proven.

Agent tools talk to the Bridge RPC contract; the MCP package does not import provider SDKs/adapters.

### Human interaction

Approvals and questions remain provider-neutral but human-controlled:

- expose only decisions/permission IDs supplied by the provider contract;
- never auto-approve or auto-deny through Jev;
- late interaction answers are stale after terminal/expiry state;
- `approve_session` may echo only provider-supplied session-scoped suggestions and must not silently persist broader provider permission changes.

## 7. Jev is advisory only

Jev Preflight, Runtime Router, Model Advisor and Approval Advisor are opt-in advisors, not an Agent runtime, authorization system or safety authority.

- With no `SERVERFS_JEV_API_KEY`, construct no Jev client and make no Jev request.
- With a key, Jev failures remain fail-open with respect to an otherwise authorized explicit task.
- Runtime Router may recommend but never switch the requested runtime/tool automatically.
- Model Advisor ranks only the normalized candidates supplied to it and never populates/overrides `submit_agent_task.model`.
- Approval Advisor can advise only after the provider creates an approval request; it cannot approve/deny, grant permission IDs or bypass `respond_agent_approval` validation.
- Jev credentials remain outside the MCP container and must not appear in logs/RPC/task events.

v0.12 added independent proxy control for Jev without increasing its authority.

## 8. Platform containment

Platform support is implemented behind explicit platform seams, not by weakening a security invariant to make another OS fit.

### Linux

The established Linux deployment remains containerized ServerFS MCP with two supported ingress shapes: the existing outbound OpenAI Secure MCP Tunnel and the v0.15 opt-in generic Streamable HTTP publication override. The base Compose shape does not publish the MCP port. Optional host-side Agent Bridge remains behind the Linux deployment overlay. User-scoped Agent deployment is non-root; do not require system users, `/etc`/`/opt`/`/var/lib` writes or automatic login lingering.

Agent-enabled Linux deployment must use the measured peer identity and the same user/group contract documented by Phase E. Do not synthesize peer UID/GID as a substitute for kernel measurement. Base `compose.yml` remains Agent-unaware; the opt-in overlay owns the socket/lock mounts.

### Windows

Native Windows filesystem service and native Agent delegation are supported through the Windows-specific backend/product boundary. Do not route Windows work through Docker/WSL merely to reuse Linux code, and do not describe the Windows revision token as stronger than its accepted NTFS semantics.

Since v0.14.0, the normal Windows outer lifecycle is a **current-user Task Scheduler 2.0 task**. Preserve all of these properties together:

- no administrator/UAC requirement for install, runtime, maintenance or removal;
- current-user `InteractiveToken` identity and `LeastPrivilege` run level; never SCM/LocalSystem, HKLM service registration, stored passwords or another machine-wide owner;
- a logon trigger for session startup plus an indefinite one-minute time-trigger heartbeat; `MultipleInstancesPolicy=IgnoreNew` keeps exactly one Task action while allowing the OS to recreate a missing watchdog;
- Task Scheduler owns a windowless watchdog, the watchdog owns one runner in a kill-on-close Job Object and restarts it with bounded short backoff, and the runner owns/restarts one Tunnel generation at a time with its own bounded backoff;
- the native Tunnel ownership mutex prevents a managed tree and a direct/manual `serverfs tunnel` tree from coexisting for the same current-user lifecycle;
- install/update preserves enabled/disabled operator intent; a fresh install is registered disabled, `service start` enables and starts it, and `service stop` disables before ending it so the heartbeat cannot resurrect an intentional stop;
- v0.13.x manual-launcher migration uses the frozen process-identity hand-over path. Never identify/kill a legacy tree by name alone or by an unverified PID.

For v0.14.0+, `deployment/windows/start-native.ps1` is compatibility/emergency material rather than the intended persistent operator path. Public lifecycle operations are `serverfs service install/start/stop/restart/status/uninstall`.

### macOS

v0.13 supports Apple M-series only, native arm64, macOS 27 Golden Gate. Preserve the measured runtime gate and fail closed outside that boundary. Darwin uses POSIX-generic filesystem/Agent seams where proven, Darwin-specific peer/runtime primitives where required, the macOS application-data home, native arm64 Tunnel asset and private AF_UNIX file ingress. **Since v0.13.2, persistent lifecycle is app-associated `SMAppService` + per-user LaunchAgents for both Tunnel and Agent Bridge.** Normal operation must not recreate a hand-installed `~/Library/LaunchAgents` plist, root LaunchDaemon, shell-profile bootstrap or `nohup` persistence. Registration/approval state belongs to ServiceManagement/Login Items and user denial must be respected. The bundled launcher contains no secrets, reads only private 0600 lifecycle state, uses absolute executable/config paths, and preserves exactly-one managed Tunnel/Bridge semantics. Do not claim Intel, Rosetta, macOS 26/28 or a `darwin-amd64` asset without new measured support.

## 9. Secrets and configuration

- Repository-root `.env` is the deployment configuration source; do not reintroduce `.env.agent` or another competing deployment env layer.
- `.env.example` documents the public configuration surface without secrets.
- Provider-native secrets/shell-only variables remain outside the repository except where an explicitly documented integration uses the untracked root `.env` and renders only the minimum private host-side config.
- Never print raw `docker compose config` output when it can expand secrets; query only the field needed.

## 10. Intentional product decisions and traps

- Hidden-path, default-deny and extra-deny controls are intentional administrator-facing policy, not bugs. Global extra deny globs are a floor; per-workdir globs can only add restrictions.
- `WORKDIR_XX_READ_ONLY` intentionally drives both application authorization and mount intent. Application authorization remains fail-closed even if the filesystem mount is writable.
- `O_NOFOLLOW|O_DIRECTORY` may report a symlink as `ENOTDIR`; classify after the failed open without weakening the kernel refusal.
- `/proc/self/fd/N` is itself a symlink; do not feed it back into a no-follow path walk as though it were a normal component.
- Publication syscalls can change inode metadata; compute revisions after commit so a newly created/edited file can be used immediately without a false conflict.
- `os.scandir(fd)` does not close the caller-owned FD.
- Python text helpers may normalize CRLF; byte-fidelity tests must compare bytes.
- Diagnose localization in the order: source text → built HTML → raw live response → rendering. Do not rewrite content based only on a screenshot.
- A bare MCP `GET` can disturb a live Tunnel session; use the intended readiness path for operational probing.
- Build is not deploy. A running container keeps its old image until recreated.
- On Linux Agent deployments, recreating only the base Compose shape drops the Agent overlay even if Agent variables remain configured.
- Recreating ServerFS can strand an existing Tunnel session; deployment procedures must restart/re-establish the Tunnel as documented.
- Build under a scratch image tag. Never let a local build shadow a published production tag named in `.env`.
