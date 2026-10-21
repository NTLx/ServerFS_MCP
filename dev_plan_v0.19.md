# ServerFS v0.19.0 Development Plan

Status: **implemented release candidate; not tagged or released**. The current published stable release remains **v0.18.0**. v0.19.0 must not be described as released until an explicitly authorized release is published.

Planning date: 2026-10-21.

## 0. Decision summary

ServerFS v0.19.0 will add **one provider-neutral Agent Client Protocol (ACP) adapter** to the existing Agent Bridge. It will not add Antigravity-, Grok-, Cursor-, Copilot-, OpenCode- or other provider-specific adapters.

The durable architecture becomes:

```text
ServerFS Agent Bridge
        |
        +-- Native adapters
        |     +-- Codex
        |     +-- Claude
        |     +-- Qoder
        |
        +-- Standard-protocol adapters
              +-- ACP v1
                    +-- operator-configured ACP profile A
                    +-- operator-configured ACP profile B
                    +-- ...
```

The native baseline stays Codex, Claude and Qoder. A future provider-specific native adapter must first pass the repository-level **Native runtime admission gate** in `docs/maintainers/invariants.md`; otherwise ACP or another applicable stable standard is the required integration path.

v0.19.0 freezes these choices:

- **ACP stable protocol v1 is the release baseline.** ACP v2 is still a draft and is outside the release contract.
- Use the **official ACP Python SDK** rather than implementing a second JSON-RPC stack by hand. Pin a stable SDK release in `agent_bridge/uv.lock`; a prerelease SDK requires an explicit recorded justification.
- ServerFS is the **ACP Client**. An operator-installed ACP Agent is launched as a bounded child process and speaks ACP over stdio.
- One generic `AcpAdapter` implementation serves all ACP profiles. Provider-name conditionals such as `if grok` or `if antigravity` do not belong in the adapter.
- ACP runtime profiles are configured by the operator. ServerFS does **not** auto-install Agents, fetch executables from the ACP Registry, or treat Registry metadata as an execution trust root.
- v0.19.0 does not replace or migrate the existing native Codex/Claude/Qoder adapters.
- Generic implementation and Linux acceptance are developed first in the VPS `ServerFS` checkout. macOS- and Windows-specific changes are implemented/accepted in their native development environments after the generic contract is stable.

Official upstream references verified during planning:

- ACP protocol repository: `https://github.com/agentclientprotocol/agent-client-protocol`
- ACP Python SDK: `https://github.com/agentclientprotocol/python-sdk`
- ACP TCK: `https://github.com/agentclientprotocol/acp-tck`
- ACP Registry: `https://github.com/agentclientprotocol/registry`
- ACP v2 draft announcement: `https://github.com/agentclientprotocol/agent-client-protocol/blob/main/docs/announcements/acp-v2-draft.mdx`

These references are evidence inputs, not runtime dependencies except for the separately pinned official Python SDK.

## 1. Goal

v0.19.0 should let one ServerFS deployment expose multiple standards-based coding Agents without adding one long-lived provider adapter per Agent.

The user-facing goal is:

```text
operator installs/authenticates an ACP Agent
        |
        v
operator defines one bounded ACP profile
        |
        v
workdir explicitly allowlists acp-<profile>
        |
        v
existing ServerFS Agent MCP tools
        |
        v
existing provider-neutral task/lease/approval/result contract
        |
        v
AcpAdapter -> ACP v1 stdio -> selected Agent
```

Success means all of the following are true:

1. one implementation can run multiple independently configured ACP Agents;
2. no task caller can turn ACP configuration into a generic shell/argv/env executor;
3. ServerFS keeps its existing authorization, writer lease, recovery guard, idempotency, result spooling and human-control semantics;
4. ACP capabilities are advertised conservatively from measured protocol evidence, not inferred from provider reputation or Registry metadata;
5. native Codex/Claude/Qoder behavior remains unchanged;
6. Linux, macOS and Windows support is claimed only after native acceptance on each platform;
7. adding another compliant ACP Agent normally requires configuration plus acceptance evidence, not source code.

## 2. Non-goals

v0.19.0 explicitly does **not** include:

- ACP v2 support or a public ACP v2 compatibility promise;
- automatic Agent installation, package download, update or Registry-driven execution;
- a generic shell, arbitrary command MCP tool, task-supplied argv, task-supplied environment variables or arbitrary executable launch API;
- replacement of Codex/Claude/Qoder native adapters with ACP wrappers;
- ServerFS-managed provider account login, browser authentication or credential collection;
- ACP client filesystem methods as a second filesystem authority path;
- ACP client terminal methods as a second shell/execution authority path;
- automatically injecting the ServerFS MCP server back into the delegated ACP Agent;
- provider-specific ACP extensions that require provider-name branches in the generic adapter;
- live steering unless a later measured ACP contract makes it possible without weakening task semantics;
- pretending all optional ACP features are supported merely because they exist in the protocol;
- changing existing workdir confinement, native service lifecycle, Tunnel/Auth architecture or Jev authority.

A missing optional capability is preferable to an unsafe partial implementation.

## 3. Relationship to the native runtime admission gate

The repository now treats provider-specific native integration as an exception.

Before any future native adapter implementation, its versioned design record must establish:

- an official stable provider control surface;
- a material capability gain over ACP or another applicable stable standard;
- faithful mapping to ServerFS session/event/interaction/cancel/recovery semantics;
- no authority expansion;
- fail-closed provider-stop evidence;
- dependency/platform isolation;
- sufficient long-term maintenance value and live acceptance evidence.

If those gates are not all met, the integration belongs behind ACP or remains unsupported.

v0.19.0 itself is the practical enforcement mechanism for the “standard protocol first” policy: ACP Agents share one protocol adapter instead of accumulating provider-specific adapters.

## 4. Protocol baseline and dependency policy

### 4.1 ACP version

The release implementation targets **ACP wire protocol version 1**.

Initialization must negotiate exactly the supported stable version. A peer that cannot negotiate the supported stable version fails with a bounded ServerFS error; ServerFS must not silently fall back to an unimplemented draft or guess at compatibility.

ACP v2 is a future design input only. Keep ACP-specific code behind a narrow transport/protocol seam so a later version can be added without rewriting Bridge service logic, but do not add speculative v2 branches in v0.19.0.

### 4.2 SDK

Add the official `agent-client-protocol` Python package only to the **Agent Bridge** environment. Preserve the frozen two-environment boundary:

- `serverfs_mcp` must not import the ACP SDK;
- Bridge core import must remain usable when the optional ACP dependency/runtime is not enabled;
- ACP SDK import is lazy through the adapter module, following the existing provider dependency isolation pattern.

Select and lock a stable SDK release during implementation. Do not take a release candidate merely because it is newer.

### 4.3 TCK role

The official ACP TCK launches an Agent implementation and checks protocol behavior. Use it to distinguish “reference Agent violates ACP” from “ServerFS ACP client mapping is wrong”. It is useful evidence for reference Agents, but it is **not** a substitute for ServerFS client-side deterministic tests or live ServerFS E2E.

## 5. Runtime identity model

A generic adapter requires dynamic runtime identities without turning the MCP API into arbitrary execution.

### 5.1 Reserved native names

Keep these names reserved:

```text
codex
claude
qoder
fake   # development/test only
```

### 5.2 ACP profile IDs

Each configured ACP profile has an operator-defined `id` matching a narrow identifier grammar. The implementation target is:

```text
^[a-z][a-z0-9_-]{0,27}$
```

The public runtime ID is derived, never independently configured:

```text
acp-<profile-id>
```

Examples:

```text
acp-grok-build
acp-antigravity
acp-opencode
```

The full public runtime ID remains bounded to 32 characters. Profile IDs are unique and may not collide with reserved native runtime names or another derived runtime ID.

### 5.3 Public runtime argument

The current MCP `Literal["codex", "claude", "qoder"]` cannot represent operator-defined ACP profiles. v0.19.0 should change the public runtime argument to a **bounded string**, then authorize it strictly against the configured/allowlisted runtime registry.

This is not an open runtime namespace:

- unknown runtime -> fail closed;
- configured but not enabled runtime -> fail closed;
- enabled but not allowlisted on the selected workdir -> fail closed;
- caller cannot provide executable/argv/env through the runtime argument.

The same normalized runtime registry must be used by Linux `.env`, native TOML, MCP validation, Bridge policy, doctor and runtime listing so configuration cannot be accepted by one layer and rejected by another.

### 5.4 Execution profile

All v0.19 ACP runtimes require `workspace-write`, exactly because ServerFS does not control an ACP Agent's native sandbox strongly enough to promise a provider-enforced read-only execution mode. `review` must therefore be rejected for `acp-*` runtimes in MCP authorization, Bridge service authorization and configuration validation. This guarantees that every accepted ACP task acquires the existing writer lease and recovery guard before provider execution can mutate the workdir.

Do not infer a future ACP read-only mode from an Agent label, session mode or permission prompt. Supporting `review` later requires a separate design with enforceable provider evidence.

## 6. ACP profile configuration contract

An ACP profile describes **how the operator-authorized ACP Agent is launched**, not a task-time execution request.

ACP profile configuration is a **trusted administrative code-launch policy** with the same trust boundary as the local ServerFS service/deployment configuration. Only the current-user operator/maintainer who owns that configuration may create or change a profile; ServerFS exposes no MCP/RPC operation that edits profile command/argv. A profile change takes effect only through the normal validated config render/sync + Bridge lifecycle path. ServerFS validates the launch structure and confinement contract, but it does not pretend an operator-selected executable is intrinsically safe: pointing a profile at an interpreter or wrapper is an explicit local-operator trust decision, never authority delegated to a remote task caller.

Allowed profile fields should stay minimal:

```text
id
command
args
use_proxy
probe_timeout_seconds
shutdown_timeout_seconds
```

Exact timeout field names may be aligned with existing Bridge conventions during implementation, but no additional authority-bearing field should be added without a demonstrated need.

Hard configuration rules:

- `command` must resolve to an **absolute regular executable path** for ACP profiles;
- `args` is a bounded JSON/TOML string array fixed by operator configuration;
- no shell command string;
- no `shell=true`;
- no per-task command override;
- no task-supplied argv;
- no per-profile arbitrary environment map;
- no task-supplied environment variables;
- no profile-supplied cwd: the Bridge supplies the already-authorized ServerFS workdir/cwd;
- no credentials in tracked/native operator configuration;
- proxy routing uses the existing ServerFS Agent proxy seam only;
- provider authentication remains in provider-native current-user state or the already established private provider environment boundary where applicable.

Bound the number and size of profiles/args. Proposed implementation limits are at most 16 ACP profiles, at most 32 argv elements per profile and a bounded aggregate encoded profile size. Final constants should be selected beside tests, not left unbounded.

### 6.1 Linux `.env`

Linux keeps `.env` as the deployment source of truth. Dynamic ACP profiles should use one strict non-secret structured value rather than inventing dozens of numbered environment variables:

```text
SERVERFS_ACP_PROFILES_JSON='[
  {"id":"grok-build","command":"/absolute/path/to/grok","args":["agent","stdio"],"use_proxy":false}
]'
```

The actual example should be compact one-line `.env` syntax. The Linux renderer must:

- parse JSON, never evaluate shell;
- reject unknown keys/types/duplicates;
- enforce path/argv/profile bounds;
- emit normalized private Bridge JSON;
- never copy credentials into the generated document.

An empty/missing value means no ACP profiles and preserves v0.18 behavior.

### 6.2 Windows/macOS native TOML

Native configuration should use structured repeated tables rather than JSON-in-TOML:

```toml
[[agent.acp_profiles]]
id = "grok-build"
command = "C:\\absolute\\path\\to\\grok.exe"
args = ["agent", "stdio"]
use_proxy = false
```

macOS uses the corresponding absolute POSIX executable path.

`serverfs.toml` remains the operator policy source; generated private Bridge state remains derived and non-secret.

## 7. ACP process and transport model

### 7.1 One child per active ServerFS task

For v0.19.0 use **one ACP child process per active ServerFS task/turn**. Do not add a daemon pool or shared multiplexed ACP process.

Benefits:

- process lifetime maps directly to ServerFS task lifetime;
- cancellation/containment evidence is simpler;
- stdout belongs to exactly one JSON-RPC connection;
- provider failure cannot corrupt another task's transport;
- workdir writer lease ownership remains obvious;
- no new scheduler/pool state is required.

This is intentionally simpler even if a future provider could be marginally faster with a shared daemon.

### 7.2 Launch

The adapter launches the preconfigured absolute executable directly without a shell and connects the official ACP SDK to child stdin/stdout.

Protocol stdout must stay protocol-clean. Agent stderr may be captured only through bounded diagnostics that preserve existing secret/path rules; raw stderr is not copied into task events or normal logs.

### 7.3 New task

A new task follows:

```text
acquire ServerFS writer lease + recovery guard
        |
spawn configured ACP child in authorized cwd
        |
ACP initialize (v1 negotiation)
        |
session/new(cwd=<authorized absolute cwd>, mcpServers=[])
        |
persist opaque ACP session ID immediately
        |
session/prompt
        |
normalize session updates / permissions
        |
receive prompt completion
        |
close/terminate child with stop evidence
        |
ServerFS terminal result + lease/guard cleanup
```

For the **normal success path**, `session/prompt` completion is only logical turn completion, not provider-stop evidence. Before `AcpAdapter.run_task()` / `continue_task()` returns a successful `AdapterResult`, the adapter must close the task-owned ACP transport, perform bounded graceful child shutdown/termination as required, and prove the task-owned containment set has stopped according to the platform seam. If that proof cannot be obtained, the adapter must not report ordinary success merely because it has a final text result; it must surface a bounded provider-stop failure/unknown state so the existing Bridge reconciliation path keeps the recovery guard fail-closed.

Do not expose additional directories. `mcpServers=[]` is deliberate in v0.19.0 so the delegated Agent does not receive a second ServerFS/control-plane path implicitly.

### 7.4 Continuation

`continue_from_task_id` keeps the existing ServerFS rules: same runtime, same workdir, terminal source task and persisted opaque native session ID.

On a new ACP child:

1. initialize v1;
2. prefer the stable session continuation method actually advertised/supported by that Agent;
3. if only a history-loading path is supported, perform it without replaying loaded history into the new ServerFS task event stream as if it were newly generated output;
4. persist/confirm the resumed opaque session ID;
5. submit the new prompt.

Capability absence returns an explicit continuation-not-supported error; it must not silently create a fresh unrelated session.

## 8. Capability model

ACP feature support varies by Agent. ServerFS must measure rather than assume.

### 8.1 No process start from ordinary runtime listing

`list_agent_runtimes` remains read-only and **must not launch an ACP Agent** merely to discover capabilities.

For a configured ACP profile, `probe()` may prove only local launch prerequisites without inference-heavy commands:

- configured executable path exists;
- path is the expected regular executable form;
- current user can execute it where the platform provides that evidence.

It must not invent a version string or ACP capability set.

### 8.2 Capability snapshot

After a real successful ACP initialization that was already required for a task, persist a small private capability snapshot keyed by the ACP profile configuration fingerprint. Use the existing Bridge SQLite state boundary with an additive `runtime_capabilities`-style table rather than inventing a second state file or deriving capabilities by scanning retained task history. The migration must be additive and old databases must remain readable.

The snapshot may contain only bounded protocol metadata needed for later reporting, for example:

```text
protocol_version
agent_name/version when ACP provides them
session continuation capability
permission capability actually observed/advertised
model/config capability metadata that ServerFS understands
measured_at
profile_config_fingerprint
```

A profile configuration change invalidates the snapshot.

Before a snapshot exists, optional `RuntimeInfo` booleans are conservative `false`/`unsupported`. Add a backward-compatible status field such as `capability_state = "unmeasured" | "measured"` and optional protocol metadata rather than pretending `false` means permanently unsupported.

Native Codex/Claude/Qoder output must remain backward compatible.

### 8.3 v0.19 capability commitments

Required for ACP release support:

- new session;
- prompt execution;
- normalized streaming/session updates sufficient to produce a final response;
- permission brokerage;
- cancellation;
- opaque session persistence and continuation when the peer exposes a supported continuation path;
- bounded error/disconnect handling.

Conservative defaults:

- `live_steer=false`;
- in-flight Bridge restart recovery is never stronger than `session-resume` unless future evidence proves real process reattachment;
- model discovery/override is unsupported unless Phase 7 below proves a stable semantic mapping;
- interactive question/elicitation is not advertised in the initial v0.19 scope; see §11.

## 9. Permission brokerage

ACP permission options cannot be safely forced into ServerFS's current four semantic decisions because ACP Agents may offer persistent choices such as “allow always” or “reject always”. ServerFS must not coerce a provider-persistent option into `approve_session` or ordinary `deny`.

### 9.1 Additive provider-neutral option path

Extend the pending approval payload with a bounded list of exact provider response options, for example:

```json
{
  "response_options": [
    {"option_id":"...","label":"...","kind":"allow_once"},
    {"option_id":"...","label":"...","kind":"allow_always"},
    {"option_id":"...","label":"...","kind":"reject_once"}
  ]
}
```

Naming can be finalized during implementation, but the semantic requirements are fixed:

- option IDs are opaque;
- only options supplied by the active ACP request may be selected;
- labels/kinds are bounded and normalized;
- raw ACP objects are not published as the stable ServerFS schema.

Extend `respond_agent_approval` additively with an optional provider option selector, e.g. `option_id`.

Compatibility rule:

- native Codex/Claude/Qoder keep using the existing `decision` path;
- ACP uses the exact offered `option_id` path;
- a request may require one path or the other, never ambiguous dual selection;
- ServerFS validates membership before returning the selection to ACP.

`cancel_task` remains a ServerFS task action rather than a fake ACP permission option. When selected, ServerFS resolves/cancels the pending ACP interaction according to the protocol, marks the ServerFS task terminal first, then interrupts the provider.

### 9.2 No automatic authority

Jev Approval Advisor may inspect normalized choices and recommend at most an actually offered option, but remains advisory. It never selects an ACP option, auto-approves, changes permission persistence or mutates provider policy.

## 10. Client authority advertised to ACP

ServerFS should advertise the **smallest ACP Client capability set** needed for the release.

v0.19.0 must not advertise:

- client filesystem read/write methods;
- client terminal execution methods;
- URL elicitation;
- terminal-based authentication;
- additional-directory authority;
- arbitrary provider extension methods.

Rationale: the delegated Agent already operates in the selected workdir through its own native execution model while ServerFS holds the writer lease. Adding ACP client fs/terminal handlers would create a second filesystem/execution channel that bypasses ServerFS's existing product boundary and is not needed to achieve generic Agent delegation.

Unknown/custom Agent-to-Client methods are rejected or ignored according to ACP rules; they are never interpreted as shell/filesystem instructions.

## 11. Authentication and elicitation

### 11.1 Authentication

Provider authentication remains provider-owned.

v0.19.0 assumes the ACP Agent has been authenticated by the current user through its own supported mechanism before ServerFS runs a task.

ServerFS will not:

- collect provider credentials;
- store provider tokens in `serverfs.toml`/`.env`/task state;
- emulate an interactive terminal login;
- open a browser on behalf of an MCP caller;
- auto-run Registry-provided authentication commands.

If the peer reports authentication is required, normalize that into a stable bounded ServerFS error such as `AGENT_AUTH_REQUIRED`, with no secret data. The operator authenticates the provider through its native interface and retries.

### 11.2 Elicitation/questions

ACP supports richer elicitation forms than ServerFS's current option/text question model. A partial mapping would create false validation guarantees.

Therefore initial v0.19.0 release scope is deliberately:

```text
ACP interactive_question = false
no elicitation capability advertised
```

A compliant Agent must not rely on a client capability that the client did not advertise.

A later release may add ACP form elicitation only after ServerFS has a complete bounded validator for the stable form schema, typed responses and constraint handling. URL elicitation remains a separate security/design decision and must not be added as a side effect of form support.

This is a deliberate quality boundary, not a permanent rejection of ACP questions.

## 12. Model discovery and request-scoped model override

ServerFS already has a strict rule: `model=None` means no override, and an explicit model must map through a stable provider-native interface without silent fallback.

ACP session configuration can expose model-like selectors, but v0.19.0 must not identify a model selector from labels or provider-specific IDs heuristically.

Implementation Phase 7 performs a narrow standards spike:

1. inspect the stable v1 SDK/schema actually pinned by v0.19.0;
2. determine whether there is a stable semantic category that unambiguously identifies a model selector;
3. verify behavior against at least two materially different reference ACP Agents;
4. verify continuation preserves/changes model exactly as documented by the peer;
5. verify a rejected model does not silently fall back.

Only if all five pass:

- map ACP model selector options into `runtime.models`;
- forward explicit ServerFS `model` using the standard ACP config option operation;
- persist the exact requested value under existing manifest/idempotency semantics.

If the mapping is not sufficiently stable, v0.19.0 ships ACP with:

```text
model_discovery = unsupported
model_override = false
```

and rejects a non-null model for ACP rather than guessing.

This optional spike cannot delay or weaken the core ACP lifecycle.

## 13. Event and result normalization

The generic adapter must translate ACP protocol messages into the existing provider-neutral Bridge event vocabulary. Do not expose raw ACP JSON as a public ServerFS API.

Required rules:

- assistant text -> existing normalized Agent message events/final response;
- tool/progress/plan/thought information -> existing bounded generic event types where a stable mapping exists;
- unknown optional update -> ignore safely or emit a bounded generic diagnostic marker without raw sensitive payload;
- ACP `_meta` and provider extensions are not automatically persisted;
- absolute host paths go through existing workdir redaction before public events/results;
- event count/size limits remain enforced;
- final response still uses inline/spool threshold, SHA-256 and bounded retrieval;
- stderr is diagnostics, never final Agent content by default;
- protocol stdout contamination is a protocol failure, not text to display as an Agent answer.

Add mapping tests using deterministic ACP fixtures so event vocabulary cannot drift accidentally.

## 14. Cancellation, child containment and recovery

This is a release-critical safety boundary.

### 14.1 Terminal-first cancellation

Preserve the current ServerFS sequence:

1. persist the ServerFS task as terminal/cancelled;
2. stale/resolve any pending interaction safely;
3. send ACP `session/cancel` where a live session exists;
4. wait only for a bounded grace period;
5. close transport and terminate the task-owned child/process tree when needed;
6. release the writer lease/recovery guard only when provider stop is proved by the applicable platform containment evidence.

A successful protocol cancel request alone does **not** prove all provider/tool processes have stopped.

Normal completion and cancellation use different protocol paths but the same safety invariant:

- **normal completion:** final prompt/result + verified task-owned child/process-tree stop is required before the adapter returns success;
- **cancellation/timeout/shutdown:** ACP cancel is advisory/control-plane input, followed by bounded process containment cleanup; if stop cannot be proved, provider reconciliation remains unknown and the recovery guard stays;
- no success/cancel response from ACP, by itself, authorizes guard removal.

### 14.2 Bridge restart

The Bridge cannot assume it can reattach to a pre-restart stdio child.

On restart:

- a persisted ACP session ID proves only logical session resumability;
- it does not prove the old ACP process is alive or dead;
- existing recovery guard remains fail-closed while stop state is unknown;
- never blindly rerun the interrupted prompt;
- once the old execution is independently proved stopped, a future ServerFS continuation may create a new ACP child and resume/load the persisted logical session according to peer capability.

ACP runtime `in_flight_recovery` must therefore remain conservative, normally `session-resume`, not `reattach`.

## 15. Platform boundaries

The ACP protocol implementation is generic; **process containment and deployment acceptance are platform-specific**.

### 15.1 Linux — primary generic development environment

Primary development occurs in the VPS `ServerFS` checkout.

Preserve:

- user-scoped non-root Agent Bridge;
- AF_UNIX / measured peer identity boundary;
- `flock` writer lease;
- Compose Agent overlay separation;
- private 0600 Bridge state/config;
- existing credentialless Agent proxy injection semantics;
- no automatic login lingering/system-user `/etc`/`/opt` redesign.

ACP child process containment should use a task-owned POSIX process group/session or an equivalently narrow existing primitive so cancellation can terminate descendants without scanning/killing unrelated processes by name.

Linux acceptance must complete before declaring the generic adapter contract stable enough to port.

### 15.2 macOS

After generic/Linux convergence, synchronize through Git and implement only the macOS-specific seams in the macOS development checkout.

Preserve:

- current supported macOS/architecture gate;
- app-associated `SMAppService` lifecycle;
- current-user execution;
- Darwin peer identity and POSIX writer-lease semantics;
- no root LaunchDaemon or hand-installed legacy plist;
- task-owned child/process-group cancellation evidence.

Linux success does not imply macOS Agent executable compatibility. At least one real ACP Agent must pass native macOS smoke before macOS ACP support is claimed.

### 15.3 Windows

After generic/Linux convergence, synchronize through Git and implement only Windows-specific seams in the Windows development checkout.

Preserve:

- native Windows, no Docker/WSL substitution;
- current-user, non-elevated lifecycle;
- Named Pipe/SID peer identity;
- `LockFileEx` writer lease;
- Task Scheduler/watchdog/runner ownership model;
- Job Object containment and stop evidence;
- no SCM/LocalSystem/admin requirement;
- no shell-based command-line reconstruction.

The ACP child must be a normal descendant of the already contained Bridge process so it inherits the existing supervisor-owned kill-on-close Job Object. Do not create a second ACP-specific Job Object and do not request job breakaway. The existing supervisor/lifecycle proof remains the restart containment boundary; ACP-specific code only owns the task-level child handle/transport inside that tree.

Windows tests must prove quoting/argv fidelity for direct process creation, inherited Job Object containment, process-tree termination for an ACP child that spawns descendants, and that a Bridge/supervisor teardown cannot leave the ACP child outside the managed tree.

## 16. Expected implementation touchpoints

The current checkout audit identified these likely touchpoints. This is a scope map, not permission for unrelated refactoring.

### 16.1 Agent Bridge

Likely additions/changes:

```text
agent_bridge/pyproject.toml
agent_bridge/uv.lock
agent_bridge/src/serverfs_agent_bridge/adapters/acp.py        # new
agent_bridge/src/serverfs_agent_bridge/adapters/__init__.py
agent_bridge/src/serverfs_agent_bridge/adapters/base.py       # only if an additive generic seam is required
agent_bridge/src/serverfs_agent_bridge/models.py
agent_bridge/src/serverfs_agent_bridge/config.py
agent_bridge/src/serverfs_agent_bridge/main.py
agent_bridge/src/serverfs_agent_bridge/service.py
agent_bridge/src/serverfs_agent_bridge/protocol.py            # additive ACP approval option RPC field
agent_bridge/src/serverfs_agent_bridge/store.py               # additive runtime capability snapshot state
agent_bridge/src/serverfs_agent_bridge/preflight.py           # runtime/approval advisory normalization
agent_bridge/src/serverfs_agent_bridge/platform_seams.py      # only if shared child-process seams require an additive hook
agent_bridge/src/serverfs_agent_bridge/render_config.py
agent_bridge/src/serverfs_agent_bridge/sync_config.py
agent_bridge/config.example.json
```

Prefer a small ACP transport/session helper module only if `acp.py` would otherwise mix protocol I/O, process containment and mapping into one untestable file. Do not build an internal framework merely for symmetry.

### 16.2 MCP/product layer

Likely changes:

```text
src/serverfs_mcp/agent_tools.py
src/serverfs_mcp/workdirs.py
src/serverfs_mcp/config.py
src/serverfs_mcp/native_config.py
src/serverfs_mcp/agent_lifecycle.py
src/serverfs_mcp/agent_doctor.py
src/serverfs_mcp/agent_client.py           # only if additive approval RPC parameters need client validation
src/serverfs_mcp/models.py                 # additive public approval schema only where required
src/serverfs_mcp/windows_job.py            # Windows containment evidence; change only if inheritance tests expose a gap
src/serverfs_mcp/supervisor.py             # same constraint: no redesign unless native acceptance proves it necessary
```

The MCP package still talks only to Bridge RPC; it must not import the ACP SDK.

### 16.3 Linux deployment

Likely changes:

```text
.env.example
deployment/agent-bridge/render_config.py
deployment/agent-bridge/provider.env.example
deployment/agent-bridge/README.md
compose.agent.yml                           # only if process/runtime wiring actually requires it
```

Do not modify base `compose.yml` merely because ACP exists; base filesystem-only behavior remains unchanged.

### 16.4 Documentation

At release convergence, update only current-state surfaces that actually changed:

```text
AGENTS.md
docs/maintainers/README.md
docs/maintainers/invariants.md
agent_bridge/README.md
README.md
README_EN.md
site/src/content/docs/docs/agent-bridge.md
site/src/content/docs/zh-cn/docs/agent-bridge.md
configuration/deployment docs as applicable
skills/serverfs-agent-bridge/* where the operational workflow needs ACP knowledge
```

Historical plans/evidence are not mechanically rewritten.

## 17. Implementation phases and current evidence

Implementation commits in this checkout:

- `1099062 feat(agent): add generic ACP runtime support` — generic ACP v1 adapter, profile configuration/rendering, authorization, tests and Linux integration.
- `b57a024 fix(agent): sync ACP profiles in native policy` — native config synchronization and focused tests.
- `be17a94 fix(windows): contain ACP process trees safely` — Windows ACP process containment; fixes launch order to `CREATE_SUSPENDED` → Job assignment → resume and adds task-level process-tree stop.

Measured acceptance and current blockers:

- Linux two-real-Agent core smoke is **not met**. Grok Build `0.2.114` completed ACP initialize/load, but `new_session` returned `auth_required`. OpenCode `1.18.19` timed out during initialize. Neither has passed the required core live smoke.
- macOS baseline code and native tests passed at `b57a024`, but no ACP-capable local Agent was available. Final `be17a94` synchronization is currently blocked because the Tunnel is disconnected. No macOS ACP platform support is accepted.
- Windows containment and argv tests passed. `be17a94` fixes suspended creation → Job assignment → resume and task-level process-tree stop. This is implementation evidence only; real ACP Agent smoke and remaining native acceptance are outstanding, so Windows ACP platform support is not accepted.
- ACP model discovery and override are unsupported in this candidate. ACP elicitation, Client filesystem, and Client terminal methods are not advertised. Runtime capabilities are conservative and may be claimed only from measured peer behavior.
- Final Linux RC regression at `be17a94` plus 0.19.0 metadata/docs: Agent Bridge **413 passed / 14 skipped**; root **1664 passed / 317 skipped**; Agent Skills **29 passed**; root/Bridge Ruff and format checks passed; Plugin `v0.19.0` validation passed; **5/5** Compose render gates passed; main Docker image built successfully; site production build generated **35 pages**; root and Agent Bridge `0.19.0` wheels built successfully; `git diff --check` passed. Cargo verification is delegated to Windows because the VPS has no `cargo`.

v0.19.0 remains an implemented release candidate with platform acceptance gates outstanding. Do not infer support from implementation or unit tests alone.

### Planned phase record (scope and gates)

### Phase 0 — governance and plan

Deliverables:

- repository-wide standard-protocol-first instruction in `AGENTS.md`;
- detailed Native runtime admission gate in `docs/maintainers/invariants.md`;
- maintainer index records v0.19 as an implemented release candidate;
- this development plan.

Gate:

- an unfamiliar Agent reading repository instructions can determine that it must not add a provider-specific native adapter without passing the gate;
- any future native proposal must contain the required **Native runtime admission record** and the Agent must stop before source implementation unless the maintainer explicitly records `Admission decision: PASS` after review; an Agent's own research conclusion is not approval;
- current implementation status and measured acceptance evidence are recorded above.

### Phase 1 — normalized runtime registry and configuration

Goal: permit dynamic ACP runtime IDs while keeping authorization closed.

Implement:

- ACP profile dataclass/schema;
- strict ID/path/argv/bounds;
- public runtime string validation against configured runtime registry;
- workdir allowlist parity across Bridge/MCP/Linux/native config;
- lazy ACP dependency registration;
- Linux `SERVERFS_ACP_PROFILES_JSON` renderer;
- native `[[agent.acp_profiles]]` parser/renderer shape;
- runtime listing for unmeasured ACP profiles.

Tests:

- unknown runtime rejected everywhere;
- duplicate/collision profile rejected;
- bad path/argv/type/size rejected;
- missing ACP config preserves v0.18 behavior;
- existing native config round-trips unchanged;
- no task-supplied launch metadata exists in MCP schema/RPC.

### Phase 2 — deterministic ACP fixture and transport

Build a tiny deterministic test ACP Agent using the official SDK or the smallest standards-faithful fixture supported by the pinned SDK.

Cover:

- initialize v1;
- protocol version mismatch;
- stdout framing hygiene;
- stderr isolation;
- session/new;
- prompt/result;
- malformed response;
- abrupt disconnect;
- bounded timeout;
- child exit and cleanup.

No real model/API should be required for this gate.

### Phase 3 — core `AcpAdapter`

Implement one generic adapter:

- profile-bound runtime name;
- launch in authorized cwd;
- initialize;
- new session;
- prompt;
- immediate opaque session-ID persistence;
- bounded event normalization;
- final response;
- continuation when peer capability permits;
- conservative `RuntimeInfo`.

Gate:

- existing `BridgeService` task/idempotency/spool/event tests pass with the ACP adapter;
- no provider-name branch in generic ACP protocol logic.

### Phase 4 — exact permission option brokerage

Implement the additive option-ID path described in §9.

Gate:

- native decision-based approval tests unchanged;
- ACP exact option ID round-trip proven;
- forged/unoffered option refused;
- persistent ACP choices are not misrepresented as `approve_session`;
- Jev remains advisory only.

### Phase 5 — cancellation and recovery safety

Implement bounded ACP cancel plus task-owned process-tree containment.

Gate:

- terminal-first cancellation proven;
- cancel during prompt and cancel during permission wait proven;
- child and test descendant termination proven;
- uncertain stop leaves recovery guard;
- no blind rerun after simulated Bridge restart;
- logical session resumability is reported separately from in-flight process recovery.

### Phase 6 — capability snapshot and continuation matrix

Implement/invalidate bounded capability snapshots from real handshakes.

Gate:

- runtime listing never launches an Agent;
- first successful task updates snapshot;
- profile config change invalidates snapshot;
- continuation uses only advertised/measured ACP behavior;
- unsupported continuation fails explicitly rather than starting a new session.

### Phase 7 — optional model selector spike

Perform §12 tests.

Decision gate:

- if stable semantic mapping passes -> implement model list/override;
- if not -> freeze ACP `model_discovery=unsupported`, `model_override=false` for v0.19.0.

This phase may produce a documented negative result; it must not invent a heuristic.

### Phase 8 — Linux deployment + real ACP reference acceptance

Integrate Linux renderer/doctor/docs and test **at least two** materially different ACP implementations when they are already installed/authenticated or can be installed explicitly by the maintainer. A third implementation is a stretch/diagnostic target, not a release blocker.

Preferred reference set:

- Grok Build — provider-official ACP-first implementation;
- Antigravity — major multi-platform ACP implementation;
- OpenCode or another independently implemented/open-source ACP Agent — preferred third implementation when practical.

Do not silently install these tools as part of tests.

For each tested Agent record:

- exact version;
- executable/profile command;
- ACP TCK result where practical;
- ServerFS probe/runtime capabilities;
- new session;
- real task in disposable subdirectory;
- permission round-trip if Agent can deterministically trigger one;
- continuation behavior;
- cancellation;
- cleanup evidence;
- any unsupported capability.

At least two different implementations must pass core ServerFS live smoke before generic ACP is considered release-ready on Linux.

### Phase 9 — macOS native integration/acceptance

Synchronize the generic implementation to the macOS ServerFS checkout via the repository workflow.

Implement only required native renderer/process seam changes, then run:

- focused parser/renderer tests;
- Bridge unit/full suite;
- native lifecycle/service status gates;
- real ACP Agent smoke;
- cancellation/descendant cleanup;
- continuation;
- existing Codex/Claude/Qoder regression smoke where required by affected seams.

Do not infer macOS support from Linux results.

### Phase 10 — Windows native integration/acceptance

Synchronize the generic implementation to the Windows ServerFS checkout.

Implement only required native renderer/process seam changes, then run:

- focused native config/renderer tests;
- Bridge/root unit/full suite;
- Named Pipe/SID/lease regression;
- current-user lifecycle regression;
- Job Object ACP child containment test;
- argv/path-with-spaces test;
- real ACP Agent smoke;
- continuation/cancel;
- existing native runtime regressions.

Do not use WSL/Docker as acceptance evidence.

### Phase 11 — release convergence

Only after all required platform gates:

- converge package/version metadata to `0.19.0`;
- update current-state docs/site/skills;
- preserve historical evidence;
- run residual stale-string scan;
- run root + Bridge lint/format/full tests;
- run relevant Compose render/build gates;
- run native release metadata gates;
- run site production build and visual verification only if layout/CSS changed;
- prepare release evidence.

Tag/release publication still requires the user's explicit instruction and the repository's existing hosted-CI rule.

## 18. Deterministic test matrix

At minimum add coverage for these cases.

### Configuration/authorization

- no ACP profile -> no behavior change;
- valid ACP profile -> derived `acp-*` runtime;
- duplicate ID/collision -> fail;
- relative executable -> fail;
- shell/metacharacters are ordinary argv text, never evaluated;
- malformed JSON/TOML -> fail;
- unknown profile key -> fail;
- workdir allowlists unknown ACP runtime -> fail;
- configured ACP runtime not allowlisted -> submit refused;
- ACP runtime + `review` profile -> fail;
- read-only workdir + workspace-write ACP -> fail;
- caller cannot override command/args/env/cwd beyond validated relative starting path.

### Protocol/session

- v1 initialize success;
- incompatible protocol -> fail;
- session ID stored before long-running prompt completes;
- new task success;
- continuation success when supported;
- unsupported continuation explicit failure;
- history/load replay does not create fake new output events;
- malformed JSON-RPC/protocol stdout contamination -> bounded protocol failure;
- peer exit -> task failure + safe cleanup.

### Permission

- exact option list stored;
- exact offered option selected;
- forged option rejected;
- cancel task while permission pending;
- late response stale;
- permission timeout interrupts task;
- no implicit mapping of allow-always to approve-session.

### Lifecycle/reliability

- idempotency retry creates exactly one ACP turn;
- conflicting idempotency key rejected;
- task deadline;
- interaction deadline;
- cancel before/after session creation;
- descendant process stop;
- unknown stop -> guard retained;
- proven stop -> guard may clear through existing rules;
- restart does not blindly rerun prompt;
- second writer blocked while guard/lease is active.

### Events/results

- text streaming normalized;
- unknown update safe;
- absolute path redaction;
- event size/count bounds;
- large result spooling/retrieval/SHA-256;
- stderr not leaked as answer;
- raw `_meta` not exposed automatically.

### Native non-regression

- Codex capabilities/models/session/steer/approval/question/cancel unchanged;
- Claude session/approval/question/cancel unchanged;
- Qoder model/session/approval/question/cancel unchanged;
- Jev recommendations remain advisory;
- Bridge unavailable/disabled behavior unchanged.

## 19. Live acceptance contract

A runtime/profile is not documented as supported solely because it appears in the ACP Registry.

Evidence levels:

```text
configured       = syntax accepted, executable locally present
protocol-seen    = successful ACP v1 initialize recorded
core-accepted    = real ServerFS task + result + cleanup pass
interaction      = real permission path pass where applicable
continuation     = measured resume/load pass
platform-accepted = required native OS lifecycle/cancel/lease gates pass
```

Public docs should state only capabilities actually accepted for the relevant profile/platform.

The Registry is useful discovery/reference metadata; ServerFS does not use it at runtime to auto-download or authorize code.

## 20. Error contract

Prefer stable ServerFS error codes over raw SDK/provider messages. Candidate additions should remain narrow, for example:

```text
AGENT_ACP_PROTOCOL_UNSUPPORTED
AGENT_ACP_LAUNCH_FAILED
AGENT_ACP_PROTOCOL_ERROR
AGENT_AUTH_REQUIRED
AGENT_CONTINUATION_UNSUPPORTED
AGENT_MODEL_UNSUPPORTED
```

Final names should follow existing Bridge error conventions and tests. Do not expose executable paths, credentials, raw stderr or ACP payloads in normal MCP errors.

Provider-specific error text may be retained only in private bounded diagnostics when safe and necessary for operators.

## 21. Security review checklist

Before release verify explicitly:

- no new generic MCP executor exists;
- no task parameter reaches executable/argv/env selection;
- ACP profile executable is operator-configured absolute path;
- no shell invocation;
- provider auth remains provider-owned;
- no secrets written into tracked files/task manifests/events;
- no ACP client fs/terminal authority advertised;
- no implicit extra workdir roots;
- no recursive ServerFS MCP injection;
- writer lease acquired before provider mutation can begin;
- cancellation cannot release lease before stop evidence;
- restart cannot clear guard merely because session is resumable;
- raw provider extensions/_meta do not become public trusted data;
- ACP dependency remains Bridge-only and lazy;
- existing native runtimes retain their security policy;
- each platform's process containment is tested natively.

## 22. Main risks and mitigations

### Risk: ACP profile becomes arbitrary execution

Mitigation: operator-only absolute executable + fixed bounded argv, no shell/env/task overrides, runtime derived from known profile.

### Risk: protocol capability is confused with provider behavior

Mitigation: conservative unmeasured state, handshake-derived snapshot and live acceptance matrix.

### Risk: persistent permission option is semantically downgraded

Mitigation: exact opaque ACP option-ID round-trip; do not coerce to legacy decisions.

### Risk: cancellation reports success while tools keep mutating files

Mitigation: terminal-first task state plus bounded protocol cancel and platform-specific process-tree stop evidence; guard remains on uncertainty.

### Risk: session resume clears recovery safety

Mitigation: session identity and execution-stop evidence remain separate concepts.

### Risk: configuration drifts across Linux/native/MCP/Bridge

Mitigation: one normalized runtime/profile registry model with parity tests at every rendering boundary.

### Risk: provider quirks recreate provider-specific adapters inside `acp.py`

Mitigation: prohibit provider-name conditionals; branch only on negotiated ACP version/capabilities. A true provider exception requires a separate documented design decision.

### Risk: optional ACP features expand scope indefinitely

Mitigation: v0.19 core freezes permission/cancel/session lifecycle; elicitation, v2 and heuristic model mapping stay out unless their explicit gates pass.

## 23. Release success criteria

v0.19.0 is complete only when all applicable criteria are evidenced:

1. Native admission rules are repository-visible and enforce standard-protocol-first behavior for future Agents.
2. One generic `AcpAdapter` supports multiple configured ACP profiles with no provider-name protocol branches.
3. ACP stable v1 negotiation is enforced; v2 is not claimed.
4. Task caller cannot choose executable/argv/env or obtain generic host execution.
5. Existing workdir authorization, writer lease, recovery guard, idempotency, deadlines and result spooling remain intact.
6. Exact ACP permission option selection is human-controlled and validated.
7. Cancellation/process-stop semantics are fail-closed on Linux, macOS and Windows where ACP support is claimed.
8. Runtime capability reporting is conservative and evidence-backed.
9. At least two materially different real ACP implementations pass the Linux core live acceptance; representative native macOS/Windows ACP smoke passes before those platforms are claimed.
10. Existing Codex/Claude/Qoder full relevant regressions remain green.
11. Documentation clearly distinguishes native adapters from standards-based ACP profiles and does not imply Registry entries are automatically trusted/supported.
12. Root, Bridge, deployment and site gates required by `docs/maintainers/workflow.md` pass on the release candidate.
13. Any unverified optional capability is documented as unsupported/not verified rather than inferred.
14. No unrelated refactor, auth redesign or lifecycle redesign is bundled into the release.

## 24. Execution order for this project

Use this environment order unless an implementation discovery proves a platform-specific blocker earlier:

```text
VPS / Linux ServerFS
  Phase 1-8 generic implementation + Linux acceptance
        |
        +--> Git synchronization
        |      |
        |      +--> Mac ServerFS: Phase 9 native changes + acceptance
        |      |
        |      +--> Windows ServerFS: Phase 10 native changes + acceptance
        |
        v
VPS / Linux ServerFS
  integrate accepted cross-platform changes
  Phase 11 full release convergence
```

Delegate to Codex only for narrow, auditable implementation or review tasks. The orchestrator remains responsible for architecture, cross-platform parity, repository mutations outside delegated scope and final verification. A delegated task must not expand the version scope because it discovers an unrelated improvement.

---

This plan intentionally optimizes for **one small standards layer with strong lifecycle semantics**, not maximum protocol feature count. The v0.19.0 architectural success is that a new compliant Agent usually becomes a ServerFS runtime profile rather than a new ServerFS provider implementation.
