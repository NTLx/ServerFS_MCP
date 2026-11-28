"""Restrict platform-local wheels to their supported native platform."""

import platform
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # Use an explicit Python-only platform tag, not the build interpreter ABI.
        if sys.platform != "win32" or platform.machine().lower() != "amd64":
            raise RuntimeError("ServerFS Windows wheels require native Windows x64")
        build_data["tag"] = "py3-none-win_amd64"
