"""Tests for the pinned tunnel-client / native-wheel bootstrap chain.

The verification chain is exercised fully offline with fixture downloads:
manifest digest pin -> manifest entry -> archive re-hash -> top-level-only
extraction. Host allow-listing and digest arithmetic are pure unit checks;
the real upstream download is a recorded manual step (Phase F evidence).
"""

from __future__ import annotations

import hashlib
import io
import re
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from serverfs_mcp import tunnel_bootstrap as tb

CLIENT = "tunnel-client.exe" if sys.platform == "win32" else "tunnel-client"


def make_release_bundle() -> tuple[bytes, dict[str, bytes]]:
    """(manifest bytes, url-suffix -> payload) for a synthetic pinned release."""
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as bundle:
        bundle.writestr(CLIENT, b"client-payload")
        bundle.writestr("cloudflared.exe" if sys.platform == "win32" else "cloudflared", b"runtime")
        bundle.writestr("cloudflared-manifest.json", b"{}")
        bundle.writestr("LICENSE", b"mit")
        bundle.writestr("NOTICE", b"notice")
        bundle.writestr("tunnel-client-v0.0.15-windows-amd64-licenses.txt", b"provenance")
        bundle.writestr("tunnel-client-v0.0.15-windows-amd64.spdx.json", b"{}")
    archive = inner.getvalue()
    tag = tb.detect_platform_tag()
    asset = f"tunnel-client-{tb.TUNNEL_CLIENT_VERSION}-{tag}.zip"
    manifest = (
        f"{hashlib.sha256(archive).hexdigest()}  {asset}\n"
        f"{hashlib.sha256(b'other').hexdigest()}  unrelated-asset.zip\n"
    ).encode()
    files = {
        tb.MANIFEST_NAME: manifest,
        asset: archive,
    }
    return manifest, files


def fake_download(files: dict[str, bytes]):
    def _download(url: str, destination: Path) -> None:
        name = url.rsplit("/", 1)[-1]
        if name not in files:
            raise AssertionError(f"unexpected download URL {url}")
        destination.write_bytes(files[name])

    return _download


@pytest.fixture()
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "data"
    monkeypatch.setenv("SERVERFS_DATA_HOME", str(home))
    return home


class TestManifest:
    def test_pinned_constants_are_wellformed(self) -> None:
        assert re.fullmatch(r"[0-9a-f]{64}", tb.PINNED_MANIFEST_SHA256)
        assert tb.RELEASE_BASE_URL.startswith("https://github.com/openai/tunnel-client/")

    def test_parse_manifest_accepts_both_digest_styles(self) -> None:
        entries = tb.parse_manifest("a" * 64 + "  asset.zip\n" + "b" * 64 + " *binary-asset.zip\n")
        assert entries == {"asset.zip": "a" * 64, "binary-asset.zip": "b" * 64}

    def test_parse_manifest_rejects_malformed_lines(self) -> None:
        with pytest.raises(tb.BootstrapError):
            tb.parse_manifest("notadigest asset.zip")


class TestHostPolicy:
    @pytest.mark.parametrize(
        ("url", "allowed"),
        [
            ("https://github.com/o/r/releases/download/v1/a.zip", True),
            ("https://objects.githubusercontent.com/x", True),
            ("https://release-assets.githubusercontent.com/x", True),
            ("http://github.com/x", False),
            ("https://evil.example.com/x", False),
            ("https://github.com.evil.example/x", False),
        ],
    )
    def test_release_host_allowlist(self, url: str, allowed: bool) -> None:
        assert tb.is_allowed_release_host(url) is allowed

    def test_default_download_refuses_disallowed_host(self, tmp_path: Path) -> None:
        with pytest.raises(tb.BootstrapError):
            tb.default_download("https://evil.example/a.zip", tmp_path / "a.zip")


class TestBootstrapTunnelClient:
    def test_happy_install_excludes_provenance_and_keeps_runtime(self, data_home: Path) -> None:
        _manifest, files = make_release_bundle()
        exe = tb.bootstrap_tunnel_client(
            base_url="https://example.invalid/release/",
            download=fake_download(files),
            pinned_manifest_sha256=hashlib.sha256(files[tb.MANIFEST_NAME]).hexdigest(),
        )
        assert exe.is_file()
        assert exe.name == CLIENT
        installed = {p.name for p in exe.parent.iterdir()}
        assert "LICENSE" in installed and "NOTICE" in installed
        assert not any(name.endswith((".spdx.json", "-licenses.txt")) for name in installed)
        assert exe.parent.name.startswith(f"tunnel-client-{tb.TUNNEL_CLIENT_VERSION}-")

    def test_second_run_without_force_refuses(self, data_home: Path) -> None:
        _manifest, files = make_release_bundle()
        pin = hashlib.sha256(files[tb.MANIFEST_NAME]).hexdigest()
        kwargs = dict(
            base_url="https://example.invalid/release/",
            download=fake_download(files),
            pinned_manifest_sha256=pin,
        )
        tb.bootstrap_tunnel_client(**kwargs)
        with pytest.raises(tb.BootstrapError, match="already installed"):
            tb.bootstrap_tunnel_client(**kwargs)
        tb.bootstrap_tunnel_client(force=True, **kwargs)

    def test_manifest_digest_mismatch_is_refused_before_trust(self, data_home: Path) -> None:
        _manifest, files = make_release_bundle()
        with pytest.raises(tb.BootstrapError, match="manifest digest"):
            tb.bootstrap_tunnel_client(
                base_url="https://example.invalid/release/",
                download=fake_download(files),
                pinned_manifest_sha256="f" * 64,
            )

    def test_archive_digest_mismatch_is_refused(self, data_home: Path) -> None:
        manifest_bytes, files = make_release_bundle()
        tag = tb.detect_platform_tag()
        asset = f"tunnel-client-{tb.TUNNEL_CLIENT_VERSION}-{tag}.zip"
        files[asset] = b"tampered-bytes"
        with pytest.raises(tb.BootstrapError, match="digest does not match the release manifest"):
            tb.bootstrap_tunnel_client(
                base_url="https://example.invalid/release/",
                download=fake_download(files),
                pinned_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )

    def test_missing_asset_entry_is_refused(self, data_home: Path) -> None:
        manifest_bytes = b"aaaa" + b"0" * 60 + b"  something-else.zip\n"
        files = {tb.MANIFEST_NAME: manifest_bytes}
        with pytest.raises(tb.BootstrapError, match="no entry"):
            tb.bootstrap_tunnel_client(
                base_url="https://example.invalid/release/",
                download=fake_download(files),
                pinned_manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            )

    @pytest.mark.parametrize("member", ["../evil.txt", "sub/dir/thing.txt", "C:evil.txt"])
    def test_zip_path_traversal_is_refused(self, data_home: Path, member: str) -> None:
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as bundle:
            bundle.writestr(CLIENT, b"client")
            bundle.writestr(member, b"payload")
        archive = inner.getvalue()
        tag = tb.detect_platform_tag()
        asset = f"tunnel-client-{tb.TUNNEL_CLIENT_VERSION}-{tag}.zip"
        manifest = f"{hashlib.sha256(archive).hexdigest()}  {asset}\n".encode()
        files = {
            tb.MANIFEST_NAME: manifest,
            asset: archive,
        }
        with pytest.raises(tb.BootstrapError, match="refusing"):
            tb.bootstrap_tunnel_client(
                base_url="https://example.invalid/release/",
                download=fake_download(files),
                pinned_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            )

    def test_missing_client_member_is_refused(self, data_home: Path) -> None:
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, "w") as bundle:
            bundle.writestr("LICENSE", b"mit")
        archive = inner.getvalue()
        tag = tb.detect_platform_tag()
        asset = f"tunnel-client-{tb.TUNNEL_CLIENT_VERSION}-{tag}.zip"
        manifest = f"{hashlib.sha256(archive).hexdigest()}  {asset}\n".encode()
        files = {tb.MANIFEST_NAME: manifest, asset: archive}
        with pytest.raises(tb.BootstrapError, match="client executable"):
            tb.bootstrap_tunnel_client(
                base_url="https://example.invalid/release/",
                download=fake_download(files),
                pinned_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
            )


class TestDefaultPathDiscovery:
    def test_nothing_installed(self, data_home: Path) -> None:
        assert tb.default_tunnel_client_path() is None

    def test_highest_semver_wins(self, data_home: Path) -> None:
        tag = tb.detect_platform_tag()
        for version in ("v0.0.9", "v0.0.15"):
            install = data_home / "bin" / f"tunnel-client-{version}-{tag}"
            install.mkdir(parents=True)
            (install / CLIENT).write_bytes(b"x")
        found = tb.default_tunnel_client_path()
        assert found is not None
        assert "v0.0.15" in found.parent.name

    def test_partial_install_without_executable_ignored(self, data_home: Path) -> None:
        install = data_home / "bin" / "tunnel-client-v9.9.9-linux-amd64"
        install.mkdir(parents=True)
        assert tb.default_tunnel_client_path() is None


class TestNativeWheelBootstrap:
    def test_digest_verified_wheel_is_stored(self, data_home: Path) -> None:
        payload = b"fake-wheel-bytes"
        files = {"serverfs_windows_native-0.10.0-cp312-abi3-win_amd64.whl": payload}

        def _download(url: str, destination: Path) -> None:
            destination.write_bytes(files[url.rsplit("/", 1)[-1]])

        path = tb.bootstrap_native_wheel(
            url="https://github.com/NTLx/ServerFS_MCP/releases/download/v0.10.0/"
            "serverfs_windows_native-0.10.0-cp312-abi3-win_amd64.whl",
            sha256=hashlib.sha256(payload).hexdigest(),
            download=_download,
        )
        assert path.is_file()
        assert path.parent == data_home / "wheels"
        assert path.read_bytes() == payload

    def test_digest_mismatch_refused(self, data_home: Path) -> None:
        with pytest.raises(tb.BootstrapError, match="wheel digest"):
            tb.bootstrap_native_wheel(
                url="https://github.com/x/releases/download/v1/a.whl",
                sha256="0" * 64,
                download=lambda url, dest: dest.write_bytes(b"different"),
            )

    def test_non_wheel_url_and_bad_digest_shape_refused(self, data_home: Path) -> None:
        with pytest.raises(tb.BootstrapError, match="\\.whl"):
            tb.bootstrap_native_wheel(
                url="https://github.com/x/releases/download/v1/a.zip",
                sha256="0" * 64,
                download=lambda url, dest: None,
            )
        with pytest.raises(tb.BootstrapError, match="64 hex"):
            tb.bootstrap_native_wheel(
                url="https://github.com/x/releases/download/v1/a.whl",
                sha256="short",
                download=lambda url, dest: None,
            )


def test_data_dir_honors_override_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SERVERFS_DATA_HOME", str(tmp_path / "override"))
    assert tb.serverfs_data_dir() == tmp_path / "override"
    shutil.rmtree(tmp_path / "override", ignore_errors=True)
