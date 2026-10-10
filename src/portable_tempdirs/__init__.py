"""Portable, owned temporary directories with conservative cleanup."""
from .core import (
    TemporaryDirectory,
    TemporaryDirectoryError,
    temporary_directory,
)

__all__ = ["TemporaryDirectory", "TemporaryDirectoryError", "temporary_directory"]
