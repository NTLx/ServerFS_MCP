"""Workdir registry validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.workdirs import (
    ACCESS_READ_ONLY,
    ACCESS_READ_WRITE,
    DISABLED_SENTINEL,
    SLOT_COUNT,
    WorkdirError,
    build_registry,
    parse_read_only,
)


def make_root(tmp_path: Path, enabled: dict[int, bool] | None = None) -> Path:
    """Create a 16-slot root; enabled slots have real dirs, others sentinels."""
    root = tmp_path / "workdirs"
    root.mkdir(parents=True)
    for slot in range(1, SLOT_COUNT + 1):
        d = root / f"{slot:02d}"
        d.mkdir()
        if not (enabled or {}).get(slot, False):
            (d / DISABLED_SENTINEL).touch()
    return root


def envs(alias: str = "", **overrides: str) -> tuple[dict[int, str], dict[int, str]]:
    aliases = {s: "" for s in range(1, SLOT_COUNT + 1)}
    if alias:
        aliases[1] = alias
    return aliases, {s: "" for s in range(1, SLOT_COUNT + 1)}


class TestValidConfig:
    def test_valid_alias(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs("projects"), workdir_root=root)
        assert reg.get("projects") is not None

    @pytest.mark.parametrize(
        "alias",
        ["logs", "app-logs", "bioinfo", "paper_db", "ProjectA", "a" * 32],
    )
    def test_alias_shapes(self, tmp_path: Path, alias: str) -> None:
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs(alias), workdir_root=root)
        assert reg.get(alias) is not None

    def test_description_round_trip(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        aliases, _ = envs("projects")
        reg = build_registry(
            aliases, {1: "项目代码", **{s: "" for s in range(2, 17)}}, workdir_root=root
        )
        assert reg.list_result().workdirs[0].description == "项目代码"

    def test_description_empty_becomes_none(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs("projects"), workdir_root=root)
        assert reg.list_result().workdirs[0].description is None


class TestInvalidAlias:
    @pytest.mark.parametrize(
        "alias",
        ["/foo", "../foo", "foo/bar", "foo bar", "123project", "", "a" * 33, "中文"],
    )
    def test_invalid_alias_fails(self, tmp_path: Path, alias: str) -> None:
        root = make_root(tmp_path, {1: True})
        if not alias:
            # empty alias + real dir = case C, different error but still fatal
            with pytest.raises(WorkdirError):
                build_registry(*envs(""), workdir_root=root)
            return
        with pytest.raises(WorkdirError, match="invalid alias"):
            build_registry(*envs(alias), workdir_root=root)

    def test_duplicate_alias_fails(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True, 2: True})
        aliases, _ = envs("projects")
        aliases[2] = "projects"
        with pytest.raises(WorkdirError, match="duplicate alias"):
            build_registry(aliases, _, workdir_root=root)


class TestSlotStates:
    def test_disabled_normal(self, tmp_path: Path) -> None:
        """Case A: empty alias + sentinel -> OK, slot skipped."""
        root = make_root(tmp_path)  # all disabled
        reg = build_registry(*envs(), workdir_root=root)
        assert len(reg) == 0

    def test_alias_set_with_sentinel_fails(self, tmp_path: Path) -> None:
        """Case B: alias set + sentinel -> startup error."""
        root = make_root(tmp_path)  # slot 1 has sentinel
        with pytest.raises(WorkdirError, match="disabled"):
            build_registry(*envs("projects"), workdir_root=root)

    def test_alias_empty_with_real_dir_fails(self, tmp_path: Path) -> None:
        """Case C: alias empty + real mounted dir -> startup error."""
        root = make_root(tmp_path, {1: True})
        with pytest.raises(WorkdirError, match="alias is empty"):
            build_registry(*envs(""), workdir_root=root)

    def test_enabled_workdir(self, tmp_path: Path) -> None:
        """Case D: alias + real dir -> enabled."""
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs("projects"), workdir_root=root)
        assert len(reg) == 1
        assert reg.get("projects") is not None

    def test_all_16_slots_configured(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {s: True for s in range(1, 17)})
        aliases = {s: f"dir{s:02d}" for s in range(1, 17)}
        reg = build_registry(aliases, {s: "" for s in range(1, 17)}, workdir_root=root)
        assert len(reg) == 16

    def test_error_messages_contain_real_slot_names(self, tmp_path: Path) -> None:
        """§34: messages must render WORKDIR_03_ALIAS, not a literal
        '{slot:02d}' placeholder."""
        # case C: mounted dir without alias on slot 03
        root = make_root(tmp_path / "a", {3: True})
        with pytest.raises(WorkdirError) as exc_info:
            build_registry(*envs(""), workdir_root=root)
        assert "WORKDIR_03_ALIAS" in str(exc_info.value)
        assert "{slot" not in str(exc_info.value)

        # case B: alias set but slot disabled on slot 05 (slot 01 must be a
        # real dir so the earlier slot does not fail first)
        root = make_root(tmp_path / "b", {1: True})  # slot 5 carries the sentinel
        aliases, descriptions = envs("projects")
        aliases[5] = "projects5"
        with pytest.raises(WorkdirError) as exc_info:
            build_registry(aliases, descriptions, workdir_root=root)
        assert "WORKDIR_05_PATH" in str(exc_info.value)
        assert "{slot" not in str(exc_info.value)

    def test_reserved_sentinel_in_real_workdir_fails(self, tmp_path: Path) -> None:
        """Real mounted dir containing .serverfs-disabled -> conflict."""
        root = make_root(tmp_path, {1: True})
        (root / "01" / DISABLED_SENTINEL).touch()
        (root / "01" / "other.txt").touch()
        with pytest.raises(WorkdirError, match="reserved"):
            build_registry(*envs("projects"), workdir_root=root)


def read_only_env(by_slot: dict[int, str] | None = None) -> dict[int, str]:
    """A full slot->raw-value mapping; unlisted slots keep the default."""
    values = {s: "" for s in range(1, SLOT_COUNT + 1)}
    values.update(by_slot or {})
    return values


class TestReadOnlyParsing:
    """§11: WORKDIR_XX_READ_ONLY is a security switch — unknown values abort
    startup instead of guessing."""

    @pytest.mark.parametrize("raw", ["true", "TRUE", " True ", "1", "yes", "on"])
    def test_true_values(self, raw: str) -> None:
        assert parse_read_only(1, raw) is True

    @pytest.mark.parametrize("raw", ["false", "FALSE", " False ", "0", "no", "off"])
    def test_false_values(self, raw: str) -> None:
        assert parse_read_only(1, raw) is False

    def test_empty_means_read_only(self) -> None:
        """§9: a v0.1 configuration has no such variable at all."""
        assert parse_read_only(1, "") is True

    @pytest.mark.parametrize("raw", ["rw", "enable", "foobar", "maybe", "2", "tru"])
    def test_unknown_values_raise(self, raw: str) -> None:
        with pytest.raises(WorkdirError, match="READ_ONLY"):
            parse_read_only(3, raw)

    def test_unknown_value_names_the_slot(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        with pytest.raises(WorkdirError) as exc_info:
            build_registry(
                *envs("projects"),
                read_only_env({1: "rw"}),
                workdir_root=root,
            )
        assert "WORKDIR_01_READ_ONLY" in str(exc_info.value)

    def test_unknown_value_on_a_disabled_slot_still_fails(self, tmp_path: Path) -> None:
        """Typos must not survive because the slot happens to be disabled."""
        root = make_root(tmp_path, {1: True})
        with pytest.raises(WorkdirError):
            build_registry(
                *envs("projects"),
                read_only_env({7: "yesplease"}),
                workdir_root=root,
            )


class TestReadOnlyDefaults:
    def test_missing_variable_is_read_only(self, tmp_path: Path) -> None:
        """An upgrade from v0.1 must not gain write access."""
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs("projects"), workdir_root=root)
        assert reg.get("projects").read_only is True
        assert reg.get("projects").access == ACCESS_READ_ONLY

    def test_omitted_slot_in_the_mapping_is_read_only(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True, 2: True})
        aliases, _ = envs("projects")
        aliases[2] = "logs"
        reg = build_registry(
            aliases, {s: "" for s in range(1, 17)}, {1: "false"}, workdir_root=root
        )
        assert reg.get("projects").read_only is False
        assert reg.get("logs").read_only is True

    def test_explicit_false_is_read_write(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs("projects"), read_only_env({1: "false"}), workdir_root=root)
        workdir = reg.get("projects")
        assert workdir.read_only is False
        assert workdir.access == ACCESS_READ_WRITE

    def test_access_values_are_exactly_two(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True, 2: True})
        aliases, _ = envs("projects")
        aliases[2] = "logs"
        reg = build_registry(
            aliases,
            {s: "" for s in range(1, 17)},
            read_only_env({2: "no"}),
            workdir_root=root,
        )
        assert {w.access for w in reg.list_result().workdirs} == {
            ACCESS_READ_ONLY,
            ACCESS_READ_WRITE,
        }

    def test_registry_reports_access_per_workdir(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True, 2: True})
        aliases, _ = envs("projects")
        aliases[2] = "logs"
        reg = build_registry(
            aliases,
            {s: "" for s in range(1, 17)},
            read_only_env({2: "false"}),
            workdir_root=root,
        )
        reported = {w.alias: w.access for w in reg.list_result().workdirs}
        assert reported == {"projects": ACCESS_READ_ONLY, "logs": ACCESS_READ_WRITE}


class TestDisabledSlotConsistency:
    """§10: a disabled slot may not be declared writable."""

    def test_disabled_slot_with_read_only_false_fails(self, tmp_path: Path) -> None:
        root = make_root(tmp_path)  # every slot disabled
        with pytest.raises(WorkdirError, match="disabled slot"):
            build_registry(*envs(), read_only_env({3: "false"}), workdir_root=root)

    def test_disabled_slot_with_read_only_true_is_fine(self, tmp_path: Path) -> None:
        root = make_root(tmp_path)
        reg = build_registry(*envs(), read_only_env({3: "true"}), workdir_root=root)
        assert len(reg) == 0

    def test_enabled_slot_with_read_only_false_is_fine(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs("projects"), read_only_env({1: "false"}), workdir_root=root)
        assert reg.get("projects").read_only is False
