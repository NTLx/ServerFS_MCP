"""Source-only contracts for Windows private installed Scheduler code parity gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "deployment/windows/v0250-windows-scheduler-installed-parity-readonly.py"
)
spec = importlib.util.spec_from_file_location("v0250_windows_private_parity", SCRIPT)
assert spec is not None and spec.loader is not None
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def test_exact_private_nonproduction_scope_and_module() -> None:
    assert audit.INSTANCE_TARGETS == (
        ("I7_CORE", "i7-public", "core"),
        ("I7_CLOUDFLARE", "i7-public", "cloudflare"),
        ("I5_BRIDGE_OWNER", "i5-shared", "bridge"),
    )
    assert audit.RELATIVE_MODULE.as_posix().endswith("serverfs_mcp/windows_v25/tasks.py")


def test_exact_byte_parity_is_not_version_number_parity(tmp_path: Path) -> None:
    target = tmp_path / "installed-tasks.py"
    assert audit.classify_source(b"fixed-current-source", target) == "ABSENT"
    target.write_bytes(b"fixed-current-source")
    assert audit.classify_source(b"fixed-current-source", target) == "MATCH"
    target.write_bytes(b"older-unsafe-source")
    assert audit.classify_source(b"fixed-current-source", target) == "STALE"


def test_unsafe_link_is_refused_without_following_it() -> None:
    class FakeLink:
        def is_symlink(self) -> bool:
            return True

        def is_file(self) -> bool:
            raise AssertionError("must never follow a link")

    assert audit.classify_source(b"source", FakeLink()) == "UNSAFE"


def test_no_service_mutation_or_external_side_effect_contract() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "storage.check_private(env, directory=True, sid=sid)",
        "storage.check_private(target, directory=False, sid=sid)",
        "current_user_sid() == storage._sid()",
        "sys.flags.isolated",
        "sys.dont_write_bytecode",
        "INSTALLED_PARITY=",
    ):
        assert marker in source
    for forbidden in (
        "Scheduler(",
        "subprocess.run(",
        "os.system(",
        "uv pip install",
        "git push",
        "tunnel.token",
        "http.env",
        "serverfs-agent",
        "requests.post(",
    ):
        assert forbidden not in source


@pytest.mark.parametrize("invalid", [[], [b""], ["wrong"]])
def test_no_false_match_for_different_expected_data(tmp_path: Path, invalid: list) -> None:
    target = tmp_path / "module.py"
    target.write_bytes(b"real-installed")
    for source in invalid:
        assert audit.classify_source(source, target) == "STALE"
