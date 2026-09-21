"""Workdir registry validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.config import Settings
from serverfs_mcp.workdirs import (
    ACCESS_READ_ONLY,
    ACCESS_READ_WRITE,
    AGENT_MODE_DISABLED,
    AGENT_MODE_WORKSPACE_WRITE,
    DISABLED_SENTINEL,
    SLOT_COUNT,
    EffectiveWorkdirPolicy,
    WorkdirError,
    build_registry,
    build_registry_from_env,
    parse_agent_mode,
    parse_agent_runtimes,
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


class TestAgentConfig:
    def test_defaults_are_disabled(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        reg = build_registry(*envs("projects"), workdir_root=root)
        wd = reg.get("projects")
        assert wd.agent_mode == AGENT_MODE_DISABLED
        assert wd.agent_runtimes == frozenset()

    @pytest.mark.parametrize("raw", ["", "disabled", "review", "workspace-write"])
    def test_agent_mode_parser(self, raw: str) -> None:
        expected = raw or AGENT_MODE_DISABLED
        assert parse_agent_mode(1, raw) == expected

    def test_invalid_agent_mode_fails(self) -> None:
        with pytest.raises(WorkdirError, match="AGENT_MODE"):
            parse_agent_mode(2, "write-all")

    def test_agent_runtime_parser(self) -> None:
        assert parse_agent_runtimes(1, " codex,claude ") == frozenset({"codex", "claude"})

    def test_unknown_agent_runtime_fails(self) -> None:
        with pytest.raises(WorkdirError, match="unknown"):
            parse_agent_runtimes(1, "codex,shell")

    def test_duplicate_agent_runtime_fails(self) -> None:
        with pytest.raises(WorkdirError, match="duplicate"):
            parse_agent_runtimes(1, "codex,codex")

    def test_workspace_write_requires_read_write_workdir(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        aliases, descriptions = envs("projects")
        with pytest.raises(WorkdirError, match="READ_ONLY=false"):
            build_registry(
                aliases,
                descriptions,
                read_only_env({1: "true"}),
                {1: "workspace-write"},
                {1: "codex"},
                workdir_root=root,
            )

    def test_native_runtime_requires_workspace_write(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        aliases, descriptions = envs("projects")
        with pytest.raises(WorkdirError, match="native mode"):
            build_registry(
                aliases,
                descriptions,
                read_only_env({1: "false"}),
                {1: "review"},
                {1: "claude"},
                workdir_root=root,
            )

    def test_enabled_agent_policy_round_trip(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        aliases, descriptions = envs("projects")
        reg = build_registry(
            aliases,
            descriptions,
            read_only_env({1: "false"}),
            {1: "workspace-write"},
            {1: "codex,claude"},
            workdir_root=root,
        )
        wd = reg.get("projects")
        assert wd.agent_mode == AGENT_MODE_WORKSPACE_WRITE
        assert wd.agent_runtimes == frozenset({"codex", "claude"})

    def test_enabled_agent_mode_requires_runtimes(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        aliases, descriptions = envs("projects")
        with pytest.raises(WorkdirError, match="enabled Agent mode requires"):
            build_registry(
                aliases,
                descriptions,
                read_only_env({1: "false"}),
                {1: "workspace-write"},
                {},
                workdir_root=root,
            )

    def test_agent_runtimes_require_enabled_mode(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        aliases, descriptions = envs("projects")
        with pytest.raises(WorkdirError, match="requires an enabled"):
            build_registry(
                aliases,
                descriptions,
                read_only_env(),
                {},
                {1: "codex"},
                workdir_root=root,
            )

    def test_disabled_slot_cannot_enable_agent(self, tmp_path: Path) -> None:
        root = make_root(tmp_path)
        with pytest.raises(WorkdirError, match="disabled slot cannot enable Agent"):
            build_registry(
                *envs(),
                read_only_env(),
                {3: "workspace-write"},
                {3: "codex"},
                workdir_root=root,
            )

    def test_empty_agent_mode_inherits_global_pair(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        env = {"WORKDIR_01_ALIAS": "projects", "WORKDIR_01_READ_ONLY": "false"}
        settings = Settings(
            agent_mode=AGENT_MODE_WORKSPACE_WRITE, agent_runtimes=frozenset({"codex"})
        )
        wd = build_registry_from_env(env, settings, workdir_root=root).get("projects")
        assert wd.policy.agent_mode == AGENT_MODE_WORKSPACE_WRITE
        assert wd.policy.agent_runtimes == frozenset({"codex"})

    def test_explicit_disabled_agent_clears_inherited_runtimes(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        env = {
            "WORKDIR_01_ALIAS": "projects",
            "WORKDIR_01_AGENT_MODE": "disabled",
        }
        settings = Settings(
            agent_mode=AGENT_MODE_WORKSPACE_WRITE, agent_runtimes=frozenset({"codex"})
        )
        wd = build_registry_from_env(env, settings, workdir_root=root).get("projects")
        assert wd.policy.agent_mode == AGENT_MODE_DISABLED
        assert wd.policy.agent_runtimes == frozenset()

    def test_global_agent_and_binary_defaults_do_not_apply_to_disabled_slots(
        self, tmp_path: Path
    ) -> None:
        root = make_root(tmp_path)
        settings = Settings(
            agent_mode=AGENT_MODE_WORKSPACE_WRITE,
            agent_runtimes=frozenset({"codex"}),
            binary_transfer_enabled=True,
        )
        assert len(build_registry_from_env({}, settings, workdir_root=root)) == 0

    def test_disabled_slot_explicit_binary_enable_fails(self, tmp_path: Path) -> None:
        root = make_root(tmp_path)
        with pytest.raises(WorkdirError, match="binary transfer"):
            build_registry_from_env(
                {"WORKDIR_02_BINARY_TRANSFER_ENABLED": "true"},
                Settings(),
                workdir_root=root,
            )


class TestPolicyInheritance:
    def test_global_and_workdir_deny_globs_are_additive(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        env = {
            "WORKDIR_01_ALIAS": "projects",
            "WORKDIR_01_EXTRA_DENY_GLOBS": "local_*, , *.tmp",
        }
        settings = Settings(extra_deny_globs=("global_*", "*.tmp"))
        wd = build_registry_from_env(env, settings, workdir_root=root).get("projects")
        assert wd.policy.extra_deny_globs == ("global_*", "*.tmp", "local_*")

    def test_binary_defaults_and_override(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True, 2: True})
        env = {
            "WORKDIR_01_ALIAS": "inherited",
            "WORKDIR_02_ALIAS": "override",
            "WORKDIR_02_BINARY_TRANSFER_ENABLED": "true",
            "WORKDIR_02_MAX_BINARY_TRANSFER_BYTES": "1234",
        }
        settings = Settings(binary_transfer_enabled=False, max_binary_transfer_bytes=99)
        registry = build_registry_from_env(env, settings, workdir_root=root)
        assert registry.get("inherited").policy.binary_transfer_enabled is False
        assert registry.get("inherited").policy.max_binary_transfer_bytes == 99
        assert registry.get("override").policy.binary_transfer_enabled is True
        assert registry.get("override").policy.max_binary_transfer_bytes == 1234

    def test_invalid_workdir_policy_overrides_fail_closed(self, tmp_path: Path) -> None:
        root = make_root(tmp_path, {1: True})
        base = {"WORKDIR_01_ALIAS": "projects"}
        with pytest.raises(WorkdirError):
            build_registry_from_env(
                {**base, "WORKDIR_01_ALLOW_HIDDEN": "maybe"}, Settings(), workdir_root=root
            )
        with pytest.raises(WorkdirError):
            build_registry_from_env(
                {**base, "WORKDIR_01_MAX_READ_LINES": "0"}, Settings(), workdir_root=root
            )

    def test_effective_policy_is_frozen(self) -> None:
        policy = EffectiveWorkdirPolicy()
        with pytest.raises((AttributeError, TypeError)):
            policy.allow_hidden = True  # type: ignore[misc]


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
