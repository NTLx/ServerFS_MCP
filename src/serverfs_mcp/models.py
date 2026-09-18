"""Pydantic models for tool inputs and outputs.

Kept in one module so the MCP tool layer can rely on type hints alone:
the SDK generates JSON schema from these models automatically.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WorkdirInfo(BaseModel):
    """One enabled workdir as presented to the agent."""

    alias: str = Field(description="Logical workdir name used in tool calls")
    description: str | None = Field(
        default=None, description="Optional human-provided description of the workdir"
    )
    access: str = Field(
        default="read-only",
        description='"read-only" or "read-write"; mutation tools only work in read-write',
    )


class ListWorkdirsResult(BaseModel):
    """Result of list_workdirs."""

    workdirs: list[WorkdirInfo]


class EntryInfo(BaseModel):
    """One directory entry."""

    name: str = Field(description="Entry name within its parent directory")
    path: str = Field(description="Workdir-relative path of the entry")
    type: str = Field(description='"file", "directory", "symlink" or "other"')
    size: int | None = Field(
        default=None, description="Size in bytes (files only; omitted for others)"
    )
    modified_at: str | None = Field(
        default=None,
        description="Last modification time as RFC 3339 UTC (e.g. 2026-09-17T01:20:30Z)",
    )


class ListDirectoryResult(BaseModel):
    """Result of list_directory."""

    workdir: str
    path: str
    entries: list[EntryInfo]
    offset: int
    limit: int
    returned: int
    has_more: bool


class FileMatch(BaseModel):
    """One find_files match."""

    path: str = Field(description="Workdir-relative path of the matched file")


class FindFilesResult(BaseModel):
    """Result of find_files."""

    matches: list[FileMatch]
    returned: int
    truncated: bool


class TextMatch(BaseModel):
    """One search_text match."""

    path: str = Field(description="Workdir-relative path of the matched file")
    line: int = Field(description="1-based line number of the match")
    text: str = Field(description="Content of the matched line")


class SearchTextResult(BaseModel):
    """Result of search_text."""

    matches: list[TextMatch]
    returned: int
    truncated: bool


class ReadTextFileResult(BaseModel):
    """Result of read_text_file."""

    workdir: str
    path: str
    start_line: int
    end_line: int
    content: str
    bytes_returned: int
    has_more: bool
    next_start_line: int | None = Field(
        default=None, description="Line number to pass as start_line to continue reading"
    )


class StatFileResult(BaseModel):
    """Result of stat_file."""

    workdir: str
    path: str
    type: str = Field(description='"file", "directory", "symlink" or "other"')
    size: int | None = Field(
        default=None, description="Size in bytes (files only; omitted for others)"
    )
    modified_at: str | None = Field(
        default=None, description="Last modification time as RFC 3339 UTC"
    )
    mime_type: str | None = Field(default=None, description="Best-effort MIME type (files only)")
