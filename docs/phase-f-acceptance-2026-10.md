# Phase F — Windows Acceptance Evidence (2026-10)

Execution host: WorkPC (Windows 11 x64 build 26200, local NTFS volumes,
Chinese-locale GBK console). All filesystem rows below were executed against
the real Rust kernel with the `cp312-abi3` wheel as the sole provider of
`serverfs_windows_native`; CI job counts are recorded with their run IDs.

Status legend: **Pass** = executed and green on this host;
**Not verified** = not executed here (reason recorded).

## 1. Branch and frozen implementation

- Branch `feat/v0.10-phase-f-acceptance`, based on `main` after the E2 merge
  (`b800f91`).
- No kernel or backend behaviour changes during Phase F except the
  acceptance-discovered launcher defect in section 6.

## 2. Clean-install wheel (exit criterion: no Docker/WSL/Rust/MSVC)

Two wheels only, fresh `uv venv`, module provenance asserted inside
`.venv-abi3.12/.venv-abi3.13\Lib\site-packages`:

| venv | CPython | Windows suites (native+backend+E2E+stdio+paths+doctor+bootstrap+cli+tunnel) | `serverfs --version` | `serverfs doctor` | stdio MCP session |
| --- | --- | --- | --- | --- | --- |
| .venv-abi3.12 | 3.12.10 | 196 passed, 2 skipped | Pass | exit 0, 0 FAIL / 0 WARN | (3.13 venv used) |
| .venv-abi3.13 | 3.13.3 | 196 passed, 2 skipped | Pass | exit 0, 0 FAIL / 0 WARN | create→stat→edit→stat, exact `b"alpha\r\nBETA\r\n"`, revision chain consistent |

Skipped rows are the two POSIX-only doctor cases (symlink root) on Windows.

This doubles as the **abi3 cross-minor evidence** required by the plan:
the same `cp312-abi3` wheel installs, imports, serves doctor and completes a
full mutation session on CPython 3.13.3 without rebuild.

## 3. Read/mutation parity on real NTFS

Executed in the clean wheel venvs (section 2 counts) and in CI:

- read-only and writable workdirs: all 11 tools;
- mutation chains, revision conflicts, atomic publication, metadata
  preservation (DACL-equivalence semantics), directory-emptiness scan;
- reparse/junction matrix: symlink and junction roots refused fail-closed at
  both the doctor pre-check and the kernel (`REPARSE_POINT_NOT_ALLOWED`);
  reparse as final component reports `type: "reparse_point"`, never followed
  by any mutation channel;
- concurrent host edit/rename: host-side rename/write/delete races against
  open sessions produce only defined codes (REVISION_CONFLICT / PATH_NOT_FOUND
  / sharing-violation containment), torn reads impossible (STATE-A/B
  uniformity assertions).

## 4. Long/deep/Unicode paths (`tests/test_windows_path_acceptance.py`, Pass)

- 24-level tree (multi-kilobyte absolute path, every component ~120 chars)
  carried through create→read→edit→stat→delete with atomic-publication temp
  verified fully cleaned; total absolute depth far exceeds the 260-character
  Win32 limit with no `LongPathsEnabled` registry change, because every
  request component opens HANDLE-relative under the `\\?\` root;
- find and search traverse to deep leaves; needle found exactly once;
- component boundary: 255 UTF-16 units accepted, 256 refused `INVALID_NAME`;
  emoji counted in UTF-16 units (128 pairs = 256 units → refused, 100 → OK);
- trailing dot/space ambiguity, embedded `\`, `:` refused before syscall;
- Unicode axes: NFC and NFD names are distinct files and read back their own
  bytes (NTFS performs no normalization); casefold-widened deny catches
  `ID_RSA`, `SeCrEt.PEM` and fullwidth variants without under-denying;
  reserved `.SERVERFS-TMP-*` is refused case-insensitively (and still refused
  when `allow_hidden=true`, via the reserved axis);
- `nul.txt` is a literal file in the handle-relative namespace (matches the
  Linux-visible behavior; the Win32 DOS-device-name surprise does not apply).

## 5. Metadata and storage-class rows

- Extended attributes: no-op on this machine's volumes measured during D1;
  fail-closed path pinned with API evidence (carried from D1 closure).
- Object IDs: positive fail-closed fixture (carried from D1 closure).
- doctor filesystem classification: local NTFS OK; exFAT/FAT/ReFS, network
  storage and unmeasurable volume class all `filesystem: FAIL` with exit 1
  (unit-tested classifier + end-to-end on WorkPC).

## 6. Launcher defect found by acceptance (fixed)

`tunnel-client` defaults its health server to `127.0.0.1:8080`; on WorkPC
that port is occupied and the client refused to start at all. The launcher
now always injects `HEALTH_LISTEN_ADDR` (default `127.0.0.1:0`, operator
overridable with `--health-listen-addr`) and validates a deployment-facing
`--base-url` control-plane override (https-only, fail-closed, redacted).
Regression coverage: `tests/test_native_tunnel.py` launcher-wiring cases.

## 7. Proxy acceptance (plan Phase F items 1–10) — Pass

Harness: `deployment/native/proxy_acceptance_harness.py` — real tunnel-client
v0.0.15 binary, real `serverfs tunnel` launcher chain, loopback HTTPS
control-plane stub (trusted via `SSL_CERT_FILE`), inspectable HTTP CONNECT
proxy with Basic-auth and failure modes. Per-scenario full process-tree kill
guarantees counters cannot cross-pollute. Final run (WorkPC, 2026-10-03,
transcript `serverfs-proxy-acceptance-ul8qbw6z`, 11/11 PASS, exit 0):

| # | Plan item | Scenario | Verdict |
| --- | --- | --- | --- |
| 1 | direct/no-proxy | no env file -> stub (TLS 401 poll loop) | Pass |
| 2 | HTTP proxy, no auth | CONNECT observed + TLS session completed to stub | Pass |
| 3 | HTTP proxy, username/password | credentials with `/ + @ : ? # %` round-tripped: percent-encoded by the launcher, decoded byte-exactly by the upstream HTTP auth layer | Pass |
| 4 | reserved-character credentials | covered inside item 3 (exact decode equality) | Pass |
| 5 | invalid proxy credentials | proxy answered 407; stub never reached; launcher stderr redacted (attempted password absent) | Pass |
| 6 | unreachable proxy | closed port -> client failure, stub never reached | Pass |
| 7 | proxy drops established connection | 200-then-close mode: client survived the drop and kept its error loop; no hang | Pass |
| 8 | restart/recovery through proxy | second scripted run re-established a fresh CONNECT + TLS session | Pass |
| 9 | child/container receives no credentials | real `forward_stdio` subprocess: child env lacks `CONTROL_PLANE_API_KEY`, `CONTROL_PLANE_HTTP_PROXY`, `SERVERFS_PROXY_*`, all proxy variables and every secret value; `PATH` preserved | Pass |
| 10 | proxy-disabled identical | blank `SERVERFS_PROXY_HOST=` behaves exactly like the no-file direct path (same rc class, zero proxy CONNECTs) | Pass |

Live extension (maintainer-supplied real HTTP proxy `proxy.cm.com:20171`,
`--live-proxy`): the launcher drove the real tunnel-client against the real
`https://api.openai.com` control plane with a deliberately fake key file;
the genuine control plane answered `401 Unauthorized` through the proxy
(`tunnel metadata fetch failed ... status_code=401`), and the key value was
asserted absent from all output. Pass. SOCKS5 (port 40170 of the same host,
when offered) is explicitly out of the v0.10 contract: there is no
configuration surface that accepts it, matching the plan's "must not claim
SOCKS5 support".

Harness defects found and fixed during this phase (harness-only, not product):
CONNECT request-line parsing left `HTTP/1.1` in the target, and the relay
initially line-buffered binary TLS bytes (a ClientHello without `0x0A`
stalled the handshake). Both replaced with raw byte-exact relay, unit-smoked
with a 256-byte newline-free payload.

## 8. Live tunnel E2E (ChatGPT → tunnel-client → supervisor → native ServerFS)

Maintainer-owned (AGENTS: end-to-end acceptance through ChatGPT belongs to
the maintainer). Runbook below; WorkPC cannot run it in parallel with the
live Docker deployment because the upstream stdio binding supports only one
active tunnel-client instance per tunnel ID.

Runbook (native profile, scheduled maintenance window):

1. Freeze agent traffic; on the Docker host: `docker compose stop openai-tunnel`
   (leave `serverfs-mcp` running or stopped — the tunnel owns the session).
2. On WorkPC, with wheels installed (section 2) and `serverfs.toml` pointing
   at the real acceptance workdirs:
   `serverfs doctor --config serverfs.toml` must exit 0 first.
3. `serverfs bootstrap tunnel-client` (pinned, verified) if not already present.
4. `serverfs tunnel --config serverfs.toml --tunnel-id <real> --api-key-file <secret outside every workdir>`
   (add `--env-file` if the proxy is needed; health binds to an ephemeral
   loopback port automatically).
5. In ChatGPT: confirm the connector shows the 11-tool surface; exercise
   read (`list_workdirs`, `read_text_file`), one guarded mutation roundtrip
   (`create_text_file` -> `edit_text_file` -> `delete_file`) in the writable
   acceptance workdir, and `download/upload_binary_file` if enabled.
6. Child-restart evidence: kill the `serverfs serve` child from Task
   Manager; tunnel-client must respawn it through the supervisor; a
   subsequent ChatGPT call succeeds.
7. Secret-boundary evidence: with Process Explorer (or the section-7 probe),
   confirm the `serverfs serve` child environment contains neither the
   Control Plane key nor proxy credentials.
8. Restore: stop `serverfs tunnel`, `docker compose start openai-tunnel`,
   verify the Docker deployment's session resumes, then record results here.

## 9. CI record

- PR #25 (E2) final head `9cd9daf`: Test 1047 passed / 10 skipped,
  native-kernel pass, Container check pass, wheel-release not applicable
  (no tag). Merged; `main` = `b800f91`.
- PR #26 (Phase F) head `a5290a7`: Test **1060 passed / 11 skipped**,
  native-kernel pass (2m34s, includes the new path-acceptance suite),
  Container check pass (29s), Publish skipping (normal for PRs).
- Linux root gate at each push covers: `ruff check`, `ruff format --check`,
  full `pytest` (incl. `test_wheel_release.py` helper matrix),
  `docker compose config`, image build.
- Windows native gate: cargo fmt/clippy/tests (symlink cases hard-required),
  Python/native set incl. Phase F suites, maturin wheel build + artifact
  acceptance in a clean venv.
- wheel-release workflow: executes only on a maintainer release tag; its
  version gate and release-notes logic are unit-tested on every push.

## 10. Release closure sequence

See `docs/phase-f-release-checklist.md`: version bump surface
(`pyproject` + `uv lock`, `native/windows/Cargo.toml` + lockfile, README/
AGENTS/site), tag + publish, and post-publish clean-machine verification.
The tag decision itself belongs to the maintainer; published stable tags
remain immutable.
