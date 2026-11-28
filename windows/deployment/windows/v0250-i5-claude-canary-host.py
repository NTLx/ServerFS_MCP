"""I5 one-use model canary supervisor, with real Windows Job containment.

Usage: python.exe -I -B v0250-i5-claude-canary-host.py --run-once
Not a ServerFS service. Launches a single installed Claude SDK worker in a
new, anonymous kill-on-close Job and disposable cwd. Exits after a bounded
response, regardless of error. No Scheduler operation or runtime mutation.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path


class I5CanaryHostError(RuntimeError):
    """A nonsecret terminal result, never a raw provider error."""


def require(ok: bool, message: str) -> None:
    if not ok:
        raise I5CanaryHostError(message)


def worker_python() -> Path:
    base = os.environ.get("LOCALAPPDATA", "")
    require(bool(base), "I5 Windows user home unavailable")
    return (
        Path(base)
        / "ServerFS"
        / "v25"
        / "instances"
        / "i4-a"
        / "envs"
        / "agent-bridge"
        / "Scripts"
        / "python.exe"
    )


def main() -> None:
    from serverfs_mcp.windows_job import WindowsJob

    require(sys.argv[1:] == ["--run-once"], "I5 provider call requires explicit --run-once")
    require(
        sys.platform == "win32"
        and os.name == "nt"
        and sys.flags.isolated
        and sys.dont_write_bytecode
        and not ctypes.windll.shell32.IsUserAnAdmin(),
        "I5 host needs native non-elevated Windows -I -B",
    )
    interpreter = worker_python()
    require(interpreter.is_file(), "I5 private SDK worker Python unavailable")
    script = Path(__file__).with_name("v0250-i5-claude-canary-worker.py")
    require(script.is_file(), "I5 fixed canary worker script unavailable")
    require(hasattr(subprocess, "CREATE_SUSPENDED"), "I5 race-free job start unsupported")

    child: subprocess.Popen | None = None
    with tempfile.TemporaryDirectory(prefix="serverfs-i5-claude-canary-") as temp:
        cwd = Path(temp).resolve(strict=True)
        with WindowsJob() as job:
            try:
                child = subprocess.Popen(
                    [str(interpreter), "-I", "-B", str(script), str(cwd)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    creationflags=(
                        subprocess.CREATE_SUSPENDED | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    ),
                )
                job.assign(child)
                job.resume(child)
                try:
                    stdout, _stderr = child.communicate(timeout=115)
                except subprocess.TimeoutExpired as exc:
                    job.close()  # Terminates SDK/CLI descendants in this job.
                    raise I5CanaryHostError("I5 canary exceeded bounded deadline") from exc
                require(child.returncode == 0, "I5 SDK/CLI worker failed")
                transcript = stdout.decode("ascii", errors="replace").splitlines()
                require(
                    transcript
                    == [
                        "I5_REAL_CLAUDE_MODEL_RESPONSE=PASS",
                        "I5_CLAUDE_TOOL_CALLS=ZERO",
                        "I5_CANARY_WORKER=PASS",
                    ],
                    "I5 real Claude result has unexpected shape",
                )
            finally:
                job.close()
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait(timeout=5)

    print("I5_WINDOWS_JOB_CONTAINMENT=VERIFIED")
    print("I5_REAL_PROVIDER_CANARY=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - no credentials/SDK raw stderr
        print("I5_REAL_PROVIDER_CANARY=FAILED type=" + type(exc).__name__, file=sys.stderr)
        raise SystemExit(2) from None
