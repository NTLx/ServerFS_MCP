import hashlib
import io
import subprocess
import tarfile

import pytest

from serverfs_mcp import cloudflare_bootstrap as bootstrap


@pytest.fixture
def archive(tmp_path, monkeypatch):
    binary_data = b"fixture cloudflared binary"
    binary = tmp_path / "cloudflared"
    binary.write_bytes(binary_data)
    packed = tmp_path / bootstrap.ASSET
    with tarfile.open(packed, "w:gz") as tar:
        tar.add(binary, arcname="cloudflared")
    monkeypatch.setattr(bootstrap, "ARCHIVE_SHA256", bootstrap._sha256(packed))
    monkeypatch.setattr(bootstrap, "BINARY_SHA256", hashlib.sha256(binary_data).hexdigest())
    return packed


def signed_runner(*, unsigned=False, wrong_version=False):
    def run(args, **_kwargs):
        command = args[0]
        if command == "codesign":
            if "--verify" in args:
                return subprocess.CompletedProcess(args, 1 if unsigned else 0, "", "")
            signer = "Authority=Wrong Signer" if unsigned else f"Authority={bootstrap.IDENTITY}"
            details = f"{signer}\nTeamIdentifier={bootstrap.TEAM_ID}\n"
            return subprocess.CompletedProcess(args, 0, "", details)
        version = "2025.1.1" if wrong_version else bootstrap.VERSION
        return subprocess.CompletedProcess(args, 0, version, "")

    return run


@pytest.fixture(autouse=True)
def supported_platform(monkeypatch):
    monkeypatch.setattr(bootstrap.sys, "platform", "darwin")
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "arm64")


def install(archive, destination, runner=None):
    return bootstrap.bootstrap_cloudflare_client(
        artifact=archive,
        dest_parent=destination,
        runner=runner or signed_runner(),
    )


def test_installs_versioned_binary_idempotently(archive, tmp_path):
    path = install(archive, tmp_path / "data" / "bin")
    assert path.is_absolute()
    assert (
        path
        == tmp_path
        / "data"
        / "bin"
        / f"cloudflared-{bootstrap.VERSION}-darwin-arm64"
        / "cloudflared"
    )
    assert install(archive, tmp_path / "data" / "bin").read_bytes() == path.read_bytes()


def test_rejects_archive_digest_mismatch(archive, tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "ARCHIVE_SHA256", "0" * 64)
    with pytest.raises(bootstrap.CloudflareBootstrapError, match="archive digest"):
        install(archive, tmp_path / "bin")


def test_rejects_unsigned_binary(archive, tmp_path):
    with pytest.raises(bootstrap.CloudflareBootstrapError, match="signature"):
        install(archive, tmp_path / "bin", signed_runner(unsigned=True))


@pytest.mark.parametrize(
    "member_name, kind", [("../cloudflared", "file"), ("cloudflared", "symlink")]
)
def test_rejects_unsafe_archive_members(tmp_path, member_name, kind, monkeypatch):
    data = b"fake"
    archive_path = tmp_path / "unsafe.tgz"
    with tarfile.open(archive_path, "w:gz") as tar:
        info = tarfile.TarInfo(member_name)
        if kind == "symlink":
            info.type = tarfile.SYMTYPE
            info.linkname = "/tmp/target"
        else:
            info.size = len(data)
        tar.addfile(info, io.BytesIO(data) if kind == "file" else None)
    monkeypatch.setattr(bootstrap, "ARCHIVE_SHA256", bootstrap._sha256(archive_path))
    with pytest.raises(bootstrap.CloudflareBootstrapError, match="unsafe|unexpected"):
        install(archive_path, tmp_path / "bin")


def test_does_not_overwrite_existing_binary(archive, tmp_path):
    path = install(archive, tmp_path / "bin")
    original = path.read_bytes()
    path.write_bytes(b"existing live binary")
    with pytest.raises(bootstrap.CloudflareBootstrapError, match="existing cloudflared install"):
        install(archive, tmp_path / "bin")
    assert path.read_bytes() == b"existing live binary"
    assert original != path.read_bytes()


def test_refuses_unsupported_platform(archive, tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "x86_64")
    with pytest.raises(bootstrap.CloudflareBootstrapError, match="macOS arm64"):
        install(archive, tmp_path / "bin")
