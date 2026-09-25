"""Check module line budgets: ENG §2.4 default, config overrides and doc module maps (U00-55).

Run: python -m tools.check_module_size [--root PATH]
Exit codes (R-73): 0 pass, 1 violations, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

_CODE_ROOTS = ("herness", "app", "tools")
_TOKEN_RE = re.compile(r"`([^`]+)`")
_INT_RE = re.compile(r"\d+")
_SEP_CELL_RE = re.compile(r":?-+:?")

Budgets = dict[str, tuple[int, str]]


class _ConfigError(Exception):
    """The `[tool.herness.module_budgets]` table is missing or malformed."""


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

    def add(self, path: str, line: int, code: str, message: str) -> None:
        self.violations.append(Violation(path, line, code, message))


def _load_config(path: Path) -> tuple[int, dict[str, tuple[int, str]]]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise _ConfigError(str(exc)) from exc
    table = data.get("tool", {}).get("herness", {}).get("module_budgets", {})
    default = table.get("default")
    if not isinstance(default, int) or isinstance(default, bool):
        msg = "[tool.herness.module_budgets].default missing or not an int"
        raise _ConfigError(msg)
    overrides: Budgets = {}
    for rel, entry in table.get("overrides", {}).items():
        limit = entry.get("limit")
        if not isinstance(limit, int) or isinstance(limit, bool):
            msg = f"overrides[{rel!r}].limit missing or not an int"
            raise _ConfigError(msg)
        overrides[rel] = (limit, str(entry.get("reason", "")))
    return default, overrides


def _split_row(line: str) -> list[str] | None:
    stripped = line.strip()
    if not stripped.startswith("|"):
        return None
    body = stripped.removeprefix("|").removesuffix("|")
    return [cell.strip() for cell in body.split("|")]


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(_SEP_CELL_RE.fullmatch(cell) for cell in cells if cell)


def _module_row(cells: list[str]) -> tuple[str, int] | None:
    tokens = _TOKEN_RE.findall(cells[0])
    if len(tokens) != 1:
        return None
    token = tokens[0]
    if not token.endswith(".py") or any(ch in token for ch in "{* "):
        return None
    budget_cell = cells[-1]
    if not _INT_RE.fullmatch(budget_cell):
        return None
    return token, int(budget_cell)


def _parse_doc(doc: Path) -> list[tuple[str, int]]:
    lines = doc.read_text(encoding="utf-8").splitlines()
    rows: list[tuple[str, int]] = []
    i = 0
    while i < len(lines):
        cells = _split_row(lines[i])
        header_ok = cells is not None and cells[0] == "Path" and cells[-1] == "Line budget"
        sep = _split_row(lines[i + 1]) if header_ok and i + 1 < len(lines) else None
        if header_ok and sep is not None and _is_separator(sep):
            i += 2
            while i < len(lines):
                body = _split_row(lines[i])
                if body is None:
                    break
                row = _module_row(body)
                if row is not None:
                    rows.append(row)
                i += 1
            continue
        i += 1
    return rows


def _doc_budgets(root: Path, report: Report) -> Budgets:
    resolved: Budgets = {}
    doc_dir = root / "docs" / "impl"
    docs = sorted(doc_dir.glob("*.impl.md")) if doc_dir.is_dir() else []
    for doc in docs:
        for path, budget in _parse_doc(doc):
            prior = resolved.get(path)
            if prior is None:
                resolved[path] = (budget, doc.name)
            elif prior[0] != budget:
                msg = f"conflicting budgets {prior[0]} ({prior[1]}) vs {budget} ({doc.name})"
                report.add(path, 0, "MS002", msg)
    return resolved


def _files(root: Path) -> list[Path]:
    found: list[Path] = []
    for name in _CODE_ROOTS:
        base = root / name
        if base.is_dir():
            found += base.rglob("*.py")
    return sorted(found)


def _effective_budget(
    rel: str, default: int, overrides: Budgets, doc_budgets: Budgets
) -> tuple[int, str]:
    override = overrides.get(rel)
    base = (override[0], "override") if override is not None else (default, "default")
    doc = doc_budgets.get(rel)
    return doc if doc is not None and doc[0] <= base[0] else base


def _check_file(
    root: Path, path: Path, default: int, overrides: Budgets, doc_budgets: Budgets, report: Report
) -> None:
    rel = path.relative_to(root).as_posix()
    budget, source = _effective_budget(rel, default, overrides, doc_budgets)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        report.add(rel, 0, "MS004", "cannot decode as UTF-8")
        return
    lines = len(text.splitlines())
    if lines > budget:
        report.add(rel, 0, "MS001", f"{lines} lines > budget {budget} ({source})")


def check(root: Path, default: int, overrides: Budgets) -> Report:
    """Run every module-size check under root and return the report."""
    report = Report()
    for rel, (limit, reason) in overrides.items():
        if limit > default and not reason.strip():
            msg = f"override limit {limit} > default {default} with empty reason"
            report.add(rel, 0, "MS003", msg)
    doc_budgets = _doc_budgets(root, report)
    for path in _files(root):
        _check_file(root, path, default, overrides, doc_budgets, report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; see the module docstring for exit codes."""
    parser = argparse.ArgumentParser(prog="check_module_size", description=__doc__)
    parser.add_argument("--root", type=Path, default=Path())
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    root = args.root.resolve()
    try:
        default, overrides = _load_config(root / "pyproject.toml")
    except _ConfigError as exc:
        sys.stderr.write(f"input error: {exc}\n")
        return 2
    report = check(root, default, overrides)
    for violation in sorted(report.violations):
        sys.stdout.write(violation.render() + "\n")
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
