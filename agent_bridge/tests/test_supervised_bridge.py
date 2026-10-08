"""End-to-end supervised Bridge lifecycle over a real stdin pipe (§15 D4, D7).

These are the only D4 tests that cross a process boundary, because the two properties that matter
cannot be observed in-process:

- the bootstrap frame actually **arrives** through the pipe the supervisor writes, and
- closing that pipe actually **stops** the Bridge, cleanly and without a traceback.

The Windows note is load-bearing rather than incidental. An asyncio reader built on
``loop.connect_read_pipe`` does not work here: on the default Proactor loop a connected pipe
delivers no data and its transport raises ``AttributeError`` on close. The implementation reads
stdin on a worker thread instead, which is the same pipe and the same one-frame contract — §15 D4
forbids moving the *transport* to an environment variable or a config file, and this does not.
The first test below pins that a frame written by a parent process is observed by the child, which
is the regression guard for exactly this.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from serverfs_agent_bridge.bootstrap import encode_bootstrap_frame
from serverfs_agent_bridge.render_config import render_native_bridge_config

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows named-pipe deployment")

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Long enough for the Bridge to load its store, create lease artifacts and bind the pipe.
STARTUP_GRACE_SECONDS = 3.0
SHUTDOWN_TIMEOUT_SECONDS = 25.0


def _fake_runtime_config(workdir: Path):
    """A real rendered config for one workspace-write workdir served by the Fake runtime."""
    return render_native_bridge_config(
        {
            "workdirs": [
                {
                    "alias": "repo",
                    "host_path": str(workdir),
                    "read_only": False,
                    "agent_mode": "workspace-write",
                    "agent_runtimes": ["fake"],
                }
            ],
            "runtimes": [],
            "enable_fake_runtime": True,
        },
        home=workdir / "bridge-home",
    )


def _launch(config_path: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "serverfs_agent_bridge.main",
            "--config",
            str(config_path),
            "--supervised",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
    )


def _finish(process: subprocess.Popen[bytes]) -> tuple[int | None, str]:
    try:
        code = process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
        code = None
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    return code, stderr


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


class TestSupervisedLifecycle:
    """The real process, the real pipe."""

    def test_frame_arrives_over_the_pipe_and_eof_shuts_the_bridge_down(self, workdir: Path) -> None:
        """The whole D4 contract: runtime material in over stdin, graceful stop on stdin EOF."""
        rendered = _fake_runtime_config(workdir)
        process = _launch(rendered.config_path)
        assert process.stdin is not None
        try:
            process.stdin.write(
                encode_bootstrap_frame(None)  # no proxy: the frame shape is what is under test
            )
            process.stdin.flush()

            # Still running after the frame, so the handshake did not merely exit.
            deadline = time.monotonic() + STARTUP_GRACE_SECONDS
            while time.monotonic() < deadline and process.poll() is not None:
                time.sleep(0.05)
            assert process.poll() is None, "the supervised Bridge exited before stdin closed"

            # EOF is the supervisor asking for a graceful stop.
            process.stdin.close()
            code, stderr = _finish(process)
        finally:
            if process.poll() is None:
                process.kill()

        assert code == 0, f"exit={code} stderr={stderr[-1500:]}"
        assert "Traceback" not in stderr, stderr[-1500:]

    def test_bridge_binds_its_pipe_before_stdin_closes(self, workdir: Path) -> None:
        """Readiness is observable state, not a sleep: the endpoint exists while stdin is open."""
        rendered = _fake_runtime_config(workdir)
        process = _launch(rendered.config_path)
        try:
            assert process.stdin is not None
            process.stdin.write(encode_bootstrap_frame(None))
            process.stdin.flush()
            deadline = time.monotonic() + STARTUP_GRACE_SECONDS
            while time.monotonic() < deadline and process.poll() is not None:
                time.sleep(0.05)
            assert process.poll() is None
            # The rendered socket_path is the deterministic SID-derived pipe name; the Bridge is
            # configured to bind it, and an unserved endpoint would fail the readiness probe.
            assert str(rendered.socket_path).startswith("\\\\.\\pipe\\serverfs-agent-bridge-v1-")
            process.stdin.close()
            code, stderr = _finish(process)
            assert code == 0, stderr[-1500:]
        finally:
            if process.poll() is None:
                process.kill()

    def test_eof_before_any_frame_refuses_to_start(self, workdir: Path) -> None:
        """A supervisor that died mid-handshake must not look like a deliberate configuration."""
        rendered = _fake_runtime_config(workdir)
        process = _launch(rendered.config_path)
        assert process.stdin is not None
        process.stdin.close()
        code, stderr = _finish(process)
        assert code == 2, f"exit={code} stderr={stderr[-1500:]}"
        assert "bootstrap" in stderr.lower()

    def test_malformed_frame_refuses_to_start(self, workdir: Path) -> None:
        rendered = _fake_runtime_config(workdir)
        process = _launch(rendered.config_path)
        assert process.stdin is not None
        try:
            process.stdin.write(b"this is not json\n")
            process.stdin.flush()
            process.stdin.close()
        finally:
            pass
        code, stderr = _finish(process)
        assert code == 2, f"exit={code} stderr={stderr[-1500:]}"
        assert "bootstrap" in stderr.lower()

    def test_refusal_never_echoes_the_frame(self, workdir: Path) -> None:
        """Whatever the failure, the frame's contents must not reach stderr."""
        rendered = _fake_runtime_config(workdir)
        process = _launch(rendered.config_path)
        assert process.stdin is not None
        marker = "http://127.0.0.1:19999"
        try:
            process.stdin.write(f'{{"version":1,"bad":1,"url":"{marker}"}}\n'.encode())
            process.stdin.flush()
            process.stdin.close()
        finally:
            pass
        _code, stderr = _finish(process)
        assert "19999" not in stderr, stderr[-1500:]

    def test_an_unsupervised_launch_is_refused_on_windows(self, workdir: Path) -> None:
        """Supervised-only on Windows, observed as a refusal rather than argued from the flag.

        This case used to assert the opposite -- "the v0.10-compatible launch path is untouched" --
        on the premise that an unsupervised Bridge is a supported v0.10 shape. It is not: v0.10
        never starts this process at all, because its launcher runs ``serverfs_mcp.cli serve``
        through the supervisor's non-Agent branch. So the only thing being kept working was a launch
        nobody performs, while the shape it allowed is the one that can leave provider state behind
        that a later supervised start would wrongly claim to have reaped.

        ``_finish`` bounds the exit, so "refused" is distinguished from "hung": a process that
        stayed up would report ``code is None`` here rather than passing.
        """
        rendered = _fake_runtime_config(workdir)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "serverfs_agent_bridge.main",
                "--config",
                str(rendered.config_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_ROOT),
        )
        code, stderr = _finish(process)
        assert code == 2, f"exit={code} stderr={stderr[-1500:]}"
        assert "unsupervised" in stderr.lower(), stderr[-1500:]
        assert "Traceback" not in stderr, stderr[-1500:]
        # The refusal is a message, not a dump: no host path, no config path.
        assert str(rendered.config_path) not in stderr, stderr[-1500:]
