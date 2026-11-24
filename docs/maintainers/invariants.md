# Maintainer Invariants

These are durable contracts. Change them only when a demonstrated defect or an explicitly approved product change requires it, and update implementation, tests and release-facing documentation together.

## 1. Capability surface and authority

- ServerFS exposes narrow filesystem/Agent capabilities, never ambient host authority.
- The base filesystem surface has **11 tools**; the following are independent combinations, not a sequential expansion or a promise of parity across all attachments. Optional binary transfer expands it to **13**. Optional Agent delegation expands it to **21**. Enabling binary + Agent yields **23**. Optional Deterministic Repo Ops independently adds four tools: filesystem + Repo Ops = **15**, binary + Repo Ops = **17**, Agent + Repo Ops = **25**, and binary + Agent + Repo Ops = **27**.
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

- Core never fetches the temporary file URL directly; the isolated sidecar owns that path;
- it talks only to the private `serverfs-file-ingress` endpoint;
- the ingress sidecar has no workdir mounts, Tunnel/OpenAI credentials or published port; named persistent macOS and Windows disable this shared file-ingress path;
- accepted remote destinations remain narrowly constrained, DNS/IP validated and TLS verified on every redirect hop;
- never turn ingress into a generic URL fetcher or credential-bearing proxy;
- destination path never derives from `file_name`, and URL/file IDs are not logged;
- `upload_binary_file` accepts exactly one payload source: base64 bytes XOR ChatGPT `file` parameter.

The public file parameter schema remains explicit and must stay synchronized with tests.

## 5. Streamable HTTP and MCP protocol safety

Transport security is an invariant for every HTTP Core:

- DNS-rebinding protection stays enabled; provide no casual disable switch;
- production Host/Origin policy remains explicit and narrow for the deployed topology;
- legacy default-owner compatibility retains the Docker-internal OpenAI Tunnel authority; named HTTP explicitly admits its actual authority/Host values and keeps ingress independent;
- named Linux Compose selects an explicit publish-http or internal-only exposure overlay; local publication defaults to loopback and wider exposure is an operator decision; native named HTTP uses the exact loopback authority;
- Internet-facing MCP requires TLS and caller admission. Core supports one static API Key or OAuth resource verification, not both; no-auth Linux is limited to explicit isolation or validated external admission. Named native HTTP requires authentication. Connector credentials are not caller credentials; named OpenAI HTTP requires static Core bearer admission, not OAuth-only;
- internal-only network selection isolates Core publication; direct HTTP publication explicitly selects a non-internal network, whose egress must be assessed. Keep OAuth introspection private, authorization service state separate, and distinguish principal removal from token revocation;
- do not broaden Host/Origin policy to wildcard configuration merely to accommodate an unmeasured development topology;
- keep real Host/Origin regression tests, startup-wiring tests, and both modern/legacy MCP-client compatibility tests.

ServerFS formally targets stdio and Streamable HTTP. Do not add WebSocket or a new legacy SSE deployment surface merely because a client happens to support them.

MCP/JSON-RPC protocol stdout must stay protocol-clean. Logs/diagnostics go to their intended logging channel, never into a stdio protocol stream.

Agent-facing errors use stable short error codes/messages. Internal paths, contents, credentials, search queries and other sensitive payloads do not belong in normal structured logs.

## 6. Agent Bridge contract

The Agent Bridge is optional and provider-neutral. Supported native runtime names are **Codex, Claude and Qoder**; operator-configured ACP profiles are standard-protocol adapters, not new native runtimes. `fake` remains test-only. No automatic runtime installation or caller-defined executable/argv/env.

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
- native providers and ACP currently require an explicitly writable workdir and `workspace-write` so the shared writer lease is held;
- the Bridge does not convert that lease into a provider sandbox or permission system.

Provider-native configuration remains provider-owned. ServerFS selects the starting workdir/runtime and may pass an explicit request-scoped model when supported; it does not rewrite the user's provider authentication, native default model, sandbox, approval policy, MCP servers, skills/plugins or shell environment.

### Model selection

`model=None` means no ServerFS override. An explicit model ID is request-scoped, included in task/idempotency/manifest evidence and forwarded only through the provider-native API.

Do not add ServerFS provider-default model environment variables, automatic cross-provider fallback, fabricated static model catalogs or a cross-provider `effective_model` claim.

Model discovery is capability-based: Codex uses its runtime catalog, Qoder its current-account catalog; Claude and ACP report unsupported discovery. ACP model override, elicitation/client filesystem/terminal and live steering are not supported. Runtime capability does not imply attachment authorization.

### Task lifecycle

Agent execution is asynchronous and normalized behind the Bridge contract. Preserve:

- durable tasks/events/pending approvals/questions;
- explicit terminal states;
- bounded deadlines/interaction lifetimes and retention;
- conditional retry-safe submission via `idempotency_key` on compatible v1 paths without duplicating a retained provider turn; signed v2 Core grants reject both `idempotency_key` and `continue_from_task_id`, even when runtime continuation is advertised. Never blindly retry an uncertain v2 submission;
- optional opaque `correlation_id` as metadata only;
- bounded large-result spooling + `read_agent_task_result` rather than unbounded inline results; defaults: 7200s task, 1800s interaction, 4 active, 7d retention, 256 KiB inline, 8 MiB maximum stored result, 64 KiB chunks of normalized path-redacted text;
- provider-aware restart reconciliation; never blindly rerun an in-flight provider task after Bridge restart;
- terminal-first cancellation semantics and best-effort provider interrupt bounded to 10s; disconnect does not cancel and terminal status is not proof of provider stop;
- shared writer lease/recovery guard behavior when provider stop cannot be proven.

Agent tools talk to the Bridge RPC contract; the MCP package does not import provider SDKs/adapters.

### Human interaction

Approvals and questions remain provider-neutral but human-controlled:

- expose only decisions/permission IDs supplied by the provider contract;
- never auto-approve or auto-deny through Jev;
- late interaction answers are stale after terminal/expiry state;
- `approve_session` may echo only provider-supplied session-scoped suggestions and must not silently persist broader provider permission changes.

## 7. Deterministic Repo Ops contract

Repo Ops is an optional, per-workdir, default-off **non-Agent** host repository capability. Its purpose is to let MCP callers finish deterministic repository gates without turning those steps into Agent delegation or exposing a generic host executor.

Preserve these invariants together:

- Repo Ops and Agent delegation are independently authorized. A workdir may enable Repo Ops with Agent mode disabled, and Repo Ops must never auto-fallback to `submit_agent_task` or another provider task path.
- The public Repo Ops surface is exactly four narrow tools: `inspect_repo`, `run_repo_action`, `commit_repo_changes`, and `push_repo`. Do not add a generic repo command/shell/argv/env/cwd/executable/remote/ref/force passthrough.
- All caller paths pass the normal ServerFS path/deny policy before Bridge RPC. The host Bridge independently confines them again and must not return absolute host workdir paths.
- Named command actions are operator-owned fixed argv profiles accepting only a policy-valid path/basename. Built-in `release_tag` / `publish_npm` profiles implement a narrower npm/`package.json` release contract: they fix a GitHub repository in administrator policy and accept only a strictly validated `release_tag`; they cannot carry argv. Release requires a clean HEAD on main, a matching root `package.json` version, remote main equal to HEAD, conflict-safe exact tags, and the fixed `publish.yml` workflow. This does not make the generic Repo Ops surface npm-specific, and these built-ins must not be presented as a universal release abstraction for non-npm repositories. Every action requires a non-empty SHA-256 integrity map over repository-controlled execution inputs; a missing/symlinked/oversized/mismatched pin fails before process creation.
- Do not auto-update integrity pins in the same task that changed the pinned repository code. A pin update is an operator review decision.
- Every named action requires a read-write workdir and acquires the exclusive writer lease even when `mutates=false`. `mutates` is audit metadata, not a sandbox claim. Commit and push use the same lease/recovery-guard boundary.
- Commit refuses pre-existing staged state, stages only explicit requested paths, rejects external clean/process filters, disables hooks and signing on the deterministic path, and returns the resulting SHA without implicitly pushing.
- Push derives the current branch/upstream internally and supplies exactly one `HEAD:<upstream-branch>` refspec. The caller cannot choose remote/ref/force. Git is non-interactive; hooks, askpass, repository SSH-command overrides and unapproved custom remote helpers remain neutralized/denied.
- Repo Ops child environments are narrowly allowlisted; provider/API secrets are not forwarded wholesale. Runtime/output limits are mandatory. On Linux, timeout/output overflow/cancellation terminates the entire subprocess group and drains child pipes before releasing the writer lease.
- Repo Ops RPC/service code must not import or invoke Agent provider adapters, select models, create sessions/tasks, or claim provider authority.
- Repo Ops retains platform-native process containment and uses explicit per-Core grants; do not infer authorization from a shared owner.
- Release acceptance requires public MCP E2E through the four tools, including status/action/diff/commit/push as applicable, and explicit evidence that no Agent task was created. Private Bridge/internal checks are necessary but not a substitute for public MCP acceptance.

## 8. Jev is advisory only

Jev Preflight, Runtime Router, Model Advisor and Approval Advisor are opt-in advisors, not an Agent runtime, authorization system or safety authority.

- Without a configured Jev key, construct no Jev client and make no Jev request. Named Linux owner uses private `jev` JSON policy; `SERVERFS_JEV_API_KEY` is legacy renderer input. Do not infer native installer support from parser support.
- With a key, Jev failures remain fail-open with respect to an otherwise authorized explicit task.
- Runtime Router may recommend but never switch the requested runtime/tool automatically.
- Model Advisor ranks only the normalized candidates supplied to it and never populates/overrides `submit_agent_task.model`.
- Approval Advisor can advise only after the provider creates an approval request; it cannot approve/deny, grant permission IDs or bypass `respond_agent_approval` validation.
- Jev credentials remain outside the MCP container and must not appear in logs/RPC/task events. Preflight sends the original prompt to the external TypeSafe API; authorize that data transfer explicitly. Model/approval redaction is best-effort, not a comprehensive secret filter. The v0.26.0 candidate distinguishes approvals before redaction using a private task-bound keyed fingerprint, retains private request/decision audit state, and refuses remote approval of recognized outside-workdir writes. Never expose the private fingerprint or key through public events; advice remains non-authoritative.

Jev has independent proxy control without increasing its authority. Agent provider proxy is separate and requires a credentialless endpoint; Connector egress and registry pulls are independent.

## 9. Platform containment

Platform support is implemented behind explicit platform seams, not by weakening a security invariant to make another OS fit.

### Linux

Use Compose for Core and optional Connectors; direct HTTP and tunnel-only exposure are distinct overlay selections. Host Bridge remains an optional user systemd owner. Verify actual socket peer identity, private signed bindings and grants; do not synthesize identity or replace a denied signed attachment with a legacy transport. Preserve project isolation and non-root ownership. The v0.26.0 candidate supports 1–16 explicitly granted Agent and/or Repo Ops aliases in slots 01–16 (v0.25.0 binds one enabled alias in slot 01), distinct Core/Bridge IDs even for dedicated owner and up to 32 initial grants; no live grant append/rotation via initial provisioning.

### Windows

Windows 11 x64, local NTFS, current-user least-privilege Task Scheduler, windowless components, Job Objects and signed Named Pipe authorization are the native boundary. Only the selected component owner may install, start, stop or unregister its own task. No SCM/LocalSystem or administrator escalation. Do not identify or kill trees merely by process name or unverified PID. Preserve HANDLE-relative filesystem and reparse-point constraints. Named initial runtime/config/Connector helpers are create-only, not in-place upgrades or rotation; up to 16 initial Cores per shared owner, distinct grants and exact owner-policy subsets. Preserve failed attempt state.

### macOS

Apple Silicon, native arm64, macOS 27 and APFS. Preserve Darwin descriptor-relative containment, TCC, signed app/runtime and authenticated Unix IPC. Named components use user SMAppService/LaunchAgents, with explicit Login Items approval. No root service, hand-installed legacy plist or approval bypass. Preserve exact identity and component scope. Owned mode uses private IPC/manifest/policy checks, not shared v2 grant protocol; shared Core requires an independent existing owner and exact owner-bound Agent aliases (one in v0.25.0; 1–16 in the v0.26.0 candidate), no Repo Ops or named Connector staging. Native staging is create-only. Unsupported platforms fail closed pending measured support.

### Component identity

Every Core, Connector and Bridge has an immutable named identity. File-only requires no Bridge. Owned native pairs use the same instance ID; named Linux dedicated owner uses distinct IDs. Signed grant paths restrict alias/methods and task ownership; macOS owned uses private native policy/IPC. Shared attachments require explicit owner-issued grants. Shared Core cannot manage its owner; independent owners must not overlap writable roots. Caller authentication is independent of Connector credentials; revocation, wrong identity, replay and cross-Core access fail closed. Use native implementations rather than forcing private IPC parity.

## 10. Secrets and configuration

- Linux named Compose uses one private env per exact project selection; root `.env` is the compatibility default only. Native Core policy uses TOML plus private auth inputs; named Bridge owner configuration and grants are private, distinct from Core config. Never expose private configuration/state through workdir mounts.
- `.env.example` documents the public configuration surface without secrets.
- Provider-native secrets/shell-only variables remain outside the repository and Core containers; only the explicitly documented legacy renderer reads private env inputs for host-side config. Never edit protected manifests or installed snapshots to bypass integrity/create-only checks.
- Never print raw `docker compose config` output when it can expand secrets; query only the field needed.

## 11. Intentional product decisions and traps

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
- Retain exact Compose project, env, all exposure/auth/ingress/Bridge overlays and profiles for scoped operations; a base-only recreate can drop policy/mount selections even with unchanged env.
- Core recreation can strand an existing MCP session. Verify fresh authorized MCP; scope any necessary Connector restart separately, never assume old SSE replay. Stop ingress before Core when required by native guards.
- Build under a scratch image tag. Never let a local build shadow a published production tag named in `.env`.
