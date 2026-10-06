"""Central Windows private-state seam: a dangling reparse point is never "absent".

``Path.exists()`` follows a link, so a symlink whose target is absent reports False while ``lstat``
still carries the reparse tag. Every Windows private-state path that gated its reparse decision
behind ``exists()`` therefore classified the cheapest object an attacker can plant -- and the one
that leaves no visible trace -- as "nothing there", and then created, opened or replaced through it.

The renderer already proved the correct ordering (reparse first, existence second). These tests pin
that same ordering across the shared seam, so the guarantee is central rather than per-caller.

**No Developer Mode required.** Symlink creation needs elevation on most Windows hosts, so a real
fixture cannot carry the coverage here. Each case is therefore driven deterministically by
simulating ``exists() == False`` together with ``is_reparse_point() == True`` -- the exact state a
dangling link produces -- and then asserting not only that the path is *classified* as a reparse
point but that the refusal happens **before** any create, open or replace is attempted. Asserting
the classification alone would leave the second, security-relevant half untested.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from serverfs_agent_bridge import private_state, windows_security
from serverfs_agent_bridge.errors import BridgeError
from serverfs_agent_bridge.private_state import (
    DirectoryMessages,
    ensure_private_directory,
    ensure_private_file,
    opened_file_is_private,
    protect_existing_file,
    require_regular_file,
    verify_private_file,
)

pytestmark = pytest.mark.skipif(not private_state.WINDOWS, reason="Windows private-state seam")

MESSAGES = DirectoryMessages(not_a_directory="not a directory", not_owned="not owned")


class _Recorder:
    """The three mutation primitives a refusal must never reach.

    A test that only asserts the classification would pass even if the caller classified correctly
    and then went on to create the object anyway, which is the failure that matters.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            windows_security,
            "create_private_directory",
            lambda path, sddl: self.calls.append(f"create_directory:{Path(path).name}"),
        )
        monkeypatch.setattr(
            windows_security,
            "create_private_file",
            lambda path, sddl: self.calls.append(f"create_file:{Path(path).name}"),
        )
        monkeypatch.setattr(
            windows_security,
            "read_object_security",
            lambda path: self.calls.append(f"read_security:{Path(path).name}"),
        )
        monkeypatch.setattr(os, "replace", lambda a, b: self.calls.append("replace"))

    @property
    def mutated(self) -> list[str]:
        return [c for c in self.calls if c != "read_object_security"]


@pytest.fixture()
def dangling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model exactly what a Windows dangling symlink looks like to this code.

    ``exists()`` False, ``is_symlink()`` True, ``lstat`` carrying the reparse tag -- and
    ``windows_security.is_reparse_point`` reporting True, which is what the real lstat path does.
    """
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(Path, "is_symlink", lambda self: True)
    monkeypatch.setattr(windows_security, "is_reparse_point", lambda path: True)


class TestDirectoryRoot:
    def test_a_dangling_reparse_root_is_refused(
        self, tmp_path: Path, dangling, monkeypatch
    ) -> None:
        recorder = _Recorder()
        recorder.install(monkeypatch)
        with pytest.raises(BridgeError):
            ensure_private_directory(tmp_path / "agent-bridge", mode=0o700, messages=MESSAGES)
        assert recorder.mutated == [], f"a refusal still mutated state: {recorder.calls}"

    def test_the_reparse_message_names_the_actual_reason(self, tmp_path: Path, dangling) -> None:
        with pytest.raises(BridgeError, match="reparse point"):
            ensure_private_directory(tmp_path / "agent-bridge", mode=0o700, messages=MESSAGES)

    def test_a_genuinely_absent_root_is_still_created(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hardening must not turn "absent" into a refusal; that breaks every cold start."""
        monkeypatch.setattr(windows_security, "is_reparse_point", lambda path: False)
        created: list[Path] = []
        monkeypatch.setattr(
            windows_security,
            "create_private_directory",
            lambda path, sddl: created.append(Path(path)),
        )
        # The verification read is stubbed to raise, proving the create happened *first*.
        monkeypatch.setattr(
            windows_security,
            "read_object_security",
            lambda path: (_ for _ in ()).throw(AssertionError("create must precede verification")),
        )
        with pytest.raises(AssertionError):
            ensure_private_directory(tmp_path / "agent-bridge", mode=0o700, messages=MESSAGES)
        assert created, "an absent root was not created"


class TestAncestorWalk:
    def test_a_dangling_reparse_parent_is_not_treated_as_missing(
        self, tmp_path: Path, dangling, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dangling parent used to be walked *past* as an absent component.

        That is worse than creating through it: the walk stops at the first existing ancestor, so
        classifying the hostile parent as missing anchors the Bridge's tree one level higher, inside
        a directory somebody else controls (§28).
        """
        recorder = _Recorder()
        recorder.install(monkeypatch)
        deep = tmp_path / "a" / "b" / "c" / "state"
        with pytest.raises(BridgeError):
            ensure_private_directory(deep, mode=0o700, messages=MESSAGES)
        assert recorder.mutated == [], f"the ancestor walk mutated state: {recorder.calls}"


class TestPrivateFile:
    def test_ensure_private_file_refuses_before_creating(
        self, tmp_path: Path, dangling, monkeypatch
    ) -> None:
        recorder = _Recorder()
        recorder.install(monkeypatch)
        with pytest.raises(BridgeError):
            ensure_private_file(tmp_path / "task.db", mode=0o600, not_regular="not a regular file")
        assert recorder.mutated == [], f"a refusal still created the file: {recorder.calls}"

    def test_require_regular_file_refuses(self, tmp_path: Path, dangling) -> None:
        with pytest.raises(BridgeError, match="reparse point"):
            require_regular_file(tmp_path / "task.db", not_regular="not a regular file")

    def test_verify_private_file_refuses_before_reading_the_descriptor(
        self, tmp_path: Path, dangling, monkeypatch: pytest.MonkeyPatch
    ):
        """Verification must refuse on the reparse tag, not proceed to inspect an ACL."""
        monkeypatch.setattr(
            windows_security,
            "read_object_security",
            lambda path: (_ for _ in ()).throw(AssertionError("ACL read must not be reached")),
        )
        with pytest.raises(BridgeError, match="reparse point"):
            verify_private_file(tmp_path / "task.db", not_regular="unsafe", not_private="unsafe")


class TestSqliteSidecar:
    def test_protect_existing_file_refuses_a_dangling_sidecar(
        self, tmp_path: Path, dangling, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A SQLite sidecar is the classic dangling plant: nothing references it afterwards."""
        recorder = _Recorder()
        recorder.install(monkeypatch)
        with pytest.raises(BridgeError):
            protect_existing_file(tmp_path / "task.db-wal", mode=0o600, not_private="not private")
        assert recorder.mutated == [], f"a refusal still proceeded: {recorder.calls}"

    def test_opened_file_is_private_is_false_for_a_dangling_path(self, tmp_path: Path, dangling):
        """A descriptor beside a dangling path must not be reported as a private regular file."""
        opened = os.stat_result((0o100600, 0, 0, 1, 0, 0, 10, 0, 0, 0))
        assert opened_file_is_private(tmp_path / "task.db", opened) is False


class TestOrderingIsTheContract:
    def test_reparse_is_asked_before_existence_everywhere(self) -> None:
        """The guarantee is the *order*, so it is asserted on the source of each Windows path.

        Checking call order through monkeypatching would only prove one call sequence; reading the
        guard shape proves the property for every input, including ones no fixture enumerates.
        """
        import inspect

        for name in ("_windows_refuse_reparse", "require_regular_file", "protect_existing_file"):
            source = inspect.getsource(getattr(private_state, name))
            reparse = source.find("is_reparse_point")
            assert reparse != -1, f"{name} no longer consults the reparse tag"
            exists = source.find(".exists()")
            assert exists == -1 or reparse < exists, (
                f"{name} consults exists() before the reparse tag, which a dangling link defeats"
            )

    def test_real_symlink_fixture_where_the_host_permits(self, tmp_path: Path) -> None:
        """The honest fixture, which runs only where the host allows creating one."""
        target = tmp_path / "absent-target"
        link = tmp_path / "planted"
        try:
            os.symlink(str(target), str(link))
        except OSError:
            pytest.skip("this host does not permit symlink creation (Developer Mode or elevation)")
        assert link.exists() is False, "premise: a dangling symlink reports exists() == False"
        assert windows_security.is_reparse_point(link) is True, "premise: lstat sees the tag"
        with pytest.raises(BridgeError, match="reparse point"):
            private_state._windows_refuse_reparse(link, "state object")
