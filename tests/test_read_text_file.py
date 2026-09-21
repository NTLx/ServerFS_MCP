"""read_text_file behaviour: encoding, pagination, limits."""

from __future__ import annotations

import dataclasses

import pytest

from serverfs_mcp.config import Settings
from serverfs_mcp.tools import READ_IMPL
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, WorkdirRegistry


def read(registry, settings, path, start=1, max_lines=200, workdir="test"):
    selected = registry.get(workdir)
    if selected is not None:
        selected = dataclasses.replace(
            selected,
            policy=EffectiveWorkdirPolicy(
                allow_hidden=settings.allow_hidden,
                disable_default_deny=settings.disable_default_deny,
                extra_deny_globs=settings.extra_deny_globs,
                max_read_bytes=settings.max_read_bytes,
                max_read_lines=settings.max_read_lines,
                max_write_bytes=settings.max_write_bytes,
                binary_transfer_enabled=settings.binary_transfer_enabled,
                max_binary_transfer_bytes=settings.max_binary_transfer_bytes,
                agent_mode=selected.policy.agent_mode,
                agent_runtimes=selected.policy.agent_runtimes,
            ),
        )
        registry = WorkdirRegistry([selected])
    return READ_IMPL(registry, settings, workdir, path, start, max_lines)


class TestEncodings:
    def test_ascii(self, registry, settings, workdir) -> None:
        (workdir.container_path / "a.txt").write_text("hello\nworld\n")
        r = read(registry, settings, "a.txt")
        assert r.content == "hello\nworld\n"

    def test_utf8_chinese(self, registry, settings, workdir) -> None:
        (workdir.container_path / "zh.txt").write_text("第一行\n第二行\n")
        r = read(registry, settings, "zh.txt")
        assert r.content == "第一行\n第二行\n"

    def test_utf8_bom(self, registry, settings, workdir) -> None:
        (workdir.container_path / "bom.txt").write_bytes("﻿content\n".encode())
        r = read(registry, settings, "bom.txt")
        assert r.content == "content\n"
        assert not r.content.startswith("﻿")

    def test_bom_start_line_2_verbatim(self, registry, settings, workdir) -> None:
        """BOM is stripped from the physical first line ONLY; a read that
        starts at line 2 must return that line without losing 3 bytes."""
        (workdir.container_path / "bom2.txt").write_bytes(
            "\ufefffirst\nsecond line\n".encode("utf-8")
        )
        r = read(registry, settings, "bom2.txt", start=2)
        assert r.content == "second line\n"

    def test_bom_chinese_second_line(self, registry, settings, workdir) -> None:
        (workdir.container_path / "bom3.txt").write_bytes("\ufeff第一行\n第二行\n".encode("utf-8"))
        r = read(registry, settings, "bom3.txt", start=2)
        assert r.content == "第二行\n"

    def test_bom_pagination_continuous(self, registry, settings, workdir) -> None:
        """Page-by-page reading of a BOM file reassembles the exact content."""
        (workdir.container_path / "bom4.txt").write_bytes(
            "\ufeffline1\nline2\nline3\n".encode("utf-8")
        )
        pages = []
        start = 1
        while True:
            r = read(registry, settings, "bom4.txt", start=start, max_lines=1)
            pages.append(r.content)
            if not r.has_more:
                break
            start = r.next_start_line
        assert "".join(pages) == "line1\nline2\nline3\n"

    def test_empty_file(self, registry, settings, workdir) -> None:
        (workdir.container_path / "empty.txt").write_text("")
        r = read(registry, settings, "empty.txt")
        assert r.content == ""
        assert r.has_more is False
        assert r.next_start_line is None

    def test_binary_file_rejected(self, registry, settings, workdir) -> None:
        (workdir.container_path / "bin.dat").write_bytes(b"\x00\x01\x02\xff")
        with pytest.raises(Exception, match="BINARY_FILE"):
            read(registry, settings, "bin.dat")

    def test_invalid_utf8_rejected(self, registry, settings, workdir) -> None:
        (workdir.container_path / "bad.txt").write_bytes(b"line1\n\xff\xfe\xfd\n")
        with pytest.raises(Exception, match="UNSUPPORTED_TEXT_ENCODING"):
            read(registry, settings, "bad.txt")


class TestPagination:
    @pytest.fixture()
    def paginated(self, workdir) -> None:
        content = "\n".join(f"line{i:03d}" for i in range(1, 101)) + "\n"
        (workdir.container_path / "big.txt").write_text(content)

    def test_start_line(self, registry, settings, paginated) -> None:
        r = read(registry, settings, "big.txt", start=50)
        assert r.start_line == 50
        assert r.content.startswith("line050\n")

    def test_first_page_has_more(self, registry, settings, paginated) -> None:
        r = read(registry, settings, "big.txt", max_lines=30)
        assert r.has_more is True
        assert r.next_start_line == 31
        assert r.end_line == 30

    def test_second_page_continues(self, registry, settings, paginated) -> None:
        r1 = read(registry, settings, "big.txt", max_lines=30)
        r2 = read(registry, settings, "big.txt", start=r1.next_start_line, max_lines=30)
        assert r2.start_line == 31
        assert r2.content.startswith("line031\n")

    def test_eof_has_more_false(self, registry, settings, paginated) -> None:
        r = read(registry, settings, "big.txt", start=95, max_lines=200)
        assert r.has_more is False
        assert r.next_start_line is None
        assert r.content.count("\n") == 6

    def test_bytes_returned_counts(self, registry, settings, paginated) -> None:
        r = read(registry, settings, "big.txt", max_lines=10)
        assert r.bytes_returned == len("line001\n") * 10


class TestLimits:
    def test_max_lines_capped(self, registry, workdir) -> None:
        settings = Settings(max_read_lines=5)
        (workdir.container_path / "x.txt").write_text("\n".join(f"l{i}" for i in range(1, 21)))
        # impl receives already-capped value from the tool layer; emulate that
        r = read(registry, settings, "x.txt", max_lines=5)
        assert r.content.count("\n") == 5
        assert r.has_more is True

    def test_max_bytes_stops(self, registry, workdir) -> None:
        settings = Settings(max_read_bytes=30)
        (workdir.container_path / "y.txt").write_text("a" * 10 + "\n" * 10)
        r = read(registry, settings, "y.txt", max_lines=200)
        assert r.bytes_returned <= 30

    def test_line_too_large(self, registry, workdir) -> None:
        settings = Settings(max_read_bytes=10)
        (workdir.container_path / "huge.txt").write_text("x" * 100 + "\n")
        with pytest.raises(Exception, match="LINE_TOO_LARGE"):
            read(registry, settings, "huge.txt")


class TestErrors:
    def test_missing_file(self, registry, settings) -> None:
        with pytest.raises(Exception, match="PATH_NOT_FOUND"):
            read(registry, settings, "nope.txt")

    def test_directory_rejected(self, registry, settings, workdir) -> None:
        (workdir.container_path / "adir").mkdir()
        with pytest.raises(Exception, match="NOT_A_FILE"):
            read(registry, settings, "adir")

    def test_unknown_workdir(self, registry, settings) -> None:
        with pytest.raises(Exception, match="WORKDIR_NOT_FOUND"):
            read(registry, settings, "a.txt", workdir="nope")

    def test_traversal_rejected(self, registry, settings) -> None:
        with pytest.raises(Exception, match="PATH_OUTSIDE_WORKDIR"):
            read(registry, settings, "../../etc/passwd")

    def test_fifo_does_not_block(self, registry, settings, workdir) -> None:
        import os

        os.mkfifo(workdir.container_path / "pipe")
        with pytest.raises(Exception, match="UNSUPPORTED_FILE_TYPE"):
            read(registry, settings, "pipe")

    def test_unix_socket_does_not_block(self, registry, settings, workdir) -> None:
        import socket

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(workdir.container_path / "asock"))
        s.close()
        with pytest.raises(Exception, match="UNSUPPORTED_FILE_TYPE"):
            read(registry, settings, "asock")
