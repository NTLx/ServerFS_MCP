"""MCP tool registration: the only 6 tools v0.1 exposes.

Each tool funnels user input through the shared path resolver and raises
ToolError with a CODE: message for every anticipated failure, so the agent
gets a short, recoverable error instead of a traceback.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .config import Settings
from .models import (
    FindFilesResult,
    ListDirectoryResult,
    ListWorkdirsResult,
    ReadTextFileResult,
    SearchTextResult,
    StatFileResult,
)
from .paths import (
    PathSecurityError,
    ResolvedPath,
    resolve_workdir_path,
)
from .search import SearchTimeout, run_search
from .workdirs import WorkdirRegistry

ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=False)

_BINARY_SAMPLE = 1024


def _resolve(
    registry: WorkdirRegistry, workdir: str, path: str, settings: Settings
) -> ResolvedPath:
    wd = registry.get(workdir)
    if wd is None:
        raise ToolError(f"WORKDIR_NOT_FOUND: {workdir!r} is not a configured workdir")
    try:
        return resolve_workdir_path(wd, path, allow_hidden=settings.allow_hidden)
    except PathSecurityError as exc:
        code = getattr(exc, "code", "ACCESS_DENIED")
        raise ToolError(f"{code}: {workdir}:{path} — {exc.message}") from exc


# ---- input schemas ----


class ListDirectoryInput(BaseModel):
    workdir: str = Field(description="Workdir alias to list")
    path: str = Field(default="", description="Directory path relative to the workdir root")
    offset: int = Field(default=0, ge=0, description="Number of entries to skip")
    limit: int = Field(default=100, ge=1, description="Maximum entries to return")


class FindFilesInput(BaseModel):
    workdir: str = Field(description="Workdir alias to search in")
    path: str = Field(default="", description="Directory path relative to the workdir root")
    pattern: str = Field(description="Glob pattern matched against file names (e.g. *.py)")
    limit: int = Field(default=50, ge=1, description="Maximum matches to return")


class SearchTextInput(BaseModel):
    workdir: str = Field(description="Workdir alias to search in")
    path: str = Field(default="", description="Directory path relative to the workdir root")
    query: str = Field(description="Literal text to search for (not a regex)")
    glob: str | None = Field(
        default=None, description="Optional glob filter on file names (e.g. *.py)"
    )
    case_sensitive: bool = Field(default=True, description="Whether matching is case-sensitive")
    limit: int = Field(default=50, ge=1, description="Maximum matches to return")


class ReadTextFileInput(BaseModel):
    workdir: str = Field(description="Workdir alias containing the file")
    path: str = Field(description="File path relative to the workdir root")
    start_line: int = Field(default=1, ge=1, description="1-based first line to read")
    max_lines: int = Field(default=200, ge=1, description="Maximum lines to return")


class StatFileInput(BaseModel):
    workdir: str = Field(description="Workdir alias containing the path")
    path: str = Field(description="Path relative to the workdir root")


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
    import os

    try:
        st = os.stat(resolved.container_path, follow_symlinks=False)
    except FileNotFoundError:
        raise ToolError(f"PATH_NOT_FOUND: {workdir}:{path} does not exist") from None
    import stat as stat_module

    if stat_module.S_ISDIR(st.st_mode):
        raise ToolError(f"NOT_A_FILE: {workdir}:{path} is a directory")
    if not stat_module.S_ISREG(st.st_mode):
        raise ToolError(f"UNSUPPORTED_FILE_TYPE: {workdir}:{path} is not a regular file")

    with open(resolved.container_path, "rb") as fh:
        sample = fh.read(_BINARY_SAMPLE)
        if b"\x00" in sample:
            raise ToolError(f"BINARY_FILE: {workdir}:{path} appears to be a binary file")
        # skip BOM on the first line only
        bom = sample.startswith(b"\xef\xbb\xbf")
        fh.seek(0)
        lines: list[bytes] = []
        # skip to start_line
        line_no = 0
        bytes_returned = 0
        end_line = start_line - 1
        has_more = False
        for raw in fh:
            line_no += 1
            if line_no < start_line:
                continue
            if line_no == start_line and bom:
                raw = raw[3:]
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
        else:
            # iterator exhausted: has_more only if more lines exist beyond
            has_more = False

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

    @mcp.tool(annotations=ANNOTATIONS)
    def list_workdirs() -> ListWorkdirsResult:
        """List all configured workdirs with their descriptions.

        Use this first to discover which workdirs are available before
        exploring the filesystem.
        """
        return registry.list_result()

    @mcp.tool(annotations=ANNOTATIONS)
    def list_directory(input: ListDirectoryInput) -> ListDirectoryResult:
        """List entries of a directory inside a workdir.

        Entries are sorted by name. Hidden files are excluded. Symlinks are
        shown as type "symlink" and never followed.
        """
        resolved = _resolve(registry, input.workdir, input.path, settings)
        limit = min(input.limit, settings.max_list_entries)
        try:
            entries, has_more = list_directory(resolved, offset=input.offset, limit=limit)
        except NotADirectoryError:
            raise ToolError(
                f"NOT_A_DIRECTORY: {input.workdir}:{input.path} is not a directory"
            ) from None
        except FileNotFoundError:
            raise ToolError(
                f"PATH_NOT_FOUND: {input.workdir}:{input.path} does not exist"
            ) from None
        return ListDirectoryResult(
            workdir=input.workdir,
            path=input.path,
            entries=entries,
            offset=input.offset,
            limit=limit,
            returned=len(entries),
            has_more=has_more,
        )

    @mcp.tool(annotations=ANNOTATIONS)
    def find_files(input: FindFilesInput) -> FindFilesResult:
        """Find files by name using a glob pattern, recursively.

        Pattern is matched against file names (fnmatch semantics), e.g.
        "*compose*.yml". Symlinked directories are not followed.
        """
        resolved = _resolve(registry, input.workdir, input.path, settings)
        import os

        try:
            st = os.stat(resolved.container_path, follow_symlinks=False)
        except FileNotFoundError:
            raise ToolError(
                f"PATH_NOT_FOUND: {input.workdir}:{input.path} does not exist"
            ) from None
        if not st.st_mode & 0o170000 == 0o040000:
            raise ToolError(f"NOT_A_DIRECTORY: {input.workdir}:{input.path}")
        limit = min(input.limit, settings.max_search_results)
        matches, truncated = find_files(
            resolved,
            pattern=input.pattern,
            limit=limit,
            max_walk_entries=settings.max_walk_entries,
        )
        return FindFilesResult(
            matches=[{"path": m} for m in matches],  # type: ignore[arg-type]
            returned=len(matches),
            truncated=truncated,
        )

    @mcp.tool(annotations=ANNOTATIONS)
    def search_text(input: SearchTextInput) -> SearchTextResult:
        """Search text file contents for a literal string (not regex).

        Uses ripgrep. UTF-8 text files are searched; oversized files are
        skipped. Provide a glob like "*.py" to restrict file names.
        """
        resolved = _resolve(registry, input.workdir, input.path, settings)
        limit = min(input.limit, settings.max_search_results)
        try:
            matches, truncated = run_search(
                resolved,
                query=input.query,
                glob=input.glob,
                case_sensitive=input.case_sensitive,
                limit=limit,
                timeout_seconds=settings.search_timeout_seconds,
                max_file_bytes=settings.search_max_file_bytes,
            )
        except SearchTimeout:
            raise ToolError(
                f"SEARCH_TIMEOUT: search in {input.workdir}:{input.path} exceeded "
                f"{settings.search_timeout_seconds}s"
            ) from None
        except RuntimeError as exc:
            raise ToolError(str(exc)) from None
        return SearchTextResult(
            matches=matches,
            returned=len(matches),
            truncated=truncated,
        )

    @mcp.tool(annotations=ANNOTATIONS)
    def read_text_file(input: ReadTextFileInput) -> ReadTextFileResult:
        """Read a UTF-8 text file with line-based pagination.

        Reads at most max_lines lines and max_bytes bytes (both configurable
        server-side). Use next_start_line to continue reading.
        """
        max_lines = min(input.max_lines, settings.max_read_lines)
        return _read_text_file_impl(
            registry,
            settings,
            input.workdir,
            input.path,
            input.start_line,
            max_lines,
        )

    @mcp.tool(annotations=ANNOTATIONS)
    def stat_file(input: StatFileInput) -> StatFileResult:
        """Get metadata for one file or directory.

        Returns type (file/directory/symlink), size, modified time (RFC 3339
        UTC) and best-effort MIME type. Symlinks are not followed.
        """
        resolved = _resolve(registry, input.workdir, input.path, settings)
        try:
            result = stat_file(resolved)
        except FileNotFoundError:
            raise ToolError(
                f"PATH_NOT_FOUND: {input.workdir}:{input.path} does not exist"
            ) from None
        return result


# keep reference for resource template reuse
READ_IMPL = _read_text_file_impl
