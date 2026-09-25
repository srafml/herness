"""ST00-11: .env.example holds no values and .gitignore keeps secrets and data out."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]


def test_st00_11_env_example_and_gitignore() -> None:
    """ST00-11 every .env.example value is empty, no security key, and .gitignore entries."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    pairs = [line for line in lines if line and not line.startswith("#")]
    assert pairs, "no keys in .env.example"
    for line in pairs:
        key, sep, value = line.partition("=")
        assert sep == "=", line
        assert value == "", line
        assert not key.startswith("HERNESS_SECURITY__"), line
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", "!.env.example", "data/"):
        assert entry in ignored
