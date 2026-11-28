"""ServerFS Agent Bridge package."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("serverfs-agent-bridge")
except PackageNotFoundError:
    # Only reachable when this package is imported from a source checkout without an
    # installed distribution -- the root-suite test environment is exactly that shape.
    # The manifest version is only meaningful for an installed one, so the fallback is
    # an explicit unknown marker rather than a fabricated release number.
    __version__ = "0.0.0+source"
