# Windows host Bridge

Use `docs/deployment/windows/v0250-native-operations.md` and primary Skill gates. Native Windows 11 x64/NTFS, protected DACLs, current-user tasks, separate Bridge environment and Agent worker venv. No elevation, WSL or VM.

Choose mode before private config. Owned Bridge ID equals Core ID; shared independently managed owner ID and explicit per-Core grants. Initial `configure-v25-private.py` supports up to 16 Cores, distinct caller keys/grants, exact subsets of owner workdir/action/runtime policy; initial bearer only. v0.26.0 supports explicit runtime/ACP `use_proxy=true` with protected `--agent-proxy-env-file` input (credentialless, outside exposed roots and checkout, no reparse paths). Use the matching manual path for OAuth or other specialized cases. Install/config helpers are create-only; no automatic live grant append/rotation or in-place update.

Install verified matching wheels into each required runtime, stage private config, then separately authorize registration/start of exact Bridge before Core. Use `serverfs_mcp.windows_v25.cli bridge --instance <id>` and matching Core identity; verify actual task, worker and protected Named Pipe authorization. Same SID is not grant authorization. Signed grant-enabled submission rejects continuation/idempotency.

Verify permitted alias/method/runtime and foreign task denial where applicable. Repo Ops-only does not start a provider. Shared Core cannot manage owner; honor active tasks/leases before stopping owner, preserve failed attempt state. Uninstall selected task only, not private state.
