"""MCP tool registration: the only 6 tools v0.1 exposes.

Tools use flat parameter signatures: the SDK turns each function parameter
into a top-level input-schema property, so agents call e.g.
``list_directory {"workdir": "projects", "path": ""}`` directly.

Every tool funnels user input through the shared path resolver and raises
ToolError with a CODE: message for anticipated failures, so the agent gets a
short, recoverable error instead of a traceback. Each call also emits a
structured audit log event (no content, no query text, no host paths).
"""

from __future__ import annotations

import contextlib
import os
import time
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import logging as jsonlog
from .config import Settings
from .fdio import open_directory_fd, open_file_fd
from .filesystem import find_files as find_files_impl
from .filesystem import list_directory as list_directory_impl
from .filesystem import stat_file as stat_file_impl
from .models import (
    FileMatch,
    FindFilesResult,
    ListDirectoryResult,
    ListWorkdirsResult,
    ReadTextFileResult,
    SearchTextResult,
    StatFileResult,
)
from .paths import DenyPolicy, PathSecurityError, ResolvedPath, resolve_workdir_path
from .search import SearchTimeout, run_search
from .workdirs import WorkdirRegistry

ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=False)

_BINARY_SAMPLE = 1024

WorkdirArg = Annotated[str, Field(description="Workdir alias to operate on")]
PathArg = Annotated[
    str, Field(default="", description="Directory path relative to the workdir root")
]

# The SDK evaluates parameter annotations against module globals, so
# server-configured defaults are published through these module-level names
# (assigned per-process in register_tools before any tool is registered).
default_list_limit = 100
default_search_limit = 50
list_limit_arg = Annotated[int, Field(default=100, ge=1, description="Maximum entries")]
search_limit_arg = Annotated[int, Field(default=50, ge=1, description="Maximum matches")]


def deny_policy_from_settings(settings: Settings) -> DenyPolicy:
    """Build the DenyPolicy a request runs under from runtime settings."""
    return DenyPolicy(
        extra_globs=tuple(settings.extra_deny_globs),
        default_deny_enabled=not settings.disable_default_deny,
    )


@contextlib.contextmanager
def _root_fd(resolved: ResolvedPath):
    fd = os.open(str(resolved.workdir.container_path), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield fd
    finally:
        os.close(fd)


def _resolve(
    registry: WorkdirRegistry, workdir: str, path: str, settings: Settings
) -> ResolvedPath:
    """Resolve + policy-validate; raises ToolError with CODE: messages."""
    wd = registry.get(workdir)
    if wd is None:
        raise ToolError(f"WORKDIR_NOT_FOUND: {workdir!r} is not a configured workdir")
    try:
        return resolve_workdir_path(
            wd,
            path,
            allow_hidden=settings.allow_hidden,
            deny_policy=deny_policy_from_settings(settings),
        )
    except PathSecurityError as exc:
        code = getattr(exc, "code", "ACCESS_DENIED")
        raise ToolError(f"{code}: {workdir}:{path} — {exc.message}") from exc


# ---- audit logging ----


def _audit(
    tool: str,
    t0: float,
    *,
    success: bool,
    workdir: str | None = None,
    path: str | None = None,
    error_code: str | None = None,
    **fields: object,
) -> None:
    """Emit one structured tool_call audit event.

    Never logs file content, query text, or any host/container path beyond
    the agent-visible workdir alias and relative path.
    """
    record: dict[str, object] = {
        "tool": tool,
        "duration_ms": round((time.monotonic() - t0) * 1000, 1),
        "success": success,
    }
    if workdir is not None:
        record["workdir"] = workdir
    if path is not None:
        record["path"] = path
    if error_code is not None:
        record["error_code"] = error_code
    record.update(fields)
    jsonlog.info("tool_call", **record)


def _error_code(exc: ToolError) -> str:
    return str(exc).split(":", 1)[0].strip()


# ---- shared read-file logic (tools + resource template) ----


def _read_text_file_impl(
    registry: WorkdirRegistry,
    settings: Settings,
    workdir: str,
    path: str,
    start_line: int,
    max_lines: int,
) -> ReadTextFileResult:
    resolved = _resolve(registry, workdir, path, settings)

    with contextlib.ExitStack() as stack:
        root = stack.enter_context(_root_fd(resolved))
        try:
            fd = stack.enter_context(open_file_fd(root, resolved.rel_parts))
        except FileNotFoundError:
            raise ToolError(f"PATH_NOT_FOUND: {workdir}:{path} does not exist") from None
        except NotADirectoryError:
            raise ToolError(f"NOT_A_FILE: {workdir}:{path} is not a regular file") from None
        except IsADirectoryError:
            raise ToolError(f"NOT_A_FILE: {workdir}:{path} is a directory") from None
        except PathSecurityError as exc:
            code = getattr(exc, "code", "ACCESS_DENIED")
            raise ToolError(f"{code}: {workdir}:{path} — {exc.message}") from exc
        except OSError as exc:
            raise ToolError(f"ACCESS_DENIED: {workdir}:{path} ({exc.strerror})") from None

        with open(fd, "rb", closefd=False) as fh:
            sample = fh.read(_BINARY_SAMPLE)
            if b"\x00" in sample:
                raise ToolError(f"BINARY_FILE: {workdir}:{path} appears to be a binary file")
            # BOM is stripped from the physical FIRST line of the file only
            bom = sample.startswith(b"\xef\xbb\xbf")
            fh.seek(0)
            lines: list[bytes] = []
            line_no = 0
            bytes_returned = 0
            end_line = start_line - 1
            has_more = False
            for raw in fh:
                line_no += 1
                if line_no == 1 and bom:
                    raw = raw[3:]
                if line_no < start_line:
                    continue
                if len(raw) > settings.max_read_bytes:
                    raise ToolError(
                        f"LINE_TOO_LARGE: {workdir}:{path} line {line_no} exceeds "
                        f"{settings.max_read_bytes} bytes"
                    )
                if len(lines) >= max_lines:
                    has_more = True
                    break
                if bytes_returned + len(raw) > settings.max_read_bytes:
                    has_more = True
                    break
                lines.append(raw)
                bytes_returned += len(raw)
                end_line = line_no

        try:
            text = b"".join(lines).decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError(
                f"UNSUPPORTED_TEXT_ENCODING: {workdir}:{path} is not valid UTF-8"
            ) from None

    next_start_line = end_line + 1 if has_more else None
    if not lines:
        end_line = start_line - 1
        has_more = False
    return ReadTextFileResult(
        workdir=workdir,
        path=path,
        start_line=start_line,
        end_line=end_line,
        content=text,
        bytes_returned=bytes_returned,
        has_more=has_more,
        next_start_line=next_start_line,
    )


# ---- registration ----


def register_tools(mcp: MCPServer, registry: WorkdirRegistry, settings: Settings) -> None:
    """Register the six v0.1 tools on the MCPServer instance."""
    global default_list_limit, default_search_limit, list_limit_arg, search_limit_arg
    default_list_limit = min(settings.default_list_limit, settings.max_list_entries)
    default_search_limit = min(settings.default_search_results, settings.max_search_results)
    list_limit_arg = Annotated[
        int,
        Field(
            default=default_list_limit,
            ge=1,
            description="Maximum entries to return (server default applies if omitted)",
        ),
    ]
    search_limit_arg = Annotated[
        int,
        Field(
            default=default_search_limit,
            ge=1,
            description="Maximum matches to return (server default applies if omitted)",
        ),
    ]

    @mcp.tool(annotations=ANNOTATIONS)
    def list_workdirs() -> ListWorkdirsResult:
        """List all configured workdirs with their descriptions.

        Use this first to discover which workdirs are available before
        exploring the filesystem.
        """
        t0 = time.monotonic()
        result = registry.list_result()
        _audit("list_workdirs", t0, success=True, returned=len(result.workdirs))
        return result

    @mcp.tool(annotations=ANNOTATIONS)
    def list_directory(
        workdir: WorkdirArg,
        path: PathArg = "",
        offset: Annotated[int, Field(default=0, ge=0, description="Entries to skip")] = 0,
        limit: list_limit_arg = default_list_limit,
    ) -> ListDirectoryResult:
        """List entries of a directory inside a workdir.

        Entries are sorted by name. Symlinks are shown as type "symlink",
        FIFOs/sockets/devices as type "other"; neither is ever followed.
        """
        t0 = time.monotonic()
        limit = min(limit, settings.max_list_entries)
        try:
            resolved = _resolve(registry, workdir, path, settings)
            entries, has_more = list_directory_impl(resolved, offset=offset, limit=limit)
        except ToolError as exc:
            _audit(
                "list_directory",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=_error_code(exc),
            )
            raise
        except PathSecurityError as exc:
            _audit(
                "list_directory",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=exc.code,
            )
            raise ToolError(f"{exc.code}: {workdir}:{path} — {exc.message}") from exc
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError) as exc:
            err = _fs_error(exc, workdir, path)
            _audit(
                "list_directory",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=_error_code(err),
            )
            raise err from exc
        _audit(
            "list_directory",
            t0,
            success=True,
            workdir=workdir,
            path=path,
            returned=len(entries),
            has_more=has_more,
        )
        return ListDirectoryResult(
            workdir=workdir,
            path=path,
            entries=entries,
            offset=offset,
            limit=limit,
            returned=len(entries),
            has_more=has_more,
        )

    @mcp.tool(annotations=ANNOTATIONS)
    def find_files(
        workdir: WorkdirArg,
        pattern: Annotated[
            str, Field(description='Glob pattern matched against file names, e.g. "*compose*.yml"')
        ],
        path: PathArg = "",
        limit: search_limit_arg = default_search_limit,
    ) -> FindFilesResult:
        """Find files by name using a glob pattern, recursively.

        Pattern is matched against file names (fnmatch semantics), e.g.
        "*compose*.yml". Symlinked directories are not followed. truncated
        is true whenever the scan stopped early due to a limit.
        """
        t0 = time.monotonic()
        limit = min(limit, settings.max_search_results)
        try:
            resolved = _resolve(registry, workdir, path, settings)
            with contextlib.ExitStack() as stack:
                root = stack.enter_context(_root_fd(resolved))
                stack.enter_context(open_directory_fd(root, resolved.rel_parts))
            matches, truncated = find_files_impl(
                resolved,
                pattern=pattern,
                limit=limit,
                max_walk_entries=settings.max_walk_entries,
            )
        except ToolError as exc:
            _audit(
                "find_files",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=_error_code(exc),
            )
            raise
        except PathSecurityError as exc:
            _audit(
                "find_files",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=exc.code,
            )
            raise ToolError(f"{exc.code}: {workdir}:{path} — {exc.message}") from exc
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError) as exc:
            err = _fs_error(exc, workdir, path)
            _audit(
                "find_files",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=_error_code(err),
            )
            raise err from exc
        _audit(
            "find_files",
            t0,
            success=True,
            workdir=workdir,
            path=path,
            returned=len(matches),
            truncated=truncated,
        )
        return FindFilesResult(
            matches=[FileMatch(path=m) for m in matches],
            returned=len(matches),
            truncated=truncated,
        )

    @mcp.tool(annotations=ANNOTATIONS)
    def search_text(
        workdir: WorkdirArg,
        query: Annotated[str, Field(description="Literal text to search for (not a regex)")],
        path: PathArg = "",
        glob: Annotated[
            str | None, Field(default=None, description='Optional glob filter, e.g. "*.py"')
        ] = None,
        case_sensitive: Annotated[
            bool, Field(default=True, description="Whether matching is case-sensitive")
        ] = True,
        limit: search_limit_arg = default_search_limit,
    ) -> SearchTextResult:
        """Search text file contents for a literal string (not regex).

        Uses ripgrep. UTF-8 text files are searched; oversized files are
        skipped. Provide a glob like "*.py" to restrict file names. The
        result limit is global: the search stops as soon as enough
        policy-valid matches are found.
        """
        t0 = time.monotonic()
        limit = min(limit, settings.max_search_results)
        try:
            resolved = _resolve(registry, workdir, path, settings)
            with contextlib.ExitStack() as stack:
                root = stack.enter_context(_root_fd(resolved))
                root_dir_fd = stack.enter_context(open_directory_fd(root, resolved.rel_parts))
                try:
                    matches, truncated = run_search(
                        root_dir_fd,
                        resolved,
                        query=query,
                        glob=glob,
                        case_sensitive=case_sensitive,
                        limit=limit,
                        timeout_seconds=settings.search_timeout_seconds,
                        max_file_bytes=settings.search_max_file_bytes,
                    )
                except SearchTimeout:
                    raise ToolError(
                        f"SEARCH_TIMEOUT: search in {workdir}:{path} exceeded "
                        f"{settings.search_timeout_seconds}s"
                    ) from None
                except RuntimeError as exc:
                    raise ToolError(str(exc)) from None
        except ToolError as exc:
            _audit(
                "search_text",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=_error_code(exc),
            )
            raise
        except PathSecurityError as exc:
            _audit(
                "search_text",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=exc.code,
            )
            raise ToolError(f"{exc.code}: {workdir}:{path} — {exc.message}") from exc
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError) as exc:
            err = _fs_error(exc, workdir, path)
            _audit(
                "search_text",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=_error_code(err),
            )
            raise err from exc
        _audit(
            "search_text",
            t0,
            success=True,
            workdir=workdir,
            path=path,
            returned=len(matches),
            truncated=truncated,
        )
        return SearchTextResult(
            matches=matches,
            returned=len(matches),
            truncated=truncated,
        )

    @mcp.tool(annotations=ANNOTATIONS)
    def read_text_file(
        workdir: WorkdirArg,
        path: Annotated[str, Field(description="File path relative to the workdir root")],
        start_line: Annotated[
            int, Field(default=1, ge=1, description="1-based first line to read")
        ] = 1,
        max_lines: Annotated[
            int, Field(default=200, ge=1, description="Maximum lines to return")
        ] = 200,
    ) -> ReadTextFileResult:
        """Read a UTF-8 text file with line-based pagination.

        Reads at most max_lines lines and a server-configured byte budget.
        Use next_start_line to continue reading.
        """
        t0 = time.monotonic()
        max_lines = min(max_lines, settings.max_read_lines)
        try:
            result = _read_text_file_impl(registry, settings, workdir, path, start_line, max_lines)
        except ToolError as exc:
            _audit(
                "read_text_file",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=_error_code(exc),
            )
            raise
        _audit(
            "read_text_file",
            t0,
            success=True,
            workdir=workdir,
            path=path,
            bytes_returned=result.bytes_returned,
            start_line=result.start_line,
            end_line=result.end_line,
            has_more=result.has_more,
        )
        return result

    @mcp.tool(annotations=ANNOTATIONS)
    def stat_file(
        workdir: WorkdirArg,
        path: Annotated[str, Field(description="Path relative to the workdir root")],
    ) -> StatFileResult:
        """Get metadata for one file or directory.

        Returns type (file/directory/symlink/other), size, modified time
        (RFC 3339 UTC) and best-effort MIME type. A symlink as the FINAL
        component is reported as type "symlink" (target never revealed);
        symlinks in parent components are rejected.
        """
        t0 = time.monotonic()
        try:
            resolved = _resolve(registry, workdir, path, settings)
            result = stat_file_impl(resolved)
        except ToolError as exc:
            _audit(
                "stat_file",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=_error_code(exc),
            )
            raise
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError) as exc:
            err = _fs_error(exc, workdir, path)
            _audit(
                "stat_file",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=_error_code(err),
            )
            raise err from exc
        except PathSecurityError as exc:
            err = ToolError(f"{exc.code}: {workdir}:{path} — {exc.message}")
            _audit(
                "stat_file",
                t0,
                success=False,
                workdir=workdir,
                path=path,
                error_code=exc.code,
            )
            raise err from exc
        _audit(
            "stat_file",
            t0,
            success=True,
            workdir=workdir,
            path=path,
            type=result.type,
        )
        return result


def _fs_error(exc: OSError, workdir: str, path: str) -> ToolError:
    """Map an OS-level failure to a coded ToolError (no internal paths)."""
    if isinstance(exc, FileNotFoundError):
        return ToolError(f"PATH_NOT_FOUND: {workdir}:{path} does not exist")
    if isinstance(exc, IsADirectoryError):
        return ToolError(f"NOT_A_FILE: {workdir}:{path} is a directory")
    if isinstance(exc, NotADirectoryError):
        return ToolError(f"NOT_A_DIRECTORY: {workdir}:{path} is not a directory")
    return ToolError(f"ACCESS_DENIED: {workdir}:{path} ({exc.strerror})")


# keep reference for resource template reuse
READ_IMPL = _read_text_file_impl
