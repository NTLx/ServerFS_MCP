# macOS 27 arm64 local CI-equivalence run

**Reason:** hosted `macos-27` runners stayed queued >1h (free-account capacity);
dev_plan_v0.13.md §16 fallback: M-series self-hosted runner. This run executes the
exact step sequence of .github/workflows/macos-native.yml on the acceptance machine.

**Date:** 2026-10-10T06:49:03Z
**Commit:** 7b26f44dacfd831c8436d7f0d4c9dc18a55adc7b
**Machine:** Mac16,12, macOS 27.0.1 (26A434)

```text
$ uname -m
arm64
$ sw_vers -productVersion
27.0.1
$ sw_vers -productVersion | cut -d. -f1
27
$ sysctl -n sysctl.proc_translated
0
$ .venv/bin/python -c "import platform; print(platform.machine())"
arm64
$ file $(readlink -f .venv/bin/python) | head -1
/Users/lx/.local/share/uv/python/cpython-3.12.12-macos-aarch64-none/bin/python3.12: Mach-O 64-bit executable arm64
```

| step | result |
| --- | --- |
| assert uname -m == arm64 | PASS |
| assert macOS major == 27 | PASS |
| assert not Rosetta | PASS |
| uv sync --frozen | PASS |
| interpreter is arm64 | PASS |
| ruff check . | PASS |
| pytest (MCP suite) | PASS |
| agent_bridge sync | PASS |
| agent_bridge ruff | PASS |
| agent_bridge pytest | PASS |
| git diff --check | PASS |

Overall: ALL PASS — CI-equivalent gate satisfied on the acceptance machine
