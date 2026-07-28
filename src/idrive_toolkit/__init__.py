"""Python client utilities for the iDrive backend."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("idrive_toolkit")
except PackageNotFoundError:
    __version__ = "0+unknown"
