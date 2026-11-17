# macOS v0.25.0 same-source Bridge wheel install — 2026-11-16

## Result

**PASS — Agent Bridge 0.25.0 was built from candidate A, validated, and installed offline into a fresh isolated venv under candidate A.** This establishes a single private input-source parity for a subsequent isolated native test. It does not establish an immutable shared SHA, run or modify a native service, or constitute release acceptance.

## Candidate and preflight

The source was the canonical path:

```text
/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41
```

Before any writes, candidate A was verified as a directory owned by UID **501**, mode **0700**, with no root `.git`. Its root `pyproject.toml`, `agent_bridge/pyproject.toml`, and root `plugin.json` declared **0.25.0**. Both requested new paths, `bridge-venv` and `bridge-wheel-same-source`, were absent and not symlinks.

The read-only candidate source gate passed before work and again immediately before this evidence file was created: **203 reviewed paths**, **2441 excluded scratch entries**, base HEAD `da9c12e600d3b336f1fd7a3d5232702e9abbc19f`. This document is a new untracked path not yet in that reviewed manifest; add it explicitly to the manifest in the following task before using the source gate again. `git diff --check` passed before this document was added and was rerun after it was written.

`UV_CACHE_DIR` and `UV_NO_CACHE` were unset; uv used its populated default cache. From candidate root and candidate `agent_bridge/` respectively, `uv lock --check --offline` passed (40 and 50 packages). The candidate Bridge lock contained **49 third-party records**, all equal to the live checkout's 49 records. Only the local package version differed: candidate **0.25.0**, live checkout **0.24.2**. No source checkout, lock, or live installation was changed by this task.

## Wheel build and validation

From candidate A `agent_bridge/`, this command built the single wheel into the requested new directory:

```sh
uv build --offline --wheel --out-dir /Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41/bridge-wheel-same-source
```

The only wheel there is `serverfs_agent_bridge-0.25.0-py3-none-any.whl`:

- SHA-256: `80721da07b18adedff5bf0244f5d17599aff5389555ded1acb48c74ea2b4ab79`
- Size: **213,146 bytes**
- METADATA Name/Version: **serverfs-agent-bridge 0.25.0**
- RECORD: every archive member had a matching row; all SHA-256 hashes and sizes validated, including the blank self-entry convention for RECORD.
- Payload: exactly **50** files; no missing, extra, or byte-different files versus candidate A `agent_bridge/src/serverfs_agent_bridge`.

The source inventory aggregate SHA-256 below is computed in sorted relative-path order over each UTF-8 relative path, NUL, file bytes, and newline:

```text
3c166a0050f814f94523075e31628c1fb83de65a84b2cb67714a525e38870c3e
```

Sorted per-file source SHA-256 inventory:

```text
e84f8d603c0625b88cd418b2ee7be25ad614cd04b9b3f8070496715889e91f84  __init__.py
065ce7bf63cb54e7b49c83aaee58856d646cc251bbaddfb2d27fc219fd780d60  adapters/__init__.py
b737fcf7ede2e2a2688e157780f45d1e44932d6827017d5a15b69a3b45901cba  adapters/acp.py
21c73ace40646386a92a9d9730b25a20b1044daa04663ce818a9a0f3119c12d9  adapters/acp_process.py
48bf0cf6bc01fd57e0ef70fc89761b9777af7859e119b8c3debd5d7be3c59ce4  adapters/acp_process_windows.py
00a9ca79fabc6dbea2901ce2390b573d3b415a0e79ca7b995457e5d58895f699  adapters/base.py
52f2d1bed2e6cd51c89ccb5973dcbb912aa27ebf1e3250c208ac2cb374ba7acd  adapters/claude.py
dd4978b0d54c7c135f1d9edadde09d4c514d9d9a961a9d508988336bfc1aad8c  adapters/codex.py
06fa6295c78db170a1e855fa257a06edc52900086134e87a139cf1bfc279f4a8  adapters/codex_posix.py
ad83f31aa515c6dac4e89a6e2c890e730a9e1d99f76316ec2d2c7f9d501b5d52  adapters/codex_transport.py
1714c92d6ecc8b58f293e76e3ccae55de4b89b3cfc7d5d134385e763ca2d06ee  adapters/codex_windows.py
47b2c93793168e685bcf3355758f17e3f8c7bca9d15c6c17e723054579d371c6  adapters/fake.py
49b6c3510fb9b4c2def6284fcb269ad01b475373fe4b029d86c3e871c165dec9  adapters/qoder.py
ecc93c1094d1b1f3a163eecd5ee3b0c6fba4cf25a1a2cc9c2bd672abcd74073b  bootstrap.py
9b1a6aca8f917d17d6b87994c768bed64480f56227d106bf324bfa6f9a00e8e2  config.py
753e257db9bdbdb6738e8fccddd2d3b5e40637607ba02370a3a215fb30f4bcfc  core_grants.py
8bcc741c419b609986b1d829bab29776fbe5a2432d8f4b68fb06af4301c3a386  core_grants_private.py
873362e5b0ca31ab9cbea740daa0b68ef840bdc86ad67d4a8a63e2c7fe7a3dfc  data_home.py
7f50486f976832b421cbcb0252de41ac56a67f540c5e5e02c5b1846caf0bf701  diagnostics.py
9bce64bbd11885afc5222b75a798635de6b562b97f03f7dd57deb118210d5ac4  errors.py
2746b8fe768092d43b90acb4fe68f9d7bb3bc9c057f01d8bed0bf64907a5ee4d  inspect_state.py
5297cc4846284d5de32859d18db1111e1787c41d45e7380f647304987ca47260  lease_identity.py
8369cc056cb8ff13f80273456c592f276fbaa273fb870b3f02c154d17754de14  leases.py
ecb1caed879c9658ec6ff73c99ec69c2f65241460939233ddbe70c580b174b08  local_ipc.py
21a1739993037ce75b7ccee28ff64bb0bdc34a3fb907264cfe270701f32b5750  macos_owned_config.py
c6ba9425ddb3dcf57b8049fd7fd96135bfc81fbd6b8ec22752253f515b2ea5d5  main.py
5a82f289e20d3fdcdadd54975dfb80b919a76a9e534621ff7ccc106144200f77  manifest.py
876e4aa07afbcf8ce86cb50c434730661692ded1d328643f9ce89881e1b39026  models.py
0291fb52437cacc0c0ce35d3647b4d0d098403e1d1db1ac644dc7f5b6d36bafb  observability.py
e78b464aa0cc76118ce4f88ad354305504114766a360f3a9540c998ab351e38a  platform_seams.py
a0a5ce09baf021d2457c81b44ca6c5267168c422d1f91ac36de21f4aa91eafdc  policy.py
34f2185d4b1a11ebeffd91b5fcc6c121d8cc20e83a7e332abbe9b45ff0c17d29  preflight.py
29e0d5adc412709a693910d7c7e2cfc3cd0f6745b104550a509c5a8577aafabf  private_state.py
8242a688d622e3e5ac96966cf2f47099261de62a5c1ebb41aced5081d45ab68c  protocol.py
6a92728dcca40731a9e908847da303d1a33b14ced1b9d24e053b3b0f38931b31  recovery.py
699ef57d53732b0e2a9bf818372ac17d66fcee6b52de642780bf7783b5ef57bb  render_config.py
a0d2db78b88c61428560f14b486400e6d62baa00413252991e29cbd2c6b3147e  repo_ops.py
7f52158bc9b66282e67e7c3719af9516c171d718cdf0f91e6ad4c701dc395cbe  repo_ops_windows.py
3d5e91816de4f47e9a591dcf32fa234d59fcac252c83a12fcf6d0785c23bcdb2  repo_release.py
34c08e33490008c31f30f33a0d4c60915b2af87c6ea01f96b560bc0740f0a2bf  result_spool.py
4cc39048541aa76c577723098f6d1600a491da8dd54d2f9de45251db919a6c1a  runtime_proxy.py
080864c9815af89c89c559e6e7c7c0b4eec86f840070fd57e78196d7a069bb6f  service.py
ebf811172113748d9ebf3388ffc385d73af9a8523a02485aa396c66932fc8753  state.py
380d032f37e7401dfc10749644537ad26dc424f885fe9e832329d06ab9878c9b  store.py
bb6bef54fe4699acba9dfa06e03102314b4094b297d006f2542871884ad5494e  sync_config.py
cf3b9d54a53dc00367f8778a04ff06536e011deec0cfa2759ef5d40e6d1bc8c1  util.py
8980d986c1ec2b12c08a1c2659e49474d64700f020beb3516dc12ea6f44c4ce2  windows_ipc.py
8d8b6f5d9736d2c108cf412f778617dcf7e5a586c7887b39fc829cff277667c3  windows_lease.py
a548bfab70be136881899360537c180ed6f19555318c4a3cca7cfe8cec05807c  windows_pipe.py
fcc0d1884f999b1e0c09cae75809a6857c055a41bea0642c7241dd70a1ffca88  windows_security.py
```

## Installed environment

The local CPython **3.12.12** interpreter created `bridge-venv` with `uv venv --offline --python 3.12`. The exact wheel above was installed with `uv pip install --offline --python <A>/bridge-venv/bin/python <exact-wheel-path>`; **40 packages** were installed from the existing cache. `uv pip check` passed for all 40.

An isolated `bridge-venv/bin/python -I -B` probe reported distribution metadata **serverfs-agent-bridge 0.25.0**. Both `serverfs_agent_bridge.main` and `serverfs_agent_bridge.core_grants_private` resolved inside:

```text
/Users/lx/Projects/ServerFS_MCP/deployment/macos/.serverfs-v0250-candidate-9f3c2a7b6d41/bridge-venv/lib/python3.12/site-packages
```

The wheel's installed `direct_url.json` identifies the exact wheel path under candidate A. No editable install or source-checkout fallback was used. Both `bridge-venv` and `bridge-wheel-same-source` are retained under A for the next isolated native test.

## Boundaries and postflight

No network, dependency download/sync, native service test, SMAppService launch/registration, launchd mutation, process launch/signal, public ingress, Git staging/commit/push/tag, or modification of the live checkout, candidate B, or older candidate scratch occurred. Only the two requested new A directories and this one evidence document were written.

The four protected launchd labels and PIDs were read-only checked after installation and remained:

| Label | PID |
| --- | ---: |
| `com.ntlx.serverfs.tunnel` | 1779 |
| `com.ntlx.serverfs.bridge` | 1790 |
| `com.ntlx.serverfs.http-origin` | 40516 |
| `com.ntlx.serverfs.cloudflare` | 66419 |

`git diff --check` passed after writing this document. This document remains a new untracked release-candidate source requiring explicit addition to the reviewed manifest in the next task. No exact shared Git SHA or same-immutable-SHA claim is made.
