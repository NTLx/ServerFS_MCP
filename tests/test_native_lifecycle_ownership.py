"""The per-user Agent lifecycle ownership lease, and the containment fact it carries.

Two measured defects are pinned here.

**Cross-attachment.** The Agent endpoint is derived from the user SID alone, so before this lease a
second Agent-enabled supervisor in one user session attached to the first chain's Bridge: its client
initialized, its tools answered, and its task, artifact and store row landed in the *other* chain.
Ownership must therefore be exclusive and must fail closed, not degrade to sharing.

**Unprovable containment.** Startup reconciliation may only clear a recovery guard once the provider
is known to be gone, and no provider adapter can know that the OS killed it. Establishing it takes
two kernel objects, and this file covers both halves: the lease below (no earlier owner is alive)
and the SID-scoped *named* Job Object (the object that actually held the previous provider tree no
longer exists). The lease alone would be an inference -- it never contained anything.

Scope is the user, matching the Named Pipe, and deliberately not the data home -- two data homes
in one session share one endpoint. It is also not the environment: `LOCALAPPDATA` can be set by a
parent, so reading it would make the scope a configuration choice again. These tests never touch
the real per-user artifact: every case passes an explicit root.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from serverfs_mcp import native_lifecycle
from serverfs_mcp.agent_lifecycle import bootstrap_frame_bytes
from serverfs_mcp.native_lifecycle import LifecycleLease, LifecycleOwnershipError


class TestTheLifecycleLeaseIsExclusive:
    def test_a_second_holder_is_refused_rather_than_shared(self, tmp_path: Path) -> None:
        """Fail closed: sharing is the measured defect, so a second acquire must not succeed."""
        first = LifecycleLease(tmp_path).acquire()
        try:
            with pytest.raises(LifecycleOwnershipError):
                LifecycleLease(tmp_path).acquire()
        finally:
            first.close()

    def test_ownership_is_reusable_once_released(self, tmp_path: Path) -> None:
        LifecycleLease(tmp_path).acquire().close()
        second = LifecycleLease(tmp_path).acquire()
        second.close()

    def test_the_artifact_name_carries_the_endpoint_identity(self) -> None:
        """One identity for the pipe and the lease, so the two cannot disagree about the owner.

        The pipe name is namespace-qualified and therefore not a legal file name, so the artifact is
        named by a hash of it -- measured: the first attempt used the pipe name verbatim and
        ``CreateFileW`` failed with ERROR_FILE_NOT_FOUND.
        """
        name = native_lifecycle.lifecycle_artifact_name()
        assert name.endswith(".lifecycle")
        digest = hashlib.sha256(native_lifecycle.lifecycle_identity().encode("utf-8")).hexdigest()[
            :32
        ]
        assert name == f"serverfs-agent-lifecycle-{digest}.lifecycle"
        assert "\\" not in name and "/" not in name and ":" not in name

    def test_the_lease_scope_ignores_the_data_home(self, tmp_path: Path, monkeypatch) -> None:
        """The scope is the user, so a second data home cannot claim the one endpoint."""
        monkeypatch.setenv("SERVERFS_DATA_HOME", str(tmp_path / "one"))
        first = native_lifecycle.lifecycle_dir()
        monkeypatch.setenv("SERVERFS_DATA_HOME", str(tmp_path / "two"))
        assert native_lifecycle.lifecycle_dir() == first


class TestOwnershipSurvivesTheOwnerDying:
    def test_a_killed_owner_releases_the_lease(self, tmp_path: Path) -> None:
        """The property the recovery path rides on: a crash must not wedge the user's lifecycle.

        Bounded rather than immediate: the kernel closes the dead process's handles during teardown,
        so the lock is released shortly after the process is signalled rather than at the instant
        `wait()` returns. Measured -- an immediate re-acquire raced it. "Released after N seconds"
        and "never released" are different findings, and only the second fails.
        """
        import time

        holder = subprocess.Popen(  # noqa: S603 - a fixed argv against sys.executable
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    f"""
                    import time
                    from serverfs_mcp.native_lifecycle import LifecycleLease
                    LifecycleLease({str(tmp_path)!r}).acquire()
                    print("held", flush=True)
                    time.sleep(600)
                    """
                ),
            ],
            stdout=subprocess.PIPE,
        )
        try:
            assert holder.stdout is not None
            assert holder.stdout.readline().strip() == b"held"
            with pytest.raises(LifecycleOwnershipError):
                LifecycleLease(tmp_path).acquire()
            holder.kill()
            holder.wait(timeout=30)

            deadline = time.monotonic() + 20
            last: Exception | None = None
            while time.monotonic() < deadline:
                try:
                    LifecycleLease(tmp_path).acquire().close()
                    return
                except LifecycleOwnershipError as exc:
                    last = exc
                    time.sleep(0.25)
            raise AssertionError(
                f"the lifecycle lease was never released after the owner died: {last}"
            )
        finally:
            if holder.poll() is None:
                holder.kill()
                holder.wait(timeout=30)


class TestTheSupervisorWatchesItsBridge:
    """A Bridge-only crash must collect the whole chain, and that needs the watcher *installed*.

    Measured defect: the first version of this change defined `_watch_bridge` and never called it --
    the helper existed, the tests that named it did not, and only a real Bridge-only kill exposed
    it. That is the same shape as every other miss in this phase: a claim that was never checked.
    So the wiring itself is pinned, by source, since a behavioural test at this level would need a
    whole launcher chain.
    """

    def test_run_with_agent_installs_the_bridge_watcher(self) -> None:
        import inspect

        from serverfs_mcp import supervisor

        body = inspect.getsource(supervisor.run_with_agent)
        assert "_watch_bridge(" in body, (
            "the supervisor must monitor its Bridge: without it a dead Bridge leaves the Job and "
            "the per-user lifecycle lease held by a chain that serves nothing"
        )

    def test_the_watcher_ends_the_forwarding_loop(self) -> None:
        """Terminating the stdio child is what runs the shutdown path; a bare return would not."""
        import inspect

        from serverfs_mcp import supervisor

        source = inspect.getsource(supervisor._watch_bridge)
        assert "terminate_quietly(stdio_child)" in source


class TestTheContainmentFactRidesThePrivateChannelOnly:
    def test_the_frame_omits_the_fact_when_it_is_not_proven(self) -> None:
        """Default False, so a path that cannot prove containment looks like an older supervisor."""
        document = json.loads(bootstrap_frame_bytes(None))
        assert "prior_bridge_execution_stopped" not in document

    def test_the_frame_carries_the_fact_when_it_is_proven(self) -> None:
        document = json.loads(bootstrap_frame_bytes(None, prior_bridge_execution_stopped=True))
        assert document["prior_bridge_execution_stopped"] is True

    def test_the_frame_version_is_unchanged(self) -> None:
        """A private lifecycle statement must not be mistaken for a protocol change."""
        frame = json.loads(bootstrap_frame_bytes(None, prior_bridge_execution_stopped=True))
        assert frame["version"] == 1


class TestTheOwnerScopeIsNotConfigurable:
    """A settable location is a settable *scope*, which is the defect this lease closes.

    Measured: the first version read ``SERVERFS_AGENT_LIFECYCLE_DIR``, and the supervisor's
    environment scrub does not remove that name. Two supervisors of one user could therefore create
    two different locks and each conclude it owned the lifecycle -- while both deriving the same
    Named Pipe from the same SID, which is the cross-attachment this lease exists to prevent.
    """

    def test_the_location_survives_plausible_overrides(self, monkeypatch) -> None:
        """The behavioural half: no environment name moves the owner scope."""
        baseline = native_lifecycle.lifecycle_dir()
        for name in (
            "SERVERFS_AGENT_LIFECYCLE_DIR",
            "SERVERFS_AGENT_LIFECYCLE_HOME",
            "SERVERFS_LIFECYCLE_DIR",
            "SERVERFS_DATA_HOME",
        ):
            monkeypatch.setenv(name, str(Path("C:/serverfs-scope-probe")))
            assert native_lifecycle.lifecycle_dir() == baseline, name

    def test_the_module_reads_no_serverfs_namespaced_name(self) -> None:
        """The source half, for a name the list above does not guess.

        Read from the AST rather than the text so the docstrings -- which necessarily discuss
        ``SERVERFS_DATA_HOME`` -- cannot make this pass or fail for the wrong reason. The shape
        filter is what separates a configuration name from a path component: every environment name
        in this project is upper-snake and namespaced, while ``"ServerFS"`` and ``"serverfs"`` are
        directory names and not configuration.
        """
        tree = ast.parse(inspect.getsource(native_lifecycle))
        literals = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        found = sorted(
            {name for name in literals if name.isupper() and name.startswith("SERVERFS")}
        )
        assert not found, (
            f"the lifecycle owner scope must not be selectable by environment: {found}"
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="the Windows root is the canonical one")
    def test_the_windows_root_is_the_shells_canonical_folder(self) -> None:
        """Not the environment's idea of it."""
        assert native_lifecycle.lifecycle_dir() == (
            native_lifecycle.window_local_app_data() / "ServerFS" / "agent-lifecycle"
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="the Windows root is the canonical one")
    def test_no_variable_moves_the_windows_root(self, monkeypatch) -> None:
        """The same defect as the removed seam, one level down.

        Measured shape of it: ``LOCALAPPDATA=C:\\A`` in one process and ``=D:\\B`` in another gave
        one user two locks while both derived the same pipe from the same SID. The variable is
        inherited, so removing the explicit ``SERVERFS_*`` override was necessary but not
        sufficient. The profile variables are included because they are the plausible fallbacks
        somebody would reach for.
        """
        baseline = native_lifecycle.lifecycle_dir()
        canonical = native_lifecycle.window_local_app_data()
        for name in (
            "LOCALAPPDATA",
            "APPDATA",
            "USERPROFILE",
            "HOMEDRIVE",
            "HOMEPATH",
            "SERVERFS_DATA_HOME",
        ):
            monkeypatch.setenv(name, str(Path("C:/serverfs-scope-probe")))
            assert native_lifecycle.lifecycle_dir() == baseline, name
            assert native_lifecycle.window_local_app_data() == canonical, name

    @pytest.mark.skipif(sys.platform != "win32", reason="the Windows root is the canonical one")
    def test_the_windows_root_reads_no_environment(self) -> None:
        """The source form of the same claim, so a new variable name cannot slip past the list.

        Scoped to the Windows branch on purpose: the POSIX branch legitimately reads the platform's
        own XDG variable, and the Agent lifecycle does not run there. Matching the function's whole
        text would make this fail on a branch that is not the deployment under test -- the first
        version of this pin did exactly that.
        """
        function = next(
            node
            for node in ast.walk(ast.parse(inspect.getsource(native_lifecycle)))
            if isinstance(node, ast.FunctionDef) and node.name == "lifecycle_dir"
        )
        windows_branch = next(
            node
            for node in function.body
            if isinstance(node, ast.If) and "nt" in ast.unparse(node.test)
        )
        assert "os.environ" not in ast.unparse(windows_branch)


class TestTheContainmentJobName:
    """The second half of the proof: the name of the object that did the containing.

    Pinned in this module rather than with the Job tests because the *identity* is what lives here.
    The name has to be spelled from the same SID-scoped value as the pipe and the artifact, or the
    barrier would be about a different user -- and a barrier about a different user is worse than
    none, because it would answer "free" while somebody else's tree is still running.
    """

    def test_the_name_is_global_and_namespaced(self) -> None:
        """``Global\\``, because the pipe crosses sessions while the lease is per user."""
        assert native_lifecycle.job_name().startswith("Global\\serverfs-agent-bridge-job-v1-")

    def test_the_name_carries_the_same_identity_as_the_artifact(self) -> None:
        digest = hashlib.sha256(native_lifecycle.lifecycle_identity().encode("utf-8")).hexdigest()[
            :32
        ]
        assert native_lifecycle.job_name() == f"Global\\serverfs-agent-bridge-job-v1-{digest}"
        assert (
            native_lifecycle.lifecycle_artifact_name()
            == f"serverfs-agent-lifecycle-{digest}.lifecycle"
        )

    def test_the_name_is_not_a_path(self) -> None:
        """A kernel object name; a path spelling would send a later reader to the filesystem."""
        local = native_lifecycle.job_name().removeprefix("Global\\")
        assert "/" not in local and ":" not in local and "\\" not in local


class TestTheArtifactMustBeAPlainFile:
    """The artifact is a containment proof, so its identity meets the workdir lease's standard.

    Startup reads "the previous owner is gone" out of this file's lock. If the path is a redirect,
    the lock is taken on an object other than the one the next owner validates -- so a directory, a
    reparse point, or an unusable directory has to fail closed rather than be locked and believed.

    The classification is pinned separately from the filesystem refusal on purpose. Creating a
    reparse point needs a privilege this host does not have, so a real fixture would silently skip;
    the predicates are pure and run everywhere, and the filesystem case below proves the refusal is
    real for the one shape that can be planted without privilege.
    """

    #: Literal attribute bits, deliberately not imported from the module: this is the OS's
    #: vocabulary, and the test should not be able to agree with a wrong constant.
    NORMAL_FILE = 0x0000_0080
    DIRECTORY = 0x0000_0010
    REPARSE = 0x0000_0400
    INVALID = 0xFFFF_FFFF
    SYMLINK_TAG = 0xA000_000C

    def test_a_plain_file_is_accepted(self) -> None:
        """The positive control: a predicate that refused everything would pass every case below."""
        assert native_lifecycle.is_redirected(self.NORMAL_FILE, 0) is False

    @pytest.mark.parametrize(
        ("attributes", "reparse_tag"),
        [
            (REPARSE, SYMLINK_TAG),
            (REPARSE | NORMAL_FILE, 0x8000_001A),
            (DIRECTORY, 0),
            (INVALID, 0),
            # A tag without the attribute bit still means the object is not a plain file.
            (NORMAL_FILE, SYMLINK_TAG),
        ],
    )
    def test_a_redirect_or_non_file_object_is_refused(
        self, attributes: int, reparse_tag: int
    ) -> None:
        assert native_lifecycle.is_redirected(attributes, reparse_tag) is True

    def test_a_real_directory_is_accepted_only_as_a_directory(self) -> None:
        assert native_lifecycle.is_unusable_directory(self.DIRECTORY) is False

    @pytest.mark.parametrize(
        "attributes",
        [DIRECTORY | REPARSE, NORMAL_FILE, INVALID],
        ids=["redirect", "not-a-directory", "absent"],
    )
    def test_an_unusable_directory_is_refused(self, attributes: int) -> None:
        assert native_lifecycle.is_unusable_directory(attributes) is True

    def test_a_directory_planted_at_the_artifact_path_is_refused(self, tmp_path: Path) -> None:
        """The real filesystem case: the outcome is a refusal, not a lock taken on a directory.

        Which check fires is not asserted, because it differs by platform -- Windows refuses the
        open before any attribute is read. The claim under test is that no path through this code
        ends with a handle held on something that is not the artifact.
        """
        (tmp_path / native_lifecycle.lifecycle_artifact_name()).mkdir()
        with pytest.raises(LifecycleOwnershipError):
            LifecycleLease(tmp_path).acquire()
