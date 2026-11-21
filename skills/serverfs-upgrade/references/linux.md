# Linux upgrade

Use the version-matched `deployment/linux/README.md` and primary Skill gates.

**Core or Connector:** retain exact Compose project, env and all overlays/profiles, old image identity and config. Verify/select the intended image; recreate only the target with `up -d --no-deps --no-build <service>` after satisfying dependency/stop constraints. Retain auth, ingress and signed attachment selection. No generic project restart or down. Rollback requires the previous image/config and compatible state.

**Host Bridge:** the named `linux_v0250_install` module stages a fresh immutable release label while the old one remains selected. Stage first; quiesce dependent Core operations and prove the selected owner stopped (including manual processes). Select the new label, start only its user systemd unit and verify signed IPC. Selection refuses running/failed/ambiguous units. To roll back, stop and select the previous label, then start. This reverses executable selection, not state schema or config. Do not overwrite release directories or reset grants/leases.

Verify actual component/version and fresh authorized behavior; separate source/readiness/public acceptance.
