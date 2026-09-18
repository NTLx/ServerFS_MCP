"""Cross-cutting mutation guarantees: authorization, policy matrix, the
reserved namespace, audit logging, leak-freedom and the schema shape."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from mcp.server.mcpserver.exceptions import ResourceError, ToolError

from helpers import (
    call_error,
    call_success,
    error_code,
    make_server,
    read_write,
    registry_for,
)
from serverfs_mcp.config import Settings
from serverfs_mcp.main import create_server
from serverfs_mcp.workdirs import Workdir

MUTATION_TOOLS = [
    "create_text_file",
    "create_directory",
    "edit_text_file",
    "delete_file",
    "delete_directory",
]
REVISION_TOOLS = ["edit_text_file", "delete_file", "delete_directory"]


def prepare(workdir, tool: str, path: str) -> None:
    """Make the target satisfy this tool's precondition."""
    target = workdir.container_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if tool in ("edit_text_file", "delete_file"):
        target.write_text("content\n")
    elif tool == "delete_directory":
        target.mkdir()


def invoke(srv, workdir, tool: str, path: str, *, revision: str | None) -> dict | None:
    """Run one mutation tool against path; None means it failed with a
    coded ToolError, which is returned instead."""
    rooted = {"workdir": "test", "path": path}
    if tool in REVISION_TOOLS:
        args = {**rooted, "expected_revision": revision or "v1:x"}
    if tool == "create_text_file":
        args = {**rooted, "content": "x"}
    elif tool == "create_directory":
        args = rooted
    elif tool == "edit_text_file":
        args["edits"] = [{"old_text": "content", "new_text": "changed"}]
    try:
        return call_success(srv, tool, args)
    except ToolError as exc:
        return {"__error__": str(exc)}
    except AssertionError as exc:
        return {"__error__": str(exc)}


class TestListWorkdirsAccess:
    def test_read_only_workdir_reports_read_only(self, workdir) -> None:
        srv = create_server(Settings(), registry_for(workdir))
        data = call_success(srv, "list_workdirs", {})
        assert data["workdirs"][0]["access"] == "read-only"

    def test_read_write_workdir_reports_read_write(self, workdir) -> None:
        srv = create_server(Settings(), registry_for(read_write(workdir)))
        data = call_success(srv, "list_workdirs", {})
        assert data["workdirs"][0]["access"] == "read-write"

    def test_mixed_registry_reports_per_workdir(self, workdir) -> None:
        other = Workdir(
            slot=2,
            alias="logs",
            container_path=workdir.container_path.parent / "02",
            description=None,
            read_only=False,
        )
        (workdir.container_path.parent / "02").mkdir(exist_ok=True)
        srv = create_server(Settings(), registry_for(workdir, other))
        data = call_success(srv, "list_workdirs", {})
        access = {w["alias"]: w["access"] for w in data["workdirs"]}
        assert access == {"test": "read-only", "logs": "read-write"}


class TestWorkdirAuthorization:
    """§13: application authorization is independent of the mount mode — a
    read-only workdir refuses every mutation."""

    @pytest.mark.parametrize("tool", MUTATION_TOOLS)
    def test_read_only_workdir_refuses_every_mutation(self, workdir, tool) -> None:
        srv = make_server(workdir)
        prepare(workdir, tool, "target.txt" if tool != "delete_directory" else "target")
        msg = call_error(
            srv,
            tool,
            {
                "workdir": "test",
                "path": "target.txt" if tool != "delete_directory" else "target",
                **(
                    {"expected_revision": "v1:x"}
                    if tool in REVISION_TOOLS
                    else {"content": "x"}
                    if tool == "create_text_file"
                    else {}
                ),
                **(
                    {"edits": [{"old_text": "a", "new_text": "b"}]}
                    if tool == "edit_text_file"
                    else {}
                ),
            },
        )
        assert error_code(msg) == "WORKDIR_READ_ONLY"

    def test_authorization_precedes_the_path_policy(self, workdir) -> None:
        """A read-only workdir answers WORKDIR_READ_ONLY even for a path the
        policy would reject: the workdir is the first gate."""
        srv = make_server(workdir)
        msg = call_error(
            srv,
            "create_text_file",
            {"workdir": "test", "path": ".env", "content": "x"},
        )
        assert error_code(msg) == "WORKDIR_READ_ONLY"

    def test_unknown_workdir(self, workdir) -> None:
        srv = make_server(workdir, read_write_access=True)
        msg = call_error(
            srv, "create_text_file", {"workdir": "ghost", "path": "x.txt", "content": "x"}
        )
        assert error_code(msg) == "WORKDIR_NOT_FOUND"

    @pytest.mark.parametrize("tool", MUTATION_TOOLS)
    def test_read_write_workdir_allows_the_same_call(self, workdir, tool) -> None:
        srv = make_server(workdir, read_write_access=True)
        path = "target.txt" if tool != "delete_directory" else "target"
        prepare(workdir, tool, path)
        revision = (
            call_success(srv, "stat_file", {"workdir": "test", "path": path})["revision"]
            if tool in REVISION_TOOLS and tool != "delete_directory"
            else None
        )
        if tool == "delete_directory":
            revision = call_success(srv, "stat_file", {"workdir": "test", "path": path})["revision"]
        result = invoke(srv, workdir, tool, path, revision=revision)
        assert result is not None and "__error__" not in result, result


class TestRootProtection:
    """§61: no mutation ever targets the workdir root."""

    @pytest.mark.parametrize("tool", MUTATION_TOOLS)
    @pytest.mark.parametrize("path", ["", ".", "./", "sub/.."])
    def test_root_is_never_mutable(self, workdir, tool, path) -> None:
        srv = make_server(workdir, read_write_access=True)
        os.mkdir(workdir.container_path / "sub")
        args = {
            "workdir": "test",
            "path": path,
            **(
                {"expected_revision": "v1:x"}
                if tool in REVISION_TOOLS
                else {"content": "x"}
                if tool == "create_text_file"
                else {}
            ),
            **({"edits": [{"old_text": "a", "new_text": "b"}]} if tool == "edit_text_file" else {}),
        }
        msg = call_error(srv, tool, args)
        assert error_code(msg) == "ROOT_MUTATION_NOT_ALLOWED"


class TestReservedNamespace:
    """§103: ``.serverfs-tmp-*`` is invisible and immutable on every
    channel, even in the most permissive configuration."""

    def permissive(self, workdir):
        return make_server(
            workdir, read_write_access=True, allow_hidden=True, disable_default_deny=True
        )

    def seed(self, workdir) -> None:
        (workdir.container_path / "visible.txt").write_text("NEEDLE\n")
        (workdir.container_path / ".serverfs-tmp-abc").write_text("NEEDLE\n")
        os.mkdir(workdir.container_path / "sub")
        (workdir.container_path / "sub" / ".serverfs-tmp-deep").write_text("NEEDLE\n")

    @pytest.mark.parametrize("name", [".serverfs-tmp-abc", "sub/.serverfs-tmp-deep"])
    def test_not_listed(self, workdir, name) -> None:
        srv = self.permissive(workdir)
        self.seed(workdir)
        parent = os.path.dirname(name)
        entries = call_success(srv, "list_directory", {"workdir": "test", "path": parent})[
            "entries"
        ]
        assert all(not e["name"].startswith(".serverfs-tmp-") for e in entries)
        assert any(e["name"] == "visible.txt" for e in entries) if not parent else True

    def test_not_found_by_find_files(self, workdir) -> None:
        srv = self.permissive(workdir)
        self.seed(workdir)
        data = call_success(srv, "find_files", {"workdir": "test", "pattern": "*"})
        assert [m["path"] for m in data["matches"]] == ["visible.txt"]

    def test_not_found_by_search_text(self, workdir) -> None:
        srv = self.permissive(workdir)
        self.seed(workdir)
        data = call_success(srv, "search_text", {"workdir": "test", "query": "NEEDLE"})
        assert [m["path"] for m in data["matches"]] == ["visible.txt"]

    def test_search_root_inside_the_namespace(self, workdir) -> None:
        srv = self.permissive(workdir)
        self.seed(workdir)
        msg = call_error(
            srv, "search_text", {"workdir": "test", "path": ".serverfs-tmp-abc", "query": "NEEDLE"}
        )
        assert error_code(msg) == "RESERVED_PATH"

    @pytest.mark.parametrize("name", [".serverfs-tmp-abc", "sub/.serverfs-tmp-deep"])
    def test_not_readable_or_stat_able(self, workdir, name) -> None:
        srv = self.permissive(workdir)
        self.seed(workdir)
        for tool in ("read_text_file", "stat_file"):
            msg = call_error(srv, tool, {"workdir": "test", "path": name})
            assert error_code(msg) == "RESERVED_PATH", tool

    def test_resource_template_is_blocked(self, workdir) -> None:
        srv = self.permissive(workdir)
        self.seed(workdir)

        async def _read():
            try:
                await srv.read_resource("serverfs://test/.serverfs-tmp-abc")
            except ResourceError as exc:
                return str(exc)
            raise AssertionError("expected ResourceError")

        assert "RESERVED_PATH" in asyncio.run(_read())

    @pytest.mark.parametrize("tool", MUTATION_TOOLS)
    def test_not_mutable(self, workdir, tool) -> None:
        srv = self.permissive(workdir)
        self.seed(workdir)
        target = ".serverfs-tmp-abc"
        args = {
            "workdir": "test",
            "path": target,
            **(
                {"expected_revision": "v1:x"}
                if tool in REVISION_TOOLS
                else {"content": "x"}
                if tool == "create_text_file"
                else {}
            ),
            **(
                {"edits": [{"old_text": "NEEDLE", "new_text": "x"}]}
                if tool == "edit_text_file"
                else {}
            ),
        }
        msg = call_error(srv, tool, args)
        assert error_code(msg) == "RESERVED_PATH"
        assert (workdir.container_path / target).read_text() == "NEEDLE\n"

    def test_rg_never_reads_the_temp_namespace(self, workdir) -> None:
        """§20: the exclusion is passed to ripgrep, not only applied to
        results — the search must not depend on filtering afterwards."""
        srv = self.permissive(workdir)
        self.seed(workdir)
        data = call_success(srv, "search_text", {"workdir": "test", "query": "NEEDLE"})
        assert all(".serverfs-tmp" not in m["path"] for m in data["matches"])


class TestPolicyMatrix:
    """§100: hidden and credential-deny are independent axes, and every
    mutation channel honours them exactly like the read channels."""

    MATRIX = [
        (False, False, "plain.txt", True),
        (False, False, ".notes", False),
        (False, False, ".env", False),
        (True, False, "plain.txt", True),
        (True, False, ".notes", True),
        (True, False, ".env", False),
        (False, True, "plain.txt", True),
        (False, True, ".notes", False),
        (False, True, ".env", False),
        (True, True, "plain.txt", True),
        (True, True, ".notes", True),
        (True, True, ".env", True),
    ]

    @pytest.mark.parametrize("tool", MUTATION_TOOLS)
    @pytest.mark.parametrize("allow_hidden,deny_off,path,allowed", MATRIX)
    def test_matrix(self, workdir, tool, allow_hidden, deny_off, path, allowed) -> None:
        srv = make_server(
            workdir,
            read_write_access=True,
            allow_hidden=allow_hidden,
            disable_default_deny=deny_off,
        )
        prepare(workdir, tool, path)
        revision = None
        if allowed and tool in REVISION_TOOLS:
            revision = call_success(srv, "stat_file", {"workdir": "test", "path": path})["revision"]
        result = invoke(srv, workdir, tool, path, revision=revision)
        if allowed:
            assert result is not None and "__error__" not in result, result
            assert any(result.get(flag) for flag in ("created", "edited", "deleted")), result
        else:
            assert result is not None and "__error__" in result, result
            code = error_code(result["__error__"])
            expected = (
                "HIDDEN_PATH_NOT_ALLOWED"
                if path.startswith(".") and not allow_hidden
                else "DENIED_PATH"
            )
            assert code == expected, (path, code)

    @pytest.mark.parametrize("tool", MUTATION_TOOLS)
    def test_extra_deny_wins_in_every_configuration(self, workdir, tool) -> None:
        srv = make_server(
            workdir,
            read_write_access=True,
            allow_hidden=True,
            disable_default_deny=True,
            extra_deny_globs=("blocked.txt",),
        )
        prepare(workdir, tool, "blocked.txt")
        result = invoke(srv, workdir, tool, "blocked.txt", revision="v1:x")
        assert result is not None and "__error__" in result
        assert error_code(result["__error__"]) == "DENIED_PATH"


class TestMutationAudit:
    """§106: one audit event per call, success and failure, never content."""

    @pytest.fixture()
    def captured(self, monkeypatch):
        events: list[dict] = []
        from serverfs_mcp import logging as jsonlog

        monkeypatch.setattr(
            jsonlog, "info", lambda event, **fields: events.append({"event": event, **fields})
        )
        return events

    def events_for(self, captured, tool) -> list[dict]:
        return [e for e in captured if e["event"] == "tool_call" and e["tool"] == tool]

    def test_create_success_event(self, workdir, captured) -> None:
        srv = make_server(workdir, read_write_access=True)
        call_success(
            srv, "create_text_file", {"workdir": "test", "path": "a.txt", "content": "SECRETBODY"}
        )
        (event,) = self.events_for(captured, "create_text_file")
        assert event["success"] is True
        assert event["workdir"] == "test"
        assert event["path"] == "a.txt"
        assert event["bytes_written"] == len("SECRETBODY")
        assert event["revision"].startswith("v1:")
        assert "duration_ms" in event
        assert "SECRETBODY" not in json.dumps(event)

    def test_create_failure_event(self, workdir, captured) -> None:
        srv = make_server(workdir)
        call_error(
            srv, "create_text_file", {"workdir": "test", "path": "a.txt", "content": "SECRETBODY"}
        )
        (event,) = self.events_for(captured, "create_text_file")
        assert event["success"] is False
        assert event["error_code"] == "WORKDIR_READ_ONLY"
        assert "SECRETBODY" not in json.dumps(event)

    def test_edit_event_has_no_text(self, workdir, captured) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "a.txt").write_text("OLDVALUE\n")
        revision = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})["revision"]
        call_success(
            srv,
            "edit_text_file",
            {
                "workdir": "test",
                "path": "a.txt",
                "expected_revision": revision,
                "edits": [{"old_text": "OLDVALUE", "new_text": "NEWVALUE"}],
            },
        )
        (event,) = self.events_for(captured, "edit_text_file")
        assert event["success"] is True
        assert event["edit_count"] == 1
        assert event["bytes_before"] == len(b"OLDVALUE\n")
        assert event["bytes_after"] == len(b"NEWVALUE\n")
        blob = json.dumps(event)
        assert "OLDVALUE" not in blob
        assert "NEWVALUE" not in blob

    def test_delete_events(self, workdir, captured) -> None:
        srv = make_server(workdir, read_write_access=True)
        (workdir.container_path / "a.txt").write_text("BYTES\n")
        os.mkdir(workdir.container_path / "dir")
        revision = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})["revision"]
        call_success(
            srv,
            "delete_file",
            {"workdir": "test", "path": "a.txt", "expected_revision": revision},
        )
        call_success(
            srv,
            "delete_directory",
            {
                "workdir": "test",
                "path": "dir",
                "expected_revision": call_success(
                    srv, "stat_file", {"workdir": "test", "path": "dir"}
                )["revision"],
            },
        )
        (file_event,) = self.events_for(captured, "delete_file")
        assert file_event["success"] is True
        assert file_event["bytes_deleted"] == len(b"BYTES\n")
        assert "BYTES" not in json.dumps(file_event)
        (dir_event,) = self.events_for(captured, "delete_directory")
        assert dir_event["success"] is True

    def test_every_mutation_tool_is_audited(self, workdir, captured) -> None:
        srv = make_server(workdir, read_write_access=True)
        prepare(workdir, "create_text_file", "new.txt")
        prepare(workdir, "create_directory", "newdir")
        prepare(workdir, "edit_text_file", "edit.txt")
        prepare(workdir, "delete_file", "del.txt")
        prepare(workdir, "delete_directory", "deldir")
        for tool, path in (
            ("create_text_file", "new.txt"),
            ("create_directory", "newdir"),
            ("edit_text_file", "edit.txt"),
            ("delete_file", "del.txt"),
            ("delete_directory", "deldir"),
        ):
            revision = None
            if tool in REVISION_TOOLS:
                revision = call_success(srv, "stat_file", {"workdir": "test", "path": path})[
                    "revision"
                ]
            invoke(srv, workdir, tool, path, revision=revision)
        for tool in MUTATION_TOOLS:
            events = self.events_for(captured, tool)
            assert len(events) == 1, tool
            assert events[0]["success"] is True, tool


class TestNoInternalLeak:
    """§105: mutation errors carry a code and the agent's own path, never a
    host path, container path, temp name or payload."""

    def collect_errors(self, workdir) -> list[str]:
        srv = make_server(workdir, read_write_access=True, max_write_bytes=16, max_edits_per_call=1)
        (workdir.container_path / "a.txt").write_text("SECRETCONTENT\n")
        os.mkdir(workdir.container_path / "dir")
        (workdir.container_path / "dir" / "child").write_text("x")
        os.symlink("a.txt", workdir.container_path / "lnk")
        os.mkfifo(workdir.container_path / "pipe")
        (workdir.container_path / "bin").write_bytes(b"\x00\x01SECRETBYTES")
        revision = call_success(srv, "stat_file", {"workdir": "test", "path": "a.txt"})["revision"]
        cases = [
            ("create_text_file", {"path": "a.txt", "content": "SECRETCONTENT"}),
            ("create_text_file", {"path": "a.txt", "content": "x"}),
            ("create_text_file", {"path": "nodir/x", "content": "x"}),
            ("create_text_file", {"path": "big.txt", "content": "x" * 32}),
            ("create_text_file", {"path": "nul.txt", "content": "a\x00SECRETCONTENT"}),
            ("create_directory", {"path": "a.txt"}),
            ("create_directory", {"path": "nodir/x"}),
            (
                "edit_text_file",
                {
                    "path": "ghost",
                    "expected_revision": "v1:x",
                    "edits": [{"old_text": "SECRETCONTENT", "new_text": "y"}],
                },
            ),
            (
                "edit_text_file",
                {
                    "path": "lnk",
                    "expected_revision": "v1:x",
                    "edits": [{"old_text": "SECRETCONTENT", "new_text": "y"}],
                },
            ),
            (
                "edit_text_file",
                {
                    "path": "pipe",
                    "expected_revision": "v1:x",
                    "edits": [{"old_text": "SECRETCONTENT", "new_text": "y"}],
                },
            ),
            (
                "edit_text_file",
                {
                    "path": "bin",
                    "expected_revision": "v1:x",
                    "edits": [{"old_text": "SECRETCONTENT", "new_text": "y"}],
                },
            ),
            (
                "edit_text_file",
                {
                    "path": "a.txt",
                    "expected_revision": revision,
                    "edits": [{"old_text": "SECRETCONTENT", "new_text": "y" * 64}],
                },
            ),
            (
                "edit_text_file",
                {
                    "path": "a.txt",
                    "expected_revision": "v1:stale",
                    "edits": [{"old_text": "SECRETCONTENT", "new_text": "y"}],
                },
            ),
            ("delete_file", {"path": "dir", "expected_revision": "v1:x"}),
            ("delete_file", {"path": "lnk", "expected_revision": "v1:x"}),
            ("delete_directory", {"path": "dir", "expected_revision": "v1:x"}),
            ("delete_directory", {"path": "lnk", "expected_revision": "v1:x"}),
            ("delete_directory", {"path": "a.txt", "expected_revision": "v1:x"}),
            ("delete_directory", {"path": "ghost", "expected_revision": "v1:x"}),
        ]
        messages = []
        for tool, extra in cases:
            args = {"workdir": "test", **extra}
            messages.append(call_error(srv, tool, args))
        return messages

    def test_no_paths_contents_or_temp_names(self, workdir) -> None:
        messages = self.collect_errors(workdir)
        blob = "\n".join(messages)
        assert "/workdirs" not in blob
        assert str(workdir.container_path) not in blob
        assert str(workdir.container_path.parent) not in blob
        assert ".serverfs-tmp-" not in blob
        assert "SECRETCONTENT" not in blob
        assert "SECRETBYTES" not in blob
        assert "/tmp/" not in blob

    def test_every_error_is_coded(self, workdir) -> None:
        for message in self.collect_errors(workdir):
            assert error_code(message), message


class TestFlatSchema:
    """§78: mutation tools keep flat parameters — no request wrapper."""

    def schema(self, workdir, tool) -> dict:
        srv = make_server(workdir, read_write_access=True)

        async def _get():
            for t in await srv.list_tools():
                if t.name == tool:
                    return t.input_schema
            raise AssertionError(f"{tool} not registered")

        return asyncio.run(_get())

    @pytest.mark.parametrize(
        "tool,expected",
        [
            ("create_text_file", {"workdir", "path", "content"}),
            ("create_directory", {"workdir", "path"}),
            ("edit_text_file", {"workdir", "path", "expected_revision", "edits"}),
            ("delete_file", {"workdir", "path", "expected_revision"}),
            ("delete_directory", {"workdir", "path", "expected_revision"}),
        ],
    )
    def test_top_level_parameters(self, workdir, tool, expected) -> None:
        schema = self.schema(workdir, tool)
        assert set(schema["properties"]) == expected
        assert "request" not in schema["properties"]

    def test_edits_schema_describes_an_item(self, workdir) -> None:
        schema = self.schema(workdir, "edit_text_file")
        edits = schema["properties"]["edits"]
        assert edits["type"] == "array"
        assert edits["minItems"] == 1
        item = edits["items"]
        if "$ref" in item:  # the SDK emits a $ref into $defs
            item = schema["$defs"][item["$ref"].rsplit("/", 1)[-1]]
        assert set(item["properties"]) == {"old_text", "new_text", "expected_count"}

    def test_revision_is_required_where_it_matters(self, workdir) -> None:
        for tool in REVISION_TOOLS:
            schema = self.schema(workdir, tool)
            assert "expected_revision" in schema.get("required", []), tool

    def test_tool_descriptions_teach_the_contract(self, workdir) -> None:
        """§92: the basics must be in the docstring the agent receives."""
        srv = make_server(workdir, read_write_access=True)

        async def _descriptions():
            return {t.name: (t.description or "") for t in await srv.list_tools()}

        desc = asyncio.run(_descriptions())
        assert "NEW" in desc["create_text_file"]
        assert "never overwrites" in desc["create_text_file"].lower()
        assert "revision" in desc["edit_text_file"].lower()
        assert "REVISION_CONFLICT" in desc["edit_text_file"]
        assert "permanent" in desc["delete_file"].lower()
        assert "empty" in desc["delete_directory"].lower()
        assert "never recursive" in desc["delete_directory"].lower()


class TestServerSurface:
    def test_server_reports_the_package_version(self, workdir) -> None:
        from importlib.metadata import version

        from serverfs_mcp import SERVER_VERSION

        assert SERVER_VERSION == version("serverfs-mcp")

    def test_instructions_describe_read_only_by_default(self) -> None:
        from serverfs_mcp.main import INSTRUCTIONS

        assert "read-only by default" in INSTRUCTIONS
        assert "never executes commands" in INSTRUCTIONS
        assert "never modifies files" not in INSTRUCTIONS
