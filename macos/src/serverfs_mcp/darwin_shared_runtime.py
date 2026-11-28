"""Offline, create-only macOS Shared Core runtime from the project's uv.lock.

A signed native Shared Core must pin a physical, owner-owned Python executable.
uv normally links a venv's bin/python to its managed base interpreter; directly
pinning that base loses installed ServerFS dependencies. Build a versioned,
private venv with locked dependencies, copy a real interpreter + its matching
libpython dylib, then publish only after an isolated real import probe.

This operator-only installer never starts a service or touches an existing
runtime, Bridge, Tunnel, Connector, credential or production instance.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from .darwin_instances import _validate_shared_core_python


class DarwinSharedRuntimeError(ValueError):
    """Cannot safely create an isolated, pinned Shared Core runtime."""


def _private_target(target: Path, source: Path) -> Path:
    if not target.is_absolute() or target.name in {"", ".", ".."}:
        raise DarwinSharedRuntimeError("runtime destination must be an absolute unused name")
    if target.exists() or target.is_symlink():
        raise DarwinSharedRuntimeError("shared Core runtime already exists")
    parent = target.parent
    if parent.resolve(strict=True) != parent:
        raise DarwinSharedRuntimeError("runtime parent must use a canonical physical path")
    info = parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise DarwinSharedRuntimeError("runtime parent must be a private user-owned directory")
    if source == parent or source in parent.parents or parent in source.parents:
        raise DarwinSharedRuntimeError("runtime destination cannot overlap project source")
    if target.name.startswith("."):
        raise DarwinSharedRuntimeError("runtime destination cannot be a hidden staging name")
    return parent


def _checked_input(source_python: Path, source: Path, uv: Path) -> tuple[Path, Path]:
    if not (source / "pyproject.toml").is_file() or not (source / "uv.lock").is_file():
        raise DarwinSharedRuntimeError("project metadata or frozen uv lock is unavailable")
    if not source_python.is_absolute() or not source_python.exists():
        raise DarwinSharedRuntimeError("base Python is unavailable")
    base = source_python.resolve(strict=True)
    info = base.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
        or not os.access(base, os.X_OK)
    ):
        raise DarwinSharedRuntimeError("base Python must be user-owned and executable")
    dylib = (
        base.parent.parent
        / "lib"
        / f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib"
    )
    if not dylib.is_file() or dylib.is_symlink():
        raise DarwinSharedRuntimeError("matching macOS libpython is unavailable")
    lib_info = dylib.stat(follow_symlinks=False)
    if not stat.S_ISREG(lib_info.st_mode) or lib_info.st_uid != os.getuid():
        raise DarwinSharedRuntimeError("matching macOS libpython is untrusted")
    if not uv.is_absolute() or not uv.is_file() or not os.access(uv, os.X_OK):
        raise DarwinSharedRuntimeError("uv executable must be an absolute installed file")
    return base, dylib


def _execute_uv(
    uv: Path,
    args: list[str],
    *,
    source: Path,
    step: str,
    timeout: int = 150,
) -> None:
    # Offline and no inherited Python path, provider credentials or index URLs.
    env = {
        "HOME": str(Path.home()),
        "PATH": os.defpath,
        "UV_OFFLINE": "1",
        "UV_NO_PROGRESS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        result = subprocess.run(
            [str(uv), *args],
            cwd=source,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        raise DarwinSharedRuntimeError(f"shared Core runtime {step} unavailable") from None
    if result.returncode != 0:
        raise DarwinSharedRuntimeError(f"shared Core runtime {step} failed")


def _probe(candidate: Path) -> None:
    executable = _validate_shared_core_python(candidate / "bin" / "serverfs-core-python")
    script = (
        "import sys; from pathlib import Path; "
        "import serverfs_mcp, serverfs_mcp.cli, mcp, httpx2; "
        "prefix=Path(sys.prefix).resolve(strict=True); "
        "assert prefix == Path(sys.argv[1]).resolve(strict=True); "
        "assert Path(sys.executable).resolve(strict=True) == "
        "Path(sys.argv[2]).resolve(strict=True); "
        "assert Path(serverfs_mcp.__file__).resolve(strict=True).is_relative_to(prefix)"
    )
    try:
        result = subprocess.run(
            [str(executable), "-I", "-B", "-c", script, str(candidate), str(executable)],
            cwd=candidate,
            env={
                "HOME": str(Path.home()),
                "PATH": os.defpath,
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise DarwinSharedRuntimeError("isolated Shared Core runtime probe unavailable") from None
    if result.returncode != 0:
        raise DarwinSharedRuntimeError("isolated Shared Core runtime probe failed")


def _publish_new_directory(source: Path, destination: Path) -> None:
    """Darwin atomic create-only directory rename, including empty destinations.

    An existence check followed by ordinary os.rename can replace an empty
    concurrently created directory. The SDK's RENAME_EXCL avoids this TOCTOU
    window. Never fall back to a potentially overwriting rename.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(os.fsencode(source), os.fsencode(destination), 0x00000004)
    except (AttributeError, OSError, TypeError):
        raise DarwinSharedRuntimeError(
            "atomic create-only macOS runtime publication is unavailable"
        ) from None
    if result != 0:
        if ctypes.get_errno() == errno.EEXIST:
            raise DarwinSharedRuntimeError("shared Core runtime already exists")
        raise DarwinSharedRuntimeError("atomic Shared Core runtime publication failed")


_PROVENANCE_KEYS = {
    "schema_version",
    "uv_lock_sha256",
    "pyproject_sha256",
    "requirements_sha256",
    "wheel_sha256",
    "python_sha256",
    "libpython_sha256",
    "python_name",
    "libpython_name",
    "offline",
}
_RELEASE_PROVENANCE_KEYS = _PROVENANCE_KEYS | {"git_commit", "release_version"}
_SITE_PACKAGES_KEYS = {"site_packages_sha256", "site_packages_file_count", "site_packages_bytes"}
_V3_PROVENANCE_KEYS = _PROVENANCE_KEYS | _SITE_PACKAGES_KEYS
_V3_RELEASE_PROVENANCE_KEYS = _RELEASE_PROVENANCE_KEYS | _SITE_PACKAGES_KEYS

_SITE_PACKAGES_MAX_FILES = 100_000
_SITE_PACKAGES_MAX_BYTES = 2 * 1024 * 1024 * 1024

_DIGEST_KEYS = {
    "uv_lock_sha256",
    "pyproject_sha256",
    "requirements_sha256",
    "wheel_sha256",
    "python_sha256",
    "libpython_sha256",
}


def verify_shared_core_runtime(
    target: Path, *, project_root: Path | None = None, require_site_packages: bool = False
) -> dict[str, str]:
    """Read-only check of an installed runtime, provenance and real imports.

    Historical schema 1/2 records remain parseable and receive their original
    checks. Only schema 3 records attest the installed site-packages bytes.
    """
    if sys.platform != "darwin":
        raise DarwinSharedRuntimeError("Shared Core runtime verification requires macOS")
    target = Path(target)
    try:
        if not target.is_absolute() or target.resolve(strict=True) != target or target.is_symlink():
            raise DarwinSharedRuntimeError("runtime path is not canonical")
        info = target.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise DarwinSharedRuntimeError("runtime directory is not private")
        record = target / "runtime-provenance.json"
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(record, flags)
        try:
            rinfo = os.fstat(fd)
            if (
                not stat.S_ISREG(rinfo.st_mode)
                or rinfo.st_uid != os.getuid()
                or stat.S_IMODE(rinfo.st_mode) != 0o600
                or not 2 <= rinfo.st_size <= 8192
            ):
                raise DarwinSharedRuntimeError("runtime provenance is not private")
            payload = os.read(fd, 8193)
            if len(payload) != rinfo.st_size:
                raise DarwinSharedRuntimeError("runtime provenance size changed")
        finally:
            os.close(fd)

        def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise DarwinSharedRuntimeError("duplicate provenance field")
                result[key] = value
            return result

        metadata = json.loads(payload, object_pairs_hook=unique_fields)
        if (
            not isinstance(metadata, dict)
            or (
                (metadata.get("schema_version") == 1 and set(metadata) != _PROVENANCE_KEYS)
                or (
                    metadata.get("schema_version") == 2
                    and set(metadata) != _RELEASE_PROVENANCE_KEYS
                )
                or (
                    metadata.get("schema_version") == 3
                    and set(metadata) != _V3_PROVENANCE_KEYS
                    and set(metadata) != _V3_RELEASE_PROVENANCE_KEYS
                )
                or type(metadata.get("schema_version")) is not int
                or metadata.get("schema_version") not in {1, 2, 3}
            )
            or (
                (
                    metadata.get("schema_version") == 2
                    or (metadata.get("schema_version") == 3 and "git_commit" in metadata)
                )
                and (
                    not isinstance(metadata.get("git_commit"), str)
                    or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", metadata["git_commit"])
                    is None
                    or not isinstance(metadata.get("release_version"), str)
                    or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", metadata["release_version"]) is None
                )
            )
            or metadata.get("offline") is not True
            or metadata.get("python_name") != "serverfs-core-python"
            or metadata.get("libpython_name")
            != f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib"
            or any(
                not isinstance(metadata.get(key), str)
                or re.fullmatch(r"[0-9a-f]{64}", metadata[key]) is None
                for key in _DIGEST_KEYS
            )
        ):
            raise DarwinSharedRuntimeError("runtime provenance schema is invalid")
        if metadata["schema_version"] == 3 and (
            not isinstance(metadata.get("site_packages_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", metadata["site_packages_sha256"]) is None
            or type(metadata.get("site_packages_file_count")) is not int
            or type(metadata.get("site_packages_bytes")) is not int
            or not 0 <= metadata["site_packages_file_count"] <= _SITE_PACKAGES_MAX_FILES
            or not 0 <= metadata["site_packages_bytes"] <= _SITE_PACKAGES_MAX_BYTES
        ):
            raise DarwinSharedRuntimeError("runtime provenance schema is invalid")
        if require_site_packages and metadata["schema_version"] != 3:
            raise DarwinSharedRuntimeError(
                "managed Shared Core runtime lacks site-packages integrity"
            )
        python = _validate_shared_core_python(target / "bin" / metadata["python_name"])
        dylib = target / "lib" / metadata["libpython_name"]
        lib_info = dylib.stat(follow_symlinks=False)
        if (
            dylib.is_symlink()
            or not stat.S_ISREG(lib_info.st_mode)
            or lib_info.st_uid != os.getuid()
            or lib_info.st_mode & 0o022
        ):
            raise DarwinSharedRuntimeError("installed runtime libpython is unsafe")
        if (
            hashlib.sha256(python.read_bytes()).hexdigest() != metadata["python_sha256"]
            or hashlib.sha256(dylib.read_bytes()).hexdigest() != metadata["libpython_sha256"]
        ):
            raise DarwinSharedRuntimeError("runtime binary integrity mismatch")
        if project_root is not None:
            project = Path(project_root).resolve(strict=True)
            if metadata["schema_version"] == 2 or (
                metadata["schema_version"] == 3 and "git_commit" in metadata
            ):
                from .darwin_shared_release_source import read_release_commit_file

                def source_bytes(filename: str) -> bytes:
                    return read_release_commit_file(project, metadata["git_commit"], filename)

                released_project = tomllib.loads(source_bytes("pyproject.toml").decode("utf-8"))
                if (
                    released_project.get("project", {}).get("version")
                    != metadata["release_version"]
                ):
                    raise DarwinSharedRuntimeError("runtime release version mismatch")
            else:

                def source_bytes(filename: str) -> bytes:
                    return (project / filename).read_bytes()

            for filename, key in (
                ("uv.lock", "uv_lock_sha256"),
                ("pyproject.toml", "pyproject_sha256"),
            ):
                if hashlib.sha256(source_bytes(filename)).hexdigest() != metadata[key]:
                    raise DarwinSharedRuntimeError("runtime source metadata drift")
        if metadata["schema_version"] == 3:
            inventory = _site_packages_inventory(target)
            if inventory != (
                metadata["site_packages_sha256"],
                metadata["site_packages_file_count"],
                metadata["site_packages_bytes"],
            ):
                raise DarwinSharedRuntimeError("installed site-packages integrity mismatch")
        _probe(target)
    except DarwinSharedRuntimeError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError):
        raise DarwinSharedRuntimeError("installed runtime verification failed") from None

    result = {
        "state": "verified",
        "python": str(python),
        "uv_lock_sha256": metadata["uv_lock_sha256"],
        "wheel_sha256": metadata["wheel_sha256"],
    }
    if metadata["schema_version"] == 2 or (
        metadata["schema_version"] == 3 and "git_commit" in metadata
    ):
        result["git_commit"] = metadata["git_commit"]
        result["release_version"] = metadata["release_version"]
    return result


def verify_managed_shared_core_python(python: Path) -> bool:
    """Verify the provenance of a managed Core Python, if it is one.

    The offline installer reserves bin/serverfs-core-python. This name
    identifies the managed distribution contract; non-managed development
    interpreters retain the pre-existing isolated import probe and must not
    be described as release-verified.
    """
    python = Path(python)
    if python.name != "serverfs-core-python":
        return False
    if python.parent.name != "bin":
        raise DarwinSharedRuntimeError("managed Shared Core Python path is invalid")
    result = verify_shared_core_runtime(python.parent.parent, require_site_packages=True)
    if result["python"] != str(python):
        raise DarwinSharedRuntimeError("managed Shared Core Python identity mismatch")
    return True


def _site_packages_inventory(target: Path) -> tuple[str, int, int]:
    """Hash files by canonical relative path and bytes in sorted traversal order.

    Every installed file, including bytecode caches, is included. The managed
    runtime installs ``sitecustomize.py`` to suppress later bytecode writes.
    Links and special files fail closed. Fixed limits bound traversal and reads.
    """
    site_packages = (
        target
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    try:
        for directory in (
            target / "lib",
            target / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}",
            site_packages,
        ):
            directory_info = directory.lstat()
            if stat.S_ISLNK(directory_info.st_mode) or not stat.S_ISDIR(directory_info.st_mode):
                raise DarwinSharedRuntimeError("installed site-packages directory is unsafe")
        digest = hashlib.sha256()
        file_count = 0
        total_bytes = 0
        for current, directories, filenames in os.walk(
            site_packages, topdown=True, followlinks=False
        ):
            current_path = Path(current)
            kept_directories: list[str] = []
            for name in sorted(directories):
                path = current_path / name
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                    raise DarwinSharedRuntimeError(
                        "installed site-packages contains an unsafe entry"
                    )
                kept_directories.append(name)
            directories[:] = kept_directories
            for name in sorted(filenames):
                path = current_path / name
                relative = path.relative_to(site_packages).as_posix()
                if relative.startswith("/") or any(
                    part in {"", ".", ".."} for part in relative.split("/")
                ):
                    raise DarwinSharedRuntimeError("installed site-packages path is not canonical")
                before = path.lstat()
                if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                    raise DarwinSharedRuntimeError(
                        "installed site-packages contains an unsafe entry"
                    )
                file_count += 1
                if (
                    file_count > _SITE_PACKAGES_MAX_FILES
                    or total_bytes + before.st_size > _SITE_PACKAGES_MAX_BYTES
                ):
                    raise DarwinSharedRuntimeError(
                        "installed site-packages inventory exceeds limits"
                    )
                path_bytes = relative.encode("utf-8", errors="strict")
                digest.update(len(path_bytes).to_bytes(8, "big"))
                digest.update(path_bytes)
                digest.update(before.st_size.to_bytes(8, "big"))
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags)
                try:
                    opened = os.fstat(fd)
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                        before.st_dev,
                        before.st_ino,
                    ):
                        raise DarwinSharedRuntimeError(
                            "installed site-packages changed during verification"
                        )
                    read_bytes = 0
                    while True:
                        chunk = os.read(fd, 1024 * 1024)
                        if not chunk:
                            break
                        read_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if total_bytes > _SITE_PACKAGES_MAX_BYTES:
                            raise DarwinSharedRuntimeError(
                                "installed site-packages inventory exceeds limits"
                            )
                        digest.update(chunk)
                    after = os.fstat(fd)
                    if read_bytes != before.st_size or (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ) != (
                        opened.st_dev,
                        opened.st_ino,
                        opened.st_size,
                        opened.st_mtime_ns,
                        opened.st_ctime_ns,
                    ):
                        raise DarwinSharedRuntimeError(
                            "installed site-packages changed during verification"
                        )
                finally:
                    os.close(fd)
        return digest.hexdigest(), file_count, total_bytes
    except DarwinSharedRuntimeError:
        raise
    except (OSError, UnicodeError, OverflowError):
        raise DarwinSharedRuntimeError("installed site-packages verification failed") from None


def create_shared_core_runtime(
    target: Path,
    project_root: Path,
    source_python: Path,
    uv: Path,
    *,
    release_commit: str | None = None,
    release_version: str | None = None,
) -> dict[str, str]:
    """Atomically provision a new offline runtime, never replace an existing one."""
    if sys.platform != "darwin":
        raise DarwinSharedRuntimeError("Shared Core runtime installer requires macOS")
    if (release_commit is None) != (release_version is None):
        raise DarwinSharedRuntimeError("release commit and version must be supplied together")
    if (
        release_version is not None
        and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", release_version) is None
    ):
        raise DarwinSharedRuntimeError("release version must be a stable semver triplet")
    source = Path(project_root).resolve(strict=True)
    target = Path(target)
    parent = _private_target(target, source)
    base, dylib = _checked_input(Path(source_python), source, Path(uv))

    # All cache/build/release work lives under the explicitly private parent.
    # Nothing is published at target unless the complete env passes its probe.
    with tempfile.TemporaryDirectory(prefix=".shared-core-build-", dir=parent) as work:
        scratch = Path(work)
        scratch.chmod(0o700)
        if release_commit is not None:
            from .darwin_shared_release_source import materialize_release_source

            build_source = materialize_release_source(
                source, release_commit, scratch / "release-source"
            )
            try:
                release_project = tomllib.loads((build_source / "pyproject.toml").read_text())
                actual_version = release_project["project"]["version"]
            except (OSError, ValueError, KeyError, TypeError, UnicodeError):
                raise DarwinSharedRuntimeError(
                    "release snapshot project version unavailable"
                ) from None
            if actual_version != release_version:
                raise DarwinSharedRuntimeError("release Git commit project version mismatch")
        else:
            build_source = source
        candidate = scratch / "runtime"
        requirements = scratch / "locked-requirements.txt"
        dist = scratch / "dist"
        _execute_uv(
            Path(uv),
            [
                "export",
                "--offline",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--output-file",
                str(requirements),
            ],
            source=build_source,
            step="lock export",
        )
        _execute_uv(
            Path(uv),
            ["build", "--offline", "--wheel", "--out-dir", str(dist)],
            source=build_source,
            step="wheel build",
        )
        wheels = list(dist.glob("serverfs_mcp-*.whl"))
        if len(wheels) != 1:
            raise DarwinSharedRuntimeError("expected exactly one ServerFS wheel")
        wheel = wheels[0]
        if release_version is not None and not wheel.name.startswith(
            f"serverfs_mcp-{release_version}-"
        ):
            raise DarwinSharedRuntimeError("release wheel version mismatch")
        wheel_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        _execute_uv(
            Path(uv),
            [
                "venv",
                "--offline",
                "--no-project",
                "--python",
                str(base),
                str(candidate),
            ],
            source=build_source,
            step="venv creation",
        )
        candidate.chmod(0o700)
        _execute_uv(
            Path(uv),
            [
                "pip",
                "sync",
                "--offline",
                "--link-mode",
                "copy",
                "--python",
                str(candidate / "bin/python"),
                str(requirements),
            ],
            source=build_source,
            step="locked dependencies",
        )
        _execute_uv(
            Path(uv),
            [
                "pip",
                "install",
                "--offline",
                "--no-deps",
                "--link-mode",
                "copy",
                "--python",
                str(candidate / "bin/python"),
                str(wheel),
            ],
            source=build_source,
            step="ServerFS wheel",
        )
        pinned = candidate / "bin" / "serverfs-core-python"
        if pinned.exists() or pinned.is_symlink():
            raise DarwinSharedRuntimeError("private pinned interpreter path is occupied")
        shutil.copy2(base, pinned)
        pinned.chmod(0o700)
        shutil.copy2(
            dylib,
            candidate / "lib" / dylib.name,
        )
        site_packages = (
            candidate
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        sitecustomize = site_packages / "sitecustomize.py"
        try:
            with sitecustomize.open("x", encoding="utf-8") as stream:
                stream.write("import sys\nsys.dont_write_bytecode = True\n")
        except OSError:
            raise DarwinSharedRuntimeError("cannot configure immutable runtime bytecode") from None
        (
            site_packages_digest,
            site_packages_file_count,
            site_packages_bytes,
        ) = _site_packages_inventory(candidate)
        metadata = {
            "schema_version": 3,
            "uv_lock_sha256": hashlib.sha256((build_source / "uv.lock").read_bytes()).hexdigest(),
            "pyproject_sha256": hashlib.sha256(
                (build_source / "pyproject.toml").read_bytes()
            ).hexdigest(),
            "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
            "wheel_sha256": wheel_digest,
            "python_sha256": hashlib.sha256(pinned.read_bytes()).hexdigest(),
            "libpython_sha256": hashlib.sha256(
                (candidate / "lib" / dylib.name).read_bytes()
            ).hexdigest(),
            "python_name": pinned.name,
            "libpython_name": dylib.name,
            "offline": True,
            "site_packages_sha256": site_packages_digest,
            "site_packages_file_count": site_packages_file_count,
            "site_packages_bytes": site_packages_bytes,
        }
        if release_commit is not None:
            metadata["git_commit"] = release_commit
            metadata["release_version"] = release_version
        record = candidate / "runtime-provenance.json"
        record.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
        record.chmod(0o600)
        verify_shared_core_runtime(candidate, project_root=source)

        # Rename is local and atomic. If a concurrent installer occupies the
        # destination, refuse rather than overwriting its valid state.
        if target.exists() or target.is_symlink():
            raise DarwinSharedRuntimeError("runtime destination appeared during staging")
        _publish_new_directory(candidate, target)
        try:
            verify_shared_core_runtime(target, project_root=source)
        except BaseException:
            # This target is exactly the new directory renamed by this call,
            # not a pre-existing runtime. Do not retain a failed candidate.
            shutil.rmtree(target)
            raise
    result = {
        "state": "installed",
        "python": str(target / "bin" / "serverfs-core-python"),
        "uv_lock_sha256": metadata["uv_lock_sha256"],
        "wheel_sha256": metadata["wheel_sha256"],
    }
    if release_commit is not None:
        result["git_commit"] = release_commit
        result["release_version"] = release_version
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="serverfs-macos-shared-runtime")
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--source-python", type=Path)
    parser.add_argument("--uv", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--release-commit",
        help="build only from the exact local Git HEAD SHA archive, never from a dirty worktree",
    )
    parser.add_argument(
        "--release-version",
        help="require the exact stable project and wheel version in the archived Git commit",
    )
    args = parser.parse_args(argv)
    try:
        if args.verify_only:
            outcome = verify_shared_core_runtime(args.target, project_root=args.project_root)
            if args.release_commit is not None and outcome.get("git_commit") != args.release_commit:
                raise DarwinSharedRuntimeError("verified runtime release commit differs")
            if (
                args.release_version is not None
                and outcome.get("release_version") != args.release_version
            ):
                raise DarwinSharedRuntimeError("verified runtime release version differs")
        else:
            if args.source_python is None or args.uv is None:
                parser.error("--source-python and --uv are required for installation")
            outcome = create_shared_core_runtime(
                args.target,
                args.project_root,
                args.source_python,
                args.uv,
                release_commit=args.release_commit,
                release_version=args.release_version,
            )
    except (DarwinSharedRuntimeError, OSError, ValueError) as exc:
        print(f"runtime install refused: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(outcome, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
