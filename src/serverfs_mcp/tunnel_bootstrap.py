"""Pinned project-local bootstrap for official OpenAI tunnel-client and the
Windows native wheel (v0.10 Phase E2, §25).

The filesystem service must stay fully runnable without anything in this
module: tunnel-client is connectivity infrastructure, not part of the
filesystem security kernel.

Verification chain (deliberately double-anchor):

1. the release manifest ``SHA256SUMS.txt`` is downloaded and its own
   SHA-256 is compared against the digest pinned in this module — the
   manifest is only trusted after that check;
2. the platform archive's digest is taken from the trusted manifest;
3. the downloaded archive is re-hashed and must match before anything is
   extracted;
4. extraction accepts top-level regular files only (no traversal, no
   directories, no absolute paths), excluding non-runtime provenance
   assets, and lands beneath the user-owned ServerFS data directory.

Downloads never modify the global PATH and refuse redirect targets outside
the GitHub release asset domains.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

TUNNEL_CLIENT_VERSION = "v0.0.15"
RELEASE_BASE_URL = (
    "https://github.com/openai/tunnel-client/releases/download/" + TUNNEL_CLIENT_VERSION + "/"
)
MANIFEST_NAME = "SHA256SUMS.txt"
# sha256sum of the upstream v0.0.15 SHA256SUMS.txt (measured on the official
# release; also identical to the asset digest GitHub computes).
PINNED_MANIFEST_SHA256 = "8a32bbcd724468f1874f12d5b0dedb6e6b07dfe5aa323cf5b4c070a5a81b0b4e"

# The official zips ship the client plus its cloudflared runtime; extracting
# "only the client exe" would break the launcher, so the whole top-level
# runtime set is kept and only provenance/documentation assets are dropped.
_EXCLUDED_SUFFIXES = (".spdx.json", "-licenses.txt")

_MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})[ *]{1,2}(\S+)$")
_ALLOWED_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
_DOWNLOAD_TIMEOUT = 60.0


class BootstrapError(Exception):
    """Operator-facing bootstrap failure; messages never contain secrets."""


def serverfs_data_dir() -> Path:
    """User-owned data directory for bootstrap-managed artifacts."""
    override = os.environ.get("SERVERFS_DATA_HOME", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "").strip()
        if not base:
            raise BootstrapError("LOCALAPPDATA is not set; configure SERVERFS_DATA_HOME")
        return Path(base) / "ServerFS"
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg:
        return Path(xdg) / "serverfs"
    return Path.home() / ".local" / "share" / "serverfs"


def detect_platform_tag() -> str:
    machine = platform.machine().lower()
    unified = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
    arch = unified.get(machine)
    system = {"win32": "windows", "linux": "linux", "darwin": "darwin"}.get(sys.platform)
    if system is None or arch is None:
        raise BootstrapError(f"no official tunnel-client distribution for {sys.platform}/{machine}")
    return f"{system}-{arch}"


def tunnel_client_executable_name() -> str:
    return "tunnel-client.exe" if sys.platform == "win32" else "tunnel-client"


def tunnel_client_install_dir(version: str, platform_tag: str, dest_parent: Path | None = None):
    base = dest_parent if dest_parent is not None else serverfs_data_dir() / "bin"
    return base / f"tunnel-client-{version}-{platform_tag}"


def default_tunnel_client_path() -> Path | None:
    """Most recent bootstrapped tunnel-client, or None when nothing is installed."""
    try:
        bin_root = serverfs_data_dir() / "bin"
    except BootstrapError:
        return None
    if not bin_root.is_dir():
        return None
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for entry in bin_dir_entries(bin_root):
        match = re.fullmatch(r"tunnel-client-v(\d+)\.(\d+)\.(\d+)-[a-z]+-[a-z0-9]+", entry.name)
        exe = entry / tunnel_client_executable_name()
        if match and exe.is_file():
            candidates.append((tuple(int(p) for p in match.groups()), exe))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def bin_dir_entries(bin_root: Path) -> list[Path]:
    try:
        return [p for p in bin_root.iterdir() if p.is_dir()]
    except OSError:
        return []


def parse_manifest(text: str) -> dict[str, str]:
    """Map asset name -> lowercase sha256 from an upstream SHA256SUMS file."""
    entries: dict[str, str] = {}
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        match = _MANIFEST_LINE.match(candidate)
        if not match:
            raise BootstrapError("malformed SHA256SUMS entry")
        digest, name = match.groups()
        entries[os.path.basename(name)] = digest.lower()
    return entries


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_allowed_release_host(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in _ALLOWED_HOSTS


def default_download(url: str, destination: Path) -> None:
    if not is_allowed_release_host(url):
        raise BootstrapError("download URLs must be https on the GitHub release asset hosts")
    request = urllib.request.Request(url, headers={"User-Agent": "serverfs-bootstrap"})
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as response:
        final_host = urllib.parse.urlparse(response.geturl()).hostname
        if final_host not in _ALLOWED_HOSTS:
            raise BootstrapError("download was redirected outside the GitHub release hosts")
        with open(destination, "wb") as fh:
            shutil.copyfileobj(response, fh, length=1024 * 1024)


def _extract_runtime_members(archive: Path, target: Path) -> list[str]:
    extracted: list[str] = []
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            name = info.filename
            if info.is_dir():
                continue
            base = name.replace("\\", "/")
            parts = [p for p in base.split("/") if p]
            if len(parts) != 1 or parts[0] in (".", "..") or ":" in parts[0]:
                raise BootstrapError("archive contains a path outside its root; refusing")
            if parts[0] != os.path.basename(parts[0]):
                raise BootstrapError("archive contains an unexpected member; refusing")
            if parts[0].endswith(_EXCLUDED_SUFFIXES):
                continue
            with bundle.open(info) as source, open(target / parts[0], "wb") as sink:
                shutil.copyfileobj(source, sink)
            extracted.append(parts[0])
    return extracted


def bootstrap_tunnel_client(
    *,
    dest_parent: Path | None = None,
    version: str = TUNNEL_CLIENT_VERSION,
    platform_tag: str | None = None,
    force: bool = False,
    base_url: str = RELEASE_BASE_URL,
    download: Callable[[str, Path], None] = default_download,
    pinned_manifest_sha256: str = PINNED_MANIFEST_SHA256,
) -> Path:
    """Download, verify and install the pinned tunnel-client release.

    Returns the path of the installed client executable.
    """
    tag = platform_tag or detect_platform_tag()
    install_dir = tunnel_client_install_dir(version, tag, dest_parent)
    executable = install_dir / tunnel_client_executable_name()
    if executable.exists() and not force:
        raise BootstrapError(f"tunnel-client {version} ({tag}) is already installed; use --force")
    asset_name = f"tunnel-client-{version}-{tag}.zip"
    with tempfile.TemporaryDirectory(prefix="serverfs-bootstrap-") as scratch:
        scratch_dir = Path(scratch)
        manifest_path = scratch_dir / MANIFEST_NAME
        download(base_url + MANIFEST_NAME, manifest_path)
        manifest_bytes = manifest_path.read_bytes()
        if sha256_of_bytes(manifest_bytes) != pinned_manifest_sha256:
            raise BootstrapError("release manifest digest does not match the pinned value")
        entries = parse_manifest(manifest_bytes.decode("utf-8"))
        if asset_name not in entries:
            raise BootstrapError(f"pinned manifest has no entry for {asset_name}")
        archive_path = scratch_dir / asset_name
        download(base_url + asset_name, archive_path)
        if sha256_of_file(archive_path) != entries[asset_name]:
            raise BootstrapError(f"{asset_name} digest does not match the release manifest")
        staging = install_dir.parent / f".{install_dir.name}.tmp-{os.getpid()}"
        staging.mkdir(parents=True, exist_ok=True)
        try:
            _extract_runtime_members(archive_path, staging)
            if not (staging / tunnel_client_executable_name()).is_file():
                raise BootstrapError("archive did not contain the client executable")
            if sys.platform != "win32":
                (staging / tunnel_client_executable_name()).chmod(0o755)
            if install_dir.exists():
                if not force:
                    raise BootstrapError("install directory appeared concurrently; refusing")
                shutil.rmtree(install_dir)
            staging.rename(install_dir)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    return executable


def bootstrap_native_wheel(
    *,
    url: str,
    sha256: str,
    dest_parent: Path | None = None,
    download: Callable[[str, Path], None] = default_download,
) -> Path:
    """Download a published native wheel, verify its release-pinned SHA-256
    and store it under the ServerFS data directory. Installation itself stays
    an explicit operator step (``uv pip install`` / ``python -m pip``)."""
    name = os.path.basename(urllib.parse.urlparse(url).path)
    if not name.endswith(".whl"):
        raise BootstrapError("native wheel URL must point at a .whl file")
    expected = sha256.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise BootstrapError("expected SHA-256 must be 64 hex characters")
    base = dest_parent if dest_parent is not None else serverfs_data_dir() / "wheels"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="serverfs-wheel-") as scratch:
        staged = Path(scratch) / name
        download(url, staged)
        if sha256_of_file(staged) != expected:
            raise BootstrapError("wheel digest does not match the expected SHA-256")
        final = base / name
        if final.exists():
            final.unlink()
        shutil.move(str(staged), str(final))
    return final


__all__ = [
    "BootstrapError",
    "PINNED_MANIFEST_SHA256",
    "RELEASE_BASE_URL",
    "TUNNEL_CLIENT_VERSION",
    "bootstrap_native_wheel",
    "bootstrap_tunnel_client",
    "default_tunnel_client_path",
    "detect_platform_tag",
    "parse_manifest",
    "serverfs_data_dir",
    "sha256_of_file",
    "tunnel_client_install_dir",
]
