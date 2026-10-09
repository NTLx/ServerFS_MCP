"""Release-workflow helpers for the Windows wheel channel (v0.10 Phase E2).

Pure-stdlib, unit-tested in the repository test suite so the release gate
logic itself has executed evidence instead of only existing inside a tag-only
workflow:

* ``version-gate`` reads the authoritative ``Version:`` from each wheel's
  ``*.dist-info/METADATA`` (never from filename segment positions, which are
  ambiguous for underscore-normalized names and ABI tags) and requires every
  wheel to carry the normalized tag version.
* ``notes-update`` regenerates the managed wheel-assets block inside a GitHub
  Release body between PAIRED markers: text the maintainer wrote before or
  after the block is preserved verbatim, the block is replaced in place on
  re-runs, an absent pair appends exactly one block, and any orphan/duplicate
  marker fails closed with exit code 3 and no output written.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from pathlib import Path

MARKER_START = "<!-- serverfs-windows-wheel-assets:start -->"
MARKER_END = "<!-- serverfs-windows-wheel-assets:end -->"

EXIT_VERSION_MISMATCH = 2
EXIT_MARKER_CONFLICT = 3


def normalize_release_version(raw: str) -> str:
    """Canonical lowercase form used to compare a tag with METADATA versions."""
    text = raw.strip().lower()
    if text.startswith("v"):
        text = text[1:]
    text = text.split("+", 1)[0]
    text = text.replace("-", "")
    # PEP 440 wants a digit after 'dev': 1.2.3dev -> 1.2.3.dev0 spelling is
    # normalized below together with stripping separators around pre/dev tags.
    text = re.sub(r"\.?(dev|a|b|c|rc)\.?", lambda m: m.group(1), text)
    text = re.sub(r"(dev|rc|a|b)(\d*)$", lambda m: m.group(1) + (m.group(2) or "0"), text)
    return text


def wheel_version(wheel: Path) -> tuple[str, str]:
    """Return (dist-info name, Version) read from authoritative METADATA."""
    with zipfile.ZipFile(wheel) as bundle:
        candidates = [
            info
            for info in bundle.namelist()
            if re.fullmatch(r"[^/]+-[^/]+\.dist-info/METADATA", info)
        ]
        if len(candidates) != 1:
            raise ValueError(f"{wheel.name}: expected exactly one dist-info METADATA")
        message = Parser().parsestr(bundle.read(candidates[0]).decode("utf-8"), headersonly=True)
    version = message["Version"]
    if not version:
        raise ValueError(f"{wheel.name}: METADATA has no Version header")
    return message.get("Name", candidates[0].split("-", 1)[0]), str(version).strip()


def cmd_version_gate(tag: str, wheels: list[Path]) -> int:
    want = normalize_release_version(tag)
    seen: list[str] = []
    for wheel in wheels:
        try:
            name, version = wheel_version(wheel)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            sys.stderr.write(f"version-gate: cannot read {wheel}: {exc}\n")
            return EXIT_VERSION_MISMATCH
        if normalize_release_version(version) != want:
            sys.stderr.write(
                f"version-gate: {wheel.name} carries METADATA version {version!r}, "
                f"tag {tag!r} normalizes to {want!r}\n"
            )
            return EXIT_VERSION_MISMATCH
        seen.append(f"{name} {version}")
    sys.stderr.write(
        "version-gate ok (tag-normalized version " + want + "): " + "; ".join(seen) + "\n"
    )
    return 0


@dataclass(frozen=True)
class NotesResult:
    body: str
    action: str  # "replaced" | "appended"


def update_release_notes(existing: str, block: str) -> NotesResult:
    """Apply the paired-marker contract; raises ValueError when malformed."""
    starts = existing.count(MARKER_START)
    ends = existing.count(MARKER_END)
    if starts == 0 and ends == 0:
        tail = existing.rstrip("\n")
        body = (tail + "\n\n" + block if tail else block) + "\n"
        return NotesResult(body=body, action="appended")
    if starts == 1 and ends == 1:
        head, _, remainder = existing.partition(MARKER_START)
        _, _, tail = remainder.partition(MARKER_END)
        body = head.rstrip("\n") + "\n\n" + block + "\n"
        if tail.strip():
            body += "\n" + tail.strip("\n") + "\n"
        return NotesResult(body=body, action="replaced")
    raise ValueError(
        f"orphaned release-notes markers ({starts} start, {ends} end); refusing to rewrite"
    )


def render_block(
    *,
    product_name: str,
    product_url: str,
    product_sha: str,
    bridge_name: str,
    bridge_url: str,
    bridge_sha: str,
    native_name: str,
    native_url: str,
    native_sha: str,
) -> str:
    return (
        f"{MARKER_START}\n"
        "\n"
        "## Windows installation assets (Python >= 3.12; no Rust/MSVC/Docker required)\n"
        "\n"
        "The three release wheels install into **two isolated environments**, matching the\n"
        "frozen ServerFS/Agent-Bridge process boundary (the supervisor selects the Bridge\n"
        "interpreter through `SERVERFS_BRIDGE_PYTHON`; the packages never share one dependency\n"
        "graph):\n"
        "\n"
        "| file | installs into | sha256 |\n"
        "| --- | --- | --- |\n"
        f"| [{product_name}]({product_url}) | ServerFS env | `{product_sha}` |\n"
        f"| [{native_name}]({native_url}) | ServerFS env | `{native_sha}` |\n"
        f"| [{bridge_name}]({bridge_url}) | Agent Bridge env | `{bridge_sha}` |\n"
        "\n"
        "ServerFS environment (product + native kernel):\n"
        "```\n"
        "uv venv --python 3.12 .venv-serverfs\n"
        f"uv pip install --python .venv-serverfs/Scripts/python.exe {product_url} {native_url}\n"
        "```\n"
        "\n"
        "Agent Bridge environment (Bridge + provider SDKs; it must NOT contain the product):\n"
        "```\n"
        "uv venv --python 3.12 .venv-bridge\n"
        f"uv pip install --python .venv-bridge/Scripts/python.exe {bridge_url}\n"
        "```\n"
        "\n"
        "Point the ServerFS supervisor at the Bridge interpreter (set this in the environment\n"
        "that starts `serverfs supervisor`; it is not a provider setting and does not belong in\n"
        "`[agent]` config):\n"
        "```\n"
        '$env:SERVERFS_BRIDGE_PYTHON = "<path>\\.venv-bridge\\Scripts\\python.exe"\n'
        "```\n"
        "\n"
        "Verified native-wheel storage alternative:\n"
        "```\n"
        f'serverfs bootstrap native-wheel --url "{native_url}" --sha256 {native_sha}\n'
        "```\n"
        f"{MARKER_END}"
    )


def cmd_notes_update(args: argparse.Namespace) -> int:
    existing = Path(args.body_file).read_text(encoding="utf-8-sig")
    block = render_block(
        product_name=args.product_name,
        product_url=args.product_url,
        product_sha=args.product_sha,
        bridge_name=args.bridge_name,
        bridge_url=args.bridge_url,
        bridge_sha=args.bridge_sha,
        native_name=args.native_name,
        native_url=args.native_url,
        native_sha=args.native_sha,
    )
    try:
        result = update_release_notes(existing, block)
    except ValueError as exc:
        sys.stderr.write(f"notes-update: {exc}\n")
        return EXIT_MARKER_CONFLICT
    Path(args.out_file).write_text(result.body, encoding="utf-8")
    sys.stderr.write(f"notes-update: {result.action}\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wheel_release", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("version-gate")
    gate.add_argument("--tag", required=True)
    gate.add_argument("--wheel", required=True, action="append", type=Path)
    notes = sub.add_parser("notes-update")
    notes.add_argument("--body-file", required=True)
    notes.add_argument("--out-file", required=True)
    notes.add_argument("--product-name", required=True)
    notes.add_argument("--product-url", required=True)
    notes.add_argument("--product-sha", required=True)
    notes.add_argument("--bridge-name", required=True)
    notes.add_argument("--bridge-url", required=True)
    notes.add_argument("--bridge-sha", required=True)
    notes.add_argument("--native-name", required=True)
    notes.add_argument("--native-url", required=True)
    notes.add_argument("--native-sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version-gate":
        return cmd_version_gate(args.tag, args.wheel)
    return cmd_notes_update(args)


if __name__ == "__main__":
    raise SystemExit(main())
