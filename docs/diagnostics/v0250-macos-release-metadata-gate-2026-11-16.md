# v0.25.0 macOS release metadata gate evidence (2026-11-16)

## Result

The dispatch-first release PREPARE metadata gate is integrated and passed focused tests and an
offline isolated v0.25.0 metadata/Wheel proof. The live checkout remains at released metadata
`0.24.2`; no release tag, commit, push, or immutable release SHA was created. This is not a
release or a new platform compatibility claim.

## Implementation and validation

`deployment/native/release_metadata.py` checks the requested strict `vMAJOR.MINOR.PATCH` tag,
Root/Bridge/Windows Cargo.toml/Plugin/Registry versions, every supported OCI package version and
its exact `ghcr.io/ntlx/serverfs_mcp:<version>` identifier, and the Root, Bridge, and Windows
local package lock records. The Release workflow invokes it in PREPARE before remote-main/tag
checks and before the dependent tag-creation job. It uses Python stdlib only. The candidate-source
gate permits exactly `.github/workflows/wheel-release.yml`; regression cases keep other hidden or
YML paths rejected.

- Focused pytest: **42 passed** (`tests/test_release_metadata.py`,
  `tests/test_release_workflow.py`, and `tests/test_darwin_v0250_candidate_source_gate.py`).
- Root `uv run ruff check .`: **passed**. Targeted `ruff format --check`: **5 files passed**.
- Candidate source admission and snapshot: **passed**, 211 reviewed paths, 2442 excluded
  pre-existing scratch entries, base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`.
- Final post-proof candidate source recheck: **passed**, 211 reviewed paths and 3240 excluded
  scratch entries (the count includes the new isolated proof artifacts); scratch was preserved.
- `git diff --check`: **passed**.

## Isolated offline proof

A newly created directory `deployment/macos/.serverfs-v0250-candidate-7d31f8a2c04e` had mode
0700 and was built through the candidate-source gate. Only this snapshot was version-transformed.
The live Root/Bridge/Cargo/plugin/registry/lock metadata remains `0.24.2`. The snapshot carries
`0.25.0` in all checked surfaces, including `server.json`'s nested package version and OCI tag.
The checker passed for `v0.25.0`, rejected a simulated nested package version drift, then passed
after that fixture was restored.

- Root and Bridge `uv lock --offline` and `uv lock --check --offline`: **all passed**.
- Third-party lock graph parity with live checkout: **passed**; Root 39, Bridge 49, Cargo 30
  non-local package records matched exactly.
- `cargo update --offline -p serverfs-windows-native`: **blocked** by offline cache/index mismatch;
  stderr required `unicode-ident 1.0.26`, while cached candidates were `1.0.24` and `1.0.22`.
  No retry or network access was made. The isolated Cargo.lock local package version was changed
  narrowly from `0.24.2` to `0.25.0`, and its complete non-local package records were compared
  equal with the live lock. No Windows binary build was attempted.
- Root and Bridge wheels built offline as `serverfs_mcp-0.25.0-py3-none-any.whl` and
  `serverfs_agent_bridge-0.25.0-py3-none-any.whl`. Existing wheel archive validation passed at
  `0.25.0`: Root 113 members / 1,445,427 bytes uncompressed; Bridge 54 / 767,102. The gate
  verifies authoritative wheel metadata and complete RECORD integrity.

## Production and remaining boundary

Read-only launchd inventory before and after reported the same protected labels/PIDs:
`com.ntlx.serverfs.cloudflare` 66419, `http-origin` 40516, `bridge` 1790, and `tunnel` 1779.
No production ServiceManagement, Tunnel, Bridge, or Core action was performed.

This work establishes only that the specified release metadata consistency gap is closed and
that Root/Bridge packaging and metadata can be proven offline in an isolated candidate snapshot.
The Cargo resolver gate remains unverified due to the missing offline index entry. No release
publication or platform compatibility is claimed.
