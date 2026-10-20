---
name: serverfs-upgrade
description: Upgrade an existing ServerFS release or deployment; preserve operator state and verify enabled subsystem continuity.
---

# Upgrade ServerFS

Treat upgrade as a state-preserving change. First prove what currently owns the deployment and what optional capabilities are enabled; then update only the product artifacts and lifecycle pieces required by the target release.

## When to use

Use this skill when the user wants an existing ServerFS installation, checkout, container deployment, native installation, Tunnel client integration, or Agent Bridge deployment updated to a newer ServerFS release.

## When NOT to use

Do not use this skill for:

- first install on a host with no ServerFS state: `serverfs-install`;
- unrelated configuration changes: `serverfs-configure`;
- unexplained breakage without a known upgrade objective: `serverfs-diagnose`;
- provider CLI upgrades that are not part of the ServerFS release itself unless they are explicitly required and documented by the Agent Bridge workflow.

An upgrade does not silently opt the operator into newly introduced optional features.

## Preflight

First locate the target ServerFS checkout or deployed installation and establish its version, active configuration and lifecycle owner. In imported/standalone mode, if no authoritative checkout/deployment source is accessible, stop at bounded guidance; do not mutate or infer target commands/settings.

Paths below are relative to the located **ServerFS checkout root**, never to this skill or the Plugin ZIP. Resolve repository paths only after locating that checkout. For an installed deployment without a checkout, use its version-matched deployment documentation, installed CLI help and active configuration; obtain matching sources before a step that needs missing repository material.

1. Identify platform and deployment family.
2. Establish the current ServerFS version and requested target version from authoritative release/package evidence.
3. Locate every operator-owned state/config source used by the deployment:
   - `.env`;
   - `serverfs.toml`;
   - auth state;
   - provider/private Agent Bridge state;
   - Tunnel credential/key files;
   - platform service registration.
4. Determine which optional capabilities are currently enabled: generic HTTP, Tunnel, API Key, OAuth, file ingress, Agent Bridge/Jev.
5. If operating on a source checkout, inspect Git status before synchronization. Unknown local changes are a stop condition; do not discard them to make the repository match remote.
6. Read the target release notes/current deployment guides for migration-specific requirements before changing runtime state.
7. Confirm there is no active operation/writer lease that the documented upgrade path requires to be quiescent.

Never expose secret values while inventorying state.

## Authoritative sources

- `README.md`
- `site/src/content/docs/docs/getting-started.md`
- `site/src/content/docs/docs/windows-native.md`
- `site/src/content/docs/docs/macos-native.md`
- `deployment/windows/README.md`
- `deployment/macos/README.md`
- `deployment/agent-bridge/README.md`
- target GitHub release/tag/package metadata when an external release is being installed;
- current CLI/doctor/service behavior on the target host.

Historical plans may explain a migration but do not override current release/deployment instructions.

## Procedure

1. **Freeze the before-state.** Record current version, service owner, exact Compose overlays/profiles or native config/env paths, and enabled optional subsystems without copying secrets. Proceed only with a non-secret before-state inventory covering every enabled subsystem.
2. **Protect operator state.** Do not overwrite `.env`, `serverfs.toml`, credential files, auth DB/state, provider configs, private Bridge state or platform approval state with repository examples. Confirm the documented recovery path and privately preserved state are available before replacing artifacts.
3. **Synchronize/acquire the target artifacts through the supported channel.** For a development/source checkout, use normal Git synchronization only after proving local changes are safe. For release installations, use the intended release assets/images/wheels. Verify acquired artifact version/integrity against the requested release before activation.
4. **Review configuration drift explicitly.** Compare new examples/schema with the existing operator config and add only newly required keys or migrations. Do not replace the whole config. Review a local diff and pass config validation before runtime replacement.
5. **Update the runtime using the platform-supported owner.** Keep the same current-user/non-elevated lifecycle. Confirm status shows the intended owner running the target artifact.
6. **Preserve optional deployment shape.** If Agent overlay, OAuth overlay or file-ingress profile was already enabled, include it in the recreation. If it was disabled, leave it disabled. Compare the recreated overlay/profile and capability inventory with the before-state.
7. **Run post-upgrade product validation.** Version, parser/doctor, service/container and transport checks must all refer to the new artifact. Proceed only after all applicable checks pass; report any missing evidence as Not verified.
8. **Check continuity of previously enabled optional subsystems.** Tunnel and Agent Bridge require their own stronger verification rather than generic process state. Complete only after each previously enabled subsystem meets its dedicated verification criteria.

## Platform branches

Load only the matching branch after identifying the deployment and requested mode:

- Linux / Docker Compose: read [linux branch](references/linux.md) before platform/mode-specific changes.
- Windows native: read [windows branch](references/windows.md) before platform/mode-specific changes.
- macOS native: read [macos branch](references/macos.md) before platform/mode-specific changes.

## Safety / stop conditions

Stop before mutation when:

- target/current version cannot be established;
- a source checkout has uncommitted/untracked state whose ownership or importance is unclear;
- the proposed action would overwrite operator config/state rather than merge it;
- the required lifecycle owner differs from the established supported current-user owner;
- there are active tasks/leases that the target update cannot safely interrupt;
- release artifact integrity/version cannot be verified;
- the target release introduces a migration that has not been read/understood;
- post-change config/doctor validation fails.

Do not delete auth/Agent/Tunnel state to force an upgrade through unless an explicit documented migration requires it and the user authorizes the destructive consequence.

## Verification

At minimum prove:

- `serverfs --version`/package/image evidence matches the target release;
- config parses/renders with the same operator values and optional feature intent;
- intended current-user service/container owns the runtime;
- base MCP/filesystem behavior is healthy;
- every previously enabled optional subsystem is still healthy through its dedicated evidence surface;
- no previously disabled subsystem became enabled accidentally.

For Agent-enabled Linux upgrades also use the current host Bridge verification path (including `--require-runtimes` where the deployment guide requires it) and preserve the Agent Compose overlay.

## Success criteria

Upgrade is complete only when the target release is the running artifact, operator configuration/state is preserved, the supported lifecycle owner remains intact, and all previously enabled required capabilities pass their post-upgrade checks.

## Failure handling

If the new artifact fails validation, do not stack unrelated changes. Capture the failing boundary, consult the target release migration/current docs and use the documented rollback mechanism where one exists.

For Agent Bridge on Linux, use the project's explicit rollback helper rather than manually swapping/removing installed state. For source/release artifact failures, report the last known-good version and exactly which verification failed.
