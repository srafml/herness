"""Build error subclasses (impl 02 U02-76, U02-77). Messages never carry data values."""

import re
from typing import ClassVar, Final

from herness.core.errors import SchemaViolation

# A quoted literal, or an unclosed one running to the end of the line.
_LITERAL_RE: Final = re.compile(r"'(?:[^']|'')*(?:'|$)")
_DB_MESSAGE_MAX: Final = 300
_MAX_CHECKS_SHOWN: Final = 20


def _sanitise_db_error(db_error: str) -> str:
    first = db_error.splitlines()[0] if db_error else ""
    name, sep, message = _LITERAL_RE.sub("'?'", first).partition(": ")
    return f"{name}{sep}{message[:_DB_MESSAGE_MAX]}" if sep else name[:_DB_MESSAGE_MAX]


class BuildSqlError(SchemaViolation):
    """A build SQL file failed; quoted literals are masked as ``'?'`` (TH02-14)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("build_id", "file", "statement_index", "db_error")

    def __init__(self, build_id: str, file: str, statement_index: int, db_error: str) -> None:
        clean = _sanitise_db_error(db_error)
        super().__init__(f"build {build_id}: {file} statement {statement_index} failed: {clean}")
        self.build_id, self.file = build_id, file
        self.statement_index, self.db_error = statement_index, clean


class DqGateFailed(SchemaViolation):
    """Promotion blocked by ``error``-severity DQ failures (CLI exit 5, R-46)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("build_id", "failed_checks")

    def __init__(self, build_id: str, failed_checks: tuple[str, ...]) -> None:
        shown = ", ".join(failed_checks[:_MAX_CHECKS_SHOWN])
        super().__init__(f"build {build_id} blocked by DQ: {shown}")
        self.build_id, self.failed_checks = build_id, tuple(failed_checks)
