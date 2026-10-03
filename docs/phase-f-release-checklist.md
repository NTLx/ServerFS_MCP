# v0.10.0 Release Checklist (Phase F closure order)

Preconditions — all must be green before the tag exists:

- [ ] Phase F evidence document (`docs/phase-f-acceptance-2026-10.md`) complete
      with CI run IDs for every pushed head.
- [ ] Live ChatGPT tunnel E2E executed by the maintainer per section 8 of the
      evidence document, OR the release is explicitly scoped without that claim.
- [ ] PR for Phase F merged to `main` after review.

Step 1 — version bump commit (single, on `main`, reviewable):

- [ ] `pyproject.toml`: `version = "0.10.0"` (SERVER_VERSION resolves through
      package metadata, no code change).
- [ ] `uv lock` to refresh `uv.lock` (committed together).
- [ ] `native/windows/Cargo.toml`: `version = "0.10.0"` (drops `-dev`; owns the
      wheel METADATA version the tag gate checks) + refresh
      `native/windows/Cargo.lock` via `cargo update --workspaces` or
      `cargo generate-lockfile` in `native/windows` — no other dependency churn.
- [ ] README: "Current stable release" line, Docker image & release channels
      table (`ghcr.io/ntlx/serverfs_mcp:0.10.0` / `0.10`), upgrade section with
      a v0.10.0 entry (Windows native section already documents the surface).
- [ ] Website (`site/`): news/release page per the site gate
      (`cd site && npm ci && npm run build`, `git diff --check`).
- [ ] After tag+publish, separate commit `docs: mark v0.10.0 as released`:
      AGENTS.md current-stable paragraphs (`dev_plan_v0.10.md` first, v0.9 plan
      becomes the frozen prior record) and the dev_plan status line.

Step 2 — tag and publish (maintainer action; tags are immutable once published):

- [ ] `git tag v0.10.0 && git push origin v0.10.0`.
- [ ] Container workflow publishes `0.10.0`, `0.10`, `latest` images.
- [ ] `wheel-release` workflow runs on the tag: version gate reads METADATA
      versions of both wheels (must equal `0.10.0`), clean two-wheel acceptance,
      then attaches `serverfs_mcp-0.10.0-py3-none-any.whl` and
      `serverfs_windows_native-0.10.0-cp312-abi3-win_amd64.whl` to the GitHub
      Release with the managed marker block (SHA-256 + asset URLs).

Step 3 — post-publish verification:

- [ ] Clean Windows machine (or fresh profile): download both release assets,
      `uv venv` + two-wheel install, `serverfs doctor` exit 0, one stdio
      mutation round-trip. This is the GA-facing install story.
- [ ] Optionally wire the `uv` URL source for the native wheel into
      `pyproject.toml` (`[tool.uv.sources] ... url = <asset>`) now that the
      asset resolves, and record the universal-lock refresh in a follow-up
      commit; until then the two-command `uv pip install` story is the contract.
- [ ] GHCR package visibility per release process (UI; GitHub has no API for it).
- [ ] `serverfs bootstrap native-wheel --url <asset> --sha256 <notes digest>`
      verified once against the live asset.

Out of scope for v0.10.0 (do not claim): PyPI publication (`Private :: Do Not
Upload` stays), SOCKS5 proxies, Windows arm64 GA, ReFS/SMB/cloud storage,
multi-instance stdio tunnels.
