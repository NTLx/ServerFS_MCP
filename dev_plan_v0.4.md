# ServerFS MCP v0.4.0 Design Baseline — Binary Transfer & Hierarchical Workdir Policy

> Status: **RELEASED / FROZEN v0.4.0** — the tag, GitHub Release and release workflow completed successfully; post-release connector verification is recorded in [`docs/phase-f-v0.4-release-candidate-2026-09-21.md`](docs/phase-f-v0.4-release-candidate-2026-09-21.md).
>
> Base release: **v0.3.1**.
>
> This document defines the v0.4.0 contract before implementation. When v0.4 work is in
> progress, this baseline overrides historical statements in v0.1/v0.2/v0.3 plans only
> where it explicitly says so. Existing accepted v0.3 Agent Bridge contracts remain frozen
> unless this document explicitly extends their configuration inheritance.

## 1. Goal

v0.4.0 has two product themes:

1. **Binary file transfer**
   - download a regular file as raw bytes through MCP;
   - upload a binary payload into an explicitly writable workdir;
   - explicitly overwrite an existing regular file under revision/CAS protection;
   - keep binary transfer disabled by default.

2. **Hierarchical workdir policy**
   - security/capability settings have a global default;
   - each enabled workdir may override that default;
   - workdir-specific scalar values take precedence;
   - additive deny rules remain additive rather than becoming a bypass;
   - effective policy is resolved once at startup and consumed uniformly by every path
     channel.

The v0.4 design must preserve the defining ServerFS property:

> new capability must reuse the existing filesystem security boundary rather than create
> a second path or mutation implementation.

## 2. Non-goals

v0.4.0 does **not** add:

- a generic shell, argv executor or environment injection tool;
- recursive copy/move/rename;
- recursive delete;
- chmod/chown tools;
- arbitrary host paths;
- provider SDKs inside the MCP container;
- Internet egress from the MCP container;
- chunk/session transfer protocol unless whole-file ChatGPT E2E proves it is unavoidable;
- resumable uploads;
- background file-transfer workers;
- transfer databases;
- an OpenAI-specific HTTP downloader inside ServerFS;
- provider-specific file IDs in the public ServerFS schema.

Do not turn a bounded file-transfer feature into a general transfer service.

## 3. Frozen v0.3 security invariants

### 3.1 Filesystem traversal

Every new binary path MUST use the existing path resolver and FD-safe primitives:

```text
workdir alias
  -> effective workdir policy
  -> resolve_workdir_path()
  -> root FD
  -> dir_fd/openat walk
  -> O_NOFOLLOW
  -> fstat regular-file validation
```

No request-derived path may be reassembled and reopened by pathname after validation.

### 3.2 Path policy parity

Every path channel must apply the same effective policy:

- list;
- find;
- search;
- text read;
- stat;
- MCP resource;
- text mutations;
- **binary download**;
- **binary upload/overwrite**.

A binary tool is not an exception to hidden-path, deny-glob or reserved-path policy.

### 3.3 Mutation serialization

Binary upload is a mutation and therefore follows:

```text
application authorization
  -> path policy
  -> process-local mutation_lock
  -> shared Agent writer lease
  -> FD-safe atomic filesystem mutation
```

If a workspace-write Agent holds the workdir writer lease, binary upload/overwrite returns
`WORKDIR_BUSY`.

### 3.4 Network and credentials

The `serverfs-mcp` container remains:

- no Internet egress;
- no published port;
- non-root;
- read-only root filesystem;
- `cap_drop: ALL`;
- `no-new-privileges`;
- no Docker socket;
- no provider credentials.

Binary upload must not fetch a ChatGPT/OpenAI temporary download URL from inside the MCP
container. That would break the v0.3 network boundary.

### 3.5 Streamable HTTP transport security (Issue #10)

v0.4.0 closed GitHub Issue #10 before release. Docker `internal: true` networking and
an unpublished port remain defense-in-depth layers, but they must not be the only Host /
Origin / DNS-rebinding boundary for the MCP HTTP transport.

Before implementing the fix, measure rather than guess:

1. the actual `Host` header used by the current OpenAI Secure MCP Tunnel when forwarding
   to `serverfs-mcp:8000`;
2. whether those requests include `Origin`, and the observed value if present;
3. the exact `mcp==2.2.0` `TransportSecuritySettings` and `streamable_http_app()` behavior
   from the installed package source/runtime;
4. the narrowest existing test layer that exercises Streamable HTTP transport requests.

The implementation MUST use MCP transport-level protection with the smallest explicit
configuration that accepts the measured production Tunnel traffic and rejects unexpected
Hosts/Origins. Do not copy an Issue example allowlist or broadly allow `*` merely to make
tests pass.

Required properties:

- legal measured Tunnel Host remains accepted;
- unexpected Host is rejected before MCP request dispatch;
- disallowed Origin is rejected;
- observed legitimate no-Origin behavior, if that is what the Tunnel actually sends,
  remains compatible with the pinned SDK's documented semantics;
- existing `/mcp` Streamable HTTP behavior and OpenAI Tunnel deployment remain functional;
- no new published port, reverse proxy, Internet egress or external middleware is added;
- configuration is explicit enough that changing deployment topology does not silently
  widen trust.

## 4. Configuration architecture

### 4.1 Separate infrastructure settings from workdir policy

Process/infrastructure settings stay in `Settings`, for example:

- log level;
- Agent Bridge socket/timeout/lock directory;
- list/search traversal ceilings;
- transport-wide implementation limits.

Workdir-effective authorization/capability settings move into one immutable policy object.

Recommended shape:

```text
EffectiveWorkdirPolicy
  read
    allow_hidden
    disable_default_deny
    extra_deny_globs
    max_read_bytes
    max_read_lines
  mutation
    max_write_bytes
  binary
    binary_transfer_enabled
    max_binary_transfer_bytes
  agent
    agent_mode
    agent_runtimes
```

The exact Python representation may stay flat if that keeps the implementation smaller,
but it MUST be one immutable effective-policy object owned by `Workdir`.

### 4.2 Resolve once at startup

Environment inheritance is configuration work, not request work.

```text
.env
  -> parse global defaults
  -> parse WORKDIR_XX overrides
  -> validate
  -> build EffectiveWorkdirPolicy per enabled slot
  -> immutable WorkdirRegistry
```

Tools must not re-read environment variables on each request.

### 4.3 Precedence

For scalar/boolean capability values:

```text
explicit WORKDIR_XX value
  > global SERVERFS value
  > safe built-in default
```

An empty workdir override means inherit.

Example:

```env
SERVERFS_BINARY_TRANSFER_ENABLED=false

WORKDIR_01_BINARY_TRANSFER_ENABLED=true
WORKDIR_02_BINARY_TRANSFER_ENABLED=
WORKDIR_03_BINARY_TRANSFER_ENABLED=false
```

Effective values:

```text
01 true
02 false (inherited)
03 false (explicit)
```

### 4.4 Additive deny rules are the deliberate exception

`EXTRA_DENY_GLOBS` is a security floor, not a normal scalar override.

Effective rules are:

```text
global SERVERFS_EXTRA_DENY_GLOBS
  UNION
WORKDIR_XX_EXTRA_DENY_GLOBS
```

A workdir may tighten the global deny policy but cannot remove a global deny rule.

This is intentional even though workdir settings otherwise win.

### 4.5 Strict parsing

Security-relevant booleans must parse strictly. Unknown values abort startup.

Do not reuse the current permissive `config._get_bool()` behavior for workdir policy
switches if it would silently interpret a typo as false. The effective policy builder
must distinguish:

- empty = inherit;
- recognized true;
- recognized false;
- invalid = startup error.

Size limits must be positive integers; invalid explicit overrides must abort startup rather
than silently fall back.

## 5. Environment variable contract

### 5.1 Existing global policy defaults retained

Existing names remain valid and keep their v0.3 defaults:

```env
SERVERFS_ALLOW_HIDDEN=false
SERVERFS_DISABLE_DEFAULT_DENY=false
SERVERFS_EXTRA_DENY_GLOBS=

SERVERFS_MAX_READ_BYTES=524288
SERVERFS_MAX_READ_LINES=500
SERVERFS_MAX_WRITE_BYTES=1048576
```

Existing v0.3 deployments therefore retain their behavior.

### 5.2 New global binary defaults

```env
SERVERFS_BINARY_TRANSFER_ENABLED=false
SERVERFS_MAX_BINARY_TRANSFER_BYTES=8388608
```

Binary transfer is opt-in.

### 5.3 New global Agent policy defaults

```env
SERVERFS_AGENT_MODE=disabled
SERVERFS_AGENT_RUNTIMES=
```

These are policy defaults only.

`SERVERFS_AGENT_BRIDGE_ENABLED` remains the infrastructure master gate. If effective
workdir Agent policy enables delegation while Bridge infrastructure is disabled, startup
fails closed exactly as v0.3 does.

### 5.4 Workdir overrides

Each slot may define:

```env
WORKDIR_XX_ALLOW_HIDDEN=
WORKDIR_XX_DISABLE_DEFAULT_DENY=
WORKDIR_XX_EXTRA_DENY_GLOBS=

WORKDIR_XX_MAX_READ_BYTES=
WORKDIR_XX_MAX_READ_LINES=
WORKDIR_XX_MAX_WRITE_BYTES=

WORKDIR_XX_BINARY_TRANSFER_ENABLED=
WORKDIR_XX_MAX_BINARY_TRANSFER_BYTES=

WORKDIR_XX_AGENT_MODE=
WORKDIR_XX_AGENT_RUNTIMES=
```

Empty means inherit global defaults.

`WORKDIR_XX_READ_ONLY` remains a separate mandatory safe-default authorization/mount
setting because it drives both Compose bind-mount mode and application authorization.

### 5.5 Backward compatibility

A v0.3.1 `.env` with no new variables must produce the same effective behavior:

- binary disabled;
- current hidden/deny policy unchanged;
- current read/write limits unchanged;
- existing explicit `WORKDIR_XX_AGENT_MODE/RUNTIMES` unchanged;
- tool surface remains 11 or 19 according to the existing Agent deployment.

## 6. Workdir discovery

`list_workdirs` should expose capabilities that an MCP client needs in order to choose a
valid tool without trial-and-error.

Recommended additions to `WorkdirInfo`:

```text
binary_transfer: bool
agent_mode: disabled | review | workspace-write
agent_runtimes: list[str]
```

Do not expose deny patterns, container paths, host paths, UIDs or other security internals.

## 7. Binary tool surface

v0.4 adds exactly two public filesystem tools:

```text
download_binary_file
upload_binary_file
```

When binary transfer is disabled for every enabled workdir, neither binary tool is
registered.

Expected surfaces:

```text
filesystem only                 11
filesystem + binary             13
filesystem + Agent              19
filesystem + binary + Agent     21
```

The tools may be globally visible when at least one workdir enables binary transfer;
authorization is still checked against the selected workdir on every call.

## 8. Binary download contract

### 8.1 Semantics

`download_binary_file(workdir, path)` returns the exact contents of one regular file.

It may return a PNG, ZIP, PDF, FSA, BAM, UTF-8 text file or any other regular-file bytes.
It does not need to classify the input as "binary" first. It is the raw-byte channel.

`read_text_file` remains the semantic UTF-8 text channel.

### 8.2 MCP representation

Prefer MCP-native binary content/resource representation supported by the pinned Python
SDK, i.e. a base64-backed blob at protocol level rather than inventing a ServerFS URL.

The public result must include integrity/identity metadata:

```text
workdir
path
size
mime_type
sha256
revision
binary content
```

If the SDK/tool-result shape cannot directly carry the preferred binary content in a way
ChatGPT consumes, Phase E must prove the smallest compatible representation before the
release contract is frozen. Do not enable Internet egress as a workaround.

### 8.3 Consistency

Read through one held FD:

```text
fstat/revision before
  -> bounded read
  -> SHA-256
  -> fstat/revision after
```

If the revision changed, return:

```text
FILE_CHANGED_DURING_READ
```

If the file exceeds the effective binary transfer limit, return:

```text
BINARY_FILE_TOO_LARGE
```

Symlink, FIFO, socket and device behavior remains the same as other regular-file channels.

### 8.4 MIME

Use a best-effort MIME type; unknown is:

```text
application/octet-stream
```

MIME is descriptive metadata, never an authorization rule.

## 9. Binary upload contract

### 9.1 Provider-neutral payload

Core ServerFS accepts the binary payload directly in the tool request, encoded in a
provider-neutral representation such as strict base64.

Do not accept a public Internet URL and fetch it from the MCP container.

Base64 decoding must reject malformed input and must perform an encoded-length precheck
before allocating a decoded payload larger than the effective limit.

### 9.2 Create

Default:

```text
overwrite=false
```

means create-only.

If anything already occupies the target path:

```text
PATH_ALREADY_EXISTS
```

Creation reuses the current same-directory temp-file + fsync + `linkat` publication
primitive. A reader observes either no file or the complete new file.

### 9.3 Controlled overwrite

```text
overwrite=true
```

means replace one existing regular file.

It MUST require:

```text
expected_revision
```

and must not mean create-or-replace.

Rules:

- missing target -> `PATH_NOT_FOUND`;
- missing expected revision -> `REVISION_REQUIRED`;
- stale revision -> `REVISION_CONFLICT`;
- directory -> `NOT_A_FILE`;
- symlink -> reject through existing path/FD security;
- multiple hard links -> `MULTIPLE_HARDLINKS_NOT_SUPPORTED`.

This is the explicit v0.4 exception to the historical v0.2 "no overwrite" statement.
There is still no unguarded `force` operation.

### 9.4 Atomic replacement and metadata

Do not create a second binary mutation engine.

Refactor the existing bytes-oriented replacement primitive currently used by
`edit_text_file` so text editing and binary overwrite share:

```text
same-directory temp file
  -> write bytes
  -> preserve ownership/mode/xattrs
  -> fsync temp
  -> last-moment revision recheck
  -> atomic os.replace
  -> compute revision after commit
  -> directory fsync/logging
```

Metadata preservation failures occur before publication.

The v0.3 hard-link restriction remains.

### 9.5 Result

Upload should report enough information for round-trip verification:

```text
workdir
path
created/replaced
bytes_written
sha256
revision
revision_before (overwrite only)
```

Do not return the payload in the mutation result.

## 10. Client-side download name collisions

ServerFS controls writes **inside configured server workdirs**.

When ChatGPT/browser saves a downloaded file to the user's local device and a local file
with the same name already exists, rename/replace behavior belongs to the client/UI and is
outside the ServerFS server contract.

The server-side "overwrite" contract applies only to `upload_binary_file` targeting an
existing ServerFS file.

## 11. ChatGPT file-input constraint

ChatGPT may expose file parameters to MCP integrations using provider-specific metadata
such as a temporary `download_url` / `file_id`. Those values are not raw bytes.

The core v0.4 ServerFS design remains provider-neutral and no-egress:

- do not put OpenAI credentials in ServerFS;
- do not fetch the temporary URL from the MCP container;
- do not add a relay service preemptively.

Phase E must run a real ChatGPT spike:

```text
ChatGPT attachment
  -> ServerFS upload_binary_file
```

If the platform can deliver/directly encode data into the tool invocation, keep the core
design only. If it cannot, design the smallest **client-side** transfer helper/UI in a
follow-up change; do not weaken the server network boundary.

## 12. Whole-file transfer first

v0.4.0 intentionally does not implement:

```text
begin_upload
upload_chunk
finish_upload
transfer_id
resume_upload
orphan cleanup
background transfer workers
```

Default binary transfer ceiling:

```text
8 MiB
```

This is a bounded whole-file MCP feature. If real workloads later demonstrate a need for
large genomic binaries or other large artifacts, a chunked protocol belongs in a later
design with its own threat/concurrency model.

## 13. Recommended module boundaries

### 13.1 `config.py`

Keep process/infrastructure settings and global policy defaults.

Do not grow request-time environment lookups.

### 13.2 `workdirs.py` / optional `workdir_policy.py`

Own:

- strict override parsing;
- global/workdir inheritance;
- additive deny union;
- effective policy construction;
- validation of Agent/binary combinations.

`main.py` should stop knowing every individual `WORKDIR_XX_*` field.

Preferred startup shape:

```text
settings_from_env(env)
build_registry_from_env(env, settings)
create_server(...)
```

### 13.3 `binary.py`

If a new module is useful, keep it narrow:

- strict base64 decoding;
- bounded raw-byte read;
- SHA-256;
- binary result construction/orchestration.

Do not duplicate path traversal, mutation publication, revision or lease logic.

### 13.4 `mutations.py`

Expose reusable internal byte-payload primitives for create/replace while preserving the
existing text API semantics.

### 13.5 `tools.py`

Remain the authorization/audit/MCP registration layer.

Binary mutations use the same `_resolve_mutable`, `mutation_lock`, and
`mutation_agent_lease` sequence.

## 14. Audit logging

Binary audit records may include:

- tool name;
- workdir alias;
- relative path;
- success/error code;
- duration;
- byte count;
- overwrite boolean;
- revision;
- SHA-256 only if explicitly accepted as non-sensitive metadata.

Never log:

- binary payload/base64;
- text content;
- provider file IDs;
- temporary download URLs;
- host/container paths;
- credentials.

Prefer not to log SHA-256 unless it has a concrete operational use.

## 15. Implementation phases

### Security Track S — Streamable HTTP transport protection (Issue #10)

> Status: **IMPLEMENTED / VERIFIED** in the released v0.4.0 source. Evidence:
> [`docs/transport-security-audit-2026-09-21.md`](docs/transport-security-audit-2026-09-21.md).
>
> GitHub Issue #10 was fixed, merged and closed for the v0.4.0 release line; the
> technical implementation and real Tunnel compatibility gate are complete.

This track is release-blocking and is executed immediately after Phase A1's policy parser
foundation, before further capability expansion.

#### S1 — Runtime measurement and SDK audit

Read-only evidence only:

- capture the real Tunnel -> MCP `Host` header without changing the production route;
- determine whether `Origin` is present;
- inspect the installed `mcp==2.2.0` transport-security implementation;
- identify the existing transport-level test seam.

#### S2 — Minimal transport-security implementation

Enable MCP transport protection using the measured values and pinned-SDK behavior. Keep
configuration minimal and explicit; do not add a generic configurable wildcard allowlist
unless measurement proves it is necessary.

#### S3 — Regression and live compatibility verification

Cover at least:

- accepted legitimate Host;
- rejected unexpected Host;
- Origin allow/reject behavior;
- no-Origin behavior as actually used by the OpenAI Tunnel;
- normal MCP initialize/tools calls;
- current Tunnel readiness / real ServerFS call path after deployment acceptance.

### Phase A — Hierarchical Workdir Policy

> Status: **COMPLETE / FROZEN** on `v0.4-dev`.
>
> The effective policy is resolved once at startup, consumed by filesystem/Agent paths,
> and exposed through `list_workdirs` for client capability discovery. Phase B and later
> must reuse this policy object rather than re-read environment variables.

Implement policy inheritance first, without binary tools.

Required outcomes:

- effective immutable workdir policy;
- strict workdir override parsing;
- read visibility policy migrated to effective workdir policy;
- read/write size policy migrated where appropriate;
- Agent global defaults + workdir override;
- additive global + workdir deny globs;
- old v0.3.1 environment behavior unchanged.

Phase A MUST NOT change the 11/19 tool surface.

### Phase B — Binary Download

> Status: **COMPLETE / FROZEN** on `v0.4-dev`.
>
> `download_binary_file` is capability-gated, reuses the shared path/FD security
> boundary, returns one MCP `EmbeddedResource/BlobResourceContents` plus structured
> metadata, and was verified against the pinned `mcp==2.2.0` wire shape. Phase C must
> reuse the same effective binary policy rather than widen the download contract.
>
> Validation at freeze: 19 Phase B tests and 717 root tests passed; targeted Ruff check
> and format check passed; exact base64 round-trip and structured metadata were verified
> through an in-memory MCP tool call.

Add:

- binary capability gate;
- `download_binary_file`;
- bounded FD read;
- raw-byte result using supported MCP binary representation;
- size/MIME/SHA-256/revision;
- concurrent-change detection.

### Phase C — Binary Upload Create

> Status: **COMPLETE / FROZEN** on `v0.4-dev`.
>
> `upload_binary_file` is registered only with the binary capability surface, defaults
> to `overwrite=false`, strictly decodes provider-neutral base64 under the effective
> workdir size limit, and reuses the existing same-directory temp + `linkat` create
> primitive. `overwrite=true` is explicitly rejected until Phase D adds the
> revision-guarded replacement contract.
>
> Validation at freeze: 25 Phase C tests and 742 root tests passed; targeted Ruff check
> and format check passed; the 13-tool filesystem+binary surface, schema default,
> exact bytes/SHA-256/revision, read-only/capability/path-policy gates, symlink-parent
> rejection, Agent writer lease and no-temp-debris failure paths were verified.

Add:

- strict provider-neutral payload decoding;
- `upload_binary_file(... overwrite=false)`;
- create-only atomic publication;
- read-only and binary-capability authorization;
- Agent writer lease;
- size/hash/revision result.

### Phase D — Controlled Binary Overwrite

> Status: **COMPLETE / FROZEN** on `v0.4-dev`.
>
> `upload_binary_file(overwrite=true)` now requires `expected_revision` and reuses the
> existing `_replace_at()` atomic replacement primitive. Replacement preserves mode,
> ownership and xattrs, rejects multiple hard links, fails on stale/missing revisions,
> and never publishes a partial file. `overwrite=false` remains create-only and rejects
> an irrelevant `expected_revision` to keep the call contract unambiguous.
>
> Validation at freeze: 12 Phase D overwrite tests, 25 upload regression tests and
> 754 root tests passed with 0 skips; targeted Ruff/format checks passed; schema/default
> and error contracts were independently verified.

Add:

- `overwrite=true`;
- mandatory `expected_revision`;
- shared atomic byte replacement primitive;
- metadata preservation;
- hard-link restriction;
- revision conflict tests.

### Phase E — Real MCP / ChatGPT E2E

> Status: **SERVER/TUNNEL ACCEPTED / FROZEN** in v0.4.0; refreshed-client binary calls
> and download output-schema exposure were verified after plugin refresh.
>
> Disposable real-MCP acceptance verified 11 / 13 / 19 / 21 tool surfaces, PNG and ZIP exact-byte upload/download round trips, revision-guarded overwrite, and both directions of global/workdir binary precedence. A separate 21-tool v0.4 stack connected through the real OpenAI Secure MCP Tunnel and received four real ChatGPT `list_workdirs` dispatches with HTTP 200 and no 421/403/Host/Origin/DNS-rebinding/session errors. See `docs/phase-e-v0.4-acceptance-2026-09-21.md`.
>
> The pre-release ChatGPT conversation retained its previously discovered 19-tool schema; after the plugin refresh, direct binary upload/download calls were verified and the download `outputSchema` was exposed. That measured client-discovery limitation is historical evidence, not a remaining release gate.

Validate through the real chain:

```text
ChatGPT -> OpenAI Tunnel -> ServerFS MCP
```

Required matrix:

- binary globally off;
- binary globally on;
- per-workdir enable override;
- per-workdir disable override;
- Agent off/on combinations;
- 11/13/19/21 tool surfaces;
- real small PNG/ZIP upload and download;
- SHA-256 byte equality.

The ChatGPT attachment input shape must be measured, not assumed.

### Phase F — Security & Release Regression

> Status: **COMPLETE / FROZEN** for the released v0.4.0 source.
>
> Final gate: root `754 passed`; Agent Bridge `83 passed`; root/bridge sync, lock, Ruff and format gates all passed; base and Agent Compose renders passed; tracked deployment shell syntax passed; scratch image build passed; no-Internet-egress / no-provider-credentials / no-published-port / internal-network boundaries passed; transport-security targeted suite `47 passed`; migration/surface suite `242 passed`; full capability surface remained exactly 21 tools with no generic executor. Production identity/start/restart/health was unchanged throughout. The v0.4.0 tag/release workflow and refreshed ChatGPT connector binary upload/download verification also passed; the download `outputSchema` was exposed. See `docs/phase-f-v0.4-release-candidate-2026-09-21.md`.

Reverify:

- no Internet egress;
- no provider credentials in MCP;
- deny/hidden parity on binary channels;
- FD traversal/symlink rejection;
- writer lease;
- existing Agent approval/question/continuation/steer/cancel behavior;
- v0.3.1 upgrade compatibility;
- Compose/base/Agent overlays;
- release documentation.

## 16. Test matrix

At minimum add MCP-surface regression coverage for:

### Policy inheritance

- global false + empty override;
- global false + explicit true;
- global true + explicit false;
- invalid boolean -> startup error;
- invalid positive integer -> startup error;
- global deny + workdir deny union;
- legacy explicit Agent workdir fields;
- global Agent defaults inherited;
- explicit workdir Agent disable over global enable.

### Binary download

- PNG;
- ZIP;
- PDF;
- empty file;
- UTF-8 text through raw channel;
- hidden allowed/denied;
- global/workdir deny;
- reserved path;
- symlink final;
- symlink parent;
- FIFO/socket;
- oversize;
- concurrent change;
- binary disabled workdir.

### Binary upload create

- arbitrary bytes including NUL;
- empty payload;
- malformed base64;
- encoded-size precheck;
- oversize decoded payload;
- read-only workdir;
- binary-disabled workdir;
- denied/hidden/reserved path;
- missing parent;
- existing file/directory/symlink;
- Agent writer lease busy;
- exact SHA-256 equality;
- no temp debris after failure.

### Binary overwrite

- correct revision;
- stale revision;
- missing revision;
- missing target;
- directory/symlink;
- multiple hard links;
- mode preservation;
- ownership preservation where test environment permits;
- xattr preservation where filesystem supports it;
- complete old-or-new visibility;
- post-commit revision matches next stat.

## 17. Tool registration rules

Binary tool registration:

```text
register if ANY enabled workdir has binary_transfer_enabled=true
```

Per-call authorization still checks selected workdir.

Agent tool registration continues to depend on:

```text
SERVERFS_AGENT_BRIDGE_ENABLED
AND
any effective workdir Agent policy enabled
```

Expected public surfaces:

```text
11  filesystem
13  filesystem + binary
19  filesystem + Agent
21  filesystem + binary + Agent
```

## 18. Migration gate

Using an unchanged v0.3.1 deployment configuration with v0.4 code MUST:

- start successfully;
- retain the same effective hidden/deny/read/write policy;
- keep binary disabled;
- preserve Agent behavior;
- expose the same 11- or 19-tool surface;
- not require a new Compose overlay;
- not require a network change;
- not require a provider reconfiguration.

## 19. Release gate for v0.4.0

The v0.4.0 release gate completed as follows:

```text
[x] GitHub Issue #10 technical fix is implemented with explicit transport-security regressions
[x] GitHub Issue #10 is closed when the fix reaches the v0.4 release line
[x] actual OpenAI Tunnel Host and Origin behavior is measured and documented
[x] legitimate measured Host is accepted
[x] unexpected Host is rejected at the MCP transport layer
[x] Origin validation is covered without breaking legitimate Tunnel traffic
[x] v0.3.1 filesystem and Agent regression suites remain green
[x] effective workdir policy is resolved once at startup
[x] scalar global -> workdir precedence is fully tested
[x] additive deny union is fully tested
[x] legacy .env behavior is preserved
[x] Agent policy supports global default + workdir override
[x] binary transfer defaults disabled
[x] binary tools are absent when disabled everywhere
[x] binary tools appear when at least one workdir enables binary
[x] list_workdirs reports binary and Agent capabilities
[x] binary download returns exact bytes
[x] binary download detects concurrent changes
[x] binary download enforces effective size limit
[x] binary upload accepts arbitrary bytes including NUL
[x] malformed/oversized payloads fail closed
[x] upload create is atomic and never overwrites
[x] overwrite is explicit
[x] overwrite requires expected_revision
[x] overwrite stale revision fails
[x] overwrite preserves metadata
[x] overwrite retains hard-link restriction
[x] binary reads obey hidden/deny/reserved policy
[x] binary writes obey hidden/deny/reserved policy
[x] binary writes obey READ_ONLY authorization
[x] binary writes obey shared Agent writer lease
[x] symlinks are never followed
[x] failed binary mutations leave no published partial file/temp debris
[x] MCP container still has no Internet egress
[x] provider credentials remain host-side
[x] no generic executor exists
[x] 11/13/19/21 tool-surface matrix passes
[x] refreshed ChatGPT connector directly invokes binary upload after final edge deployment
[x] refreshed ChatGPT connector directly invokes binary download after final edge deployment
[x] current ChatGPT client tool-discovery cache limitation is measured and documented
[x] root release gate passes
[x] Agent Bridge independent gate passes when touched
[x] deployment shell syntax gate passes when touched
```

## 20. Development discipline

Work phase-by-phase. Each implementation task should be atomic and must:

1. cite the relevant section of this plan;
2. change only the minimum modules needed for that phase/sub-phase;
3. add regression tests through the public MCP surface where observable;
4. run targeted tests;
5. report exact results and residual limitations;
6. not silently continue into the next phase.

The maintainer performs architecture review between phases.

The v0.4.0 tag and release were created after Phase F closed the release gate; the
published acceptance record remains the historical evidence for that decision.
