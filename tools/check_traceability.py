"""Check that task, unit, flow, threat and test IDs resolve across the specs (U00-56, ENG §7).

Run: python -m tools.check_traceability [--root PATH] [--require-implemented NN[,NN...]]
Exit codes (R-73): 0 pass, 1 violations, 2 usage or input error. Test files are parsed
with ast and never imported.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

_ID_RE = re.compile(r"(?<![A-Za-z0-9_])(TH|UT|PT|IT|FT|ST|BT|ET|T|U|F)(\d{2})-(\d{2,3})(?![0-9])")
_NAME_ID_RE = re.compile(r"(?<![a-z0-9])(ut|pt|it|ft|st|bt|et)(\d{2})_(\d{2,3})(?![0-9])")
_TEST_KINDS = frozenset({"UT", "PT", "IT", "FT", "ST", "BT", "ET"})
_HEADING_RE = re.compile(r"(#{1,6}) (.*)")
_CROSS_RE = re.compile(r"X:\d{2}/\S+")
_SEP_CELL_RE = re.compile(r":?-+:?")
_SPEC_RE = re.compile(r"\d{2}")
_RANGE_SEP = r"\s*(?:\N{EN DASH}|\N{HORIZONTAL ELLIPSIS}|\.\.\.)\s*"
_RANGE_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Z]{2})(\d{2})-(\d{2,3})"
    + _RANGE_SEP
    + r"([A-Z]{2})(\d{2})-(\d{2,3})(?![0-9])"
)

Loc = tuple[str, int]


@dataclass(frozen=True, order=True)
class Violation:
    """One finding, rendered as ``<path>:<line>: <CODE> <message>``."""

    path: str
    line: int
    code: str
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.code} {self.message}"


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)

    def add(self, loc: Loc, code: str, message: str) -> None:
        self.violations.append(Violation(loc[0], loc[1], code, message))


@dataclass
class DocIndex:
    """What the doc scan found: definitions, references and card test rows."""

    definitions: dict[str, list[Loc]] = field(default_factory=dict)
    references: dict[str, list[Loc]] = field(default_factory=dict)
    on_cards: set[str] = field(default_factory=set)
    threat_rows: list[tuple[str, Loc, bool]] = field(default_factory=list)
    cross_refs: list[tuple[str, Loc]] = field(default_factory=list)


@dataclass(frozen=True)
class TestFunc:
    loc: Loc
    name: str
    ids: frozenset[str]


def _kind(id_text: str) -> str:
    return id_text[: len(id_text) - len(id_text.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))]


def _is_test_id(id_text: str) -> bool:
    return _kind(id_text) in _TEST_KINDS


def _spec_of(id_text: str) -> str:
    kind = _kind(id_text)
    return id_text[len(kind) : len(kind) + 2]


def _ids_in(text: str) -> list[str]:
    return [m.group(0) for m in _ID_RE.finditer(text)]


def _as_id(token: str) -> str | None:
    match = _ID_RE.fullmatch(token.strip(" `"))
    return match.group(0) if match is not None else None


def _split_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    body = stripped.removeprefix("|").removesuffix("|")
    return [cell.strip() for cell in body.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(_SEP_CELL_RE.fullmatch(cell) for cell in cells if cell)


def _card_test_ids(line: str) -> set[str]:
    """IDs of a card's Tests row; a range ``A<sep>B`` of one kind and spec is expanded.

    ``<sep>`` is an en dash, an ellipsis or three dots, as the specs write ranges.
    Deliberate deviation from U00-56 (T00-11 ruling, recorded in the group ledger): the
    algorithm counts only literal IDs; expansion applies to TR005 and nothing else.
    """
    found = set(_ids_in(line))
    for m in _RANGE_RE.finditer(line):
        kind, spec, first, end = m.group(1), m.group(2), m.group(3), int(m.group(6))
        if (m.group(4), m.group(5)) == (kind, spec):
            width = len(first)
            found.update(f"{kind}{spec}-{n:0{width}d}" for n in range(int(first), end + 1))
    return found


def _defined_id(line: str) -> tuple[str | None, int]:
    """Return the ID this line defines (if any) and its heading level (0 for non-headings)."""
    heading = _HEADING_RE.match(line)
    if heading is not None:
        words = heading.group(2).split()
        return (_as_id(words[0]) if words else None), len(heading.group(1))
    cells = _split_row(line)
    if cells is None or _is_separator(cells):
        return None, 0
    return _as_id(cells[0]), 0


class _DocScanner:
    """Scans one doc line by line, tracking fences and the current task-card section."""

    def __init__(self, rel: str, index: DocIndex) -> None:
        self.rel = rel
        self.index = index
        self.fence = ""
        self.card_level = 0

    def scan(self, lines: list[str]) -> None:
        for number, line in enumerate(lines, start=1):
            marker = line.lstrip()[:3]
            if marker in ("```", "~~~"):
                # A fence closes only on the marker that opened it.
                if not self.fence:
                    self.fence = marker
                elif marker == self.fence:
                    self.fence = ""
                continue
            if not self.fence:
                self._line((self.rel, number), line)

    def _line(self, loc: Loc, line: str) -> None:
        defined, level = _defined_id(line)
        if level:
            self._track_card(defined, level)
        found = _ids_in(line)
        if defined is not None and defined in found:
            self.index.definitions.setdefault(defined, []).append(loc)
            found.remove(defined)
            if _kind(defined) == "TH" and not level:
                has_st = any(_kind(other) == "ST" for other in found)
                self.index.threat_rows.append((defined, loc, has_st))
        for ref in found:
            self.index.references.setdefault(ref, []).append(loc)
        cells = _split_row(line)
        if self.card_level and cells is not None and cells[0] == "Tests":
            self.index.on_cards.update(i for i in _card_test_ids(line) if _is_test_id(i))
        self.index.cross_refs.extend((m.group(0), loc) for m in _CROSS_RE.finditer(line))

    def _track_card(self, defined: str | None, level: int) -> None:
        if self.card_level and level <= self.card_level:
            self.card_level = 0
        if defined is not None and _kind(defined) == "T":
            self.card_level = level


def scan_docs(root: Path, doc_dir: Path) -> tuple[DocIndex, dict[str, str]]:
    """Scan every ``*.impl.md`` doc; return the index and each doc's spec number."""
    index = DocIndex()
    specs: dict[str, str] = {}
    for doc in sorted(doc_dir.glob("*.impl.md")):
        rel = doc.relative_to(root).as_posix()
        specs[rel] = doc.name[:2]
        _DocScanner(rel, index).scan(doc.read_text(encoding="utf-8").splitlines())
    return index, specs


def _check_definitions(index: DocIndex, specs: dict[str, str], report: Report) -> None:
    """TR002, TR003 and TR005 for every defined ID."""
    for id_text, locs in index.definitions.items():
        if len(locs) > 1:
            where = ", ".join(f"{path}:{line}" for path, line in locs)
            report.add(locs[1], "TR002", f"duplicate {id_text} defined at {where}")
        for loc in locs:
            if _spec_of(id_text) != specs[loc[0]]:
                report.add(loc, "TR003", f"wrong spec {id_text} in spec {specs[loc[0]]}")
        if _is_test_id(id_text) and id_text not in index.on_cards:
            report.add(locs[0], "TR005", f"test not on a card {id_text}")


def check_docs(index: DocIndex, specs: dict[str, str], report: Report) -> None:
    """Apply TR001-TR005 and TR008 to the doc index."""
    for ref, locs in index.references.items():
        if ref not in index.definitions:
            for loc in locs:
                report.add(loc, "TR001", f"undefined {ref}")
    _check_definitions(index, specs, report)
    for id_text, loc, has_st in index.threat_rows:
        if not has_st:
            report.add(loc, "TR004", f"threat without security test {id_text}")
    for text, loc in index.cross_refs:
        report.add(loc, "TR008", f"unresolved cross-spec reference {text}")


def _function_ids(node: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[str]:
    ids = {
        f"{m.group(1).upper()}{m.group(2)}-{m.group(3)}" for m in _NAME_ID_RE.finditer(node.name)
    }
    doc = ast.get_docstring(node)
    if doc:
        ids.update(i for i in _ids_in(doc) if _is_test_id(i))
    return frozenset(ids)


def scan_tests(root: Path, report: Report) -> list[TestFunc]:
    """Parse every test file under ``<root>/tests`` and collect the IDs of its test functions."""
    funcs: list[TestFunc] = []
    tests_dir = root / "tests"
    files = sorted(tests_dir.rglob("*.py")) if tests_dir.is_dir() else []
    for path in files:
        rel = path.relative_to(root).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            line = getattr(exc, "lineno", None) or 0
            report.add((rel, line), "TR090", "syntax error")
            continue
        funcs.extend(
            TestFunc((rel, node.lineno), node.name, _function_ids(node))
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test_")
        )
    return sorted(funcs, key=lambda f: f.loc)


def check_code(funcs: list[TestFunc], index: DocIndex, report: Report) -> set[str]:
    """Apply TR006, TR007 and TR010; return the set of IDs carried by test functions."""
    owners: dict[str, TestFunc] = {}
    for func in funcs:
        if len(func.ids) > 1:
            report.add(func.loc, "TR010", f"several ids on one test {', '.join(sorted(func.ids))}")
        for id_text in sorted(func.ids):
            if id_text not in index.definitions:
                report.add(func.loc, "TR006", f"unknown test id {id_text} on {func.name}")
            first = owners.setdefault(id_text, func)
            if first is not func:
                where = f"{first.loc[0]}:{first.loc[1]}"
                report.add(func.loc, "TR007", f"test id used twice {id_text} (also {where})")
    return set(owners)


def check_implemented(
    specs_required: Sequence[str], index: DocIndex, carried: set[str], report: Report
) -> None:
    """TR009 for each defined test ID of the listed specs that no test function carries."""
    wanted = set(specs_required)
    for id_text, locs in sorted(index.definitions.items()):
        if _is_test_id(id_text) and _spec_of(id_text) in wanted and id_text not in carried:
            report.add(locs[0], "TR009", f"not implemented {id_text}")


def _parse_specs(value: str) -> list[str]:
    parts = [part.strip() for part in value.split(",")]
    if not parts or not all(_SPEC_RE.fullmatch(part) for part in parts):
        msg = f"--require-implemented expects NN[,NN...], got {value!r}"
        raise argparse.ArgumentTypeError(msg)
    return parts


def run(root: Path, required: Sequence[str]) -> tuple[Report, str]:
    """Run every traceability check under root; return the report and the summary line."""
    report = Report()
    index, specs = scan_docs(root, root / "docs" / "impl")
    check_docs(index, specs, report)
    carried = check_code(scan_tests(root, report), index, report)
    if required:
        check_implemented(required, index, carried, report)
    implemented = len(carried & index.definitions.keys())
    summary = (
        f"defined={len(index.definitions)} referenced={len(index.references)} "
        f"implemented={implemented}"
    )
    return report, summary


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; see the module docstring for exit codes."""
    parser = argparse.ArgumentParser(prog="check_traceability", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path())
    parser.add_argument("--require-implemented", type=_parse_specs, default=[])
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    root = args.root.resolve()
    if not (root / "docs" / "impl").is_dir():
        sys.stderr.write(f"input error: {root / 'docs' / 'impl'} is not a directory\n")
        return 2
    try:
        report, summary = run(root, args.require_implemented)
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"input error: {exc}\n")
        return 2
    for violation in sorted(report.violations):
        sys.stdout.write(violation.render() + "\n")
    sys.stdout.write(summary + "\n")
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
