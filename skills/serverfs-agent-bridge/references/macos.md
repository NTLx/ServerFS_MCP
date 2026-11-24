# macOS host Bridge

Use `deployment/macos/README.md` and primary Skill gates. Native arm64 signed SMAppService; keep owner state, provider credentials and runtime private.

**Owned:** same instance ID for Core and dedicated Bridge. Choose owned mode before staging; `owned-preflight`, `owned-stage`, `owned-policy-check`, `owned-start`, `owned-status`. Private policy derives from TOML and native manifest/socket checks, not shared v2 grants. Use actual derived socket path. Named owned staging does not pass the legacy Jev private overlay. Stage is create-only, not live reconfiguration.

**Shared:** independently provisioned v2 owner required; no general owner installer through shared CLI. Owner issues per-Core grant and private binding from actual workdir policy digest, not TOML hash. One Agent-enabled alias in v0.25.0; 1–16 explicitly owner-bound Agent aliases in the v0.26.0 candidate (schema 1 for one alias, schema 2 for multiple). Additional disjoint file-only workdirs allowed; no Repo Ops or named Connector staging. Use pinned runtime and `shared-stage`, `shared-preflight`, `shared-start`, `shared-status` with exact `--bridge-id`. Owner can be offline during staging but must be ready/authorize at activation. Signed v2 rejects continuation/idempotency. Shared Core never operates owner.

Respect Login Items approval and active Connector stop guards. Verify Core and only requested Bridge capabilities; no default-owner group operations, forced unstage or lease removal.
