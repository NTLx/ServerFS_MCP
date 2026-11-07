"""Install the pinned official Cloudflare cloudflared client on macOS arm64."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

VERSION = "2026.10.7"
ASSET = "cloudflared-darwin-arm64.tgz"
ARCHIVE_SHA256 = "3586ccf397e98e5f437e60e7c307ca6ac463e110580503af8bb8e6f9ebf831bd"
BINARY_SHA256 = "2136431d5eb467bf447d2ee514b713e192719b143aec1d9e09bf7dc290d5bb12"
TEAM_ID = "68WVV388M8"
IDENTITY = "Developer ID Application: Cloudflare Inc. (68WVV388M8)"
RELEASE_URL = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/{ASSET}"
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
TIMEOUT = 60


class CloudflareBootstrapError(Exception):
    """Safe operator-facing cloudflared installation error."""


def serverfs_data_dir() -> Path:
    override = os.environ.get("SERVERFS_DATA_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "ServerFS"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if not base:
            raise CloudflareBootstrapError("LOCALAPPDATA is not set; configure SERVERFS_DATA_HOME")
        return Path(base) / "ServerFS"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(xdg) / "serverfs" if xdg else Path.home() / ".local" / "share" / "serverfs"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    parsed = urllib.parse.urlparse(url)
    expected = urllib.parse.urlparse(RELEASE_URL)
    if (parsed.scheme, parsed.netloc, parsed.path) != ("https", expected.netloc, expected.path):
        raise CloudflareBootstrapError(
            "only the pinned official Cloudflare release asset is allowed"
        )
    request = urllib.request.Request(url, headers={"User-Agent": "serverfs-bootstrap"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            final = urllib.parse.urlparse(response.geturl())
            if final.scheme != "https" or final.hostname not in {
                "github.com",
                "objects.githubusercontent.com",
                "release-assets.githubusercontent.com",
            }:
                raise CloudflareBootstrapError(
                    "release download redirected outside official GitHub hosts"
                )
            total = 0
            with destination.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise CloudflareBootstrapError("release archive exceeds the size limit")
                    output.write(chunk)
    except (OSError, TimeoutError) as exc:
        raise CloudflareBootstrapError("could not download the official release asset") from exc


def _extract(archive: Path, destination: Path) -> Path:
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            if len(members) != 1:
                raise CloudflareBootstrapError(
                    "archive must contain exactly one cloudflared binary"
                )
            member = members[0]
            if (
                member.name != "cloudflared"
                or not member.isfile()
                or member.issym()
                or member.islnk()
            ):
                raise CloudflareBootstrapError("archive contains an unsafe or unexpected member")
            if member.size > MAX_ARCHIVE_BYTES:
                raise CloudflareBootstrapError("cloudflared binary exceeds the size limit")
            source = bundle.extractfile(member)
            if source is None:
                raise CloudflareBootstrapError("archive binary could not be read")
            target = destination / "cloudflared"
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(0o755)
            return target
    except (tarfile.TarError, OSError) as exc:
        raise CloudflareBootstrapError("invalid Cloudflare release archive") from exc


def _verify_binary(binary: Path, runner: Callable = subprocess.run) -> None:
    try:
        result = runner(
            ["codesign", "--verify", "--strict", "--verbose=2", str(binary)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
        details = runner(
            ["codesign", "-dv", "--verbose=4", str(binary)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
        version = runner(
            [str(binary), "--version"], capture_output=True, text=True, timeout=TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CloudflareBootstrapError(
            "could not verify cloudflared signature and version"
        ) from exc
    identity_lines = (details.stderr + details.stdout).splitlines()
    if (
        result.returncode
        or not any(line.strip() == f"Authority={IDENTITY}" for line in identity_lines)
        or not any(line.strip() == f"TeamIdentifier={TEAM_ID}" for line in identity_lines)
    ):
        raise CloudflareBootstrapError(
            "cloudflared signature is invalid or is not signed by Cloudflare"
        )
    if version.returncode or VERSION not in (version.stdout + version.stderr):
        raise CloudflareBootstrapError(
            "cloudflared binary version does not match the pinned release"
        )


def bootstrap_cloudflare_client(
    *,
    artifact: Path | None = None,
    dest_parent: Path | None = None,
    download: Callable[[str, Path], None] = _download,
    runner: Callable = subprocess.run,
) -> Path:
    if sys.platform != "darwin" or platform.machine().lower() != "arm64":
        raise CloudflareBootstrapError("cloudflared bootstrap requires native macOS arm64")
    install_dir = (
        dest_parent or serverfs_data_dir() / "bin"
    ) / f"cloudflared-{VERSION}-darwin-arm64"
    executable = install_dir / "cloudflared"
    if executable.exists():
        if executable.is_file() and _sha256(executable) == BINARY_SHA256:
            _verify_binary(executable, runner)
            return executable
        raise CloudflareBootstrapError(
            "an existing cloudflared install is invalid; refusing to overwrite it"
        )
    install_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="serverfs-cloudflared-") as scratch:
        scratch_path = Path(scratch)
        archive = scratch_path / ASSET
        if artifact is None:
            download(RELEASE_URL, archive)
        else:
            shutil.copyfile(artifact, archive)
        if _sha256(archive) != ARCHIVE_SHA256:
            raise CloudflareBootstrapError("Cloudflare release archive digest mismatch")
        staging = scratch_path / "stage"
        staging.mkdir()
        binary = _extract(archive, staging)
        if _sha256(binary) != BINARY_SHA256:
            raise CloudflareBootstrapError("cloudflared binary digest mismatch")
        _verify_binary(binary, runner)
        try:
            staging.rename(install_dir)
        except FileExistsError as exc:
            raise CloudflareBootstrapError(
                "cloudflared install appeared concurrently; refusing overwrite"
            ) from exc
    return executable
