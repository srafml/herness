"""Pytest plugin: category marker enforcement and test-ID collection (U11-30, U11-31).

Every test file carries exactly one category marker (design §4.1). Test IDs (ENG §6)
are extracted from test names and docstrings, indexed for the impl 00 traceability
script and used to select tests for phase gates.
"""

import json
import os
import re
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

import pytest

MARKERS: tuple[tuple[str, str], ...] = (
    ("unit", "no I/O beyond tmp files, no subprocess, no network"),
    ("integration", "real DuckDB, SQLite and LanceDB on tiny or small builds, fakes and stubs"),
    ("fault", "HERNESS_ENV=test, fault plans, subprocess kills"),
    ("eval", "golden or classifier evaluation"),
    ("gpu", "needs vLLM, OpenJev or Laya on CUDA"),
    ("slow", "over 30 s per test, or small or full scale"),
)
CATEGORY_MARKERS: frozenset[str] = frozenset({"unit", "integration", "fault", "eval"})

TEST_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(UT|PT|IT|FT|ST|BT|ET)(\d{2})[-_](\d{2,3})(?![0-9])",
    re.IGNORECASE,
)
INDEX_SCHEMA = 1


def extract_test_ids(name: str, docstring: str | None) -> tuple[str, ...]:
    """Return the normalized test IDs (`UT11-01`) in a test name and docstring.

    Upper case, hyphen separator, sorted and unique.
    """
    found: set[str] = set()
    for text in (name, docstring or ""):
        for kind, spec, number in TEST_ID_PATTERN.findall(text):
            found.add(f"{kind.upper()}{spec}-{number}")
    return tuple(sorted(found))


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("herness", "Herness test IDs (impl 11 U11-31)")
    group.addoption(
        "--collect-test-ids",
        dest="collect_test_ids",
        metavar="PATH",
        default=None,
        help="write the test-ID index as JSON to PATH after collection",
    )
    group.addoption(
        "--require-test-ids",
        dest="require_test_ids",
        action="store_true",
        default=False,
        help="fail collection when any test carries no test ID",
    )
    group.addoption(
        "--select-test-ids",
        dest="select_test_ids",
        metavar="ID[,ID...]",
        default=None,
        help="run only tests carrying one of the given test IDs",
    )


def pytest_configure(config: pytest.Config) -> None:
    for name, description in MARKERS:
        config.addinivalue_line("markers", f"{name}: {description}")


# --- marker enforcement (U11-30) -------------------------------------------------


def _mark_names(marks: object) -> list[str]:
    """Names of the marks in a `pytestmark` value (a mark or a list of marks)."""
    items: Iterable[object] = marks if isinstance(marks, list | tuple) else [marks]
    names: list[str] = []
    for mark in items:
        name = getattr(mark, "name", None)
        if isinstance(name, str):
            names.append(name)
    return names


def _module_category_count(module: ModuleType) -> int:
    names = _mark_names(getattr(module, "pytestmark", []))
    return sum(1 for name in names if name in CATEGORY_MARKERS)


def _item_categories(item: pytest.Item) -> set[str]:
    return {mark.name for mark in item.iter_markers() if mark.name in CATEGORY_MARKERS}


def marker_violations(items: Iterable[pytest.Item]) -> list[str]:
    """Files whose `pytestmark` has not exactly one category, then mixed-category items."""
    bad_files: dict[str, None] = {}
    bad_items: list[str] = []
    checked: set[str] = set()
    for item in items:
        module = getattr(item, "module", None)
        if not isinstance(module, ModuleType):
            continue
        path = str(item.path)
        if path not in checked:
            checked.add(path)
            if _module_category_count(module) != 1:
                bad_files[path] = None
        if path not in bad_files and len(_item_categories(item)) > 1:
            bad_items.append(item.nodeid)
    return [*bad_files, *bad_items]


# --- test IDs (U11-31) ----------------------------------------------------------


def item_test_ids(item: pytest.Item) -> tuple[str, ...]:
    """IDs of one collected item, from its original name and function docstring."""
    name = getattr(item, "originalname", None) or item.name
    function = getattr(item, "function", None)
    docstring = getattr(function, "__doc__", None)
    return extract_test_ids(name, docstring if isinstance(docstring, str) else None)


REVIEW_TEST_NAME = re.compile(r"^test_(rf|cv)_[A-Za-z0-9_]+$")


def _is_review_test(item: pytest.Item) -> bool:
    """`test_rf_<slug>` (review focus) and `test_cv_<slug>` (controller verification)."""
    name = getattr(item, "originalname", None) or item.name
    return REVIEW_TEST_NAME.match(name) is not None


def build_index(items: Iterable[pytest.Item]) -> tuple[dict[str, list[str]], list[str]]:
    """Return `({id: sorted nodeids}, sorted untagged nodeids)`.

    Several test functions may share one test ID. Review-focus and controller-verification
    tests (`test_rf_*`, `test_cv_*`) count as tagged without a spec test ID.
    """
    nodes: dict[str, set[str]] = defaultdict(set)
    untagged: list[str] = []
    for item in items:
        ids = item_test_ids(item)
        if not ids and not _is_review_test(item):
            untagged.append(item.nodeid)
        for test_id in ids:
            nodes[test_id].add(item.nodeid)
    index = {test_id: sorted(nodeids) for test_id, nodeids in sorted(nodes.items())}
    return index, sorted(untagged)


def write_index(path: Path, index: dict[str, list[str]], untagged: list[str]) -> None:
    """Write the ID index JSON atomically (temporary file, then `os.replace`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": INDEX_SCHEMA, "ids": index, "untagged": untagged}
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def parse_id_list(raw: str) -> frozenset[str]:
    """Normalize `--select-test-ids`; an entry that is not a test ID is a usage error."""
    selected: set[str] = set()
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        match = TEST_ID_PATTERN.fullmatch(entry)
        if match is None:
            msg = f"--select-test-ids: not a test ID: {entry!r}"
            raise pytest.UsageError(msg)
        kind, spec, number = match.groups()
        selected.add(f"{kind.upper()}{spec}-{number}")
    if not selected:
        msg = "--select-test-ids: no test IDs given"
        raise pytest.UsageError(msg)
    return frozenset(selected)


def _select(config: pytest.Config, items: list[pytest.Item], wanted: frozenset[str]) -> None:
    kept: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        (kept if wanted.intersection(item_test_ids(item)) else dropped).append(item)
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Enforce category markers, check and index test IDs, then select by ID."""
    violations = marker_violations(items)
    if violations:
        msg = "each test file needs exactly one category marker (unit, integration, fault, eval):\n"
        raise pytest.UsageError(msg + "\n".join(f"  {entry}" for entry in violations))
    index, untagged = build_index(items)
    if config.getoption("require_test_ids") and untagged:
        msg = "tests without a test ID:\n" + "\n".join(f"  {nodeid}" for nodeid in untagged)
        raise pytest.UsageError(msg)
    target = config.getoption("collect_test_ids")
    if target:
        write_index(Path(target), index, untagged)
    raw_selection = config.getoption("select_test_ids")
    if raw_selection:
        _select(config, items, parse_id_list(raw_selection))
