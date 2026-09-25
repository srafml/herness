"""ST00-09: ruff bans pickle, unsafe YAML, direct httpx clients and from-imports of time."""

import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]

X_PY = """\
import pickle

import httpx
import yaml
from herness.core.time import now


def f(s: str) -> object:
    return pickle, yaml.load(s), httpx.Client(), now
"""

Y_PY = """\
import httpx


def g(url: str) -> object:
    return httpx.AsyncHTTPTransport(), httpx.get(url)
"""


def test_st00_09_ruff_bans(tmp_path: Path) -> None:
    """ST00-09 banned APIs are reported in herness/ and in connectors (no exception, R-06)."""
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    (tmp_path / "herness" / "connectors").mkdir(parents=True)
    (tmp_path / "herness" / "x.py").write_text(X_PY, encoding="utf-8")
    (tmp_path / "herness" / "connectors" / "y.py").write_text(Y_PY, encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format",
            "json",
            "--config",
            str(tmp_path / "pyproject.toml"),
            "herness/x.py",
            "herness/connectors/y.py",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode != 0
    findings = json.loads(result.stdout)
    per_file: dict[str, Counter[str]] = {}
    for item in findings:
        name = Path(item["filename"]).name
        per_file.setdefault(name, Counter())[item["code"]] += 1
    assert per_file["x.py"]["TID251"] == 3
    assert per_file["x.py"]["ICN003"] == 1
    assert per_file["y.py"]["TID251"] == 2
