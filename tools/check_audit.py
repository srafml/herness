"""Dependency-audit gate over pip-audit and osv-scanner JSON (U00-57, ENG §7, TH00-11).

Run: python -m tools.check_audit --pip-audit PATH --osv PATH [--ignore PATH]
     [--summary PATH] [--today YYYY-MM-DD]
Fails on a High or Critical vulnerability (CVSS >= 7.0) with a fix available; an unknown
severity counts as 7.0 (fail closed). pip-audit supplies fix versions, osv-scanner severity.
Exit codes (R-73): 0 pass, 1 blocking findings, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import re
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from herness.core import time as clock

_NAME_RUN_RE = re.compile(r"[-_.]+")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_IGNORE_KEYS = frozenset({"id", "package", "reason", "expires"})
_HIGH = 7.0
_BLOCKING = frozenset({"AU001 blocking", "AU003 expired ignore"})


class _InputError(Exception):
    """An input file is unreadable or has an unexpected shape."""


@dataclass
class Finding:
    """One vulnerability of one package, merged across both scanners."""

    package: str
    version: str
    ids: set[str]
    fix_available: bool
    severity: float | None = None
    decision: str = ""


@dataclass
class IgnoreEntry:
    """One time-boxed exception from the ignore file."""

    id: str
    package: str
    reason: str
    expires: datetime.date
    used: bool = field(default=False, compare=False)


def normalise(name: str) -> str:
    """PEP 503 name normalisation."""
    return _NAME_RUN_RE.sub("-", name).lower()


def _as[T](kind: type[T], value: object, what: str) -> T:
    if not isinstance(value, kind):
        msg = f"{what}: expected {kind.__name__}"
        raise _InputError(msg)
    return value


def _strs(value: object, what: str) -> list[str]:
    return [_as(str, item, what) for item in _as(list, value, what)]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"cannot read {path.name}: {exc}"
        raise _InputError(msg) from exc
    return _as(dict, doc, path.name)


def _parse_pip_audit(doc: dict[str, Any], warnings: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    for raw in _as(list, doc.get("dependencies"), "pip-audit dependencies"):
        dep = _as(dict, raw, "pip-audit dependency")
        name = _as(str, dep.get("name"), "pip-audit name")
        if "skip_reason" in dep:
            warnings.append(f"AU010 skipped {name}")
            continue
        version = _as(str, dep.get("version", ""), "pip-audit version")
        for raw_vuln in _as(list, dep.get("vulns", []), "pip-audit vulns"):
            vuln = _as(dict, raw_vuln, "pip-audit vuln")
            ids = {_as(str, vuln.get("id"), "pip-audit id")}
            ids.update(_strs(vuln.get("aliases", []), "pip-audit aliases"))
            fix = bool(_strs(vuln.get("fix_versions", []), "pip-audit fix_versions"))
            findings.append(Finding(normalise(name), version, ids, fix))
    return findings


def _osv_fixed(vuln: dict[str, Any]) -> bool:
    for affected in _as(list, vuln.get("affected", []), "osv affected"):
        for rng in _as(list, _as(dict, affected, "osv affected").get("ranges", []), "osv ranges"):
            events = _as(list, _as(dict, rng, "osv range").get("events", []), "osv events")
            if any("fixed" in _as(dict, event, "osv event") for event in events):
                return True
    return False


def _severity(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


Group = tuple[str, set[str], float | None]


def _parse_osv_package(raw: object, findings: list[Finding], groups: list[Group]) -> None:
    entry = _as(dict, raw, "osv package entry")
    info = _as(dict, entry.get("package"), "osv package")
    name = normalise(_as(str, info.get("name"), "osv package name"))
    version = _as(str, info.get("version", ""), "osv package version")
    for raw_vuln in _as(list, entry.get("vulnerabilities", []), "osv vulnerabilities"):
        vuln = _as(dict, raw_vuln, "osv vulnerability")
        ids = {_as(str, vuln.get("id"), "osv id")}
        ids.update(_strs(vuln.get("aliases", []), "osv aliases"))
        findings.append(Finding(name, version, ids, _osv_fixed(vuln)))
    for raw_group in _as(list, entry.get("groups", []), "osv groups"):
        group = _as(dict, raw_group, "osv group")
        gids = set(_strs(group.get("ids", []), "osv group ids"))
        groups.append((name, gids, _severity(group.get("max_severity"))))


def _parse_osv(doc: dict[str, Any]) -> tuple[list[Finding], list[Group]]:
    findings: list[Finding] = []
    groups: list[Group] = []
    for raw_result in _as(list, doc.get("results", []), "osv results"):
        result = _as(dict, raw_result, "osv result")
        for raw in _as(list, result.get("packages", []), "osv packages"):
            _parse_osv_package(raw, findings, groups)
    return findings, groups


def merge(records: list[Finding], groups: list[Group]) -> list[Finding]:
    """Merge records of one package whose ID sets intersect; attach the osv severity."""
    merged: list[Finding] = []
    for rec in records:
        target = Finding(rec.package, rec.version, set(rec.ids), rec.fix_available)
        for other in [f for f in merged if f.package == rec.package and f.ids & rec.ids]:
            target.ids |= other.ids
            target.fix_available |= other.fix_available
            target.version = other.version or target.version
            merged.remove(other)
        merged.append(target)
    for finding in merged:
        scores = [
            score
            for package, gids, score in groups
            if score is not None and package == finding.package and gids & finding.ids
        ]
        finding.severity = max(scores, default=None)
    return sorted(merged, key=lambda f: (f.package, sorted(f.ids)))


def _parse_date(value: object, what: str) -> datetime.date:
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str) and _DATE_RE.fullmatch(value):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            pass
    msg = f"{what}: expected a YYYY-MM-DD date"
    raise _InputError(msg)


def _ignore_entry(raw: object) -> IgnoreEntry:
    entry = _as(dict, raw, "ignore entry")
    if set(entry) != _IGNORE_KEYS:
        msg = "ignore entry: needs exactly id, package, reason, expires"
        raise _InputError(msg)
    texts = {key: _as(str, entry[key], f"ignore {key}") for key in ("id", "package", "reason")}
    if not all(text.strip() for text in texts.values()):
        msg = "ignore entry: id, package and reason must be non-empty"
        raise _InputError(msg)
    expires = _parse_date(entry["expires"], "ignore expires")
    return IgnoreEntry(texts["id"], normalise(texts["package"]), texts["reason"], expires)


def load_ignore(path: Path) -> list[IgnoreEntry]:
    """Read ``ignore = [{id, package, reason, expires}]``; any other shape is an input error."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        msg = f"cannot read {path.name}: {exc}"
        raise _InputError(msg) from exc
    if set(data) != {"ignore"}:
        msg = f"{path.name}: expected only the key 'ignore'"
        raise _InputError(msg)
    return [_ignore_entry(raw) for raw in _as(list, data["ignore"], "ignore")]


def decide(finding: Finding, entries: list[IgnoreEntry], today: datetime.date) -> str:
    """Return the decision for one finding and mark the ignore entries it uses."""
    matches = [e for e in entries if e.id in finding.ids and e.package == finding.package]
    for entry in matches:
        entry.used = True
    if any(entry.expires >= today for entry in matches):
        return "ignored"
    if matches:
        return "AU003 expired ignore"
    severity = _HIGH if finding.severity is None else finding.severity
    if severity >= _HIGH and finding.fix_available:
        return "AU001 blocking"
    return "AU002 warning"


def _cells(f: Finding) -> list[str]:
    severity = "unknown" if f.severity is None else str(f.severity)
    fix = "yes" if f.fix_available else "no"
    return [f.package, f.version, ", ".join(sorted(f.ids)), severity, fix, f.decision]


def _render(f: Finding) -> str:
    package, version, ids, severity, fix, decision = _cells(f)
    return f"{decision} {package} {version} {ids.replace(', ', ',')} severity={severity} fix={fix}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def write_summary(path: Path, findings: list[Finding]) -> None:
    """Write the Markdown table ``package | version | ids | severity | fix | decision``."""
    lines = [
        "| package | version | ids | severity | fix | decision |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines += ["| " + " | ".join(_cell(c) for c in _cells(f)) + " |" for f in findings]
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        msg = f"cannot write {path.name}: {exc}"
        raise _InputError(msg) from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="check_audit", description=__doc__)
    parser.add_argument("--pip-audit", dest="pip_audit", type=Path, required=True)
    parser.add_argument("--osv", type=Path, required=True)
    parser.add_argument("--ignore", type=Path, default=Path("tools/audit_ignore.toml"))
    parser.add_argument("--summary", type=Path, default=None)
    parser.add_argument("--today", default=None)
    return parser


def run(args: argparse.Namespace) -> int:
    """Run the gate for parsed arguments; raises _InputError on bad input."""
    today_text = args.today if args.today is not None else clock.utc_day(clock.now())
    today = _parse_date(today_text, "--today")
    warnings: list[str] = []
    records = _parse_pip_audit(_load_json(args.pip_audit), warnings)
    osv_records, groups = _parse_osv(_load_json(args.osv))
    entries = load_ignore(args.ignore)
    findings = merge(records + osv_records, groups)
    for finding in findings:
        finding.decision = decide(finding, entries, today)
    warnings += [f"AU011 unused ignore {e.id} {e.package}" for e in entries if not e.used]
    if args.summary is not None:
        write_summary(args.summary, findings)
    out = [_render(f) for f in findings] + warnings
    sys.stdout.write("".join(line + "\n" for line in out))
    return 1 if any(f.decision in _BLOCKING for f in findings) else 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; see the module docstring for exit codes."""
    try:
        args = _parser().parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        return run(args)
    except _InputError as exc:
        sys.stderr.write(f"input error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
