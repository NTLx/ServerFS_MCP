"""Restrict platform-local wheels to their supported native platform."""

import platform
import sys

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        # Use an explicit Python-only platform tag, not the build interpreter ABI.
        if (
            sys.platform != "darwin"
            or platform.machine() != "arm64"
            or platform.mac_ver()[0].split(".")[0] != "27"
        ):
            raise RuntimeError("ServerFS macOS wheels require native macOS 27 arm64")
        build_data["tag"] = "py3-none-macosx_27_0_arm64"
