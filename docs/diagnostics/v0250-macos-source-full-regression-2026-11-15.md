# v0.25.0 macOS Source Regression: Formatting Follow-up (2026-11-15)

This follow-up resolves the four Ruff format findings from the prior complete macOS regression. The changes are formatting-only; no behavior was changed.

## Scope

- `src/serverfs_mcp/cli.py`
- `tests/test_darwin_v0250_instances.py`
- `tests/test_darwin_v0250_openai_connector_ids.py`
- `tests/test_darwin_v0250_shared_interpreter_pin.py`

The `cli.py` change is a single Ruff-compatible line-wrap adjustment. Its concurrent functional changes were preserved. No live registration or opt-in acceptance was run.

## Verification run for this follow-up

- `uv run ruff check` and `uv run ruff format --check` passed for the four files.
- The same Ruff checks passed for the current macOS-related source, test, and deployment set: 170 files already formatted.
- Targeted pytest passed: 38 tests across the three named test files.
- Root pytest passed with all live/runtime/SMAppService opt-in environment flags unset: **2338 passed, 417 skipped**. This count is from this follow-up run.
- `git diff --check` passed.

## Previously recorded regression gates

The prior complete regression record reports root pytest at **2338 passed, 417 skipped** and Agent Bridge pytest at **538 passed, 134 skipped**. The root result was independently rerun above. Agent Bridge was not rerun for this formatting-only follow-up; its numbers remain prior recorded evidence, not new results.

No commit, push, tag, release publication, service operation, or Cloudflare operation was performed.
