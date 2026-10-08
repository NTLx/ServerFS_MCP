"""D9: the Phase D acceptance -- one real Windows native Agent lifecycle, end to end.

Every process in this chain is the production implementation:

    python -m serverfs_mcp.cli tunnel -> cmd_tunnel -> native_tunnel
    -> fake tunnel-client -> supervisor -> private config renderer
    -> Job Object -> real Bridge process -> real Named Pipe
    -> native ServerFS stdio -> the published MCP Agent surface -> the provider adapter

The chain starts at the product's own CLI entry point. An earlier revision imported
``run_native_tunnel`` and called it directly, which skipped argparse, ``cmd_tunnel`` and the CLI's
error normalization -- one layer short of what an operator runs, and it made a redacted CLI refusal
look like a product traceback.

Only the provider adapter at the end is a test double, injected as a ``sitecustomize`` so the
repository gains no test-only operator surface, and the public runtime name stays ``codex`` so the
frozen ten Agent tools and the writer lease are the real ones. Nothing is stubbed between the steps.

**Why this is not a unit test pile.** Each case below asserts a link that only exists across a
    process
boundary: the config the renderer wrote, the pipe both sides derived, the 21-tool surface the stdio
child published, the file the adapter wrote into the authorized workdir, the environment a
grandchild process actually received. A test that exercised any one of those in-process would prove
something narrower, and would keep passing if the chain between them broke.

**Grounded in reality, not in mocks.** Several cases assert a *negative* -- that a credential did
    not
reach a child -- and a negative assertion is vacuous if the thing it denies was never there. So the
parent deliberately plants every namespace in ``TUNNEL_MARKERS`` and ``GENERIC_PROXY_MARKERS``
before launching, and one test asserts they really arrived.

**Timing is awaited, never assumed.** A terminal task status is published a moment *before* the
service's ``finally`` block releases the writer lease, so a client that saw ``succeeded`` and
    mutated
immediately would race the release. The resulting ``WORKDIR_RECOVERY_REQUIRED`` would say nothing
about the contract, so the release is polled for and the timeout still fails the test.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

E2E_DIR = Path(__file__).resolve().parent / "e2e"


def _load(name: str):
    """Import a harness module by path.

    ``tests/e2e`` is a directory of scripts rather than a package -- it has no ``__init__.py`` and
        is
    never imported by production code -- so the modules are loaded by file location instead of being
    put on ``sys.path``, which would leak two generic module names into the whole test session.
    """
    spec = importlib.util.spec_from_file_location(f"d9_{name}", E2E_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_bootstrap = _load("d9_bridge_bootstrap")
_lifecycle_module = _load("d9_lifecycle")

ENV_CAPTURE_FILE = _bootstrap.ENV_CAPTURE_FILE
PROVIDER_CHILD_FILE = _bootstrap.PROVIDER_CHILD_FILE
WAIT_FILE = _bootstrap.WAIT_FILE
WORKSPACE_WRITE_BYTES = _bootstrap.WORKSPACE_WRITE_BYTES
WORKSPACE_WRITE_FILE = _bootstrap.WORKSPACE_WRITE_FILE

AGENT_ENDPOINT_PORT = _lifecycle_module.AGENT_ENDPOINT_PORT
BRIDGE_PYTHON = _lifecycle_module.BRIDGE_PYTHON
GENERIC_PROXY_MARKERS = _lifecycle_module.GENERIC_PROXY_MARKERS
TUNNEL_MARKERS = _lifecycle_module.TUNNEL_MARKERS
Lifecycle = _lifecycle_module.Lifecycle
_alive = _lifecycle_module._alive
interpreters_present = _lifecycle_module.interpreters_present
process_command_lines = _lifecycle_module.process_command_lines
wait_until = _lifecycle_module.wait_until

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows native lifecycle"),
    pytest.mark.skipif(
        not interpreters_present(), reason="both project virtualenvs must be present"
    ),
]

#: The frozen public Agent surface, transcribed from the registered tool names rather than counted.
#: Two names in the first draft were wrong (``get_agent_task_evidence`` does not exist,
#: ``answer_agent_question`` does), which a count-only assertion would not have caught.
AGENT_TOOLS = frozenset(
    {
        "submit_agent_task",
        "get_agent_task",
        "cancel_agent_task",
        "read_agent_task_events",
        "read_agent_task_result",
        "respond_agent_approval",
        "answer_agent_question",
        "send_agent_message",
        "list_agent_runtimes",
        "list_agent_models",
    }
)

#: Every marker the secret scan looks for. Grouped by what it would mean if it appeared.
FORBIDDEN_IN_CONFIG = tuple(TUNNEL_MARKERS.values()) + tuple(GENERIC_PROXY_MARKERS.values())


class Client:
    """A JSON-RPC client speaking MCP over the chain's real stdio."""

    def __init__(self, lifecycle: Lifecycle) -> None:
        self.lifecycle = lifecycle
        self._next_id = 0

    def _send(self, payload: dict) -> None:
        assert self.lifecycle.process is not None
        assert self.lifecycle.process.stdin is not None
        self.lifecycle.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        self.lifecycle.process.stdin.flush()

    def _read(self) -> dict:
        assert self.lifecycle.process is not None
        assert self.lifecycle.process.stdout is not None
        line = self.lifecycle.process.stdout.readline()
        assert line, f"the chain closed stdout: {self.lifecycle.stderr_text()[-1500:]}"
        return json.loads(line.decode("utf-8"))

    def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        self._send(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}
        )
        while True:
            message = self._read()
            if message.get("id") == self._next_id:
                return message

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> dict:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "d9-acceptance", "version": "1"},
            },
        )
        self.notify("notifications/initialized")
        return response

    def tool_names(self) -> list[str]:
        return sorted(tool["name"] for tool in self.request("tools/list")["result"]["tools"])

    def call(self, name: str, arguments: dict) -> dict:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        result = response["result"]
        if result.get("isError"):
            raise AssertionError(f"{name} failed: {result}")
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def call_error(self, name: str, arguments: dict) -> str:
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        assert response["result"].get("isError"), f"{name} unexpectedly succeeded"
        return json.dumps(response["result"])

    def wait_for_status(self, task_id: str, timeout: float = 60.0) -> str:
        deadline = time.monotonic() + timeout
        status = ""
        while time.monotonic() < deadline:
            status = self.call("get_agent_task", {"task_id": task_id})["status"]
            if status in {"succeeded", "failed", "cancelled", "interrupted"}:
                return status
            time.sleep(0.05)
        raise AssertionError(f"task {task_id} stayed {status!r} for {timeout}s")

    def wait_for_mutation(self, path: str, timeout: float = 45.0) -> bool:
        """Poll until one mutation is accepted, which is how the lease release is observed.

        The first rejected attempt is not an error: it is the observation that the lease was still
        held. What matters is that a mutation eventually succeeds without any operator action.
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.call("create_text_file", {"path": path, "workdir": "repo", "content": "ok"})
                return True
            except AssertionError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)


def _submit(client: Client, prompt: str) -> str:
    """Submit one workspace-write task through the public surface and return its id."""
    return client.call(
        "submit_agent_task",
        {
            "workdir": "repo",
            "runtime": "codex",
            "profile": "workspace-write",
            "prompt": prompt,
        },
    )["task_id"]


@pytest.fixture()
def chain(tmp_path: Path):
    """A launched Agent-enabled chain, guaranteed to be torn down."""
    lifecycle = Lifecycle(tmp_path, agent_enabled=True)
    lifecycle.launch()
    client = Client(lifecycle)
    client.initialize()
    try:
        yield lifecycle, client
    finally:
        lifecycle.stop()


class TestTheChainStarts:
    def test_the_full_launcher_chain_reaches_a_serving_bridge(self, chain) -> None:
        """The subject of D9: every link exists and the Bridge answers on the derived pipe."""
        lifecycle, client = chain
        # The renderer wrote the private config: proof the supervisor, the renderer subprocess and
        # the private-state contract all ran.
        assert lifecycle.bridge_json.is_file()
        document = json.loads(lifecycle.bridge_json.read_text(encoding="utf-8"))
        assert document["lease_key"] == "alias"
        assert document["codex"] == {"enabled": True, "codex_bin": "codex", "use_proxy": False}
        assert document["allowed_peer_sid"].startswith("S-1-5-21")
        # The Bridge really is a live process serving the config the renderer produced.
        assert lifecycle.bridge_pids(), "no Bridge process is running"
        # And it answers Agent calls, so readiness was genuine rather than a sleep.
        assert client.call("list_agent_runtimes", {"workdir": "repo"})["runtimes"]

    def test_the_tunnel_client_really_received_the_encoded_command(self, chain) -> None:
        """The launcher step is observed, not assumed.

        ``encode_tunnel_command_argv`` exists because naive joining breaks on Windows paths with
        spaces. If the chain reached the supervisor without decoding it, the whole Windows-specific
        quoting contract would be untested.
        """
        lifecycle, _client = chain
        record = lifecycle.result().tunnel_record
        assert record["argv"][1:3] == ["-m", "serverfs_mcp.supervisor"], record["argv"]
        assert "--config" in record["argv"]

    def test_the_launcher_scrubbed_the_credential_namespaces_before_the_client(self, chain) -> None:
        """The v0.10 launcher contract, observed at the client rather than assumed.

        The planted markers were in the environment the *launcher* was started with; by the time the
        client runs, ``native_tunnel`` must already have removed the Tunnel, Control Plane, MCP,
        OpenAI and proxy namespaces. Recording what the client actually saw is how that is checked
        without trusting the code that does it.
        """
        lifecycle, _client = chain
        record = lifecycle.result().tunnel_record
        assert record["leaked_prefixes"] == [], record["leaked_prefixes"]
        assert record["leaked_proxy_names"] == [], record["leaked_proxy_names"]

    def test_the_planted_markers_were_really_present_before_the_launch(self, tmp_path) -> None:
        """Ground truth for every "never reached the child" assertion below.

        Without this, a harness that failed to plant the markers would make all of them pass for the
        wrong reason.
        """
        lifecycle = Lifecycle(tmp_path, agent_enabled=False)
        environment = lifecycle.child_env()
        for name, value in TUNNEL_MARKERS.items():
            assert environment[name] == value, name
        for name, value in GENERIC_PROXY_MARKERS.items():
            assert environment[name] == value, name

    def test_the_stdio_child_starts_only_after_the_bridge_is_ready(self, chain) -> None:
        """A published Agent surface with nothing behind it is the failure D5 forbids.

        Ordering is inferred rather than timestamped: the stdio child only exists if the supervisor
        reached step 11, which is after the authenticated readiness probe at step 10.
        """
        _lifecycle, client = chain
        runtimes = client.call("list_agent_runtimes", {"workdir": "repo"})["runtimes"]
        assert runtimes[0]["name"] == "codex"
        assert runtimes[0]["available"] is True


class TestThePublishedSurface:
    def test_agent_enabled_publishes_the_filesystem_surface_plus_the_ten_agent_tools(
        self, chain
    ) -> None:
        _lifecycle, client = chain
        names = set(client.tool_names())
        assert AGENT_TOOLS <= names, sorted(AGENT_TOOLS - names)
        # Exactly the ten and nothing else: a surface that had grown an eleventh tool, or lost one,
        # would both pass a subset assertion alone.
        assert names & set(AGENT_TOOLS) == AGENT_TOOLS
        # The ten are additive: the filesystem surface is untouched by delegation being on.
        assert len(names) == 21, sorted(names)

    def test_the_agent_disabled_chain_publishes_only_the_filesystem_surface(self, tmp_path) -> None:
        """The upgrade gate, through the real chain rather than a config parse.

        This is the case an operator upgrading from v0.10 actually hits, so the assertion is about
            the
        surface they would see, not about an internal flag.
        """
        lifecycle = Lifecycle(tmp_path, agent_enabled=False)
        lifecycle.launch()
        try:
            client = Client(lifecycle)
            client.initialize()
            names = set(client.tool_names())
            assert not (names & AGENT_TOOLS), sorted(names & AGENT_TOOLS)
            assert len(names) == 11, sorted(names)
        finally:
            lifecycle.stop()

    def test_a_filesystem_read_and_write_work_through_the_chain(self, tmp_path) -> None:
        """Agent delegation must not change filesystem semantics; that is asserted by using them."""
        lifecycle = Lifecycle(tmp_path, agent_enabled=False)
        lifecycle.launch()
        try:
            client = Client(lifecycle)
            client.initialize()
            listed = client.call("list_directory", {"path": ".", "workdir": "repo"})
            assert "seed.txt" in json.dumps(listed)
            client.call(
                "create_text_file",
                {"path": "chain-written.txt", "workdir": "repo", "content": "ok"},
            )
            assert (lifecycle.workdir / "chain-written.txt").read_text(encoding="utf-8") == "ok"
        finally:
            lifecycle.stop()


class TestTheAgentTaskLifecycle:
    def test_a_workspace_write_task_succeeds_and_mutates_the_authorized_workdir(
        self, chain
    ) -> None:
        """The Phase D acceptance proper, over the public MCP surface.

        The load-bearing assertion is the file. A turn that merely returned a string would prove the
        transport and nothing about the workspace-write contract, so the adapter writes into the cwd
        the Bridge resolved for the authorized workdir and the test reads it back through the public
        file surface.
        """
        lifecycle, client = chain
        task_id = _submit(client, "phase-d-native-lifecycle")

        # Strictly "succeeded". Any other terminal state fails, because a cancelled or failed turn
        # could still leave the artifact behind.
        assert client.wait_for_status(task_id) == "succeeded"

        # Read back through the published readers, not through the adapter's own return value. The
        # envelope field is ``event_type``; a reader that guessed ``kind`` would silently collect
        # nothing.
        events = client.call("read_agent_task_events", {"task_id": task_id})
        types = [event["event_type"] for event in events.get("events", [])]
        # Service-side lifecycle events plus the provider-side ones the adapter emitted through the
        # real context callback. Both halves matter: the first is the Bridge's bookkeeping, the
        # second proves a real adapter turn actually ran.
        for expected in ("task.started", "task.completed", "turn.started", "turn.completed"):
            assert expected in types, (expected, types)

        # The mutation, observed through the public file surface. This is a read, so it is not
        # serialized against the writer lease and needs no wait. The exact bytes are asserted
        # against the file on disk; the reader is asserted against its own documented shape so a
        # change in either would be visible rather than absorbed by an "in" comparison.
        assert (lifecycle.workdir / WORKSPACE_WRITE_FILE).is_file()
        assert (lifecycle.workdir / WORKSPACE_WRITE_FILE).read_bytes() == WORKSPACE_WRITE_BYTES
        read_back = client.call("read_text_file", {"path": WORKSPACE_WRITE_FILE, "workdir": "repo"})
        assert read_back["content"] == WORKSPACE_WRITE_BYTES.decode("utf-8")
        assert read_back["bytes_returned"] == len(WORKSPACE_WRITE_BYTES)

    def test_a_live_workspace_write_turn_excludes_an_mcp_mutation(self, tmp_path) -> None:
        """Mutual exclusion while a turn is live.

        The wait mode makes this deterministic: the adapter writes a marker before parking, so the
        test knows the turn actually started rather than inferring it from a status that could read
        "running" before the adapter ran at all.
        """
        lifecycle = Lifecycle(tmp_path, agent_enabled=True)
        lifecycle.launch(extra_env={"SERVERFS_TEST_BRIDGE_MODE": "wait"})
        try:
            client = Client(lifecycle)
            client.initialize()
            _submit(client, "phase-d-lease")

            assert wait_until(lambda: (lifecycle.workdir / WAIT_FILE).exists(), timeout=45), (
                "the waiting turn never started"
            )

            error = client.call_error(
                "create_text_file",
                {"path": "during.txt", "workdir": "repo", "content": "nope"},
            )
            assert "WORKDIR_BUSY" in error, error
            assert not (lifecycle.workdir / "during.txt").exists()
        finally:
            lifecycle.stop()

    def test_cancellation_releases_the_lease(self, tmp_path) -> None:
        """Cancellation cleanup. Phase C covers this at unit level; D9 shows it end to end."""
        lifecycle = Lifecycle(tmp_path, agent_enabled=True)
        lifecycle.launch(extra_env={"SERVERFS_TEST_BRIDGE_MODE": "wait"})
        try:
            client = Client(lifecycle)
            client.initialize()
            task_id = _submit(client, "phase-d-cancel")
            assert wait_until(lambda: (lifecycle.workdir / WAIT_FILE).exists(), timeout=45)
            client.call("cancel_agent_task", {"task_id": task_id})
            assert client.wait_for_status(task_id) in {"cancelled", "interrupted"}
            assert client.wait_for_mutation("after-cancel.txt"), (
                "the lease was never released after cancellation"
            )
            assert (lifecycle.workdir / "after-cancel.txt").is_file()
        finally:
            lifecycle.stop()

    def test_normal_completion_releases_the_lease_immediately(self, chain) -> None:
        """Normal completion. A distinct cleanup contract from cancellation."""
        lifecycle, client = chain
        task_id = _submit(client, "phase-d-release")
        assert client.wait_for_status(task_id) == "succeeded"
        assert client.wait_for_mutation("after-success.txt"), (
            "the lease survived a normally completed task"
        )
        assert (lifecycle.workdir / "after-success.txt").is_file()


class TestTrustDomainSeparation:
    @staticmethod
    def _captured_environment(tmp_path: Path, *, use_proxy: bool, proxy_enabled: bool) -> dict:
        lifecycle = Lifecycle(
            tmp_path, agent_enabled=True, proxy_enabled=proxy_enabled, use_proxy=use_proxy
        )
        lifecycle.launch(extra_env={"SERVERFS_TEST_BRIDGE_MODE": "env"})
        try:
            client = Client(lifecycle)
            client.initialize()
            task_id = _submit(client, "phase-d-env")
            assert client.wait_for_status(task_id) == "succeeded"
            capture = lifecycle.record_dir / ENV_CAPTURE_FILE
            assert capture.is_file(), "the provider child never recorded its own environment"
            return json.loads(capture.read_text(encoding="utf-8"))
        finally:
            lifecycle.stop()

    def test_the_provider_child_sees_only_the_dedicated_agent_endpoint(self, tmp_path) -> None:
        """The one place the endpoint is allowed to appear.

        Read from the child's own ``os.environ`` by a separate process, so it cannot be satisfied by
            a
        helper returning the right value with nothing calling it.
        """
        environment = self._captured_environment(tmp_path, use_proxy=True, proxy_enabled=True)
        assert environment.get("HTTPS_PROXY") == f"http://127.0.0.1:{AGENT_ENDPOINT_PORT}"
        bypass = environment.get("NO_PROXY", "")
        for mandatory in ("127.0.0.1", "localhost", "::1"):
            assert mandatory in bypass, bypass
        assert "corp.example" in bypass, "the operator's own bypass entries must survive"

    def test_the_provider_child_never_sees_the_other_namespaces(self, tmp_path) -> None:
        environment = self._captured_environment(tmp_path, use_proxy=True, proxy_enabled=True)
        # Raw Agent material: the child receives the *mapped* values, not the source ones.
        assert "SERVERFS_AGENT_PROXY_URL" not in environment
        assert "SERVERFS_AGENT_NO_PROXY" not in environment
        # The Tunnel and Control Plane credentials.
        for name in ("SERVERFS_PROXY_PASSWORD", "CONTROL_PLANE_API_KEY", "TUNNEL_CLIENT_PROFILE"):
            assert name not in environment, name
        # Generic proxy variables: the child gets HTTPS_PROXY and NO_PROXY only, because Phase 0F
        # measured that HTTP_PROXY alone is insufficient for HTTPS and that more names widen the
        # surface for no benefit.
        for name in (
            "HTTP_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ):
            assert name not in environment, name

    def test_provider_native_environment_survives_the_scrub(self, tmp_path) -> None:
        """An over-aggressive scrub would break provider auth as surely as a leak would.

        Absence alone is therefore not the contract: the child must still have what a real provider
        needs to find its own configuration.
        """
        environment = self._captured_environment(tmp_path, use_proxy=True, proxy_enabled=True)
        for name in ("PATH", "SYSTEMROOT", "USERPROFILE", "LOCALAPPDATA"):
            assert name in environment, f"the scrub removed {name}, which providers need"

    def test_use_proxy_false_gives_a_genuinely_proxy_free_child(self, tmp_path) -> None:
        """With proxying off the child must be proxy-free even though an endpoint is configured.

        This is the property Phase 0F requires because such a child cannot be cleaned after the
            fact.
        """
        environment = self._captured_environment(tmp_path, use_proxy=False, proxy_enabled=True)
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        ):
            assert name not in environment, name


class TestGeneratedArtifactsCarryNoSecrets:
    def test_the_rendered_config_holds_no_credential_or_endpoint(self, chain) -> None:
        lifecycle, _client = chain
        assert lifecycle.bridge_json.is_file(), "nothing to scan"
        blob = lifecycle.bridge_json.read_text(encoding="utf-8")
        for marker in FORBIDDEN_IN_CONFIG:
            assert marker not in blob, f"{marker!r} reached bridge.json"
        # The endpoint itself is not a value anywhere in the document: use_proxy is routing policy
        # and lives beside it, never the thing it points at.
        assert str(AGENT_ENDPOINT_PORT) not in blob

    def test_the_rendered_config_loads_through_the_real_loader(self, chain) -> None:
        """A document the Bridge would refuse could not have produced a running Bridge.

        Asserted anyway, because "the process started" and "the document is valid" are different
        claims and only the second survives a future schema change.
        """
        lifecycle, _client = chain
        repo = Path(__file__).resolve().parents[1]
        # ``lease_key`` is a document field rather than a BridgeConfig attribute, so the document is
        # read for it and the loader is asked to prove the document is loadable at all.
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from serverfs_agent_bridge.config import BridgeConfig\n"
            "doc = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
            "c = BridgeConfig.load(Path(sys.argv[1]))\n"
            "print(json.dumps({'sid': c.allowed_peer_sid, 'lease': doc.get('lease_key'),\n"
            "                  'codex': c.codex.codex_bin, 'use_proxy': c.codex.use_proxy,\n"
            "                  'socket': str(c.socket_path)}))\n"
        )
        completed = _run_json(
            [str(BRIDGE_PYTHON), "-c", script, str(lifecycle.bridge_json)],
            cwd=str(repo / "agent_bridge"),
            env_overrides={"PYTHONPATH": str(repo / "agent_bridge" / "src")},
        )
        assert completed is not None, "the rendered config did not load"
        assert completed["lease"] == "alias"
        assert completed["codex"] == "codex"
        assert completed["use_proxy"] is False
        assert completed["sid"].startswith("S-1-5-21")
        assert completed["socket"].startswith("\\\\.\\pipe\\serverfs-agent-bridge-v1-")

    def test_the_bridge_argv_carries_no_secret(self, chain) -> None:
        """argv is visible to any same-user process, so it is scanned like a published artifact."""
        lifecycle, _client = chain
        scanned = False
        for _pid, command_line in process_command_lines():
            if str(lifecycle.bridge_json) not in command_line:
                continue
            scanned = True
            for marker in FORBIDDEN_IN_CONFIG:
                assert marker not in command_line, f"{marker!r} reached the Bridge argv"
        assert scanned, "the Bridge process was not found, so the argv scan proved nothing"


class TestGracefulShutdown:
    def test_the_chain_stops_without_a_forced_kill(self, chain) -> None:
        """Acceptance must show the graceful path works, not merely that the job eventually kills.

        The Bridge is asked to stop by closing the lifecycle pipe, and the test asserts it exited on
        its own. A deployment that only worked because the Job Object killed everything would leave
            a
        provider holding a writer lease until the handle closed.
        """
        lifecycle, _client = chain
        bridge_pids = lifecycle.bridge_pids()
        assert bridge_pids, "no Bridge was running to stop"

        returncode = lifecycle.stop(timeout=45)
        assert returncode == 0, lifecycle.stderr_text()[-1500:]

        assert wait_until(lambda: not any(_alive(pid) for pid in bridge_pids), timeout=30), (
            "the Bridge outlived the graceful shutdown"
        )
        assert not lifecycle.supervisor_pids(), "the supervisor is still running"

    def test_no_recovery_guard_survives_a_completed_task(self, chain) -> None:
        """The workdir must be immediately usable again.

        The lock *file* and the empty ``active/`` directory are persistent private state and are
            meant
        to survive -- they are what the next start reuses. What must not survive is a recovery guard
        still present, which would make the next run fail with ``WORKDIR_RECOVERY_REQUIRED``.
            Asserting
        the directory was empty would therefore be asserting the wrong thing.
        """
        lifecycle, client = chain
        # A guard would be observable as a blocked mutation, so take and release a real lease first.
        assert client.wait_for_status(_submit(client, "phase-d-guard")) == "succeeded"
        assert client.wait_for_mutation("guard-probe.txt")
        active = lifecycle.lock_dir / "active"
        if active.is_dir():
            assert not list(active.iterdir()), "a recovery guard outlived the task"


class TestAbnormalTerminationIsContained:
    def test_killing_the_supervisor_takes_the_whole_bridge_tree_with_it(self, tmp_path) -> None:
        """Containment under an abnormal exit, through the real chain rather than a unit fixture.

        An unrelated process is started alongside so over-reach would be visible, which is the half
            of
        containment that is easy to get wrong and impossible to notice.
        """
        lifecycle = Lifecycle(tmp_path, agent_enabled=True)
        lifecycle.launch(extra_env={"SERVERFS_TEST_BRIDGE_MODE": "env"})
        client = Client(lifecycle)
        client.initialize()
        assert client.wait_for_status(_submit(client, "phase-d-containment")) == "succeeded"

        supervisor_pids = lifecycle.supervisor_pids()
        bridge_pids = lifecycle.bridge_pids()
        assert supervisor_pids and bridge_pids, "the chain did not start"

        bystander = _spawn_sleeper(120)
        try:
            # Terminate the supervisor abruptly: no stdin close, no graceful request, nothing the
            # Bridge could cooperate with.
            for pid in supervisor_pids:
                _terminate(pid)

            assert wait_until(lambda: not any(_alive(pid) for pid in bridge_pids), timeout=45), (
                "the Bridge survived the supervisor's death; the Job Object did not contain it"
            )
            assert _alive(bystander), "containment reached an unrelated process"
        finally:
            _terminate(bystander)
            lifecycle.kill()

    def test_killing_only_the_bridge_collapses_the_chain_and_frees_the_lifecycle(
        self, tmp_path: Path
    ) -> None:
        """The other half of containment, and the one nothing monitored before Phase F.

        The case above kills the supervisor and proves the Job Object reaps the tree. It says
        nothing about a Bridge that dies while its supervisor lives: the supervisor then holds the
        Job and the per-user lifecycle lease with nothing behind them -- a chain that serves nothing
        and blocks every later start. That was a measured defect: the first version of the bridge
        watcher was defined and never called, and only a real Bridge-only kill exposed it. This is
        the case that would have caught it.

        The provider child is the load-bearing part. ``_terminate`` on the Bridge proves the Bridge
        died, which is true by construction; what needs proving is that its *descendants* are
        reclaimed, and asserting that about a chain with no descendants would be asserting nothing.
        So the fake runtime spawns a real, long-lived child and records its pid -- a Job member by
        inheritance, killed when the supervisor's teardown closes the Job.
        """
        lifecycle = Lifecycle(tmp_path, agent_enabled=True, bridge_mode="child")
        lifecycle.launch()
        bystander = _spawn_sleeper(120)
        provider_child = 0
        try:
            client = Client(lifecycle)
            client.initialize()
            assert client.wait_for_status(_submit(client, "phase-d-bridge-only")) == "succeeded"

            provider_child = _recorded_provider_child(lifecycle)
            assert _alive(provider_child), (
                "the Bridge-owned descendant never started, so the containment step would be "
                "vacuous"
            )
            bridge_pids = lifecycle.bridge_pids()
            assert lifecycle.supervisor_pids() and bridge_pids, "the chain did not start"

            # Only the Bridge is terminated. Nothing closes the supervisor's stdin or asks it to
            # stop, so whatever happens next it must have decided on its own.
            for pid in bridge_pids:
                _terminate(pid)

            assert wait_until(lambda: not any(_alive(pid) for pid in bridge_pids), timeout=45), (
                "the Bridge survived its own termination"
            )
            assert wait_until(lambda: not lifecycle.supervisor_pids(), timeout=45), (
                "the supervisor outlived its Bridge"
            )
            assert wait_until(lambda: not _alive(provider_child), timeout=45), (
                "the Bridge-owned descendant outlived the chain: closing the Job reclaims it"
            )
            assert _alive(bystander), "containment reached an unrelated process"
        finally:
            _terminate(bystander)
            if provider_child:
                _terminate(provider_child)
            lifecycle.kill()

        assert _lifecycle_lease_is_free(timeout=30), (
            "the per-user lifecycle ownership was not released, so every later start would be "
            "refused by a chain that no longer serves anything"
        )


class TestStartupFailureLeavesNothingRunning:
    def test_a_bridge_that_cannot_start_leaves_no_orphan_and_no_stdio_child(self, tmp_path) -> None:
        """One representative failure through the real chain; the matrix lives in the unit suites.

        The failure is an Agent-enabled configuration whose Bridge interpreter does not exist. The
        chain is launched for real, the supervisor resolves the interpreter and fails to start a
        child, and the failure therefore happens inside the lifecycle under test.

        Launcher-level refusals are a separate class with their own cases below, because they happen
        before the supervisor exists and are normalized by a different layer.

        What is asserted here is the supervisor's own contract: a non-zero exit, a redacted message
        carrying no traceback and no interpreter path, no Bridge left running, and no lease taken. A
        partially started Agent service would hold a writer lease nobody owns.
        """
        lifecycle = Lifecycle(tmp_path, agent_enabled=True)
        missing = tmp_path / "no-such-dir" / "python.exe"
        lifecycle.launch(extra_env={"SERVERFS_BRIDGE_PYTHON": str(missing)})
        returncode = lifecycle.stop(timeout=90)

        stderr = lifecycle.stderr_text()
        assert returncode not in (0, None), (
            f"a failed Agent startup reported success: {stderr[-1200:]}"
        )
        assert "Traceback" not in stderr, stderr[-1500:]
        assert str(missing) not in stderr, stderr[-1500:]
        assert not lifecycle.bridge_pids(), "a Bridge was left running after a failed startup"
        if lifecycle.lock_dir.exists():
            assert not list(lifecycle.lock_dir.rglob("*")), "a lease survived a failed startup"

    def test_a_taken_containment_job_name_refuses_the_whole_startup(self, tmp_path: Path) -> None:
        """The barrier, through the real chain: an existing name must not become a join.

        ``CreateJobObjectW`` answers ``ERROR_ALREADY_EXISTS`` with a *usable* handle to the old
        object, so the tempting failure is to carry on inside a job the previous generation still
        owns -- and then send a containment statement downstream that is false. Refusing is the only
        safe reading, and this is the case that fails if the supervisor ever stops using the
        lifecycle identity: an anonymous job would start happily here, and the "fresh name shows the
        previous generation stopped" proof would silently become a claim about nothing.
        """
        from serverfs_mcp.native_lifecycle import LifecycleLease, job_name
        from serverfs_mcp.windows_job import WindowsJob

        holder = WindowsJob(job_name())
        holder.open()
        lifecycle = Lifecycle(tmp_path, agent_enabled=True)
        try:
            lifecycle.launch()
            returncode = lifecycle.stop(timeout=90)
            stderr = lifecycle.stderr_text()

            assert returncode not in (0, None), (
                f"startup claimed success while another owner held the containment job: "
                f"{stderr[-1200:]}"
            )
            assert "Traceback" not in stderr, stderr[-1500:]
            # The refusal is a failure class, not a dump: nothing about the holder.
            assert "already exists" in stderr, stderr[-1200:]
            assert not lifecycle.bridge_pids(), "a Bridge was started into a foreign job"
            # And the failed start owns nothing on the way out: it released the lifecycle lease, so
            # the next start is blocked by the holder's job, not by a leaked lock. (The holder is a
            # Job handle, deliberately not the lease -- the two are different objects.)
            LifecycleLease().acquire().close()
        finally:
            lifecycle.kill()
            holder.close()


class TestLauncherRefusalsAreRedacted:
    """Two configuration refusals, each through the real ``serverfs tunnel`` CLI.

    An earlier D9 revision recorded these as a known gap: "a malformed config or a non-existent
    workdir root is refused before the supervisor exists, and that path emits an unredacted
    traceback". That claim was an artefact of the harness, not a product defect. The harness
    imported ``run_native_tunnel`` and called it directly, skipping ``cmd_tunnel``; and
    ``cmd_tunnel`` catches ``(OSError, ValueError)`` while ``NativeTunnelError`` is a
    ``ValueError`` subclass. The traceback came from the layer the harness had removed, not
    from the operator-facing path.

    These cases settle the claim by observation rather than argument, and a future change that
    removes the normalization fails here rather than re-introducing a false known gap.

    Both assert the whole contract, not just the absence of a traceback: a non-zero exit, the
    normalized ``serverfs:`` message, no supervisor, no Bridge, and no Agent state or lease.
    """

    def _refuse(self, tmp_path: Path, config_text: str) -> tuple[Lifecycle, str]:
        """Launch the CLI against one configuration and return the lifecycle plus its stderr."""
        lifecycle = Lifecycle(
            tmp_path,
            agent_enabled=False,
            config_override=config_text,
            api_key_outside_workdirs=True,
        )
        lifecycle.launch()
        # The launcher writes its refusal and exits, so the sink is read after it does; otherwise
        # the file can still be empty here and the assertion would read as "the CLI said nothing".
        return lifecycle, lifecycle.stderr_text(wait=30.0)

    def test_a_malformed_config_is_a_normalized_refusal(self, tmp_path: Path) -> None:
        lifecycle, stderr = self._refuse(tmp_path, '[server\nlog_level = "INFO"\n')
        try:
            assert lifecycle.process is not None
            returncode = lifecycle.process.wait(timeout=60)
            assert returncode != 0, stderr[-1200:]
            assert "serverfs:" in stderr, stderr[-1200:]
            assert "Traceback" not in stderr, stderr[-1500:]
            # Nothing an operator should not have to read: no host path, no config file name.
            assert str(tmp_path) not in stderr, stderr[-1500:]
            assert not lifecycle.supervisor_pids()
            assert not lifecycle.bridge_pids()
        finally:
            lifecycle.kill()

    def test_a_workdir_root_that_does_not_exist_is_a_normalized_refusal(
        self, tmp_path: Path
    ) -> None:
        """The second half of the claim: a configured root that is not there.

        The API key is placed outside the workdirs so the launcher's own key-location rule does not
        fire first. Otherwise this case would pass for the wrong reason and prove nothing about
        the workdir root.
        """
        absent = tmp_path / "absent-root"
        lifecycle, stderr = self._refuse(
            tmp_path,
            '[server]\nlog_level = "INFO"\n\n[[workdirs]]\nalias = "repo"\n'
            f'path = "{_toml_path(absent)}"\nread_only = false\n',
        )
        try:
            assert lifecycle.process is not None
            returncode = lifecycle.process.wait(timeout=60)
            assert returncode != 0, stderr[-1200:]
            assert "serverfs:" in stderr, stderr[-1200:]
            assert "Traceback" not in stderr, stderr[-1500:]
            assert not lifecycle.supervisor_pids()
            assert not lifecycle.bridge_pids()
            # No Agent state may be created for a chain that never started.
            assert not lifecycle.bridge_json.exists()
        finally:
            lifecycle.kill()


class TestV010Compatibility:
    def test_a_disabled_agent_never_touches_the_agent_surface_on_disk(self, tmp_path) -> None:
        """No Agent state may be created for a deployment that did not ask for delegation."""
        lifecycle = Lifecycle(tmp_path, agent_enabled=False)
        lifecycle.launch()
        try:
            Client(lifecycle).initialize()
            assert not lifecycle.bridge_json.exists(), "a Bridge config was rendered for v0.10"
            assert not lifecycle.bridge_pids(), "a Bridge process was started for v0.10"
        finally:
            lifecycle.stop()

    def test_a_disabled_agent_requires_no_bridge_interpreter(self, tmp_path) -> None:
        """The v0.10 path must not depend on the Agent distribution being installed at all."""
        lifecycle = Lifecycle(tmp_path, agent_enabled=False)
        lifecycle.launch(extra_env={"SERVERFS_BRIDGE_PYTHON": str(tmp_path / "absent.exe")})
        try:
            client = Client(lifecycle)
            client.initialize()
            assert len(client.tool_names()) == 11
            client.call("list_directory", {"path": ".", "workdir": "repo"})
        finally:
            lifecycle.stop()


# -- small process helpers, kept local so this file needs no test utility module ----------------


def _toml_path(path: Path) -> str:
    """A Windows path as a TOML basic string, with the separators escaped."""
    return str(path).replace("\\", "\\\\")


def _run_json(
    argv: list[str], cwd: str | None = None, env_overrides: dict[str, str] | None = None
) -> dict | None:
    env = dict(os.environ)
    if env_overrides:
        env.update(env_overrides)
    completed = subprocess.run(argv, capture_output=True, text=True, timeout=90, cwd=cwd, env=env)
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def _spawn_sleeper(seconds: int) -> int:
    return subprocess.Popen(
        [sys.executable, "-c", f"import time;time.sleep({seconds})"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).pid


def _recorded_provider_child(lifecycle: Lifecycle) -> int:
    """The pid the fake runtime recorded for the real child it spawned."""
    record = lifecycle.record_dir / PROVIDER_CHILD_FILE
    assert record.is_file(), "the test runtime never reported a provider child"
    return int(json.loads(record.read_text(encoding="utf-8"))["pid"])


def _lifecycle_lease_is_free(*, timeout: float) -> bool:
    """Whether this user's lifecycle ownership can be taken again, bounded.

    Taken and released immediately: the question is only whether the previous owner let go, and
    holding it would make the next chain fail for a reason this test created. Bounded because the
    kernel closes a dying process's handles during teardown rather than at the instant its parent
    reaps it -- "released after N seconds" and "never released" are different findings.
    """
    from serverfs_mcp.native_lifecycle import LifecycleLease, LifecycleOwnershipError

    deadline = time.monotonic() + timeout
    while True:
        try:
            LifecycleLease().acquire().close()
            return True
        except LifecycleOwnershipError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)


def _terminate(pid: int) -> None:
    """Terminate one process and close the handle, because Phase D is strict about handle hygiene.

    ``OpenProcess`` returns a handle the caller owns. Leaving it open leaks one per terminated
    process: harmless in a short test run, but exactly the discipline D6 fixed in the product, so
    an acceptance helper that contradicts it advertises the contract badly.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 1)
    finally:
        kernel32.CloseHandle(handle)
