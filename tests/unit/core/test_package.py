"""Tests for the package root (U00-48)."""

from importlib import metadata

import pytest

import herness

pytestmark = pytest.mark.unit


def test_ut00_55_version_matches_metadata() -> None:
    """UT00-55 __version__ equals the installed distribution version."""
    assert herness.__version__ == metadata.version("herness")
