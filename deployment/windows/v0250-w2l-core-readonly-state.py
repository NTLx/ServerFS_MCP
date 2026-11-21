"""W2L safe read-only snapshot of the installed, *disabled* new Core owner.

Run with the dedicated core-owner-env Python, -I -B. Nothing is started,
enabled, stopped or installed.  Only a fixed set of non-secret status labels
is printed, never process IDs, image paths, SIDs, keys or exception messages.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    if sys.platform != "win32" or os.name != "nt":
        print("W2L_CORE_STATE=FAILED (WindowsRequired)", file=sys.stderr)
        return 2
    try:
        from serverfs_mcp import windows_core_control as core
        from serverfs_mcp.native_lifecycle import window_local_app_data
        from serverfs_mcp.windows_process import process_identity

        expected = window_local_app_data() / "ServerFS" / "core-owner-env"
        prefix = Path(sys.prefix).resolve(strict=True)
        if prefix != expected.resolve(strict=True) or not sys.flags.isolated:
            raise ValueError("not the dedicated isolated Core environment")
        snap = core.status()
        if not (
            snap.get("registered") is True
            and snap.get("task_valid") is True
            and snap.get("enabled") is False
            and snap.get("manifest_installed") is True
            and snap.get("core_alive") is False
        ):
            raise ValueError("new Core is not conclusively disabled/offline")

        state = core._read_state()
        stage = state.get("stage")
        if stage not in ("disabled-never-started", "owner-started"):
            stage = "UNVERIFIED"
        has_generation_fields = (
            type(state.get("core_pid")) is int
            and type(state.get("core_creation_time")) is int
            and isinstance(state.get("core_image_path"), str)
            and bool(state.get("core_image_path"))
        )
        own = process_identity(os.getpid())
        # A venv's python.exe may be a redirector: never conflate
        # sys.executable with the actual image reported by the Windows kernel.
        venv_python = expected / "Scripts" / "python.exe"
        venv_launcher_verified = os.path.normcase(
            str(Path(sys.executable).resolve(strict=True))
        ) == os.path.normcase(str(venv_python.resolve(strict=True)))
        if not venv_launcher_verified:
            raise ValueError("dedicated venv launcher identity is not verified")
        image_matches_sys_executable = own is not None and os.path.normcase(
            str(own.image_path)
        ) == os.path.normcase(str(Path(sys.executable).resolve(strict=True)))

        # Windows venv python.exe may be a redirector: the kernel's actual
        # image can be CPython's installed base interpreter instead of
        # sys.executable. Compare ONLY against CPython's own explicit
        # sys._base_executable, not against an arbitrary python.exe basename.
        base_candidate = getattr(sys, "_base_executable", None)
        base_verified = False
        kernel_matches_base = False
        if isinstance(base_candidate, str) and base_candidate:
            base_path = Path(base_candidate)
            if base_path.is_absolute() and base_path.name.casefold() == "python.exe":
                try:
                    real_base = base_path.resolve(strict=True)
                    base_verified = real_base.is_file()
                    kernel_matches_base = (
                        base_verified
                        and own is not None
                        and os.path.normcase(str(own.image_path))
                        == os.path.normcase(str(real_base))
                    )
                except OSError:
                    base_verified = False

        result = {
            "dedicated_env": "VERIFIED",
            "core_registered": True,
            "core_disabled": True,
            "core_offline": True,
            "core_listener_owner": str(snap.get("listener_owner", "UNVERIFIED")),
            "core_last_state_stage": stage,
            "core_state_has_generation_identity": has_generation_fields,
            "probe_python_process_image_matches_sys_executable": image_matches_sys_executable,
            "probe_python_venv_launcher_verified": venv_launcher_verified,
            "probe_python_base_executable_verified": base_verified,
            "probe_kernel_image_matches_python_base_executable": kernel_matches_base,
        }
        if result["core_listener_owner"] not in ("verified", "not_verified"):
            raise ValueError("unknown listener verification state")
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:  # noqa: BLE001 - redact any private values
        print(f"W2L_CORE_STATE=FAILED ({type(exc).__name__})", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
