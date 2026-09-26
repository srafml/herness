"""Static checks of pyproject.toml (U00-49, U00-50, U00-51, U00-53)."""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]

RUNTIME = {
    "duckdb": ">=1.3",
    "pyarrow": ">=17",
    "polars": ">=1.10",
    "numpy": "",
    "scipy": "",
    "pydantic": ">=2.9",
    "pydantic-settings": ">=2.5",
    "pyyaml": "",
    "jsonschema": "",
    "typer": ">=0.12",
    "structlog": ">=24",
    "rich": "",
    "httpx": ">=0.27",
    "httpx2": ">=2.13",  # R-06 egress clients for the httpx2-based SDKs (T10-17 ruling)
    "tenacity": ">=9",
    "tzdata": "",
    "psutil": "",
    "keyring": "",
    "pyahocorasick": "",
    "pymongo": ">=4.8",
    "snowflake-connector-python[pandas]": ">=3.12",
    "msal": ">=1.31",
    "torch": "",
    "sentence-transformers": ">=3",
    "scikit-learn": ">=1.5",
    "lancedb": ">=0.13",
    "rapidfuzz": ">=3",
    "openai": ">=1.50",
    "anthropic": ">=1,<2",
    "sqlglot": "",
    "ortools": ">=9.10",
    "jinja2": ">=3.1",
    "streamlit": ">=1.39",
}
DEV = {
    "pytest": ">=8.0",
    "pytest-asyncio": "",
    "pytest-cov": "",
    "pytest-benchmark": "",
    "pytest-timeout": "",
    "hypothesis": "",
    "respx": "",
    "freezegun": "",
    "mongomock": "",
    "ruff": "",
    "mypy": "",
    "pre-commit": "",
    "import-linter": ">=2.1",
    "pip-audit": "",
    "cyclonedx-bom": "",
    "detect-secrets": "",
    "types-PyYAML": "",
    "types-jsonschema": "",
    "types-psutil": "",
}
ENG_RUFF = [
    "E",
    "W",
    "F",
    "I",
    "N",
    "UP",
    "B",
    "A",
    "C4",
    "C90",
    "SIM",
    "PT",
    "PL",
    "RUF",
    "S",
    "DTZ",
    "TRY",
    "ASYNC",
    "PERF",
    "ANN",
    "BLE",
    "EM",
    "G",
    "LOG",
    "T20",
    "ERA",
]
_REQ = re.compile(r"^([A-Za-z0-9_.\-]+(?:\[[^\]]+\])?)(.*)$")


def _load() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _as_map(requirements: list[str]) -> dict[str, str]:
    out = {}
    for req in requirements:
        match = _REQ.match(req)
        assert match is not None, req
        out[match.group(1)] = match.group(2).strip()
    return out


def test_ut00_56_dependencies_match_table_14_1() -> None:
    """UT00-56 runtime and dev dependencies, extras, script and classifier match impl 00."""
    data = _load()
    project = data["project"]
    assert _as_map(project["dependencies"]) == RUNTIME
    assert _as_map(data["dependency-groups"]["dev"]) == DEV
    assert project["optional-dependencies"] == {
        "ner": ["presidio-analyzer", "spacy"],
        "pdf": ["weasyprint"],
    }
    assert project["scripts"] == {"herness": "herness.cli:main"}
    assert "Private :: Do Not Upload" in project["classifiers"]
    assert project["requires-python"] == ">=3.12,<3.13"


def test_ut00_57_tool_tables() -> None:
    """UT00-57 ruff, mypy, pytest and coverage settings match impl 00 §3.6."""
    tool = _load()["tool"]
    lint = tool["ruff"]["lint"]
    assert lint["select"] == [*ENG_RUFF, "TID251", "ICN003"]
    assert lint["mccabe"]["max-complexity"] == 10
    assert lint["pylint"]["max-args"] == 6
    banned = lint["flake8-tidy-imports"]["banned-api"]
    for name in (
        "pickle",
        "yaml.load",
        "httpx.Client",
        "httpx.AsyncHTTPTransport",
        "datetime.datetime.utcnow",
    ):
        assert name in banned
    assert lint["flake8-import-conventions"]["banned-from"] == ["herness.core.time"]
    mypy = tool["mypy"]
    assert mypy["strict"] is True
    existing = {name for name in ("herness", "app", "tools") if (ROOT / name).is_dir()}
    assert set(mypy["files"]) == existing
    pytest_cfg = tool["pytest"]["ini_options"]
    assert len(pytest_cfg["markers"]) == 6
    assert "--strict-markers" in pytest_cfg["addopts"]
    assert tool["coverage"]["run"]["branch"] is True
