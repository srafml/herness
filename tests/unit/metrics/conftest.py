"""Metrics test helpers: make the hyphenated test IDs selectable with ``pytest -k``."""

import re
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_ID_RE = re.compile(r"[A-Z]{2}\d{2}-\d{2,3}")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Add the docstring's leading test ID (for example ``UT04-01``) as a ``-k`` keyword."""
    for item in items:
        if _HERE not in Path(str(item.path)).resolve().parents:
            continue
        doc = getattr(getattr(item, "function", None), "__doc__", None) or ""
        match = _ID_RE.match(doc.strip())
        if match is not None:
            item.extra_keyword_matches.add(match.group(0))
