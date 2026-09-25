"""End-to-end policy + audit + resource tests through the MCP surface.

Exercises the tools/resources the way the agent would: via mcp.call_tool /
read_resource, verifying that policy errors surface as recoverable coded
ToolError messages, that the hidden/deny policy matrix holds on every
channel, and that audit logs carry no secrets.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import socket

import pytest
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from helpers import make_server
from serverfs_mcp.config import Settings
from serverfs_mcp.main import STREAMABLE_HTTP_TRANSPORT_SECURITY, create_server
from serverfs_mcp.workdirs import EffectiveWorkdirPolicy, Workdir, WorkdirRegistry


@pytest.fixture()
def server(registry, settings):
    return create_server(settings, registry)


def call_error(server, name: str, args: dict) -> str:
    """Call a tool expecting a ToolError; return its message."""

    async def _call():
        try:
            await server.call_tool(name, args)
        except ToolError as e:
            return str(e)
        raise AssertionError(f"expected ToolError from {name}, got success")

    return asyncio.run(_call())


def call_success(server, name: str, args: dict) -> dict:
    """Call a tool expecting success; return structured content."""

    async def _call():
        result = await server.call_tool(name, args)
        assert not result.is_error, result
        return result.structured_content

    return asyncio.run(_call())


def read_resource_ok(server, uri: str) -> str:

    async def _read():
        r = await server.read_resource(uri)
        return r[0].content

    return asyncio.run(_read())


def read_resource_error(server, uri: str) -> str:

    async def _read():
        try:
            await server.read_resource(uri)
        except ResourceError as e:
            return str(e)
        raise AssertionError(f"expected ResourceError for {uri}")

    return asyncio.run(_read())


def _registry_for(workdir):
    from serverfs_mcp.workdirs import WorkdirRegistry

    return WorkdirRegistry([workdir])


def _make_server(workdir, **settings_kw):
    return make_server(workdir, **settings_kw)


def _streamable_http_request(server, headers: list[tuple[bytes, bytes]]) -> int:
    """Send an initialize request through the real Streamable HTTP ASGI app."""

    async def _request() -> int:
        app = server.streamable_http_app(
            streamable_http_path="/mcp",
            json_response=True,
            stateless_http=True,
            transport_security=STREAMABLE_HTTP_TRANSPORT_SECURITY,
            host="0.0.0.0",
        )
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "security-test", "version": "1"},
                },
            }
        ).encode()
        messages: list[dict] = []
        request_sent = False

        async def receive() -> dict:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "headers": [*headers, (b"accept", b"application/json, text/event-stream")],
            "client": ("security-test", 1),
            "server": ("serverfs-mcp", 8000),
        }
        async with app.router.lifespan_context(app):
            await app(scope, receive, send)
        response = next(message for message in messages if message["type"] == "http.response.start")
        return int(response["status"])

    return asyncio.run(_request())


SENSITIVE = ".env"


class TestStreamableHTTPTransportSecurity:
    def test_measured_host_without_origin_reaches_mcp_transport(self, server) -> None:
        status = _streamable_http_request(
            server,
            [(b"host", b"serverfs-mcp:8000"), (b"content-type", b"application/json")],
        )
        assert status == 200

    @pytest.mark.parametrize("host", [b"unexpected.example:8000", b"serverfs-mcp:9999"])
    def test_unexpected_host_is_rejected(self, server, host) -> None:
        status = _streamable_http_request(
            server,
            [(b"host", host), (b"content-type", b"application/json")],
        )
        assert status == 421

    def test_missing_host_is_rejected(self, server) -> None:
        status = _streamable_http_request(server, [(b"content-type", b"application/json")])
        assert status == 421

    def test_nonempty_origin_is_rejected(self, server) -> None:
        status = _streamable_http_request(
            server,
            [
                (b"host", b"serverfs-mcp:8000"),
                (b"origin", b"https://unexpected.example"),
                (b"content-type", b"application/json"),
            ],
        )
        assert status == 403


class TestToolErrors:
    def test_traversal_blocked(self, server) -> None:
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "../../etc/passwd"})
        assert "PATH_OUTSIDE_WORKDIR" in msg

    def test_absolute_path_blocked(self, server) -> None:
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "/etc/passwd"})
        assert "PATH_OUTSIDE_WORKDIR" in msg

    def test_workdir_not_found(self, server) -> None:
        msg = call_error(server, "list_directory", {"workdir": "nope", "path": ""})
        assert "WORKDIR_NOT_FOUND" in msg

    def test_hidden_read_blocked(self, server, workdir) -> None:
        (workdir.container_path / ".env").write_text("SECRET=1")
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": ".env"})
        assert "HIDDEN_PATH_NOT_ALLOWED" in msg

    def test_denied_read_blocked_even_with_allow_hidden(self, workdir) -> None:
        (workdir.container_path / "id_rsa").write_text("KEY")
        settings = Settings(allow_hidden=True)
        srv = make_server(workdir, allow_hidden=settings.allow_hidden)
        msg = call_error(srv, "read_text_file", {"workdir": "test", "path": "id_rsa"})
        assert "DENIED_PATH" in msg

    def test_symlink_escape_blocked(self, server, workdir) -> None:
        os.symlink("/etc", workdir.container_path / "etclink")
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "etclink/passwd"})
        assert "SYMLINK_NOT_ALLOWED" in msg

    def test_symlink_parent_stat_blocked(self, server, workdir) -> None:
        os.symlink("/etc", workdir.container_path / "etclink")
        msg = call_error(server, "stat_file", {"workdir": "test", "path": "etclink/passwd"})
        assert "SYMLINK_NOT_ALLOWED" in msg

    def test_stat_final_symlink_reports_type(self, server, workdir) -> None:
        (workdir.container_path / "real.txt").write_text("x")
        os.symlink("real.txt", workdir.container_path / "lnk")
        data = call_success(server, "stat_file", {"workdir": "test", "path": "lnk"})
        assert data["type"] == "symlink"

    def test_path_not_found(self, server) -> None:
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "ghost.txt"})
        assert "PATH_NOT_FOUND" in msg

    def test_search_text_non_directory(self, server, workdir) -> None:
        """§14: searching with path=README.md gives NOT_A_DIRECTORY."""
        (workdir.container_path / "README.md").write_text("hello\n")
        msg = call_error(
            server, "search_text", {"workdir": "test", "path": "README.md", "query": "hello"}
        )
        assert "NOT_A_DIRECTORY" in msg

    def test_nul_rejected(self, server) -> None:
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "a\x00b"})
        assert "ACCESS_DENIED" in msg

    def test_find_files_emfile_reports_resource_exhausted(self, server, monkeypatch) -> None:
        def fail_scandir(*args, **kwargs):
            raise OSError(errno.EMFILE, "Too many open files")

        monkeypatch.setattr(os, "scandir", fail_scandir)
        msg = call_error(server, "find_files", {"workdir": "test", "pattern": "*"})
        assert "RESOURCE_EXHAUSTED" in msg


class TestPolicyMatrix:
    """§48: hidden policy and credential-deny policy are two independent
    dimensions, consistent across ALL channels."""

    CHANNELS = [
        ("read", lambda s, wd: call_error(s, "read_text_file", {"workdir": "test", "path": wd})),
        ("stat", lambda s, wd: call_error(s, "stat_file", {"workdir": "test", "path": wd})),
    ]

    def _seed(self, workdir, name: str, content: str = "VALUE=1\n") -> None:
        (workdir.container_path / name).write_text(content)

    def _visible_in_list(self, srv, name) -> bool:
        data = call_success(srv, "list_directory", {"workdir": "test", "path": ""})
        return any(e["name"] == name for e in data["entries"])

    def _found_by_find(self, srv, name) -> bool:
        data = call_success(srv, "find_files", {"workdir": "test", "pattern": name})
        return any(m["path"] == name for m in data["matches"])

    def _found_by_search(self, srv, name, needle) -> bool:
        data = call_success(srv, "search_text", {"workdir": "test", "query": needle})
        return any(m["path"] == name for m in data["matches"])

    def _readable(self, srv, name) -> bool:
        try:
            data = call_success(srv, "read_text_file", {"workdir": "test", "path": name})
            return bool(data["content"])
        except (AssertionError, ToolError):
            return False

    def _stat_ok(self, srv, name) -> bool:
        try:
            call_success(srv, "stat_file", {"workdir": "test", "path": name})
            return True
        except (AssertionError, ToolError):
            return False

    def test_hidden_off_deny_on_all_channels(self, workdir) -> None:
        """allow_hidden=false, default deny on."""
        srv = _make_server(workdir)
        self._seed(workdir, ".env")
        self._seed(workdir, "plain.txt", "NEEDLE\n")
        assert not self._visible_in_list(srv, ".env")
        assert not self._found_by_find(srv, ".env")
        assert not self._found_by_search(srv, ".env", "VALUE")
        assert not self._readable(srv, ".env")
        assert not self._stat_ok(srv, ".env")
        # plain file works everywhere
        assert self._readable(srv, "plain.txt")

    def test_hidden_on_deny_on_all_channels(self, workdir) -> None:
        """allow_hidden=true: hidden visible, but deny still blocks."""
        srv = _make_server(workdir, allow_hidden=True)
        self._seed(workdir, ".env")
        self._seed(workdir, ".notes", "NEEDLE\n")
        # .env still denied by credential rules on every channel
        assert not self._readable(srv, ".env")
        assert not self._found_by_find(srv, ".env")
        assert not self._found_by_search(srv, ".env", "VALUE")
        # non-credential hidden file now accessible everywhere
        assert self._visible_in_list(srv, ".notes")
        assert self._found_by_find(srv, ".notes")
        assert self._found_by_search(srv, ".notes", "NEEDLE")
        assert self._readable(srv, ".notes")
        assert self._stat_ok(srv, ".notes")

    def test_hidden_off_deny_off(self, workdir) -> None:
        """allow_hidden=false, deny off: hidden still blocked (independent)."""
        srv = _make_server(workdir, disable_default_deny=True)
        self._seed(workdir, ".env")
        assert not self._readable(srv, ".env")  # hidden policy
        assert not self._visible_in_list(srv, ".env")

    def test_hidden_on_deny_off_dev_mode(self, workdir) -> None:
        """§46: allow_hidden=true + disable_default_deny=true — everything
        readable on every channel."""
        srv = _make_server(workdir, allow_hidden=True, disable_default_deny=True)
        self._seed(workdir, ".env")
        self._seed(workdir, "app.pem", "CERT\n")
        assert self._readable(srv, ".env")
        assert self._visible_in_list(srv, ".env")
        assert self._found_by_find(srv, ".env")
        assert self._found_by_search(srv, ".env", "VALUE")
        assert self._stat_ok(srv, ".env")
        assert self._readable(srv, "app.pem")

    def test_extra_deny_survives_disable(self, workdir) -> None:
        """§47: extra globs apply on every channel even with deny off."""
        srv = _make_server(
            workdir,
            allow_hidden=True,
            disable_default_deny=True,
            extra_deny_globs=("*.env",),
        )
        self._seed(workdir, ".env")
        self._seed(workdir, "app.env")
        for name in (".env", "app.env"):
            assert not self._readable(srv, name)
            assert not self._visible_in_list(srv, name)
            assert not self._found_by_find(srv, name)
            assert not self._found_by_search(srv, name, "VALUE")
            assert not self._stat_ok(srv, name)

    def test_scoped_search_deny_bypass_blocked(self, workdir) -> None:
        """§5: search root inside a denied subtree → DENIED_PATH, no rg."""
        os.mkdir(workdir.container_path / "internal")
        (workdir.container_path / "internal" / "x.py").write_text("SECRET\n")
        srv = _make_server(workdir, extra_deny_globs=("internal/**",))
        msg = call_error(
            srv, "search_text", {"workdir": "test", "path": "internal", "query": "SECRET"}
        )
        assert "DENIED_PATH" in msg
        # root search does not leak matches under internal/
        data = call_success(srv, "search_text", {"workdir": "test", "query": "SECRET"})
        assert data["returned"] == 0

    def test_default_cred_dir_scoped_search(self, workdir) -> None:
        """§45: with allow_hidden + default deny, search_text(path=".ssh")
        → DENIED_PATH; root search never returns .ssh/config content."""
        os.mkdir(workdir.container_path / ".ssh")
        (workdir.container_path / ".ssh" / "config").write_text("Host secret\n")
        srv = _make_server(workdir, allow_hidden=True)
        msg = call_error(srv, "search_text", {"workdir": "test", "path": ".ssh", "query": "Host"})
        assert "DENIED_PATH" in msg
        data = call_success(srv, "search_text", {"workdir": "test", "query": "Host"})
        assert data["returned"] == 0


class TestDefaultLimits:
    """§18: SERVERFS_DEFAULT_* must take effect on the actual MCP surface."""

    def test_default_list_limit_applied(self, workdir) -> None:
        for i in range(30):
            (workdir.container_path / f"f{i:02d}.txt").write_text("x")
        srv = _make_server(workdir, default_list_limit=7)
        data = call_success(srv, "list_directory", {"workdir": "test", "path": ""})
        assert data["limit"] == 7
        assert data["returned"] == 7
        assert data["has_more"] is True

    def test_default_search_results_applied(self, workdir) -> None:
        (workdir.container_path / "lim.txt").write_text("NEEDLE\n" * 10)
        srv = _make_server(workdir, default_search_results=3)
        data = call_success(srv, "search_text", {"workdir": "test", "query": "NEEDLE"})
        assert data["returned"] == 3
        assert data["truncated"] is True

    def test_schema_default_published(self, workdir) -> None:
        """The tool schema must advertise the server-configured default."""
        srv = _make_server(workdir, default_list_limit=7)

        async def get_schema():
            tools = await srv.list_tools()
            for t in tools:
                if t.name == "list_directory":
                    return t.input_schema
            raise AssertionError("list_directory not found")

        schema = asyncio.run(get_schema())
        assert schema["properties"]["limit"]["default"] == 7


class TestResourceTemplate:
    """§21/§20: resource goes through the same policy; no silent truncation."""

    def _srv(self, workdir, **kw):
        return _make_server(workdir, **kw)

    def test_small_file_full_content(self, workdir) -> None:
        (workdir.container_path / "ok.txt").write_text("hello resource\n")
        text = read_resource_ok(self._srv(workdir), "serverfs://test/ok.txt")
        assert text == "hello resource\n"

    def test_large_line_count_rejected(self, workdir) -> None:
        (workdir.container_path / "big.txt").write_text("line\n" * 2000)
        msg = read_resource_error(self._srv(workdir), "serverfs://test/big.txt")
        assert "RESOURCE_TOO_LARGE" in msg

    def test_large_byte_count_rejected(self, workdir) -> None:
        # 600 KB total in 300 lines: over the byte budget, under the line cap
        (workdir.container_path / "wide.txt").write_text(("x" * 1999 + "\n") * 300)
        msg = read_resource_error(self._srv(workdir), "serverfs://test/wide.txt")
        assert "RESOURCE_TOO_LARGE" in msg

    def test_hidden_policy_applies(self, workdir) -> None:
        (workdir.container_path / ".notes").write_text("x")
        msg = read_resource_error(self._srv(workdir), "serverfs://test/.notes")
        assert "HIDDEN_PATH_NOT_ALLOWED" in msg

    def test_deny_policy_applies(self, workdir) -> None:
        (workdir.container_path / "id_rsa").write_text("KEY")
        msg = read_resource_error(self._srv(workdir, allow_hidden=True), "serverfs://test/id_rsa")
        assert "DENIED_PATH" in msg

    def test_disable_default_deny_resource_readable(self, workdir) -> None:
        (workdir.container_path / ".env").write_text("VALUE=1\n")
        text = read_resource_ok(
            self._srv(workdir, allow_hidden=True, disable_default_deny=True),
            "serverfs://test/.env",
        )
        assert text == "VALUE=1\n"

    def test_extra_deny_resource_blocked(self, workdir) -> None:
        (workdir.container_path / ".env").write_text("VALUE=1\n")
        msg = read_resource_error(
            self._srv(
                workdir, allow_hidden=True, disable_default_deny=True, extra_deny_globs=("*.env",)
            ),
            "serverfs://test/.env",
        )
        assert "DENIED_PATH" in msg


class TestSpecialFilesViaTool:
    def test_fifo_never_blocks(self, server, workdir) -> None:
        os.mkfifo(workdir.container_path / "apipe")
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "apipe"})
        assert "UNSUPPORTED_FILE_TYPE" in msg

    def test_fifo_stat_other(self, server, workdir) -> None:
        os.mkfifo(workdir.container_path / "apipe")
        data = call_success(server, "stat_file", {"workdir": "test", "path": "apipe"})
        assert data["type"] == "other"

    def test_fifo_listed_as_other(self, server, workdir) -> None:
        os.mkfifo(workdir.container_path / "apipe")
        data = call_success(server, "list_directory", {"workdir": "test", "path": ""})
        entry = next(e for e in data["entries"] if e["name"] == "apipe")
        assert entry["type"] == "other"

    def test_unix_socket_never_blocks(self, server, workdir) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(str(workdir.container_path / "asock"))
        s.close()
        msg = call_error(server, "read_text_file", {"workdir": "test", "path": "asock"})
        assert "UNSUPPORTED_FILE_TYPE" in msg


def test_effective_policy_is_selected_per_workdir_for_tools_and_resources(tmp_path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / ".hidden.txt").write_text("one\ntwo\n")
    (second_root / ".hidden.txt").write_text("one\ntwo\n")
    (first_root / "large.txt").write_text("123456789\n")
    (second_root / "large.txt").write_text("123456789\n")
    first = Workdir(
        1,
        "first",
        first_root,
        None,
        policy=EffectiveWorkdirPolicy(allow_hidden=True, max_read_bytes=20, max_read_lines=1),
    )
    second = Workdir(
        2,
        "second",
        second_root,
        None,
        policy=EffectiveWorkdirPolicy(max_read_bytes=5, max_read_lines=2),
    )
    server = create_server(Settings(), WorkdirRegistry([first, second]))

    first_read = call_success(
        server, "read_text_file", {"workdir": "first", "path": ".hidden.txt", "max_lines": 10}
    )
    assert first_read["content"] == "one\n"
    assert "HIDDEN_PATH_NOT_ALLOWED" in call_error(
        server, "read_text_file", {"workdir": "second", "path": ".hidden.txt"}
    )
    assert "LINE_TOO_LARGE" in call_error(
        server, "read_text_file", {"workdir": "second", "path": "large.txt"}
    )
    with pytest.raises(ResourceError, match="RESOURCE_TOO_LARGE"):
        read_resource_ok(server, "serverfs://first/.hidden.txt")
    with pytest.raises(ResourceError, match="LINE_TOO_LARGE"):
        read_resource_ok(server, "serverfs://second/large.txt")


class TestAuditLogging:
    """§58: every call emits a structured tool_call event with no secrets."""

    @pytest.fixture()
    def captured(self, monkeypatch):
        events: list[dict] = []
        from serverfs_mcp import logging as jsonlog

        monkeypatch.setattr(
            jsonlog, "info", lambda event, **fields: events.append({"event": event, **fields})
        )
        return events

    def test_success_event_fields(self, workdir, captured) -> None:
        (workdir.container_path / "ok.txt").write_text("CONTENTBODY\n")
        srv = _make_server(workdir)
        data = call_success(srv, "read_text_file", {"workdir": "test", "path": "ok.txt"})
        ev = next(
            e for e in captured if e["event"] == "tool_call" and e["tool"] == "read_text_file"
        )
        assert ev["success"] is True
        assert ev["workdir"] == "test"
        assert ev["path"] == "ok.txt"
        assert ev["bytes_returned"] == data["bytes_returned"]
        assert "duration_ms" in ev
        assert "CONTENTBODY" not in json.dumps(ev)

    def test_failure_event_error_code(self, workdir, captured) -> None:
        (workdir.container_path / ".env").write_text("SECRET=1\n")
        srv = _make_server(workdir)
        call_error(srv, "read_text_file", {"workdir": "test", "path": ".env"})
        ev = next(
            e
            for e in captured
            if e["event"] == "tool_call" and e["tool"] == "read_text_file" and not e["success"]
        )
        assert ev["error_code"] == "HIDDEN_PATH_NOT_ALLOWED"
        assert "SECRET" not in json.dumps(ev)

    def test_log_never_contains_container_path_or_key(self, workdir, captured) -> None:
        (workdir.container_path / "ok.txt").write_text("body\n")
        srv = _make_server(workdir)
        call_success(srv, "read_text_file", {"workdir": "test", "path": "ok.txt"})
        call_success(srv, "list_directory", {"workdir": "test", "path": ""})
        blob = json.dumps(captured)
        assert "/workdirs" not in blob
        assert str(workdir.container_path.parent) not in blob
        assert "CONTROL_PLANE_API_KEY" not in blob

    def test_search_event_has_no_query(self, workdir, captured) -> None:
        (workdir.container_path / "a.txt").write_text("NEEDLEWORD\n")
        srv = _make_server(workdir)
        call_success(srv, "search_text", {"workdir": "test", "query": "NEEDLEWORD"})
        ev = next(e for e in captured if e["event"] == "tool_call" and e["tool"] == "search_text")
        assert "NEEDLEWORD" not in json.dumps(ev)
        assert ev["returned"] == 1

    def test_startup_event_includes_security_mode(self, monkeypatch, workdir, registry) -> None:
        from serverfs_mcp import logging as jsonlog
        from serverfs_mcp.main import log_startup

        events: list[dict] = []
        monkeypatch.setattr(
            jsonlog, "info", lambda event, **fields: events.append({"event": event, **fields})
        )

        log_startup(Settings(allow_hidden=True, extra_deny_globs=("*.x",)), registry)
        ev = next(e for e in events if e["event"] == "startup")
        assert ev["global_allow_hidden"] is True
        assert ev["global_default_deny_enabled"] is True
        assert ev["global_extra_deny_rule_count"] == 1
        assert "*.x" not in json.dumps(ev)  # rule content stays out of logs


class TestToolSurface:
    """§95: exactly eleven tools — six read, five mutation — with the
    required annotations."""

    READ_TOOLS = [
        "find_files",
        "list_directory",
        "list_workdirs",
        "read_text_file",
        "search_text",
        "stat_file",
    ]
    MUTATION_TOOLS = [
        "create_directory",
        "create_text_file",
        "delete_directory",
        "delete_file",
        "edit_text_file",
    ]
    # read_only, destructive, idempotent
    EXPECTED_ANNOTATIONS = {
        **{name: (True, None, None) for name in READ_TOOLS},
        "create_text_file": (False, False, False),
        "create_directory": (False, False, True),
        "edit_text_file": (False, True, True),
        "delete_file": (False, True, True),
        "delete_directory": (False, True, True),
    }

    def test_eleven_tools_with_annotations(self, server) -> None:

        async def check():
            tools = await server.list_tools()
            names = sorted(t.name for t in tools)
            assert names == sorted(self.READ_TOOLS + self.MUTATION_TOOLS)
            for t in tools:
                assert t.annotations is not None, t.name
                read_only, destructive, idempotent = self.EXPECTED_ANNOTATIONS[t.name]
                assert t.annotations.read_only_hint is read_only, t.name
                assert t.annotations.open_world_hint is False, t.name
                if destructive is not None:
                    assert t.annotations.destructive_hint is destructive, t.name
                if idempotent is not None:
                    assert t.annotations.idempotent_hint is idempotent, t.name

        asyncio.run(check())
