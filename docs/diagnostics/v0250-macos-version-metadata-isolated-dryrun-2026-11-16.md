# macOS v0.25.0 isolated version/lock/Wheel dry run — 2026-11-16

## Result

**PASS — isolated version-metadata packaging feasibility. NOT A RELEASE.** The live repository still carries stable `0.24.2` project/Plugin/lock metadata, with uncommitted development source over local HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`. No Git commit, staging, push, tag, package installation, production process, service configuration, Tunnel Token or public DNS was changed.

A **new randomized private candidate snapshot** was reconstructed from Git HEAD plus the precise reviewed overlay allowlist using `deployment/macos/v0250_candidate_source_gate.py`. The inventory admission reported **193 reviewed paths** and **55 excluded existing scratch paths**. Only files inside this one new snapshot were edited for the trial.

## Version coordination

The trial first verified original `0.24.2` version metadata in the Root and Agent Bridge projects and Plugin. Then it updated **only the snapshot** to `0.25.0` for these three project metadata files, and ran:

- Root: `uv lock --offline` followed by `uv lock --check --offline` — both **PASS**.
- Bridge: `uv lock --offline` followed by `uv lock --check --offline` — both **PASS**.
- Lockfile package graph comparison against the live baseline — **PASS**. Root had **40** lock package entries, Bridge **50**. Nonlocal package versions, sources and dependency graph were unchanged; the local Root/Bridge project version entry changed to `0.25.0`.

No network access, dependency sync or live-project lockfile modification was required.

## Distribution verification

Both Root and Bridge Wheels built offline from the **versioned isolated snapshot**, with `0.25.0` embedded in the archive metadata. The existing macOS Wheel archive gate **PASS** at expected version `0.25.0`; independent ZIP `METADATA` checks identified the exact intended versions. Plugin JSON was `0.25.0` and `deployment/plugin/package_plugin.py validate` **PASS** against the snapshot.

One initial archive-gate invocation failed to start (exit **127**) because the command used a relative interpreter path after changing the working directory into the snapshot. The existing interpreter's absolute path was then used, and the real archive and Plugin checks passed. This was an invocation-path error, not evidence of a failing Wheel or inconsistent lock. No package source was changed to work around it.

## Cleanup and production invariants

The single task snapshot had owner UID **501**, mode **0700**, and **0 symlinks** in the inspected tree. It was removed after success. The four protected launchd PIDs **1779 / 1790 / 40516 / 66419** were still present. Existing scratch trees were not targeted.

Postflight, before creation of this report, showed **11 tracked unstaged changes, 237 untracked individual files, and zero staged**. The count is consistent with 193 reviewed changes (11 tracked) plus the 55 historical scratch paths. `git diff --check` passed.

## Subsequent complete development-regression gate

After this isolated metadata trial and the final reviewed-path report inclusion, the current macOS source checkout was verified again without enabling real native-service or SSE test opt-ins:

- Root full default pytest: **PASS, exit 0**, bounded at 240 seconds; the quiet runner did not retain an authoritative pass/skip summary.
- Root Ruff lint/format: **PASS**, **715 Python files** reported formatted.
- Bridge full default pytest: **PASS, exit 0**, bounded at 240 seconds; exact pass/skip counts not retained.
- Bridge Ruff lint/format: **PASS**, **120 Python files** reported formatted.
- Candidate manifest check: **PASS**, **194 reviewed paths** and **55 excluded pre-existing scratch paths**.
- Current stable Plugin package validation: **PASS** at version `0.24.2`; distinct from the already completed isolated `0.25.0` Plugin validation above.
- `git diff --check`: **PASS**. Final Git status at the check: **11 tracked modified, 238 untracked (per-file porcelain), zero staged**.
- Four production launchd PIDs were unchanged at **1779 / 1790 / 40516 / 66419**; no `v25c` or `kac` test launchd service labels were found.

This full regression verifies the **current dirty source state**, not installation or native acceptance from an exact immutable `0.25.0` commit.

## Remaining release gate

The completed trial demonstrates the technical feasibility of coordinating the Root/Bridge/Plugin/lockfile versions and producing `0.25.0` Wheels without third-party dependency drift. It does **not** create an immutable release SHA, commit version changes, establish native ServiceManagement acceptance from the versioned Wheels, retest Agent Bridge/root installed distributions for that SHA, or validate Windows/Linux at that same SHA. Those separate gates remain required before release publication.
