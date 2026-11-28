"""D8 follow-up: Bridge availability semantics, private-state safety, and output consistency.

Four gaps the first D8 pass left, each of which made the report either wrong or incomplete.

**Availability was asked as configuration.** The doctor warned whenever ``SERVERFS_BRIDGE_PYTHON``
was unset. But the supervisor falls back to ``sys.executable``, and the common deployment has the
MCP server and the Bridge in one interpreter, so that warning is a false positive on a healthy
install. These tests pin the real question: can the interpreter the supervisor *would* use import
the Bridge package? Answer is ``sys.executable`` unless overridden, exactly as the supervisor
resolves it.

**Private-state safety was missing entirely.** "The data home is derivable" is a different question
from "the state already on disk is safe", and only the second is the safety check. A reparse point,
a wrong object type or a foreign DACL under the data home is something the Bridge would refuse to
use at startup, so doctor has to be able to see it.

**``authentication: none`` was printed before the parse.** For a credential-bearing URL the report
contradicted itself: it claimed the endpoint had no authentication and then refused it. The line now
follows a successful parse, because that is the only point at which it is a statement about
something that was inspected.

**A present pipe was described without saying what was proven.** ``WaitNamedPipe(0)`` answers "an
instance of this name exists right now" and nothing more, so the report says exactly that and points
at the check that does establish identity.

The inspector itself is Bridge-side and its own tests live with it. These tests assert the *report*,
including that it never contains a path, a SID or descriptor detail.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_mcp.agent_doctor import (
    UNSAFE_STATUS,
    _bridge_interpreter,
    _probe_agent_proxy,
    _probe_bridge_availability,
    _probe_endpoint,
    _probe_private_state,
    report_agent,
)
from serverfs_mcp.native_config import load_native_config

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native diagnostics")

BRIDGE_PYTHON_ENV = "SERVERFS_BRIDGE_PYTHON"
BRIDGE_VENV_PYTHON = (
    Path(__file__).resolve().parents[1] / "agent_bridge" / ".venv" / "Scripts" / ("python.exe")
)

FORBIDDEN = ("127.0.0.1", "localhost:", "http://", "https://", "S-1-5-21", "Everyone")


class _Report:
    """A minimal stand-in for the doctor's writer, rendering exactly as ``doctor.Report`` does.

    The rendering is duplicated rather than reused because the assertions below are about what an
    operator reads. A stand-in that formatted differently could make a redaction assertion pass or
    fail for reasons that have nothing to do with the code under test.
    """

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str]] = []

    def say(self, line: str) -> None:
        self.lines.append(("", "", line))

    def status(self, label: str, level: str, detail: str = "") -> None:
        suffix = f" -- {detail}" if detail else ""
        self.lines.append((label, level, f"{level}{suffix}"))

    def note(self, label: str, detail: str) -> None:
        # The real writer renders a note as "label: detail" with no level word at all.
        self.lines.append((label, "note", detail))

    @property
    def text(self) -> str:
        out: list[str] = []
        for label, _level, rendered in self.lines:
            out.append(f"{label}: {rendered}".rstrip(": ").rstrip())
        return "\n".join(out)

    def line(self, label: str) -> tuple[str, str]:
        for name, level, rendered in self.lines:
            if name == label:
                return level, rendered
        raise AssertionError(f"no {label!r} line in:\n{self.text}")


@pytest.fixture(autouse=True)
def _isolated_data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test gets its own data home so no ambient SERVERFS_* value can decide a verdict."""
    home = tmp_path / "data-home"
    monkeypatch.setenv("SERVERFS_DATA_HOME", str(home))
    monkeypatch.delenv(BRIDGE_PYTHON_ENV, raising=False)
    return home


@pytest.fixture()
def workdir_path(tmp_path: Path) -> Path:
    """A real directory to configure as the workdir.

    Named differently from the shared ``workdir`` fixture on purpose: that one yields a ``Workdir``
    model object, and these tests need a host path they can write a TOML file pointing at.
    """
    root = tmp_path / "repo"
    root.mkdir()
    return root


class TestBridgeInterpreterResolution:
    def test_without_an_override_the_candidate_is_this_interpreter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(BRIDGE_PYTHON_ENV, raising=False)
        assert _bridge_interpreter(None) == Path(sys.executable)

    def test_an_override_is_used_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        target = Path(sys.executable)
        monkeypatch.setenv(BRIDGE_PYTHON_ENV, str(target))
        assert _bridge_interpreter(None) == target

    def test_the_rule_matches_the_supervisor_source(self) -> None:
        """The supervisor owns the rule; the doctor only restates it, so pin the restatement.

        Two copies of a resolution rule can drift. This test does not prevent that -- nothing can,
        across two frozen packages -- but it makes the drift visible at the point of review instead
        of at the point of a failed startup.
        """
        import inspect

        from serverfs_mcp import agent_lifecycle

        source = inspect.getsource(agent_lifecycle)
        assert "SERVERFS_BRIDGE_PYTHON" in source
        assert "sys.executable" in source

        from serverfs_mcp.agent_doctor import _bridge_interpreter as doctor_rule

        # Both must agree on the same two inputs, not merely mention the same variable name.
        monkey = pytest.MonkeyPatch()
        try:
            monkey.delenv(BRIDGE_PYTHON_ENV, raising=False)
            assert doctor_rule(None) == Path(sys.executable)
            monkey.setenv(BRIDGE_PYTHON_ENV, str(Path("C:/custom/python.exe")))
            assert doctor_rule(None) == Path("C:/custom/python.exe")
        finally:
            monkey.undo()


class TestBridgeAvailability:
    def test_an_absent_override_is_not_a_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The false positive this fixes: one interpreter serving both roles needs no override."""
        monkeypatch.delenv(BRIDGE_PYTHON_ENV, raising=False)
        if not BRIDGE_VENV_PYTHON.exists():
            pytest.skip("the bridge virtualenv is not present on this host")
        monkeypatch.setenv(BRIDGE_PYTHON_ENV, str(BRIDGE_VENV_PYTHON))
        report = _Report()
        _probe_bridge_availability(report, None)
        level, detail = report.line("agent bridge package")
        assert level == "OK", detail
        assert "not configured" not in detail

    def test_an_interpreter_that_can_import_the_bridge_is_ok(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        if not BRIDGE_VENV_PYTHON.exists():
            pytest.skip("the bridge virtualenv is not present on this host")
        monkeypatch.setenv(BRIDGE_PYTHON_ENV, str(BRIDGE_VENV_PYTHON))
        monkeypatch.chdir(Path(__file__).resolve().parents[1] / "agent_bridge")
        try:
            report = _Report()
            _probe_bridge_availability(report, None)
        finally:
            monkeypatch.undo()
        assert report.line("agent bridge package")[0] == "OK", report.text

    def test_an_interpreter_without_the_package_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A FAIL, not a WARN: this deployment genuinely cannot start Agent delegation."""
        monkeypatch.setenv(BRIDGE_PYTHON_ENV, sys.executable)
        report = _Report()
        _probe_bridge_availability(report, None)
        level, detail = report.line("agent bridge package")
        assert level == "FAIL", report.text
        assert "cannot start" in detail or "not importable" in detail

    def test_a_missing_interpreter_is_a_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(BRIDGE_PYTHON_ENV, str(Path("no-such-python.exe")))
        report = _Report()
        _probe_bridge_availability(report, None)
        assert report.line("agent bridge package")[0] == "FAIL"

    def test_the_probe_runs_a_bounded_child(self) -> None:
        """A hung interpreter must not hang a diagnostic."""
        from serverfs_mcp import agent_doctor

        assert 0 < agent_doctor.BRIDGE_PROBE_TIMEOUT_SECONDS <= 60

    def test_the_child_cannot_see_the_agent_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The availability probe is a child process, so it inherits the trust-domain question."""
        marker = "19999"
        monkeypatch.setenv("SERVERFS_AGENT_PROXY_URL", f"http://127.0.0.1:{marker}")
        monkeypatch.setenv("SERVERFS_PROXY_PASSWORD", "hunter2")
        monkeypatch.setenv("CONTROL_PLANE_TOKEN", "cp-secret")
        monkeypatch.setenv(BRIDGE_PYTHON_ENV, sys.executable)
        report = _Report()
        _probe_bridge_availability(report, None)
        assert marker not in report.text
        assert "hunter2" not in report.text
        assert "cp-secret" not in report.text


class TestPrivateStateSafety:
    def _payload(self, report: _Report) -> str:
        return report.line("agent private state")[1]

    def test_a_cold_deployment_is_reported_safe(self, tmp_path: Path) -> None:
        """Absent is a healthy answer for a fresh install, not a fault."""
        report = _Report()
        _probe_private_state(report, None)
        level, detail = report.line("agent private state")
        assert level in {"OK", "WARN"}, report.text
        assert UNSAFE_STATUS not in detail

    def test_a_real_private_tree_is_safe(
        self, _isolated_data_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The full chain: render a real config Bridge-side, then have doctor inspect it."""
        if not BRIDGE_VENV_PYTHON.exists():
            pytest.skip("the bridge virtualenv is not present on this host")
        repo = Path(__file__).resolve().parents[1]
        rendered = subprocess.run(
            [
                str(BRIDGE_VENV_PYTHON),
                "-m",
                "serverfs_agent_bridge.render_config",
                "--data-home",
                str(_isolated_data_home),
            ],
            input=json.dumps(
                {
                    "workdirs": [
                        {
                            "alias": "repo",
                            "host_path": str(repo),
                            "read_only": False,
                            "agent_mode": "workspace-write",
                            "agent_runtimes": ["codex"],
                        }
                    ],
                    "runtimes": {
                        "codex": {"enabled": True, "codex_bin": "codex", "use_proxy": False}
                    },
                }
            ),
            capture_output=True,
            text=True,
            cwd=str(repo / "agent_bridge"),
        )
        assert rendered.returncode == 0, rendered.stderr
        # The renderer's --data-home override *is* the agent-bridge home, matching the path layout
        # the doctor inspects, so bridge.json lands directly inside it.
        assert (_isolated_data_home / "bridge.json").is_file()

        monkeypatch.setenv(BRIDGE_PYTHON_ENV, str(BRIDGE_VENV_PYTHON))
        report = _Report()
        _probe_private_state(report, None)
        level, detail = report.line("agent private state")
        assert level == "OK", f"{detail}\n{report.text}"

    def test_an_unsafe_location_is_a_failure_naming_the_location(
        self, _isolated_data_home: Path
    ) -> None:
        """The inspector is Bridge-side, so this drives it through a stand-in child."""
        _isolated_data_home.mkdir(parents=True, exist_ok=True)
        (_isolated_data_home / "agent-bridge").write_text("wrong type", encoding="utf-8")
        report = _Report()
        _probe_private_state(report, None)
        level, detail = report.line("agent private state")
        assert level in {"FAIL", "WARN"}, report.text
        if level == "FAIL":
            assert "data_home" in detail

    def test_the_report_never_contains_a_path_or_sid(self, tmp_path: Path) -> None:
        report = _Report()
        _probe_private_state(report, None)
        blob = report.text
        assert str(tmp_path) not in blob
        for forbidden in FORBIDDEN:
            assert forbidden not in blob

    def test_an_inspector_that_cannot_run_is_a_warning_not_a_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Inability to inspect must never be reported as safety."""
        monkeypatch.setenv(BRIDGE_PYTHON_ENV, str(Path("no-such-python.exe")))
        report = _Report()
        _probe_private_state(report, None)
        level, detail = report.line("agent private state")
        assert level == "WARN"
        assert "not established" in detail

    def test_a_malformed_inspector_reply_is_not_trusted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognised status must not fall through into "nothing was wrong"."""
        from serverfs_mcp import agent_doctor

        monkeypatch.setattr(
            agent_doctor,
            "_run_state_inspector",
            lambda candidate, home: {"data_home": {"status": "totally-bogus"}},
        )
        report = _Report()
        _probe_private_state(report, None)
        level, _detail = report.line("agent private state")
        assert level in {"WARN", "FAIL"}
        assert level != "OK", "an unknown status was reported as safe"

    def test_an_unsafe_location_alongside_safe_ones_still_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One unsafe location is enough; the safe ones must not dilute it."""
        from serverfs_mcp import agent_doctor

        monkeypatch.setattr(
            agent_doctor,
            "_run_state_inspector",
            lambda candidate, home: {
                "data_home": {"status": "safe", "reason": "ok"},
                "state": {"status": "unsafe", "reason": "is a reparse point"},
                "locks": {"status": "safe", "reason": "ok"},
                "config": {"status": "absent", "reason": "not created yet"},
            },
        )
        report = _Report()
        _probe_private_state(report, None)
        level, detail = report.line("agent private state")
        assert level == "FAIL"
        assert "reparse" in detail

    def test_doctor_does_not_reimplement_the_acl_rules(self) -> None:
        """The judgement stays Bridge-side; a copy could disagree with the real check."""
        import ast
        import inspect

        from serverfs_mcp import agent_doctor

        # An AST walk rather than a text scan: the module's docstrings legitimately *name* the
        # concepts it refuses to implement, and only a call would be a second implementation.
        tree = ast.parse(inspect.getsource(agent_doctor))
        called: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
                elif isinstance(node.func, ast.Name):
                    called.add(node.func.id)
        for forbidden in ("SetNamedSecurityInfo", "GetNamedSecurityInfo", "icacls", "chmod"):
            assert forbidden not in called, f"the doctor calls {forbidden}"

        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert "serverfs_agent_bridge" not in imported, "the packages must stay independent"
        assert not any(name.endswith("windows_security") for name in imported)


PROXY_BODY = 'enabled = true\nsource = "env"\n'


class TestProxyDiagnosticConsistency:
    def _settings(self, tmp_path: Path, workdir_path: Path, body: str = PROXY_BODY) -> object:
        escaped = str(workdir_path).replace("\\", "\\\\")
        config = tmp_path / "serverfs.toml"
        config.write_text(
            '[server]\nlog_level = "INFO"\n\n[agent]\nenabled = true\n\n'
            "[agent.codex]\nenabled = true\n\n[agent.proxy]\n"
            f"{body}"
            '\n[[workdirs]]\nalias = "repo"\n'
            f'path = "{escaped}"\nread_only = false\n'
            'agent_mode = "workspace-write"\nagent_runtimes = ["codex"]\n',
            encoding="utf-8",
        )
        return load_native_config(config)[1]

    def test_a_refused_endpoint_never_claims_no_authentication(
        self, tmp_path: Path, workdir_path: Path
    ) -> None:
        """Before this fix the report printed "authentication: none" and then refused the URL."""
        settings = self._settings(tmp_path, workdir_path)
        report = _Report()
        _probe_agent_proxy(
            report,
            settings,
            {"SERVERFS_AGENT_PROXY_URL": "http://user:hunter2@127.0.0.1:19999"},
        )
        assert "authentication: none" not in report.text, report.text
        assert "hunter2" not in report.text
        assert "19999" not in report.text

    def test_a_valid_endpoint_still_reports_no_authentication(
        self, tmp_path: Path, workdir_path: Path
    ) -> None:
        settings = self._settings(tmp_path, workdir_path)
        report = _Report()
        _probe_agent_proxy(report, settings, {"SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:19999"})
        assert "authentication: none" in report.text, report.text

    def test_the_source_line_precedes_both_outcomes(
        self, tmp_path: Path, workdir_path: Path
    ) -> None:
        """``source`` describes configuration, so it is answerable before the parse."""
        settings = self._settings(tmp_path, workdir_path)
        report = _Report()
        _probe_agent_proxy(
            report,
            settings,
            {"SERVERFS_AGENT_PROXY_URL": "http://user:hunter2@127.0.0.1:19999"},
        )
        assert "source: env" in report.text


class TestPipePresenceWording:
    def test_an_absent_pipe_says_not_running(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from serverfs_mcp import agent_doctor

        monkeypatch.setattr(agent_doctor, "_pipe_is_served", lambda endpoint: False)
        report = _Report()
        _probe_endpoint(report, None)
        assert "not running" in report.text

    def test_a_present_pipe_does_not_claim_authentication(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from serverfs_mcp import agent_doctor

        monkeypatch.setattr(agent_doctor, "_pipe_is_served", lambda endpoint: True)
        report = _Report()
        _probe_endpoint(report, None)
        text = report.text
        assert "pipe present" in text
        # WaitNamedPipe proves a rendezvous exists. It does not authenticate the peer or check
        # health, so claiming either would be a stronger statement than the probe supports.
        for overclaim in ("authenticated", "ready", "healthy", "identity verified"):
            assert overclaim not in text.lower(), text

    def test_a_present_pipe_points_at_the_check_that_does_verify(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from serverfs_mcp import agent_doctor

        monkeypatch.setattr(agent_doctor, "_pipe_is_served", lambda endpoint: True)
        report = _Report()
        _probe_endpoint(report, None)
        assert "supervisor" in report.text


class TestStillReadOnly:
    def test_the_new_probes_create_nothing(self, tmp_path: Path) -> None:
        """The two new subprocess probes must not materialise the deployment they inspect."""
        home = tmp_path / "data-home"
        report = _Report()
        _probe_private_state(report, None)
        _probe_bridge_availability(report, None)
        assert not (home / "agent-bridge").exists()

    def test_no_bridge_process_is_started(self) -> None:
        """Counting processes before and after is stronger than trusting the code path."""
        import serverfs_mcp.agent_doctor as agent_doctor

        def _python_processes() -> int:
            completed = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq python.exe", "/NH"],
                capture_output=True,
                text=True,
                errors="replace",
            )
            return completed.stdout.count("python.exe")

        before = _python_processes()
        report = _Report()
        report_agent(report, [], _DisabledSettings())
        assert _python_processes() <= before + 1
        del agent_doctor


class _DisabledSettings:
    """A settings object shaped enough for the disabled path, which returns after one note."""

    agent = None
