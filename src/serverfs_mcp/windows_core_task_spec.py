"""Pure Windows independent MCP Core Task Scheduler contract (v0.25.0 W1c).

No task registration, child processes, credentials or service mutation.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .windows_component_task import ComponentTask, build_xml, validate_registered
from .windows_core_service import _strict_hostname

TASK_NAME = "ServerFS MCP Core"
SCHEMA = 1
_SID = re.compile(r"S-1-(?:[0-9]+-)+[0-9]+\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class CoreTaskSpecError(ValueError):
    """Fixed task-contract refusal with no private paths or secret values."""


@dataclass(frozen=True)
class CoreTaskSpec:
    sid: str
    pythonw: Path
    config_path: Path
    auth_file: Path
    port: int
    hostname: str
    enabled: bool = False

    def validated(self) -> CoreTaskSpec:
        if not isinstance(self.sid, str) or not _SID.fullmatch(self.sid):
            raise CoreTaskSpecError("Core task SID is invalid")
        if self.pythonw.name.casefold() != "pythonw.exe":
            raise CoreTaskSpecError("Core task interpreter must be pythonw.exe")
        if not all(x.is_absolute() for x in (self.pythonw, self.config_path, self.auth_file)):
            raise CoreTaskSpecError("Core task paths must be absolute")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise CoreTaskSpecError("Core task loopback port is invalid")
        if type(self.enabled) is not bool:
            raise CoreTaskSpecError("Core task enabled state must be boolean")
        try:
            _strict_hostname(self.hostname)
        except Exception as exc:  # noqa: BLE001 - the task contract has fixed errors
            raise CoreTaskSpecError("Core task HTTP Host is invalid") from exc
        return self

    def arguments(self) -> tuple[str, ...]:
        self.validated()
        return (
            "-I",
            "-m",
            "serverfs_mcp.windows_core_service",
            "_run",
            "--config",
            str(self.config_path),
            "--auth-file",
            str(self.auth_file),
            "--port",
            str(self.port),
            "--hostname",
            self.hostname,
        )

    def task(self) -> ComponentTask:
        return ComponentTask(
            name=TASK_NAME,
            sid=self.sid,
            executable=self.pythonw,
            arguments=self.arguments(),
            enabled=self.enabled,
        )


def build_task_xml(spec: CoreTaskSpec, *, boundary: str | None = None) -> bytes:
    return build_xml(spec.validated().task(), boundary=boundary)


def manifest_for(spec: CoreTaskSpec, authored_xml: bytes) -> dict[str, Any]:
    """Only references to private files, never the content of authentication."""
    spec.validated()
    return {
        "schema_version": SCHEMA,
        "component": "mcp-core",
        "task_name": TASK_NAME,
        "sid": spec.sid,
        "pythonw": str(spec.pythonw),
        "config_path": str(spec.config_path),
        "auth_file": str(spec.auth_file),
        "port": spec.port,
        "hostname": spec.hostname,
        "arguments": list(spec.arguments()),
        "xml_sha256": hashlib.sha256(authored_xml).hexdigest(),
    }


def validate_manifest(manifest: object) -> tuple[bool, str]:
    if not isinstance(manifest, dict):
        return False, "Core task manifest is invalid"
    try:
        item = CoreTaskSpec(
            sid=manifest["sid"],
            pythonw=Path(manifest["pythonw"]),
            config_path=Path(manifest["config_path"]),
            auth_file=Path(manifest["auth_file"]),
            port=manifest["port"],
            hostname=manifest["hostname"],
        ).validated()
        expected = manifest_for(item, build_task_xml(item, boundary="2000-01-01T00:00:00+00:00"))
        if (
            set(manifest) != set(expected)
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != SCHEMA
        ):
            return False, "Core task manifest schema is invalid"
        for field in set(expected) - {"xml_sha256"}:
            if manifest[field] != expected[field]:
                return False, "Core task manifest identity mismatch"
        if not isinstance(manifest["xml_sha256"], str) or not _DIGEST.fullmatch(
            manifest["xml_sha256"]
        ):
            return False, "Core task manifest XML digest is invalid"
        return True, ""
    except (ValueError, TypeError, KeyError, CoreTaskSpecError):
        return False, "Core task manifest is invalid"


def _manifest_spec(manifest: dict[str, Any]) -> CoreTaskSpec:
    return CoreTaskSpec(
        sid=manifest["sid"],
        pythonw=Path(manifest["pythonw"]),
        config_path=Path(manifest["config_path"]),
        auth_file=Path(manifest["auth_file"]),
        port=manifest["port"],
        hostname=manifest["hostname"],
    ).validated()


def validate_authored_xml(xml: bytes, manifest: object) -> tuple[bool, str]:
    valid, reason = validate_manifest(manifest)
    if not valid:
        return False, reason
    assert isinstance(manifest, dict)
    if hashlib.sha256(xml).hexdigest() != manifest["xml_sha256"]:
        return False, "Core authored XML digest mismatch"
    try:
        return validate_registered(xml.decode("utf-16"), _manifest_spec(manifest).task())
    except (UnicodeError, ValueError):
        return False, "Core authored XML is invalid"


def validate_registered_xml(xml: str, manifest: object) -> tuple[bool, str]:
    """Task Scheduler can normalize XML, so compare semantics, not source bytes."""
    valid, reason = validate_manifest(manifest)
    if not valid:
        return False, reason
    assert isinstance(manifest, dict)
    return validate_registered(xml, _manifest_spec(manifest).task())
