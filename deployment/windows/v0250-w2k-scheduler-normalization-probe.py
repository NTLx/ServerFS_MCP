"""Disposable Win32 Task Scheduler XML normalization probe for W2i.

Does NOT touch the real Core or any legacy/new ServerFS task. Registers one
unique test-only task with a NONEXISTENT executable, disabled logon trigger,
and a time trigger in 2099. Briefly enables its task *definition* without
running an action, compares normalized XML semantics, disables, and deletes
that same temporary task. Refuses an unexpected identity or cleanup drift.

Produces a create-new, secret-free Markdown report under docs/diagnostics.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

_LABELS = frozenset(
    {
        "URI",
        "SID",
        "logon trigger SID",
        "logon token",
        "least privilege",
        "single task instance",
        "non-expiring process",
        "repetition",
        "indefinite",
        "start boundary",
        "logon enabled",
        "timer enabled",
        "repetition continues",
        "restart interval",
        "restart count",
        "executable",
        "arguments",
        "working directory",
        "one action",
        "one Exec",
        "two triggers",
        "one principal",
    }
)


def _mismatches(reason: str, valid: bool) -> frozenset[str]:
    """Consume only the fixed source validator's mismatch field labels."""
    if valid:
        return frozenset()
    prefix = "component task contract mismatch: "
    if not reason.startswith(prefix):
        raise ValueError("task normalization validator reported a non-contract error")
    labels = frozenset(reason[len(prefix) :].split(", "))
    if not labels or not labels <= _LABELS:
        raise ValueError("task normalization validator returned unexpected mismatch labels")
    return labels


def _fixed_task_xml_with_no_active_logon(component_task, build_xml, namespace) -> bytes:
    # TimeTrigger is in 2099; disabling LogonTrigger guarantees that enabling
    # the test task cannot result in a logon-triggered process.
    document = build_xml(component_task, boundary="2099-01-01T00:00:00+00:00")
    root = ET.fromstring(document)
    node = root.find(
        f"./{{{namespace}}}Triggers/{{{namespace}}}LogonTrigger/{{{namespace}}}Enabled"
    )
    if node is None:
        raise ValueError("disposable task's logon trigger cannot be disabled")
    node.text = "false"
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n' + ET.tostring(root, encoding="unicode")
    ).encode("utf-16")


def main() -> int:
    if os.name != "nt" or sys.platform != "win32":
        print("W2K_TASK_XML=FAILED (WindowsRequired)", file=sys.stderr)
        return 2
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo / "src"))
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_component_task import (
        NAMESPACE,
        ComponentTask,
        build_xml,
        validate_registered,
    )
    from serverfs_mcp.windows_service import (
        _decode_process_output,
        _identity_matches_sid,
        _schtasks_executable,
    )

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    task_name = "ServerFS W2K XML Probe " + uuid.uuid4().hex[:16]
    # The test action must not exist. There is NO executable operation to run.
    disposable_dir = repo / "scratchpad" / ("w2k-xml-" + stamp)
    action = disposable_dir / "nonexistent-test-action.exe"
    report = repo / "docs" / "diagnostics" / ("v0250-windows-w2k-scheduler-xml-" + stamp + ".md")
    passed = False
    created = False
    disabled_verified = False
    deleted_verified = False
    enabled_rc = "NOT_ATTEMPTED"
    baseline: frozenset[str] = frozenset()
    after_enable: frozenset[str] = frozenset()
    after_disable: frozenset[str] = frozenset()
    stage = "initial"
    outcome = "UNVERIFIED"
    sid = ""

    def call(*arguments: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(_schtasks_executable()), *arguments],
            capture_output=True,
            check=False,
            timeout=25,
        )

    def read_xml() -> str:
        observed = call("/Query", "/TN", task_name, "/XML", "/HResult")
        if observed.returncode != 0:
            raise RuntimeError("disposable task could not be read")
        xml = _decode_process_output(observed.stdout).lstrip("\ufeff\x00 \t\r\n")
        if not xml:
            raise RuntimeError("disposable task returned no registered XML")
        return xml

    def owned_test_task(xml: str) -> bool:
        try:
            root = ET.fromstring(xml)
            ns = {"t": NAMESPACE}

            def field(query: str) -> str:
                found = root.find(query, ns)
                return "" if found is None or found.text is None else found.text.strip()

            exe = field("./t:Actions/t:Exec/t:Command")
            principal = field("./t:Principals/t:Principal/t:UserId")
            principal_matches = principal.casefold() == sid.casefold() or (
                bool(principal) and _identity_matches_sid(principal, sid)
            )
            return (
                root.tag == f"{{{NAMESPACE}}}Task"
                and field("./t:RegistrationInfo/t:URI") == "\\" + task_name
                and principal_matches
                and field("./t:Actions/t:Exec/t:WorkingDirectory") == str(action.parent)
                and os.path.normcase(exe) == os.path.normcase(str(action))
                and len(root.findall("./t:Actions/t:Exec", ns)) == 1
                and field("./t:Triggers/t:TimeTrigger/t:StartBoundary").startswith("2099-")
                and not action.exists()
            )
        except Exception:
            return False

    try:
        stage = "preflight-test-fixture"
        if action.exists() or disposable_dir.exists() or report.exists():
            raise RuntimeError("test-only output already exists")
        disposable_dir.mkdir()
        sid = current_user_sid()
        synthetic = ComponentTask(
            name=task_name,
            sid=sid,
            executable=action,
            arguments=(),
            enabled=False,
        )
        xml_bytes = _fixed_task_xml_with_no_active_logon(synthetic, build_xml, NAMESPACE)
        xml_file = disposable_dir / "task.xml"
        with xml_file.open("xb") as stream:
            stream.write(xml_bytes)

        stage = "register-disabled-disposable"
        status = call("/Create", "/TN", task_name, "/XML", str(xml_file))
        if status.returncode != 0:
            raise RuntimeError("disposable disabled Task Scheduler registration failed")
        created = True

        stage = "disabled-XML-baseline"
        first = read_xml()
        if not owned_test_task(first):
            raise RuntimeError("disposable task identity cannot be verified")
        valid, reason = validate_registered(first, synthetic)
        baseline = _mismatches(reason, valid)
        if baseline != frozenset({"logon enabled"}):
            raise RuntimeError("disposable task already normalized other security fields")

        stage = "enable-disposable-definition-with-no-live-triggers"
        # No /Run call, time trigger begins in 2099, and logon is disabled.
        status = call("/Change", "/TN", task_name, "/ENABLE")
        enabled_rc = "ZERO" if status.returncode == 0 else "NONZERO"
        if status.returncode != 0:
            raise RuntimeError("disposable Task Scheduler enable was refused")

        stage = "observe-enabled-XML-normalization"
        enabled_xml = read_xml()
        if not owned_test_task(enabled_xml):
            raise RuntimeError("disposable task identity drifted on enable")
        valid, reason = validate_registered(enabled_xml, synthetic)
        after_enable = _mismatches(reason, valid)
        outcome = (
            "ENABLED_XML_ADDED_CONTRACT_MISMATCH"
            if after_enable - baseline
            else "ENABLED_XML_NO_ADDITIONAL_CONTRACT_MISMATCH"
        )
        passed = True

    except Exception as exc:
        outcome = "PROBE_FAILED_" + type(exc).__name__

    finally:
        if created:
            # Only disable/delete the exact unique, non-runnable disposable.
            # Even if the XML normalization is unexpected, never target Core.
            try:
                step = call("/Change", "/TN", task_name, "/DISABLE")
                if step.returncode != 0:
                    raise RuntimeError("disposable task disable refused")
                current = read_xml()
                if not owned_test_task(current):
                    raise RuntimeError("disposable task identity drifted before cleanup")
                valid, reason = validate_registered(current, synthetic)
                after_disable = _mismatches(reason, valid)
                node = ET.fromstring(current).find(
                    f"./{{{NAMESPACE}}}Settings/{{{NAMESPACE}}}Enabled"
                )
                if node is None or node.text != "false":
                    raise RuntimeError("disposable task still enabled after disable")
                disabled_verified = True
                if after_disable != baseline:
                    passed = False
                    outcome = "DISPOSABLE_XML_NOT_RESTORED"
                # Delete even if harmless enabled-state XML normalization
                # persists: exact test-only URI, SID and nonexistent action
                # have been revalidated, and the task is now Disabled.
                step = call("/Delete", "/TN", task_name, "/F")
                if step.returncode != 0:
                    raise RuntimeError("disposable task delete refused")
                deleted_verified = True
                xml_file = disposable_dir / "task.xml"
                xml_file.unlink()
                disposable_dir.rmdir()
            except Exception:
                passed = False
                outcome = "DISPOSABLE_CLEANUP_NOT_VERIFIED"
        if not (disabled_verified and deleted_verified):
            passed = False

        def safe_labels(values: frozenset[str]) -> str:
            return ", ".join(sorted(values)) if values else "none"

        document = [
            "# Windows v0.25.0 — W2K disposable Task Scheduler XML normalization",
            "",
            "UTC: " + datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "Overall: " + ("PASS" if passed else "NOT_ACCEPTED"),
            "Last stage: " + stage,
            "Finding: " + outcome,
            "Test-only task name: " + task_name,
            "Disposable registration created: " + str(created),
            "Disposable enable return: " + enabled_rc,
            "Disposable disabled verified: " + str(disabled_verified),
            "Disposable deleted verified: " + str(deleted_verified),
            "Disabled baseline contract mismatches: " + safe_labels(baseline),
            "Enabled contract mismatches: " + safe_labels(after_enable),
            "Restored-disabled contract mismatches: " + safe_labels(after_disable),
            "",
            "This probe never targets an existing ServerFS component or legacy task.",
            "The test executable path does not exist; LogonTrigger is disabled",
            "and TimeTrigger begins in 2099; /Run is never invoked.",
            "The only scheduled task mutation is a randomly named test fixture.",
            "No Task XML, user path, password, SID, process or credential is logged.",
        ]
        try:
            with report.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(document) + "\n")
            print("W2K disposable Scheduler normalization report: " + str(report))
        except OSError:
            print("W2K_TASK_XML=REPORT_WRITE_FAILED", file=sys.stderr)
            return 2
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
