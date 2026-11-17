# macOS v0.25.0 reviewed-source snapshot dry run — 2026-11-16

## Result and scope

**PASS for the local dirty-tree source-selection and offline packaging drill**, not for a release. The inspected checkout was still based on local Git HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`, and Root/Bridge project and Wheel metadata remain `0.24.2`. No commit, push, tag, source-version change, dependency sync, production service restart or new Cloudflare resource was used.

The source input gate is `deployment/macos/v0250_candidate_source_gate.py`, with an exact reviewed overlay list in `deployment/macos/v0250_candidate_source_paths.txt`. The gate compares Git porcelain `--untracked-files=all` against that list and fails closed on staged changes, unknown paths and unsafe candidate files. Pre-stage inventory returned **192 reviewed paths**, excluding **55 pre-existing generated scratch paths**.

## Independent reconstruction and build

A single new, randomized private directory under `deployment/macos/.serverfs-v0250-candidate-<12-hex>` was used for the entire exercise. The gate first materialized **Git HEAD** into a new snapshot and then overlaid exactly the 192 reviewed changed files; it did not read unreviewed generated scratch as input. The resulting tree contained **786 files**. Checks confirmed that `.git` and pre-existing `.serverfs-*` scratch were absent, Root and Bridge `pyproject.toml`/`uv.lock` were present, and the required private-named runtime modules matched the active source bytes.

Offline Wheel builds executed **from the reconstructed snapshot**, not directly from the dirty repository checkout. Both succeeded. The existing archive gate accepted the two Wheels at their actual metadata version `0.24.2` and checked member safety and full RECORD digests:

| Package | Archive gate | Members | Metadata version |
| --- | --- | ---: | --- |
| Root ServerFS MCP | PASS | 113 | 0.24.2 |
| Agent Bridge | PASS | 54 | 0.24.2 |

This proves that the explicitly selected development source set can independently generate the expected two packages. It does **not** prove a clean committed release SHA, a version-matched `0.25.0` release artifact, a fresh installed-wheel E2E, or independent Windows/Linux acceptance.

## Validation and custody

The candidate gate's focused test suite returned **13 passed**, and targeted Ruff lint, Ruff format and `git diff --check` passed. The source selection check returned PASS. Before removing the unique task-owned scratch, the operator checked current-user UID 501, private directory mode `0700`, and absence of symlinks across **862 scratch nodes**. The task-owned scratch was then removed. The four protected macOS production service processes remained present under PIDs **1779, 1790, 40516, 66419**. Other retained scratch directories were not authorized for cleanup and remain intact.

Git postflight counted **11 unstaged tracked modifications, 236 untracked individual paths, and zero staged paths** with `git status --porcelain -z --untracked-files=all`, before this report was created. These are consistent with 192 candidate changed paths (of which 11 are tracked) plus 55 existing scratch paths: `(192 - 11) + 55 = 236`. The ordinary Git status display collapsed untracked directories and showed only 178 entries, which is **not evidence of deletion**. The older scratch roots were independently observed still existing.

## Remaining release gates

- Final path-owner review and a reviewed integration commit containing this reconstructed source set and required tests/docs.
- Coordinated Root, Bridge, Plugin and lockfile metadata `0.25.0`; build artifacts from one exact immutable commit SHA.
- Full macOS native/installed-wheel lifecycle and ingress acceptance against the release SHA, with Windows native and Linux Compose acceptance at the same SHA.
- Separately characterize intermittent Cloudflare MCP tool errors and nested provider MCP approval; neither is resolved by this source drill.

No release or production cutover is claimed.
