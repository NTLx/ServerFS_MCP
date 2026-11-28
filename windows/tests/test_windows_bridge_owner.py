"""W1a tests: the independent Agent Bridge owner, in isolation (v0.25.0 §W1a).

The owner is the piece that must eventually run without a ``tunnel-client`` parent, so what these
tests have to establish is not "a Bridge starts" but the *ownership order* that makes starting one
safe:

- the per-user lease is taken before anything is created, and a second owner is refused rather than
  attaching to the first one's Bridge;
- the containment fact (``prior_bridge_execution_stopped``) is only stated after this process has
  both the lease and a *freshly created* named job — a job name that is already taken must produce a
  refusal, never a claim;
- every failure path still closes the job and releases the lease, because a partially-started Agent
  service would hold a writer lease nobody owns;
- the owner starts exactly one process and it is the Bridge: no stdio MCP child, no HTTP Core, no
  Connector.

Two of these use real kernel objects on purpose — a real ``LifecycleLease`` over a temporary root
and a real anonymous ``WindowsJob`` — so the ordering is measured rather than mocked. Neither ever
touches the production lease artifact or the production job name; the autouse fixture below makes
that a hard failure rather than a convention.

What is **not** covered here, and must not be claimed: the production named job cannot be created in
a test (its name is derived from the live identity and the running supervisor owns it), so
"a second owner is refused by the *job* name" is exercised with a fake that raises, and the real
named-object singleton proof belongs to the W2 fixture.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from serverfs_mcp import windows_bridge_owner as owner
from serverfs_mcp.agent_lifecycle import AgentLifecycleError
from serverfs_mcp.native_lifecycle import LifecycleLease, LifecycleOwnershipError
from serverfs_mcp.windows_job import JobObjectError, WindowsJob

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native lifecycle")

AGENT_CONFIG = """\
[server]
log_level = "INFO"

[agent]
enabled = true

[agent.codex]
enabled = true

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
agent_mode = "workspace-write"
agent_runtimes = ["codex"]
"""

DISABLED_CONFIG = """\
[server]
log_level = "INFO"

[[workdirs]]
alias = "repo"
path = "{root}"
read_only = false
"""

#: Dummy values only: markers that must never reach the Bridge environment.
POLLUTION = {
    "SERVERFS_AGENT_PROXY_URL": "http://127.0.0.1:19999",
    "SERVERFS_AGENT_NO_PROXY": "marker-no-proxy",
    "HTTP_PROXY": "http://127.0.0.1:19080",
    "CONTROL_PLANE_FAKE_SECRET": "marker-control-plane",
}


@pytest.fixture(autouse=True)
def forbid_the_production_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may take the real per-user lease or the production job name.

    The owner's dependency defaults resolve these through ``default_factory``, so patching the
    module attributes here is enough to make a test that forgot to inject an isolated lifecycle fail
    loudly instead of starting a real Bridge under the operator's identity.
    """

    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a W1a test tried to touch the production lifecycle")

    monkeypatch.setattr(owner, "_default_lease", _refuse)
    monkeypatch.setattr(owner, "_default_job", _refuse)


class _Recorder:
    """The order things happened in, which is most of what this task is about."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def add(self, name: str) -> None:
        self.events.append(name)

    def index(self, name: str) -> int:
        assert name in self.events, f"{name!r} never happened: {self.events}"
        return self.events.index(name)


class _FakeStdin:
    def __init__(self, recorder: _Recorder, error: Exception | None) -> None:
        self.recorder = recorder
        self.error = error
        self.payload = b""

    def write(self, data: bytes) -> int:
        if self.error is not None:
            raise self.error
        self.recorder.add("bootstrap")
        self.payload += data
        return len(data)

    def flush(self) -> None:
        return None


class _FakeProcess:
    """A Bridge stand-in: a lifecycle pipe and a scripted ``poll()``."""

    def __init__(
        self,
        recorder: _Recorder,
        *,
        write_error: Exception | None = None,
        exit_codes: tuple[int, ...] = (),
    ) -> None:
        self.recorder = recorder
        self.stdin = _FakeStdin(recorder, write_error)
        self._codes = list(exit_codes)

    def poll(self) -> int | None:
        return self._codes.pop(0) if self._codes else None


class _FakeLease:
    def __init__(self, recorder: _Recorder) -> None:
        self.recorder = recorder
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.recorder.add("lease.close")


class _FakeJob:
    def __init__(
        self,
        recorder: _Recorder,
        *,
        open_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.recorder = recorder
        self.open_error = open_error
        self.close_error = close_error
        self.opened = False
        self.closed = False

    def open(self) -> None:
        self.recorder.add("job.open")
        if self.open_error is not None:
            raise self.open_error
        self.opened = True

    def close(self) -> None:
        self.recorder.add("job.close")
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _Harness:
    """Builds the dependency set for one test, with the knobs that test needs.

    Every seam is replaced by default, so no test can reach the production lifecycle or start a real
    process by accident. ``use_real_*`` opts a single test back into the real kernel objects.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.recorder = _Recorder()
        self.lease_error: Exception | None = None
        self.job_error: Exception | None = None
        self.spawn_error: Exception | None = None
        self.write_error: Exception | None = None
        self.ready_error: Exception | None = None
        self.render_request_error: Exception | None = None
        self.environ: dict[str, str] = {}
        self.exit_codes: tuple[int, ...] = ()
        self.graceful = True
        self.stop_after: float | None = None
        self.use_real_lease = False
        self.use_real_job = False
        self.use_real_spawn_and_shutdown = False
        self.captured: dict[str, object] = {}

        self.lease: _FakeLease | None = None
        self.job: _FakeJob | None = None
        self.process: _FakeProcess | None = None
        self.real_job: WindowsJob | None = None
        self.real_process: subprocess.Popen[bytes] | None = None
        self.config_path = tmp_path / "serverfs.toml"
        self._deps_override: owner.OwnerDependencies | None = None

    # -- seams ---------------------------------------------------------
    def _acquire(self) -> object:
        if self.use_real_lease:
            self.recorder.add("lease.acquire")
            return LifecycleLease(root=self.tmp_path / "lifecycle").acquire()
        self.recorder.add("lease.acquire")
        if self.lease_error is not None:
            raise self.lease_error
        self.lease = _FakeLease(self.recorder)
        return self.lease

    def _make_job(self) -> object:
        if self.use_real_job:
            self.real_job = WindowsJob()
            return self.real_job
        self.job = _FakeJob(self.recorder, open_error=self.job_error)
        return self.job

    def _load(self, path: Path) -> object:
        return (object(), SimpleNamespace(agent=SimpleNamespace(enabled=True)))

    def _render_request(self, _workdirs: object, _settings: object) -> dict:
        if self.render_request_error is not None:
            raise self.render_request_error
        return {"workdirs": [{"alias": "repo"}]}

    def _render_config(self, request: dict) -> object:
        self.captured["request"] = request
        return SimpleNamespace(
            config_path=self.tmp_path / "bridge.json",
            socket_path=self.tmp_path / "unused.pipe",
        )

    def _parse_proxy(self, _agent: object, _environ: object) -> None:
        return None

    def _build_env(self, _proxy: object, source: object) -> dict[str, str]:
        if self.use_real_spawn_and_shutdown:
            return {"PATH": str(Path(sys.executable).parent)}
        return {"PATH": "p"}

    def _spawn(self, **kwargs: object) -> object:
        self.recorder.add("spawn")
        self.captured["spawn_kwargs"] = kwargs
        if self.spawn_error is not None:
            raise self.spawn_error
        if self.use_real_spawn_and_shutdown:
            self.real_process = subprocess.Popen(  # noqa: PLW2901 - owned by the harness fixture
                [sys.executable, "-c", "import sys; sys.stdin.read()"],
                stdin=subprocess.PIPE,
            )
            job = kwargs.get("job")
            if job is not None:
                job.assign(self.real_process)
            return self.real_process
        self.process = _FakeProcess(
            self.recorder, write_error=self.write_error, exit_codes=self.exit_codes
        )
        return self.process

    def _await_ready(self, _socket_path: Path, _process: object) -> None:
        self.recorder.add("ready")
        if self.ready_error is not None:
            raise self.ready_error

    def _await_ready_bound(self, socket_path: Path, process: object, job: object) -> None:
        self.captured["bound_job"] = job
        self._await_ready(socket_path, process)

    def _request_stop(self, _process: object) -> None:
        self.recorder.add("stop")
        if self.use_real_spawn_and_shutdown and self.real_process is not None:
            assert self.real_process.stdin is not None
            self.real_process.stdin.close()

    def _await_exit(self, _process: object) -> bool:
        self.recorder.add("exit")
        if self.use_real_spawn_and_shutdown and self.real_process is not None:
            self.real_process.wait(timeout=15)
            return True
        return self.graceful

    @property
    def deps(self) -> owner.OwnerDependencies:
        if self._deps_override is not None:
            return self._deps_override
        return owner.OwnerDependencies(
            acquire_lease=self._acquire,
            make_job=self._make_job,
            load_config=self._load,
            render_request=self._render_request,
            render_config=self._render_config,
            parse_proxy=self._parse_proxy,
            build_env=self._build_env,
            bridge_argv=lambda path: [
                sys.executable,
                "-m",
                "serverfs_agent_bridge.main",
                "--config",
                str(path),
                "--supervised",
            ],
            spawn=self._spawn,
            await_ready=self._await_ready,
            await_ready_bound=self._await_ready_bound,
            request_stop=self._request_stop,
            await_exit=self._await_exit,
        )

    @deps.setter
    def deps(self, value: owner.OwnerDependencies) -> None:
        self._deps_override = value

    def run(self, **kwargs: object) -> int:
        stop = threading.Event()
        timer: threading.Timer | None = None
        if self.stop_after is not None:
            timer = threading.Timer(self.stop_after, stop.set)
            timer.start()
        try:
            return owner.run(
                self.config_path,
                environ=self.environ or None,
                stop_event=stop,
                poll_interval=0.01,
                deps=self.deps,
                **kwargs,
            )
        finally:
            if timer is not None:
                timer.cancel()


class TestOwnershipOrder:
    """The order is the safety property, so it is asserted directly."""

    def test_lease_then_job_then_bridge_then_bootstrap(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.stop_after = 0.01
        assert harness.run() == owner.EXIT_OK
        assert harness.recorder.events[:5] == [
            "lease.acquire",
            "job.open",
            "spawn",
            "bootstrap",
            "ready",
        ]

    def test_the_containment_frame_is_only_written_after_a_fresh_job_exists(
        self, tmp_path: Path
    ) -> None:
        harness = _Harness(tmp_path)
        harness.stop_after = 0.01
        assert harness.run() == owner.EXIT_OK
        assert harness.recorder.index("job.open") < harness.recorder.index("spawn")
        assert harness.recorder.index("spawn") < harness.recorder.index("bootstrap")

    def test_the_frame_states_prior_execution_stopped(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.stop_after = 0.01
        assert harness.run() == owner.EXIT_OK
        assert harness.process is not None
        assert b'"prior_bridge_execution_stopped":true' in harness.process.stdin.payload

    def test_a_busy_lease_is_refused_before_anything_is_created(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.lease_error = LifecycleOwnershipError(
            "another Agent-enabled supervisor of this user already owns the lifecycle"
        )
        assert harness.run() == owner.EXIT_REFUSED
        assert harness.recorder.events == ["lease.acquire"]

    def test_a_taken_job_name_is_refused_without_spawning_or_claiming(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.job_error = JobObjectError("the containment job already exists")
        assert harness.run() == owner.EXIT_REFUSED
        assert "spawn" not in harness.recorder.events
        assert "bootstrap" not in harness.recorder.events
        assert harness.recorder.events[-1] == "lease.close"
        assert harness.job is not None and harness.job.closed

    def test_a_real_isolated_lease_refuses_a_second_owner(self, tmp_path: Path) -> None:
        """The lease is a real exclusive lock, so a second owner is refused by the OS."""
        held = LifecycleLease(root=tmp_path / "lifecycle").acquire()
        try:
            harness = _Harness(tmp_path)
            # Default lease path is patched to refuse, so point at the same real root explicitly.
            harness.deps = dataclasses.replace(
                harness.deps,
                acquire_lease=lambda: LifecycleLease(root=tmp_path / "lifecycle").acquire(),
            )
            assert harness.run() == owner.EXIT_REFUSED
            assert "job.open" not in harness.recorder.events
            assert "spawn" not in harness.recorder.events
        finally:
            held.close()


def test_installed_owner_uses_the_pinned_renderer_and_bridge_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path)
    harness.stop_after = 0.01
    harness.environ = {"SERVERFS_BRIDGE_PYTHON": "C:/untrusted/python.exe"}
    pinned = tmp_path / "private-bridge-env" / "python.exe"
    observed: list[Path] = []

    def render(request: dict, bridge_python: Path) -> object:
        observed.append(bridge_python)
        return harness._render_config(request)

    monkeypatch.setattr(owner, "_render_config_pinned", render)
    assert harness.run(bridge_python=pinned) == owner.EXIT_OK
    assert observed == [pinned]
    assert harness.captured["bound_job"] is harness.job
    argv = harness.captured["spawn_kwargs"]["argv"]
    assert argv[0:4] == [str(pinned), "-I", "-B", "-m"]
    assert "C:/untrusted/python.exe" not in argv
    assert harness.recorder.events[-2:] == ["job.close", "lease.close"]


class TestStartupFailuresReleaseEverything:
    """A partially started Agent service would hold a writer lease nobody owns."""

    def test_policy_refusal_creates_no_job(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        harness = _Harness(tmp_path)
        harness.render_request_error = AgentLifecycleError(
            "the native configuration does not require the Agent Bridge"
        )
        assert harness.run() == owner.EXIT_REFUSED
        assert "job.open" not in harness.recorder.events
        assert harness.recorder.events[-1] == "lease.close"
        assert "does not require the Agent Bridge" in capsys.readouterr().err

    def test_spawn_failure_closes_the_job_and_releases_the_lease(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.spawn_error = AgentLifecycleError(
            "the Agent Bridge could not be started (FileNotFoundError)"
        )
        assert harness.run() == owner.EXIT_REFUSED
        # The spawn was attempted and refused; what must not happen is a containment claim.
        assert "bootstrap" not in harness.recorder.events
        assert harness.recorder.events[-2:] == ["job.close", "lease.close"]
        assert harness.job is not None and harness.job.closed

    def test_bootstrap_write_failure_still_closes_the_job(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.write_error = OSError("pipe closed")
        assert harness.run() == owner.EXIT_REFUSED
        assert "ready" not in harness.recorder.events
        assert harness.recorder.events[-1] == "lease.close"
        assert harness.job is not None and harness.job.closed

    def test_readiness_failure_still_closes_the_job(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.ready_error = AgentLifecycleError(
            "the Agent Bridge did not become ready within 30s"
        )
        assert harness.run() == owner.EXIT_REFUSED
        assert harness.job is not None and harness.job.closed
        assert harness.lease is not None and harness.lease.closed

    def test_missing_lifecycle_pipe_refuses_before_readiness(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        deps = harness.deps

        def spawn_without_stdin(**kwargs: object) -> object:
            process = deps.spawn(**kwargs)
            process.stdin = None
            return process

        harness.deps = dataclasses.replace(deps, spawn=spawn_without_stdin)
        assert harness.run() == owner.EXIT_REFUSED
        assert "bootstrap" not in harness.recorder.events
        assert "ready" not in harness.recorder.events
        assert harness.recorder.events[-2:] == ["job.close", "lease.close"]

    def test_partial_bootstrap_write_refuses_before_readiness(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        deps = harness.deps

        class ShortWriter:
            def write(self, payload: bytes) -> int:
                return len(payload) - 1

            def flush(self) -> None:
                raise AssertionError("partial bootstrap must not be flushed")

        def spawn_with_partial_pipe(**kwargs: object) -> object:
            process = deps.spawn(**kwargs)
            process.stdin = ShortWriter()
            return process

        harness.deps = dataclasses.replace(deps, spawn=spawn_with_partial_pipe)
        assert harness.run() == owner.EXIT_REFUSED
        assert "ready" not in harness.recorder.events
        assert harness.recorder.events[-2:] == ["job.close", "lease.close"]

    def test_job_factory_failure_still_releases_lease(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)

        def unavailable_job() -> object:
            raise JobObjectError("job creation refused")

        harness.deps = dataclasses.replace(harness.deps, make_job=unavailable_job)
        assert harness.run() == owner.EXIT_REFUSED
        assert harness.recorder.events == ["lease.acquire", "lease.close"]

    def test_raw_native_error_details_do_not_leak_to_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        harness = _Harness(tmp_path)
        sensitive = "sensitive-path-marker"

        def failing_loader(_path: Path) -> object:
            raise OSError(f"cannot read {sensitive}")

        harness.deps = dataclasses.replace(harness.deps, load_config=failing_loader)
        assert harness.run() == owner.EXIT_REFUSED
        output = capsys.readouterr().err
        assert "OSError" in output
        assert sensitive not in output
        assert "Traceback" not in output
        assert harness.recorder.events == ["lease.acquire", "lease.close"]

    def test_a_close_failure_does_not_skip_the_lease_release(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.job_error = None
        deps = harness.deps
        failing_job = _FakeJob(harness.recorder, close_error=OSError("handle already gone"))

        harness.deps = dataclasses.replace(deps, make_job=lambda: failing_job)
        harness.stop_after = 0.01
        # A failed close cannot be reported as a successful Bridge shutdown.
        assert harness.run() == owner.EXIT_REFUSED
        assert harness.recorder.events[-1] == "lease.close"
        assert failing_job.closed


class TestLifetimeAndShutdown:
    """Shutdown order: request, bounded wait, job close (the kill), lease release."""

    def test_a_stop_request_shuts_down_in_order(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.stop_after = 0.02
        assert harness.run() == owner.EXIT_OK
        assert harness.recorder.events[-4:] == ["stop", "exit", "job.close", "lease.close"]

    def test_a_bridge_exit_is_reported_and_not_restarted(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.exit_codes = (7,)
        assert harness.run() == owner.EXIT_BRIDGE_LOST
        assert harness.recorder.events.count("spawn") == 1
        assert harness.recorder.events[-2:] == ["job.close", "lease.close"]

    def test_a_graceful_timeout_still_closes_the_job(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        harness = _Harness(tmp_path)
        harness.stop_after = 0.01
        harness.graceful = False
        assert harness.run() == owner.EXIT_OK
        assert harness.recorder.events[-2:] == ["job.close", "lease.close"]
        assert "did not exit within the graceful window" in capsys.readouterr().err


class TestNoCoreOrConnector:
    """The whole point of the split: this owner starts the Bridge and nothing else."""

    def test_exactly_one_process_is_started_and_it_is_the_bridge(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.stop_after = 0.01
        assert harness.run() == owner.EXIT_OK
        assert harness.recorder.events.count("spawn") == 1
        kwargs = harness.captured["spawn_kwargs"]
        assert isinstance(kwargs, dict)
        argv = kwargs["argv"]
        # Exactly one module is launched, and it is the Bridge. Asserting the ``-m`` targets rather
        # than scanning for words avoids matching ``--supervised`` and friends.
        modules = [argv[index + 1] for index, part in enumerate(argv) if part == "-m"]
        assert modules == ["serverfs_agent_bridge.main"]
        assert "--supervised" in argv

    def test_the_module_has_no_stdio_child_or_connector_path(self) -> None:
        """Structural, because the failure being prevented is "someone adds it back later"."""
        source = Path(owner.__file__).read_text(encoding="utf-8")
        for forbidden in ("_start_stdio_child", "forward_stdio", "subprocess.Popen("):
            assert forbidden not in source, forbidden

    def test_a_bridge_loss_does_not_touch_anything_else(self, tmp_path: Path) -> None:
        """No Core, Core-kill or ingress call exists to run, which the event list shows."""
        harness = _Harness(tmp_path)
        harness.exit_codes = (9,)
        assert harness.run() == owner.EXIT_BRIDGE_LOST
        assert set(harness.recorder.events) <= {
            "lease.acquire",
            "job.open",
            "spawn",
            "bootstrap",
            "ready",
            "stop",
            "exit",
            "job.close",
            "lease.close",
        }


class TestPolicyAndEnvironment:
    def test_the_rendered_policy_reaches_the_bridge(self, tmp_path: Path) -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        config = tmp_path / "serverfs.toml"
        config.write_text(
            AGENT_CONFIG.format(root=str(workdir).replace("\\", "\\\\")), encoding="utf-8"
        )
        harness = _Harness(tmp_path)
        harness.config_path = config
        deps = dataclasses.replace(
            harness.deps,
            load_config=owner.load_native_config,
            render_request=owner._render_request,
            render_config=harness._render_config,
        )
        harness.deps = deps
        harness.stop_after = 0.01
        assert harness.run() == owner.EXIT_OK

        request = harness.captured["request"]
        assert isinstance(request, dict)
        assert [entry["alias"] for entry in request["workdirs"]] == ["repo"]
        assert request["workdirs"][0]["agent_mode"] == "workspace-write"
        assert request["workdirs"][0]["agent_runtimes"] == ["codex"]
        blob = str(request)
        assert "127.0.0.1" not in blob
        assert "agent_proxy" not in request

    def test_the_bridge_environment_is_scrubbed(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.environ = dict(POLLUTION)
        harness.stop_after = 0.01
        deps = dataclasses.replace(harness.deps, build_env=owner.bridge_child_environment)
        harness.deps = deps
        assert harness.run() == owner.EXIT_OK

        kwargs = harness.captured["spawn_kwargs"]
        assert isinstance(kwargs, dict)
        child_env = kwargs["child_env"]
        assert isinstance(child_env, dict)
        for name in POLLUTION:
            assert name not in child_env, name
        assert not [name for name in child_env if name.upper().startswith("SERVERFS_AGENT_")]

    def test_an_agent_disabled_configuration_is_refused_at_once(self, tmp_path: Path) -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        config = tmp_path / "serverfs.toml"
        config.write_text(
            DISABLED_CONFIG.format(root=str(workdir).replace("\\", "\\\\")), encoding="utf-8"
        )
        harness = _Harness(tmp_path)
        harness.config_path = config
        deps = dataclasses.replace(
            harness.deps,
            load_config=owner.load_native_config,
            render_request=owner._render_request,
        )
        harness.deps = deps
        assert harness.run() == owner.EXIT_REFUSED
        assert "job.open" not in harness.recorder.events
        assert harness.recorder.events[-1] == "lease.close"

    def test_refusal_messages_carry_no_path_and_no_traceback(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        harness = _Harness(tmp_path)
        harness.spawn_error = AgentLifecycleError(
            "the Agent Bridge could not be started (FileNotFoundError)"
        )
        assert harness.run() == owner.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "FileNotFoundError" in err
        assert "Traceback" not in err
        assert str(tmp_path) not in err


class TestRealIsolatedGeneration:
    """Real lease, real anonymous job, real child: the order is measured, not described."""

    def test_a_generation_starts_serves_and_releases_cleanly(self, tmp_path: Path) -> None:
        harness = _Harness(tmp_path)
        harness.use_real_lease = True
        harness.use_real_job = True
        harness.use_real_spawn_and_shutdown = True
        harness.stop_after = 0.3
        try:
            assert harness.run() == owner.EXIT_OK
            assert harness.real_process is not None
            assert harness.real_process.poll() is not None, "the child outlived the owner"
            assert harness.real_job is not None and harness.real_job.is_open is False
            # The real job and lease are not recorder-aware; their own state is the assertion.
            assert harness.recorder.events[-2:] == ["stop", "exit"]
        finally:
            if harness.real_process is not None and harness.real_process.poll() is None:
                harness.real_process.kill()
                harness.real_process.wait(timeout=15)

        # The lease is genuinely released: the same isolated root can be taken again.
        again = LifecycleLease(root=tmp_path / "lifecycle").acquire()
        again.close()
