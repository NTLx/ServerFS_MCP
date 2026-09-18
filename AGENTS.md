# AGENTS.md

Read `README.md` for current behaviour, the security model, deployment and the release
contract. Read `dev_plan.md` as the original v0.1 design and implementation baseline —
historical context, not current requirements. Where the two disagree, the README, the
tests and the implementation win; `SERVERFS_DISABLE_DEFAULT_DENY` is one rule this
project deliberately reversed. This file carries what neither of them does: the reasons
behind the design, the traps that already cost debugging time here, and how work gets
verified in this repository.

## Change protocol

Start from a written problem statement — a task-book section, a review issue, an
observed misbehaviour. Restate the issue first, then make every changed line trace
to it. This codebase has been hardened by successive review passes; unsolicited
rewrites and drive-by cleanups discard decisions that are not visible from the code.

Work in steps: modify, run the targeted test, then run the full suite. Reaching the
end of an edit is not a milestone; a passing targeted test is.

Every fix lands with a regression test, exercised through the MCP surface where the
bug was observable — a test that calls an internal helper proves less than one that
calls the tool.

Before declaring anything done, the full gate in `README.md` → Development passes:
`uv sync --frozen`, `ruff check`, `ruff format --check`, `pytest`,
`docker compose config`, `docker compose build`. All six, actually executed.

Then report: files changed, how each issue was fixed, regression tests added, pytest
counts, and residual limitations. Anything not executed is `Not verified` — never
"should pass" or "theoretically fine".

## Channels

Every way a path is reached — read, stat, list, find, search, resource — is a
**channel**. The central invariant of this project is that **all channels filter
identically**.

A `DenyPolicy` is built once per call and travels on `ResolvedPath`; each channel
reads the policy from the resolved path rather than re-deriving rules. A new channel
or a new filter routes through that same object — a second matcher implementation is
a defect, not a shortcut.

`allow_hidden` and the credential deny rules are independent axes; all four
combinations are legal configurations and are covered by tests. Keep them uncoupled.

Tracing a deny bypass means following the *full* workdir-relative path on every
channel. A policy decision made against a search root's own relative path is a
partial path, and partial paths are how bypasses ship.

## Filesystem access

Request-derived traversal is FD-based: each component is opened relative to an
already-open directory descriptor — `dir_fd` plus `O_NOFOLLOW`, and `O_DIRECTORY` for
directories — and the final descriptor is `fstat`ed. `fdio.py` holds the shared
primitives and is the security boundary; `find_files` and the private `_root_fd`
helpers in `tools.py` and `filesystem.py` open descriptors of their own and must keep
the same semantics. No request-derived path travels as `lstat`-then-`open(path)`: that
gap is the TOCTOU window this design closes.

The workdir root is the one path opened by name — the trusted anchor from
configuration, carrying no request input, which is why the walk starts there.
`fdio.open_root` is that operation, error-mapped like the rest of `fdio`, and it
currently has no callers: the same two modules re-implement it without the mapping.
Prefer converging on the shared primitive over adding a third copy.

## Search

`rg` runs with `shell=False` and an argument array, `--json` output, streamed through
`selectors`, stopping at `limit + 1` policy-valid matches. Its cwd is the validated
directory FD via `/proc/self/fd/<fd>`, and result paths are re-checked against the
hidden and deny policy — validating the search root is one gate, not the only one.
`-L`/`--follow` are never passed; rg follows no symlinks.

## Logs

Structured JSON, one event per tool call. File contents, search queries, host and
container paths, and credentials stay out of the log, including error text.
Agent-facing errors are `CODE: short message`; internal paths are for DEBUG logs at
most.

## Intentional decisions

`SERVERFS_ALLOW_HIDDEN`, `SERVERFS_DISABLE_DEFAULT_DENY` and
`SERVERFS_EXTRA_DENY_GLOBS` are product features, not oversights: safe by default,
explicitly releasable by the administrator. `EXTRA_DENY_GLOBS` applies
unconditionally and survives `DISABLE_DEFAULT_DENY=true` by design.

The deployment shape — `serverfs-mcp` + `openai-tunnel`, internal-only network, no
published ports, no OAuth, 16 workdir slots — is fixed for v0.1. Write support,
shell execution, indexing, a web UI and non-OpenAI clients are out of scope for
v0.1, not pending work.

## Traps

- `O_NOFOLLOW|O_DIRECTORY` reports a symlink as `ENOTDIR`, not `ELOOP`. Classify it
  with a supplementary `lstat` after the open has already failed — the access itself
  is still refused by the kernel, so this adds no race.
- An empty component set means the workdir root: `os.dup` that descriptor.
  `/proc/self/fd/N` is a symlink in its own right and `O_NOFOLLOW` rejects it.
- A test helper that opens a different root than production hides real bugs. The
  scoped-search path collapse (`foo/foo/`) survived a full green suite because the
  helper passed the workdir root where production passes the search root. Keep
  helpers on the production call path.
- `docker compose config` interpolates `.env` and prints real secrets. Never paste
  its raw output; select the field you need, e.g.
  `docker compose config --format json | jq -r '.services["serverfs-mcp"].image'`.
- A bare `GET /mcp` against a running container can terminate the tunnel's active
  MCP session, leaving the deployment idle rather than visibly broken. Probe the
  tunnel's `/readyz` instead.
- The running deployment is live and serves a real tunnel. Exercise new behaviour on
  a throwaway stack under a separate compose project name rather than against it.
- Rebuilding the image is not deploying it: the running container keeps the old
  image until `docker compose up -d` recreates it.

## Release

Use `gh` for GitHub operations (`gh api`, `gh run`, `gh release`) rather than curl or
the web UI. When GitHub exposes no supported CLI or API operation for what you need —
GHCR package visibility is the known case — use the UI and report the exception
explicitly instead of reaching for an undocumented endpoint.

Tags drive images: `main` publishes `:edge`; a `vX.Y.Z` tag publishes `X.Y.Z`, `X.Y`
and `latest`. `latest` comes only from a stable tag, and the image version comes from
the Git tag — never from a GitHub Release event. Workflows pin every Action to a full
commit SHA and authenticate with `GITHUB_TOKEN` alone; pull-request builds never log
in, never push, and never write the shared build cache. Production deployments pin
`SERVERFS_IMAGE` to an exact version; `:edge` and `latest` are for trying things out.

End-to-end acceptance through ChatGPT belongs to the maintainer: an agent's reach
ends at the container's MCP surface. Say so rather than implying it was verified.

## Language

Code, comments, docstrings, tests, commits, pull requests and this file are English.
Talk to the maintainer in Chinese.