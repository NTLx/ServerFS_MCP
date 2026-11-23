# Windows upgrade

Use the matching `deployment/windows/v0250-native-operations.md` and primary Skill gates.

Named runtime install, initial private configuration and Connector helpers are create-only. They are not in-place upgrades; failed attempt markers and partial state must remain for diagnosis. Do not rerun, overwrite, delete or re-ACL to bypass checks. Stage trusted matching native x64 wheel artifacts and offline cache privately; plan explicit fresh-instance migration/cutover unless a documented supported alternative exists.

Use exact current-user component CLI identity and verified installed runtime. Quiesce ingress, Core and owner in dependency order where affected; never stop a shared owner through its Core. Uninstall removes the selected verified inactive task, not private state. Do not elevate or kill by executable name.

Verify actual version, scheduler state, ACLs and authorized MCP/IPC. No automatic rollback or credential rotation; preserve original artifacts/state and verify compatibility before recovery.
