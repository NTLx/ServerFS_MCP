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
    binary_transfer: bool = Field(
        default=False,
        description="Whether binary download/upload tools are authorized for this workdir",
    )
    agent_mode: str = Field(
        default="disabled",
        description='Effective Agent policy: "disabled", "review" or "workspace-write"',
    )
    agent_runtimes: list[str] = Field(
        default_factory=list,
        description="Effective native Agent runtimes allowlisted for this workdir",
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
    revision: str = Field(
        description=(
            "Opaque revision of the file content; pass it as expected_revision to "
            "edit_text_file. Stable across pages of the same unchanged file."
        ),
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
    revision: str = Field(
        description=(
            "Opaque revision of this object (metadata included); pass it as "
            "expected_revision to edit_text_file / delete_file / delete_directory."
        ),
    )


class TextEdit(BaseModel):
    """One exact-match edit within edit_text_file."""

    old_text: str = Field(
        description=(
            "Exact text to replace. Empty is allowed only to fill a completely "
            "empty file. Include enough context to make the match unique."
        )
    )
    new_text: str = Field(description="Replacement text")
    expected_count: int = Field(
        default=1, ge=1, description="Number of occurrences old_text must have; else EDIT_CONFLICT"
    )


class CreateTextFileResult(BaseModel):
    """Result of create_text_file."""

    workdir: str
    path: str
    created: bool
    bytes_written: int
    revision: str = Field(description="Revision of the created file")


class EditTextFileResult(BaseModel):
    """Result of edit_text_file."""

    workdir: str
    path: str
    edited: bool
    edits_applied: int
    bytes_before: int
    bytes_after: int
    revision_before: str
    revision: str = Field(description="Revision of the file after the edit")


class DeleteFileResult(BaseModel):
    """Result of delete_file."""

    workdir: str
    path: str
    deleted: bool
    bytes_deleted: int
    revision_deleted: str


class CreateDirectoryResult(BaseModel):
    """Result of create_directory."""

    workdir: str
    path: str
    created: bool
    revision: str = Field(description="Revision of the created directory")


class AgentQuestionAnswer(BaseModel):
    """One normalized answer to a pending Agent question."""

    question_id: str = Field(description="Question identifier from the pending request")
    selected_option_ids: list[str] = Field(
        default_factory=list,
        description="Selected option identifiers; empty when using free text only",
    )
    text: str | None = Field(default=None, description="Optional free-text answer")


class DeleteDirectoryResult(BaseModel):
    """Result of delete_directory."""

    workdir: str
    path: str
    deleted: bool
    revision_deleted: str
