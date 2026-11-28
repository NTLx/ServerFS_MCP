"""Pinned Windows native regression batch; one invocation, no provider or service work.

Run using the existing Core venv. Only disposable Git worktree/test state is written,
plus a new sanitized diagnostic report. No fetch/install/push or native wheel rebuild.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
INTEGRITY = "tests/test_windows_v026_workdir_integrity.py"
PROXY = "tests/test_windows_v026_named_proxy_live.py"
CORE = (
    "tests/test_windows_v25_runtime_installer.py",
    "tests/test_windows_v25_bridge_v2_public_agent_tools.py",
    "tests/test_windows_v25_core_bridge_binding.py",
    "tests/test_windows_v25_core_bridge_workdir_parity.py",
    "tests/test_windows_v25_private_provision.py",
    "tests/test_windows_v026_named_proxy.py",
    PROXY,
    "tests/test_windows_job.py",
    INTEGRITY,
    "tests/test_agent_tools.py",
    "tests/test_agent_client.py",
    "tests/test_agent_core_grant_client.py",
)
BRIDGE = (
    "tests/test_windows_task_list_native.py",
    "tests/test_windows_pipe_core_v2.py",
    "tests/test_windows_core_grant_runtime_discovery.py",
    "tests/test_claude_adapter.py",
    "tests/test_qoder_adapter.py",
    "tests/test_core_grants_protocol.py",
)
ENV_NAMES = set(
    "SYSTEMROOT WINDIR SYSTEMDRIVE PATH PATHEXT COMSPEC TEMP TMP APPDATA LOCALAPPDATA "
    "USERPROFILE PROGRAMDATA USERNAME USERDOMAIN HOMEDRIVE HOMEPATH "
    "NUMBER_OF_PROCESSORS PROCESSOR_ARCHITECTURE PROCESSOR_ARCHITEW6432 OS".split()
)
WORKER = """
import importlib, sys
from pathlib import Path
source, package, project, bridge_python, proxy_test, *args = sys.argv[1:]
sys.path.insert(0, source)
module = importlib.import_module(package)
assert Path(module.__file__).resolve().is_relative_to(Path(source).resolve())
import pytest
class VerifyCollection:
    def pytest_collection_modifyitems(self, items):
        assert items
        for item in items:
            path = Path(item.path).resolve()
            assert path.is_relative_to(Path(project).resolve())
            if proxy_test and path == Path(project, proxy_test).resolve():
                item.module._BRIDGE_PYTHON = Path(bridge_python)
raise SystemExit(pytest.main(args, plugins=[VerifyCollection()]))
"""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument(
        "--allow-integrity-overlay",
        action="store_true",
        help="Explicitly allow the integrity test overlay for an older diagnostic baseline.",
    )
    args = parser.parse_args()
    assert sys.platform == "win32" and re.fullmatch(r"[0-9a-f]{40}", args.sha)
    assert re.fullmatch(r"v026-windows-[A-Za-z0-9_-]+[.]json", args.report)
    report = ROOT / "docs/diagnostics" / args.report
    python = ROOT / ".venv/Scripts/python.exe"
    bridge_python = ROOT / "agent_bridge/.venv/Scripts/python.exe"
    env = {k: v for k, v in os.environ.items() if k.upper() in ENV_NAMES}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, PYTHONDONTWRITEBYTECODE="1")
    evidence = {
        "source_sha": args.sha,
        "status": "INCOMPLETE",
        "steps": [],
        "real_provider": False,
        "fresh_native_artifact_built": False,
        "existing_dependency_venvs": True,
    }
    with report.open("x", encoding="utf-8") as f:
        json.dump(evidence, f)

    def save():
        pending = report.with_suffix(".pending")
        with pending.open("x", encoding="utf-8") as f:
            json.dump(evidence, f, indent=2, sort_keys=True)
            f.write("\n")
        pending.replace(report)

    def git(cwd, *command):
        return subprocess.check_output(
            ["git", "-c", "core.autocrlf=false", *command],
            cwd=cwd,
            env=env,
            stderr=subprocess.PIPE,
            timeout=90,
            creationflags=FLAGS,
        )

    scratch = wt = original = original_head = None
    added = overlay = timed_out = False
    try:
        original = git(ROOT, "status", "--porcelain", "--untracked-files=no")
        original_head = git(ROOT, "rev-parse", "HEAD")
        assert python.is_file() and bridge_python.is_file()
        assert git(ROOT, "rev-parse", args.sha + "^{commit}").decode().strip() == args.sha
        scratch = Path(tempfile.mkdtemp(prefix="v26-batch-", dir=ROOT))
        wt = scratch / "wt"
        hooks = scratch / "empty-hooks"
        hooks.mkdir()
        git(
            ROOT,
            "-c",
            "core.hooksPath=" + str(hooks),
            "worktree",
            "add",
            "--detach",
            str(wt),
            args.sha,
        )
        added = True
        assert git(wt, "rev-parse", "HEAD").decode().strip() == args.sha
        assert not git(wt, "status", "--porcelain")
        if not (wt / INTEGRITY).exists():
            if not args.allow_integrity_overlay:
                raise RuntimeError("candidate lacks the tracked integrity test")
            shutil.copyfile(ROOT / INTEGRITY, wt / INTEGRITY)
            overlay = True
        evidence["test_overlay"] = (
            {INTEGRITY: sha((wt / INTEGRITY).read_bytes())} if overlay else {}
        )
        evidence["exact_candidate_tests"] = not overlay
        evidence["test_runner_sha256"] = sha(Path(__file__).read_bytes())
        evidence["candidate_test_hashes"] = {
            f"{prefix}{name}": sha((project / name).read_bytes())
            for prefix, project, tests in (
                ("", wt, CORE),
                ("agent_bridge/", wt / "agent_bridge", BRIDGE),
            )
            for name in tests
        }
        for role, project, executable, package, tests in (
            ("core", wt, python, "serverfs_mcp", CORE),
            ("bridge", wt / "agent_bridge", bridge_python, "serverfs_agent_bridge", BRIDGE),
        ):
            assert all((project / name).is_file() for name in tests)
            xml = scratch / (role + ".xml")
            command = [
                str(executable),
                "-I",
                "-B",
                "-c",
                WORKER,
                str(project / "src"),
                package,
                str(project),
                str(bridge_python),
                PROXY if role == "core" else "",
                "-p",
                "no:cacheprovider",
                "-o",
                "addopts=",
                "-q",
                "-rs",
                "--basetemp",
                str(scratch / (role + "-tmp")),
                "--junitxml",
                str(xml),
                *tests,
            ]
            row = {"step": role, "test_files": list(tests)}
            try:
                result = subprocess.run(
                    command,
                    cwd=project,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=900,
                    check=False,
                    creationflags=FLAGS,
                )
                row.update(
                    exit_code=result.returncode, output_sha256=sha(result.stdout + result.stderr)
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                row["status"] = "TIMEOUT"
            if xml.is_file():
                doc = ET.parse(xml).getroot()
                groups = [doc] if doc.tag == "testsuite" else doc.findall("testsuite")
                row["counts"] = {
                    k: sum(int(group.attrib[k]) for group in groups)
                    for k in ("tests", "failures", "errors", "skipped")
                }
                row["skipped_tests"] = [
                    case.attrib.get("name")
                    for case in doc.iter("testcase")
                    if case.find("skipped") is not None
                ]
                row["failed_tests"] = [
                    case.attrib.get("name")
                    for case in doc.iter("testcase")
                    if case.find("failure") is not None or case.find("error") is not None
                ]
                if role == "core":
                    proxy_cases = [
                        case
                        for case in doc.iter("testcase")
                        if "test_named_private_proxy_routes_real_loopback_traffic"
                        in case.attrib.get("name", "")
                    ]
                    row["three_proxy_cases_passed"] = len(proxy_cases) == 3 and all(
                        not list(case)
                        or all(child.tag not in {"failure", "error", "skipped"} for child in case)
                        for case in proxy_cases
                    )
            evidence["steps"].append(row)
            save()
            print(role, row.get("exit_code", row.get("status")), row.get("counts"), flush=True)
            if timed_out:
                break
        evidence["candidate_source_unchanged"] = (
            git(wt, "rev-parse", "HEAD").decode().strip() == args.sha
            and not git(wt, "diff", "--name-only", "HEAD")
            and set(git(wt, "ls-files", "--others", "--exclude-standard").decode().splitlines())
            == ({INTEGRITY} if overlay else set())
        )
        evidence["candidate_tests_unchanged"] = all(
            sha((wt / name).read_bytes()) == digest
            for name, digest in evidence["candidate_test_hashes"].items()
        )
        passed = (
            len(evidence["steps"]) == 2
            and all(
                row.get("exit_code") == 0
                and row.get("counts", {}).get("tests", 0) > row["counts"]["skipped"]
                and not row["counts"]["errors"]
                and not row["counts"]["failures"]
                for row in evidence["steps"]
            )
            and evidence["steps"][0].get("three_proxy_cases_passed") is True
            and evidence["candidate_source_unchanged"]
            and evidence["candidate_tests_unchanged"]
        )
        evidence["status"] = "SCOPED_PASS" if passed else "FAIL"
    except Exception as exc:
        evidence.update(status="FAIL", failure_type=type(exc).__name__)
    finally:
        cleaned = False
        if scratch is not None and not timed_out:
            try:
                if added:
                    if overlay:
                        (wt / INTEGRITY).unlink()
                    git(ROOT, "worktree", "remove", str(wt))
                elif wt.exists():
                    raise RuntimeError("partial worktree creation; preserve for review")
                shutil.rmtree(scratch)
                cleaned = not scratch.exists()
            except Exception:
                evidence["cleanup_blocked"] = True
        evidence["cleanup_verified"] = cleaned
        try:
            evidence["original_tracked_unchanged"] = (
                original_head is not None
                and original_head == git(ROOT, "rev-parse", "HEAD")
                and original == git(ROOT, "status", "--porcelain", "--untracked-files=no")
            )
        except (OSError, subprocess.SubprocessError):
            evidence["original_tracked_unchanged"] = False
        if not cleaned or not evidence["original_tracked_unchanged"]:
            evidence["status"] = "FAIL"
        save()
    return int(evidence["status"] != "SCOPED_PASS")


if __name__ == "__main__":
    raise SystemExit(main())
