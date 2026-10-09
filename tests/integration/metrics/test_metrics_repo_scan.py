"""Repository scans of impl 04 ownership rules (T04-22): IT04-10 (R-15) and IT04-11 (R-14,
R-64, layering).

IT04-10: the evidence helpers `result_hash`, `rows_equivalent` and `iter_batch_rows` are
defined only in `herness/metrics/evidence.py` across the production trees (`herness/`,
`tools/`, `app/` when present). IT04-11: `herness/metrics/` defines none of the
`herness.core.ids` functions, references no synthetic truth file and imports neither
`herness.enrich` nor `herness.harness` (also not lazily inside a function, not through a
relative import resolved against the file's package, and not through an
`importlib.import_module` / `__import__` call with a string-literal module name).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.integration

REPO: Final = Path(__file__).resolve().parents[3]
METRICS: Final = REPO / "herness" / "metrics"
EVIDENCE_HELPERS: Final = frozenset({"result_hash", "rows_equivalent", "iter_batch_rows"})
CORE_IDS: Final = frozenset({"canonical_json", "normalize_sql", "query_id"})
FORBIDDEN_IMPORTS: Final = ("herness.enrich", "herness.harness")
# What tools/synth/truth_writer.py writes beside data/ (design 11 §3.1) and its readers.
TRUTH_MARKERS: Final = (
    "truth.json",
    "truth_labels",
    "t2_members",
    "t3_pairs",
    "name_directory",
    "truth_writer",
    "herness.eval.truth",
    "tests.support.metrics_truth",
)


def _python_files(*roots: Path) -> Iterator[Path]:
    for root in roots:
        if root.is_dir():
            yield from sorted(root.rglob("*.py"))


def _definitions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    kinds = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    names = {node.name for node in ast.walk(tree) if isinstance(node, kinds)}
    for node in tree.body:  # module-level bindings only: local variables are not definitions
        if isinstance(node, ast.Assign):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _package(path: Path) -> list[str]:
    """Package parts of `path` (a module's package, or the package an `__init__` defines)."""
    return list(path.relative_to(REPO).with_suffix("").parts[:-1])


def _resolve(node: ast.ImportFrom, package: list[str]) -> str:
    if node.level == 0:
        return node.module or ""
    base = package[: len(package) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def _dynamic(node: ast.Call) -> str | None:
    """The string-literal module of an `importlib.import_module(...)` / `__import__(...)`."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in {"import_module", "__import__"} or not node.args:
        return None
    arg = node.args[0]
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package(path)
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            module = _resolve(node, package)
            out.add(module)
            out |= {f"{module}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Call) and (literal := _dynamic(node)) is not None:
            out.add(literal)
    return out


def _rel(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def test_it04_10_evidence_helpers_defined_only_in_evidence_module() -> None:
    """IT04-10 `result_hash`, `rows_equivalent` and `iter_batch_rows` are defined in
    `herness/metrics/evidence.py` and nowhere else in `herness/`, `tools/` or `app/`."""
    where: dict[str, list[str]] = {name: [] for name in EVIDENCE_HELPERS}
    for path in _python_files(REPO / "herness", REPO / "tools", REPO / "app"):
        for name in _definitions(path) & EVIDENCE_HELPERS:
            where[name].append(_rel(path))
    assert where == {name: ["herness/metrics/evidence.py"] for name in EVIDENCE_HELPERS}


def test_it04_11_metrics_package_owns_no_core_ids_and_reads_no_truth() -> None:
    """IT04-11 `herness/metrics/` defines no `canonical_json`, `normalize_sql` or `query_id`
    (R-14), names no truth file (R-64) and imports no `herness.enrich` / `herness.harness`."""
    files = list(_python_files(METRICS))
    assert METRICS / "evidence.py" in files
    redefined = {_rel(p): sorted(_definitions(p) & CORE_IDS) for p in files}
    assert {k: v for k, v in redefined.items() if v} == {}
    layered = {
        _rel(p): sorted(m for m in _imports(p) if m.startswith(FORBIDDEN_IMPORTS)) for p in files
    }
    assert {k: v for k, v in layered.items() if v} == {}
    truth_refs = []
    for path in sorted(
        p for p in METRICS.rglob("*") if p.is_file() and "__pycache__" not in p.parts
    ):
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        truth_refs += [(_rel(path), m) for m in TRUTH_MARKERS if m in text]
    assert truth_refs == []
    assert any("herness.core.ids" in _imports(p) for p in files)  # imported, not redefined
