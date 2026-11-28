"""Fail-fast macOS Unix socket path checks for installed-Wheel acceptance fixtures."""

from pathlib import Path

import pytest

from test_darwin_v0250_installed_wheel_three_process_macos import (
    _require_short_socket_path,
    max_socket_path_bytes,
)


def test_socket_path_accepts_exact_platform_byte_budget() -> None:
    limit = max_socket_path_bytes()
    _require_short_socket_path(Path("/") / ("s" * (limit - 1)))


def test_socket_path_rejects_one_byte_over_budget() -> None:
    limit = max_socket_path_bytes()
    with pytest.raises(ValueError, match="use a shorter SERVERFS_V0250_WHEEL_SCRATCH"):
        _require_short_socket_path(Path("/") / ("s" * limit))


def test_socket_path_measures_utf8_bytes_not_characters() -> None:
    limit = max_socket_path_bytes()
    with pytest.raises(ValueError, match="socket path is"):
        _require_short_socket_path(Path("/") / ("é" * (limit // 2 + 1)))
