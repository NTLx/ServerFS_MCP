"""Static integrity gate for the two macOS v0.25.0 wheel archives."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import re
import sys
import zipfile
import zlib
from email.parser import Parser
from pathlib import Path, PurePosixPath
from stat import S_IFLNK, S_IFMT, S_IFREG

MAX_MEMBERS = 1_000
MAX_UNCOMPRESSED = 64 * 1024 * 1024
EXIT_INVALID = 2

ROOT_NAME = "serverfs-mcp"
BRIDGE_NAME = "serverfs-agent-bridge"
ROOT_REQUIRED = {
    "serverfs_mcp/darwin_lifecycle.py",
    "serverfs_mcp/darwin_shared_runtime.py",
    "serverfs_mcp/darwin_instance_shared_lifecycle.py",
    "serverfs_mcp/darwin_shared_binding_private.py",
    "serverfs_mcp/_darwin_service_manager/Sources/ServiceManager/main.swift",
    "serverfs_mcp/_darwin_service_manager/Sources/ServiceLauncher/main.swift",
    "serverfs_mcp/_darwin_service_manager/Sources/ServiceLauncher/SharedManifestGate.swift",
    "serverfs_mcp/_darwin_service_manager/build.sh",
}
BRIDGE_REQUIRED = {
    "serverfs_agent_bridge/core_grants.py",
    "serverfs_agent_bridge/core_grants_private.py",
    "serverfs_agent_bridge/protocol.py",
    "serverfs_agent_bridge/store.py",
}


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "_", value).lower()


def _safe_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if not name or name.endswith("/") or "\\" in name or name.startswith("/"):
        raise ValueError("unsafe archive member path")
    if PurePosixPath(name).is_absolute() or re.match(r"^[A-Za-z]:", name):
        raise ValueError("unsafe archive member path")
    parts = name.rstrip("/").split("/")
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise ValueError("unsafe archive member path")
    lowered = "/".join(parts).lower()
    if any(word in lowered for word in ("secret", "credential", "password", "token")):
        raise ValueError("sensitive archive member name")
    if any(part == ".env" or part.startswith(".env.") for part in parts):
        raise ValueError("sensitive archive member name")
    if info.flag_bits & 0x1:
        raise ValueError("encrypted archive member")
    mode = info.external_attr >> 16
    kind = S_IFMT(mode)
    if kind == S_IFLNK:
        raise ValueError("symlink archive member")
    if kind not in (0, S_IFREG) or (mode & 0o111):
        raise ValueError("non-regular or executable archive member")


def _verify_wheel(
    path: Path, package: str, expected_version: str, required: set[str]
) -> tuple[int, int, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_MEMBERS:
                raise ValueError("archive member count outside limit")
            if sum(item.file_size for item in members) > MAX_UNCOMPRESSED:
                raise ValueError("archive uncompressed size outside limit")
            names: set[str] = set()
            for item in members:
                _safe_member(item)
                if item.filename in names:
                    raise ValueError("duplicate archive member")
                names.add(item.filename)
            if not required.issubset(names):
                raise ValueError("required package files missing")

            dist_infos = [n for n in names if n.endswith(".dist-info/METADATA")]
            if len(dist_infos) != 1:
                raise ValueError("expected one distribution metadata file")
            dist_info = dist_infos[0].removesuffix("METADATA")
            expected_dist_info = f"{normalized_name(package)}-{expected_version}.dist-info/"
            if dist_info != expected_dist_info:
                raise ValueError("distribution metadata path mismatch")
            metadata = Parser().parsestr(archive.read(dist_infos[0]).decode("utf-8"))
            if normalized_name(metadata.get("Name", "")) != normalized_name(package):
                raise ValueError("distribution name mismatch")
            if metadata.get("Version") != expected_version:
                raise ValueError("distribution version mismatch")
            wheel_file = dist_info + "WHEEL"
            record_file = dist_info + "RECORD"
            if wheel_file not in names or record_file not in names:
                raise ValueError("distribution metadata set incomplete")
            if (
                sum(name.endswith(".dist-info/WHEEL") for name in names) != 1
                or sum(name.endswith(".dist-info/RECORD") for name in names) != 1
            ):
                raise ValueError("expected one WHEEL and RECORD")

            rows = list(
                csv.reader(
                    io.StringIO(archive.read(record_file).decode("utf-8"), newline=""), strict=True
                )
            )
            recorded: dict[str, tuple[str, str]] = {}
            for row in rows:
                if len(row) != 3 or row[0] in recorded:
                    raise ValueError("malformed or duplicate RECORD row")
                recorded[row[0]] = (row[1], row[2])
            if set(recorded) != names:
                raise ValueError("RECORD member set mismatch")
            for name, (digest, size) in recorded.items():
                if name == record_file:
                    if digest or size:
                        raise ValueError("RECORD self-entry must be empty")
                    continue
                if not digest.startswith("sha256=") or not re.fullmatch(
                    r"[A-Za-z0-9_-]+", digest[7:]
                ):
                    raise ValueError("RECORD digest format invalid")
                payload = archive.read(name)
                expected_digest = (
                    base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
                )
                if digest[7:] != expected_digest or size != str(len(payload)):
                    raise ValueError("RECORD digest or size mismatch")
            archive_digest = hashlib.sha256()
            with path.open("rb") as wheel_file_handle:
                for chunk in iter(lambda: wheel_file_handle.read(1024 * 1024), b""):
                    archive_digest.update(chunk)
            digest = archive_digest.hexdigest()
            return len(members), sum(item.file_size for item in members), digest
    except (
        OSError,
        UnicodeError,
        csv.Error,
        zipfile.BadZipFile,
        RuntimeError,
        KeyError,
        NotImplementedError,
        EOFError,
        zlib.error,
    ) as exc:
        raise ValueError("wheel archive cannot be read") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-wheel", required=True, type=Path)
    parser.add_argument("--bridge-wheel", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for label, path, package, required in (
        ("root", args.root_wheel, ROOT_NAME, ROOT_REQUIRED),
        ("bridge", args.bridge_wheel, BRIDGE_NAME, BRIDGE_REQUIRED),
    ):
        try:
            count, size, digest = _verify_wheel(path, package, args.expected_version, required)
        except ValueError as exc:
            print(f"{label}: integrity check failed ({exc})", file=sys.stderr)
            return EXIT_INVALID
        print(f"{label}: {path.name} members={count} uncompressed={size} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
