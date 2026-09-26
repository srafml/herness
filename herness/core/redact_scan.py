"""Fixture PII scanner behind ``python -m herness.core.redact --scan PATH`` (U10-49).

Run by the spec 11 pre-commit hook: every detected span outside the reserved synthetic
ranges of spec 11 §5.1.4 (R-56, R-67) and every denylisted domain is a finding. A finding
prints ``<path>:<line>:<col> <TYPE>`` and never the value. In a Parquet file ``line`` is
the 1-based row and ``col`` the 1-based column index in the schema; a file-level finding
("file too large to scan", "unreadable") is reported at ``0:0``. Exit codes: 0 no finding,
1 findings, 2 usage or input error (R-73).
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

from herness.core.config_sources import deep_merge, load_yaml_file
from herness.core.errors import ConfigError
from herness.core.redact import Redactor
from herness.core.redact_patterns import EntityType, normalize_value
from herness.core.settings import RedactionConfig

__all__ = ["main"]

MAX_FILE_BYTES: Final = 50 * 1024 * 1024
SUFFIXES: Final = frozenset(
    {".csv", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".md", ".sql", ".parquet"}
)
_CONFIG_FILES: Final = (Path("config/herness.yaml"), Path("config/profiles/synth.yaml"))
_FICTIONAL_PHONE: Final = re.compile(r"\+120255501\d{2}")
_RESERVED_NETS: Final = tuple(
    ipaddress.ip_network(net) for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
_HOST: Final = "[A-Za-z0-9-]"
# Not PII although a detector matches (T10-11 deviation, pending ruling): a decimal fraction
# read as PHONE (``0.333333333``) and a CARD with leading 0 (no PAN starts with MII 0).
_DECIMAL: Final = re.compile(r"\d{1,2}\.\d{6}")

_Finding = tuple[int, int, str]  # line, column (both 1-based), type or reason


def _reserved_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return any(address in net for net in _RESERVED_NETS)


def _allowed(kind: EntityType, value: str) -> bool:
    """True for a value in the reserved synthetic ranges (spec 11 §5.1.4, R-56, R-67)."""
    lowered = value.lower()
    if kind == "EMAIL":
        return lowered.endswith(("@example.com", "@example.org"))
    if kind == "PHONE":
        fictional = _FICTIONAL_PHONE.fullmatch(normalize_value("PHONE", value)) is not None
        return fictional or _DECIMAL.match(value) is not None
    if kind == "IP":
        return _reserved_ip(value)
    if kind in {"CREDENTIAL", "URL_TOKEN"}:
        return lowered.startswith("synthetic")
    if kind == "NATIONAL_ID":
        return value.startswith(("9", "000"))
    return kind == "EMPLOYEE_ID" or (kind == "CARD" and value.startswith(("4111", "0")))


def _denylist_pattern(domains: Sequence[str]) -> re.Pattern[str] | None:
    """A denylisted domain as a label-bounded host suffix, case-insensitive (step 6)."""
    if not domains:
        return None
    names = "|".join(re.escape(domain) for domain in domains)
    return re.compile(rf"(?<!{_HOST})(?:{names})(?!{_HOST}|\.{_HOST})", re.IGNORECASE)


def _denylist_domains(cwd: Path) -> list[str]:
    """``security.redaction.denylist_domains`` of ``herness.yaml`` merged with ``synth.yaml``."""
    merged: dict[str, object] = {}
    for relative in _CONFIG_FILES:
        path = cwd / relative
        if path.is_file():  # a missing file contributes nothing
            merged = deep_merge(merged, load_yaml_file(path))
    section: object = merged
    for key in ("security", "redaction", "denylist_domains"):
        section = section.get(key) if isinstance(section, dict) else None
    if section is None:
        return []
    if not isinstance(section, list) or not all(isinstance(item, str) for item in section):
        msg = "security.redaction.denylist_domains must be a list of strings"
        raise ConfigError(msg)
    return [item.strip(".") for item in section if item.strip(".")]


class _Scanner:
    def __init__(self, denylist: Sequence[str]) -> None:
        # All-zero key: detection does not depend on it; no name directory in the repository.
        self._redactor = Redactor(RedactionConfig(directory_file=None), bytes(32))
        self._deny = _denylist_pattern(denylist)

    def text(self, text: str, line: int, col: int = 0) -> Iterator[_Finding]:
        """Findings of one line; a Parquet cell passes its column index as ``col``."""
        for span in self._redactor.scan(text):
            if not _allowed(span.type, text[span.start : span.end]):
                yield line, col or span.start + 1, span.type
        for match in self._deny.finditer(text) if self._deny is not None else ():
            yield line, col or match.start() + 1, "DENYLISTED_DOMAIN"

    def file(self, path: Path) -> Iterator[_Finding]:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                yield 0, 0, "file too large to scan"
                return
            if path.suffix.lower() == ".parquet":
                yield from self._parquet(path)
                return
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            yield 0, 0, "unreadable"
            return
        for number, line in enumerate(content.splitlines(), start=1):
            yield from self.text(line, number)

    def _parquet(self, path: Path) -> Iterator[_Finding]:
        import pyarrow as pa  # noqa: PLC0415 - only a Parquet file needs pyarrow
        import pyarrow.parquet as pq  # noqa: PLC0415 - only a Parquet file needs pyarrow

        try:
            table = pq.read_table(path)
        except (OSError, pa.ArrowException):
            yield 0, 0, "unreadable"
            return
        for index, field in enumerate(table.schema, start=1):
            if pa.types.is_string(field.type) or pa.types.is_large_string(field.type):
                for row, value in enumerate(table.column(index - 1).to_pylist(), start=1):
                    if value is not None:
                        yield from self.text(value, row, index)


def _files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUFFIXES:
            yield path


def main(argv: Sequence[str] | None = None) -> int:
    """Scan every ``--scan PATH``; print ``<path>:<line>:<col> <TYPE>`` per finding (U10-49)."""
    parser = argparse.ArgumentParser(prog="python -m herness.core.redact")
    parser.add_argument("--scan", action="append", required=True, type=Path, metavar="PATH")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits 2 on a usage error, 0 on --help
        return 2 if exc.code else 0
    missing = [path for path in args.scan if not path.exists()]
    if missing:
        sys.stderr.write(f"redact --scan: path not found: {missing[0].as_posix()}\n")
        return 2
    try:
        scanner = _Scanner(_denylist_domains(Path.cwd()))
    except ConfigError as exc:
        sys.stderr.write(f"redact --scan: {exc.message}\n")
        return 2
    found = 0
    for root in args.scan:
        for path in _files(root):
            for line, col, kind in scanner.file(path):
                sys.stdout.write(f"{path.as_posix()}:{line}:{col} {kind}\n")
                found += 1
    return 1 if found else 0
