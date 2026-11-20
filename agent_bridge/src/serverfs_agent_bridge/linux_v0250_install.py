"""Stage and select one Linux v2 Bridge release; never start or restart services."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

from .linux_v0250_preflight import _directory, _private_json
from .linux_v0250_provision import _canonical_no_links

_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?\Z")
_RELEASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_MARKER = ".serverfs-release-ready"
_UNIT = "serverfs-agent-bridge@.service"
# Run against the installed candidate, not the caller's import path.
_CHECK_CONFIG = """
import os, sys
from pathlib import Path
from serverfs_agent_bridge.config import BridgeConfig
from serverfs_agent_bridge.core_grants_private import _parse_grants
from serverfs_agent_bridge.linux_v0250_preflight import _directory, _private_json
root = _directory(Path(sys.argv[1]), private=True)
config_path = root / "config.json"
_private_json(config_path)
config = BridgeConfig.load(config_path)
if not (
    config.protocol_mode == "v2" and config.bridge_id == sys.argv[2]
    and config.lease_key == "alias"
    and config.allowed_peer_uid == os.getuid()
    and config.allowed_peer_gid == os.getgid()
    and config.socket_path == root / "bridge/run/bridge.sock"
    and config.lock_dir == root / "bridge/locks"
    and config.state_dir == root / "bridge/state"
):
    raise SystemExit(2)
_directory(config.state_dir, private=True)
_parse_grants(_private_json(config.state_dir / "core-grants.json"),
              config.bridge_id, config.policies)
"""


class InstallError(ValueError):
    """A fixed diagnostic label, not subprocess output or private config."""


def _require(condition: bool, stage: str) -> None:
    if not condition:
        raise InstallError(stage)


def _identity(bridge_id: str, release: str | None = None) -> None:
    _require(sys.platform == "linux" and os.getuid() != 0, "nonroot-linux")
    _require(bool(_ID.fullmatch(bridge_id)) and bridge_id != "default", "bridge-id")
    if release is not None:
        _require(bool(_RELEASE.fullmatch(release)), "release-id")


def _private_mkdir(path: Path) -> Path:
    _canonical_no_links(path, must_exist=False)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return _directory(path, private=True)


def _config_root(bridge_id: str) -> Path:
    root = Path.home() / ".config/serverfs-agent-bridge/instances" / bridge_id
    _directory(root, private=True)
    _private_json(root / "config.json")
    return root


def _app_root(bridge_id: str) -> Path:
    return Path.home() / ".local/share/serverfs-agent-bridge/instances" / bridge_id


@contextmanager
def _locked(root: Path):
    lock = root / ".deployment.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "deployment-lock",
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError("deployment-busy") from None
        yield
    finally:
        os.close(fd)


def _run(argv: list[str], *, cwd: Path, stage: str, timeout: int = 300, env=None) -> None:
    try:
        result = subprocess.run(
            argv, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError(stage) from None
    _require(result.returncode == 0, stage)


def _verify_release(path: Path, config_root: Path, bridge_id: str) -> None:
    executable = path / ".venv/bin/serverfs-agent-bridge"
    python = path / ".venv/bin/python"
    _require(executable.is_file() and os.access(executable, os.X_OK), "release-entrypoint")
    _run(
        [str(python), "-I", "-B", "-c", _CHECK_CONFIG, str(config_root), bridge_id],
        cwd=path,
        stage="release-config",
    )
    _run([str(executable), "--help"], cwd=path, stage="release-help")


def _copy_source(source: Path, destination: Path) -> None:
    source = _directory(source)
    for filename in ("pyproject.toml", "uv.lock"):
        item = source / filename
        _canonical_no_links(item, must_exist=True)
        _require(item.is_file(), "source-layout")
        shutil.copyfile(item, destination / filename)
    tree = _directory(source / "src")
    for item in tree.rglob("*"):
        _require(not item.is_symlink() and (item.is_file() or item.is_dir()), "source-layout")
    shutil.copytree(
        tree, destination / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )


def stage(bridge_id: str, release: str, source: Path, uv: Path, *, offline: bool = False) -> Path:
    """Create a release at its final path (venv entrypoints contain absolute paths)."""
    _identity(bridge_id, release)
    config_root = _config_root(bridge_id)
    _require(uv.is_absolute() and uv.is_file() and os.access(uv, os.X_OK), "uv-executable")
    root = _private_mkdir(_app_root(bridge_id))
    releases = _private_mkdir(root / "releases")
    with _locked(root):
        target = releases / release
        # Never overwrite any previous release, including an incomplete one.
        target.mkdir(mode=0o700)
        try:
            _copy_source(source, target)
            env = dict(os.environ)
            env.pop("VIRTUAL_ENV", None)
            env.pop("PYTHONPATH", None)
            env["UV_PROJECT_ENVIRONMENT"] = str(target / ".venv")
            argv = [str(uv), "sync", "--frozen", "--no-dev", "--no-editable"]
            if offline:
                argv.append("--offline")
            _run(argv, cwd=target, stage="uv-sync", env=env)
            _verify_release(target, config_root, bridge_id)
            marker = target / _MARKER
            with marker.open("x", encoding="utf-8") as stream:
                json.dump({"bridge_id": bridge_id, "release": release}, stream)
            marker.chmod(0o400)
        except BaseException:
            # Only this invocation's newly reserved release may be removed.
            shutil.rmtree(target)
            raise
    return target


def _require_stopped(bridge_id: str) -> None:
    name = f"serverfs-agent-bridge@{bridge_id}.service"
    result = subprocess.run(
        ["systemctl", "--user", "show", name, "--property=LoadState", "--property=ActiveState"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    # A missing unit is allowed for first selection. Refuse an unreachable manager,
    # failed/activating/deactivating service or any ambiguous response.
    _require(
        fields.get("ActiveState") == "inactive"
        and (
            result.returncode == 0
            and fields.get("LoadState") == "loaded"
            or fields.get("LoadState") == "not-found"
        ),
        "unit-not-stopped",
    )


def select(bridge_id: str, release: str) -> Path:
    """Select an existing verified release while the instance is stopped."""
    _identity(bridge_id, release)
    config_root = _config_root(bridge_id)
    root = _directory(_app_root(bridge_id), private=True)
    releases = _directory(root / "releases", private=True)
    with _locked(root):
        _require_stopped(bridge_id)
        target = _directory(releases / release, private=True)
        _require(
            _private_json(target / _MARKER) == {"bridge_id": bridge_id, "release": release},
            "release-marker",
        )
        _verify_release(target, config_root, bridge_id)
        current = root / "current"
        if current.is_symlink():
            old = current.resolve(strict=True)
            _require(old.parent == releases and old.is_dir(), "current-target")
        else:
            _require(not current.exists(), "current-target")
        # Temporary link and atomic replacement keep the previous pointer on failure.
        with tempfile.TemporaryDirectory(prefix=".select-", dir=root) as directory:
            temporary = Path(directory) / "current"
            temporary.symlink_to(target)
            os.replace(temporary, current)
    return target


def install_template(source: Path) -> Path:
    """Publish a new template without replacing an existing differing template."""
    _require(sys.platform == "linux" and os.getuid() != 0, "nonroot-linux")
    _canonical_no_links(source, must_exist=True)
    _require(source.is_file() and source.name == _UNIT, "template-source")
    contents = source.read_bytes()
    _require(0 < len(contents) <= 16384, "template-source")
    directory = Path.home() / ".config/systemd/user"
    _canonical_no_links(directory, must_exist=False)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.stat()
    _require(info.st_uid == os.getuid() and not info.st_mode & 0o022, "unit-directory")
    path = directory / _UNIT
    if path.is_symlink():
        raise InstallError("template-conflict")
    if path.exists():
        existing = path.stat()
        _require(
            stat.S_ISREG(existing.st_mode)
            and existing.st_uid == os.getuid()
            and not existing.st_mode & 0o022
            and existing.st_nlink == 1
            and path.read_bytes() == contents,
            "template-conflict",
        )
        return path
    # Hard-link publication is atomic and cannot overwrite an existing file.
    with tempfile.TemporaryDirectory(prefix=".template-", dir=directory) as temp:
        candidate = Path(temp) / _UNIT
        candidate.write_bytes(contents)
        candidate.chmod(0o644)
        os.link(candidate, path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    stage_args = sub.add_parser("stage")
    stage_args.add_argument("bridge_id")
    stage_args.add_argument("release")
    stage_args.add_argument("--source", type=Path, required=True)
    stage_args.add_argument("--uv", type=Path, required=True)
    stage_args.add_argument("--offline", action="store_true")
    select_args = sub.add_parser("select")
    select_args.add_argument("bridge_id")
    select_args.add_argument("release")
    template_args = sub.add_parser("install-template")
    template_args.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "stage":
            stage(args.bridge_id, args.release, args.source, args.uv, offline=args.offline)
        elif args.action == "select":
            select(args.bridge_id, args.release)
        else:
            install_template(args.source)
    except InstallError as exc:
        print(f"Linux Bridge installation refused ({exc})", file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        print("Linux Bridge installation refused (filesystem-or-configuration)", file=sys.stderr)
        return 2
    print(f"Linux Bridge {args.action} passed; no service activation performed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
