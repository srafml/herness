"""Check shared-type ownership, core.types import rules and settings imports (U00-47).

Run: python -m tools.check_type_ownership [--root PATH]
Exit codes (R-73): 0 pass, 1 violations, 2 usage or input error. Source files are parsed
with ast and never imported; only the ownership tables file is executed (runpy).
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


class InputError(Exception):
    """An input file cannot be read or loaded; main() reports it and exits 2 (R-73)."""


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
    except OSError as exc:
        msg = f"cannot read {rel} ({type(exc).__name__})"
        raise InputError(msg) from exc
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
    try:
        ns = runpy.run_path(str(path))
        return Tables(
            ns["TYPE_OWNERS"], ns["OWNER_MODULES"], ns["OWNER_IMPORTS"], ns["DECLARED_ELSEWHERE"]
        )
    except Exception as exc:  # any failure of the executed tables file is an input error
        msg = f"cannot load {path.as_posix()} ({type(exc).__name__})"
        raise InputError(msg) from exc


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


def _check_defined(
    owner: str, defined: dict[str, tuple[str, int]], anchor: str, tables: Tables, report: Report
) -> None:
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
            report.add(src.rel, line, "OWN050", f"forbidden import in settings module {module}")


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
    try:
        report = check(root, _load_tables(ownership))
    except InputError as exc:
        sys.stderr.write(f"input error: {exc}\n")
        return 2
    for info in report.infos:
        sys.stdout.write(info + "\n")
    for violation in sorted(report.violations):
        sys.stdout.write(violation.render() + "\n")
    return 1 if report.violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
