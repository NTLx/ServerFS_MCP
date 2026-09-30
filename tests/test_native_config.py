"""Tests for the native TOML configuration model (v0.10 Phase A, §7).

Contract focus: the config file is operator-owned trust input, so every rule
violation aborts startup instead of being repaired at request time. Parsing
must stay platform-neutral — these tests assert validation rules, not any
filesystem behavior (root acquisition belongs to the backend).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.native_config import (
    MAX_NATIVE_WORKDIRS,
    NativeConfigError,
    load_native_config,
)


def _write(tmp_path: Path, content: str) -> Path:
    config = tmp_path / "serverfs.toml"
    config.write_text(content, encoding="utf-8")
    return config


BASE_CONFIG = """\
[server]
log_level = "INFO"

[defaults]
max_read_bytes = 262144

[[workdirs]]
alias = "projects"
path = "/srv/projects"
description = "Development projects"
read_only = false

[[workdirs]]
alias = "documents"
path = "/srv/documents"
read_only = true
"""


class TestValidConfig:
    def test_parses_workdirs_and_defaults(self, tmp_path: Path) -> None:
        workdirs, server = load_native_config(_write(tmp_path, BASE_CONFIG))
        assert [w.alias for w in workdirs] == ["projects", "documents"]
        assert server.log_level == "INFO"
        assert workdirs[0].read_only is False
        assert workdirs[1].read_only is True
        assert workdirs[0].description == "Development projects"
        assert workdirs[1].description is None
        # defaults apply to every workdir
        assert all(w.policy.max_read_bytes == 262144 for w in workdirs)
        # native workdirs carry no legacy slot
        assert all(w.slot == 0 for w in workdirs)

    def test_minimal_config_uses_safe_defaults(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            """\
[[workdirs]]
alias = "solo"
path = "/srv/solo"
""",
        )
        workdirs, server = load_native_config(config)
        assert server.log_level == "INFO"
        w = workdirs[0]
        assert w.read_only is True  # safe default when omitted
        assert w.policy.allow_hidden is False
        assert w.policy.max_read_bytes == 524_288


class TestFailClosed:
    @pytest.mark.parametrize(
        "alias",
        ["", "1abc", "-abc", "has space", "has/slash", "ümlaut", "a" * 33],
    )
    def test_invalid_alias_fails(self, tmp_path: Path, alias: str) -> None:
        config = _write(
            tmp_path,
            f"""\
[[workdirs]]
alias = "{alias}"
path = "/srv/x"
""",
        )
        with pytest.raises(NativeConfigError, match="alias"):
            load_native_config(config)

    @pytest.mark.parametrize("raw_path", ["relative/path", "./here"])
    def test_relative_path_fails(self, tmp_path: Path, raw_path: str) -> None:
        config = _write(
            tmp_path,
            f"""\
[[workdirs]]
alias = "x"
path = "{raw_path}"
""",
        )
        with pytest.raises(NativeConfigError, match="absolute"):
            load_native_config(config)

    def test_parent_component_path_fails(self, tmp_path: Path) -> None:
        # The root is a trusted anchor (§10.2); `..` must not survive parsing.
        config = _write(tmp_path, '[[workdirs]]\nalias = "x"\npath = "/srv/../etc"\n')
        with pytest.raises(NativeConfigError, match=r"\.\."):
            load_native_config(config)

    def test_duplicate_alias_fails(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            """\
[[workdirs]]
alias = "dup"
path = "/srv/one"

[[workdirs]]
alias = "dup"
path = "/srv/two"
""",
        )
        with pytest.raises(NativeConfigError, match="duplicate"):
            load_native_config(config)

    def test_no_workdirs_fails(self, tmp_path: Path) -> None:
        config = _write(tmp_path, '[server]\nlog_level = "INFO"\n')
        with pytest.raises(NativeConfigError, match="at least one"):
            load_native_config(config)

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        with pytest.raises(NativeConfigError, match="not found"):
            load_native_config(tmp_path / "absent.toml")

    def test_invalid_toml_fails(self, tmp_path: Path) -> None:
        config = _write(tmp_path, "this is [not valid toml")
        with pytest.raises(NativeConfigError, match="invalid TOML"):
            load_native_config(config)

    def test_wrong_type_read_only_fails(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            """\
[[workdirs]]
alias = "x"
path = "/srv/x"
read_only = "false"
""",
        )
        with pytest.raises(NativeConfigError, match="read_only"):
            load_native_config(config)

    def test_negative_limit_fails(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            """\
[defaults]
max_read_bytes = -1

[[workdirs]]
alias = "x"
path = "/srv/x"
""",
        )
        with pytest.raises(NativeConfigError, match="max_read_bytes"):
            load_native_config(config)

    def test_unknown_workdir_key_fails(self, tmp_path: Path) -> None:
        # unknown keys fail rather than being silently ignored: a typo like
        # read_only vs readonly must not silently become "writable default"
        config = _write(
            tmp_path,
            """\
[[workdirs]]
alias = "x"
path = "/srv/x"
readonly = false
""",
        )
        with pytest.raises(NativeConfigError, match="unknown keys"):
            load_native_config(config)

    def test_unknown_defaults_key_fails(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            """\
[defaults]
max_read_bytse = 1

[[workdirs]]
alias = "x"
path = "/srv/x"
""",
        )
        with pytest.raises(NativeConfigError, match="unknown .defaults. keys"):
            load_native_config(config)

    def test_invalid_log_level_fails(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            '[server]\nlog_level = "LOUD"\n\n[[workdirs]]\nalias = "x"\npath = "/srv/x"\n',
        )
        with pytest.raises(NativeConfigError, match="log_level"):
            load_native_config(config)

    def test_workdir_count_ceiling(self, tmp_path: Path) -> None:
        entries = "\n".join(
            f'[[workdirs]]\nalias = "w{i}"\npath = "/srv/w{i}"\n'
            for i in range(MAX_NATIVE_WORKDIRS + 1)
        )
        config = _write(tmp_path, entries)
        with pytest.raises(NativeConfigError, match="too many workdirs"):
            load_native_config(config)


class TestWindowsPathNeutrality:
    """Native config must accept Windows-style absolute roots verbatim.

    Parsing stays platform-neutral (§7.1): a Windows root is validated only
    for absoluteness/lexical rules here; whether it can be opened is the
    backend's job on the platform that runs it.
    """

    @pytest.mark.parametrize(
        "raw_path",
        [r"D:\Projects", r"C:\Users\me\Documents", r"\\?\D:\Projects"],
    )
    def test_windows_root_parses(self, tmp_path: Path, raw_path: str) -> None:
        literal = raw_path.replace("\\", "\\\\")
        config = _write(
            tmp_path,
            f'[[workdirs]]\nalias = "win"\npath = "{literal}"\nread_only = true\n',
        )
        workdirs, _ = load_native_config(config)
        assert str(workdirs[0].container_path) == raw_path

    def test_windows_root_with_parent_component_fails(self, tmp_path: Path) -> None:
        config = _write(
            tmp_path,
            '[[workdirs]]\nalias = "win"\npath = "D:\\\\..\\\\secret"\n',
        )
        with pytest.raises(NativeConfigError, match=r"\.\."):
            load_native_config(config)
