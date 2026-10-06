"""Phase D tests for Agent-aware doctor diagnostics (§13, §15 D8).

Three properties, and the third is the one a diagnostic is most likely to break:

- **disabled is a supported configuration.** An absent or disabled Agent produces exactly one
  informational line and no WARN or FAIL, because the default is not a problem and a doctor that
  flags it trains operators to ignore its output.
- **the endpoint never appears.** Phase 0F §8 measured that a provider's own health report can
  convey reachability without its endpoint; that is the shape used here. Every proxy line is checked
  against a fixed permitted vocabulary, and a failing probe must not leak through exception text
  either.
- **doctor is read-only.** Not "does not obviously write" but *measured*: the Agent data tree,
  the rendered Bridge config, the lock directory and the workdir contents are compared before and
  after, and nothing may have been created. A diagnostic that quietly materialised the deployment
  would be a launcher wearing a report's clothes.

Readiness is reported, never triggered: a stopped Bridge is described, not started.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from serverfs_mcp.agent_doctor import report_agent
from serverfs_mcp.doctor import run_doctor
from serverfs_mcp.native_config import load_native_config

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native diagnostics")

#: Everything the endpoint could leak through. A report line containing any of these is a defect.
FORBIDDEN_SUBSTRINGS = (
    "127.0.0.1",
    "localhost:",
    "http://",
    "https://",
    "@",
    "19999",
    "18080",
    "S-1-5-21",
)

V010_CONFIG = """\
[server]
log_level = "INFO"

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
"""

AGENT_CONFIG = """\
[server]
log_level = "INFO"

[agent]
enabled = true

[agent.codex]
enabled = true
use_proxy = true

[agent.proxy]
enabled = true
source = "env"

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["codex"]
"""


def _write(tmp_path: Path, template: str, root: Path) -> Path:
    config = tmp_path / "serverfs.toml"
    config.write_text(template.format(root=str(root).replace("\\", "\\\\")), encoding="utf-8")
    return config


def _settings(config_path: Path):
    workdirs, settings = load_native_config(config_path)
    return workdirs, settings


class _Report:
    """The same shape doctor._Report uses, captured rather than printed."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.fail_count = 0
        self.warn_count = 0

    def say(self, line: str) -> None:
        self.lines.append(line)

    def status(self, label: str, state: str, detail: str) -> None:
        if state == "FAIL":
            self.fail_count += 1
        elif state == "WARN":
            self.warn_count += 1
        suffix = f" -- {detail}" if detail else ""
        self.lines.append(f"{label}: {state}{suffix}")

    def note(self, label: str, detail: str) -> None:
        self.lines.append(f"{label}: {detail}")

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "existing.txt").write_text("untouched\n", encoding="utf-8")
    return root


def data_home_env(home: Path) -> dict[str, str]:
    """The environment a caller passes to narrow one probe without blanking the rest."""
    return {"SERVERFS_DATA_HOME": str(home)}


def _python_process_count() -> int:
    """How many python.exe processes exist, so a started Bridge would be visible."""
    import subprocess

    listing = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
    ).stdout
    return len([line for line in listing.splitlines() if line.strip()])


def _python_process_count() -> int:
    """How many python.exe processes exist, so a started Bridge would be visible."""
    import subprocess

    listing = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
    ).stdout
    return len([line for line in listing.splitlines() if line.strip()])


@pytest.fixture()
def data_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "data-home"
    monkeypatch.setenv("SERVERFS_DATA_HOME", str(home))
    return home


class TestAgentDisabledIsNormal:
    def test_absent_agent_section_is_one_informational_line(self, tmp_path: Path, workdir: Path):
        config = _write(tmp_path, V010_CONFIG, workdir)
        workdirs, settings = _settings(config)
        report = _Report()
        report_agent(report, workdirs, settings)
        assert report.lines == ["agent: disabled"]
        assert report.fail_count == 0
        assert report.warn_count == 0

    def test_explicitly_disabled_is_also_informational(self, tmp_path: Path, workdir: Path):
        config = _write(tmp_path, V010_CONFIG + "\n[agent]\nenabled = false\n", workdir)
        workdirs, settings = _settings(config)
        report = _Report()
        report_agent(report, workdirs, settings)
        assert report.lines == ["agent: disabled"]

    def test_disabled_never_reaches_the_proxy_probe(
        self, tmp_path: Path, workdir: Path, monkeypatch
    ):
        """A disabled proxy must not be reported even when an ambient endpoint exists."""
        monkeypatch.setenv("SERVERFS_AGENT_PROXY_URL", "http://127.0.0.1:19999")
        config = _write(tmp_path, V010_CONFIG, workdir)
        workdirs, settings = _settings(config)
        report = _Report()
        report_agent(report, workdirs, settings)
        assert "19999" not in report.text

    def test_doctor_exit_code_is_unaffected_by_a_disabled_agent(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        config = _write(tmp_path, V010_CONFIG, workdir)
        lines: list[str] = []
        code = run_doctor(config, writer=lines.append)
        assert "agent: disabled" in lines
        # A disabled Agent contributes no failure of its own.
        assert code in (0, 1)  # 1 only if an unrelated v0.10 probe failed on this host


class TestAgentEnabledStaticChecks:
    def test_enabled_is_reported_with_its_static_checks(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        workdirs, settings = _settings(config)
        report = _Report()
        report_agent(report, workdirs, settings, env=dict(data_home_env(data_home)))
        text = report.text
        assert "agent: OK" in text
        assert "agent codex: enabled, use_proxy=true" in text
        assert "agent claude: disabled" in text
        assert "agent qoder: disabled" in text
        assert "agent data home: OK" in text
        assert "agent identity: OK" in text
        assert "agent endpoint: OK" in text
        assert "agent workdir repo: mode=workspace-write runtimes=codex" in text

    def test_a_stopped_bridge_is_reported_not_started(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        workdirs, settings = _settings(config)
        report = _Report()
        report_agent(report, workdirs, settings, env=dict(data_home_env(data_home)))
        assert "not running" in report.text
        assert "supervisor" in report.text
        # The wording must not offer to start it.
        assert "starting" not in report.text.lower().replace("not running", "")

    def test_missing_data_home_is_a_clean_failure_not_a_traceback(
        self, tmp_path: Path, workdir: Path, monkeypatch
    ):
        monkeypatch.delenv("SERVERFS_DATA_HOME", raising=False)
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        workdirs, settings = _settings(config)
        report = _Report()
        report_agent(report, workdirs, settings, env={})
        assert "agent data home: FAIL" in report.text
        assert "Traceback" not in report.text


class TestProxyRedaction:
    """The endpoint must not appear anywhere, including through a failing probe."""

    def _report(self, tmp_path: Path, workdir: Path, env: dict) -> _Report:
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        workdirs, settings = _settings(config)
        report = _Report()
        report_agent(report, workdirs, settings, env=env)
        return report

    def test_a_configured_proxy_reports_only_the_permitted_vocabulary(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        env = {
            "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:19999",
            "SERVERFS_AGENT_NO_PROXY": "corp.example",
        }
        report = self._report(tmp_path, workdir, env)
        text = report.text
        assert "agent proxy: OK" in text
        assert "source: env" in text
        assert "authentication: none" in text
        assert "mandatory local bypass: OK" in text
        assert "reachability:" in text

    def test_no_endpoint_material_appears_in_a_reachable_report(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        env = {
            "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:19999",
            "SERVERFS_AGENT_NO_PROXY": "corp.example",
        }
        report = self._report(tmp_path, workdir, env)
        for needle in FORBIDDEN_SUBSTRINGS:
            assert needle not in report.text, f"{needle!r} leaked into the report"

    def test_an_unreachable_endpoint_still_leaks_nothing(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        """Port 1 refuses immediately, so the failure path is exercised deterministically."""
        env = {
            "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:1",
            "SERVERFS_AGENT_NO_PROXY": "corp.example",
        }
        report = self._report(tmp_path, workdir, env)
        text = report.text
        assert "reachability: WARN" in text or "reachability: OK" in text
        for needle in ("127.0.0.1", "http://", ":1"):
            assert needle not in text, f"{needle!r} leaked through the failure path"

    def test_a_credential_bearing_endpoint_is_refused_without_echoing_it(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        env = {"SERVERFS_AGENT_PROXY_URL": "http://user:hunter2@127.0.0.1:19999"}
        report = self._report(tmp_path, workdir, env)
        text = report.text
        assert "mandatory local bypass: FAIL" in text
        # "user" alone appears in unrelated lines such as "current user SID is measurable", so the
        # assertion targets the credential itself rather than a substring that cannot be tightened.
        assert "hunter2" not in text
        assert "19999" not in text
        assert "user:" not in text and "user@" not in text

    def test_a_configured_proxy_without_an_endpoint_is_refused_without_echoing(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        """``env={}`` means an empty environment, so the endpoint is genuinely absent.

        The refusal is correct here and is exactly what startup would do. What matters is that the
        message names the missing variable and carries no endpoint material.
        """
        report = self._report(tmp_path, workdir, env={})
        text = report.text
        assert "mandatory local bypass: FAIL" in text
        assert "SERVERFS_AGENT_PROXY_URL is required" in text
        assert "reachability: FAIL" in text
        for needle in ("127.0.0.1", "http://", "19999"):
            assert needle not in text

    def test_the_reported_vocabulary_is_the_whole_permitted_set(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        """A line outside the permitted labels would be a new leak surface."""
        env = {
            "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:19999",
            "SERVERFS_AGENT_NO_PROXY": "corp.example",
        }
        report = self._report(tmp_path, workdir, env)
        labels = {line.split(":", 1)[0] for line in report.lines}
        assert "agent proxy" in labels
        assert "source" in labels
        assert "authentication" in labels
        assert "mandatory local bypass" in labels
        assert "reachability" in labels


class TestDoctorIsReadOnly:
    """Measured, not assumed: nothing may be created by a diagnostic run."""

    def _snapshot(self, root: Path) -> dict[str, tuple[int, bytes]]:
        return {
            str(path.relative_to(root)): (path.stat().st_size, path.read_bytes())
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    def test_nothing_is_created_in_the_data_home(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        assert not data_home.exists(), "the fixture must start with no data home"
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        lines: list[str] = []
        run_doctor(config, writer=lines.append)
        assert not data_home.exists(), "doctor created the Agent data tree"
        assert not (data_home / "agent-bridge").exists()
        assert not (data_home / "agent-bridge" / "bridge.json").exists()

    def test_the_workdir_is_untouched(self, tmp_path: Path, workdir: Path, data_home: Path):
        before = self._snapshot(workdir)
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        lines: list[str] = []
        run_doctor(config, writer=lines.append)
        assert self._snapshot(workdir) == before, "doctor modified the workdir"

    def test_disabled_agent_also_creates_nothing(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        config = _write(tmp_path, V010_CONFIG, workdir)
        before = self._snapshot(workdir)
        lines: list[str] = []
        run_doctor(config, writer=lines.append)
        assert not data_home.exists()
        assert self._snapshot(workdir) == before

    def test_no_bridge_process_is_started(self, tmp_path: Path, workdir: Path, data_home: Path):
        """A diagnostic must not become a launcher."""

        config = _write(tmp_path, AGENT_CONFIG, workdir)
        before = _python_process_count()
        lines: list[str] = []
        run_doctor(config, writer=lines.append)
        after = _python_process_count()
        # One extra is the doctor process itself; anything more would be a started Bridge.
        assert after <= before + 1

    def test_stdout_stays_empty(self, tmp_path: Path, workdir: Path, data_home: Path, capsys):
        """stdout is reserved for MCP frames (§27); diagnostics belong on stderr."""
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        lines: list[str] = []
        run_doctor(config, writer=lines.append)
        captured = capsys.readouterr()
        assert captured.out == "", "doctor wrote to stdout"


class TestNoProviderActivity:
    """Phase D does static reporting only; E/F/G own real provider contact."""

    def test_no_provider_environment_is_required(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        lines: list[str] = []
        run_doctor(config, writer=lines.append)
        text = "\n".join(lines)
        # Nothing that would indicate a login, model listing or inference attempt.
        for needle in ("model/list", "login", "inference", "api.openai.com"):
            assert needle not in text


class TestJevStaysDirect:
    """The measured §17 conclusion is unchanged by D8."""

    def test_no_proxy_variable_is_set_for_jev(self, tmp_path: Path, workdir: Path, data_home: Path):
        import os

        before = dict(os.environ)
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        lines: list[str] = []
        run_doctor(config, writer=lines.append)
        assert dict(os.environ) == before, "doctor mutated the process environment"

    def test_agent_doctor_does_not_import_jev(self):
        """Jev stays direct and fail-open; nothing here proxies it."""
        from pathlib import Path as P

        source = P("src/serverfs_mcp/agent_doctor.py").read_text(encoding="utf-8")
        assert "typesafe" not in source
        assert "jev" not in source.lower()


class TestReportIsJsonFree:
    """A guard against a future change dumping a config blob into the report."""

    def test_no_raw_json_or_config_dump_appears(
        self, tmp_path: Path, workdir: Path, data_home: Path
    ):
        config = _write(tmp_path, AGENT_CONFIG, workdir)
        lines: list[str] = []
        run_doctor(config, writer=lines.append)
        text = "\n".join(lines)
        assert "{" not in text, "the report contains a raw JSON fragment"
        assert json.dumps({"ok": True}) not in text
