---
name: serverfs-configure
description: Change existing ServerFS workdirs, filesystem policy, binary transfer, file ingress, or v0.23 Linux Repo Ops policy; validate effective configuration.
---

# Configure ServerFS

Change the smallest possible part of the operator's existing configuration and prove that the resulting configuration is valid before restarting or redeploying anything.

## When to use

Use this skill to add, remove or change ServerFS workdirs and ordinary filesystem/capability policy, including read-only state, size limits, hidden-file policy, deny rules, binary transfer, file-ingress settings, and — on the v0.23 Linux development line — per-workdir Deterministic Repo Ops enablement / named-action policy.

Use it when the user has a specific configuration objective but is not primarily asking to configure Tunnel, HTTP authentication, or Agent delegation.

## When NOT to use

Use the dedicated skill instead for:

- OpenAI Secure MCP Tunnel: `serverfs-tunnel`;
- static HTTP API Key or OAuth: `serverfs-auth`;
- Agent Bridge installation/lifecycle, runtime allowlists, provider/Jev behavior: `serverfs-agent-bridge`; Repo Ops policy itself remains a normal per-workdir configuration concern, but its Linux host Bridge transport must follow the current Bridge deployment guide;
- first installation with no existing configuration: `serverfs-install`;
- version upgrade: `serverfs-upgrade`;
- unexplained failure: `serverfs-diagnose` first.

Do not use this skill to invent a new configuration key or duplicate schema behavior in a helper script.

## Preflight

First locate the target ServerFS checkout or deployed installation and establish its version, active configuration and lifecycle owner. In imported/standalone mode, if no authoritative checkout/deployment source is accessible, stop at bounded guidance; do not mutate or infer target commands/settings.

Paths below are relative to the located **ServerFS checkout root**, never to this skill or the Plugin ZIP. Resolve repository paths only after locating that checkout. For an installed deployment without a checkout, use its version-matched deployment documentation, installed CLI help and active configuration; obtain matching sources before a step that needs missing repository material.

1. Identify the target platform and deployment family.
2. Locate the active configuration source:
   - Linux/Docker: existing untracked `.env` plus the exact Compose overlays in use;
   - Windows/macOS native: `serverfs.toml` for non-secret policy plus the documented narrow `.env` for secret/proxy/runtime values.
3. Read the current configuration before editing. Never start by copying an example over an existing file.
4. Read the current versioned reference for the keys being changed:
   - `.env.example`
   - `serverfs.toml.example`
5. Establish the user's exact intended change and preserve every unrelated key/value.

If the current config cannot be read safely or its ownership/source is ambiguous, stop and use `serverfs-diagnose` rather than reconstructing it from examples.

## Authoritative sources

- `.env.example` — complete Linux/Docker environment reference.
- `serverfs.toml.example` — native non-secret configuration reference.
- `site/src/content/docs/docs/configuration.md` — current configuration model and inheritance.
- `site/src/content/docs/docs/binary-transfer.md` — binary/file-ingress boundary.
- `site/src/content/docs/docs/repo-ops.md` — v0.23 Linux Repo Ops policy, named actions, integrity pins and host-Bridge boundary.
- `site/src/content/docs/docs/windows-native.md`
- `site/src/content/docs/docs/macos-native.md`
- current config parser/`serverfs doctor` behavior in the target checkout/release.

When these disagree, use implementation/tests and current parser behavior rather than guessing from historical plans.

## Procedure

1. **Snapshot logically, not by leaking.** Read the relevant existing section and remember enough surrounding context to make a revision-guarded/surgical edit. Do not print the entire secret-bearing `.env` into chat or logs. Before editing, identify the active file and the exact keys in scope.
2. **Resolve inheritance before changing it.** On Linux, global `SERVERFS_*` values may be inherited by `WORKDIR_XX_*`; deny-glob restrictions union rather than replace. Confirm whether the desired behavior belongs globally or to one workdir. Proceed after the effective inherited value and target workdir scope are established.
3. **Preserve secure defaults.** New workdirs should remain read-only unless the user explicitly needs writes. Optional binary transfer/file ingress and Repo Ops remain opt-in. Enabling Repo Ops does not enable Agent delegation; named actions require a read-write workdir and mandatory reviewed SHA-256 integrity pins.
4. **Apply only the requested keys.** Do not reorder/reformat the whole file or opportunistically normalize unrelated settings. Review the diff locally; every changed key must map to the request.
5. **Validate syntax and semantics before lifecycle changes.** Use the current parser/doctor/Compose render for the exact deployment shape. Proceed only on a successful validator exit for the same config/env/overlays used at runtime.
6. **Restart/recreate only if required by the affected setting and requested objective.** Keep all previously enabled overlays/profiles; do not accidentally drop Host Bridge (`compose.agent.yml`), OAuth or file-ingress mounts during recreation. Repo Ops and Agent delegation may share the Host Bridge transport while remaining independently authorized. Confirm status reflects the intended owner and retained deployment shape.
7. **Re-check effective behavior.** Prove the target workdir/policy changed and unrelated optional capabilities retained their prior state.

## Platform branches

### Linux / Docker Compose

Configuration lives primarily in the existing `.env`. Workdir slots use the current `WORKDIR_XX_*` schema from `.env.example`; do not hard-code a stale field list from this skill.

Render the exact deployment shape without emitting expanded secret values when only validity is needed. Examples:

```bash
docker compose --env-file .env -f compose.yml -f compose.http.yml config --quiet
```

Add the existing overlays/profiles that the real deployment uses, such as `compose.agent.yml`, `compose.oauth.yml`, or `--profile file-ingress`. On v0.23 Linux, `compose.agent.yml` is the authenticated Host Bridge transport for Agent delegation and/or Repo Ops; using the overlay does not itself authorize either capability.

A host workdir path typo must fail rather than being silently created for convenience. Do not create a requested workdir directory unless directory creation itself is explicitly part of the user's task.

### Windows native

Use `serverfs.toml` for workdirs and native policy. Keep secrets out of TOML. The v0.23 Deterministic Repo Ops implementation is Linux-only; do not add Linux Repo Ops fields or imply native Windows support. Validate with the stable connector environment:

```powershell
& $serverfs doctor --config serverfs.toml
```

If the managed lifecycle already exists, use `serverfs service status` to confirm which config/env it owns before changing/restarting it.

### macOS native

Use `serverfs.toml` for workdirs and policy and keep secrets in the documented private env/state boundary. The v0.23 Deterministic Repo Ops implementation is Linux-only; do not add Linux Repo Ops fields or imply native macOS support. Validate with:

```bash
serverfs doctor --config serverfs.toml
serverfs service status
```

Do not bypass APFS/platform/TCC failures by weakening path checks or using root.

## Safety / stop conditions

Stop instead of mutating when:

- the active config file/deployment shape is unknown;
- the requested workdir path or platform storage class is unsupported or cannot be verified;
- an existing config contains unfamiliar local changes that would be overwritten by the proposed edit;
- the requested change would silently change Auth, Tunnel, Repo Ops, or Agent policy without explicit intent;
- a Repo Ops action would use caller-controlled executable/argv/env/remote/ref/force, omit integrity pins, or auto-update pins without a separate operator review decision;
- a secret would be moved into `serverfs.toml`, a tracked file or an exposed workdir;
- validation reports an unknown/invalid key, path, policy combination or platform failure;
- the change appears to require root/Administrator ownership to work.

Never use `.env.example` or `serverfs.toml.example` as a wholesale replacement for a populated operator config.

## Verification

Use the strongest applicable existing validation without exposing secrets:

- Linux: exact `docker compose ... config --quiet`, then service status/behavior if the runtime was intentionally recreated;
- Windows/macOS: `serverfs doctor --config serverfs.toml` with the same env/lifecycle context used by the deployment, plus `serverfs service status` when persistent services are involved;
- workdir behavior: list/read/write only to the degree needed to prove the requested policy, using a safe disposable file when a write test is explicitly required.

If file ingress is changed, verify both its feature gate and deployment profile. If binary transfer is changed, verify that workdir/global limits remain within the current schema rather than assuming values from this skill. If Linux Repo Ops is changed, verify `repo_ops` in the effective workdir projection, preserve `compose.agent.yml`, verify named-action policy/integrity pins when configured, and never substitute Agent submission for a missing deterministic Repo Ops capability. Native Windows/macOS must remain Repo Ops-unclaimed until their own product support and acceptance exist.

## Success criteria

The task is complete when:

- only requested configuration semantics changed;
- the current parser/Compose/doctor accepts the result;
- the target workdir or policy exhibits the requested behavior;
- unrelated Auth/Tunnel/Repo Ops/Agent and other workdirs retain their previous state;
- no secret or host path was exposed through an inappropriate channel.

## Failure handling

On validation failure, stop and report the exact non-secret error and the changed key(s). Re-read the current schema/parser source before attempting a correction.

If the same class of edit fails twice, do not keep trying variants. Diagnose the actual active config/deployment source and compare it with the authoritative current template/implementation.
