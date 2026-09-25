"""MCP tool registration: core filesystem tools plus optional capabilities.

Tools use flat parameter signatures: the SDK turns each function parameter
into a top-level input-schema property, so agents call e.g.
``list_directory {"workdir": "projects", "path": ""}`` directly.

Every tool funnels user input through the shared path resolver and raises
ToolError with a CODE: message for anticipated failures, so the agent gets a
short, recoverable error instead of a traceback. Each call also emits a
structured audit log event (no content, no query text, no host paths).

Mutation tools add one more gate ahead of the path policy: the workdir must
be configured read-write. ServerFS refuses to write even if the Docker bind
mount happens to be writable, so application authorization and mount mode
are independent layers.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import os
import time
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    ToolAnnotations,
)
from pydantic import Field

from . import logging as jsonlog
from .agent_leases import (
    AgentLeaseError,
    WorkdirBusyError,
    WorkdirRecoveryRequiredError,
    mutation_agent_lease,
)
from .binary import BinaryTransferError, decode_base64_payload
from .binary import read_binary_file as read_binary_file_impl
from .config import Settings
from .fdio import open_directory_fd, open_file_fd, root_fd
from .file_ingress_client import FileIngressClient
from .filesystem import find_files as find_files_impl
from .filesystem import list_directory as list_directory_impl
from .filesystem import stat_file as stat_file_impl
from .models import (
    CreateDirectoryResult,
    CreateTextFileResult,
    DeleteDirectoryResult,
    DeleteFileResult,
    DownloadBinaryFileMetadata,
    EditTextFileResult,
    FileMatch,
    FindFilesResult,
    ListDirectoryResult,
    ListWorkdirsResult,
    OpenAIFileInput,
    ReadTextFileResult,
    SearchTextResult,
    StatFileResult,
    TextEdit,
    UploadBinaryFileResult,
)
from .mutations import MutationError, mutation_lock
from .mutations import compute_revision as revision_of
from .mutations import create_binary_file as create_binary_file_impl
from .mutations import create_directory as create_directory_impl
from .mutations import create_text_file as create_text_file_impl
from .mutations import delete_directory as delete_directory_impl
from .mutations import delete_file as delete_file_impl
from .mutations import edit_text_file as edit_text_file_impl
from .mutations import replace_binary_file as replace_binary_file_impl
from .paths import DenyPolicy, PathSecurityError, ResolvedPath, resolve_workdir_path
from .search import SearchTimeout, run_search
from .workdirs import WorkdirRegistry

# read-only tools
ANNOTATIONS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
# create_text_file may touch parent-directory metadata even when publication
# fails (the same-directory temp file is created and cleaned up), so advertise
# the conservative retry hint. create_directory has no such temporary entry.
CREATE_FILE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
CREATE_DIRECTORY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
# edit/delete replace or remove data: idempotent because a retry with the
# same arguments cannot apply a second change (REVISION_CONFLICT/not-found)
DESTRUCTIVE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)

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


def deny_policy_from_workdir(workdir) -> DenyPolicy:
    """Build the DenyPolicy from one workdir's resolved effective policy."""
    return DenyPolicy(
        extra_globs=workdir.policy.extra_deny_globs,
        default_deny_enabled=not workdir.policy.disable_default_deny,
    )


def _root_fd(resolved: ResolvedPath):
    """Root-FD context manager for a resolved workdir path (fdio.root_fd)."""
    return root_fd(str(resolved.workdir.container_path))


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
            allow_hidden=wd.policy.allow_hidden,
            deny_policy=deny_policy_from_workdir(wd),
        )
    except PathSecurityError as exc:
        code = getattr(exc, "code", "ACCESS_DENIED")
        raise ToolError(f"{code}: {workdir}:{path} — {exc.message}") from exc


def _resolve_binary_read(
    registry: WorkdirRegistry, workdir: str, path: str, settings: Settings
) -> ResolvedPath:
    """Authorize the optional binary channel, then reuse the shared path gate."""
    wd = registry.get(workdir)
    if wd is None:
        raise ToolError(f"WORKDIR_NOT_FOUND: {workdir!r} is not a configured workdir")
    if not wd.policy.binary_transfer_enabled:
        raise ToolError(
            f"BINARY_TRANSFER_DISABLED: binary transfer is disabled for workdir {workdir}"
        )
    return _resolve(registry, workdir, path, settings)


def _resolve_binary_mutable(
    registry: WorkdirRegistry, workdir: str, path: str, settings: Settings
) -> ResolvedPath:
    """Authorize a binary mutation before applying the shared path policy."""
    wd = registry.get(workdir)
    if wd is None:
        raise ToolError(f"WORKDIR_NOT_FOUND: {workdir!r} is not a configured workdir")
    if wd.read_only:
        raise ToolError(
            f"WORKDIR_READ_ONLY: {workdir} is configured read-only. "
            "Only workdirs reported as read-write accept mutations."
        )
    if not wd.policy.binary_transfer_enabled:
        raise ToolError(
            f"BINARY_TRANSFER_DISABLED: binary transfer is disabled for workdir {workdir}"
        )
    resolved = _resolve(registry, workdir, path, settings)
    if not resolved.rel_parts:
        raise ToolError(
            f"ROOT_MUTATION_NOT_ALLOWED: the root of {workdir} cannot be created or replaced"
        )
    return resolved


def _binary_resource_uri(workdir: str, path: str) -> str:
    """Stable agent-visible URI for one binary content block."""
    return f"serverfs://{workdir}/{quote(path, safe='/')}"


def _resolve_mutable(
    registry: WorkdirRegistry, workdir: str, path: str, settings: Settings
) -> ResolvedPath:
    """Resolve a path for mutation: authorize the workdir, then the path.

    Authorization comes first (a read-only workdir rejects every mutation
    regardless of the path), then the same policy gate the read channels
    use, then the workdir root itself is refused: no mutation ever targets
    the workdir root.
    """
    wd = registry.get(workdir)
    if wd is None:
        raise ToolError(f"WORKDIR_NOT_FOUND: {workdir!r} is not a configured workdir")
    if wd.read_only:
        raise ToolError(
            f"WORKDIR_READ_ONLY: {workdir} is configured read-only. "
            "Only workdirs reported as read-write accept mutations."
        )
    resolved = _resolve(registry, workdir, path, settings)
    if not resolved.rel_parts:
        raise ToolError(
            f"ROOT_MUTATION_NOT_ALLOWED: the root of {workdir} cannot be created, edited or deleted"
        )
    return resolved


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


def _mutation_tool_error(exc: Exception, workdir: str, path: str) -> ToolError:
    """Map a mutation failure to a coded ToolError (no internal paths).

    Nothing here renders a host path, a container path, a temp file name or
    any payload: the message is built from the agent's own inputs and a
    fixed phrase, exactly like the read tools.
    """
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, WorkdirBusyError):
        return ToolError(f"WORKDIR_BUSY: {workdir} has an active Agent writer")
    if isinstance(exc, WorkdirRecoveryRequiredError):
        return ToolError(
            f"WORKDIR_RECOVERY_REQUIRED: {workdir} has unresolved Agent recovery state"
        )
    if isinstance(exc, BinaryTransferError):
        return ToolError(f"{exc.code}: {workdir}:{path} — {exc.message}")
    if isinstance(exc, AgentLeaseError):
        return ToolError(f"AGENT_LOCK_UNAVAILABLE: shared Agent lease for {workdir} is unavailable")
    if isinstance(exc, MutationError):
        return ToolError(f"{exc.code}: {workdir}:{path} — {exc.message}")
    if isinstance(exc, PathSecurityError):
        return ToolError(f"{exc.code}: {workdir}:{path} — {exc.message}")
    if isinstance(exc, FileNotFoundError):
        return ToolError(f"PATH_NOT_FOUND: {workdir}:{path} does not exist")
    if isinstance(exc, IsADirectoryError):
        return ToolError(f"NOT_A_FILE: {workdir}:{path} is a directory")
    if isinstance(exc, NotADirectoryError):
        return ToolError(f"NOT_A_DIRECTORY: {workdir}:{path} is not a directory")
    if isinstance(exc, PermissionError):
        return ToolError(f"ACCESS_DENIED: {workdir}:{path} (permission denied)")
    if isinstance(exc, OSError):
        return ToolError(f"MUTATION_IO_ERROR: {workdir}:{path} ({exc.strerror})")
    jsonlog.debug("mutation_unexpected_error", error_type=type(exc).__name__)
    return ToolError(f"MUTATION_IO_ERROR: {workdir}:{path}")


def _run_mutation(tool: str, workdir: str, path: str, t0: float, body):
    """Run one mutation body: exactly one audit event, never a traceback."""
    try:
        result, fields = body()
    except Exception as exc:
        err = _mutation_tool_error(exc, workdir, path)
        _audit(
            tool,
            t0,
            success=False,
            workdir=workdir,
            path=path,
            error_code=_error_code(err),
        )
        raise err from exc
    _audit(tool, t0, success=True, workdir=workdir, path=path, **fields)
    return result


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
    policy = resolved.workdir.policy
    max_lines = min(max_lines, policy.max_read_lines)

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

        # identity is checked on the FD we read, before and after: a file
        # that changes underneath us must not yield a snapshot whose
        # revision does not match the content we are returning
        revision = revision_of(os.fstat(fd))
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
                if len(raw) > policy.max_read_bytes:
                    raise ToolError(
                        f"LINE_TOO_LARGE: {workdir}:{path} line {line_no} exceeds "
                        f"{policy.max_read_bytes} bytes"
                    )
                if len(lines) >= max_lines:
                    has_more = True
                    break
                if bytes_returned + len(raw) > policy.max_read_bytes:
                    has_more = True
                    break
                lines.append(raw)
                bytes_returned += len(raw)
                end_line = line_no

        if revision_of(os.fstat(fd)) != revision:
            raise ToolError(
                f"FILE_CHANGED_DURING_READ: {workdir}:{path} changed while being read; retry"
            )

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
        revision=revision,
    )


# ---- registration ----


def register_tools(
    mcp: MCPServer,
    registry: WorkdirRegistry,
    settings: Settings,
    file_ingress_client: FileIngressClient | None = None,
) -> None:
    """Register core filesystem tools and optional binary transfer tools."""
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

    if any(w.policy.binary_transfer_enabled for w in registry.all_workdirs()):

        @mcp.tool(annotations=ANNOTATIONS)
        def download_binary_file(
            workdir: WorkdirArg,
            path: Annotated[str, Field(description="File path relative to the workdir root")],
        ) -> Annotated[CallToolResult, DownloadBinaryFileMetadata]:
            """Download one regular file through the optional raw-byte channel.

            The selected workdir must explicitly enable binary transfer. The
            file is read from one held descriptor, bounded by that workdir's
            binary transfer limit, and rejected if it changes while being
            read. The MCP result contains a standard binary resource block
            plus structured size/MIME/SHA-256/revision metadata.
            """
            t0 = time.monotonic()
            try:
                resolved = _resolve_binary_read(registry, workdir, path, settings)
                binary = read_binary_file_impl(
                    resolved,
                    max_bytes=resolved.workdir.policy.max_binary_transfer_bytes,
                )
            except ToolError as exc:
                _audit(
                    "download_binary_file",
                    t0,
                    success=False,
                    workdir=workdir,
                    path=path,
                    error_code=_error_code(exc),
                )
                raise
            except BinaryTransferError as exc:
                err = ToolError(f"{exc.code}: {workdir}:{path} — {exc.message}")
                _audit(
                    "download_binary_file",
                    t0,
                    success=False,
                    workdir=workdir,
                    path=path,
                    error_code=exc.code,
                )
                raise err from exc
            except PathSecurityError as exc:
                err = ToolError(f"{exc.code}: {workdir}:{path} — {exc.message}")
                _audit(
                    "download_binary_file",
                    t0,
                    success=False,
                    workdir=workdir,
                    path=path,
                    error_code=exc.code,
                )
                raise err from exc
            except FileNotFoundError as exc:
                err = ToolError(f"PATH_NOT_FOUND: {workdir}:{path} does not exist")
                _audit(
                    "download_binary_file",
                    t0,
                    success=False,
                    workdir=workdir,
                    path=path,
                    error_code="PATH_NOT_FOUND",
                )
                raise err from exc
            except (NotADirectoryError, IsADirectoryError) as exc:
                err = ToolError(f"NOT_A_FILE: {workdir}:{path} is not a regular file")
                _audit(
                    "download_binary_file",
                    t0,
                    success=False,
                    workdir=workdir,
                    path=path,
                    error_code="NOT_A_FILE",
                )
                raise err from exc
            except OSError as exc:
                err = _fs_error(exc, workdir, path)
                _audit(
                    "download_binary_file",
                    t0,
                    success=False,
                    workdir=workdir,
                    path=path,
                    error_code=_error_code(err),
                )
                raise err from exc

            metadata = DownloadBinaryFileMetadata(
                workdir=workdir,
                path=path,
                size=binary.size,
                mime_type=binary.mime_type,
                sha256=binary.sha256,
                revision=binary.revision,
            )
            resource = EmbeddedResource(
                resource=BlobResourceContents(
                    uri=_binary_resource_uri(workdir, path),
                    mime_type=binary.mime_type,
                    blob=base64.b64encode(binary.data).decode("ascii"),
                )
            )
            _audit(
                "download_binary_file",
                t0,
                success=True,
                workdir=workdir,
                path=path,
                bytes_returned=binary.size,
                mime_type=binary.mime_type,
                revision=binary.revision,
            )
            return CallToolResult(
                content=[resource],
                structured_content=metadata.model_dump(),
            )

        upload_meta = {"openai/fileParams": ["file"]} if settings.file_ingress_enabled else None

        @mcp.tool(annotations=CREATE_FILE_ANNOTATIONS, meta=upload_meta)
        def upload_binary_file(
            workdir: WorkdirArg,
            path: Annotated[
                str, Field(description="Path of the new file, relative to the workdir root")
            ],
            data_base64: Annotated[
                str | None,
                Field(
                    default=None,
                    description=(
                        "Optional complete file payload as strict RFC 4648 base64; "
                        "provide exactly one of data_base64 or file"
                    ),
                ),
            ] = None,
            file: Annotated[
                OpenAIFileInput,
                Field(
                    default=None,
                    description=(
                        "Optional ChatGPT/OpenAI file parameter; provide exactly one of "
                        "data_base64 or file"
                    ),
                ),
            ] = None,
            overwrite: Annotated[
                bool,
                Field(
                    default=False,
                    description=(
                        "False creates a new file; true replaces one existing regular file "
                        "and requires expected_revision"
                    ),
                ),
            ] = False,
            expected_revision: Annotated[
                str | None,
                Field(
                    default=None,
                    description=(
                        "Required only when overwrite=true; current revision from stat_file "
                        "or download_binary_file"
                    ),
                ),
            ] = None,
        ) -> UploadBinaryFileResult:
            """Create or revision-guardedly replace one regular file from raw bytes.

            Binary transfer must be enabled for the selected read-write
            workdir. overwrite=false is create-only and never replaces an
            existing path. overwrite=true requires expected_revision and
            atomically replaces exactly the regular file at that revision,
            preserving metadata and rejecting multiple hard links.
            """
            t0 = time.monotonic()

            def body():
                resolved = _resolve_binary_mutable(registry, workdir, path, settings)
                if overwrite and expected_revision is None:
                    raise ToolError(
                        "EXPECTED_REVISION_REQUIRED: overwrite=true requires expected_revision"
                    )
                if not overwrite and expected_revision is not None:
                    raise ToolError(
                        "EXPECTED_REVISION_NOT_ALLOWED: expected_revision is only valid "
                        "when overwrite=true"
                    )
                if data_base64 is None and file is None:
                    raise ToolError(
                        "BINARY_SOURCE_REQUIRED: provide exactly one of data_base64 or file"
                    )
                if data_base64 is not None and file is not None:
                    raise ToolError(
                        "BINARY_SOURCE_CONFLICT: provide exactly one of data_base64 or file"
                    )
                max_bytes = resolved.workdir.policy.max_binary_transfer_bytes
                if file is not None:
                    if not settings.file_ingress_enabled or file_ingress_client is None:
                        raise ToolError(
                            "FILE_INGRESS_DISABLED: ChatGPT file ingress is not enabled"
                        )
                    data = file_ingress_client.fetch(file.download_url, max_bytes=max_bytes)
                else:
                    assert data_base64 is not None
                    data = decode_base64_payload(data_base64, max_bytes=max_bytes)
                with (
                    mutation_lock(),
                    mutation_agent_lease(
                        Path(settings.agent_lock_dir),
                        resolved.workdir.slot,
                        enabled=settings.agent_bridge_enabled,
                    ),
                ):
                    if overwrite:
                        result = replace_binary_file_impl(
                            resolved,
                            data,
                            expected_revision,
                            max_binary_bytes=resolved.workdir.policy.max_binary_transfer_bytes,
                        )
                    else:
                        result = create_binary_file_impl(
                            resolved,
                            data,
                            max_binary_bytes=resolved.workdir.policy.max_binary_transfer_bytes,
                        )
                return result, {
                    "bytes_written": result.bytes_written,
                    "revision": result.revision,
                }

            return _run_mutation("upload_binary_file", workdir, path, t0, body)

    @mcp.tool(annotations=CREATE_FILE_ANNOTATIONS)
    def create_text_file(
        workdir: WorkdirArg,
        path: Annotated[str, Field(description="Path of the file to create, relative to the root")],
        content: Annotated[str, Field(description="Full UTF-8 text content of the new file")],
    ) -> CreateTextFileResult:
        """Create a NEW UTF-8 text file. Never overwrites an existing path.

        Use this only when the target does not exist — inspect it first with
        stat_file or list_directory if it might. If anything already occupies
        the path (file, directory or symlink) the call fails with
        PATH_ALREADY_EXISTS; there is no overwrite or force option. The
        parent directory must already exist. Content is written exactly as
        given (no newline or whitespace normalization) and published
        atomically, so a concurrent reader sees either no file or the whole
        file. Requires a read-write workdir.
        """
        t0 = time.monotonic()

        def body():
            resolved = _resolve_mutable(registry, workdir, path, settings)
            with (
                mutation_lock(),
                mutation_agent_lease(
                    Path(settings.agent_lock_dir),
                    resolved.workdir.slot,
                    enabled=settings.agent_bridge_enabled,
                ),
            ):
                result = create_text_file_impl(
                    resolved,
                    content,
                    max_write_bytes=resolved.workdir.policy.max_write_bytes,
                )
            return result, {"bytes_written": result.bytes_written, "revision": result.revision}

        return _run_mutation("create_text_file", workdir, path, t0, body)

    @mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
    def edit_text_file(
        workdir: WorkdirArg,
        path: Annotated[str, Field(description="Path of the file to edit, relative to the root")],
        expected_revision: Annotated[
            str,
            Field(
                description=(
                    "Revision returned by read_text_file or stat_file for this file; "
                    "the edit is refused if the file changed since"
                )
            ),
        ],
        edits: Annotated[
            list[TextEdit],
            Field(min_length=1, description="Exact-match text edits, applied in order"),
        ],
    ) -> EditTextFileResult:
        """Replace exact text in an existing UTF-8 text file. Never creates one.

        Read the file first (read_text_file or stat_file) and pass the
        revision it returned as expected_revision; if the file has changed
        since, the call fails with REVISION_CONFLICT — re-read the file and
        retry with fresh text. If the path does not exist the call fails with
        PATH_NOT_FOUND: it will not create a file, so a mistyped path cannot
        silently become a new file.

        Each edit replaces every occurrence of exactly `old_text` and
        requires that count to equal `expected_count` (default 1); a mismatch
        fails with EDIT_CONFLICT, so include enough surrounding context to
        make the match unique. All edits in one call are applied in order and
        are all-or-nothing: if one fails, the file is left untouched.
        Deletions and replacements happen in a single atomic step, so no
        reader ever sees a partial file.
        """
        t0 = time.monotonic()

        def body():
            resolved = _resolve_mutable(registry, workdir, path, settings)
            with (
                mutation_lock(),
                mutation_agent_lease(
                    Path(settings.agent_lock_dir),
                    resolved.workdir.slot,
                    enabled=settings.agent_bridge_enabled,
                ),
            ):
                result = edit_text_file_impl(
                    resolved,
                    expected_revision,
                    edits,
                    max_write_bytes=resolved.workdir.policy.max_write_bytes,
                    max_edits_per_call=settings.max_edits_per_call,
                )
            return result, {
                "edit_count": result.edits_applied,
                "bytes_before": result.bytes_before,
                "bytes_after": result.bytes_after,
                "revision": result.revision,
            }

        return _run_mutation("edit_text_file", workdir, path, t0, body)

    @mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
    def delete_file(
        workdir: WorkdirArg,
        path: Annotated[str, Field(description="Path of the file to delete, relative to the root")],
        expected_revision: Annotated[
            str,
            Field(
                description=(
                    "Revision returned by stat_file or read_text_file for this file; "
                    "the delete is refused if the file changed since"
                )
            ),
        ],
    ) -> DeleteFileResult:
        """Permanently delete one regular file.

        Stat or read it first and pass the current revision as
        expected_revision, so a file that changed (or was already replaced)
        is not deleted by mistake. Any regular file can be deleted, binary
        included — only *editing* is limited to UTF-8 text. Directories are
        not accepted here (see delete_directory) and symlinks are never
        followed or removed. The deletion is permanent: there is no trash,
        backup or undo.
        """
        t0 = time.monotonic()

        def body():
            resolved = _resolve_mutable(registry, workdir, path, settings)
            with (
                mutation_lock(),
                mutation_agent_lease(
                    Path(settings.agent_lock_dir),
                    resolved.workdir.slot,
                    enabled=settings.agent_bridge_enabled,
                ),
            ):
                result = delete_file_impl(resolved, expected_revision)
            return result, {
                "bytes_deleted": result.bytes_deleted,
                "revision": result.revision_deleted,
            }

        return _run_mutation("delete_file", workdir, path, t0, body)

    @mcp.tool(annotations=CREATE_DIRECTORY_ANNOTATIONS)
    def create_directory(
        workdir: WorkdirArg,
        path: Annotated[
            str, Field(description="Path of the directory to create, relative to the root")
        ],
    ) -> CreateDirectoryResult:
        """Create ONE new directory. The parent must already exist.

        Not recursive: to create a/b/c, first create a/b, then a/b/c — each
        step is explicit and auditable. Fails with PATH_ALREADY_EXISTS if
        anything already occupies the path (an existing directory included),
        so a repeat call never performs a second change. Requires a
        read-write workdir.
        """
        t0 = time.monotonic()

        def body():
            resolved = _resolve_mutable(registry, workdir, path, settings)
            with (
                mutation_lock(),
                mutation_agent_lease(
                    Path(settings.agent_lock_dir),
                    resolved.workdir.slot,
                    enabled=settings.agent_bridge_enabled,
                ),
            ):
                result = create_directory_impl(resolved)
            return result, {"revision": result.revision}

        return _run_mutation("create_directory", workdir, path, t0, body)

    @mcp.tool(annotations=DESTRUCTIVE_ANNOTATIONS)
    def delete_directory(
        workdir: WorkdirArg,
        path: Annotated[
            str, Field(description="Path of the directory to delete, relative to the root")
        ],
        expected_revision: Annotated[
            str,
            Field(
                description=(
                    "Revision returned by stat_file for this directory; "
                    "the delete is refused if it changed since"
                )
            ),
        ],
    ) -> DeleteDirectoryResult:
        """Permanently delete one EMPTY directory. Never recursive.

        Stat the directory first and pass its revision as expected_revision.
        If the directory holds anything at all — including hidden, denied or
        temporary entries — the call fails with DIRECTORY_NOT_EMPTY; list it
        and delete the contents first, one file at a time. There is no
        recursive or force option, and the deletion is permanent.
        """
        t0 = time.monotonic()

        def body():
            resolved = _resolve_mutable(registry, workdir, path, settings)
            with (
                mutation_lock(),
                mutation_agent_lease(
                    Path(settings.agent_lock_dir),
                    resolved.workdir.slot,
                    enabled=settings.agent_bridge_enabled,
                ),
            ):
                result = delete_directory_impl(resolved, expected_revision)
            return result, {"revision": result.revision_deleted}

        return _run_mutation("delete_directory", workdir, path, t0, body)


def _fs_error(exc: OSError, workdir: str, path: str) -> ToolError:
    """Map an OS-level failure to a coded ToolError (no internal paths)."""
    if isinstance(exc, FileNotFoundError):
        return ToolError(f"PATH_NOT_FOUND: {workdir}:{path} does not exist")
    if isinstance(exc, IsADirectoryError):
        return ToolError(f"NOT_A_FILE: {workdir}:{path} is a directory")
    if isinstance(exc, NotADirectoryError):
        return ToolError(f"NOT_A_DIRECTORY: {workdir}:{path} is not a directory")
    if exc.errno in (errno.EMFILE, errno.ENFILE):
        return ToolError(f"RESOURCE_EXHAUSTED: {workdir}:{path} ({exc.strerror})")
    return ToolError(f"ACCESS_DENIED: {workdir}:{path} ({exc.strerror})")


# keep reference for resource template reuse
READ_IMPL = _read_text_file_impl
