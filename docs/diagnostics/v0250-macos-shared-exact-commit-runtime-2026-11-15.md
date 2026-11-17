# macOS v0.25.0 exact-commit Shared Core runtime

## Scope

The release runtime input is materialized from the requested full lowercase Git
SHA only when it is the local checkout's current `HEAD`. `git archive` supplies
tracked committed bytes; dirty tracked files and untracked files do not enter
the build snapshot. The helper uses local Git object access and does not contact
remotes.

Archive extraction rejects links, special files, duplicate entries, traversal,
noncanonical paths, and oversized input. If extraction fails after this call
created its destination, that directory is removed. If destination creation
collides with a concurrent caller, its directory and contents are preserved.

Release installation requires both the exact commit SHA and stable project
version. The version is checked against the archived `pyproject.toml` and wheel
filename. Schema v2 provenance records the commit, release version, offline
status, and source/build/runtime digests. Verification reads source metadata
from that commit and rejects commit, version, or digest drift.

## Local validation

The source tests use disposable local Git repositories and synthetic runtime
inputs; they do not install a service or alter a managed runtime. The broader
Shared Core/macOS test selection was run with the opt-in runtime install,
network/live HTTP, signed live preflight, and SMAppService registration tests
disabled. Native service registration and production deployment were not run.

See the task run for exact command output and pass/skip counts. No managed
service registration was invoked or changed.
