"""Herness: local-first IT operations analytics and agent harness."""

from importlib import metadata
from typing import Final


def _installed_version() -> str:
    try:
        return metadata.version("herness")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


__version__: Final[str] = _installed_version()
