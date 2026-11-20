"""Read-only W2g recovery proof for exactly one pre-existing private candidate venv.

The original W2g attempt populated bridge-owner-env before its subsequent ACL
hardening failed. We can finish that environment only after proving the Product
and Native installed bytes match the already host-accepted W2f wheel candidates.
This helper never installs, changes ACLs, deletes files or touches services.
"""

from __future__ import annotations

import hashlib
import os
import stat
import sys
import zipfile
from pathlib import Path

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class RecoveryProofError(ValueError):
    """Fixed installation recovery refusal, safe without full private paths."""


def _ordinary(path: Path, *, directory: bool) -> None:
    try:
        mode = path.lstat()
    except OSError as exc:
        raise RecoveryProofError("required candidate file is missing") from exc
    if (getattr(mode, "st_file_attributes", 0) & _REPARSE) or stat.S_ISLNK(mode.st_mode):
        raise RecoveryProofError("candidate contains a redirected object")
    if directory and not stat.S_ISDIR(mode.st_mode):
        raise RecoveryProofError("candidate directory is invalid")
    if not directory and not stat.S_ISREG(mode.st_mode):
        raise RecoveryProofError("candidate file is invalid")


def _unshared(path: Path) -> None:
    _ordinary(path, directory=False)
    if path.lstat().st_nlink != 1:
        raise RecoveryProofError("candidate Python interpreter has multiple hardlinks")


def _actual_file_matches_bytes(path: Path, expected: bytes) -> None:
    _ordinary(path, directory=False)
    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(expected).digest():
        raise RecoveryProofError("candidate installed package differs from accepted wheel")


def verify_one_wheel(site: Path, archive_path: Path, package: str) -> int:
    """Byte-compare every installed package file and wheel's metadata, not just imports.

    Installer-generated RECORD and direct_url.json are intentionally not compared:
    uv is allowed to regenerate those when installing a wheel.
    """
    _ordinary(archive_path, directory=False)
    total = 0
    extension = False
    with zipfile.ZipFile(archive_path) as wheel:
        seen: set[str] = set()
        metadata_files: list[str] = []
        for info in wheel.infolist():
            member = info.filename
            if member in seen:
                raise RecoveryProofError("candidate wheel contains duplicate entries")
            seen.add(member)
            if member.endswith("/"):
                continue
            if member.startswith(f"{package}/"):
                pass
            elif member.endswith(".dist-info/METADATA"):
                metadata_files.append(member)
            else:
                continue
            # Prevent wheel paths from traversing the private venv.
            if ".." in Path(member).parts or Path(member).is_absolute():
                raise RecoveryProofError("candidate wheel member path is invalid")
            installed = site.joinpath(*member.split("/"))
            # A final ordinary file is not enough if an ancestor is junctioned.
            for parent in installed.parents:
                if parent == site:
                    break
                _ordinary(parent, directory=True)
            _actual_file_matches_bytes(installed, wheel.read(info))
            total += 1
            extension |= package == "serverfs_windows_native" and member.endswith(".pyd")
        if total < 2 or len(metadata_files) != 1:
            raise RecoveryProofError("installed wheel proof is incomplete")
        if package == "serverfs_windows_native" and not extension:
            raise RecoveryProofError("installed native extension is absent")
        if package == "serverfs_mcp" and total < 20:
            raise RecoveryProofError("installed Product source proof is incomplete")
        # An additional executable/source file under either package could
        # shadow the accepted wheel modules during import. Reject it.
        package_dir = site / package
        for candidate in package_dir.rglob("*"):
            if candidate.is_dir():
                _ordinary(candidate, directory=True)
                continue
            if candidate.suffix.casefold() in {".py", ".pyd", ".dll"}:
                member = candidate.relative_to(site).as_posix()
                if member not in seen:
                    raise RecoveryProofError("unexpected executable package member exists")
    return total


def verify_recovered_owner(root: Path, product: Path, native: Path) -> tuple[int, int]:
    if sys.platform != "win32" or os.name != "nt":
        raise RecoveryProofError("Windows recovery only")
    root = root.absolute()
    site = root / "Lib" / "site-packages"
    for target in (root, root / "Scripts", root / "Lib", site):
        _ordinary(target, directory=True)
    for target in (
        root / "pyvenv.cfg",
        root / "Scripts" / "python.exe",
        root / "Scripts" / "pythonw.exe",
    ):
        _ordinary(target, directory=False)

    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_cloudflare_security import _no_reparse_components
    from serverfs_mcp.windows_core_control import _check_private

    _no_reparse_components(root)
    _check_private(root, directory=True, sid=current_user_sid())
    # The root is already protected. Future W2g applies individual DACL
    # protections to each critical child AFTER validating wheel bytes.
    _unshared(root / "Scripts" / "python.exe")
    _unshared(root / "Scripts" / "pythonw.exe")
    cfg = (root / "pyvenv.cfg").read_text(encoding="utf-8")
    if "include-system-site-packages = false" not in cfg.lower():
        raise RecoveryProofError("system site-packages are not isolated")
    if (site / "serverfs_agent_bridge").exists():
        raise RecoveryProofError("foreign Agent Bridge package is present")
    return (
        verify_one_wheel(site, product, "serverfs_mcp"),
        verify_one_wheel(site, native, "serverfs_windows_native"),
    )


def main() -> int:
    if len(sys.argv) != 4:
        print("W2G_RECOVERY=FAILED (invalid arguments)", file=sys.stderr)
        return 2
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "src"))
    try:
        product_count, native_count = verify_recovered_owner(
            Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
        )
    except Exception as exc:  # noqa: BLE001 - redact paths, config and file names
        print(f"W2G_RECOVERY=FAILED ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(f"W2G_RECOVERY=PASS (Product files={product_count}; Native files={native_count})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
