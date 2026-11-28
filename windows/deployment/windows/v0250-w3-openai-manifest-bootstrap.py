"""Prepare a disabled-capable v25 OpenAI HTTP Connector from the live legacy owner.

Only newly created, owner-private v25/services/openai files may be changed.
Never edits, re-ACLs or stops the old OpenAI Tunnel/Agent Bridge, and never
prints tunnel IDs, keys, file paths, hashes, SIDs or provider credentials.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import sys
from pathlib import Path

STAGE = "preflight"


def require(ok: bool) -> None:
    if not ok:
        raise ValueError("OpenAI owner or protected source differs")


def validated_legacy_key_payload(value: bytes) -> bytes:
    """Return the secret bytes; allow only an optional final LF or CRLF.

    Existing Windows api-key files commonly end in CRLF. Preserve their exact
    raw file bytes when copying; reject embedded CR, LF and NUL so an invalid
    multiline credential is never normalized or silently accepted.
    """
    payload = (
        value[:-2] if value.endswith(b"\r\n") else value[:-1] if value.endswith(b"\n") else value
    )
    require(
        32 <= len(payload.strip()) <= 4096
        and b"\x00" not in payload
        and b"\r" not in payload
        and b"\n" not in payload
    )
    return payload


def digest(path: Path) -> bytes:
    result = hashlib.sha256()
    with path.open("rb") as reader:
        for data in iter(lambda: reader.read(1024 * 1024), b""):
            result.update(data)
    return result.digest()


def main() -> None:
    global STAGE
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.native_tunnel import _TUNNEL_ID
    from serverfs_mcp.tunnel_bootstrap import default_tunnel_client_path
    from serverfs_mcp.windows_cloudflare_security import (
        _no_reparse_components,
        _read_windows_file_sddl,
        _sddl_sections,
        _within,
        validate_private_file,
    )
    from serverfs_mcp.windows_private_artifacts import protect_new_file
    from serverfs_mcp.windows_service import (
        _manifest_document,
        _read_env_file,
        default_api_key_path,
    )
    from serverfs_mcp.windows_v25 import cli, runtime, storage
    from serverfs_mcp.windows_v25.spec import Manifest
    from serverfs_mcp.windows_v25.tasks import Scheduler

    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
    )
    root = storage.root()
    current = root / "envs" / "openai"
    require(
        os.path.normcase(str(Path(sys.prefix).resolve(strict=True)))
        == os.path.normcase(str(current.resolve(strict=True)))
    )
    sid = current_user_sid()
    core = storage.load("core")
    cf = storage.load("cloudflare")
    statuses = (Scheduler().status(core), Scheduler().status(cf))
    require(
        all(x["running"] and x["enabled"] for x in statuses)
        and statuses[0]["readiness"] == "verified"
    )
    require(
        core.settings["port"] == 18767 and core.settings["hostname"] == "workpc-serverfs.ntlx.top"
    )
    STAGE = "legacy-authority"
    old = _manifest_document()
    require(
        old.get("provider", "openai") == "openai"
        and old.get("task_name") == "ServerFS Native Tunnel"
        and old.get("user_sid") == sid
    )
    config, roots = runtime._roots(Path(str(core.settings["config"])))
    require(Path(str(old["config_path"])).resolve(strict=True) == config)
    old_env = _read_env_file(Path(str(old["env_file"])))
    tunnel_id = old_env.get("CONTROL_PLANE_TUNNEL_ID", "")
    require(bool(_TUNNEL_ID.fullmatch(tunnel_id)))
    repo = Path(__file__).resolve().parents[2]

    STAGE = "old-control-plane-key"
    old_key = default_api_key_path()
    _no_reparse_components(old_key)
    keyinfo = old_key.lstat()
    require(
        stat.S_ISREG(keyinfo.st_mode)
        and not (keyinfo.st_file_attributes & 0x400)
        and keyinfo.st_nlink == 1
        and 32 <= keyinfo.st_size <= 4096
        and _sddl_sections(_read_windows_file_sddl(old_key)).get("O") == sid
    )
    keypath = old_key.resolve(strict=True)
    require(not any(_within(keypath, p) for p in (repo, *roots)))
    old_key_bytes = old_key.read_bytes()
    # Preserve the exact legacy file bytes; only validate their contents in
    # memory, never log/store them in the checkout or TaskScheduler XML.
    # CRLF is a normal terminal newline on Windows, not a secret value.
    # Keep the exact historical bytes for the new private file.
    validated_legacy_key_payload(old_key_bytes)

    STAGE = "known-tunnel-binary"
    source = default_tunnel_client_path()
    require(source is not None)
    _no_reparse_components(source)
    info = source.lstat()
    require(
        source.name.casefold() == "tunnel-client.exe"
        and stat.S_ISREG(info.st_mode)
        and not source.is_symlink()
        and not (info.st_file_attributes & 0x400)
        and info.st_nlink == 1
        and 1024 * 1024 <= info.st_size <= 200 * 1024 * 1024
    )

    # Bootstrap bundles tunnel-client.exe with its cloudflared.exe runtime.
    # Copying only the main executable would break the connector process.
    cloudflared_source = source.parent / "cloudflared.exe"
    _no_reparse_components(cloudflared_source)
    child_info = cloudflared_source.lstat()
    require(
        stat.S_ISREG(child_info.st_mode)
        and not cloudflared_source.is_symlink()
        and not (child_info.st_file_attributes & 0x400)
        and child_info.st_nlink == 1
        and 1024 * 1024 <= child_info.st_size <= 200 * 1024 * 1024
    )

    STAGE = "existing-core-auth"
    core_config, auth = runtime._core_configuration(core)
    require(core_config == config)
    api_key = auth.get("SERVERFS_HTTP_API_KEY", "")
    require(isinstance(api_key, str) and re.fullmatch(r"[A-Za-z0-9_-]{40,}", api_key))
    header = ("Bearer " + api_key).encode("ascii")
    require(old_key_bytes.strip() != api_key.encode("ascii"))

    STAGE = "target-private-boundary"
    home = storage.home("openai")
    storage.check_private(home, directory=True, sid=sid)
    pyw = storage.pinned_pythonw("openai")
    storage.check_private(pyw, directory=False, sid=sid)
    require(pyw.lstat().st_nlink == 1)
    target_binary = home / "tunnel-client.exe"
    target_cloudflared = home / "cloudflared.exe"
    target_key = home / "control-plane.key"
    target_header = home / "core-auth.header"
    for target in (
        target_binary,
        target_cloudflared,
        target_key,
        target_header,
        home / "service.json",
    ):
        require(not target.exists() and not target.is_symlink())

    manifest = Manifest.parse(
        {
            "schema": 1,
            "component": "openai",
            "sid": sid,
            "pythonw": str(pyw),
            "settings": {
                "tunnel_client": str(target_binary),
                "tunnel_id": tunnel_id,
                "control_plane_key_file": str(target_key),
                "core_auth_header_file": str(target_header),
                "port": 18767,
                "hostname": "workpc-serverfs.ntlx.top",
            },
        }
    )
    require(not Scheduler().status(manifest)["registered"])

    created: list[Path] = []
    try:
        STAGE = "private-binary-copy"
        before_hash = digest(source)
        with source.open("rb") as reader, target_binary.open("xb") as writer:
            created.append(target_binary)
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        protect_new_file(target_binary, sid)
        storage.check_private(target_binary, directory=False, sid=sid)
        require(
            target_binary.lstat().st_nlink == 1
            and before_hash == digest(target_binary) == digest(source)
        )
        STAGE = "private-cloudflared-runtime-copy"
        cloudflared_hash = digest(cloudflared_source)
        with cloudflared_source.open("rb") as reader, target_cloudflared.open("xb") as writer:
            created.append(target_cloudflared)
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        protect_new_file(target_cloudflared, sid)
        storage.check_private(target_cloudflared, directory=False, sid=sid)
        require(
            target_cloudflared.lstat().st_nlink == 1
            and cloudflared_hash == digest(target_cloudflared) == digest(cloudflared_source)
        )
        STAGE = "private-key-copy"
        with target_key.open("xb") as stream:
            created.append(target_key)
            stream.write(old_key_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        protect_new_file(target_key, sid)
        validate_private_file(
            target_key,
            checkout_root=repo,
            workdir_roots=roots,
            user_sid=sid,
        )
        require(target_key.read_bytes() == old_key_bytes)

        STAGE = "private-core-header"
        with target_header.open("xb") as stream:
            created.append(target_header)
            stream.write(header)
            stream.flush()
            os.fsync(stream.fileno())
        protect_new_file(target_header, sid)
        validate_private_file(
            target_header,
            checkout_root=repo,
            workdir_roots=roots,
            user_sid=sid,
        )
        require(target_header.read_bytes() == header)

        STAGE = "new-v25-manifest"
        cli._preflight(manifest)
        storage.stage(manifest)
        require(storage.load("openai") == manifest)
    except Exception:
        if not (home / "service.json").exists():
            # Cleanup only privately owned newly-created files from this run.
            for path in reversed(created):
                try:
                    storage.check_private(path, directory=False, sid=sid)
                    path.unlink()
                except Exception:
                    pass
        raise
    STAGE = "accepted"
    print("V25_W3_OPENAI_PRIVATE_TUNNEL_BINARY=PASS")
    print("V25_W3_OPENAI_PRIVATE_BUNDLED_CLOUDFLARED=PASS")
    print("V25_W3_OPENAI_CONTROL_PLANE_KEY_COPIED_PRIVATE=PASS")
    print("V25_W3_OPENAI_CORE_HEADER_CREATED_PRIVATE=PASS")
    print("V25_W3_OPENAI_MANIFEST_CONFIG_PARITY=PASS")
    print("V25_W3_OPENAI_MANIFEST_STAGED=PASS")
    print("V25_W3_OPENAI_TASK_REGISTERED=False")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            "V25_W3_OPENAI_BOOTSTRAP=FAILED:" + type(exc).__name__ + " stage=" + STAGE,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
