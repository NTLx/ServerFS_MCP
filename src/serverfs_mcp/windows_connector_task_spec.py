"""Fixed separate Windows Task Scheduler identities for v0.25.0 connectors.

These tasks own only one connector subprocess apiece. Neither can start Core
or the Agent Bridge. All configs are private, and installation is disabled.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .windows_component_task import ComponentTask, build_xml, validate_registered

TASK_NAMES = {
    "cloudflare": "ServerFS Cloudflare Connector",
    "openai": "ServerFS OpenAI Connector",
}
SCHEMA = 1
_SID = re.compile(r"S-1-(?:[0-9]+-)+[0-9]+\Z")


class ConnectorTaskError(ValueError):
    """Redacted fixed task-spec refusal."""


@dataclass(frozen=True)
class ConnectorTaskSpec:
    kind: str
    sid: str
    pythonw: Path
    policy_file: Path
    enabled: bool = False

    def validated(self) -> ConnectorTaskSpec:
        if self.kind not in TASK_NAMES:
            raise ConnectorTaskError("Connector component kind is invalid")
        if not isinstance(self.sid, str) or not _SID.fullmatch(self.sid):
            raise ConnectorTaskError("Connector current-user SID is invalid")
        if self.pythonw.name.casefold() != "pythonw.exe":
            raise ConnectorTaskError("Connector task requires pythonw.exe")
        if not self.pythonw.is_absolute() or not self.policy_file.is_absolute():
            raise ConnectorTaskError("Connector service paths must be absolute")
        if type(self.enabled) is not bool:
            raise ConnectorTaskError("Connector task enabled state is invalid")
        return self

    def arguments(self) -> tuple[str, ...]:
        self.validated()
        return (
            "-I",
            "-m",
            "serverfs_mcp.windows_connector_service",
            "_run",
            "--kind",
            self.kind,
            "--policy",
            str(self.policy_file),
        )

    def task(self) -> ComponentTask:
        self.validated()
        return ComponentTask(
            name=TASK_NAMES[self.kind],
            sid=self.sid,
            executable=self.pythonw,
            arguments=self.arguments(),
            enabled=self.enabled,
        )


def build_task_xml(spec: ConnectorTaskSpec, *, boundary: str | None = None) -> bytes:
    return build_xml(spec.validated().task(), boundary=boundary)


def build_manifest(spec: ConnectorTaskSpec, xml: bytes, policy: bytes) -> dict[str, Any]:
    spec.validated()
    return {
        "schema_version": SCHEMA,
        "component": spec.kind + "-connector",
        "task_name": TASK_NAMES[spec.kind],
        "kind": spec.kind,
        "sid": spec.sid,
        "pythonw": str(spec.pythonw),
        "policy_file": str(spec.policy_file),
        "arguments": list(spec.arguments()),
        "policy_sha256": hashlib.sha256(policy).hexdigest(),
        "task_sha256": hashlib.sha256(xml).hexdigest(),
    }


def _manifest_spec(manifest: dict[str, Any]) -> ConnectorTaskSpec:
    return ConnectorTaskSpec(
        kind=manifest["kind"],
        sid=manifest["sid"],
        pythonw=Path(manifest["pythonw"]),
        policy_file=Path(manifest["policy_file"]),
    ).validated()


def validate_manifest(manifest: object) -> tuple[bool, str]:
    if not isinstance(manifest, dict):
        return False, "Connector component manifest is invalid"
    try:
        spec = _manifest_spec(manifest)
        baseline = build_manifest(
            spec,
            build_task_xml(spec, boundary="2000-01-01T00:00:00+00:00"),
            b"",
        )
        if (
            set(manifest) != set(baseline)
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != SCHEMA
        ):
            return False, "Connector component manifest schema is invalid"
        for name in set(baseline) - {"policy_sha256", "task_sha256"}:
            if manifest[name] != baseline[name]:
                return False, "Connector component manifest identity is invalid"
        for name in ("policy_sha256", "task_sha256"):
            value = manifest[name]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                return False, "Connector component digest is invalid"
        return True, ""
    except (TypeError, KeyError, ValueError, OSError):
        return False, "Connector component manifest is invalid"


def validate_authored_task(xml: bytes, manifest: object) -> tuple[bool, str]:
    valid, msg = validate_manifest(manifest)
    if not valid:
        return valid, msg
    assert isinstance(manifest, dict)
    if hashlib.sha256(xml).hexdigest() != manifest["task_sha256"]:
        return False, "Connector authored task XML digest mismatch"
    try:
        return validate_registered(xml.decode("utf-16"), _manifest_spec(manifest).task())
    except UnicodeError:
        return False, "Connector authored task XML is invalid"


def validate_registered_task(xml: str, manifest: object) -> tuple[bool, str]:
    valid, msg = validate_manifest(manifest)
    if not valid:
        return valid, msg
    assert isinstance(manifest, dict)
    return validate_registered(xml, _manifest_spec(manifest).task())
