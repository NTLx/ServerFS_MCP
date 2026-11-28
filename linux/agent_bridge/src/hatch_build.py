"""Restrict platform-local wheels to their supported native platform."""

import platform
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # Use an explicit Python-only platform tag, not the build interpreter ABI.
        machine = platform.machine().lower()
        if sys.platform != "linux" or machine not in {"x86_64", "aarch64"}:
            raise RuntimeError("ServerFS Linux wheels require native Linux x86_64 or aarch64")
        build_data["tag"] = f"py3-none-linux_{machine}"
