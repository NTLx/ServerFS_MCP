from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import stat
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "v0250_wheel_archive_gate",
    ROOT / "deployment" / "macos" / "v0250_wheel_archive_gate.py",
)
assert _SPEC and _SPEC.loader
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)

with (ROOT / "pyproject.toml").open("rb") as project_file:
    VERSION = tomllib.load(project_file)["project"]["version"]


def _wheel(
    tmp_path: Path,
    package: str = "serverfs-mcp",
    version: str = VERSION,
    metadata_version: str | None = None,
    extra: str | None = None,
    symlink: str | None = None,
    executable: str | None = None,
    omit: str | None = None,
) -> Path:
    root = "serverfs_mcp" if package == "serverfs-mcp" else "serverfs_agent_bridge"
    required = gate.ROOT_REQUIRED if package == "serverfs-mcp" else gate.BRIDGE_REQUIRED
    files = {name: b"static harmless payload\n" for name in required if name != omit}
    dist = f"{root}-{version}.dist-info"
    files[f"{dist}/WHEEL"] = b"Wheel-Version: 1.0\n"
    metadata_name = "serverfs_mcp" if package == "serverfs-mcp" else "serverfs_agent_bridge"
    metadata_version = metadata_version or version
    files[f"{dist}/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {metadata_name}\nVersion: {metadata_version}\n".encode()
    )
    record = f"{dist}/RECORD"
    if extra:
        files[extra] = b"unrecorded payload"
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, payload in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", str(len(payload))))
    writer.writerow((record, "", ""))
    path = tmp_path / "synthetic.whl"
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in files.items():
            if name == symlink:
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, payload)
            elif name == executable:
                info = zipfile.ZipInfo(name)
                info.external_attr = (stat.S_IFREG | 0o755) << 16
                archive.writestr(info, payload)
            else:
                archive.writestr(name, payload)
        archive.writestr(record, output.getvalue())
    return path


def _inspect(path: Path, package: str = "serverfs-mcp") -> tuple[int, int, str]:
    required = gate.ROOT_REQUIRED if package == "serverfs-mcp" else gate.BRIDGE_REQUIRED
    return gate._verify_wheel(path, package, VERSION, required)


def _rewrite_record(wheel: Path, record: str, contents: str) -> None:
    replacement = wheel.with_name("replacement.whl")
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(replacement, "w") as target:
        for name in source.namelist():
            target.writestr(name, contents if name == record else source.read(name))
    replacement.replace(wheel)


def test_valid_static_wheels_pass_for_both_packages(tmp_path: Path) -> None:
    assert _inspect(_wheel(tmp_path))[0] > 0
    assert _inspect(_wheel(tmp_path, "serverfs-agent-bridge"), "serverfs-agent-bridge")[0] > 0


@pytest.mark.parametrize(
    "package,required_member",
    [
        ("serverfs-mcp", "serverfs_mcp/darwin_shared_binding_private.py"),
        ("serverfs-agent-bridge", "serverfs_agent_bridge/core_grants_private.py"),
    ],
)
def test_missing_private_binding_source_is_rejected_even_with_valid_record(
    tmp_path: Path, package: str, required_member: str
) -> None:
    """A source module named private is mandatory code, not an optional credential file."""
    required = gate.ROOT_REQUIRED if package == "serverfs-mcp" else gate.BRIDGE_REQUIRED
    assert required_member in required
    with pytest.raises(ValueError, match="required package files missing"):
        _inspect(_wheel(tmp_path, package=package, omit=required_member), package)


def test_metadata_mismatch_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="version mismatch"):
        _inspect(_wheel(tmp_path, metadata_version="0.24.1"))


@pytest.mark.parametrize("mutation", ["hash", "size"])
def test_record_hash_and_size_are_checked(tmp_path: Path, mutation: str) -> None:
    wheel = _wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        target = names[0]
        record = next(name for name in names if name.endswith("/RECORD"))
        rows = list(csv.reader(io.StringIO(archive.read(record).decode())))
    index = next(i for i, row in enumerate(rows) if row[0] == target)
    rows[index][1 if mutation == "hash" else 2] = "sha256=AAAA" if mutation == "hash" else "999"
    _rewrite_record(wheel, record, "".join(",".join(row) + "\n" for row in rows))
    with pytest.raises(ValueError, match="RECORD digest or size mismatch"):
        _inspect(wheel)


def test_missing_record_fails(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    with zipfile.ZipFile(wheel) as source:
        entries = [
            (item, source.read(item)) for item in source.namelist() if not item.endswith("/RECORD")
        ]
    replacement = tmp_path / "no-record.whl"
    with zipfile.ZipFile(replacement, "w") as target:
        for name, data in entries:
            target.writestr(name, data)
    with pytest.raises(ValueError, match="metadata set incomplete"):
        _inspect(replacement)


def test_unexpected_unrecorded_member_fails(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("serverfs_mcp/unexpected.py", b"harmless")
    with pytest.raises(ValueError, match="RECORD member set mismatch"):
        _inspect(wheel)


@pytest.mark.parametrize(
    "member", ["../escape", ".env", "serverfs_mcp/.secret", "serverfs_mcp/key_credentials.json"]
)
def test_unsafe_or_hidden_secret_members_fail(tmp_path: Path, member: str) -> None:
    wheel = _wheel(tmp_path, extra=member)
    with pytest.raises(ValueError):
        _inspect(wheel)


def test_duplicate_member_fails(tmp_path: Path) -> None:
    wheel = _wheel(tmp_path)
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(archive.namelist()[0], b"duplicate")
    with pytest.raises(ValueError, match="duplicate archive member"):
        _inspect(wheel)


def test_symlink_member_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="symlink archive member"):
        _inspect(_wheel(tmp_path, extra="serverfs_mcp/link", symlink="serverfs_mcp/link"))


def test_executable_payload_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-regular or executable"):
        _inspect(_wheel(tmp_path, extra="serverfs_mcp/tool", executable="serverfs_mcp/tool"))
