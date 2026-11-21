"""Pure Windows venv launcher/base interpreter attestation regression.

All paths and executables are disposable synthetic files. No services,
Task Scheduler entries, process launches, sockets or credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from serverfs_mcp.windows_process import registered_pythonw_image_matches


@pytest.fixture
def pythonw_paths(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    prefix = tmp_path / "core-owner-env"
    scripts = prefix / "Scripts"
    scripts.mkdir(parents=True)
    pythonw = scripts / "pythonw.exe"
    pythonw.write_bytes(b"virtual-environment-redirector")
    python = scripts / "python.exe"
    python.write_bytes(b"console-redirector")
    base = tmp_path / "CPython"
    base.mkdir()
    basew = base / "pythonw.exe"
    basew.write_bytes(b"installed-gui-interpreter")
    (base / "python.exe").write_bytes(b"installed-console-interpreter")
    return prefix, python, pythonw, base


def _verified(
    paths: tuple[Path, Path, Path, Path],
    *,
    image: Path | None = None,
    base_override: Path | None = None,
    entrypoint: bool = True,
    launcher_override: Path | None = None,
    executable_override: Path | None = None,
    prefix_override: Path | None = None,
    permit_peer_controller: bool = False,
) -> bool:
    prefix, python, pythonw, base = paths
    return registered_pythonw_image_matches(
        registered_launcher=launcher_override or pythonw,
        kernel_image=image or (base / "pythonw.exe"),
        venv_prefix=prefix_override or prefix,
        current_executable=executable_override or (pythonw if entrypoint else python),
        base_executable=base_override or (base / ("pythonw.exe" if entrypoint else "python.exe")),
        require_pythonw_entrypoint=entrypoint,
        permit_peer_controller=permit_peer_controller,
    )


@pytest.mark.parametrize("entrypoint", [True, False])
def test_exact_base_pythonw_redirector_is_accepted_for_runtime_and_listener(
    pythonw_paths, entrypoint: bool
) -> None:
    assert _verified(pythonw_paths, entrypoint=entrypoint)


@pytest.mark.parametrize("entrypoint", [True, False])
def test_direct_registered_pythonw_image_remains_accepted(pythonw_paths, entrypoint) -> None:
    assert _verified(pythonw_paths, image=pythonw_paths[2], entrypoint=entrypoint)


@pytest.mark.parametrize(
    "variant",
    ("foreign_image", "wrong_venv", "foreign_executable", "other_base", "missing_base"),
)
def test_foreign_or_untrusted_pythonw_is_rejected(pythonw_paths, tmp_path, variant) -> None:
    prefix, console, gui, base = pythonw_paths
    foreign = tmp_path / "foreign" / "pythonw.exe"
    foreign.parent.mkdir()
    foreign.write_bytes(b"foreign")
    other_base = foreign.parent / "python.exe"
    other_base.write_bytes(b"foreign-console")
    if variant == "foreign_image":
        assert not _verified(pythonw_paths, image=foreign)
    elif variant == "wrong_venv":
        assert not _verified(pythonw_paths, prefix_override=tmp_path / "wrong")
    elif variant == "foreign_executable":
        assert not _verified(pythonw_paths, executable_override=foreign)
    elif variant == "other_base":
        assert not _verified(pythonw_paths, base_override=other_base)
    else:
        assert not _verified(pythonw_paths, base_override=tmp_path / "missing/pythonw.exe")


def test_base_interpreter_inside_venv_is_not_an_external_trusted_base(pythonw_paths) -> None:
    prefix, _, gui, _ = pythonw_paths
    assert not _verified(pythonw_paths, base_override=gui)


def test_cannot_relabel_a_foreign_interpreter_as_registered_venv(pythonw_paths, tmp_path) -> None:
    _, _, gui, _ = pythonw_paths
    foreign_launcher = tmp_path / "foreign/Scripts/pythonw.exe"
    foreign_launcher.parent.mkdir(parents=True)
    foreign_launcher.write_bytes(b"foreign")
    assert not _verified(pythonw_paths, launcher_override=foreign_launcher)


def test_peer_connector_can_attest_core_base_without_trusting_peer_python(
    pythonw_paths, tmp_path: Path
) -> None:
    core_prefix, _, _, base = pythonw_paths
    (core_prefix / "pyvenv.cfg").write_text(
        f"home = {base}\ninclude-system-site-packages = false\n", encoding="utf-8"
    )
    peer_prefix = tmp_path / "connector-env"
    peer_console = peer_prefix / "Scripts" / "python.exe"
    peer_console.parent.mkdir(parents=True)
    peer_console.write_bytes(b"connector-console")
    unrelated_base = tmp_path / "unrelated-python" / "python.exe"
    unrelated_base.parent.mkdir()
    unrelated_base.write_bytes(b"unrelated-base")
    assert _verified(
        pythonw_paths,
        prefix_override=peer_prefix,
        executable_override=peer_console,
        base_override=unrelated_base,
        entrypoint=False,
        permit_peer_controller=True,
    )
    assert not _verified(
        pythonw_paths,
        prefix_override=peer_prefix,
        executable_override=peer_console,
        base_override=unrelated_base,
        entrypoint=False,
        permit_peer_controller=False,
    )


def test_peer_attestation_refuses_missing_or_forged_core_venv_home(
    pythonw_paths, tmp_path: Path
) -> None:
    core_prefix, _, _, base = pythonw_paths
    peer_prefix = tmp_path / "connector-env"
    peer_console = peer_prefix / "Scripts/python.exe"
    peer_console.parent.mkdir(parents=True)
    peer_console.write_bytes(b"console")

    def check() -> bool:
        return _verified(
            pythonw_paths,
            prefix_override=peer_prefix,
            executable_override=peer_console,
            entrypoint=False,
            permit_peer_controller=True,
        )

    assert not check()
    (core_prefix / "pyvenv.cfg").write_text(f"home = {base}\nhome = {tmp_path}\n", encoding="utf-8")
    assert not check()
    (core_prefix / "pyvenv.cfg").write_text("home = nonexistent-relative-home\n", encoding="utf-8")
    assert not check()
    (core_prefix / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    assert check()
