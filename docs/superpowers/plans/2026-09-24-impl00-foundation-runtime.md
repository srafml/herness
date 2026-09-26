# Foundation Runtime (impl 00, part 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the L0 foundation every other Herness spec imports: project metadata and tooling configuration, the error taxonomy, the UTC clock, identifiers and `query_id`, structured JSON logging, the shared-types package with its ownership checker, the import-layer contracts, and the shared numbers module.

**Architecture:** One `herness` package managed by `uv`, Python 3.12, mypy strict. `herness/core/` holds seven small pure-Python modules layered strictly `errors → time | numbers → ids → types | _log_pipeline → logging` (contract C3). Logging runs structlog through the standard-library `logging` module so one JSON-lines formatter chain handles structlog events and third-party records. `herness/core/types/` is a re-export package whose submodules are created later by their owner specs. A static AST checker enforces type ownership.

**Tech Stack:** Python 3.12, uv 0.11.8, hatchling, structlog ≥ 24, tzdata, pytest ≥ 8 (+ asyncio, timeout, benchmark, cov), Hypothesis, freezegun, ruff, mypy (pydantic plugin), import-linter ≥ 2.1.

**Spec:** `docs/impl/00-foundation.impl.md` (impl 00). It is governed by `docs/impl/ENG-STANDARDS.md` (ENG) and `docs/impl/DECISIONS.md` (rulings `R-nn`). Unit IDs (`U00-nn`), test IDs (`UT00-nn`, `PT`, `FT`, `ST`, `IT`, `BT`) and task cards (`T00-nn`) below refer to impl 00; read the unit spec named in each task before coding it.

**Scope:** task cards T00-01 … T00-09 and T00-16. The CI tooling cards T00-10 … T00-15 (module-size, traceability, audit and licence checks, pre-commit, CI and release workflows) are a separate plan written after this one lands.

## Global Constraints

- Python `>=3.12,<3.13`; `[tool.uv] required-version = "==0.11.8"` (the uv on this machine; O-07).
- All installs from `uv.lock`: `uv sync --frozen`; editable install in development (R-58).
- `mypy --strict` must report 0 errors on every commit; `ruff check` and `ruff format --check` must report 0 issues.
- Line budgets (impl 00 §2): `errors.py` ≤ 380, `time.py` ≤ 200, `ids.py` ≤ 330, `numbers.py` ≤ 320, `logging.py` ≤ 260, `_log_pipeline.py` ≤ 360, `types/__init__.py` ≤ 150, `types/_ownership.py` ≤ 120, `tools/check_type_ownership.py` ≤ 390. Function complexity ≤ 10 (ruff `C901`), ≤ 6 arguments (`PLR0913`).
- Coverage of each `herness/core` file ≥ 90 % line and ≥ 85 % branch.
- Every test function name contains its ID with `_` (for example `test_ut00_55_...`) so `pytest -k UT00_55` selects it; the first line of its docstring starts with the ID (`"""UT00-55 ..."""`). Every test file sets module-level `pytestmark` (`pytest.mark.unit`, or `integration`, or `[integration, slow]` for benchmarks).
- Ruff `EM101/EM102` is on: assign the message to `msg` before `raise`. Ruff `TRY003` is ignored globally.
- Before every commit, run `uv run ruff format .` and then `uv run ruff check --fix .`, and fix any remaining finding without changing behaviour. The `# noqa` comments in this plan are best guesses: `ruff check --fix` removes any that turn out unused (`RUF100`), and a finding the plan did not foresee gets a fix or a `# noqa: <code>` with its reason.
- Import `herness.core.time` only as `from herness.core import time as clock` (ruff `ICN003` bans `from herness.core.time import …`).
- No `print` (ruff `T20`); CLI tools write with `sys.stdout.write` / `sys.stderr.write` and return exit codes 0 pass, 1 findings, 2 usage or input error (R-73).
- No secret value, ticket text or personal data in any error message, `hint`, `details` or log field (ENG §3.4).
- Commit messages follow Conventional Commits and end with the line `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`.

### Deliberate deviations from the letter of impl 00 (reviewer: confirm or reject)

1. **Log chain order.** `add_component` runs **before** `ProcessorFormatter.remove_processors_meta`, not after it (§3.4 steps 5–6). `add_component` reads `event_dict["_record"]`, which `remove_processors_meta` deletes, so the spec order cannot work.
2. **"Listed if and only if it exists" applied to tool paths.** mypy `files` and import-linter `root_packages` list only the directories that exist (`herness` at first, then `tools`; `app` arrives with impl 09). Contracts C2 and C4 name only existing modules, and C4 is omitted while it would have no forbidden module. mypy and import-linter both fail on a missing path.
3. **C1 `unmatched_ignore_imports_alerting = "none"`.** import-linter errors on an `ignore_imports` entry that matches nothing (its default). The settings exception `herness.core.config -> herness.**.settings` matches nothing until impl 10 creates `herness.core.config`.
4. **Parameter types widened for runtime checks.** `ensure_utc(dt: datetime.date)` and `parse_utc(text: object)`. mypy strict would otherwise flag the spec's run-time type checks as unreachable.
5. **ASCII-only digit classes** (`re.ASCII`) in the timestamp, date and build-ID patterns, so full-width digits are rejected (ST00-14). `time.zone` also maps `OSError` (a zone name that is a tzdata directory) to `ConfigError`.
6. **`README.md` stub in Task 1.** hatchling needs the file to build the editable install. Task 2 writes the full content.
7. **`get_logger` stays lazy (spec defect in U00-38).** It passes `component` as an initial value to `structlog.stdlib.get_logger` instead of calling `.bind(component=...)`. `.bind()` on structlog's lazy proxy resolves the configuration immediately, so a logger created at import time would ignore `configure_logging` (Review Focus 5).
8. **`pythonpath = ["."]` in `[tool.pytest.ini_options]`.** The `pytest` entry point does not put the repository root on `sys.path`, and the tool tests import `tools.*`.

## Review Focus

These five inputs are implied by impl 00 but no spec test exercises them; each has a test added to its owning task:

1. **Display strings passed as `NumberRef.value`** (`"1,250"`, `"$5"`): `format_value` must return `n/a`, never raise or print a wrong number. Test `test_rf_display_strings_are_not_numbers` in Task 10.
2. **Aware non-UTC times near midnight** given to `new_build_id` and `utc_day`: the date part must be the UTC date. Tests `test_rf_build_id_uses_utc_date` in Task 5 and `test_rf_utc_day_non_utc_input` in Task 4.
3. **Operator timestamps with surrounding whitespace or a lowercase `z`** (`" 2026-09-24T10:00:00z "`): `parse_iso` must accept them. Test `test_rf_parse_iso_whitespace_and_lowercase_z` in Task 4.
4. **Signed, currency and attached-percent numerals** (`$1,200`, `-5`, `12%`): the scanner's hit text keeps the sign or symbol, so the operator sees what was flagged. Test `test_rf_hits_keep_sign_and_symbol` in Task 10.
5. **A logger created at import time, before `configure_logging`:** its later lines must still be JSON with `component` (loggers are not cached). Test `test_rf_logger_created_before_configure` in Task 7.

## File Structure

| File | Responsibility | Task |
|------|----------------|------|
| `pyproject.toml` | metadata, dependencies, uv, hatch, ruff, mypy, pytest, coverage, import-linter | 1, 8, 9, 10 |
| `uv.lock` | generated lock | 1 |
| `herness/__init__.py`, `herness/py.typed`, `herness/core/__init__.py` | package roots, `__version__` | 1 |
| `README.md`, `.gitignore`, `.gitattributes`, `.env.example` | hygiene and onboarding | 1 (README stub), 2 |
| `herness/core/errors.py` | error taxonomy, `error_kind`, `to_log_fields` | 3 |
| `herness/core/time.py` | clock, sleeps, UTC text, zones | 4 |
| `herness/core/ids.py` | ULIDs, prefixed IDs, `build_id`, `record_id`, canonical JSON, `query_id`, tokens | 5 |
| `herness/core/_log_pipeline.py` | logging constants, processors, `DailyJsonlHandler` | 6 |
| `herness/core/logging.py` | `configure_logging`, `get_logger`, `bind_ids`, `reset_logging` | 7 |
| `herness/core/types/__init__.py`, `herness/core/types/_ownership.py` | re-export point and ownership tables | 8 |
| `tools/__init__.py`, `tools/check_type_ownership.py` | ownership checker CLI | 8 |
| `herness/core/numbers.py` | markers, numeral scanner, `NumberRef` formatting | 10 |
| `tests/unit/core/*`, `tests/unit/repo/*`, `tests/unit/tools/*`, `tests/integration/repo/*`, `tests/bench/test_core_bench.py` | tests | all |

---

## Task 0: Branch

- [ ] **Step 1: Create the working branch**

Run: `git switch -c feat/impl00-foundation-runtime`
Expected: `Switched to a new branch 'feat/impl00-foundation-runtime'`

---

## Task 1: Project metadata and package roots (T00-01)

Units U00-48, U00-49, U00-50, U00-51, U00-53. Tests UT00-55, UT00-56, UT00-57, ST00-09.

**Files:**
- Create: `pyproject.toml`, `herness/__init__.py`, `herness/core/__init__.py`, `herness/py.typed`, `README.md` (stub)
- Generate: `uv.lock`
- Test: `tests/unit/core/test_package.py`, `tests/unit/repo/test_pyproject.py`, `tests/integration/repo/test_ruff_bans.py`

**Interfaces:**
- Consumes: nothing.
- Produces: an importable, installed `herness` package; `herness.__version__: str`; the tool configuration every later task runs under.

- [ ] **Step 1: Install Python 3.12 for uv**

Run: `uv python install 3.12`
Expected: a line reporting an installed `cpython-3.12.x` (or "already installed").

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling>=1.25,<2"]
build-backend = "hatchling.build"

[project]
name = "herness"
version = "0.1.0"
description = "Local-first IT operations analytics and agent harness"
readme = "README.md"
requires-python = ">=3.12,<3.13"
license = "LicenseRef-Proprietary"
classifiers = [
    "Private :: Do Not Upload",
    "Programming Language :: Python :: 3.12",
    "Operating System :: Microsoft :: Windows",
    "Operating System :: POSIX :: Linux",
]
dependencies = [
    "duckdb>=1.3",
    "pyarrow>=17",
    "polars>=1.10",
    "numpy",
    "scipy",
    "pydantic>=2.9",
    "pydantic-settings>=2.5",
    "pyyaml",
    "jsonschema",
    "typer>=0.12",
    "structlog>=24",
    "rich",
    "httpx>=0.27",
    "tenacity>=9",
    "tzdata",
    "psutil",
    "keyring",
    "pyahocorasick",
    "pymongo>=4.8",
    "snowflake-connector-python[pandas]>=3.12",
    "msal>=1.31",
    "torch",
    "sentence-transformers>=3",
    "scikit-learn>=1.5",
    "lancedb>=0.13",
    "rapidfuzz>=3",
    "openai>=1.50",
    "anthropic>=1,<2",
    "sqlglot",
    "ortools>=9.10",
    "jinja2>=3.1",
    "streamlit>=1.39",
]

[project.optional-dependencies]
ner = ["presidio-analyzer", "spacy"]
pdf = ["weasyprint"]

[project.scripts]
herness = "herness.cli:main"

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio",
    "pytest-cov",
    "pytest-benchmark",
    "pytest-timeout",
    "hypothesis",
    "respx",
    "freezegun",
    "mongomock",
    "ruff",
    "mypy",
    "pre-commit",
    "import-linter>=2.1",
    "pip-audit",
    "cyclonedx-bom",
    "detect-secrets",
    "types-PyYAML",
    "types-jsonschema",
    "types-psutil",
]

[tool.uv]
required-version = "==0.11.8"
default-groups = ["dev"]

[[tool.uv.index]]
name = "pytorch-cu128"
url = "https://download.pytorch.org/whl/cu128"
explicit = true

[tool.uv.sources]
torch = [{ index = "pytorch-cu128", marker = "sys_platform == 'win32' or sys_platform == 'linux'" }]

[tool.hatch.build.targets.wheel]
packages = ["herness"]

[tool.hatch.build.targets.sdist]
include = ["herness", "README.md", "pyproject.toml", "uv.lock"]

[tool.ruff]
line-length = 100
target-version = "py312"
src = ["herness", "app", "tools", "tests"]
extend-exclude = ["data", "docs"]

[tool.ruff.lint]
select = [
    "E", "W", "F", "I", "N", "UP", "B", "A", "C4", "C90", "SIM", "PT", "PL", "RUF", "S",
    "DTZ", "TRY", "ASYNC", "PERF", "ANN", "BLE", "EM", "G", "LOG", "T20", "ERA",
    "TID251", "ICN003",
]
ignore = ["TRY003"]

[tool.ruff.lint.per-file-ignores]
"tests/**" = ["S101", "PLR2004", "S311", "ANN"]
"herness/core/errors.py" = ["N818"]
"herness/core/logging.py" = ["A005"]
"herness/core/time.py" = ["A005"]
"herness/core/numbers.py" = ["A005"]
"herness/core/types/__init__.py" = ["A005"]
"herness/core/egress.py" = ["TID251"]

[tool.ruff.lint.mccabe]
max-complexity = 10

[tool.ruff.lint.pylint]
max-args = 6

[tool.ruff.lint.isort]
known-first-party = ["herness", "app", "tools"]

[tool.ruff.lint.flake8-tidy-imports.banned-api]
"pickle".msg = "ENG §3.5: no pickle, marshal or shelve"
"marshal".msg = "ENG §3.5: no pickle, marshal or shelve"
"shelve".msg = "ENG §3.5: no pickle, marshal or shelve"
"yaml.load".msg = "use yaml.safe_load (ENG §3.5)"
"yaml.unsafe_load".msg = "use yaml.safe_load (ENG §3.5)"
"yaml.full_load".msg = "use yaml.safe_load (ENG §3.5)"
"requests".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"urllib.request".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.Client".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.AsyncClient".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.HTTPTransport".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.AsyncHTTPTransport".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.request".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.stream".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.get".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.post".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.put".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.patch".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.delete".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.head".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"httpx.options".msg = "HTTP clients and transports only via herness.core.egress (R-06; loopback model servers via egress.loopback_http_client)"
"datetime.datetime.utcnow".msg = "naive datetime; use herness.core.time"
"datetime.datetime.utcfromtimestamp".msg = "naive datetime; use herness.core.time"

[tool.ruff.lint.flake8-import-conventions]
banned-from = ["herness.core.time"]

[tool.ruff.format]
quote-style = "double"
line-ending = "lf"

[tool.mypy]
python_version = "3.12"
strict = true
files = ["herness"]
plugins = ["pydantic.mypy"]
warn_unreachable = true
enable_error_code = ["ignore-without-code", "redundant-expr", "truthy-bool"]

[[tool.mypy.overrides]]
module = ["app.*", "tools.*"]
disallow_untyped_decorators = false

[tool.pydantic-mypy]
init_forbid_extra = true
init_typed = true
warn_required_dynamic_aliases = true

[tool.pytest.ini_options]
minversion = "8.0"
testpaths = ["tests"]
pythonpath = ["."]
addopts = "--strict-markers --strict-config -ra"
markers = [
    "unit: no I/O beyond tmp files, no subprocess, no network",
    "integration: real DuckDB, SQLite and LanceDB on tiny or small builds, fakes and stubs",
    "fault: HERNESS_ENV=test, fault plans, subprocess kills",
    "eval: golden or classifier evaluation",
    "gpu: needs vLLM, OpenJev or Laya on CUDA",
    "slow: over 30 s per test, or small or full scale",
]
asyncio_mode = "strict"
timeout = 300
filterwarnings = ["error:::herness"]

[tool.coverage.run]
branch = true
source = ["herness"]

[tool.coverage.report]
exclude_also = ["if TYPE_CHECKING:", "\\.\\.\\.$", "# pragma: gpu"]
show_missing = true

[tool.coverage.json]
output = "coverage.json"

[tool.coverage.xml]
output = "coverage.xml"
```

- [ ] **Step 3: Write the package roots and the README stub**

`herness/__init__.py`:

```python
"""Herness: local-first IT operations analytics and agent harness."""

from importlib import metadata
from typing import Final


def _installed_version() -> str:
    try:
        return metadata.version("herness")
    except metadata.PackageNotFoundError:
        return "0.0.0+unknown"


__version__: Final[str] = _installed_version()
```

`herness/core/__init__.py`:

```python
"""Foundation package (impl 00): errors, time, ids, numbers, logging and shared types."""
```

`herness/py.typed`: an empty file.

`README.md` (Task 2 replaces it):

```markdown
# Herness

Local-first IT operations analytics and agent harness. Full README: Task 2 of the impl 00 plan.
```

- [ ] **Step 4: Lock and sync**

Run: `uv lock` then `uv sync --frozen --all-extras`
Expected: both succeed; `uv.lock` exists.
If `uv lock` reports that no version satisfies a bound (for example `anthropic>=1,<2`), stop and report the package, the bound and the newest available version. Don't change the bound yourself: the bounds come from impl 00 §14.1.

- [ ] **Step 5: Write the failing tests**

`tests/unit/core/test_package.py`:

```python
"""Tests for the package root (U00-48)."""

from importlib import metadata

import pytest

import herness

pytestmark = pytest.mark.unit


def test_ut00_55_version_matches_metadata() -> None:
    """UT00-55 __version__ equals the installed distribution version."""
    assert herness.__version__ == metadata.version("herness")
```

`tests/unit/repo/test_pyproject.py`:

```python
"""Static checks of pyproject.toml (U00-49, U00-50, U00-51, U00-53)."""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]

RUNTIME = {
    "duckdb": ">=1.3", "pyarrow": ">=17", "polars": ">=1.10", "numpy": "", "scipy": "",
    "pydantic": ">=2.9", "pydantic-settings": ">=2.5", "pyyaml": "", "jsonschema": "",
    "typer": ">=0.12", "structlog": ">=24", "rich": "", "httpx": ">=0.27", "tenacity": ">=9",
    "tzdata": "", "psutil": "", "keyring": "", "pyahocorasick": "", "pymongo": ">=4.8",
    "snowflake-connector-python[pandas]": ">=3.12", "msal": ">=1.31", "torch": "",
    "sentence-transformers": ">=3", "scikit-learn": ">=1.5", "lancedb": ">=0.13",
    "rapidfuzz": ">=3", "openai": ">=1.50", "anthropic": ">=1,<2", "sqlglot": "",
    "ortools": ">=9.10", "jinja2": ">=3.1", "streamlit": ">=1.39",
}
DEV = {
    "pytest": ">=8.0", "pytest-asyncio": "", "pytest-cov": "", "pytest-benchmark": "",
    "pytest-timeout": "", "hypothesis": "", "respx": "", "freezegun": "", "mongomock": "",
    "ruff": "", "mypy": "", "pre-commit": "", "import-linter": ">=2.1", "pip-audit": "",
    "cyclonedx-bom": "", "detect-secrets": "", "types-PyYAML": "", "types-jsonschema": "",
    "types-psutil": "",
}
ENG_RUFF = [
    "E", "W", "F", "I", "N", "UP", "B", "A", "C4", "C90", "SIM", "PT", "PL", "RUF", "S",
    "DTZ", "TRY", "ASYNC", "PERF", "ANN", "BLE", "EM", "G", "LOG", "T20", "ERA",
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
    for name in ("pickle", "yaml.load", "httpx.Client", "httpx.AsyncHTTPTransport",
                 "datetime.datetime.utcnow"):
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
```

`tests/integration/repo/test_ruff_bans.py`:

```python
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
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--output-format", "json",
         "--config", str(tmp_path / "pyproject.toml"),
         "herness/x.py", "herness/connectors/y.py"],
        cwd=tmp_path, capture_output=True, text=True, check=False, timeout=120,
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
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/unit/core/test_package.py tests/unit/repo/test_pyproject.py tests/integration/repo/test_ruff_bans.py -v`
Expected: 4 passed. The configuration was written in Step 2, so these tests pin it. If one fails, fix `pyproject.toml`, not the test.

- [ ] **Step 7: Run the quality gates**

Run: `uv run ruff check .` then `uv run ruff format --check .` then `uv run mypy`
Expected: `All checks passed!`, no files to reformat, `Success: no issues found`. Apply `uv run ruff format .` if formatting differs.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock README.md herness tests
git commit -m "build: add project metadata, tooling config and package roots (T00-01)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Repository hygiene files (T00-02)

Units U00-61, U00-62, U00-63. Test ST00-11.

**Files:**
- Modify: `.gitignore` (replace), `README.md` (replace the stub)
- Create: `.gitattributes`, `.env.example`
- Test: `tests/unit/repo/test_hygiene.py`

**Interfaces:**
- Consumes: Task 1 (`pyproject.toml`).
- Produces: ignore rules later tasks rely on (`data/`, caches, `coverage.json`).

- [ ] **Step 1: Write the failing test**

`tests/unit/repo/test_hygiene.py`:

```python
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
        assert sep == "=" and value == "", line
        assert not key.startswith("HERNESS_SECURITY__"), line
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".env", "!.env.example", "data/"):
        assert entry in ignored
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/unit/repo/test_hygiene.py -v`
Expected: FAIL with `FileNotFoundError` for `.env.example`.

- [ ] **Step 3: Write the files**

`.gitignore` (replace the whole file):

```gitignore
data/
.env
!.env.example
.venv/
build/
dist/
*.egg-info/
__pycache__/
*.py[cod]
.mypy_cache/
.ruff_cache/
.pytest_cache/
.hypothesis/
.benchmarks/
.import_linter_cache/
.coverage
.coverage.*
coverage.xml
coverage.json
htmlcov/
*.duckdb
*.duckdb.wal
*.sqlite
*.sqlite-wal
*.sqlite-shm
docker.env
.idea/
.vscode/
.DS_Store
Thumbs.db
```

`.gitattributes`:

```gitattributes
* text=auto eol=lf
*.ps1 text eol=crlf
*.bat text eol=crlf
*.cmd text eol=crlf
*.parquet binary
*.duckdb binary
*.sqlite binary
*.png binary
*.jpg binary
*.pdf binary
*.xlsx binary
*.safetensors binary
*.whl binary
```

`.env.example`:

```dotenv
# Copy to .env for development only. Never commit .env.
# .env is read only when the process environment has HERNESS_ENV=dev (spec 10 §4.1); set HERNESS_ENV in the shell, not in this file.
# security.* keys cannot be set here (spec 10 §4.1).
# The dotenv secrets backend is allowed only with HERNESS_ENV=dev or profile synth.

# Config profile (default: local)
HERNESS_PROFILE=
# Log level
HERNESS_LOGGING__LEVEL=
# Secret vllm.api_key
HERNESS_SECRET__VLLM_API_KEY=
# Secret OPENJEV_API_KEY
HERNESS_SECRET__OPENJEV_API_KEY=
# Secret redact.hmac_key (64 hex characters)
HERNESS_SECRET__REDACT_HMAC_KEY=
# Secret ui_user_ref_key
HERNESS_SECRET__UI_USER_REF_KEY=
# Optional synthetic-generator config fragment path (spec 10 §4.3)
HERNESS_SYNTH_CONFIG=
```

`README.md` (replace the stub; the 12 sections of U00-63, in order):

````markdown
# Herness

## 1. Herness

Herness analyses IT service-management, change, monitoring and delivery data and produces evidence-backed answers to two questions: what to fund next, and which groups should improve and how. It follows six principles ([design 00 §2](docs/specs/00-overview-and-contracts.md#2-principles-binding-on-every-component)): numbers come from SQL; every number is traceable; a deterministic core with pluggable edges; idempotent and resumable jobs; local by default; small, plain Python.

## 2. Status

Phase 1 (foundation) in progress. Open decisions: [docs/specs/open-questions.md](docs/specs/open-questions.md).

## 3. Requirements

- Windows 11 Pro with WSL2 (Linux supported).
- Python 3.12 through `uv` (the version pinned in `pyproject.toml`, `[tool.uv] required-version`).
- An NVIDIA GPU with 24 GB or more for Phases 3–4 (design 10).

## 4. Quick start (development)

```bash
uv sync --frozen                 # installs herness in editable mode (R-58)
uv run pre-commit install
export HERNESS_ENV=dev           # PowerShell: $env:HERNESS_ENV = "dev"
cp .env.example .env             # optional
uv run pytest -m unit
uv run python tools/synth_data.py --seed 7 --scale tiny
uv run herness config validate --profile synth
```

## 5. Repository layout

See [design 00 §3](docs/specs/00-overview-and-contracts.md#3-runtime-and-repository-layout) and the module map (§2) of each implementation spec in [docs/impl/](docs/impl/).

## 6. Configuration and secrets

See [design 10](docs/specs/10-config-security-deployment.md). Secrets live in Windows Credential Manager (`herness secrets set`). Never put secrets in files.

## 7. Development workflow

Work is organised as task cards in the implementation specs. A card is done when it meets ENG §8 (definition of done) and passes the quality gates of ENG §7. Run the checks locally with `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run lint-imports`, `uv run python -m tools.check_type_ownership` and `uv run pytest -m unit`.

## 8. Testing

Markers and selections are defined in design 11 §4.1–4.2: `unit`, `integration`, `fault`, `eval`, `gpu`, `slow`. Commit gate: `pytest -m unit`. Push gate: `pytest -m "(unit or integration or fault) and not gpu and not slow"`.

## 9. CI and releases

The `main` branch is protected: the hosted CI checks must pass, and releases are cut from signed annotated tags `vX.Y.Z`. A release produces the wheel, the sdist, a CycloneDX SBOM and signed build provenance. Verify an attestation with `gh attestation verify <wheel> --repo <owner>/<repo>`. The target box installs only the release wheel, through `herness deploy install`, which runs that verification and the SBOM check first (R-58, design 10).

## 10. Security

Report vulnerabilities internally to the project owner; do not open public issues. Engineering security rules: [ENG §5](docs/impl/ENG-STANDARDS.md).

## 11. Documentation index

- [docs/architecture.html](docs/architecture.html)
- [docs/specs/](docs/specs/): design specs
- [docs/impl/](docs/impl/): implementation specs, decisions and engineering standards

## 12. Licence

Proprietary. Internal use only.
````

- [ ] **Step 4: Run the test and the git checks**

Run: `uv run pytest tests/unit/repo/test_hygiene.py -v`
Expected: PASS.
Run: `git check-ignore data/x .env` then `git check-ignore .env.example`
Expected: the first prints `data/x` and `.env`; the second prints nothing and exits 1.

- [ ] **Step 5: Commit**

```bash
git add .gitignore .gitattributes .env.example README.md tests/unit/repo/test_hygiene.py
git commit -m "chore: add repository hygiene files and README (T00-02)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
## Task 3: Error taxonomy (T00-03)

Units U00-01 … U00-08 (read impl 00 §3.1). Tests UT00-01 … UT00-08, UT00-71, PT00-01, ST00-13.

**Files:**
- Create: `herness/core/errors.py`
- Test: `tests/unit/core/test_errors.py`

**Interfaces:**
- Consumes: nothing (standard library only).
- Produces: `HernessError(message, /, *, hint=None, details=None, **context)` with `.message: str`, `.hint: str | None`, `.context: Mapping[str, Scalar]`, `.details: Mapping[str, str]`; categories `RetryableError`, `RecoverableError`, `FatalError`; leaves `SourceUnavailable`, `ModelUnavailable`, `StoreBusy`, `RateLimited(retry_after=)`, `CircuitOpen(key=, retry_at=)`, `OutputValidationError`, `ToolInputError`, `QueryError`, `ModelRefused(category=)`, `PolicyViolation`, `ReportContractError`, `NotFound`, `ConfigError`, `AuthError`, `SchemaViolation`, `BudgetExceeded`, `PermissionDenied`, `EgressBlocked`; `error_kind(exc) -> Literal["retryable","recoverable","fatal","unknown"]`; `to_log_fields(exc) -> dict[str, Scalar]`; type alias `Scalar = str | int | float | bool | None`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/core/test_errors.py`:

```python
"""Tests for herness.core.errors (U00-01 … U00-08)."""

import datetime
import json
import math
import pickle  # noqa: TID251 - proves multiprocessing transport (UT00-08)
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core import errors as e

pytestmark = pytest.mark.unit

PARENTS = {
    e.RetryableError: e.HernessError, e.RecoverableError: e.HernessError,
    e.FatalError: e.HernessError,
    e.SourceUnavailable: e.RetryableError, e.RateLimited: e.RetryableError,
    e.ModelUnavailable: e.RetryableError, e.StoreBusy: e.RetryableError,
    e.CircuitOpen: e.RetryableError,
    e.OutputValidationError: e.RecoverableError, e.ToolInputError: e.RecoverableError,
    e.QueryError: e.RecoverableError, e.ModelRefused: e.RecoverableError,
    e.PolicyViolation: e.RecoverableError, e.ReportContractError: e.RecoverableError,
    e.NotFound: e.RecoverableError,
    e.ConfigError: e.FatalError, e.AuthError: e.FatalError, e.SchemaViolation: e.FatalError,
    e.BudgetExceeded: e.FatalError, e.PermissionDenied: e.FatalError,
    e.EgressBlocked: e.FatalError,
}
RETRY_AT = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)


def _instance(cls: type[e.HernessError]) -> e.HernessError:
    if cls is e.RateLimited:
        return e.RateLimited("rl", retry_after=2.5, source="jira")
    if cls is e.CircuitOpen:
        return e.CircuitOpen("open", key="jira", retry_at=RETRY_AT, source="jira")
    if cls is e.ModelRefused:
        return e.ModelRefused("refused", category="cyber", model="m")
    return cls("boom", job_id="job_x", n=3)


def test_ut00_01_class_parents() -> None:
    """UT00-01 each class has the design 00 §7 parent; 3 categories and 18 leaves."""
    for cls, parent in PARENTS.items():
        assert cls.__bases__ == (parent,), cls.__name__
    leaves = [c for c, p in PARENTS.items() if p is not e.HernessError]
    assert len(leaves) == 18
    assert e.NotFound.__bases__ == (e.RecoverableError,)


def test_ut00_02_bounds_message_and_context() -> None:
    """UT00-02 message and string context are bounded; objects become type tags."""
    err = e.HernessError("m" * 1500, job_id="job_x", obj=object(), long="a" * 300)
    assert err.message == "m" * 1000 + "…"
    assert err.context["obj"] == "<object>"
    assert err.context["long"] == "a" * 200 + "…"
    assert str(err) == err.message
    with pytest.raises(TypeError):
        err.context["job_id"] = "x"  # type: ignore[index]


@pytest.mark.parametrize(
    ("given_value", "expected"),
    [(7.0, 7.0), (-3, 0.0), (math.nan, None), (math.inf, None), (None, None)],
)
def test_ut00_03_rate_limited_retry_after(given_value: Any, expected: float | None) -> None:
    """UT00-03 retry_after is normalised to None or a finite float >= 0."""
    assert e.RateLimited("rl", retry_after=given_value).retry_after == expected


def test_ut00_04_circuit_open_retry_at_utc() -> None:
    """UT00-04 retry_at is stored as UTC; naive values are read as UTC; key kept."""
    plus2 = datetime.datetime(2026, 9, 24, 14, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))
    aware = e.CircuitOpen("x", key="k", retry_at=plus2)
    assert aware.retry_at == RETRY_AT
    assert aware.retry_at.tzinfo is datetime.UTC
    naive = e.CircuitOpen("x", key="k", retry_at=datetime.datetime(2026, 9, 24, 12, 0))  # noqa: DTZ001
    assert naive.retry_at == RETRY_AT
    assert naive.key == "k"


def test_ut00_05_model_refused_category() -> None:
    """UT00-05 category is cut to 64 characters; None stays None."""
    assert e.ModelRefused("x", category="c" * 100).category == "c" * 64
    assert e.ModelRefused("x").category is None


def test_ut00_06_error_kind() -> None:
    """UT00-06 error_kind classifies by category; bare and foreign errors are unknown."""
    for cls in PARENTS:
        kind = e.error_kind(_instance(cls))
        if issubclass(cls, e.RetryableError):
            assert kind == "retryable"
        elif issubclass(cls, e.RecoverableError):
            assert kind == "recoverable"
        else:
            assert kind == "fatal"
    assert e.error_kind(e.HernessError("x")) == "unknown"
    assert e.error_kind(ValueError()) == "unknown"


def test_ut00_07_to_log_fields_circuit_open() -> None:
    """UT00-07 to_log_fields flattens attributes and prefixes colliding context keys."""
    err = e.CircuitOpen("open", key="jira", retry_at=RETRY_AT, error_type="x", source="jira")
    out = e.to_log_fields(err)
    assert out["error_type"] == "CircuitOpen"
    assert out["error_kind"] == "retryable"
    assert out["error_message"] == "open"
    assert out["key"] == "jira"
    assert out["retry_at"] == RETRY_AT.isoformat()
    assert out["ctx_error_type"] == "x"
    assert out["source"] == "jira"
    assert out["cause_type"] is None


def test_ut00_08_pickle_round_trip() -> None:
    """UT00-08 every class survives pickle with message, context and extra attributes."""
    for cls in PARENTS:
        err = _instance(cls)
        back = pickle.loads(pickle.dumps(err))  # noqa: S301 - multiprocessing transport
        assert type(back) is cls
        assert back.message == err.message
        assert dict(back.context) == dict(err.context)
        for name in cls._extra_attrs:
            assert getattr(back, name) == getattr(err, name)


def test_ut00_71_not_found_hint_and_details() -> None:
    """UT00-71 hint and details are bounded, survive pickle and appear in log fields."""
    details: dict[str, Any] = {"a b": "x", "run_id": "run_x", "n": 5}
    details.update({f"k{i}": "v" for i in range(60)})
    err = e.NotFound("run not found", hint="h" * 600, details=details, run_id="run_x")
    assert err.hint == "h" * 500 + "…"
    assert len(err.details) == 50
    assert err.details["key_0"] == "x"
    assert err.details["n"] == "<int>"
    assert e.error_kind(err) == "recoverable"
    fields = e.to_log_fields(err)
    assert fields["error_hint"] == err.hint
    assert fields["detail_run_id"] == "run_x"
    back = pickle.loads(pickle.dumps(err))  # noqa: S301 - multiprocessing transport
    assert back.hint == err.hint
    assert dict(back.details) == dict(err.details)
    plain = e.ConfigError("x")
    assert plain.hint is None
    assert dict(plain.details) == {}


_scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(), st.text(max_size=300))


@given(
    message=st.text(max_size=2000),
    context=st.dictionaries(
        st.from_regex(r"[a-z][a-z_]{0,10}", fullmatch=True).filter(
            lambda k: k not in {"hint", "details"}
        ),
        st.one_of(_scalars, st.builds(object)),
        max_size=5,
    ),
)
def test_pt00_01_log_fields_are_json_safe(message: str, context: dict[str, Any]) -> None:
    """PT00-01 to_log_fields output is JSON-serialisable and the message is bounded."""
    out = e.to_log_fields(e.SourceUnavailable(message, **context))
    json.dumps(out)
    assert len(str(out["error_message"])) <= 1001


def test_st00_13_foreign_and_cause_messages_never_logged() -> None:
    """ST00-13 third-party and cause messages never reach the log fields."""
    try:
        try:
            raise ValueError("https://x/?token=abc")  # noqa: EM101, TRY301
        except ValueError as inner:
            raise e.SourceUnavailable("fetch failed") from inner  # noqa: EM101
    except e.SourceUnavailable as err:
        out = e.to_log_fields(err)
    assert "abc" not in json.dumps(out)
    assert out["cause_type"] == "ValueError"
    foreign = e.to_log_fields(RuntimeError("password=abc"))
    assert "abc" not in json.dumps(foreign)
    assert foreign["error_message"] == ""
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/core/test_errors.py -v`
Expected: collection ERROR `ModuleNotFoundError: No module named 'herness.core.errors'`.

- [ ] **Step 3: Implement `herness/core/errors.py`**

```python
"""Error taxonomy of design 00 §7, plus NotFound, hint and details (R-19).

Every error Herness raises is one of these classes, so the resilience layer (impl 08)
decides by class, never by string matching. No other spec adds classes to this file.
"""

from __future__ import annotations

import datetime
import math
import re
import types
from collections.abc import Iterable, Mapping
from typing import ClassVar, Final, Literal

MAX_MESSAGE_CHARS: Final = 1000
MAX_CONTEXT_STR_CHARS: Final = 200
MAX_HINT_CHARS: Final = 500
MAX_DETAILS: Final = 50
MAX_DETAIL_KEY_CHARS: Final = 64
MAX_DETAIL_VALUE_CHARS: Final = 2000
_MAX_CATEGORY_CHARS: Final = 64
_DETAIL_KEY_RE: Final = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_ELLIPSIS: Final = "…"

type Scalar = str | int | float | bool | None
type ErrorKind = Literal["retryable", "recoverable", "fatal", "unknown"]


def _bound(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + _ELLIPSIS


def _type_tag(value: object) -> str:
    return "<" + type(value).__name__ + ">"


def _scalar(value: object, limit: int) -> Scalar:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _bound(value, limit)
    return _type_tag(value)


def _bound_context(items: Iterable[tuple[str, object]]) -> dict[str, Scalar]:
    return {key: _scalar(value, MAX_CONTEXT_STR_CHARS) for key, value in items}


def _bound_hint(hint: object) -> str | None:
    if hint is None:
        return None
    return _bound(hint, MAX_HINT_CHARS) if isinstance(hint, str) else _type_tag(hint)


def _bound_details(items: Iterable[tuple[object, object]] | None) -> dict[str, str]:
    det: dict[str, str] = {}
    if items is None:
        return det
    for index, (key, value) in enumerate(items):
        if index >= MAX_DETAILS:
            break
        valid = isinstance(key, str) and _DETAIL_KEY_RE.fullmatch(key) is not None
        name = key if valid and isinstance(key, str) else f"key_{index}"
        det[name] = (
            _bound(value, MAX_DETAIL_VALUE_CHARS) if isinstance(value, str) else _type_tag(value)
        )
    return det


class HernessError(Exception):
    """Root of every error Herness raises.

    Carries a bounded message, a scalar-only identifier context, an optional operator
    hint and optional string details. The constructor raises nothing.
    """

    _extra_attrs: ClassVar[tuple[str, ...]] = ()
    message: str
    hint: str | None
    _context: dict[str, Scalar]
    _details: dict[str, str]

    def __init__(
        self,
        message: str,
        /,
        *,
        hint: str | None = None,
        details: Mapping[str, str] | None = None,
        **context: Scalar,
    ) -> None:
        bounded = _bound(message, MAX_MESSAGE_CHARS)
        super().__init__(bounded)
        self.message = bounded
        self._context = _bound_context(context.items())
        self.hint = _bound_hint(hint)
        self._details = _bound_details(None if details is None else details.items())

    @property
    def context(self) -> Mapping[str, Scalar]:
        """Read-only identifier context."""
        return types.MappingProxyType(self._context)

    @property
    def details(self) -> Mapping[str, str]:
        """Read-only structured details (R-19)."""
        return types.MappingProxyType(self._details)

    def __reduce__(self) -> tuple[object, ...]:
        extra = {name: getattr(self, name) for name in type(self)._extra_attrs}
        return (
            _rebuild,
            (type(self), self.message, dict(self._context), self.hint, dict(self._details), extra),
        )


def _rebuild(
    cls: type[HernessError],
    message: str,
    context: dict[str, Scalar],
    hint: str | None,
    details: dict[str, str],
    extra: dict[str, object],
) -> HernessError:
    obj = cls.__new__(cls)
    Exception.__init__(obj, message)
    obj.message = message
    obj._context = context
    obj.hint = hint
    obj._details = details
    for name, value in extra.items():
        setattr(obj, name, value)
    return obj


class RetryableError(HernessError):
    """Retried with backoff by the resilience layer (impl 08)."""


class RecoverableError(HernessError):
    """The caller repairs and retries differently."""


class FatalError(HernessError):
    """No retry; the job fails and the task goes to the dead letter list."""


class SourceUnavailable(RetryableError):
    """A source system cannot be reached or returned a server error."""


class ModelUnavailable(RetryableError):
    """A model endpoint cannot be reached or is overloaded."""


class StoreBusy(RetryableError):
    """SQLite or DuckDB lock contention outlasted its busy timeout."""


class RateLimited(RetryableError):
    """A rate limit was hit; carries the server-requested wait (seconds or None)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("retry_after",)
    retry_after: float | None

    def __init__(
        self,
        message: str,
        /,
        *,
        retry_after: float | None = None,
        hint: str | None = None,
        details: Mapping[str, str] | None = None,
        **context: Scalar,
    ) -> None:
        super().__init__(message, hint=hint, details=details, **context)
        if retry_after is None or not math.isfinite(retry_after):
            self.retry_after = None
        elif retry_after < 0:
            self.retry_after = 0.0
        else:
            self.retry_after = float(retry_after)


class CircuitOpen(RetryableError):
    """A circuit breaker is open; the caller must not call until retry_at (UTC)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("key", "retry_at")
    key: str
    retry_at: datetime.datetime

    def __init__(
        self,
        message: str,
        /,
        *,
        key: str,
        retry_at: datetime.datetime,
        hint: str | None = None,
        details: Mapping[str, str] | None = None,
        **context: Scalar,
    ) -> None:
        super().__init__(message, hint=hint, details=details, **context)
        self.key = key
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            self.retry_at = retry_at.replace(tzinfo=datetime.UTC)
        else:
            self.retry_at = retry_at.astimezone(datetime.UTC)


class OutputValidationError(RecoverableError):
    """Model output failed schema validation; repair prompt (max 2), then fallback model."""


class ToolInputError(RecoverableError):
    """Tool arguments are invalid; returned as an error tool result."""


class QueryError(RecoverableError):
    """SQL failed or was rejected; returned as an error tool result with a hint."""


class ModelRefused(RecoverableError):
    """The model refused to answer; the fallback chain moves to its next entry."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("category",)
    category: str | None

    def __init__(
        self,
        message: str,
        /,
        *,
        category: str | None = None,
        hint: str | None = None,
        details: Mapping[str, str] | None = None,
        **context: Scalar,
    ) -> None:
        super().__init__(message, hint=hint, details=details, **context)
        self.category = None if category is None else category[:_MAX_CATEGORY_CHARS]


class PolicyViolation(RecoverableError):
    """Memory write policy or injection scan refused a write; pending or rejected."""


class ReportContractError(RecoverableError):
    """A draft fails the rendering contract."""


class NotFound(RecoverableError):
    """A requested object does not exist; the caller reports it or picks another (R-19)."""


class ConfigError(FatalError):
    """Configuration is invalid or incomplete."""


class AuthError(FatalError):
    """Authentication with a source or model endpoint failed."""


class SchemaViolation(FatalError):
    """Data does not match its declared contract."""


class BudgetExceeded(FatalError):
    """A token, cost, tool-call or wall-clock budget is exhausted."""


class PermissionDenied(FatalError):
    """The caller's role is not allowed to perform the action."""


class EgressBlocked(FatalError):
    """The egress guard refused an off-network call."""


def error_kind(exc: BaseException) -> ErrorKind:
    """Classify any exception into its taxonomy category. Raises nothing."""
    if isinstance(exc, RetryableError):
        return "retryable"
    if isinstance(exc, RecoverableError):
        return "recoverable"
    if isinstance(exc, FatalError):
        return "fatal"
    return "unknown"


def _attr_value(value: object) -> Scalar:
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return _scalar(value, MAX_DETAIL_VALUE_CHARS)


def _herness_fields(out: dict[str, Scalar], exc: HernessError) -> None:
    out["error_message"] = exc.message
    out["error_hint"] = exc.hint
    for key, value in exc.details.items():
        out["detail_" + key] = value
    for name in type(exc)._extra_attrs:
        out[name] = _attr_value(getattr(exc, name))
    for key, value in exc.context.items():
        out["ctx_" + key if key in out else key] = value


def to_log_fields(exc: BaseException) -> dict[str, Scalar]:
    """Turn an exception into safe, flat, JSON-serialisable fields. Raises nothing.

    Messages of non-Herness exceptions and of causes are never copied (TH00-06).
    """
    out: dict[str, Scalar] = {"error_type": type(exc).__name__, "error_kind": error_kind(exc)}
    if isinstance(exc, HernessError):
        _herness_fields(out, exc)
    else:
        out["error_message"] = ""
    cause = exc.__cause__ or exc.__context__
    out["cause_type"] = None if cause is None else type(cause).__name__
    return out
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/core/test_errors.py -v`
Expected: all tests pass (20 including the parametrised cases).

- [ ] **Step 5: Gates and coverage**

Run: `uv run ruff check herness tests` and `uv run ruff format --check .` and `uv run mypy`
Expected: 0 issues. Fix any lint finding without changing behaviour. A `TRY004` or `PERF` finding may need a local rewrite.
Run: `uv run pytest tests/unit/core/test_errors.py --cov=herness.core.errors --cov-branch --cov-report=term-missing`
Expected: `herness/core/errors.py` ≥ 90 % line and ≥ 85 % branch. Check that the file has ≤ 340 lines.

- [ ] **Step 6: Commit**

```bash
git add herness/core/errors.py tests/unit/core/test_errors.py
git commit -m "feat(core): add error taxonomy with hint and details (T00-03)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Time helpers (T00-04)

Units U00-09 … U00-18 (read impl 00 §3.2). Tests UT00-09 … UT00-18, PT00-02, ST00-14, plus Review Focus 2 and 3.

**Files:**
- Create: `herness/core/time.py`
- Test: `tests/unit/core/test_time.py`

**Interfaces:**
- Consumes: `herness.core.errors.SchemaViolation`, `ConfigError`.
- Produces (callers use `from herness.core import time as clock`): `now() -> datetime`; `sleep(seconds: float) -> None`; `async asleep(seconds: float) -> None`; `monotonic() -> float`; `ensure_utc(dt: datetime.date) -> datetime`; `format_utc(dt: datetime) -> str` (27 chars, `YYYY-MM-DDTHH:MM:SS.ffffffZ`); `parse_utc(text: object) -> datetime`; `parse_iso(text: str) -> datetime`; `utc_day(dt: datetime) -> str`; `zone(name: str) -> zoneinfo.ZoneInfo`; constants `DB_TS_LEN = 27`, `MAX_SLEEP_S = 3600.0`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/core/test_time.py`:

```python
"""Tests for herness.core.time (U00-09 … U00-18)."""

import asyncio
import datetime
import math
import time
import zoneinfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core import time as clock
from herness.core.errors import ConfigError, SchemaViolation

pytestmark = pytest.mark.unit

UTC = datetime.UTC


def _tz(hours: float) -> datetime.timezone:
    return datetime.timezone(datetime.timedelta(hours=hours))


def test_ut00_09_now_is_utc() -> None:
    """UT00-09 now() returns an aware UTC datetime."""
    assert clock.now().tzinfo is UTC


def test_ut00_10_sleep_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-10 sleep delegates valid durations and rejects invalid ones."""
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", calls.append)
    clock.sleep(0.5)
    assert calls == [0.5]
    for bad in (-1, math.nan, 3601):
        with pytest.raises(SchemaViolation):
            clock.sleep(bad)


@pytest.mark.asyncio
async def test_ut00_11_asleep_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-11 asleep awaits asyncio.sleep for valid durations only."""
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await clock.asleep(0.1)
    assert calls == [0.1]
    with pytest.raises(SchemaViolation):
        await clock.asleep(-1)


def test_ut00_12_monotonic_non_decreasing() -> None:
    """UT00-12 monotonic never goes backwards."""
    values = [clock.monotonic() for _ in range(1000)]
    assert values == sorted(values)


def test_ut00_13_ensure_utc() -> None:
    """UT00-13 aware values convert to UTC; naive datetimes and dates are rejected."""
    value = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=_tz(2))
    assert clock.ensure_utc(value) == datetime.datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    with pytest.raises(SchemaViolation):
        clock.ensure_utc(datetime.datetime(2026, 9, 24))  # noqa: DTZ001
    with pytest.raises(SchemaViolation):
        clock.ensure_utc(datetime.date(2026, 9, 24))


def test_ut00_14_format_utc() -> None:
    """UT00-14 fixed-width zero-padded UTC text."""
    assert clock.format_utc(datetime.datetime(5, 1, 2, 3, 4, 5, 6, tzinfo=UTC)) == (
        "0005-01-02T03:04:05.000006Z"
    )
    text = clock.format_utc(datetime.datetime(2026, 9, 24, 5, 30, tzinfo=_tz(5.5)))
    assert text == "2026-09-24T00:00:00.000000Z"
    assert len(text) == clock.DB_TS_LEN


def test_ut00_15_parse_utc() -> None:
    """UT00-15 strict inverse of format_utc; invalid text never echoed in the error."""
    text = "2026-09-24T10:00:00.123456Z"
    assert clock.format_utc(clock.parse_utc(text)) == text
    for bad in (text[:-1], text + "0", text[:-1] + "0", "2026-02-30T00:00:00.000000Z"):
        with pytest.raises(SchemaViolation) as info:
            clock.parse_utc(bad)
        assert all(bad not in str(v) for v in info.value.context.values())


def test_ut00_16_parse_iso() -> None:
    """UT00-16 lenient ISO parsing that still rejects naive values."""
    assert clock.parse_iso("2026-09-24") == datetime.datetime(2026, 9, 24, tzinfo=UTC)
    assert clock.parse_iso("2026-09-24T10:00:00Z") == datetime.datetime(
        2026, 9, 24, 10, tzinfo=UTC
    )
    assert clock.parse_iso("2026-09-24T10:00:00+02:00") == datetime.datetime(
        2026, 9, 24, 8, tzinfo=UTC
    )
    for bad in ("2026-09-24T10:00:00", "1" * 65, ""):
        with pytest.raises(SchemaViolation):
            clock.parse_iso(bad)


def test_ut00_17_utc_day() -> None:
    """UT00-17 utc_day of 23:30 at -02:00 is the next day's date."""
    value = datetime.datetime(2026, 9, 24, 23, 30, tzinfo=_tz(-2))
    assert clock.utc_day(value) == "2026-09-25"


def test_ut00_18_zone() -> None:
    """UT00-18 known zones resolve; bad names raise ConfigError with zone in context."""
    assert isinstance(clock.zone("Europe/London"), zoneinfo.ZoneInfo)
    for bad in ("Mars/Base", "../etc/passwd", ""):
        with pytest.raises(ConfigError) as info:
            clock.zone(bad)
        assert "zone" in info.value.context


@given(
    st.datetimes(
        min_value=datetime.datetime(1, 1, 2),  # noqa: DTZ001
        max_value=datetime.datetime(9999, 12, 30),  # noqa: DTZ001
        timezones=st.sampled_from([UTC, _tz(5.5), _tz(-8), _tz(14)]),
    ),
    st.datetimes(
        min_value=datetime.datetime(1, 1, 2),  # noqa: DTZ001
        max_value=datetime.datetime(9999, 12, 30),  # noqa: DTZ001
        timezones=st.just(UTC),
    ),
)
def test_pt00_02_round_trip_and_order(a: datetime.datetime, b: datetime.datetime) -> None:
    """PT00-02 parse_utc(format_utc(d)) round-trips and text order equals time order."""
    assert clock.parse_utc(clock.format_utc(a)) == a.astimezone(UTC)
    ta, tb = clock.format_utc(a), clock.format_utc(b)
    assert (ta < tb) == (a < b)


def test_st00_14_hostile_timestamps() -> None:
    """ST00-14 injected, oversized and full-width inputs raise without echoing input."""
    inputs = [
        "2026-01-01T00:00:00.000000Z' OR 1=1",
        "9" * 10_000,
        "２０２６-01-01T00:00:00.000000Z",
    ]
    for text in inputs:
        for parse in (clock.parse_utc, clock.parse_iso):
            with pytest.raises(SchemaViolation) as info:
                parse(text)
            assert all(text not in str(v) for v in info.value.context.values())


def test_rf_utc_day_non_utc_input() -> None:
    """RF-2 utc_day of an aware +05:30 time just after midnight is the previous UTC day."""
    value = datetime.datetime(2026, 9, 25, 0, 10, tzinfo=_tz(5.5))
    assert clock.utc_day(value) == "2026-09-24"


def test_rf_parse_iso_whitespace_and_lowercase_z() -> None:
    """RF-3 operator input with surrounding whitespace and a lowercase z parses."""
    assert clock.parse_iso(" 2026-09-24T10:00:00z ") == datetime.datetime(
        2026, 9, 24, 10, tzinfo=UTC
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/core/test_time.py -v`
Expected: collection ERROR `AttributeError` or `ImportError` for `herness.core.time`.

- [ ] **Step 3: Implement `herness/core/time.py`**

```python
"""UTC clock, bounded sleeps, fixed-width UTC timestamp text and time zones (design 00 §8).

Callers import this module as ``from herness.core import time as clock`` and call
``clock.now()``, so the test FakeClock (impl 11) can patch the module attributes.
"""

from __future__ import annotations

import asyncio
import datetime
import math
import re
import time
import zoneinfo
from typing import Final

from herness.core.errors import ConfigError, SchemaViolation

DB_TS_LEN: Final = 27
MAX_SLEEP_S: Final = 3600.0
_MAX_ISO_CHARS: Final = 64
_MAX_ZONE_CHARS: Final = 64
_DB_TS_RE: Final = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.(\d{6})Z", re.ASCII
)
_DATE_RE: Final = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
_ZONE_RE: Final = re.compile(r"[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+)*")


def now() -> datetime.datetime:
    """Return the current wall-clock time as an aware UTC datetime; the only clock read."""
    return datetime.datetime.now(datetime.UTC)


def _check_duration(seconds: float) -> None:
    msg = "invalid sleep duration"
    if not math.isfinite(seconds):
        raise SchemaViolation(msg, seconds=repr(seconds))
    if seconds < 0 or seconds > MAX_SLEEP_S:
        raise SchemaViolation(msg, seconds=seconds)


def sleep(seconds: float) -> None:
    """Block the thread for 0..3600 s. Raises SchemaViolation (seconds) when out of range."""
    _check_duration(seconds)
    time.sleep(seconds)


async def asleep(seconds: float) -> None:
    """Non-blocking bounded sleep. Raises SchemaViolation (seconds) when out of range."""
    _check_duration(seconds)
    await asyncio.sleep(seconds)


def monotonic() -> float:
    """Monotonic seconds for measuring durations."""
    return time.monotonic()


def ensure_utc(dt: datetime.date) -> datetime.datetime:
    """Reject naive datetimes and normalise aware ones to UTC.

    Raises SchemaViolation (got) for a non-datetime and SchemaViolation for a naive value.
    """
    if not isinstance(dt, datetime.datetime):
        msg = "expected datetime"
        raise SchemaViolation(msg, got=type(dt).__name__)  # noqa: TRY004 - taxonomy error
    if dt.tzinfo is None or dt.utcoffset() is None:
        msg = "naive datetime rejected"
        raise SchemaViolation(msg)
    return dt.astimezone(datetime.UTC)


def format_utc(dt: datetime.datetime) -> str:
    """Fixed-width UTC text ``YYYY-MM-DDTHH:MM:SS.ffffffZ``. Raises as ensure_utc."""
    u = ensure_utc(dt)
    return (
        f"{u.year:04d}-{u.month:02d}-{u.day:02d}T"
        f"{u.hour:02d}:{u.minute:02d}:{u.second:02d}.{u.microsecond:06d}Z"
    )


def parse_utc(text: object) -> datetime.datetime:
    """Strict inverse of format_utc. Raises SchemaViolation (length); input never echoed."""
    msg = "bad timestamp text"
    if not isinstance(text, str):
        raise SchemaViolation(msg, length=-1)  # noqa: TRY004 - taxonomy error
    if len(text) != DB_TS_LEN:
        raise SchemaViolation(msg, length=len(text))
    match = _DB_TS_RE.fullmatch(text)
    if match is None:
        raise SchemaViolation(msg, length=len(text))
    parts = [int(group) for group in match.groups()]
    try:
        return datetime.datetime(*parts, tzinfo=datetime.UTC)
    except ValueError as exc:
        raise SchemaViolation(msg, length=len(text)) from exc


def parse_iso(text: str) -> datetime.datetime:
    """Lenient ISO-8601 parser for operator input. Raises SchemaViolation (length)."""
    s = text.strip()
    msg = "bad ISO timestamp"
    if not 0 < len(s) <= _MAX_ISO_CHARS:
        raise SchemaViolation(msg, length=len(s))
    try:
        if _DATE_RE.fullmatch(s):
            day = datetime.date.fromisoformat(s)
            return datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.UTC)
        if s[-1] in "Zz":
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
    except ValueError as exc:
        raise SchemaViolation(msg, length=len(s)) from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        msg = "naive datetime rejected"
        raise SchemaViolation(msg)
    return dt.astimezone(datetime.UTC)


def utc_day(dt: datetime.datetime) -> str:
    """UTC date text ``YYYY-MM-DD``. Raises as ensure_utc."""
    return format_utc(dt)[:10]


def zone(name: str) -> zoneinfo.ZoneInfo:
    """Resolve an IANA time zone from tzdata. Raises ConfigError (zone) when invalid."""
    msg = "unknown time zone"
    if not 0 < len(name) <= _MAX_ZONE_CHARS or _ZONE_RE.fullmatch(name) is None or ".." in name:
        raise ConfigError(msg, zone=name)
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigError(msg, zone=name) from exc
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/core/test_time.py -v`
Expected: all pass.

- [ ] **Step 5: Gates and coverage**

Run: `uv run ruff check herness tests`, `uv run ruff format --check .`, `uv run mypy`
Expected: 0 issues.
Run: `uv run pytest tests/unit/core/test_time.py --cov=herness.core.time --cov-branch --cov-report=term-missing`
Expected: ≥ 90 % line and ≥ 85 % branch; the file has ≤ 200 lines.

- [ ] **Step 6: Commit**

```bash
git add herness/core/time.py tests/unit/core/test_time.py
git commit -m "feat(core): add UTC clock, bounded sleeps and timestamp text (T00-04)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Identifiers, canonical JSON and `query_id` (T00-05)

Units U00-19 … U00-34 (read impl 00 §3.3). Tests UT00-19 … UT00-34, PT00-03 … PT00-05, FT00-02, ST00-05, ST00-17, BT00-01, BT00-02, plus Review Focus 2.

**Files:**
- Create: `herness/core/ids.py`
- Test: `tests/unit/core/test_ids.py`, `tests/bench/test_core_bench.py` (created here; Tasks 7 and 10 add to it)

**Interfaces:**
- Consumes: `herness.core.errors.SchemaViolation`; `clock.ensure_utc`, `clock.format_utc`.
- Produces: `CROCKFORD_ALPHABET`, `ULID_LEN = 26`, `RECORD_KEY_MAX_LEN = 512`; `class IdKind(StrEnum)` (`RUN`, `TASK`, `JOB`, `FINDING`, `REC`, `OUTCOME`, `MEMORY`, `CLUSTER`, `ITEM`, `REQUEST`, `EGRESS`, `AUDIT`); `ID_PREFIXES: Mapping[IdKind, str]`; `new_ulid() -> str`; `new_id(kind: IdKind) -> str`; `new_build_id(now: datetime) -> str`; `is_valid_ulid(value: object) -> bool`; `is_valid_id(kind: IdKind, value: object) -> bool`; `is_valid_build_id(value: object) -> bool`; `make_record_id(source, entity, source_key) -> str`; `split_record_id(record_id) -> tuple[str, str, str]`; `canonical_json(value: object) -> str`; `sha256_hex(data: str | bytes) -> str`; `normalize_sql(sql: str) -> str`; `query_id(sql: str, params: Mapping[str, object], build_id: str) -> str`; `new_token(nbytes: int = 32) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/core/test_ids.py`:

```python
"""Tests for herness.core.ids (U00-19 … U00-34)."""

import base64
import datetime
import decimal
import enum
import hashlib
import json
import pathlib
import secrets
import threading
import time
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core import ids
from herness.core.errors import SchemaViolation

pytestmark = pytest.mark.unit

UTC = datetime.UTC
FIXED_MS = 1_790_000_000_000
BUILD = "20260924-211403-ABCDEF"


@pytest.fixture
def fresh_ulid_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ids, "_STATE", ids._UlidState())


def _decode(text: str) -> int:
    value = 0
    for char in text:
        value = value * 32 + ids.CROCKFORD_ALPHABET.index(char)
    return value


def _fixed_ns(monkeypatch: pytest.MonkeyPatch, *values: int) -> None:
    queue = list(values)
    monkeypatch.setattr(time, "time_ns", lambda: queue.pop(0) if len(queue) > 1 else queue[0])


@pytest.mark.usefixtures("fresh_ulid_state")
def test_ut00_19_ulid_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-19 a ULID is 26 Crockford characters whose first 10 decode to the time."""
    _fixed_ns(monkeypatch, FIXED_MS * 1_000_000)
    value = ids.new_ulid()
    assert ids.is_valid_ulid(value)
    assert _decode(value[:10]) == FIXED_MS


@pytest.mark.usefixtures("fresh_ulid_state")
def test_ut00_20_same_millisecond_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-20 within one millisecond the random part increments by one."""
    _fixed_ns(monkeypatch, FIXED_MS * 1_000_000)
    monkeypatch.setattr(secrets, "randbits", lambda _bits: 12345)
    first, second = ids.new_ulid(), ids.new_ulid()
    assert _decode(second) == _decode(first) + 1
    assert second > first


@pytest.mark.usefixtures("fresh_ulid_state")
def test_ut00_21_random_overflow_moves_to_next_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-21 random-part overflow advances the timestamp by one millisecond."""
    _fixed_ns(monkeypatch, FIXED_MS * 1_000_000)
    monkeypatch.setattr(secrets, "randbits", lambda _bits: 2**80 - 1)
    first, second = ids.new_ulid(), ids.new_ulid()
    assert _decode(second[:10]) == _decode(first[:10]) + 1


def test_ut00_22_threads_unique_and_ordered() -> None:
    """UT00-22 8 threads x 10,000 ULIDs are unique and increasing per thread."""
    results: list[list[str]] = [[] for _ in range(8)]

    def work(slot: list[str]) -> None:
        slot.extend(ids.new_ulid() for _ in range(10_000))

    threads = [threading.Thread(target=work, args=(slot,)) for slot in results]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    everything = [value for slot in results for value in slot]
    assert len(set(everything)) == 80_000
    for slot in results:
        assert slot == sorted(slot)
        assert len(set(slot)) == len(slot)


def test_ut00_23_prefixed_ids() -> None:
    """UT00-23 new_id uses the design 00 §5 prefix for every kind."""
    expected = {
        ids.IdKind.RUN: "run", ids.IdKind.TASK: "task", ids.IdKind.JOB: "job",
        ids.IdKind.FINDING: "fnd", ids.IdKind.REC: "rec", ids.IdKind.OUTCOME: "out",
        ids.IdKind.MEMORY: "mem", ids.IdKind.CLUSTER: "cl", ids.IdKind.ITEM: "rev",
        ids.IdKind.REQUEST: "del", ids.IdKind.EGRESS: "egr", ids.IdKind.AUDIT: "aud",
    }
    assert dict(ids.ID_PREFIXES) == expected
    for kind, prefix in expected.items():
        value = ids.new_id(kind)
        assert value.startswith(prefix + "_")
        assert ids.is_valid_id(kind, value)


def test_ut00_24_build_id() -> None:
    """UT00-24 build_id has the stamp, 22 characters and a Crockford suffix."""
    value = ids.new_build_id(datetime.datetime(2026, 9, 24, 21, 14, 3, tzinfo=UTC))
    assert value.startswith("20260924-211403-")
    assert len(value) == 22
    assert all(char in ids.CROCKFORD_ALPHABET for char in value[-6:])
    with pytest.raises(SchemaViolation):
        ids.new_build_id(datetime.datetime(2026, 9, 24))  # noqa: DTZ001


def test_ut00_25_is_valid_ulid() -> None:
    """UT00-25 only canonical uppercase ULID text is valid."""
    good = ids.new_ulid()
    bad: list[Any] = [
        good.lower(), good[:25], good + "0", "8" + good[1:],
        good[:25] + "I", good[:25] + "L", good[:25] + "O", good[:25] + "U", 12,
    ]
    assert ids.is_valid_ulid(good)
    assert not any(ids.is_valid_ulid(value) for value in bad)


def test_ut00_26_is_valid_id() -> None:
    """UT00-26 kind, prefix and ULID must all match."""
    run = ids.new_id(ids.IdKind.RUN)
    assert ids.is_valid_id(ids.IdKind.RUN, run)
    assert not ids.is_valid_id(ids.IdKind.TASK, run)
    assert not ids.is_valid_id(ids.IdKind.RUN, "run_BAD")
    assert not ids.is_valid_id(ids.IdKind.RUN, 12)


def test_ut00_27_is_valid_build_id() -> None:
    """UT00-27 only real calendar instants with the exact shape are valid."""
    assert ids.is_valid_build_id(BUILD)
    for bad in ("20261340-000000-ABCDEF", "20260924_211403_ABCDEF", "20260924-211403-abcdef"):
        assert not ids.is_valid_build_id(bad)


def test_ut00_28_make_record_id() -> None:
    """UT00-28 parts are validated and joined; failures name the part."""
    assert ids.make_record_id("jira", "issue", "10001") == "jira:issue:10001"
    cases = [
        ("Source", "issue", "k"), ("jira", "is-sue", "k"), ("jira", "issue", "a\nb"),
        ("jira", "issue", "k" * 513), ("jira", "issue", " k"),
    ]
    for source, entity, key in cases:
        with pytest.raises(SchemaViolation) as info:
            ids.make_record_id(source, entity, key)
        assert "part" in info.value.context


def test_ut00_29_split_record_id() -> None:
    """UT00-29 the source key keeps its own colons; too few parts fail."""
    assert ids.split_record_id("monitoring:event:prometheus:abc:1") == (
        "monitoring", "event", "prometheus:abc:1",
    )
    with pytest.raises(SchemaViolation):
        ids.split_record_id("a:b")


class _Color(enum.StrEnum):
    RED = "red"


def test_ut00_30_canonical_json() -> None:
    """UT00-30 canonical encoding of supported types; everything else is rejected."""
    value = {
        "b": 1,
        "a": {"d": decimal.Decimal("12.50"), "c": datetime.datetime(2026, 9, 24, 10, tzinfo=UTC)},
        "e": datetime.date(2026, 9, 24),
        "f": pathlib.PurePosixPath("a/b"),
        "g": _Color.RED,
        "h": (1, 2),
    }
    assert ids.canonical_json(value) == (
        '{"a":{"c":"2026-09-24T10:00:00.000000Z","d":"12.50"},"b":1,'
        '"e":"2026-09-24","f":"a/b","g":"red","h":[1,2]}'
    )
    deep: Any = 1
    for _ in range(64):
        deep = [deep]
    ids.canonical_json(deep)
    bad: list[Any] = [
        float("nan"), {1: 2}, {1, 2}, b"x", datetime.datetime(2026, 9, 24),  # noqa: DTZ001
        [deep],
    ]
    for item in bad:
        with pytest.raises(SchemaViolation):
            ids.canonical_json(item)


def test_ut00_31_normalize_sql() -> None:
    """UT00-31 whitespace collapsed and trailing semicolons removed."""
    assert ids.normalize_sql("  SELECT\n\t1 ;; ") == "SELECT 1"
    assert ids.normalize_sql("SELECT 1") == "SELECT 1"
    assert ids.normalize_sql(";") == ""


def test_ut00_32_query_id_known_vector() -> None:
    """UT00-32 query_id equals the independently computed vector; bad inputs raise."""
    payload = json.dumps(
        {"sql": "SELECT 1", "params": {"a": 1}, "build_id": BUILD},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    expected = "q_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    assert ids.query_id(" SELECT  1; ", {"a": 1}, BUILD) == expected
    with pytest.raises(SchemaViolation):
        ids.query_id(" ; ", {}, BUILD)
    with pytest.raises(SchemaViolation):
        ids.query_id("SELECT 1", {}, "bad")


def test_ut00_33_sha256_hex() -> None:
    """UT00-33 str input is UTF-8 encoded; output is 64 lowercase hex."""
    value = ids.sha256_hex("é")
    assert value == ids.sha256_hex("é".encode())
    assert len(value) == 64
    assert value == value.lower()


def test_ut00_34_new_token() -> None:
    """UT00-34 token sizes and bounds."""
    assert len(ids.new_token()) == 43
    assert len(ids.new_token(16)) == 22
    for bad in (15, 65):
        with pytest.raises(SchemaViolation):
            ids.new_token(bad)


_sources = st.from_regex(r"[a-z][a-z0-9_]{0,31}", fullmatch=True)
_entities = st.from_regex(r"[a-z][a-z0-9_]{0,63}", fullmatch=True)
_keys = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=512
)


@given(_sources, _entities, _keys)
def test_pt00_03_record_id_round_trip(source: str, entity: str, key: str) -> None:
    """PT00-03 split_record_id inverts make_record_id."""
    assert ids.split_record_id(ids.make_record_id(source, entity, key)) == (source, entity, key)


_json = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=10)),
    lambda children: st.dictionaries(st.text(max_size=5), children, max_size=4),
    max_leaves=10,
)


@given(st.dictionaries(st.text(max_size=5), _json, max_size=5), st.randoms())
def test_pt00_04_key_order_irrelevant(value: dict[str, Any], rnd: Any) -> None:
    """PT00-04 shuffling key insertion order gives identical output."""
    items = list(value.items())
    rnd.shuffle(items)
    out = ids.canonical_json(value)
    assert ids.canonical_json(dict(items)) == out
    assert json.loads(out) == value


@given(
    st.from_regex(r"SELECT [a-z]{1,8} FROM [a-z]{1,8}", fullmatch=True),
    st.sampled_from([" ", "  ", "\n", "\t "]),
    st.integers(min_value=0, max_value=3),
)
def test_pt00_05_query_id_whitespace_invariant(sql: str, ws: str, semis: int) -> None:
    """PT00-05 whitespace and trailing semicolons do not change query_id; build does."""
    noisy = ws + sql.replace(" ", ws + " ") + ws + ";" * semis
    assert ids.query_id(noisy, {}, BUILD) == ids.query_id(sql, {}, BUILD)
    assert ids.query_id(sql, {}, BUILD) != ids.query_id(sql, {}, "20260924-211404-ABCDEF")


@pytest.mark.usefixtures("fresh_ulid_state")
def test_ft00_02_clock_backwards(monkeypatch: pytest.MonkeyPatch) -> None:
    """FT00-02 a clock moving backwards still yields an increasing ULID."""
    _fixed_ns(monkeypatch, FIXED_MS * 1_000_000, (FIXED_MS - 5) * 1_000_000)
    first = ids.new_ulid()
    second = ids.new_ulid()
    assert second > first


def test_st00_05_tokens_unique_and_unpredictable() -> None:
    """ST00-05 100,000 tokens are unique and not near-sequential."""
    tokens = [ids.new_token() for _ in range(100_000)]
    assert len(set(tokens)) == len(tokens)
    values = [int.from_bytes(base64.urlsafe_b64decode(t + "=")) for t in tokens[:1000]]
    for left, right in zip(values, values[1:], strict=False):
        assert abs(left - right) >= 2**64


def test_st00_17_query_id_distinguishes_types_and_order() -> None:
    """ST00-17 1, "1", 1.0 and list order give distinct query IDs."""
    produced = {
        ids.query_id("SELECT 1", params, BUILD)
        for params in ({"a": 1}, {"a": "1"}, {"a": 1.0})
    }
    assert len(produced) == 3
    assert ids.query_id("SELECT 1", {"a": [1, 2]}, BUILD) != ids.query_id(
        "SELECT 1", {"a": [2, 1]}, BUILD
    )


def test_rf_build_id_uses_utc_date() -> None:
    """RF-2 a +05:30 time just after local midnight gives the UTC (previous) date."""
    tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    value = ids.new_build_id(datetime.datetime(2026, 9, 25, 0, 10, tzinfo=tz))
    assert value.startswith("20260924-184000-")
```

`tests/bench/test_core_bench.py`:

```python
"""Foundation benchmarks (impl 00 §10, §11.6). Run: pytest -m "integration and slow" tests/bench."""

import time
from typing import Any

import pytest

from herness.core import ids

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _p95(benchmark: Any) -> float:
    data = sorted(benchmark.stats.stats.data)
    return float(data[int(0.95 * (len(data) - 1))])


def test_bt00_01_ulid_throughput() -> None:
    """BT00-01 at least 200,000 new_ulid calls per second."""
    start = time.perf_counter()
    for _ in range(1_000_000):
        ids.new_ulid()
    rate = 1_000_000 / (time.perf_counter() - start)
    assert rate >= 200_000


def test_bt00_02_query_id_p95(benchmark: Any) -> None:
    """BT00-02 query_id on 2 KB SQL with 10 params: p95 under 50 microseconds."""
    sql = "SELECT " + ", ".join(f"col_{i}" for i in range(250)) + " FROM t"
    params = {f"p{i}": i for i in range(10)}
    benchmark.pedantic(
        ids.query_id, args=(sql, params, "20260924-211403-ABCDEF"), rounds=10_000, iterations=1
    )
    assert _p95(benchmark) < 50e-6
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/core/test_ids.py -v`
Expected: collection ERROR, `herness.core.ids` not found.

- [ ] **Step 3: Implement `herness/core/ids.py`**

```python
"""Identifiers, canonical JSON, SHA-256 and query_id (design 00 §5, R-14).

canonical_json, normalize_sql and query_id are the single implementations used by
impl 04 and impl 05. ULIDs are identifiers, not secrets: use new_token for bearer values.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import hashlib
import json
import math
import pathlib
import re
import secrets
import threading
import time
import types
from collections.abc import Mapping
from typing import Final

from herness.core import time as clock
from herness.core.errors import SchemaViolation

CROCKFORD_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LEN: Final[int] = 26
RECORD_KEY_MAX_LEN: Final = 512
_ULID_RE: Final = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_RAND_BITS: Final = 80
_TS_BITS: Final = 48
_RAND_LIMIT: Final = 1 << _RAND_BITS
_TS_LIMIT: Final = 1 << _TS_BITS
_SOURCE_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
_ENTITY_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_BUILD_ID_RE: Final = re.compile(r"\d{8}-\d{6}-[0-9A-HJKMNP-TV-Z]{6}", re.ASCII)
_BUILD_ID_LEN: Final = 22
_FIRST_CONTROL: Final = 32
_DEL_CHAR: Final = 127
_MAX_JSON_DEPTH: Final = 64
_TOKEN_MIN_BYTES: Final = 16
_TOKEN_MAX_BYTES: Final = 64
_WS_RE: Final = re.compile(r"\s+")


class IdKind(enum.StrEnum):
    """The closed set of prefixed ULID identifiers of design 00 §5."""

    RUN = "run"
    TASK = "task"
    JOB = "job"
    FINDING = "finding"
    REC = "rec"
    OUTCOME = "outcome"
    MEMORY = "memory"
    CLUSTER = "cluster"
    ITEM = "item"
    REQUEST = "request"
    EGRESS = "egress"
    AUDIT = "audit"


ID_PREFIXES: Final[Mapping[IdKind, str]] = types.MappingProxyType(
    {
        IdKind.RUN: "run", IdKind.TASK: "task", IdKind.JOB: "job", IdKind.FINDING: "fnd",
        IdKind.REC: "rec", IdKind.OUTCOME: "out", IdKind.MEMORY: "mem", IdKind.CLUSTER: "cl",
        IdKind.ITEM: "rev", IdKind.REQUEST: "del", IdKind.EGRESS: "egr", IdKind.AUDIT: "aud",
    }
)


class _UlidState:
    """Generator state; module state is an accepted ENG §2.3 exception (impl 00 §13.3)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_ms = -1
        self.last_rand = 0


_STATE = _UlidState()


def _encode(value: int) -> str:
    return "".join(CROCKFORD_ALPHABET[(value >> (5 * (25 - i))) & 31] for i in range(ULID_LEN))


def new_ulid() -> str:
    """Return a ULID, strictly increasing within this process.

    Raises SchemaViolation when the timestamp overflows 48 bits (after year 10889).
    """
    ms = time.time_ns() // 1_000_000
    state = _STATE
    with state.lock:
        if ms > state.last_ms:
            rand = secrets.randbits(_RAND_BITS)
        else:
            ms = state.last_ms
            rand = state.last_rand + 1
            if rand >= _RAND_LIMIT:
                ms = state.last_ms + 1
                rand = secrets.randbits(_RAND_BITS)
        state.last_ms = ms
        state.last_rand = rand
    if ms >= _TS_LIMIT:
        msg = "ULID timestamp overflow"
        raise SchemaViolation(msg)
    return _encode((ms << _RAND_BITS) | rand)


def new_id(kind: IdKind) -> str:
    """Return a new prefixed identifier such as ``run_01J8...``. Raises as new_ulid."""
    return ID_PREFIXES[kind] + "_" + new_ulid()


def new_build_id(now: datetime.datetime) -> str:
    """Return a warehouse build_id ``YYYYMMDD-HHMMSS-<ulid6>`` from an aware time.

    Raises SchemaViolation for a naive time.
    """
    u = clock.ensure_utc(now)
    stamp = f"{u.year:04d}{u.month:02d}{u.day:02d}-{u.hour:02d}{u.minute:02d}{u.second:02d}"
    return stamp + "-" + new_ulid()[-6:]


def is_valid_ulid(value: object) -> bool:
    """True iff value is canonical ULID text."""
    return isinstance(value, str) and len(value) == ULID_LEN and bool(_ULID_RE.fullmatch(value))


def is_valid_id(kind: IdKind, value: object) -> bool:
    """True iff value is ``<prefix of kind>_<valid ULID>``."""
    if not isinstance(value, str):
        return False
    prefix = ID_PREFIXES[kind] + "_"
    return value.startswith(prefix) and is_valid_ulid(value[len(prefix) :])


def is_valid_build_id(value: object) -> bool:
    """True iff value is a build_id whose date-time part is a real calendar instant."""
    if not isinstance(value, str) or len(value) != _BUILD_ID_LEN:
        return False
    if _BUILD_ID_RE.fullmatch(value) is None:
        return False
    try:
        datetime.datetime.strptime(value[:15], "%Y%m%d-%H%M%S")  # noqa: DTZ007 - shape check only
    except ValueError:
        return False
    return True


def _valid_key(key: str) -> bool:
    if not 0 < len(key) <= RECORD_KEY_MAX_LEN or key != key.strip():
        return False
    return not any(ord(char) < _FIRST_CONTROL or ord(char) == _DEL_CHAR for char in key)


def _check_parts(source: str, entity: str, source_key: str) -> None:
    msg = "bad record_id part"
    if _SOURCE_RE.fullmatch(source) is None:
        raise SchemaViolation(msg, part="source")
    if _ENTITY_RE.fullmatch(entity) is None:
        raise SchemaViolation(msg, part="entity")
    if not _valid_key(source_key):
        raise SchemaViolation(msg, part="source_key", length=len(source_key))


def make_record_id(source: str, entity: str, source_key: str) -> str:
    """Build ``<source>:<entity>:<source_key>``. Raises SchemaViolation (part, length)."""
    _check_parts(source, entity, source_key)
    return f"{source}:{entity}:{source_key}"


def split_record_id(record_id: str) -> tuple[str, str, str]:
    """Parse a record_id; the source key keeps further colons. Raises SchemaViolation."""
    parts = record_id.split(":", 2)
    if len(parts) != 3:  # noqa: PLR2004 - source, entity, key
        msg = "bad record_id"
        raise SchemaViolation(msg)
    source, entity, key = parts
    _check_parts(source, entity, key)
    return source, entity, key


def _convert_number(value: float | decimal.Decimal) -> object:
    if isinstance(value, float):
        if not math.isfinite(value):
            msg = "non-finite float"
            raise SchemaViolation(msg)
        return value
    if not value.is_finite():
        msg = "non-finite decimal"
        raise SchemaViolation(msg)
    return str(value)


def _convert_leaf(value: object, depth: int) -> object:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float | decimal.Decimal):
        return _convert_number(value)
    if isinstance(value, datetime.datetime):
        return clock.format_utc(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, pathlib.PurePath):
        return value.as_posix()
    if isinstance(value, enum.Enum):
        return _convert(value.value, depth)
    msg = "unsupported type"
    raise SchemaViolation(msg, type=type(value).__name__)


def _convert(value: object, depth: int) -> object:
    if depth > _MAX_JSON_DEPTH:
        msg = "canonical JSON too deep"
        raise SchemaViolation(msg)
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                msg = "non-string key"
                raise SchemaViolation(msg)
            out[key] = _convert(item, depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_convert(item, depth + 1) for item in value]
    return _convert_leaf(value, depth)


def canonical_json(value: object) -> str:
    """The one canonical JSON encoding for hashed identifiers (R-14).

    Raises SchemaViolation (type) for unsupported, non-finite, too-deep or naive values.
    """
    return json.dumps(
        _convert(value, 0), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(data: str | bytes) -> str:
    """SHA-256 hex digest; str input is UTF-8 encoded."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def normalize_sql(sql: str) -> str:
    """Collapse whitespace runs and remove trailing semicolons (design 00 §5)."""
    out = _WS_RE.sub(" ", sql).strip(" ")
    while out.endswith(";"):
        out = out[:-1].rstrip(" ")
    return out


def query_id(sql: str, params: Mapping[str, object], build_id: str) -> str:
    """``q_`` + 16 hex of SHA-256 over canonical {sql, params, build_id} (design 00 §5).

    Raises SchemaViolation for empty SQL, a bad build_id or non-canonicalisable params.
    """
    norm = normalize_sql(sql)
    if not norm:
        msg = "empty SQL for query_id"
        raise SchemaViolation(msg)
    if not is_valid_build_id(build_id):
        msg = "bad build_id for query_id"
        raise SchemaViolation(msg)
    payload = canonical_json({"sql": norm, "params": params, "build_id": build_id})
    return "q_" + sha256_hex(payload)[:16]


def new_token(nbytes: int = 32) -> str:
    """Unguessable URL-safe token for bearer values. Raises SchemaViolation (nbytes)."""
    if not _TOKEN_MIN_BYTES <= nbytes <= _TOKEN_MAX_BYTES:
        msg = "bad token size"
        raise SchemaViolation(msg, nbytes=nbytes)
    return secrets.token_urlsafe(nbytes)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/core/test_ids.py -v`
Expected: all pass. `test_st00_05` takes a few seconds.

- [ ] **Step 5: Run the benchmarks once**

Run: `uv run pytest tests/bench/test_core_bench.py -m "integration and slow" -k "BT00_01 or BT00_02" -v`
Expected: both pass on the reference PC. On a slower machine, record the measured numbers in the task report instead of changing the thresholds.

- [ ] **Step 6: Gates and coverage**

Run: `uv run ruff check herness tests`, `uv run ruff format --check .`, `uv run mypy`
Expected: 0 issues.
Run: `uv run pytest tests/unit/core/test_ids.py --cov=herness.core.ids --cov-branch --cov-report=term-missing`
Expected: ≥ 90 % line and ≥ 85 % branch; the file has ≤ 330 lines.

- [ ] **Step 7: Commit**

```bash
git add herness/core/ids.py tests/unit/core/test_ids.py tests/bench/test_core_bench.py
git commit -m "feat(core): add ULIDs, prefixed IDs, canonical JSON and query_id (T00-05)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
## Task 6: Log pipeline processors and file handler (T00-06)

Units U00-40 … U00-43, plus the U00-35 constants the pipeline needs (read impl 00 §3.4). Tests UT00-47, FT00-01, and the processor-level variants of ST00-02, ST00-03, ST00-04. Task 7 runs the configured variants.

**Files:**
- Create: `herness/core/_log_pipeline.py`
- Test: `tests/unit/core/test_log_pipeline.py`

**Interfaces:**
- Consumes: `clock.now`, `clock.monotonic`, `clock.format_utc`, `clock.utc_day`; `SchemaViolation`.
- Produces (Task 7 imports and re-exports them): constants `REQUIRED_KEYS`, `CONTEXT_ID_KEYS`, `SECRET_KEYS`, `TEXT_KEYS`, `MAX_FIELD_CHARS = 2000`, `MAX_LINE_BYTES = 16384`, `MAX_DEPTH = 4`, `EVENT_NAME_RE`, `FILE_RETRY_S = 60.0`, `LOG_FILE_PREFIX = "herness-"`, `OMITTED = "[omitted]"`; processors `add_component`, `add_timestamp`, `normalize_values`, `guard_sensitive`, `limit_sizes`, `render_json` (each `(logger, method_name, event_dict) -> event_dict`, and `render_json` returns `str`); factory `check_event_name(strict: bool) -> Processor`; `class DailyJsonlHandler(logging.Handler)` with `__init__(log_dir: Path)`; module function `_open_append(path) -> TextIO` (patched by FT00-01).

- [ ] **Step 1: Write the failing tests**

`tests/unit/core/test_log_pipeline.py`:

```python
"""Tests for herness.core._log_pipeline (U00-40 … U00-43)."""

import datetime
import decimal
import json
import logging
import pathlib
from typing import Any

import pydantic
import pytest

from herness.core import _log_pipeline as lp
from herness.core import time as clock

pytestmark = pytest.mark.unit


class _Unprintable:
    def __str__(self) -> str:
        raise ValueError("no")  # noqa: EM101


def test_ut00_47_normalize_values() -> None:
    """UT00-47 values become JSON-native; deep values and unprintables are stringified."""
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    event: dict[str, Any] = {
        "dec": decimal.Decimal("1.5"),
        "path": pathlib.PureWindowsPath("C:/x/y"),
        "secret": pydantic.SecretStr("x"),
        "naive": datetime.datetime(2026, 9, 24, 10, 0),  # noqa: DTZ001
        "set": {"b", "a"},
        "deep": deep,
        "bad": _Unprintable(),
    }
    out = lp.normalize_values(None, "info", event)
    assert out["dec"] == "1.5"
    assert out["path"] == "C:/x/y"
    assert out["secret"] == "**********"
    assert out["naive"] == "2026-09-24T10:00:00 naive"
    assert out["set"] == ["a", "b"]
    assert out["deep"]["a"]["b"]["c"]["d"] == str({"e": {"f": 1}})
    assert out["bad"] == "<unprintable _Unprintable>"


def test_st00_02_guard_processor_level() -> None:
    """ST00-02 (processor level) secrets always omitted; free text omitted above DEBUG."""
    def event(level: str) -> dict[str, Any]:
        return {
            "level": level, "event": "core.test.guard", "prompt": "p", "description": "d",
            "api_key": "k", "nested": {"access_token": "t"},
        }

    info = lp.guard_sensitive(None, "info", event("info"))
    assert info["prompt"] == info["description"] == info["api_key"] == lp.OMITTED
    assert info["nested"]["access_token"] == lp.OMITTED
    debug = lp.guard_sensitive(None, "debug", event("debug"))
    assert (debug["prompt"], debug["description"]) == ("p", "d")
    assert debug["api_key"] == debug["nested"]["access_token"] == lp.OMITTED


def test_st00_03_render_json_no_line_forging() -> None:
    """ST00-03 (processor level) an injected newline cannot start a second line."""
    value = 'x"}\n{"level":"critical","event":"core.fake.injected"'
    line = lp.render_json(
        None, "info",
        {"ts": "t", "level": "info", "event": "core.test.inject", "component": "c", "v": value},
    )
    assert "\n" not in line
    assert json.loads(line)["event"] == "core.test.inject"


def test_st00_04_limit_and_drop() -> None:
    """ST00-04 (processor level) long fields cut; oversized lines drop the largest fields."""
    event: dict[str, Any] = {
        "ts": "t", "level": "info", "event": "core.test.big", "component": "c", "pid": 1,
        "huge": "h" * 100_000,
    }
    event.update({f"f{i:02d}": "x" * 1500 for i in range(20)})
    line = lp.render_json(None, "info", lp.limit_sizes(None, "info", event))
    out = json.loads(line)
    assert len(line.encode("utf-8")) <= lp.MAX_LINE_BYTES
    assert "huge" in out["dropped_fields"]
    for key in ("ts", "level", "event", "component", "pid"):
        assert key in out
    cut = lp.limit_sizes(None, "info", {"v": "h" * 100_000})["v"]
    assert cut == "h" * lp.MAX_FIELD_CHARS + "\u2026[truncated]"


def test_ft00_01_sink_failure_and_recovery(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """FT00-01 a failed open drops lines for 60 s, then recovers and reports the count."""
    real_open = lp._open_append
    calls = {"n": 0}

    def flaky(path: pathlib.Path) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")  # noqa: EM101
        return real_open(path)

    now = [0.0]
    monkeypatch.setattr(lp, "_open_append", flaky)
    monkeypatch.setattr(clock, "monotonic", lambda: now[0])
    handler = lp.DailyJsonlHandler(tmp_path)
    handler.setFormatter(logging.Formatter("%(message)s"))
    for when, message in ((0.0, "one"), (10.0, "two"), (61.0, "three")):
        now[0] = when
        handler.emit(logging.LogRecord("x", logging.INFO, __file__, 1, message, None, None))
    handler.close()
    failed = [line for line in capsys.readouterr().err.splitlines() if "sink_failed" in line]
    assert len(failed) == 1
    (day_file,) = tmp_path.glob("herness-*.jsonl")
    lines = day_file.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["event"] == "core.logging.sink_recovered"
    assert first["dropped_lines"] == 2
    assert lines[1] == "three"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/core/test_log_pipeline.py -v`
Expected: collection ERROR, `herness.core._log_pipeline` not found.

- [ ] **Step 3: Implement `herness/core/_log_pipeline.py`**

```python
"""Private structlog processors and the daily JSONL file handler (impl 00 §3.4).

Import the public names from herness.core.logging, which re-exports them.
"""

from __future__ import annotations

import contextlib
import datetime
import decimal
import enum
import json
import logging
import math
import os
import pathlib
import re
import sys
from collections.abc import Mapping
from typing import Final, TextIO

from structlog.typing import EventDict, Processor

from herness.core import time as clock
from herness.core.errors import SchemaViolation

REQUIRED_KEYS: Final = ("ts", "level", "event", "component")
CONTEXT_ID_KEYS: Final = ("run_id", "task_id", "job_id", "build_id")
SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password", "passwd", "secret", "token", "api_key", "apikey", "authorization",
        "cookie", "set_cookie", "private_key", "client_secret", "access_token", "refresh_token",
    }
)
TEXT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "prompt", "prompts", "completion", "messages", "text", "ticket_text", "description",
        "short_description", "close_notes", "comments", "summary", "body", "content",
        "payload", "raw",
    }
)
MAX_FIELD_CHARS: Final = 2000
MAX_LINE_BYTES: Final = 16384
MAX_DEPTH: Final = 4
EVENT_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}")
FILE_RETRY_S: Final = 60.0
LOG_FILE_PREFIX: Final = "herness-"
OMITTED: Final = "[omitted]"
_SECRET_SUFFIXES: Final = ("_password", "_secret", "_token", "_api_key")
_NEVER_GUARDED: Final = frozenset({"event", "component", "level", "ts"})
_META_KEYS: Final = frozenset({"_record", "_from_structlog"})
_FIRST_KEYS: Final = ("ts", "level", "event", "component", "pid", *CONTEXT_ID_KEYS)
_KEPT_KEYS: Final = frozenset({*_FIRST_KEYS, "dropped_fields"})
_TRUNCATED: Final = "\u2026[truncated]"
_EVENT_CUT: Final = 200
_MASK: Final = "**********"


def _is_secret_key(lowered: str) -> bool:
    return lowered in SECRET_KEYS or lowered.endswith(_SECRET_SUFFIXES)


def add_component(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Fill ``component`` from the standard-library logger name when it is missing."""
    if "component" in event_dict:
        return event_dict
    record = event_dict.get("_record")
    if not isinstance(record, logging.LogRecord):
        event_dict["component"] = "unknown"
    elif record.name.startswith("herness."):
        event_dict["component"] = record.name[len("herness.") :]
    else:
        event_dict["component"] = "ext." + record.name.split(".", 1)[0]
    return event_dict


def add_timestamp(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Add ``ts`` (fixed-width UTC text, follows FakeClock) and ``pid``."""
    event_dict["ts"] = clock.format_utc(clock.now())
    event_dict["pid"] = os.getpid()
    return event_dict


def _safe_str(value: object) -> str:
    try:
        return str(value)
    except (ValueError, TypeError, RecursionError):
        return "<unprintable " + type(value).__name__ + ">"


def _is_pydantic_secret(value: object) -> bool:
    cls = type(value)
    return cls.__name__ in {"SecretStr", "SecretBytes"} and cls.__module__.startswith("pydantic")


def _normalize_leaf(value: object, depth: int) -> object:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.isoformat() + " naive"
        return clock.format_utc(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, pathlib.PurePath):
        return value.as_posix()
    if isinstance(value, enum.Enum):
        return _normalize(value.value, depth)
    if _is_pydantic_secret(value):
        return _MASK
    if isinstance(value, BaseException):
        return type(value).__name__ + ": " + _safe_str(value)
    return _safe_str(value)


def _normalize(value: object, depth: int) -> object:
    if depth > MAX_DEPTH:
        return _safe_str(value)
    if value is None or isinstance(value, str | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(item, depth + 1) for item in value]
    if isinstance(value, set | frozenset):
        return [_normalize(item, depth + 1) for item in sorted(value, key=str)]
    return _normalize_leaf(value, depth)


def normalize_values(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Make every value JSON-native (depth 4) before guarding, scrubbing and truncation."""
    for key in list(event_dict):
        if key not in _META_KEYS:
            event_dict[key] = _normalize(event_dict[key], 1)
    return event_dict


def _guard(key: str, value: object, is_debug: bool, depth: int) -> object:
    lowered = key.lower()
    if _is_secret_key(lowered) or (lowered in TEXT_KEYS and not is_debug):
        return OMITTED
    if isinstance(value, dict) and depth < MAX_DEPTH:
        return {str(k): _guard(str(k), v, is_debug, depth + 1) for k, v in value.items()}
    return value


def guard_sensitive(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Drop secret-named fields always and free-text fields above DEBUG (ENG §3.6)."""
    is_debug = event_dict.get("level") == "debug"
    for key in list(event_dict):
        if key not in _NEVER_GUARDED:
            event_dict[key] = _guard(key, event_dict[key], is_debug, 1)
    return event_dict


def check_event_name(strict: bool) -> Processor:
    """Return a processor enforcing ``component.object.action`` event names.

    In strict mode a bad name raises SchemaViolation (event) in the caller's thread.
    """

    def _check(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
        event = event_dict.get("event")
        if isinstance(event, str) and EVENT_NAME_RE.fullmatch(event):
            return event_dict
        if strict:
            msg = "invalid log event name"
            raise SchemaViolation(msg, event=str(event)[:120])
        event_dict["event_name_invalid"] = True
        return event_dict

    return _check


def _limit(value: object) -> object:
    if isinstance(value, str):
        return value if len(value) <= MAX_FIELD_CHARS else value[:MAX_FIELD_CHARS] + _TRUNCATED
    if isinstance(value, dict):
        return {key: _limit(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_limit(item) for item in value]
    return value


def limit_sizes(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Cut every string value longer than MAX_FIELD_CHARS characters."""
    for key in list(event_dict):
        event_dict[key] = _limit(event_dict[key])
    return event_dict


def _dump(ordered: Mapping[str, object]) -> str:
    return json.dumps(
        ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str
    )


def _largest_optional(ordered: Mapping[str, object]) -> str | None:
    victim, size = None, -1
    for key, value in ordered.items():
        if key in _KEPT_KEYS:
            continue
        length = len(_dump({"v": value}))
        if length >= size:
            victim, size = key, length
    return victim


def render_json(logger: object, method_name: str, event_dict: EventDict) -> str:
    """Render one JSON line: required keys first, at most MAX_LINE_BYTES bytes."""
    ordered: dict[str, object] = {k: event_dict[k] for k in _FIRST_KEYS if k in event_dict}
    ordered.update((k, v) for k, v in event_dict.items() if k not in ordered)
    line = _dump(ordered)
    dropped: list[str] = []
    while len(line.encode("utf-8")) > MAX_LINE_BYTES:
        victim = _largest_optional(ordered)
        if victim is None:
            ordered["event"] = str(ordered.get("event", ""))[:_EVENT_CUT]
            return _dump(ordered)
        del ordered[victim]
        dropped.append(victim)
        ordered["dropped_fields"] = sorted(dropped)
        line = _dump(ordered)
    return line


def _open_append(path: pathlib.Path) -> TextIO:
    return path.open("a", encoding="utf-8", newline="\n")


def _status_line(level: str, event: str, path: pathlib.Path, **fields: object) -> str:
    body: dict[str, object] = {
        "ts": clock.format_utc(clock.now()), "level": level, "event": event,
        "component": "core.logging", "path": path.as_posix(),
    }
    body.update(fields)
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


class DailyJsonlHandler(logging.Handler):
    """Append lines to ``<log_dir>/herness-<UTC date>.jsonl``; survive disk errors.

    emit never raises: an OSError drops lines for FILE_RETRY_S seconds, reported on stderr.
    """

    def __init__(self, log_dir: pathlib.Path) -> None:
        super().__init__()
        self._log_dir = log_dir
        self._day: str | None = None
        self._stream: TextIO | None = None
        self._failed_at: float | None = None
        self._dropped = 0

    def _path(self, day: str) -> pathlib.Path:
        return self._log_dir / (LOG_FILE_PREFIX + day + ".jsonl")

    def _ensure_stream(self) -> tuple[pathlib.Path, TextIO]:
        day = clock.utc_day(clock.now())
        path = self._path(day)
        if day != self._day or self._stream is None:
            self._close_stream()
            self._stream = _open_append(path)
            self._day = day
        return path, self._stream

    def _close_stream(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(OSError):
                self._stream.close()
        self._stream = None

    def emit(self, record: logging.LogRecord) -> None:
        """Write one formatted record; see the class docstring for failure handling."""
        line = self.format(record)
        if self._failed_at is not None and clock.monotonic() - self._failed_at < FILE_RETRY_S:
            self._dropped += 1
            return
        path = self._path(self._day or clock.utc_day(clock.now()))
        try:
            path, stream = self._ensure_stream()
            if self._failed_at is not None:
                recovered = _status_line(
                    "info", "core.logging.sink_recovered", path, dropped_lines=self._dropped
                )
                stream.write(recovered + "\n")
                sys.stderr.write(recovered + "\n")
                self._failed_at = None
                self._dropped = 0
            stream.write(line + "\n")
            stream.flush()
        except OSError as exc:
            self._close_stream()
            self._failed_at = clock.monotonic()
            self._dropped += 1
            failed = _status_line(
                "error", "core.logging.sink_failed", path,
                error_type=type(exc).__name__, retry_in_s=FILE_RETRY_S,
            )
            sys.stderr.write(failed + "\n")

    def close(self) -> None:
        """Close the day file, then the handler."""
        self.acquire()
        try:
            self._close_stream()
        finally:
            self.release()
        super().close()
```

Note on FT00-01: the first line that fails is counted in `_dropped` together with the line dropped during the retry window, so the recovery line reports `dropped_lines = 2`, as the test expects.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/core/test_log_pipeline.py -v`
Expected: 5 passed.

- [ ] **Step 5: Gates**

Run: `uv run ruff check herness tests`, `uv run ruff format --check .`, `uv run mypy`
Expected: 0 issues. The file has ≤ 360 lines.

- [ ] **Step 6: Commit**

```bash
git add herness/core/_log_pipeline.py tests/unit/core/test_log_pipeline.py
git commit -m "feat(core): add log pipeline processors and daily JSONL handler (T00-06)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Public logging API (T00-07)

Units U00-35 … U00-39 (read impl 00 §3.4). Tests UT00-35 … UT00-46, ST00-01, BT00-03, plus Review Focus 5.

**Deviation 7 (spec defect):** U00-38 says to return `structlog.stdlib.get_logger(...).bind(component=...)`. structlog's lazy proxy resolves the configuration inside `bind()`, so a logger created at import time would keep structlog's defaults forever. Instead, pass `component` as an initial value, `structlog.stdlib.get_logger(name, component=component)`, which stays lazy. Review Focus 5 pins this behaviour.

**Files:**
- Create: `herness/core/logging.py`, `tests/unit/core/conftest.py`
- Modify: `tests/bench/test_core_bench.py` (add BT00-03)
- Test: `tests/unit/core/test_logging.py`

**Interfaces:**
- Consumes: everything Task 6 produces; `ConfigError`, `SchemaViolation`; `IdKind`, `is_valid_id`, `is_valid_build_id`.
- Produces: `configure_logging(level="INFO", *, log_dir=None, scrubber=None, stderr=True, strict_event_names=False) -> None`; `get_logger(component: str) -> structlog.stdlib.BoundLogger`; `bind_ids(**ids: str)` context manager; `reset_logging() -> None`; `LogLevel`; `COMPONENT_RE`; `NOISY_LOGGERS`; and the re-exported constants of Task 6.

- [ ] **Step 1: Write the fixture and the failing tests**

`tests/unit/core/conftest.py`:

```python
"""Fixtures for foundation unit tests (impl 00 §11)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from structlog.typing import EventDict

from herness.core.logging import configure_logging, reset_logging

SENTINEL = "SENTINEL-SECRET-9f3a"  # noqa: S105 - test sentinel, not a secret


def _scrub(value: object) -> object:
    if isinstance(value, str):
        return value.replace(SENTINEL, "***")
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    return value


def sentinel_scrubber(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        event_dict[key] = _scrub(event_dict[key])
    return event_dict


@pytest.fixture
def configured_logging(tmp_path: Path) -> Iterator[Path]:
    configure_logging(
        "DEBUG", log_dir=tmp_path, scrubber=sentinel_scrubber, strict_event_names=True
    )
    yield tmp_path
    reset_logging()
```

`tests/unit/core/test_logging.py`:

```python
"""Tests for herness.core.logging (U00-35 … U00-39)."""

import asyncio
import datetime
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from herness.core import time as clock
from herness.core._log_pipeline import DailyJsonlHandler
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.ids import IdKind, new_id
from herness.core.logging import bind_ids, configure_logging, get_logger, reset_logging

pytestmark = pytest.mark.unit

SENTINEL = "SENTINEL-SECRET-9f3a"  # noqa: S105 - test sentinel, not a secret
UTC = datetime.UTC
_EARLY = get_logger("core.early")


def _passthrough(logger: object, method_name: str, event_dict: Any) -> Any:
    return event_dict


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    reset_logging()


def _file_lines(log_dir: Path) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for path in sorted(log_dir.glob("herness-*.jsonl")):
        lines += [json.loads(text) for text in path.read_text(encoding="utf-8").splitlines()]
    return lines


def _err_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.strip()]


def _event(lines: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(line for line in lines if line.get("event") == name)


def test_ut00_35_json_line_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-35 one JSON line with the required keys and the extra field."""
    configure_logging("INFO")
    get_logger("core.test").info("core.test.done", n=1)
    line = _event(_err_lines(capsys), "core.test.done")
    assert line["level"] == "info"
    assert line["component"] == "core.test"
    assert len(line["ts"]) == 27
    assert isinstance(line["pid"], int)
    assert line["n"] == 1


def test_ut00_36_day_rollover(configured_logging: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-36 lines on either side of UTC midnight go to two day files."""
    current = [datetime.datetime(2030, 1, 1, 23, 59, 59, 900_000, tzinfo=UTC)]
    monkeypatch.setattr(clock, "now", lambda: current[0])
    log = get_logger("core.test")
    log.info("core.test.before")
    current[0] = datetime.datetime(2030, 1, 2, 0, 0, 0, 100_000, tzinfo=UTC)
    log.info("core.test.after")
    first = (configured_logging / "herness-2030-01-01.jsonl").read_text(encoding="utf-8")
    second = (configured_logging / "herness-2030-01-02.jsonl").read_text(encoding="utf-8")
    assert len(first.splitlines()) == 1
    assert len(second.splitlines()) == 1


def test_ut00_37_configuration_errors(tmp_path: Path) -> None:
    """UT00-37 bad level, file without scrubber, and an uncreatable directory."""
    with pytest.raises(ConfigError):
        configure_logging("VERBOSE")
    with pytest.raises(ConfigError):
        configure_logging(log_dir=tmp_path)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError):
        configure_logging(log_dir=blocker / "sub", scrubber=_passthrough)


@pytest.mark.asyncio
async def test_ut00_38_bind_ids_nesting_and_tasks(configured_logging: Path) -> None:
    """UT00-38 nested bindings stack and restore; asyncio tasks keep their own IDs."""
    log = get_logger("core.test")
    run_id, task_id = new_id(IdKind.RUN), new_id(IdKind.TASK)
    with bind_ids(run_id=run_id):
        with bind_ids(task_id=task_id):
            log.info("core.test.inner")
        log.info("core.test.outer")

    async def work(rid: str) -> None:
        with bind_ids(run_id=rid):
            await asyncio.sleep(0)
            log.info("core.test.task", marker=rid)

    await asyncio.gather(work(new_id(IdKind.RUN)), work(new_id(IdKind.RUN)))
    lines = _file_lines(configured_logging)
    inner, outer = _event(lines, "core.test.inner"), _event(lines, "core.test.outer")
    assert (inner["run_id"], inner["task_id"]) == (run_id, task_id)
    assert outer["run_id"] == run_id
    assert "task_id" not in outer
    tasks = [line for line in lines if line["event"] == "core.test.task"]
    assert len(tasks) == 2
    assert all(line["run_id"] == line["marker"] for line in tasks)


def test_ut00_39_bind_ids_rejects(configured_logging: Path) -> None:
    """UT00-39 unknown keys and invalid IDs are refused without echoing the value."""
    with pytest.raises(SchemaViolation), bind_ids(user="x"):
        pass
    with pytest.raises(SchemaViolation) as info, bind_ids(run_id="run_bad\n{"):
        pass
    assert all("run_bad" not in str(value) for value in info.value.context.values())


def test_ut00_40_get_logger_component() -> None:
    """UT00-40 invalid component names are refused."""
    for bad in ("Harness", "a" * 65):
        with pytest.raises(SchemaViolation):
            get_logger(bad)
    assert get_logger("harness.tool") is not None


def test_ut00_41_key_order(configured_logging: Path) -> None:
    """UT00-41 required keys, pid and context IDs come first, then insertion order."""
    with bind_ids(job_id=new_id(IdKind.JOB)):
        get_logger("core.test").info("core.test.order", zeta=1, alpha=2)
    line = _event(_file_lines(configured_logging), "core.test.order")
    assert list(line)[:8] == [
        "ts", "level", "event", "component", "pid", "job_id", "zeta", "alpha",
    ]


def test_ut00_42_event_names(configured_logging: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-42 strict mode raises to the caller; non-strict marks the line."""
    with pytest.raises(SchemaViolation):
        get_logger("core.test").info("Bad Event")
    configure_logging("INFO")
    get_logger("core.test").info("Bad Event")
    line = _event(_err_lines(capsys), "Bad Event")
    assert line["event_name_invalid"] is True


def test_ut00_43_third_party_loggers(capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-43 noisy loggers drop INFO; foreign records get an ext.* component."""
    configure_logging("INFO")
    logging.getLogger("httpx").info("hidden")
    logging.getLogger("httpx").warning("shown")
    logging.getLogger("thirdparty.sub").warning("other")
    lines = _err_lines(capsys)
    assert not [line for line in lines if line["event"] == "hidden"]
    assert _event(lines, "shown")["component"] == "ext.httpx"
    assert _event(lines, "other")["component"] == "ext.thirdparty"


def _fail() -> None:
    password_local = SENTINEL  # noqa: S105, F841 - must not reach the log
    raise ValueError("boom")  # noqa: EM101


def test_ut00_44_exception_without_locals(configured_logging: Path) -> None:
    """UT00-44 exceptions are rendered without locals, so local secrets never appear."""
    try:
        _fail()
    except ValueError:
        get_logger("core.test").exception("core.test.failed")
    line = _event(_file_lines(configured_logging), "core.test.failed")
    assert "exception" in line
    assert "locals" not in json.dumps(line["exception"])
    for path in configured_logging.glob("*.jsonl"):
        assert SENTINEL not in path.read_text(encoding="utf-8")


def test_ut00_45_reset(configured_logging: Path) -> None:
    """UT00-45 reset removes Herness handlers, closes the file and clears context."""
    get_logger("core.test").info("core.test.x")
    reset_logging()
    root = logging.getLogger()
    assert not [h for h in root.handlers if isinstance(h, DailyJsonlHandler)]
    for path in configured_logging.glob("*.jsonl"):
        os.remove(path)
    assert structlog.contextvars.get_contextvars() == {}


def test_ut00_46_configured_event(configured_logging: Path) -> None:
    """UT00-46 the first file line is core.logging.configured with scrubber true."""
    first = _file_lines(configured_logging)[0]
    assert first["event"] == "core.logging.configured"
    assert first["scrubber"] is True


def test_st00_01_sentinel_never_written(
    capsys: pytest.CaptureFixture[str], configured_logging: Path
) -> None:
    """ST00-01 the scrubber removes the sentinel from values, exceptions and long strings."""
    log = get_logger("core.test")
    log.info("core.test.value", value="x " + SENTINEL)
    try:
        raise ValueError(SENTINEL)
    except ValueError:
        log.exception("core.test.exc")
    log.info("core.test.long", long="a" * 1995 + SENTINEL + "b" * 100)
    text = "".join(p.read_text(encoding="utf-8") for p in configured_logging.glob("*.jsonl"))
    assert SENTINEL not in text
    assert SENTINEL not in capsys.readouterr().err
    assert "***" in text


def test_rf_logger_created_before_configure(capsys: pytest.CaptureFixture[str]) -> None:
    """RF-5 a logger created at import time still emits configured JSON lines."""
    configure_logging("INFO")
    _EARLY.info("core.early.ready")
    line = _event(_err_lines(capsys), "core.early.ready")
    assert line["component"] == "core.early"
```

In `tests/bench/test_core_bench.py`, add this import next to `from herness.core import ids`:

```python
from herness.core.logging import configure_logging, get_logger, reset_logging
```

and append:

```python
def test_bt00_03_file_logging_throughput(tmp_path: Any) -> None:
    """BT00-03 10,000 INFO lines of 10 fields through the file handler in under 1 s."""

    def passthrough(logger: object, method_name: str, event_dict: Any) -> Any:
        return event_dict

    configure_logging("INFO", log_dir=tmp_path, scrubber=passthrough, stderr=False)
    log = get_logger("core.bench")
    fields = {f"f{i}": i for i in range(10)}
    start = time.perf_counter()
    for _ in range(10_000):
        log.info("core.bench.line", **fields)
    elapsed = time.perf_counter() - start
    reset_logging()
    assert elapsed < 1.0
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/core/test_logging.py -v`
Expected: collection ERROR, `herness.core.logging` not found.

- [ ] **Step 3: Implement `herness/core/logging.py`**

```python
"""Public logging API: configure, get a logger, bind context IDs, reset (design 00 §8).

structlog hands every event to the standard-library logging module, so structlog events
and third-party records share one JSON-lines formatter chain (impl 00 §3.4).
"""

from __future__ import annotations

import contextlib
import logging
import pathlib
import re
import sys
import threading
import types
from collections.abc import Callable, Iterator, Mapping
from typing import Final, Literal, cast

import structlog
from structlog.processors import ExceptionRenderer
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import Processor

from herness.core._log_pipeline import (
    CONTEXT_ID_KEYS,
    EVENT_NAME_RE,
    FILE_RETRY_S,
    LOG_FILE_PREFIX,
    MAX_DEPTH,
    MAX_FIELD_CHARS,
    MAX_LINE_BYTES,
    OMITTED,
    REQUIRED_KEYS,
    SECRET_KEYS,
    TEXT_KEYS,
    DailyJsonlHandler,
    add_component,
    add_timestamp,
    check_event_name,
    guard_sensitive,
    limit_sizes,
    normalize_values,
    render_json,
)
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.ids import IdKind, is_valid_build_id, is_valid_id

__all__ = [
    "COMPONENT_RE", "CONTEXT_ID_KEYS", "EVENT_NAME_RE", "FILE_RETRY_S", "LOG_FILE_PREFIX",
    "MAX_DEPTH", "MAX_FIELD_CHARS", "MAX_LINE_BYTES", "NOISY_LOGGERS", "OMITTED",
    "REQUIRED_KEYS", "SECRET_KEYS", "TEXT_KEYS", "LogLevel", "bind_ids", "configure_logging",
    "get_logger", "reset_logging",
]

type LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
_LEVELS: Final = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
COMPONENT_RE: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*")
_MAX_COMPONENT_CHARS: Final = 64
NOISY_LOGGERS: Final = (
    "httpx", "httpcore", "urllib3", "asyncio", "filelock", "huggingface_hub",
    "snowflake.connector", "pymongo", "openai", "anthropic",
)
_ID_VALIDATORS: Final[Mapping[str, Callable[[object], bool]]] = types.MappingProxyType(
    {
        "run_id": lambda value: is_valid_id(IdKind.RUN, value),
        "task_id": lambda value: is_valid_id(IdKind.TASK, value),
        "job_id": lambda value: is_valid_id(IdKind.JOB, value),
        "build_id": is_valid_build_id,
    }
)


class _State:
    """Handlers installed by this module (accepted ENG §2.3 exception, impl 00 §13.3)."""

    def __init__(self) -> None:
        self.handlers: list[logging.Handler] = []


_STATE: Final = _State()
_CONFIG_LOCK: Final = threading.Lock()


def _formatter(scrubber: Processor | None) -> logging.Formatter:
    steps: list[Processor] = [
        add_component,
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        structlog.stdlib.add_log_level,
        add_timestamp,
        ExceptionRenderer(ExceptionDictTransformer(show_locals=False, max_frames=20)),
        normalize_values,
        guard_sensitive,
    ]
    if scrubber is not None:
        steps.append(scrubber)
    steps += [limit_sizes, render_json]
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[structlog.contextvars.merge_contextvars], processors=steps
    )


def _remove_handlers() -> None:
    root = logging.getLogger()
    for handler in _STATE.handlers:
        root.removeHandler(handler)
        handler.close()
    _STATE.handlers = []


def _install(lvl: str, handlers: list[logging.Handler], strict: bool) -> None:
    root = logging.getLogger()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(lvl)
    noisy = max(logging.getLevelNamesMapping()[lvl], logging.WARNING)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(noisy)
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.contextvars.merge_contextvars,
            check_event_name(strict),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    _STATE.handlers = handlers


def configure_logging(
    level: str = "INFO",
    *,
    log_dir: pathlib.Path | None = None,
    scrubber: Processor | None = None,
    stderr: bool = True,
    strict_event_names: bool = False,
) -> None:
    """Configure process-wide JSON-lines logging for structlog and standard logging.

    Raises ConfigError (level) for a bad level, ConfigError when log_dir is given without a
    scrubber, and ConfigError (path) when log_dir cannot be created.
    """
    lvl = level.upper()
    if lvl not in _LEVELS:
        msg = "invalid log level"
        raise ConfigError(msg, level=level[:20])
    if log_dir is not None and scrubber is None:
        msg = "file logging requires a scrubber"
        raise ConfigError(msg)
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = "cannot create log directory"
            raise ConfigError(msg, path=str(log_dir)) from exc
    with _CONFIG_LOCK:
        _remove_handlers()
        formatter = _formatter(scrubber)
        handlers: list[logging.Handler] = []
        if stderr:
            handlers.append(logging.StreamHandler(sys.stderr))
        if log_dir is not None:
            handlers.append(DailyJsonlHandler(log_dir))
        for handler in handlers:
            handler.setFormatter(formatter)
        _install(lvl, handlers, strict_event_names)
    get_logger("core.logging").info(
        "core.logging.configured", level=lvl,
        log_dir=None if log_dir is None else log_dir.as_posix(),
        scrubber=scrubber is not None, strict_event_names=strict_event_names,
    )


@contextlib.contextmanager
def bind_ids(**ids: str) -> Iterator[None]:
    """Attach run/task/job/build IDs to every line in this block (thread or task only).

    Raises SchemaViolation (key) for an unknown key or an invalid ID; values are not echoed.
    """
    for key, value in ids.items():
        validator = _ID_VALIDATORS.get(key)
        if validator is None:
            msg = "unknown log context key"
            raise SchemaViolation(msg, key=key[:40])
        if not validator(value):
            msg = "invalid id for log context"
            raise SchemaViolation(msg, key=key)
    tokens = structlog.contextvars.bind_contextvars(**ids)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def get_logger(component: str) -> structlog.stdlib.BoundLogger:
    """Return a logger whose lines carry ``component``. Raises SchemaViolation (component).

    The logger stays lazy, so one created at import time follows later configuration.
    """
    if len(component) > _MAX_COMPONENT_CHARS or COMPONENT_RE.fullmatch(component) is None:
        msg = "invalid log component"
        raise SchemaViolation(msg, component=component[:_MAX_COMPONENT_CHARS])
    return cast(
        "structlog.stdlib.BoundLogger",
        structlog.stdlib.get_logger("herness." + component, component=component),
    )


def reset_logging() -> None:
    """Undo configure_logging: remove and close handlers, restore defaults, clear context."""
    with _CONFIG_LOCK:
        _remove_handlers()
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/unit/core/test_logging.py -v`
Expected: all pass. If UT00-43 fails because another test left root-level handlers, check that every test either uses the fixture or relies on the autouse `_reset`.

- [ ] **Step 5: Benchmark once**

Run: `uv run pytest tests/bench/test_core_bench.py -m "integration and slow" -k BT00_03 -v`
Expected: PASS on the reference PC (record the time if it fails elsewhere).

- [ ] **Step 6: Gates and coverage**

Run: `uv run ruff check herness tests`, `uv run ruff format --check .`, `uv run mypy`
Expected: 0 issues.
Run: `uv run pytest tests/unit/core --cov=herness.core --cov-branch --cov-report=term-missing`
Expected: every `herness/core` file so far ≥ 90 % line and ≥ 85 % branch; `logging.py` has ≤ 260 lines.

- [ ] **Step 7: Commit**

```bash
git add herness/core/logging.py tests/unit/core/conftest.py tests/unit/core/test_logging.py tests/bench/test_core_bench.py
git commit -m "feat(core): add structured JSON logging API (T00-07)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---
## Task 8: Shared-types package and ownership checker (T00-08)

Units U00-44 … U00-47 (read impl 00 §3.5). Tests UT00-48 … UT00-54, UT00-72, UT00-73, UT00-80, UT00-81, ST00-15.

**Files:**
- Create: `herness/core/types/__init__.py`, `herness/core/types/_ownership.py`, `tools/__init__.py`, `tools/check_type_ownership.py`
- Modify: `pyproject.toml` (`[tool.mypy] files` gains `"tools"`)
- Test: `tests/unit/core/test_types_ownership.py`, `tests/unit/tools/test_check_type_ownership.py`

**Interfaces:**
- Consumes: nothing at run time. The checker parses files and never imports Herness.
- Produces: `herness.core.types` (empty `__all__` until owner specs add submodules); `herness.core.types._ownership.TYPE_OWNERS: Mapping[str, str]`, `OWNER_MODULES: Mapping[str, str]`, `OWNER_IMPORTS: Mapping[str, frozenset[str]]`, `DECLARED_ELSEWHERE: Mapping[str, tuple[str, str]]`; `tools.check_type_ownership.main(argv: Sequence[str] | None = None) -> int` with violation codes `OWN001`–`OWN090`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/core/test_types_ownership.py`:

```python
"""Tests for the shared-types package and its ownership tables (U00-44, U00-45, U00-46)."""

import ast
from pathlib import Path

import pytest

import herness.core.types as shared
from herness.core.types import _ownership as own

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]

EXPECTED = {
    "03": {"QuestionType", "Entity", "Question", "QuestionSet", "DecisionInput", "Answer",
           "DecisionOutput"},
    "05": {"TextPart", "ToolCall", "ToolCallPart", "ToolResultPart", "ReasoningPart", "Message",
           "SystemBlock", "ToolSpec", "RequestMeta", "LLMRequest", "Usage", "LLMResponse", "Tool",
           "AsyncTool", "ToolErrorInfo", "ToolResult", "SqlLimits", "Budgets", "BudgetLedger",
           "TraceEmitter", "WarehouseHandle", "OpsHandle", "VectorHandle", "VectorHit",
           "ToolContext", "NumberRef", "Evidence", "NumberCheck", "UncitedSpan", "ItemResult",
           "VerificationResult", "VerifiableItem", "LoopSignal", "LoopLimits", "LoopState",
           "LoopCheckpoint", "AgentResult"},
    "06": {"RunKind", "Depth", "Role", "Specialty", "ScopeEntityType", "SkepticCheck",
           "SKEPTIC_CHECKS", "RejectReason", "FindingStatus", "Banner", "SectionId",
           "EntityScope", "TaskInputs", "TaskBudget", "TaskSpec", "PlannedTask", "Finding",
           "CheckResult", "Challenge", "CrossCheck", "VerificationRecord", "SwarmTaskState",
           "Paragraph", "Section", "RecommendationItem", "RankedEntity", "Coverage",
           "ReportDraft", "ChatAnswer", "ChatEvent"},
    "07": {"Layer", "Kind", "Status", "KIND_LAYER", "Provenance", "MemoryItem", "MemoryProposal",
           "RecallHit", "MemoryRunContext", "RecommendationDraft", "PriorRecommendation",
           "PriorContext", "ConfidenceAdjustment", "SimilarOutcome"},
    "08": {"GpuClass", "JobKind", "ServiceName", "ChatMode", "BreakerState", "PolicyName",
           "JobSpec", "JobOutcome", "MetricSample"},
    "09": {"ReportManifest"},
}


def test_ut00_48_init_reexports_only() -> None:
    """UT00-48 __all__ equals the owner names of existing submodules; no definitions."""
    present = {
        owner for owner, sub in own.OWNER_MODULES.items()
        if (ROOT / "herness/core/types" / f"{sub}.py").exists()
        or (ROOT / "herness/core/types" / sub / "__init__.py").exists()
    }
    expected = sorted(n for n, o in own.TYPE_OWNERS.items() if o in present)
    assert list(shared.__all__) == expected
    tree = ast.parse((ROOT / "herness/core/types/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        assert not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        if isinstance(node, ast.Assign | ast.AnnAssign):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            assert isinstance(target, ast.Name) and target.id == "__all__"


def test_ut00_80_ownership_tables() -> None:
    """UT00-80 names per owner, no duplicates, exclusions and an acyclic import graph."""
    by_owner: dict[str, set[str]] = {}
    for name, owner in own.TYPE_OWNERS.items():
        by_owner.setdefault(owner, set()).add(name)
    assert by_owner == EXPECTED
    assert not set(own.TYPE_OWNERS) & set(own.DECLARED_ELSEWHERE)
    assert "impact_usd" not in own.TYPE_OWNERS
    assert "JobContext" not in own.TYPE_OWNERS
    assert own.OWNER_IMPORTS["06"] == frozenset({"harness", "jobs"})
    subs = {sub: owner for owner, sub in own.OWNER_MODULES.items()}
    visiting: set[str] = set()

    def visit(owner: str) -> None:
        assert owner not in visiting, "cycle"
        visiting.add(owner)
        for sub in own.OWNER_IMPORTS[owner]:
            visit(subs[sub])
        visiting.discard(owner)

    for owner in own.OWNER_MODULES:
        visit(owner)
```

`tests/unit/tools/test_check_type_ownership.py`:

```python
"""Tests for tools.check_type_ownership (U00-47)."""

import shutil
from pathlib import Path

import pytest

from herness.core.types._ownership import TYPE_OWNERS
from tools.check_type_ownership import main

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NAMES_05 = sorted(n for n, o in TYPE_OWNERS.items() if o == "05")
NAMES_06 = sorted(n for n, o in TYPE_OWNERS.items() if o == "06")


def _classes(names: list[str]) -> str:
    return "".join(f"class {name}:\n    pass\n\n\n" for name in names)


def _tree(tmp: Path, reexports: dict[str, list[str]] | None = None) -> Path:
    types_dir = tmp / "herness" / "core" / "types"
    types_dir.mkdir(parents=True)
    shutil.copy(ROOT / "herness/core/types/_ownership.py", types_dir / "_ownership.py")
    lines = ['"""Shared types."""']
    everything: list[str] = []
    for sub, names in (reexports or {}).items():
        lines.append(f"from herness.core.types.{sub} import {', '.join(sorted(names))}")
        everything += names
    lines.append(f"__all__: tuple[str, ...] = {tuple(sorted(everything))!r}")
    (types_dir / "__init__.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = main(["--root", str(root)])
    return code, capsys.readouterr().out


def test_ut00_49_missing_type(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-49 an owner submodule missing a registered name gives OWN010."""
    root = _tree(tmp_path, {"harness": NAMES_05})
    _write(root, "herness/core/types/harness.py", _classes([n for n in NAMES_05 if n != "NumberRef"]))
    code, out = _run(root, capsys)
    assert code == 1
    assert "OWN010" in out and "NumberRef" in out


def test_ut00_50_unregistered_and_wrong_owner(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-50 an extra public name gives OWN011; another owner's name gives OWN012."""
    root = _tree(tmp_path, {"harness": NAMES_05})
    _write(root, "herness/core/types/harness.py", _classes([*NAMES_05, "Extra", "Finding"]))
    _, out = _run(root, capsys)
    assert "OWN011 unregistered Extra" in out
    assert "OWN012 wrong owner Finding" in out


def test_ut00_51_forbidden_imports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-51 duckdb, a core module and a non-sibling submodule each give OWN020."""
    root = _tree(tmp_path, {"swarm": NAMES_06})
    body = "import duckdb\nimport herness.core.config\nfrom herness.core.types.memory import Kind\n"
    _write(root, "herness/core/types/swarm.py", body + _classes(NAMES_06))
    _, out = _run(root, capsys)
    assert out.count("OWN020") == 3


def test_ut00_52_no_owner_submodules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-52 with no owner submodule the check passes with six pending lines."""
    code, out = _run(_tree(tmp_path), capsys)
    assert code == 0
    assert out.count("INFO pending owner") == 6


def test_ut00_53_import_via_submodule(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-53 importing a type from a submodule outside the package gives OWN041."""
    root = _tree(tmp_path)
    _write(root, "herness/harness/x.py", "from herness.core.types.harness import NumberRef\n")
    _, out = _run(root, capsys)
    assert "OWN041" in out


def test_ut00_54_declared_elsewhere(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-54 behavioral names in core.types give OWN043; in a foreign module OWN042."""
    root = _tree(tmp_path, {"harness": NAMES_05})
    _write(root, "herness/core/types/harness.py", _classes([*NAMES_05, "Tracer"]))
    _write(root, "herness/harness/other.py", "class ModelChain:\n    pass\n")
    _, out = _run(root, capsys)
    assert "OWN043" in out
    assert "OWN042" in out


def test_ut00_72_package_form(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-72 package-form submodules are accepted; two forms and extra code are not."""
    half = len(NAMES_05) // 2
    first = _tree(tmp_path / "a", {"harness": NAMES_05})
    init = (
        f"from .llm import {', '.join(NAMES_05[:half])}\n"
        f"from .evidence import {', '.join(NAMES_05[half:])}\n"
        f"__all__ = {tuple(NAMES_05)!r}\n"
    )
    _write(first, "herness/core/types/harness/__init__.py", init)
    _write(first, "herness/core/types/harness/llm.py", _classes(NAMES_05[:half]))
    _write(first, "herness/core/types/harness/evidence.py", _classes(NAMES_05[half:]))
    assert _run(first, capsys)[0] == 0
    second = _tree(tmp_path / "b", {"harness": NAMES_05})
    _write(second, "herness/core/types/harness.py", _classes(NAMES_05))
    _write(second, "herness/core/types/harness/__init__.py", init)
    assert "OWN003" in _run(second, capsys)[1]
    third = _tree(tmp_path / "c", {"harness": NAMES_05})
    _write(third, "herness/core/types/harness/__init__.py", init + "class Extra:\n    pass\n")
    _write(third, "herness/core/types/harness/llm.py", _classes(NAMES_05[:half]))
    _write(third, "herness/core/types/harness/evidence.py", _classes(NAMES_05[half:]))
    assert "OWN033" in _run(third, capsys)[1]


def test_ut00_73_settings_imports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-73 settings modules may import only stdlib, pydantic, core.types, core.errors."""
    root = _tree(tmp_path)
    body = (
        "import typing\nimport pydantic\nimport herness.core.errors\nimport herness.core.types\n"
        "import httpx\nimport herness.core.config\nimport herness.core.types.harness\n"
    )
    _write(root, "herness/connectors/settings.py", body)
    _, out = _run(root, capsys)
    lines = [line for line in out.splitlines() if "OWN050" in line]
    assert len(lines) == 3
    for name in ("httpx", "herness.core.config", "herness.core.types.harness"):
        assert any(line.endswith(name) for line in lines)


def test_ut00_81_declared_module_or_package(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """UT00-81 definitions inside the declared package pass; elsewhere give OWN042."""
    good = _tree(tmp_path / "a")
    _write(good, "herness/core/jobs/ports.py", "class JobContext:\n    pass\n")
    _write(good, "herness/core/resilience/chain.py", "class ModelChain:\n    pass\n")
    assert "OWN042" not in _run(good, capsys)[1]
    bad = _tree(tmp_path / "b")
    _write(bad, "herness/harness/other.py", "class JobContext:\n    pass\n")
    out = _run(bad, capsys)[1]
    assert "OWN042" in out and "JobContext" in out


def test_st00_15_redefinition_outside(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """ST00-15 a shared type redefined elsewhere gives OWN040 at that file."""
    root = _tree(tmp_path, {"harness": NAMES_05})
    _write(root, "herness/core/types/harness.py", _classes(NAMES_05))
    _write(root, "herness/metrics/x.py", "class NumberRef:\n    pass\n")
    code, out = _run(root, capsys)
    assert code == 1
    assert "herness/metrics/x.py:1: OWN040" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/core/test_types_ownership.py tests/unit/tools/test_check_type_ownership.py -v`
Expected: collection ERRORs (`herness.core.types` and `tools` not found).

- [ ] **Step 3: Write the package files**

`herness/core/types/__init__.py`:

```python
"""Shared types of design 00 §6, re-exported from their owner submodules (impl 00 §3.5).

This file defines nothing. Each owner card (T03-01, T05-01, T06-01, T07-01, T08-01,
T09-01) adds one import line for its submodule and its names to __all__.
"""

__all__: tuple[str, ...] = ()
```

`herness/core/types/_ownership.py`:

```python
"""Ownership tables of the shared-types package (impl 00 U00-45, U00-46; R-01, R-02).

Read by tools/check_type_ownership.py with runpy. Imports only types and typing.
"""

import types
from typing import Final

OWNER_MODULES: Final = types.MappingProxyType(
    {"03": "decisions", "05": "harness", "06": "swarm", "07": "memory", "08": "jobs",
     "09": "reports"}
)
OWNER_IMPORTS: Final = types.MappingProxyType(
    {"03": frozenset[str](), "05": frozenset[str](), "06": frozenset({"harness", "jobs"}),
     "07": frozenset({"harness", "swarm"}), "08": frozenset({"harness"}),
     "09": frozenset[str]()}
)
_NAMES: Final = {
    "03": ("QuestionType", "Entity", "Question", "QuestionSet", "DecisionInput", "Answer",
           "DecisionOutput"),
    "05": ("TextPart", "ToolCall", "ToolCallPart", "ToolResultPart", "ReasoningPart", "Message",
           "SystemBlock", "ToolSpec", "RequestMeta", "LLMRequest", "Usage", "LLMResponse",
           "Tool", "AsyncTool", "ToolErrorInfo", "ToolResult", "SqlLimits", "Budgets",
           "BudgetLedger", "TraceEmitter", "WarehouseHandle", "OpsHandle", "VectorHandle",
           "VectorHit", "ToolContext", "NumberRef", "Evidence", "NumberCheck", "UncitedSpan",
           "ItemResult", "VerificationResult", "VerifiableItem", "LoopSignal", "LoopLimits",
           "LoopState", "LoopCheckpoint", "AgentResult"),
    "06": ("RunKind", "Depth", "Role", "Specialty", "ScopeEntityType", "SkepticCheck",
           "SKEPTIC_CHECKS", "RejectReason", "FindingStatus", "Banner", "SectionId",
           "EntityScope", "TaskInputs", "TaskBudget", "TaskSpec", "PlannedTask", "Finding",
           "CheckResult", "Challenge", "CrossCheck", "VerificationRecord", "SwarmTaskState",
           "Paragraph", "Section", "RecommendationItem", "RankedEntity", "Coverage",
           "ReportDraft", "ChatAnswer", "ChatEvent"),
    "07": ("Layer", "Kind", "Status", "KIND_LAYER", "Provenance", "MemoryItem",
           "MemoryProposal", "RecallHit", "MemoryRunContext", "RecommendationDraft",
           "PriorRecommendation", "PriorContext", "ConfidenceAdjustment", "SimilarOutcome"),
    "08": ("GpuClass", "JobKind", "ServiceName", "ChatMode", "BreakerState", "PolicyName",
           "JobSpec", "JobOutcome", "MetricSample"),
    "09": ("ReportManifest",),
}
TYPE_OWNERS: Final = types.MappingProxyType(
    {name: owner for owner, names in _NAMES.items() for name in names}
)
DECLARED_ELSEWHERE: Final = types.MappingProxyType(
    {
        "LoopHooks": ("05", "herness.harness.loop"),
        "HarnessHooks": ("05", "herness.harness.hooks"),
        "GatedClient": ("05", "herness.harness.hooks"),
        "Tracer": ("05", "herness.harness.tracing"),
        "RunBudget": ("06", "herness.harness.budget"),
        "ModelChain": ("08", "herness.core.resilience"),
        "loop_signal_policy": ("08", "herness.core.resilience"),
        "JobContext": ("08", "herness.core.jobs"),
    }
)
```

`tools/__init__.py`:

```python
"""Developer and CI check scripts (impl 00 §3.7). Exit codes: 0 pass, 1 findings, 2 usage."""
```

- [ ] **Step 4: Write `tools/check_type_ownership.py`**

```python
"""Check shared-type ownership, core.types import rules and settings imports (U00-47).

Run: python -m tools.check_type_ownership [--root PATH]
Exit codes (R-73): 0 pass, 1 violations, 2 usage or input error. Files are parsed with
ast and never imported.
"""

from __future__ import annotations

import argparse
import ast
import runpy
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_TYPES = "herness.core.types"
_TYPES_DIR = "herness/core/types"
_TYPE_THIRD_PARTY = frozenset({"pydantic", "pydantic_core", "typing_extensions", "annotated_types"})
_TYPE_CORE = frozenset({"herness.core.errors", "herness.core.ids"})
_SETTINGS_HERNESS = frozenset({"herness.core.types", "herness.core.errors"})


@dataclass(frozen=True, order=True)
class Violation:
    """One finding, rendered as ``<path>:<line>: <CODE> <message>``."""

    path: str
    line: int
    code: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.message}"


@dataclass(frozen=True)
class Tables:
    type_owners: Mapping[str, str]
    owner_modules: Mapping[str, str]
    owner_imports: Mapping[str, frozenset[str]]
    declared_elsewhere: Mapping[str, tuple[str, str]]


@dataclass(frozen=True)
class Source:
    rel: str
    module: str
    is_package: bool
    tree: ast.Module


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)

    def add(self, rel: str, line: int, code: str, message: str) -> None:
        self.violations.append(Violation(rel, line, code, message))


def _parse(root: Path, path: Path, report: Report) -> Source | None:
    rel = path.relative_to(root).as_posix()
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    module = ".".join(parts[:-1] if is_package else parts)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    except (SyntaxError, UnicodeDecodeError, ValueError):
        report.add(rel, 0, "OWN090", "syntax error")
        return None
    return Source(rel, module, is_package, tree)


def _resolve_from(src: Source, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    package = src.module if src.is_package else src.module.rpartition(".")[0]
    for _ in range(node.level - 1):
        package = package.rpartition(".")[0]
    return f"{package}.{node.module}" if node.module else package


def _imports(src: Source, submodules: frozenset[str]) -> Iterator[tuple[int, str]]:
    for node in ast.walk(src.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(src, node)
            subs = [a.name for a in node.names if base == _TYPES and a.name in submodules]
            for name in subs:
                yield node.lineno, f"{base}.{name}"
            if len(subs) < len(node.names):
                yield node.lineno, base


def _is_stdlib(module: str) -> bool:
    return module.split(".", 1)[0] in sys.stdlib_module_names


def _within(module: str, parent: str) -> bool:
    return module == parent or module.startswith(parent + ".")


def _top_level_names(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in tree.body:
        yield from _assigned(node)
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            yield node.lineno, node.name


def _assigned(node: ast.stmt) -> Iterator[tuple[int, str]]:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                yield node.lineno, target.id
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        yield node.lineno, node.target.id
    elif isinstance(node, ast.TypeAlias):
        yield node.lineno, node.name.id


def _is_docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


def _is_all(node: ast.stmt) -> bool:
    return any(name == "__all__" for _, name in _assigned(node))


def _owner_names(tables: Tables, owner: str) -> set[str]:
    return {name for name, who in tables.type_owners.items() if who == owner}


def _load_tables(path: Path) -> Tables:
    ns = runpy.run_path(str(path))
    return Tables(
        ns["TYPE_OWNERS"], ns["OWNER_MODULES"], ns["OWNER_IMPORTS"], ns["DECLARED_ELSEWHERE"]
    )


def _check_tables(tables: Tables, report: Report) -> None:
    rel = f"{_TYPES_DIR}/_ownership.py"
    for name in sorted(set(tables.type_owners) & set(tables.declared_elsewhere)):
        report.add(rel, 0, "OWN001", f"{name} in TYPE_OWNERS and DECLARED_ELSEWHERE")
    for owner in sorted(set(tables.type_owners.values()) - set(tables.owner_modules)):
        report.add(rel, 0, "OWN002", f"owner {owner} missing from OWNER_MODULES")


def _check_owner_imports(src: Source, owner: str, tables: Tables, report: Report) -> None:
    sub = tables.owner_modules[owner]
    allowed = [f"{_TYPES}.{sub}"] + [f"{_TYPES}.{s}" for s in tables.owner_imports[owner]]
    for line, module in _imports(src, frozenset(tables.owner_modules.values())):
        ok = (
            _is_stdlib(module)
            or module.split(".", 1)[0] in _TYPE_THIRD_PARTY
            or module in _TYPE_CORE
            or any(_within(module, parent) for parent in allowed)
        )
        if not ok:
            report.add(src.rel, line, "OWN020", f"forbidden import {module}")


def _check_package_init(src: Source, owner: str, tables: Tables, report: Report) -> None:
    imported: set[str] = set()
    for node in src.tree.body:
        if _is_docstring(node) or _is_all(node):
            continue
        if isinstance(node, ast.ImportFrom) and _resolve_from(src, node).startswith(
            src.module + "."
        ):
            imported.update(alias.asname or alias.name for alias in node.names)
            continue
        report.add(
            src.rel, node.lineno, "OWN033", "package __init__ may only import and set __all__"
        )
    if imported != _owner_names(tables, owner):
        report.add(src.rel, 0, "OWN031", f"re-exported names differ for owner {owner}")


def _check_defined(owner: str, defined: dict[str, tuple[str, int]], anchor: str,
                   tables: Tables, report: Report) -> None:
    for name in sorted(_owner_names(tables, owner) - set(defined)):
        report.add(anchor, 0, "OWN010", f"missing {name}")
    for name, (rel, line) in sorted(defined.items()):
        if name.startswith("_"):
            continue
        if name in tables.declared_elsewhere:
            report.add(rel, line, "OWN043", f"declared elsewhere {name}")
        elif name not in tables.type_owners:
            report.add(rel, line, "OWN011", f"unregistered {name}")
        elif tables.type_owners[name] != owner:
            report.add(rel, line, "OWN012", f"wrong owner {name}")


def _check_owner(root: Path, owner: str, tables: Tables, report: Report) -> bool:
    sub = tables.owner_modules[owner]
    module_file = root / _TYPES_DIR / f"{sub}.py"
    package_dir = root / _TYPES_DIR / sub
    has_module, has_package = module_file.is_file(), (package_dir / "__init__.py").is_file()
    if has_module and has_package:
        report.add(f"{_TYPES_DIR}/{sub}", 0, "OWN003", f"two forms for owner {owner}")
        return True
    if not (has_module or has_package):
        report.infos.append(f"INFO pending owner {owner}")
        return False
    files = [module_file] if has_module else sorted(package_dir.rglob("*.py"))
    anchor = f"{_TYPES_DIR}/{sub}.py" if has_module else f"{_TYPES_DIR}/{sub}/__init__.py"
    defined: dict[str, tuple[str, int]] = {}
    for path in files:
        src = _parse(root, path, report)
        if src is None:
            continue
        _check_owner_imports(src, owner, tables, report)
        if src.is_package and src.module == f"{_TYPES}.{sub}":
            _check_package_init(src, owner, tables, report)
            continue
        for line, name in _top_level_names(src.tree):
            if name != "__all__":
                defined.setdefault(name, (src.rel, line))
    _check_defined(owner, defined, anchor, tables, report)
    return True


def _check_types_init(root: Path, present: dict[str, str], tables: Tables, report: Report) -> None:
    path = root / _TYPES_DIR / "__init__.py"
    rel = f"{_TYPES_DIR}/__init__.py"
    src = _parse(root, path, report) if path.is_file() else None
    if src is None:
        report.add(rel, 0, "OWN030", "missing or unreadable package initialiser")
        return
    imported: dict[str, set[str]] = {}
    all_node: ast.expr | None = None
    for node in src.tree.body:
        if _is_docstring(node):
            continue
        if _is_all(node) and isinstance(node, ast.Assign | ast.AnnAssign):
            all_node = node.value
            continue
        base = _resolve_from(src, node) if isinstance(node, ast.ImportFrom) else ""
        sub = base.removeprefix(_TYPES + ".")
        if isinstance(node, ast.ImportFrom) and sub in present.values():
            imported.setdefault(sub, set()).update(a.asname or a.name for a in node.names)
            continue
        report.add(rel, node.lineno, "OWN030", "only owner imports and __all__ are allowed")
    for owner, sub in present.items():
        if imported.get(sub, set()) != _owner_names(tables, owner):
            report.add(rel, 0, "OWN031", f"re-exported names differ for owner {owner}")
    expected = tuple(sorted(set[str]().union(*imported.values())))
    literal = ast.literal_eval(all_node) if isinstance(all_node, ast.Tuple) else None
    if literal != expected:
        report.add(rel, 0, "OWN032", "__all__ must be the sorted tuple of re-exported names")


def _definitions(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            yield node.lineno, node.name
    scopes: list[ast.Module | ast.ClassDef] = [tree]
    scopes += [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    for scope in scopes:
        for stmt in scope.body:
            yield from _assigned(stmt)


def _check_outside(src: Source, tables: Tables, report: Report) -> None:
    for line, name in _definitions(src.tree):
        if name in tables.type_owners:
            report.add(src.rel, line, "OWN040", f"{name} redefined outside core.types")
        declared = tables.declared_elsewhere.get(name)
        if declared is not None and not _within(src.module, declared[1]):
            report.add(src.rel, line, "OWN042", f"{name} belongs in {declared[1]}")
    submodules = frozenset({*tables.owner_modules.values(), "_ownership"})
    for line, module in _imports(src, submodules):
        if module.startswith(_TYPES + "."):
            report.add(src.rel, line, "OWN041", f"import via submodule {module}")


def _check_settings(src: Source, tables: Tables, report: Report) -> None:
    for line, module in _imports(src, frozenset(tables.owner_modules.values())):
        ok = (
            _is_stdlib(module)
            or module.split(".", 1)[0] in _TYPE_THIRD_PARTY
            or module in _SETTINGS_HERNESS
        )
        if not ok:
            report.add(
                src.rel, line, "OWN050", f"forbidden import in settings module {module}"
            )


def check(root: Path, tables: Tables) -> Report:
    """Run every check under root and return the report."""
    report = Report()
    _check_tables(tables, report)
    present = {
        owner: tables.owner_modules[owner]
        for owner in sorted(tables.owner_modules)
        if _check_owner(root, owner, tables, report)
    }
    _check_types_init(root, present, tables, report)
    for top in ("herness", "app"):
        for path in sorted((root / top).rglob("*.py")) if (root / top).is_dir() else []:
            if path.relative_to(root).as_posix().startswith(_TYPES_DIR + "/"):
                continue
            src = _parse(root, path, report)
            if src is None:
                continue
            _check_outside(src, tables, report)
            if top == "herness" and path.name == "settings.py":
                _check_settings(src, tables, report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; see the module docstring for exit codes."""
    parser = argparse.ArgumentParser(prog="check_type_ownership", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path())
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    root = args.root.resolve()
    ownership = root / _TYPES_DIR / "_ownership.py"
    if not ownership.is_file():
        sys.stderr.write(f"input error: {ownership.as_posix()} not found\n")
        return 2
    report = check(root, _load_tables(ownership))
    for info in report.infos:
        sys.stdout.write(info + "\n")
    for violation in sorted(report.violations):
        sys.stdout.write(violation.render() + "\n")
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Add `tools` to mypy**

In `pyproject.toml`, change `[tool.mypy] files = ["herness"]` to `files = ["herness", "tools"]`.

- [ ] **Step 6: Run the tests and the checker**

Run: `uv run pytest tests/unit/core/test_types_ownership.py tests/unit/tools/test_check_type_ownership.py tests/unit/repo/test_pyproject.py -v`
Expected: all pass.
Run: `uv run python -m tools.check_type_ownership`
Expected: exit 0 with exactly six lines `INFO pending owner 03` … `INFO pending owner 09`.

- [ ] **Step 7: Gates**

Run: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`
Expected: 0 issues. The line budgets hold (`_ownership.py` ≤ 120, the checker ≤ 390, `types/__init__.py` ≤ 150).

- [ ] **Step 8: Commit**

```bash
git add herness/core/types tools pyproject.toml tests/unit/core/test_types_ownership.py tests/unit/tools
git commit -m "feat(core): add shared-types package and ownership checker (T00-08)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Import-linter contracts (T00-09)

Unit U00-52 (read impl 00 §3.6). Tests UT00-58, ST00-10.

**Files:**
- Modify: `pyproject.toml` (add `[tool.importlinter]`)
- Test: `tests/unit/repo/test_import_contracts.py`, `tests/integration/repo/test_import_contracts_enforced.py`

**Interfaces:**
- Consumes: the modules of Tasks 1–8.
- Produces: contracts named exactly `herness layers` (C1), `herness never imports app or tools` (C2), `core base order` (C3), `core base is closed` (C4, present only once it forbids something), `types import only errors and ids` (C5), `settings modules are leaves` (C6, added by the first settings card). Every later card that creates a listed module updates these contracts; UT00-58 fails until it does.

- [ ] **Step 1: Write the failing tests**

`tests/unit/repo/test_import_contracts.py`:

```python
"""UT00-58: import-linter contracts list exactly the modules that exist (U00-52)."""

import tomllib
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
BASE = [
    "herness.core.logging", "herness.core._log_pipeline", "herness.core.types",
    "herness.core.ids", "herness.core.time", "herness.core.numbers", "herness.core.errors",
]


def _exists(module: str) -> bool:
    path = ROOT / module.replace(".", "/")
    return (path / "__init__.py").is_file() or path.with_suffix(".py").is_file()


def _flatten(layers: list[str]) -> list[str]:
    return [name.strip() for layer in layers for name in layer.split("|")]


def _config() -> dict[str, Any]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["importlinter"]


def test_ut00_58_contracts_match_repository() -> None:
    """UT00-58 C1-C6 list exactly the existing modules; one settings exception."""
    config = _config()
    contracts = {c["name"]: c for c in config["contracts"]}
    assert config["root_packages"] == [p for p in ("herness", "app", "tools") if _exists(p)]
    top = {f"herness.{p.name}" for p in (ROOT / "herness").iterdir() if (p / "__init__.py").is_file()}
    c1 = contracts["herness layers"]
    assert set(_flatten(c1["layers"])) == top
    assert c1["ignore_imports"] == ["herness.core.config -> herness.**.settings"]
    c2 = contracts["herness never imports app or tools"]
    assert c2["forbidden_modules"] == [p for p in ("app", "tools") if _exists(p)]
    base = {m for m in BASE if _exists(m)}
    assert set(_flatten(contracts["core base order"]["layers"])) == base
    core = ROOT / "herness" / "core"
    others = {f"herness.core.{p.stem}" for p in core.glob("*.py") if p.stem != "__init__"}
    others |= {f"herness.core.{p.name}" for p in core.iterdir() if (p / "__init__.py").is_file()}
    forbidden = (others - set(BASE)) | (top - {"herness.core"})
    if forbidden:
        assert set(contracts["core base is closed"]["forbidden_modules"]) == forbidden
    else:
        assert "core base is closed" not in contracts
    settings = sorted((ROOT / "herness").rglob("settings.py"))
    assert ("settings modules are leaves" in contracts) == bool(settings)
    if settings and _exists("herness.core.ids"):
        assert "herness.core.ids" in contracts["settings modules are leaves"]["forbidden_modules"]
    for contract in config["contracts"]:
        names = _flatten(contract.get("layers", []))
        names += contract.get("source_modules", []) + contract.get("forbidden_modules", [])
        for name in names:
            assert "*" in name or _exists(name), f"{contract['name']} lists missing {name}"
```

`tests/integration/repo/test_import_contracts_enforced.py`:

```python
"""ST00-10: a planted upward import from the core base breaks C1 and C4."""

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]


def test_st00_10_upward_import_rejected(tmp_path: Path) -> None:
    """ST00-10 lint-imports fails naming 'herness layers' and 'core base is closed'."""
    shutil.copytree(ROOT / "herness", tmp_path / "herness")
    shutil.copytree(ROOT / "tools", tmp_path / "tools")
    (tmp_path / "herness" / "harness").mkdir()
    (tmp_path / "herness" / "harness" / "__init__.py").write_text("", encoding="utf-8")
    errors = tmp_path / "herness" / "core" / "errors.py"
    errors.write_text(errors.read_text(encoding="utf-8") + "\nimport herness.harness\n",
                      encoding="utf-8")
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    text = text.replace('layers = [\n    "herness.core",\n]',
                        'layers = [\n    "herness.harness",\n    "herness.core",\n]', 1)
    config = tomllib.loads(text)["tool"]["importlinter"]
    base = next(c for c in config["contracts"] if c["name"] == "core base order")["layers"]
    sources = [name.strip() for layer in base for name in layer.split("|")]
    if not any(c["name"] == "core base is closed" for c in config["contracts"]):
        text += (
            '\n[[tool.importlinter.contracts]]\nname = "core base is closed"\n'
            'type = "forbidden"\n'
            f"source_modules = {sources!r}\n"
            'forbidden_modules = ["herness.harness"]\n'
        ).replace("'", '"')
    (tmp_path / "pyproject.toml").write_text(text, encoding="utf-8")
    lint = shutil.which("lint-imports")
    assert lint is not None
    result = subprocess.run(  # noqa: S603
        [lint], cwd=tmp_path, capture_output=True, text=True, check=False, timeout=300,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert result.returncode != 0
    assert "herness layers" in result.stdout
    assert "core base is closed" in result.stdout
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/repo/test_import_contracts.py -v`
Expected: FAIL with `KeyError: 'importlinter'`.

- [ ] **Step 3: Add the contracts to `pyproject.toml`**

Append after the coverage tables. Keep C1's `layers` exactly in the multi-line form shown, because ST00-10 edits it by text:

```toml
[tool.importlinter]
root_packages = ["herness", "tools"]
include_external_packages = false

[[tool.importlinter.contracts]]
name = "herness layers"
type = "layers"
layers = [
    "herness.core",
]
ignore_imports = [
    # settings exception (ENG §2.1, R-03)
    "herness.core.config -> herness.**.settings",
]
# The settings exception matches nothing until impl 10 creates herness.core.config.
unmatched_ignore_imports_alerting = "none"

[[tool.importlinter.contracts]]
name = "herness never imports app or tools"
type = "forbidden"
source_modules = ["herness"]
forbidden_modules = ["tools"]

[[tool.importlinter.contracts]]
name = "core base order"
type = "layers"
layers = [
    "herness.core.logging",
    "herness.core._log_pipeline | herness.core.types",
    "herness.core.ids",
    "herness.core.time",
    "herness.core.errors",
]

[[tool.importlinter.contracts]]
name = "types import only errors and ids"
type = "forbidden"
source_modules = ["herness.core.types"]
forbidden_modules = ["herness.core.time", "herness.core.logging", "herness.core._log_pipeline"]
allow_indirect_imports = true
```

C4 (`core base is closed`) is not added yet: no module outside the core base exists, and a forbidden contract needs at least one forbidden module. The first card that creates such a module adds C4 with every module C3 names as `source_modules`, and UT00-58 enforces that. C6 is added by the first settings card.

- [ ] **Step 4: Run lint-imports and the tests**

Run: `uv run lint-imports`
Expected: `Contracts: 4 kept, 0 broken.` If import-linter rejects the one-layer C1, stop and report the exact error. Don't restructure the contracts yourself.
Run: `uv run pytest tests/unit/repo/test_import_contracts.py tests/integration/repo/test_import_contracts_enforced.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/unit/repo/test_import_contracts.py tests/integration/repo/test_import_contracts_enforced.py
git commit -m "build: add import-linter layer contracts (T00-09)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Shared numbers module (T00-16)

Units U00-64 … U00-70 (read impl 00 §3.10). Tests UT00-74 … UT00-79, PT00-06, PT00-07, ST00-18, BT00-05, plus Review Focus 1 and 4.

**Files:**
- Create: `herness/core/numbers.py`
- Modify: `pyproject.toml` (C3 time layer becomes `"herness.core.time | herness.core.numbers"`), `tests/bench/test_core_bench.py` (add BT00-05)
- Test: `tests/unit/core/test_numbers.py`

**Interfaces:**
- Consumes: `herness.core.errors.ConfigError` only (contract C3).
- Produces: constants `MARKER_RE`, `ANY_MARKER_RE`, `MARKER_ID_RE`, `NUMERAL_RE`, `MAX_SCAN_CHARS = 100_000`, `HIT_TEXT_MAX = 80`, `MAX_ALLOWED_PATTERNS = 50`, `MAX_PATTERN_CHARS = 200`, `TOO_LONG_TEXT`, `NOT_AVAILABLE = "n/a"`, `NUMBER_FORMATS`, `DEFAULT_FORMAT_BY_UNIT`; frozen dataclasses `Marker(id, start, end)`, `MalformedMarker(text, start, end)`, `MarkerScan(markers, malformed)` with property `ids`, `NumeralHit(text, start, end)`; protocol `FormattableNumber` (`value`, `unit`, `format`); functions `parse_markers(text) -> MarkerScan`, `compile_allowed_patterns(patterns: Sequence[object]) -> tuple[re.Pattern[str], ...]`, `find_uncited(text, allowed) -> tuple[NumeralHit, ...]`, `format_value(value, unit, fmt) -> str`, `format_number(ref: FormattableNumber) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/core/test_numbers.py`:

```python
"""Tests for herness.core.numbers (U00-64 … U00-70)."""

import datetime
import re
from dataclasses import dataclass

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from herness.core import numbers as nm
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

DEFAULT_PATTERNS = [
    r"\b(19|20)\d{2}\b", r"\d{4}-\d{2}-\d{2}", r"Q[1-4] \d{4}", r"(INC|CHG|PRB)\d+",
    r"[A-Z][A-Z0-9]+-\d+",
]
ALLOWED = nm.compile_allowed_patterns(DEFAULT_PATTERNS)
ALPHABET = set("0123456789,.$%- KMBhmin")


def test_ut00_74_parse_markers() -> None:
    """UT00-74 valid markers keep duplicates; malformed tokens are reported with offsets."""
    scan = nm.parse_markers("a [[n1]] b [[n12]] [[x1]] [[]] [[n 1]] [[n1]]")
    assert scan.markers == (
        nm.Marker("n1", 2, 8), nm.Marker("n12", 11, 18), nm.Marker("n1", 39, 45),
    )
    assert scan.ids == ("n1", "n12", "n1")
    assert scan.malformed == (
        nm.MalformedMarker("x1", 19, 25), nm.MalformedMarker("", 26, 30),
        nm.MalformedMarker("n 1", 31, 38),
    )


def test_ut00_75_compile_allowed_patterns() -> None:
    """UT00-75 five defaults compile in order; invalid lists raise without pattern text."""
    assert [p.pattern for p in ALLOWED] == DEFAULT_PATTERNS
    bad_inputs: list[object] = [[], ["x"] * 51, ["("], ["a" * 201], [5], r"\d{4}"]
    for bad in bad_inputs:
        with pytest.raises(ConfigError) as info:
            nm.compile_allowed_patterns(bad)  # type: ignore[arg-type]
        context = dict(info.value.context)
        assert not set(context) - {"count", "index"}


def test_ut00_76_find_uncited_defaults() -> None:
    """UT00-76 only the uncited numerals are reported; allowed ones and markers are exempt."""
    text = (
        "In Q3 2026 cost rose to [[n1]] from 1,200 on 2026-09-24 for INC0012345 and "
        "PAY-123 (up 12 %) in 2025."
    )
    hits = nm.find_uncited(text, ALLOWED)
    assert [h.text for h in hits] == ["1,200", "12 %"]
    assert hits[0].start == text.index("1,200")
    assert hits[1].start == text.index("12 %")


def test_ut00_77_unicode_and_long_text() -> None:
    """UT00-77 superscripts, fractions, non-ASCII digits and malformed markers are hits."""
    for text, expected in (
        ("up ²", "²"), ("about ½", "½"), ("٣ tickets", "٣"),
        ("[[12]] items", "12"),
    ):
        hits = nm.find_uncited(text, ALLOWED)
        assert [h.text for h in hits] == [expected]
    long_hits = nm.find_uncited("a" * 100_001, ALLOWED)
    assert long_hits == (nm.NumeralHit(nm.TOO_LONG_TEXT, 100_000, 100_001),)


@dataclass(frozen=True)
class _Ref:
    value: object
    unit: str
    format: str | None


def test_ut00_78_format_value() -> None:
    """UT00-78 every NumberRef format renders deterministically."""
    cases = [
        ("-1250000", "usd", "usd", "-$1,250,000.00"),
        (1840000, "usd", "usd_compact", "$1.84M"),
        (12500, "usd", "usd_compact", "$12.5K"),
        (950, "usd", "usd_compact", "$950"),
        (1204, "count", "int", "1,204"),
        (42, "pct", "pct1", "42.0%"),
        (0.3749, "ratio", "ratio2", "0.37"),
        (3.46, "hours", "hours1", "3.5 h"),
        (45.4, "minutes", "minutes0", "45 min"),
        (0.805, "probability", "prob2", "0.80"),
        (7.0, "score", "plain", "7"),
        (0.12344, "score", "plain", "0.1234"),
    ]
    for value, unit, fmt, expected in cases:
        assert nm.format_value(value, unit, fmt) == expected
    assert nm.format_number(_Ref("1250000.00", "usd", None)) == "$1.25M"


def test_ut00_79_format_edge_cases() -> None:
    """UT00-79 default formats, tier promotion, invalid input, negative zero, no exponent."""
    assert nm.format_value(7.5, "score", None) == "7.5"
    assert nm.format_value(999960, "usd", "usd_compact") == "$1.00M"
    assert nm.format_value(999.6, "usd", "usd_compact") == "$1.0K"
    for value, fmt in ((True, "int"), ("abc", "int"), (float("nan"), "int"), (None, "int"),
                       (5, "pct3")):
        assert nm.format_value(value, "count", fmt) == nm.NOT_AVAILABLE
    assert nm.format_value(-0.001, "count", "int") == "0"
    assert nm.format_value(1e22, "score", "plain") == "10,000,000,000,000,000,000,000"


_values = st.one_of(
    st.decimals(min_value=-10**15, max_value=10**15, allow_nan=False, allow_infinity=False),
    st.integers(min_value=-10**15, max_value=10**15),
    st.floats(min_value=-1e15, max_value=1e15, allow_nan=False, allow_infinity=False),
)


@given(_values, st.sampled_from(sorted(nm.NUMBER_FORMATS)),
       st.sampled_from(["usd", "pct", "count", "hours", "minutes", "ratio", "score"]))
def test_pt00_06_format_alphabet(value: object, fmt: str, unit: str) -> None:
    """PT00-06 output is n/a or uses only the documented alphabet, with no exponent."""
    out = nm.format_value(value, unit, fmt)
    assert out == nm.format_value(value, unit, fmt)
    if out != nm.NOT_AVAILABLE:
        assert set(out) <= ALPHABET
        assert "e" not in out.lower()


_word = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=3, max_size=8)
_year = st.integers(min_value=1900, max_value=2099).map(str)
_date = st.dates(min_value=datetime.date(1900, 1, 1)).map(lambda d: d.isoformat())
_marker = st.integers(min_value=1, max_value=999).map(lambda n: f"[[n{n}]]")


@given(st.lists(st.one_of(_word, _year, _date, _marker), min_size=1, max_size=30),
       st.integers(min_value=0, max_value=99_999), st.data())
def test_pt00_07_single_inserted_numeral(words: list[str], number: int, data: st.DataObject) -> None:
    """PT00-07 clean text has no hit; one inserted integer gives exactly one hit."""
    assume(not 1900 <= number <= 2099)
    assert nm.find_uncited(" ".join(words), ALLOWED) == ()
    position = data.draw(st.integers(min_value=0, max_value=len(words)))
    new_words = [*words[:position], str(number), *words[position:]]
    text = " ".join(new_words)
    hits = nm.find_uncited(text, ALLOWED)
    assert [h.text for h in hits] == [str(number)]
    assert hits[0].start == len(" ".join(new_words[:position])) + (1 if position else 0)


def test_st00_18_evasions_are_caught() -> None:
    """ST00-18 each evasion text yields a hit, never inside a valid marker."""
    texts = [
        "cost [[1,250]] USD", "１２ tickets", "³ outages", "Ⅻ teams",
        "[[n1]]12 more", "1​200 users", "INC 42",
        "x" * 100_010 + "7" + "y" * 39,
    ]
    for text in texts:
        hits = nm.find_uncited(text, ALLOWED)
        assert hits, text[:40]
        markers = nm.parse_markers(text[: nm.MAX_SCAN_CHARS]).markers
        for hit in hits:
            assert not any(m.start <= hit.start < m.end for m in markers)
    assert nm.find_uncited(texts[-1], ALLOWED)[-1].text == nm.TOO_LONG_TEXT


def test_rf_display_strings_are_not_numbers() -> None:
    """RF-1 display strings passed as values format as n/a, never as a number."""
    for value in ("1,250", "$5", "12%", ""):
        assert nm.format_value(value, "usd", "usd") == nm.NOT_AVAILABLE


def test_rf_hits_keep_sign_and_symbol() -> None:
    """RF-4 the hit text keeps the currency sign, minus sign and attached percent."""
    hits = nm.find_uncited("pay $1,200 or -5 now, 12% more", ALLOWED)
    assert [h.text for h in hits] == ["$1,200", "-5", "12%"]
```

In `tests/bench/test_core_bench.py`, add `from herness.core import numbers` next to the other imports and append:

```python
def _bench_text() -> str:
    parts: list[str] = []
    for i in range(1000):
        parts.append(f"metric [[n{i}]] rose")
        if i % 2 == 0:
            parts.append(f"in {1990 + i % 30}")
        if i % 20 == 0:
            parts.append(f"by {i + 3} units")
    text = " ".join(parts)
    return (text + " " + "padding " * 20_000)[:100_000]


def test_bt00_05_find_uncited_median(benchmark: Any) -> None:
    """BT00-05 find_uncited on a 100,000-character text: median under 100 ms."""
    allowed = numbers.compile_allowed_patterns(
        [r"\b(19|20)\d{2}\b", r"\d{4}-\d{2}-\d{2}", r"Q[1-4] \d{4}", r"(INC|CHG|PRB)\d+",
         r"[A-Z][A-Z0-9]+-\d+"]
    )
    text = _bench_text()
    benchmark.pedantic(numbers.find_uncited, args=(text, allowed), rounds=100, iterations=1)
    assert benchmark.stats.stats.median < 0.1
```

The benchmark text holds 1,000 valid markers, 500 allowed years and 50 uncited numerals, matching §10.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/core/test_numbers.py -v`
Expected: collection ERROR, `herness.core.numbers` not found.

- [ ] **Step 3: Implement `herness/core/numbers.py`**

```python
"""Number markers, the uncited-numeral scanner and NumberRef formatting (design 00 §12.1).

The single implementation (R-16) used by the Verifier (impl 05), the renderer (impl 09)
and impl 06, 07 and 11. Pure: no I/O, clock, logging or configuration access.
"""

from __future__ import annotations

import bisect
import decimal
import re
import types
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from herness.core.errors import ConfigError

MARKER_RE: Final = re.compile(r"\[\[(n[0-9]{1,39})\]\]")
ANY_MARKER_RE: Final = re.compile(r"\[\[([^\[\]]{0,40})\]\]")
MARKER_ID_RE: Final = re.compile(r"^n[0-9]+$")
NUMERAL_RE: Final = re.compile(r"(?<![\w.])[-+]?\$?\d[\d,]*(\.\d+)?\s*(%|k|K|M|bn|x)?(?!\w)")
MAX_SCAN_CHARS: Final = 100_000
HIT_TEXT_MAX: Final = 80
MAX_ALLOWED_PATTERNS: Final = 50
MAX_PATTERN_CHARS: Final = 200
TOO_LONG_TEXT: Final = "<text too long>"
NOT_AVAILABLE: Final = "n/a"
NUMBER_FORMATS: Final[frozenset[str]] = frozenset(
    {"usd", "usd_compact", "int", "pct1", "ratio2", "hours1", "minutes0", "prob2", "plain"}
)
DEFAULT_FORMAT_BY_UNIT: Final[Mapping[str, str]] = types.MappingProxyType(
    {"usd": "usd_compact", "pct": "pct1", "count": "int", "hours": "hours1",
     "minutes": "minutes0", "ratio": "ratio2"}
)
_NON_ASCII: Final = re.compile(r"[^\x00-\x7f]")
_CTX: Final = decimal.Context(prec=60, rounding=decimal.ROUND_HALF_EVEN)
_GROUP: Final = 3
_THOUSAND: Final = decimal.Decimal(1000)
_TIERS: Final = (
    (decimal.Decimal("1e9"), 2, "B"), (decimal.Decimal("1e6"), 2, "M"),
    (decimal.Decimal("1e3"), 1, "K"), (decimal.Decimal(1), 0, ""),
)
_SIMPLE: Final[Mapping[str, tuple[int, str, str]]] = types.MappingProxyType(
    {"usd": (2, "$", ""), "int": (0, "", ""), "pct1": (1, "", "%"), "ratio2": (2, "", ""),
     "prob2": (2, "", ""), "hours1": (1, "", " h"), "minutes0": (0, "", " min")}
)


@dataclass(frozen=True, slots=True)
class Marker:
    """A valid number marker ``[[nK]]`` and its offsets."""

    id: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class MalformedMarker:
    """A double-bracket token that is not a valid marker (inner text, max 40 chars)."""

    text: str
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class MarkerScan:
    """Result of parse_markers, each tuple sorted by start."""

    markers: tuple[Marker, ...]
    malformed: tuple[MalformedMarker, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        """Marker ids in text order, duplicates kept."""
        return tuple(marker.id for marker in self.markers)


@dataclass(frozen=True, slots=True)
class NumeralHit:
    """An uncited numeral (text at most HIT_TEXT_MAX characters) and its offsets."""

    text: str
    start: int
    end: int


class FormattableNumber(Protocol):
    """Read-only view of a NumberRef (impl 05) that format_number needs."""

    @property
    def value(self) -> float | int | str: ...

    @property
    def unit(self) -> str: ...

    @property
    def format(self) -> str | None: ...


class _SpanIndex:
    """Answers "is [s, e) inside some span" in O(log n)."""

    def __init__(self, spans: Sequence[tuple[int, int]]) -> None:
        ordered = sorted(spans)
        self._starts = [start for start, _ in ordered]
        self._max_end: list[int] = []
        best = -1
        for _, end in ordered:
            best = max(best, end)
            self._max_end.append(best)

    def covers(self, start: int, end: int) -> bool:
        index = bisect.bisect_right(self._starts, start) - 1
        return index >= 0 and self._max_end[index] >= end


def parse_markers(text: str) -> MarkerScan:
    """Find every valid marker and every malformed double-bracket token. Raises nothing."""
    markers: list[Marker] = []
    malformed: list[MalformedMarker] = []
    for match in ANY_MARKER_RE.finditer(text[:MAX_SCAN_CHARS]):
        inner = match.group(1)
        if MARKER_ID_RE.fullmatch(inner):
            markers.append(Marker(inner, match.start(), match.end()))
        else:
            malformed.append(MalformedMarker(inner, match.start(), match.end()))
    return MarkerScan(tuple(markers), tuple(malformed))


def compile_allowed_patterns(patterns: Sequence[object]) -> tuple[re.Pattern[str], ...]:
    """Validate and compile reports.allowed_numeral_patterns once.

    Raises ConfigError (count or index); pattern text is never copied into the error.
    """
    if isinstance(patterns, str):
        msg = "allowed numeral patterns must be a list"
        raise ConfigError(msg)  # noqa: TRY004 - taxonomy error
    if not 0 < len(patterns) <= MAX_ALLOWED_PATTERNS:
        msg = "allowed numeral pattern count out of range"
        raise ConfigError(msg, count=len(patterns))
    compiled: list[re.Pattern[str]] = []
    for index, pattern in enumerate(patterns):
        if not isinstance(pattern, str) or not 0 < len(pattern) <= MAX_PATTERN_CHARS:
            msg = "invalid allowed numeral pattern"
            raise ConfigError(msg, index=index)
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            msg = "allowed numeral pattern does not compile"
            raise ConfigError(msg, index=index) from exc
    return tuple(compiled)


def _blank_markers(scan: str) -> str:
    chars = list(scan)
    for marker in parse_markers(scan).markers:
        chars[marker.start : marker.end] = " " * (marker.end - marker.start)
    return "".join(chars)


def find_uncited(text: str, allowed: Sequence[re.Pattern[str]]) -> tuple[NumeralHit, ...]:
    """Return every numeral outside a valid marker that no allowed pattern covers.

    Raises nothing. Text past MAX_SCAN_CHARS always yields a TOO_LONG_TEXT hit.
    """
    scan = text[:MAX_SCAN_CHARS]
    blanked = _blank_markers(scan)
    allowed_index = _SpanIndex(
        [m.span() for p in allowed for m in p.finditer(blanked) if m.end() > m.start()]
    )
    hits: list[NumeralHit] = []
    numeral_spans: list[tuple[int, int]] = []
    for match in NUMERAL_RE.finditer(blanked):
        start, end = match.span()
        numeral_spans.append((start, end))
        while end > start and blanked[end - 1].isspace():
            end -= 1
        if not allowed_index.covers(start, end):
            hits.append(NumeralHit(scan[start:end][:HIT_TEXT_MAX], start, end))
    numeral_index = _SpanIndex(numeral_spans)
    for match in _NON_ASCII.finditer(blanked):
        char, index = match.group(), match.start()
        if char.isspace() or unicodedata.numeric(char, None) is None:
            continue
        if not (numeral_index.covers(index, index + 1) or allowed_index.covers(index, index + 1)):
            hits.append(NumeralHit(char, index, index + 1))
    if len(text) > MAX_SCAN_CHARS:
        hits.append(NumeralHit(TOO_LONG_TEXT, MAX_SCAN_CHARS, len(text)))
    return tuple(sorted(hits, key=lambda hit: (hit.start, hit.end)))


def _to_decimal(value: object) -> decimal.Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str | decimal.Decimal):
        return None
    try:
        number = _CTX.create_decimal(str(value))
    except decimal.InvalidOperation:
        return None
    return number if number.is_finite() else None


def _quantize(number: decimal.Decimal, places: int) -> decimal.Decimal:
    return number.quantize(decimal.Decimal(1).scaleb(-places), context=_CTX)


def _group(whole: str) -> str:
    parts: list[str] = []
    while len(whole) > _GROUP:
        parts.insert(0, whole[-_GROUP:])
        whole = whole[:-_GROUP]
    parts.insert(0, whole)
    return ",".join(parts)


def _render(q: decimal.Decimal, prefix: str, suffix: str) -> str:
    whole, _, frac = format(abs(q), "f").partition(".")
    sign = "-" if q < 0 else ""
    return sign + prefix + _group(whole) + ("." + frac if frac else "") + suffix


def _usd_compact(number: decimal.Decimal) -> str:
    tier = next(
        i for i, (limit, _, _) in enumerate(_TIERS) if abs(number) >= limit or i == len(_TIERS) - 1
    )
    while True:
        limit, places, suffix = _TIERS[tier]
        q = _quantize(_CTX.divide(number, limit), places)
        if abs(q) >= _THOUSAND and tier > 0:
            tier -= 1
            continue
        return _render(q, "$", suffix)


def _plain(number: decimal.Decimal) -> str:
    if number == number.to_integral_value():
        return _render(_quantize(number, 0), "", "")
    q = _quantize(number, 4)
    whole, _, frac = format(abs(q), "f").partition(".")
    frac = frac.rstrip("0")
    sign = "-" if q < 0 else ""
    return sign + _group(whole) + ("." + frac if frac else "")


def format_value(value: object, unit: str, fmt: str | None) -> str:
    """Display a number for every NumberRef.format value; invalid input gives "n/a".

    Raises nothing. Output is fixed-point, locale-independent and deterministic.
    """
    chosen = fmt if fmt is not None else DEFAULT_FORMAT_BY_UNIT.get(unit, "plain")
    if chosen not in NUMBER_FORMATS:
        return NOT_AVAILABLE
    number = _to_decimal(value)
    if number is None:
        return NOT_AVAILABLE
    try:
        if chosen == "usd_compact":
            return _usd_compact(number)
        if chosen == "plain":
            return _plain(number)
        places, prefix, suffix = _SIMPLE[chosen]
        return _render(_quantize(number, places), prefix, suffix)
    except decimal.InvalidOperation:
        return NOT_AVAILABLE


def format_number(ref: FormattableNumber) -> str:
    """Display a NumberRef value: format_value(ref.value, ref.unit, ref.format)."""
    return format_value(ref.value, ref.unit, ref.format)
```

- [ ] **Step 4: Update contract C3**

In `pyproject.toml`, in the `core base order` contract, replace the line `"herness.core.time",` with `"herness.core.time | herness.core.numbers",`.

- [ ] **Step 5: Run the tests, lint-imports and the benchmark**

Run: `uv run pytest tests/unit/core/test_numbers.py tests/unit/repo/test_import_contracts.py -v`
Expected: all pass.
Run: `uv run lint-imports`
Expected: `Contracts: 4 kept, 0 broken.`
Run: `uv run pytest tests/bench/test_core_bench.py -m "integration and slow" -k BT00_05 -v`
Expected: PASS on the reference PC.

- [ ] **Step 6: Gates and coverage**

Run: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`
Expected: 0 issues. If ruff flags `A003` on the `id` or `format` fields, add `# noqa: A003` with the reason "name fixed by U00-65". The spec fixes these names.
Run: `uv run pytest tests/unit/core/test_numbers.py --cov=herness.core.numbers --cov-branch --cov-report=term-missing`
Expected: ≥ 90 % line and ≥ 85 % branch; the file has ≤ 320 lines.

- [ ] **Step 7: Commit**

```bash
git add herness/core/numbers.py pyproject.toml tests/unit/core/test_numbers.py tests/bench/test_core_bench.py
git commit -m "feat(core): add shared number markers, scanner and formatter (T00-16)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Final verification

- [ ] **Step 1: Full local gate run**

Run each and confirm the expected result:
- `uv sync --frozen` → no changes.
- `uv run ruff check .` → `All checks passed!`
- `uv run ruff format --check .` → no files to reformat.
- `uv run mypy` → `Success: no issues found`.
- `uv run lint-imports` → `4 kept, 0 broken`.
- `uv run python -m tools.check_type_ownership` → exit 0, six `INFO pending owner` lines.
- `uv run pytest -m "unit or integration" --cov=herness --cov-branch --cov-report=term-missing` → all pass; every `herness/core` file ≥ 90 % line and ≥ 85 % branch.
- `uv run pytest -m "integration and slow" tests/bench` → all pass on the reference PC; record the timings.

- [ ] **Step 2: Check the line budgets**

Run: `wc -l herness/core/*.py herness/core/types/*.py tools/check_type_ownership.py`
Expected: each file within its Global Constraints budget.

- [ ] **Step 3: Report**

Summarise in the final message: tasks completed, any deviations applied beyond the eight listed, benchmark timings, and anything that needed a decision (for example a dependency bound `uv lock` could not satisfy).
