# macOS exact-commit Shared Core offline install (2026-11-15)

## Objective and boundary

Performed one real create-only Shared Core runtime install using the exact local
Git `HEAD` archive and the implemented
`darwin_shared_runtime.create_shared_core_runtime(...,
release_commit=<full HEAD SHA>, release_version=<archived project version>)`.
This verifies exact-commit build behavior. **It is not a v0.25.0 release
acceptance:** the tested commit is older and its actual project version is
`0.24.2`.

The operation used an ephemeral 0700 directory under the canonical physical
macOS temporary directory. No service registration/start/stop, managed runtime,
Cloudflare, network or remote Git access, credential use, or repository source
change occurred. Only the task-created temporary root was removed.

## Exact inputs and commands

Initial inspection:

```sh
git status --short --branch
git rev-parse HEAD
git show HEAD:pyproject.toml
uname -a
command -v uv
uv --version
uv run --offline --frozen python -c 'import sys, serverfs_mcp; print(sys.executable); print(serverfs_mcp.__file__)'
```

The exact commit was `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`; reading that
commit's `pyproject.toml` yielded version `0.24.2`. Host: Apple Silicon macOS
27.2.0. uv: `/Users/lx/.local/bin/uv`, version `0.9.7`. The offline project
environment used CPython `3.12.12` at
`/Users/lx/.local/share/uv/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12`.

The successful operation was run as a Python stdin harness under:

```sh
uv run --offline --frozen python -
```

The harness obtained the SHA with `git rev-parse HEAD`, obtained the version
with `git show <SHA>:pyproject.toml`, resolved `sys.executable` and `uv` to
physical absolute paths, and created the temporary parent beneath
`Path(tempfile.gettempdir()).resolve(strict=True)` with mode 0700. It then ran:

```python
create_shared_core_runtime(
    target,
    repo,
    base_python,
    uv,
    release_commit="da9c12e600d3b336f1fd7a3d5232702e9abbc19f",
    release_version="0.24.2",
)
verify_shared_core_runtime(target, project_root=repo)
```

The same harness retried `create_shared_core_runtime` against the occupied
target and invoked the verify-only CLI twice with a deliberately wrong SHA and
a deliberately wrong version. The full literal operation was executed from
stdin and was not saved as a repository script.

## Results

- **PASS:** source was dirty relative to HEAD: 128 porcelain status entries,
  87,602 bytes of tracked `git diff HEAD` output, and 123 untracked files at
  the time of the run. The installer materialized source with local
  `git archive` for the exact SHA; the recorded `pyproject_sha256`
  (`e57b335b43c6b2b52805a5c91bae7b66d04ac52af93ebc71805195641f014e7c`)
  matched `git show <SHA>:pyproject.toml`, not the dirty worktree input.
- **PASS:** real runtime install returned `state=installed`, SHA
  `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`, and version `0.24.2`.
- **PASS:** read-only verification returned `state=verified`; provenance was
  schema v2, `offline=true`, and carried the exact commit and version. All six
  source/build/runtime SHA-256 fields were present and 64 lowercase hex
  characters. Wheel SHA-256:
  `b3a195f3bb52fb92f7481bbd380ab84abd77c6b31c047fe39ed149fc3c4a3238`.
- **PASS:** installed `bin/serverfs-core-python` resolved to the requested
  pinned path, and the installer's isolated `-I -B` import probe passed for
  `serverfs_mcp`, `serverfs_mcp.cli`, `mcp`, and `httpx2`.
- **PASS:** runtime and parent directory modes were 0700. Reinstallation
  refused with `shared Core runtime already exists`.
- **PASS:** verify-only with the wrong SHA and wrong version each exited 1,
  reporting `verified runtime release commit differs` and `verified runtime
  release version differs` respectively.
- **PASS:** cleanup removed only the generated temporary root; its post-cleanup
  existence check was false.

Two safe setup attempts were rejected before building: importing from global
pyenv CPython lacked installed project metadata; the first temporary parent
used macOS's noncanonical `/var` alias and was refused. The first temporary
directory was removed. Using the existing offline uv project environment and
the canonical resolved temp path allowed the acceptance to complete; neither
attempt modified repository or managed runtime state.

## Conclusion and limitation

The exact-commit offline create-only install mechanism passed this local
acceptance for commit `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`, project
version `0.24.2`. This is implementation evidence for the mechanism only. It
does not establish or claim a v0.25.0 release, release gates, service lifecycle
acceptance, or production deployment acceptance.
