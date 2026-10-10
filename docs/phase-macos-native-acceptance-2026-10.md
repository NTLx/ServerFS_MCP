# Phase J — Native macOS Filesystem Acceptance (real APFS / M-series)

**Date:** 2026-10-10
**Machine:** MacBook Air (Mac16,12), Apple M4, 24 GB
**OS:** macOS 27.0.1 (26A434), Darwin 27.0.0, native arm64 (`sysctl.proc_translated` = 0)
**Python:** CPython 3.12.12 (uv-managed, Mach-O arm64)
**ServerFS commit:** `df94673e16927e4a27d22428ce693cac5f3ec665` (feat/v0.13-macos27-arm64)
**Filesystem:** local APFS (root volume)
**Method:** live drive of the real MCP server surface (`create_server` + `call_tool`) against a
real APFS workdir, script captured in the acceptance run log below.

## Results — 23/23 PASS

| # | Check | Result |
| --- | --- | --- |
| 1 | create_text_file | PASS |
| 2 | read_text_file (exact content) | PASS |
| 3 | stat_file (type + opaque revision) | PASS |
| 4 | list_directory (sorted, paginated) | PASS |
| 5 | find_files (glob) | PASS |
| 6 | search_text (literal) | PASS |
| 7 | edit_text_file (exact CAS replace) | PASS |
| 8 | revision CAS conflict → `REVISION_CONFLICT` | PASS |
| 9 | read-only workdir → `WORKDIR_READ_ONLY` | PASS |
| 10 | final-component symlink refused → `SYMLINK_NOT_ALLOWED` | PASS |
| 11 | hard-linked file edit refused → `MULTIPLE_HARDLINKS_NOT_SUPPORTED` | PASS |
| 12 | hidden path refused → `HIDDEN_PATH_NOT_ALLOWED` | PASS |
| 13 | default-deny credential glob refused → `DENIED_PATH` | PASS |
| 14 | upload_binary_file (1 MiB, SHA-256 exact) | PASS |
| 15 | download_binary_file (1 MiB byte-exact round-trip) | PASS |
| 16 | binary atomic replace with expected_revision (overwrite=true) | PASS |
| 17 | concurrent racer against `renameat` replace (25 CAS rounds, zero torn writes) | PASS |
| 18 | Unicode paths (`数据/日本語-ファイル/README-🎉.txt`) | PASS |
| 19 | 8 MiB binary file at the limit boundary | PASS |
| 20 | over-limit binary refused → `BINARY_PAYLOAD_TOO_LARGE` | PASS |
| 21 | FD-bounded deep walk (60 nested levels) | PASS |
| 22 | delete_file with revision | PASS |
| 23 | delete_directory revision check → `REVISION_CONFLICT` on stale token | PASS |

The full automated suite additionally passes on this machine: **1118 passed / 0 failed**
(`pytest`, darwin-arm64), including the Darwin kernel contract tests (platform gate incl.
Rosetta/unmeasurable rejection, fcopyfile metadata preservation with mode+xattr+ACL, the full
Linux/Windows search-contract parity set run against the Darwin searcher, symlink traversal
attacks) and the doctor diagnostics suite.

## Notes

- The revision/CAS racer (check 17) demonstrates the documented concurrency boundary: the
  CAS + last-moment re-check held for every round; the non-cooperating external writer can
  still win individual rounds (REVISION_CONFLICT), which is the contract, not a defect.
- ACL/xattr preservation is proven by `tests/test_darwin_backend.py::TestMetadataPreservation`
  (mode 0o604 + user xattr + extended ACL carried onto the replacement inode via
  `fcopyfile(COPYFILE_METADATA)`, content untouched) on this machine.
- Large-file coverage here is 8 MiB (the binary channel limit). The spool path handles larger
  results; see the agent acceptance record.
