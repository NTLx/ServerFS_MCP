"""Read-only native Windows PythonW redirector/image identity proof.

The parent starts ONE short-lived, dedicated *interpreter-only* pythonw.exe
from core-owner-env. It does not import or start the Core, Task Scheduler,
Bridge, Connector, HTTP server or Agent Provider. Child exports safe booleans
into a disposable scratchpad file, which the parent reads and removes.
No full paths, PIDs, account names or credentials appear in the report.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

_FIELDS = frozenset(
    {
        "dedicated_venv_prefix",
        "isolated_no_bytecode_mode",
        "sys_executable_is_private_pythonw_launcher",
        "kernel_image_is_private_pythonw_launcher",
        "base_pythonw_executable_exists",
        "kernel_image_is_base_pythonw_executable",
        "base_pythonw_is_distinct_from_venv_launcher",
        "native_kernel_process_identity_available",
    }
)
_ALLOWED_ENV = frozenset(
    {
        "SYSTEMROOT",
        "WINDIR",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "TEMP",
        "TMP",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "PROGRAMDATA",
    }
)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve(strict=True))) == os.path.normcase(
            str(right.resolve(strict=True))
        )
    except (OSError, RuntimeError):
        return False


def _private_pythonw() -> tuple[Path, Path]:
    from serverfs_mcp.native_lifecycle import window_local_app_data

    prefix = window_local_app_data() / "ServerFS" / "core-owner-env"
    binary = prefix / "Scripts" / "pythonw.exe"
    if not binary.is_file():
        raise ValueError("dedicated Core pythonw binary is unavailable")
    return prefix, binary


def _child(output: Path) -> int:
    from serverfs_mcp.windows_process import process_identity

    prefix, binary = _private_pythonw()
    own = process_identity(os.getpid())
    base_raw = getattr(sys, "_base_executable", None)
    base = Path(base_raw) if isinstance(base_raw, str) and base_raw else None
    valid_base = (
        base is not None
        and base.is_absolute()
        and base.name.casefold() == "pythonw.exe"
        and base.is_file()
    )
    results = {
        "dedicated_venv_prefix": _same_path(Path(sys.prefix), prefix),
        "isolated_no_bytecode_mode": bool(sys.flags.isolated and sys.dont_write_bytecode),
        "sys_executable_is_private_pythonw_launcher": _same_path(Path(sys.executable), binary),
        "kernel_image_is_private_pythonw_launcher": (
            own is not None and _same_path(own.image_path, binary)
        ),
        "base_pythonw_executable_exists": bool(valid_base),
        "kernel_image_is_base_pythonw_executable": (
            bool(valid_base) and own is not None and _same_path(own.image_path, base)
        ),
        "base_pythonw_is_distinct_from_venv_launcher": (
            bool(valid_base) and not _same_path(base, binary)
        ),
        "native_kernel_process_identity_available": own is not None,
    }
    if set(results) != _FIELDS or any(type(v) is not bool for v in results.values()):
        return 2
    # Publish a complete boolean-only payload atomically. The parent must
    # never race with a half-written file from the redirector child.
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(results, sort_keys=True, separators=(",", ":")))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    return 0


def _parent() -> int:
    repo = Path(__file__).resolve().parents[2]
    prefix, pythonw = _private_pythonw()
    report = (
        repo
        / "docs"
        / "diagnostics"
        / (
            "v0250-windows-w2m-pythonw-image-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            + ".md"
        )
    )
    observed = False
    results: dict[str, bool] = {}
    error_class = "none"
    try:
        from serverfs_mcp.windows_cloudflare_runtime import _launch
        from serverfs_mcp.windows_job import WindowsJob

        scratch = repo / "scratchpad"
        if not scratch.is_dir():
            raise ValueError("existing trusted scratchpad unavailable")
        with tempfile.TemporaryDirectory(prefix="w2m-pythonw-", dir=scratch) as dirname:
            output = Path(dirname) / "boolean-only.json"
            env = {key: value for key, value in os.environ.items() if key.upper() in _ALLOWED_ENV}
            # Suspend before process execution, then assign to a fresh,
            # ANONYMOUS kill-on-close Job. This contains venv redirector
            # descendants and closes the same process tree even on timeout
            # or parent interruption. Never join any existing ServerFS Job.
            with WindowsJob() as job:
                process = _launch(
                    pythonw,
                    ["-I", "-B", str(Path(__file__).resolve()), "--child", str(output)],
                    env=env,
                    cwd=repo,
                    job=job,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    popen=subprocess.Popen,
                )
                try:
                    exit_code = process.wait(timeout=15)
                except subprocess.TimeoutExpired as exc:
                    # Job context closes before cleanup of disposable data,
                    # stopping only the one owned interpreter process tree.
                    job.close()
                    process.wait(timeout=5)
                    raise TimeoutError("one-shot PythonW did not exit") from exc
                finally:
                    job.close()
            if exit_code != 0:
                raise ValueError("one-shot PythonW interpreter did not exit successfully")
            payload = json.loads(output.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, dict)
                or set(payload) != _FIELDS
                or any(type(value) is not bool for value in payload.values())
            ):
                raise ValueError("interpreter identity probe did not return fixed booleans")
            results = payload
            observed = True
    except Exception as exc:  # noqa: BLE001 - no exception detail is disclosed
        error_class = type(exc).__name__

    lines = [
        "# Windows v0.25.0 W2M — dedicated PythonW kernel image proof",
        "",
        "UTC: " + datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Overall: " + ("OBSERVED" if observed else "INCOMPLETE"),
        "Probe error class: " + error_class,
        "",
        "## Fixed-key boolean observations",
        *("- " + field + ": " + str(results.get(field, "UNVERIFIED")) for field in sorted(_FIELDS)),
        "",
        "Only a short-lived, dedicated Core venv PythonW interpreter was run.",
        "No Core/Bridge/Connector/Agent Provider module was imported or started.",
        "No Task Scheduler task, service, HTTP endpoint or credential was accessed.",
        "Only boolean path comparisons are recorded, never private paths or PIDs.",
        "The temporary boolean data file is removed on normal exit.",
    ]
    try:
        with report.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write("\n".join(lines) + "\n")
    except OSError:
        print("W2M_PYTHONW_IMAGE=REPORT_WRITE_FAILED", file=sys.stderr)
        return 2
    print("W2M PythonW image proof report: " + str(report))
    return 0 if observed else 2


def main(argv: list[str]) -> int:
    if sys.platform != "win32" or os.name != "nt":
        print("W2M_PYTHONW_IMAGE=WINDOWS_REQUIRED", file=sys.stderr)
        return 2
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "src"))
    try:
        if not argv:
            return _parent()
        if len(argv) == 2 and argv[0] == "--child":
            return _child(Path(argv[1]))
    except Exception as exc:  # noqa: BLE001 - never expose interpreter paths
        print(f"W2M_PYTHONW_IMAGE=FAILED ({type(exc).__name__})", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
