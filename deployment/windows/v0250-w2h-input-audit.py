"""W2h read-only private input/owner inventory for disabled task staging.

Run only from the Windows source tree .venv interpreter, in a normal current
user session.  Reads trusted legacy manifests and filesystem metadata. Does not
read token or key file contents, invoke providers, register tasks, or change ACL.
Output is one safe JSON object containing names, statuses and counts ONLY.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path


def _main() -> int:
    if os.name != "nt" or sys.platform != "win32":
        print("W2H_INPUT_AUDIT=WINDOWS_REQUIRED", file=sys.stderr)
        return 2

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "src"))
    from serverfs_mcp.native_config import load_native_config
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.native_lifecycle import window_local_app_data
    from serverfs_mcp.windows_cloudflare_security import (
        _no_reparse_components,
        _read_windows_file_sddl,
        _validate_private_sddl,
    )
    from serverfs_mcp.windows_core_control import _check_private

    sid = current_user_sid()
    base = window_local_app_data() / "ServerFS"
    homes = {
        "core": base / "service-manager/windows/mcp-core",
        "bridge": base / "service-manager/windows/agent-bridge",
        "cloudflare": base / "service-manager/windows/connectors/cloudflare",
        "openai": base / "service-manager/windows/connectors/openai",
    }
    venvs = {
        "bridge_owner": base / "bridge-owner-env",
        "bridge_runtime": base / "bridge-runtime-env",
        "core_owner": base / "core-owner-env",
        "cloudflare_connector": base / "cloudflare-connector-env",
        "openai_connector": base / "openai-connector-env",
    }

    result: dict[str, object] = {
        "legacy_default_manifest": "UNVERIFIED",
        "legacy_cloudflare_manifest": "UNVERIFIED",
        "config_same_in_legacy_instances": "UNVERIFIED",
        "native_config": "UNVERIFIED",
        "core_port_18766_free": "UNVERIFIED",
        "core_public_hostname_match": "UNVERIFIED",
        "legacy_cloudflare_binary": "UNVERIFIED",
        "legacy_cloudflare_token_metadata": "UNVERIFIED",
        "legacy_default_key_metadata": "UNVERIFIED",
        "legacy_origin_auth_metadata": "UNVERIFIED",
        "legacy_default_env_placement": "UNVERIFIED",
        "candidate_envs": {},
        "new_service_homes": {},
    }

    def private_metadata(path: Path) -> str:
        try:
            if not path.is_file():
                return "ABSENT"
            _no_reparse_components(path)
            _validate_private_sddl(_read_windows_file_sddl(path), sid)
            return "CURRENT_USER_PRIVATE"
        except Exception:
            return "NOT_VERIFIED_PRIVATE"

    # Never create/repair anything and never traverse the contents of homes.
    for key, path in homes.items():
        try:
            if not path.exists() and not path.is_symlink():
                result["new_service_homes"][key] = "ABSENT"
            else:
                _no_reparse_components(path)
                _check_private(path, directory=True, sid=sid)
                result["new_service_homes"][key] = "EXISTS_PRIVATE"
        except Exception:
            result["new_service_homes"][key] = "EXISTS_UNVERIFIED"

    for key, path in venvs.items():
        try:
            for entry in (
                path,
                path / "Scripts",
                path / "Lib",
                path / "Lib" / "site-packages",
            ):
                _check_private(entry, directory=True, sid=sid)
            for name in ("python.exe", "pythonw.exe"):
                _check_private(path / "Scripts" / name, directory=False, sid=sid)
            result["candidate_envs"][key] = "PRIVATE_PYTHON_OK"
        except Exception:
            result["candidate_envs"][key] = "NOT_VERIFIED"

    from serverfs_mcp import windows_cloudflare_parallel, windows_service
    from serverfs_mcp.cloudflare_bootstrap import (
        WINDOWS_BINARY_SHA256,
        _sha256,
    )

    default = None
    cloudflare = None
    try:
        default = windows_service._manifest_document()
        if default.get("task_name") != "ServerFS Native Tunnel":
            raise ValueError("legacy task identity mismatch")
        result["legacy_default_manifest"] = "VALID"
    except Exception:
        pass
    try:
        cloudflare = windows_cloudflare_parallel._manifest_document()
        result["legacy_cloudflare_manifest"] = "VALID"
    except Exception:
        pass

    if isinstance(default, dict):
        source_key = windows_service.default_api_key_path()
        result["legacy_default_key_metadata"] = private_metadata(source_key)
        source_env = default.get("env_file")
        if isinstance(source_env, str) and Path(source_env).is_absolute():
            try:
                _no_reparse_components(Path(source_env))
                result["legacy_default_env_placement"] = (
                    "INSIDE_REPOSITORY"
                    if Path(source_env).resolve().is_relative_to(root.resolve())
                    else "OUTSIDE_REPOSITORY"
                )
            except Exception:
                result["legacy_default_env_placement"] = "UNVERIFIED"

    if isinstance(cloudflare, dict):
        result["legacy_cloudflare_token_metadata"] = private_metadata(
            Path(cloudflare["token_file"])
        )
        result["legacy_origin_auth_metadata"] = private_metadata(Path(cloudflare["env_file"]))
        try:
            binary = Path(cloudflare["cloudflared"])
            _no_reparse_components(binary)
            result["legacy_cloudflare_binary"] = (
                "BINARY_SHA256_PINNED"
                if binary.is_file() and _sha256(binary) == WINDOWS_BINARY_SHA256
                else "BINARY_NOT_VERIFIED"
            )
        except Exception:
            result["legacy_cloudflare_binary"] = "BINARY_NOT_VERIFIED"
        result["core_public_hostname_match"] = (
            "PASS" if cloudflare.get("hostname") == "workpc-serverfs.ntlx.top" else "FAIL"
        )

    if isinstance(default, dict) and isinstance(cloudflare, dict):
        first = default.get("config_path")
        second = cloudflare.get("config_path")
        if isinstance(first, str) and isinstance(second, str):
            try:
                same = Path(first).resolve(strict=True) == Path(second).resolve(strict=True)
                result["config_same_in_legacy_instances"] = "PASS" if same else "FAIL"
                if same:
                    workdirs, settings = load_native_config(Path(first))
                    result["native_config"] = (
                        "CONFIG_WITH_WORKDIRS_AND_BRIDGE"
                        if workdirs and settings.bridge_required
                        else "CONFIG_MISSING_BRIDGE_OR_WORKDIRS"
                    )
            except Exception:
                result["native_config"] = "NOT_VERIFIED"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 18766))
        result["core_port_18766_free"] = "PASS"
    except OSError:
        result["core_port_18766_free"] = "FAIL"

    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main())
    except Exception as exc:
        # Fail closed without exposing any private paths, token values, or SIDs.
        print(f"W2H_INPUT_AUDIT=FAILED ({type(exc).__name__})", file=sys.stderr)
        raise SystemExit(2) from None
