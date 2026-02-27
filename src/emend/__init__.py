"""emend - Python refactoring CLI tool."""

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("emend")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
