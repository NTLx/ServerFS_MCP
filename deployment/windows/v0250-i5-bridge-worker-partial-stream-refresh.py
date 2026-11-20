"""I5 one-use offline refresh for SDK partial-text streaming, Worker only.

The retained previous I5 Worker Wheel must match the installed private Worker,
and the new source must differ ONLY in the approved Claude adapter module.
No source copy into site-packages, production service/Task Scheduler lifecycle,
Cloudflare, internet, credentials, Provider calls, or Core/Bridge Owner update.
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PHASE = "entry"
CHANGED = "adapters/claude.py"
NEW_SLOT = "bridge-worker-partial-stream-refresh-1"
PREVIOUS_SLOT = "bridge-worker-race-refresh-1"


class I5StreamRefreshError(RuntimeError):
    """Fixed, private-data-free stage refusal."""


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise I5StreamRefreshError(reason)


def _load_helpers():
    import importlib.util

    name = Path(__file__).with_name("v0250-i5-bridge-worker-race-refresh.py")
    spec = importlib.util.spec_from_file_location("i5_previous_worker_refresh", name)
    require(spec is not None and spec.loader is not None, "previous refresh helper missing")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _baseline(previous, *, installed_new: bool = False):
    from serverfs_mcp.native_endpoint import current_user_sid
    from serverfs_mcp.windows_v25 import storage

    previous._host()
    gate = previous.helper("v0250-i5-provider-shared-preflight.py", "i5_partial_stream_protected")
    named = previous.helper("v0250-i6-named-connectors-stage.py", "i5_partial_stream_i6_protected")
    require(
        gate.BRIDGE == "i5-shared" and named.IDS == ("i6-edge-a", "i6-edge-b"),
        "source identity mismatch",
    )
    root, venv, python, package, sid, _original_wheel = previous._baseline(gate, named, storage)
    require(sid == current_user_sid(), "private owner SID mismatch")
    old = tuple((root / "build" / PREVIOUS_SLOT).glob("serverfs_agent_bridge-*.whl"))
    require(len(old) == 1, "previous single-use Wheel is not uniquely retained")
    storage.check_private(old[0], directory=False, sid=sid)
    if not installed_new:
        require(
            previous._archive_matches(old[0], package),
            "installed Worker drifted from previous Wheel",
        )
    else:
        new = tuple((root / "build" / NEW_SLOT).glob("serverfs_agent_bridge-*.whl"))
        require(len(new) == 1, "new single-use Wheel is not uniquely retained")
        storage.check_private(new[0], directory=False, sid=sid)
        require(
            previous._archive_matches(new[0], package),
            "installed Worker drifted from the new Wheel",
        )
    return previous, gate, named, storage, root, venv, python, package, sid, old[0]


def _source_change_is_exact(previous, package: Path, sid: str) -> None:
    from serverfs_mcp.windows_v25 import storage

    source = REPO / "agent_bridge" / "src" / "serverfs_agent_bridge"
    require(
        (source / CHANGED).read_bytes() != (package / CHANGED).read_bytes(),
        "Claude stream candidate source has no changed bytes",
    )
    adapter = (source / CHANGED).read_text(encoding="utf-8")
    require(
        "include_partial_messages=True" in adapter
        and '"agent.text_delta"' in adapter
        and "message.parent_tool_use_id is not None" in adapter
        and '"text_delta"' in adapter,
        "Claude stream expected source markers missing",
    )
    for relative in previous.CHECKS:
        installed = package / relative
        storage.check_private(installed, directory=False, sid=sid)
        if relative != CHANGED:
            require(
                installed.read_bytes() == (source / relative).read_bytes(),
                "unrelated Worker module has changed since installed snapshot",
            )


def _check_wheel_matches_source(wheel: Path, previous) -> None:
    source = REPO / "agent_bridge" / "src" / "serverfs_agent_bridge"
    with zipfile.ZipFile(wheel) as archive:
        require(archive.testzip() is None, "Worker wheel is corrupt")
        for relative in previous.CHECKS:
            require(
                archive.read("serverfs_agent_bridge/" + relative)
                == (source / relative).read_bytes(),
                "Worker wheel source byte parity failed",
            )


def _installed_stream_api(python: Path, installer) -> None:
    output = installer.run(
        [
            str(python),
            "-I",
            "-B",
            "-c",
            "from claude_agent_sdk import ClaudeAgentOptions, StreamEvent; "
            "from serverfs_agent_bridge.adapters.claude import ClaudeAdapter; "
            "assert 'include_partial_messages' in ClaudeAgentOptions.__dataclass_fields__; "
            "assert hasattr(StreamEvent, '__annotations__'); "
            "print('I5_PARTIAL_STREAM_SDK_IMPORT_OK')",
        ],
        timeout=35,
    )
    require(
        output.strip() == b"I5_PARTIAL_STREAM_SDK_IMPORT_OK",
        "updated Worker cannot import pinned SDK streaming API",
    )


def main(argv: list[str] | None = None) -> None:
    global PHASE

    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--audit", action="store_true")
    group.add_argument("--stage", action="store_true")
    group.add_argument("--postflight", action="store_true")
    args = parser.parse_args(argv)

    previous = _load_helpers()
    previous._host()
    PHASE = "protected-I5-I6-and-previous-installed-wheel"
    (previous, gate, named, storage, root, venv, python, package, sid, old_wheel) = _baseline(
        previous, installed_new=args.postflight
    )
    print("I5_STREAM_REFRESH_PROTECTED_AND_PREVIOUS_WHEEL=PASS")

    candidate = root / "build" / NEW_SLOT
    if args.audit or args.stage:
        require(not candidate.exists() and not candidate.is_symlink(), "one-use slot is occupied")
        _source_change_is_exact(previous, package, sid)
        print("I5_STREAM_REFRESH_CLAUDE_ONLY_SOURCE_DIFF=PASS")
        if args.audit:
            print("I5_STREAM_REFRESH_AUDIT=PASS")
            return

    installer = previous.helper("v0250-i2-stage.py", "i5_stream_offline_wheel_helpers")
    require(installer.REPO == REPO, "offline Wheel helper root differs")
    if args.stage:
        PHASE = "one-use-offline-wheel-build"
        installer._new_dir(candidate, sid)
        wheel = installer._wheel(
            REPO / "agent_bridge",
            candidate,
            "serverfs_agent_bridge-*.whl",
            previous.CHECKS,
        )
        installer._protect_tree(candidate, sid)
        _check_wheel_matches_source(wheel, previous)
        print("I5_STREAM_REFRESH_NEW_WHEEL_SOURCE=VERIFIED")

        PHASE = "offline-dry-run"
        command = [
            "uv",
            "pip",
            "install",
            "--offline",
            "--no-deps",
            "--reinstall",
            "--python",
            str(python),
            str(wheel),
        ]
        env = dict(os.environ)
        env["UV_LINK_MODE"] = "copy"
        installer.run([*command, "--dry-run"], env=env, timeout=60)
        previous._baseline(gate, named, storage)
        require(previous._archive_matches(old_wheel, package), "Worker drift before install")
        print("I5_STREAM_REFRESH_DRY_RUN=PASS")

        PHASE = "only-i5-private-worker-package"
        installer.run(command, env=env, timeout=120)
        installer._protect_tree(venv, sid)
        print("I5_STREAM_REFRESH_PRIVATE_INSTALL=PASS")

    PHASE = "postflight-installed-sdk-and-source"
    previous._assert_source_parity(package, sid)
    _installed_stream_api(python, installer)
    require(
        len(tuple(candidate.glob("serverfs_agent_bridge-*.whl"))) == 1,
        "single new Wheel not retained",
    )
    wheel = next(candidate.glob("serverfs_agent_bridge-*.whl"))
    storage.check_private(wheel, directory=False, sid=sid)
    _check_wheel_matches_source(wheel, previous)
    # Old source wheel remains retained separately, never replaced by this gate.
    storage.check_private(old_wheel, directory=False, sid=sid)
    # The shared native fixture check does not require old package bytes;
    # this re-attests all protected I5/I6 Scheduler identities after install.
    previous._baseline(gate, named, storage)
    print("I5_STREAM_REFRESH_INSTALLED_EXACT_AND_PRIVATE=PASS")
    print("I5_STREAM_REFRESH_POSTFLIGHT=PASS")


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:  # noqa: BLE001 - sanitized stage/class only
        print(
            "I5_STREAM_REFRESH=FAILED phase=" + PHASE + " type=" + type(exc).__name__,
            file=sys.stderr,
        )
        raise SystemExit(2) from None
