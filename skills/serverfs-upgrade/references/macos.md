# macOS upgrade

Use the matching `deployment/macos/README.md` and primary Skill gates.

Named staging is create-only. Do not overwrite an installed runtime, pinned snapshot, private manifest or App to simulate an upgrade. Stage trusted matching native arm64 artifacts privately; verify signatures and Login Items requirements. Plan explicit migration/cutover if no supported version-switch path is documented. Preserve current runtime, state and recovery evidence.

Use mode-specific lifecycle: file-only, owned pair or shared Core. Stop ingress before stopping Core/owned pair; native stop guard refuses active Connectors. Shared Core never stops its owner and has no named Connector staging. Unstage only eligible inactive state after separate authorization; evidence can cause refusal. Do not force unstage.

Legacy default-owner operations require their own version-matched compatibility procedure. Verify a fresh session and unchanged unrelated components; do not claim automatic rollback.
