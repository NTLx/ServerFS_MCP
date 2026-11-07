import hashlib
import io
import re
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
    monkeypatch.setattr(bootstrap, "_host_platform", lambda: "darwin")
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


def windows_signed_runner(*, unsigned=False, wrong_version=False):
    def run(args, **_kwargs):
        if args[0] == "powershell.exe":
            return subprocess.CompletedProcess(
                args,
                3 if unsigned else 0,
                "" if unsigned else "valid-cloudflare-signature\n",
                "",
            )
        return subprocess.CompletedProcess(
            args,
            0,
            "2025.1.1" if wrong_version else bootstrap.VERSION,
            "",
        )

    return run


@pytest.fixture
def windows_binary(tmp_path, monkeypatch):
    binary = tmp_path / bootstrap.WINDOWS_ASSET
    binary.write_bytes(b"fixture Windows cloudflared executable")
    monkeypatch.setattr(bootstrap, "WINDOWS_BINARY_SHA256", bootstrap._sha256(binary))
    monkeypatch.setattr(bootstrap, "_host_platform", lambda: "win32")
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "AMD64")
    return binary


@pytest.mark.parametrize(
    ("subject", "valid"),
    [
        ('CN="Cloudflare, Inc.", O="Cloudflare, Inc.", C=US', True),
        ("CN=Cloudflare, Inc., O=Cloudflare, Inc., C=US", True),
        ("CN=Cloudflare Inc., O=Cloudflare Inc., C=US", True),
        ('CN="Cloudflare, Inc. Evil", O=Wrong, C=US', False),
        ('O=Wrong, CN="Cloudflare, Inc.", C=US', False),
        ("CN=Malicious Cloudflare, Inc., O=Wrong", False),
        ("CN=Cloudflare, Inc. Evil, O=Wrong", False),
    ],
)
def test_windows_signer_cn_matches_only_expected_issuer(subject, valid):
    assert bool(re.search(bootstrap.WINDOWS_SIGNER_SUBJECT_RE, subject)) is valid


def test_windows_signature_probe_drops_inherited_pwsh7_module_path(windows_binary, monkeypatch):
    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    probed = []

    def runner(args, **kwargs):
        probed.append((args, kwargs))
        return windows_signed_runner()(args, **kwargs)

    bootstrap._verify_windows_binary(windows_binary, runner=runner)
    assert probed[0][0][0] == "powershell.exe"
    assert "PSModulePath" not in probed[0][1]["env"]


def test_windows_bootstrap_installs_and_is_idempotent(windows_binary, tmp_path):
    target = tmp_path / "data" / "bin"
    path = bootstrap.bootstrap_cloudflare_client(
        artifact=windows_binary, dest_parent=target, runner=windows_signed_runner()
    )
    assert path == target / f"cloudflared-{bootstrap.VERSION}-windows-amd64" / "cloudflared.exe"
    assert path.read_bytes() == windows_binary.read_bytes()
    assert (
        bootstrap.bootstrap_cloudflare_client(
            artifact=windows_binary, dest_parent=target, runner=windows_signed_runner()
        )
        == path
    )


def test_windows_bootstrap_rejects_digest_mismatch(windows_binary, tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "WINDOWS_BINARY_SHA256", "0" * 64)
    with pytest.raises(bootstrap.CloudflareBootstrapError, match="digest mismatch"):
        bootstrap.bootstrap_cloudflare_client(
            artifact=windows_binary,
            dest_parent=tmp_path / "bin",
            runner=windows_signed_runner(),
        )


@pytest.mark.parametrize("unsigned,wrong_version", [(True, False), (False, True)])
def test_windows_bootstrap_rejects_bad_signature_or_version(
    windows_binary, tmp_path, unsigned, wrong_version
):
    with pytest.raises(bootstrap.CloudflareBootstrapError, match="signature|version"):
        bootstrap.bootstrap_cloudflare_client(
            artifact=windows_binary,
            dest_parent=tmp_path / "bin",
            runner=windows_signed_runner(unsigned=unsigned, wrong_version=wrong_version),
        )


def test_windows_bootstrap_requires_x64(windows_binary, tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: "ARM64")
    with pytest.raises(bootstrap.CloudflareBootstrapError, match="Windows x64"):
        bootstrap.bootstrap_cloudflare_client(
            artifact=windows_binary,
            dest_parent=tmp_path / "bin",
            runner=windows_signed_runner(),
        )


def test_windows_bootstrap_rejects_tampered_existing_binary(windows_binary, tmp_path):
    destination = tmp_path / "bin"
    path = bootstrap.bootstrap_cloudflare_client(
        artifact=windows_binary, dest_parent=destination, runner=windows_signed_runner()
    )
    path.write_bytes(b"tampered")
    with pytest.raises(bootstrap.CloudflareBootstrapError, match="existing cloudflared install"):
        bootstrap.bootstrap_cloudflare_client(
            artifact=windows_binary, dest_parent=destination, runner=windows_signed_runner()
        )
