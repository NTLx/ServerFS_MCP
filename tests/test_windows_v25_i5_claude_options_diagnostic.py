"""I5 safe post-failure SDK diagnostic contracts (no network/provider)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "deployment"
    / "windows"
    / "v0250-i5-claude-options-readonly-diagnostic.py"
)


def _module():
    spec = importlib.util.spec_from_file_location("i5_sdk_options_diagnostic", _SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_diagnostic_never_discloses_ambient_values() -> None:
    module = _module()
    assert module.NESTED_NAMES == (
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_SESSION_ID",
    )
    with pytest.raises(module.I5OptionsDiagnosticError):
        module.require(False, "unavailable")


def test_diagnostic_only_instantiates_sdk_without_execution() -> None:
    source = _SOURCE.read_text("utf-8")
    for required in (
        "dataclasses.fields(ClaudeAgentOptions)",
        "ClaudeSDKClient(options)",
        "setting_sources=[]",
        "allowed_tools=[]",
        "disallowed_tools=[",
        "max_turns=1",
        "can_use_tool=deny",
        "any(name in os.environ for name in NESTED_NAMES)",
        "I5_CLAUDE_OPTIONS_CONTRACT=PASS",
        "I5_CLAUDE_SDK_CONSTRUCTOR=PASS",
        "I5_CLAUDE_MODEL_CALL=NOT_RUN",
    ):
        assert required in source
    for forbidden in (
        "client.connect(",
        "client.query(",
        "receive_response(",
        "subprocess.",
        "urllib.",
        "requests.",
        'os.environ.get("ANTHROPIC',
        "Scheduler()",
        "git push",
        "git commit",
    ):
        assert forbidden not in source
