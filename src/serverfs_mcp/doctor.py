"""Full ``serverfs doctor`` health report (v0.10 Phase E2, §26).

Read-only diagnostics for operators: configuration parse, backend
availability, per-workdir root acquisition, filesystem class, reparse
topology, non-mutating read/write capability probes, tunnel-client
presence/version and the project-managed HTTP proxy status.

Rules this module must never break:

- output goes to stderr only; stdout stays reserved for MCP frames (§27);
- no user file is created, modified or deleted: the write probe asks the
  kernel/OS for the directory's add-file *capability*, it never adds an
  entry (the plan's "capability assessment without mutating user files");
- proxy and tunnel credentials never appear here: host and port are shown
  only in this explicitly local, human-facing context (§24 addendum);
  the derived credential-bearing URL is never reconstructed for display;
- filesystem verdicts follow the section 33 support matrix: Windows GA is local
  NTFS only, anything else (network, FAT/exFAT, ReFS, unknown) is reported
  as *not supported until acceptance*, never silently blessed.
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from . import SERVER_VERSION
from .native_config import NativeConfigError, load_native_config
from .native_tunnel import NativeTunnelError, parse_proxy_env_file, proxy_url

OK = "OK"
FAIL = "FAIL"
WARN = "WARN"


class _Report:
    """Collects status lines and the FAIL/WARN tally that drives the exit code."""

    def __init__(self, writer: Callable[[str], None] | None = None):
        self._write = writer if writer is not None else (lambda line: print(line, file=sys.stderr))
        self.fail_count = 0
        self.warn_count = 0

    def say(self, line: str) -> None:
        self._write(line)

    def status(self, label: str, state: str, detail: str) -> None:
        if state == FAIL:
            self.fail_count += 1
        elif state == WARN:
            self.warn_count += 1
        suffix = f" -- {detail}" if detail else ""
        self.say(f"{label}: {state}{suffix}")

    def note(self, label: str, detail: str) -> None:
        self.say(f"{label}: {detail}")


def _classify_windows_storage(info: tuple[str, bool] | None) -> tuple[str, str]:
    """§33 support-matrix verdict for one Windows root.

    Windows GA is LOCAL NTFS only. Network shares, mapped drives, FAT/exFAT,
    ReFS/Dev Drive and any storage whose class cannot be measured must fail
    clearly -- an unknown filesystem never silently inherits the NTFS
    security claim (fail closed, "must fail clearly or run only in an
    explicitly documented reduced mode").
    """
    if info is None:
        return FAIL, "storage class could not be determined -- failing closed (section 33)"
    fs_name, is_remote = info
    if is_remote:
        return FAIL, f"{fs_name} on network storage -- unsupported until section 33 acceptance"
    if fs_name.upper() == "NTFS":
        return OK, "NTFS"
    return FAIL, f"{fs_name} -- not local NTFS, unsupported until section 33 acceptance"


def _filesystem_probe(report: _Report, root: Path) -> None:
    """Filesystem class + storage topology for one configured root (section 33)."""
    if sys.platform == "win32":
        state, detail = _classify_windows_storage(_windows_volume_info(root))
        report.status("filesystem", state, detail)
        return
    fs_name = _linux_mount_fstype(root)
    if fs_name is None:
        report.status("filesystem", WARN, "mount entry not found (unknown filesystem)")
    else:
        report.status("filesystem", OK, fs_name)


def _windows_volume_info(root: Path) -> tuple[str, bool] | None:
    """(filesystem name, is network storage) for the operator-configured root.

    This is a diagnostics-only, configuration-derived call: doctor runs
    outside the request path, the path is the operator's own config value,
    and nothing is opened relative to the result. Request-path channels
    stay on the handle-relative kernel.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetVolumePathNameW.restype = wintypes.BOOL
    kernel32.GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]

    # GetVolumeInformationW only accepts a volume-root path (measured: a plain
    # subdirectory fails with 123/144), so resolve the containing volume first.
    volume_root = ctypes.create_unicode_buffer(261)
    if not kernel32.GetVolumePathNameW(str(root), volume_root, 261):
        return None
    fs_name = ctypes.create_unicode_buffer(261)
    if not kernel32.GetVolumeInformationW(
        volume_root.value, None, 0, None, None, None, fs_name, 261
    ):
        return None
    drive_root = root.drive + "\\" if root.drive else None
    is_remote = False
    if drive_root is not None:
        is_remote = kernel32.GetDriveTypeW(drive_root) == 4  # DRIVE_REMOTE
    elif str(root).startswith("\\\\") or volume_root.value.startswith("\\\\"):
        is_remote = True
    return (fs_name.value or "unknown"), is_remote


def _linux_mount_fstype(root: Path) -> str | None:
    """Longest-mount-point prefix match against /proc/mounts."""
    try:
        resolved = os.path.realpath(root)
    except OSError:
        return None
    best_point = ""
    best_fstype: str | None = None
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 3:
                    continue
                point, fstype = fields[1], fields[2]
                if resolved == point or resolved.startswith(point.rstrip("/") + "/"):
                    if len(point) > len(best_point):
                        best_point, best_fstype = point, fstype
    except OSError:
        return None
    return best_fstype


def _reparse_probe(report: _Report, root: Path, root_stat: os.stat_result) -> bool:
    """Configuration-level symlink/junction detection ahead of the open.

    Returns True when the root is refused as reparse topology. The native
    kernel independently rejects a reparse root on the request path; this
    check exists so the operator sees the precise topology reason (a plain
    ``not a directory`` verdict on a POSIX symlink root would hide it).
    """
    import stat as _stat

    if sys.platform == "win32":
        reparse = bool(root_stat.st_file_attributes & _stat.FILE_ATTRIBUTE_REPARSE_POINT)
        if reparse or _stat.S_ISLNK(root_stat.st_mode):
            report.status(
                "reparse",
                FAIL,
                "root is a reparse point (symlink/junction); point at a real directory",
            )
            return True
        report.status("reparse", OK, "not a reparse point")
        return False
    if _stat.S_ISLNK(root_stat.st_mode):
        report.status("reparse", FAIL, "root is a symlink; point at a real directory")
        return True
    report.status("reparse", OK, "not a symlink")
    return False


def _read_probe(report: _Report, session: object, workdir: object) -> None:
    """One policy-filtered root listing through the real backend channel."""
    from .paths import DenyPolicy, resolve_workdir_path
    from .workdirs import Workdir

    wd: Workdir = workdir  # type: ignore[assignment]
    resolved = resolve_workdir_path(
        wd,
        ".",
        allow_hidden=wd.policy.allow_hidden,
        deny_policy=DenyPolicy(
            extra_globs=wd.policy.extra_deny_globs,
            default_deny_enabled=not wd.policy.disable_default_deny,
            case_insensitive=sys.platform == "win32",
        ),
    )
    entries, _has_more = session.list(resolved, offset=0, limit=1)  # type: ignore[attr-defined]
    report.status(
        "read", OK, f"root listing succeeded ({len(entries)} entries visible under policy)"
    )


def _write_probe(report: _Report, workdir: object) -> None:
    from .workdirs import Workdir

    wd: Workdir = workdir  # type: ignore[assignment]
    if wd.read_only:
        report.note("write", "not evaluated (read-only workdir)")
        return
    if sys.platform == "win32":
        capable, detail = _windows_add_file_probe(wd.root)
        report.status("write", OK if capable else FAIL, detail)
    else:
        writable = os.access(wd.root, os.W_OK | os.X_OK)
        report.status(
            "write",
            OK if writable else FAIL,
            "add-file capability present"
            if writable
            else "directory is not writable for this process",
        )


def _windows_add_file_probe(root: Path) -> tuple[bool, str]:
    """Open the root with FILE_ADD_FILE only: proves the ACL allows file
    creation without creating anything. No contents are ever written."""
    import ctypes
    from ctypes import wintypes

    FILE_ADD_FILE = 0x00000002
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    share = 0x1 | 0x2 | 0x4  # READ | WRITE | DELETE -- diagnostics never block users

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        str(root),
        wintypes.DWORD(FILE_ADD_FILE),
        wintypes.DWORD(share),
        None,
        wintypes.DWORD(OPEN_EXISTING),
        wintypes.DWORD(FILE_FLAG_BACKUP_SEMANTICS),
        None,
    )
    if handle != wintypes.HANDLE(-1).value:
        kernel32.CloseHandle(handle)
        return True, "add-file capability present (nothing was created)"
    error = kernel32.GetLastError()
    if error == 5:
        return False, "CreateFileW(FILE_ADD_FILE) denied by the directory ACL"
    return False, f"CreateFileW(FILE_ADD_FILE) failed with Win32 error {error}"


def _backend_line(report: _Report) -> None:
    if sys.platform == "win32":
        try:
            import serverfs_windows_native  # noqa: F401
        except ImportError:
            report.status(
                "native backend",
                FAIL,
                "serverfs_windows_native is not importable; "
                "install the prebuilt wheel (see README: Windows native)",
            )
            return
        version = _distribution_version("serverfs-windows-native")
        report.status("native backend", OK, f"serverfs-windows-native {version}")
    elif sys.platform == "linux":
        report.status("native backend", OK, "linux fdio kernel")
    else:
        report.status("native backend", WARN, f"unsupported platform ({sys.platform})")


def _distribution_version(package: str) -> str:
    from importlib import metadata

    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return "unknown version (installed without metadata)"


def _tunnel_probe(report: _Report, tunnel_client: Path | None) -> None:
    if tunnel_client is None:
        from .tunnel_bootstrap import default_tunnel_client_path

        tunnel_client = default_tunnel_client_path()
        source = " (bootstrap directory)"
        if tunnel_client is None:
            report.note(
                "tunnel-client",
                "not configured; run 'serverfs bootstrap tunnel-client' or pass --tunnel-client",
            )
            return
    else:
        source = ""
    if not tunnel_client.is_file():
        report.status("tunnel-client", FAIL, f"not found at {tunnel_client}")
        return
    try:
        completed = subprocess.run(
            [str(tunnel_client), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.status("tunnel-client", FAIL, f"version probe failed ({type(exc).__name__})")
        return
    line = (completed.stdout or completed.stderr or "").strip().splitlines()
    first = line[0] if line else ""
    if completed.returncode == 0 and first:
        report.status("tunnel-client", OK, f"{first}{source}")
    else:
        report.status(
            "tunnel-client", FAIL, f"probe exited with code {completed.returncode}{source}"
        )


def _resolve_env_file(env_file: Path | None, config_path: Path) -> Path | None:
    if env_file is not None:
        return env_file
    sibling = config_path.resolve().parent / ".env"
    return sibling if sibling.exists() else None


def _proxy_probe(report: _Report, env_path: Path | None) -> None:
    if env_path is None:
        report.note("proxy", "disabled (no env file; direct connectivity)")
        return
    try:
        values = parse_proxy_env_file(env_path)
        url_host_port = proxy_url(values)
    except NativeTunnelError as exc:
        # NativeTunnelError messages are redacted by construction (§24).
        report.status("proxy", FAIL, str(exc))
        return
    if url_host_port is None:
        report.note("proxy", "disabled (no SERVERFS_PROXY_HOST configured; direct connectivity)")
        return
    host = values.get("SERVERFS_PROXY_HOST", "")
    port = int(values.get("SERVERFS_PROXY_PORT", "0"))
    auth = "configured" if values.get("SERVERFS_PROXY_USERNAME", "") else "none"
    report.note("proxy", f"enabled http host={host} port={port} auth={auth}")
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        report.status("proxy reachability", FAIL, f"DNS resolution failed for {host}")
        return
    try:
        with socket.create_connection((host, port), timeout=5):
            report.status("proxy reachability", OK, f"TCP connect to {host}:{port} succeeded")
    except OSError as exc:
        report.status(
            "proxy reachability",
            FAIL,
            f"TCP connect to {host}:{port} failed ({type(exc).__name__})",
        )


def _probe_workdir(report: _Report, backend: object | None, workdir: object) -> None:
    from .backends import BackendError
    from .workdirs import Workdir

    wd: Workdir = workdir  # type: ignore[assignment]
    access = "read-write" if not wd.read_only else "read-only"
    report.say(f"workdir: {wd.alias} ({access})")
    try:
        root_stat = os.lstat(wd.root)
    except FileNotFoundError:
        report.status("root", FAIL, "configured root does not exist")
        return
    except OSError as exc:
        report.status("root", FAIL, f"configured root cannot be inspected ({type(exc).__name__})")
        return
    import stat as _stat

    # A POSIX symlink root lstats as S_IFLNK, not S_IFDIR: decide the
    # reparse topology first so the operator sees the precise reason
    # instead of a bare "not a directory".
    if _reparse_probe(report, wd.root, root_stat):
        return
    if not _stat.S_ISDIR(root_stat.st_mode):
        report.status("root", FAIL, "configured root is not a directory")
        return
    _filesystem_probe(report, wd.root)
    if backend is None:
        report.status("read", WARN, "not probed (backend unavailable)")
        report.status("write", WARN, "not probed (backend unavailable)")
        return
    try:
        session = backend.open_session(wd)  # type: ignore[attr-defined]
    except BackendError as exc:
        report.status("root", FAIL, f"backend refused the root ({exc.code})")
        return
    except (
        Exception
    ) as exc:  # kernel import/initialization failure must be visible, not a traceback
        report.status("root", FAIL, f"backend open failed ({type(exc).__name__})")
        return
    token = getattr(session, "identity_token", None)
    detail = "opened" + (f" (object token {_short(token)})" if isinstance(token, str) else "")
    report.status("root", OK, detail)
    try:
        _read_probe(report, session, wd)
    except BackendError as exc:
        report.status("read", FAIL, f"root listing failed ({exc.code})")
    except Exception as exc:
        report.status("read", FAIL, f"root listing failed ({type(exc).__name__})")
    _write_probe(report, wd)


def _short(token: str) -> str:
    return token[:16] + ("…" if len(token) > 16 else "")


def run_doctor(
    config_path: Path,
    *,
    env_file: Path | None = None,
    tunnel_client: Path | None = None,
    writer: Callable[[str], None] | None = None,
) -> int:
    """Execute the full report; returns the process exit code."""
    report = _Report(writer)
    report.say(f"ServerFS {SERVER_VERSION}")
    report.say(f"Python {sys.version.split()[0]} ({sys.platform} {platform.machine()})")
    try:
        workdirs, _settings = load_native_config(config_path)
    except NativeConfigError as exc:
        report.say(f"config: FAIL -- {exc}")
        return 2
    report.say(f"config: OK ({config_path})")
    _backend_line(report)

    backend: object | None
    if sys.platform in {"win32", "linux"}:
        try:
            from .backends import get_backend

            backend = get_backend()
        except Exception as exc:  # missing native wheel must be a line, not a traceback
            report.status("backend", FAIL, f"kernel unavailable ({type(exc).__name__})")
            backend = None
    else:
        backend = None
        report.note("workdirs", "not probed (no backend for this platform)")

    if backend is not None:
        for wd in workdirs:
            _probe_workdir(report, backend, wd)

    _tunnel_probe(report, tunnel_client)
    _proxy_probe(report, _resolve_env_file(env_file, config_path))

    report.say(f"summary: {report.fail_count} FAIL, {report.warn_count} WARN")
    return 1 if report.fail_count else 0
