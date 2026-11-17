# macOS v0.25.0 signed preflight residual cleanup — 2026-11-15

## Result: NOT VERIFIED; no cleanup performed

This bounded cleanup stopped at the read-only safety gate. Neither the
instance candidate nor the scratch directory was modified or removed.

The requested identity was constructed exactly as
`InstanceIdentity('shpf-a-0958931b80', 'shared', 'shpf-owner')`. The exact
instance candidate was
`~/Library/Application Support/ServerFS/instances/shpf-a-0958931b80`; the
exact scratch candidate was `/tmp/shpf-0958931b80-0cs3xq79`.

## Read-only evidence

- Product `_read_json` accepted the root manifest, and its identity/schema
  shape matched the requested named shared Core. Manifest contents were not
  included in this report.
- The expected instance entries were present; its three private JSON files
  passed current-user/mode checks. `logs` and `data` were private and empty.
- Product signed-app verification passed. Manager status was `not-found`;
  the fixed strict launchd parser returned literal `False`, with no candidate
  PID.
- The scratch path resolved to the requested name, was a current-user-owned
  mode-0700 directory, had a ctime cluster on the recorded failed-run date,
  and its top-level inventory matched the test-generated entries observed.
- No matching test owner/Core process or open scratch file was found.
- Protected production launchd PID and process start-time baselines all
  matched: Tunnel 1779, Bridge 1790, Cloudflare 99279, HTTP 99271.

## Failed gates and retained state

- The complete instance directory ancestor chain did not pass the required
  owner/UID/mode check. The precise failing ancestor was not pursued.
- A symlink was present somewhere inside the scratch tree. Its location was
  not pursued. Therefore the scratch tree was not eligible for removal.
- `shutil.rmtree.avoids_symlink_attacks` was not used as a cleanup gate because
  the earlier read-only gates had already failed.

Consequently `unstage_shared_core(identity)` was not called, and no native
register/stop/unregister or Manager mutation was attempted. Both exact
candidates remain retained. No other scratch paths, instances, production
services, wheels, or Git state were touched. No test was run.

After writing this report, both exact candidate roots were re-read and remained
present as UID 501 mode-0700 directories. The four protected launchd PID and
start-time baselines were re-read and still matched.
