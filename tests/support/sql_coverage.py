"""SQL table coverage check (design §4.3, U11-33).

Every table or view created by `herness/model/sql/*.sql` must be named by at least one
test, so a query nobody exercises does not silently rot.
"""

from __future__ import annotations

import re
from pathlib import Path

_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_COMMENT_LINE_RE = re.compile(r"--[^\n]*")
_CREATE_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?(?P<temp>TEMP(?:ORARY)?\s+)?(?:TABLE|VIEW)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?(?P<schema>[a-z_]+)\.(?P<table>[a-z_0-9]+)",
    re.IGNORECASE,
)
_REF_RE = re.compile(r"\b(stg|core|enrich|metrics|score|meta)\.([a-z_0-9]+)\b")


def _strip_comments(text: str) -> str:
    return _COMMENT_LINE_RE.sub(" ", _COMMENT_BLOCK_RE.sub(" ", text))


def tables_created(sql_dir: Path) -> set[str]:
    """`schema.table` names created (not `TEMP`/`TEMPORARY`) by `*.sql` files in `sql_dir`."""
    created: set[str] = set()
    if not sql_dir.is_dir():
        return created
    for path in sorted(sql_dir.glob("*.sql")):
        text = _strip_comments(path.read_text(encoding="utf-8"))
        for match in _CREATE_RE.finditer(text):
            if match.group("temp") is not None:
                continue
            created.add(f"{match.group('schema').lower()}.{match.group('table').lower()}")
    return created


def tables_referenced_by_tests(tests_dir: Path) -> set[str]:
    """`schema.table` tokens named anywhere in `tests/**/test_*.py` under `tests_dir`."""
    referenced: set[str] = set()
    if not tests_dir.is_dir():
        return referenced
    for path in sorted(tests_dir.rglob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        for schema, table in _REF_RE.findall(text):
            referenced.add(f"{schema.lower()}.{table.lower()}")
    return referenced
