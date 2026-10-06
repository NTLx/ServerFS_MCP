"""Codex CLI diagnostics in ``serverfs doctor`` (Phase E §35).

The D8 doctor reported Agent *configuration* only. Phase E adds the first executable checks: an
operator whose Windows Codex runtime refuses to start needs to know whether the CLI on the machine
is one this runtime can drive, and whether the flags it depends on are still offered.

The properties under test are as much about restraint as about coverage. Every check is read-only
and bounded; nothing starts an ``app-server``, runs a turn, or touches provider state. The
``codex login status`` output is reduced to a fixed vocabulary before it reaches a report line, so a
future CLI that prints an account identifier cannot leak one -- the same reasoning Phase 0F §8
applied to the proxy endpoint.

``codex_bin`` is a single executable, exactly as production configures it, so the cases needing a
particular CLI answer drive the real probe's decision function rather than smuggling arguments into
the configured path. The healthy path is then checked against the actual ``codex`` on this machine,
which is the only way to claim the check works rather than merely that a branch is reachable.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from serverfs_mcp import agent_doctor as module
from serverfs_mcp.agent_doctor import (
    CODEX_APP_SERVER_FLAGS,
    CODEX_AUTH,
    CODEX_CLI,
    CODEX_REQUIRED_FLAGS,
    _probe_runtimes,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native diagnostics")

#: Nothing here may print a host, a port, a path, a SID or an account.
FORBIDDEN = (
    "127.0.0.1",
    "localhost",
    "http://",
    "https://",
    "S-1-5-21",
    "Everyone",
    "acct-",
    "@example.com",
)

_REQUIRED_FLAG_HELP = "".join(f"      {flag} <X>\n" for flag in CODEX_REQUIRED_FLAGS)


class _Report:
    """A stand-in for the doctor's writer, rendering as ``doctor.Report`` does.

    Duplicated rather than reused because the assertions are about what an operator reads; a
    stand-in that formatted differently could make a redaction assertion pass for the wrong reason.
    """

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str]] = []

    def say(self, line: str) -> None:
        self.lines.append(("", "", line))

    def status(self, label: str, level: str, detail: str = "") -> None:
        suffix = f" -- {detail}" if detail else ""
        self.lines.append((label, level, f"{level}{suffix}"))

    def note(self, label: str, detail: str) -> None:
        self.lines.append((label, "note", detail))

    @property
    def text(self) -> str:
        return "\n".join(
            f"{label}: {rendered}".rstrip(": ").rstrip() for label, _lvl, rendered in self.lines
        )

    def line(self, label: str) -> tuple[str, str]:
        for name, level, rendered in self.lines:
            if name == label:
                return level, rendered
        raise AssertionError(f"no {label!r} line in:\n{self.text}")


class _Runtime:
    """The slice of a runtime policy the Codex probe reads."""

    def __init__(self, codex_bin: str, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.use_proxy = False
        self.codex_bin = codex_bin


class _Agent:
    def __init__(self, codex_bin: str) -> None:
        self._runtimes = {
            name: _Runtime(codex_bin if name == "codex" else "unused")
            for name in ("codex", "claude", "qoder")
        }

    def runtime(self, name: str) -> _Runtime:
        return self._runtimes[name]


class _Settings:
    def __init__(self, codex_bin: str) -> None:
        self.agent = _Agent(codex_bin)


def _completed(
    stdout: str = "", returncode: int = 0, stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["codex"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _healthy_cli(
    monkeypatch: pytest.MonkeyPatch, *, login_stream: str = "stderr", login_line: str | None = None
) -> None:
    """Answer the three read-only commands the way a current Codex CLI does.

    ``login_stream`` selects which stream carries the status, because the real CLI uses stderr: a
    case that only ever put it on stdout would let that detail stay wrong. ``login_line`` overrides
    the text so a case can supply output the current CLI does not produce.
    """

    def fake_run(binary: str, *args: str):
        if args == ("--version",):
            return _completed("codex-cli 0.159.2\n")
        if args == ("app-server", "--help"):
            return _completed(f"Usage: codex app-server\n{_REQUIRED_FLAG_HELP}")
        if args == ("login", "status"):
            line = "Logged in using ChatGPT\n" if login_line is None else login_line
            if login_stream == "stderr":
                return _completed(stderr=line)
            return _completed(line)
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(module, "_run_codex", fake_run)


class TestCodexProbeOutcomes:
    """The decision function, driven through the real probe with each CLI answer supplied."""

    def test_a_healthy_cli_reports_version_flags_and_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _healthy_cli(monkeypatch, login_stream="stderr")
        report = _Report()
        _probe_runtimes(report, _Settings("codex"))
        assert report.line(CODEX_CLI)[0] == "OK"
        assert "0.159.2" in report.text
        assert report.line(CODEX_APP_SERVER_FLAGS)[0] == "OK"
        assert report.line(CODEX_AUTH)[0] == "OK"
        assert "ChatGPT" in report.text

    def test_a_cli_missing_the_required_flags_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Protocol drift must be visible in a diagnostic, not as a later opaque startup failure."""
        help_text = "Usage: codex app-server\n      --stdio\n"
        absent = [flag for flag in CODEX_REQUIRED_FLAGS if flag not in help_text]
        assert absent, "the fixture must actually omit a required flag"

        def fake_run(binary: str, *args: str):
            if args == ("--version",):
                return _completed("codex-cli 0.200.0\n")
            if args == ("app-server", "--help"):
                return _completed(help_text)
            return _completed("Logged in using ChatGPT\n")

        monkeypatch.setattr(module, "_run_codex", fake_run)
        report = _Report()
        _probe_runtimes(report, _Settings("codex"))
        level, rendered = report.line(CODEX_APP_SERVER_FLAGS)
        assert level == "FAIL"
        assert "flags" in rendered

    def test_an_account_identifier_in_login_status_never_reaches_the_report(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The auth line is a fixed vocabulary, so provider output cannot leak through it."""
        _healthy_cli(monkeypatch, login_line="Logged in using ChatGPT as acct-7f3d9@example.com\n")
        report = _Report()
        _probe_runtimes(report, _Settings("codex"))
        assert report.line(CODEX_AUTH)[0] == "OK"
        assert "acct-7f3d9@example.com" not in report.text
        assert "@example.com" not in report.text

    def test_the_auth_status_is_read_from_stderr_as_well_as_stdout(self) -> None:
        """Measured on codex-cli 0.159.2: ``login status`` leaves stdout empty.

        Reading stdout alone reported every correctly signed-in deployment as unrecognised, which
        is a false negative of exactly the kind that teaches an operator to ignore the line.
        """
        if shutil.which("codex") is None:
            pytest.skip("no codex CLI on this machine")
        completed = module._run_codex("codex", "login", "status")
        assert completed is not None
        assert completed.stdout.strip() == "", (
            "this CLI now writes the login status to stdout; the probe reads both streams, "
            "so it stays correct either way"
        )
        report = _Report()
        _probe_runtimes(report, _Settings("codex"))
        assert report.line(CODEX_AUTH)[0] == "OK", report.text

    def test_the_report_never_contains_an_endpoint_or_a_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _healthy_cli(monkeypatch, login_stream="stderr")
        report = _Report()
        _probe_runtimes(report, _Settings("codex"))
        for forbidden in FORBIDDEN:
            assert forbidden not in report.text, f"{forbidden!r} leaked into the report"

    def test_a_cli_that_cannot_run_is_a_warning_not_a_crash(self) -> None:
        missing = str(Path("no-such-codex-anywhere") / "codex")
        report = _Report()
        _probe_runtimes(report, _Settings(missing))
        assert report.line(CODEX_CLI)[0] == "WARN"
        # The remaining checks are not attempted once the CLI is known to be unusable.
        assert all(name != CODEX_AUTH for name, _lvl, _r in report.lines)

    def test_a_nonzero_version_exit_is_a_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            module, "_run_codex", lambda binary, *args: _completed("", returncode=2)
        )
        report = _Report()
        _probe_runtimes(report, _Settings("codex"))
        assert report.line(CODEX_CLI)[0] == "WARN"

    def test_a_signed_out_cli_is_a_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def signed_out(binary: str, *args: str):
            if args == ("login", "status"):
                return _completed(stderr="Not logged in\n")
            return _completed("codex-cli 0.159.2\n")

        monkeypatch.setattr(module, "_run_codex", signed_out)
        report = _Report()
        _probe_runtimes(report, _Settings("codex"))
        assert report.line(CODEX_AUTH)[0] == "WARN"

    def test_a_disabled_codex_is_not_probed(self) -> None:
        """A disabled runtime is a normal configuration, so it gets no executable check at all."""
        report = _Report()
        settings = _Settings("codex")
        settings.agent.runtime("codex").enabled = False
        _probe_runtimes(report, settings)
        assert report.line("agent codex") == ("note", "disabled")
        for label in (CODEX_CLI, CODEX_APP_SERVER_FLAGS, CODEX_AUTH):
            assert all(name != label for name, _lvl, _r in report.lines)


class TestCodexProbeRestraint:
    def test_the_child_is_bounded_and_scrubbed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A wedged CLI must be reported rather than waited on, and must not see the endpoint."""
        seen: dict[str, object] = {}
        real_run = subprocess.run

        def capture(argv, **kwargs):
            seen["argv"] = argv
            seen["timeout"] = kwargs.get("timeout")
            seen["env_keys"] = sorted(kwargs.get("env", {}))
            return real_run(argv, **kwargs)

        monkeypatch.setattr(module.subprocess, "run", capture)
        _probe_runtimes(_Report(), _Settings("codex"))
        assert seen["timeout"] == module.CODEX_PROBE_TIMEOUT_SECONDS
        # The scrubbed child cannot see the Agent proxy namespace, so diagnosing the proxy cannot
        # hand its endpoint to the Codex CLI.
        assert not [key for key in seen["env_keys"] if key.startswith("SERVERFS_AGENT_")]

    def test_only_read_only_commands_are_issued(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No login, logout, setup, config mutation or app-server start -- on any branch."""
        issued: list[tuple[str, ...]] = []

        def fake_run(binary: str, *args: str):
            issued.append(args)
            if args == ("--version",):
                return _completed("codex-cli 0.159.2\n")
            if args == ("app-server", "--help"):
                return _completed(f"Usage: codex app-server\n{_REQUIRED_FLAG_HELP}")
            return _completed("Logged in using ChatGPT\n")

        monkeypatch.setattr(module, "_run_codex", fake_run)
        _probe_runtimes(_Report(), _Settings("codex"))
        assert issued == [("--version",), ("app-server", "--help"), ("login", "status")]
        for args in issued:
            joined = " ".join(args)
            # "login status" is a read; a bare "login" would start an interactive flow.
            assert joined != "login"
            for forbidden in ("logout", "setup", "daemon", "--listen"):
                assert forbidden not in joined


class TestAgainstTheRealCli:
    """The healthy path against the actual Codex installed on this machine.

    Everything above proves the decision function is reachable. Only this proves the check works:
    the flags the Windows runtime depends on are confirmed present on the CLI that would run them.
    """

    def test_the_installed_cli_satisfies_the_windows_runtime(self) -> None:
        if shutil.which("codex") is None:
            pytest.skip("no codex CLI on this machine")
        report = _Report()
        _probe_runtimes(report, _Settings("codex"))
        text = report.text
        assert report.line(CODEX_CLI)[0] == "OK", text
        assert report.line(CODEX_APP_SERVER_FLAGS)[0] == "OK", text
        for forbidden in FORBIDDEN:
            assert forbidden not in text, f"{forbidden!r} leaked into the report"

    def test_the_installed_cli_offers_exactly_the_flags_the_runtime_requires(self) -> None:
        """Pins the frozen transport flags against the real CLI, so drift fails here first."""
        if shutil.which("codex") is None:
            pytest.skip("no codex CLI on this machine")
        completed = module._run_codex("codex", "app-server", "--help")
        assert completed is not None
        assert completed.returncode == 0
        for flag in CODEX_REQUIRED_FLAGS:
            assert flag in completed.stdout, (
                f"{flag} is gone from `codex app-server --help`; "
                "the Windows transport is built on it"
            )
