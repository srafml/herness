"""Licence gate over the CycloneDX SBOM of the runtime environment (U00-58, ENG §5.6).

Run: python -m tools.check_licences --sbom PATH [--pyproject PATH] [--mode enforce|report]
Policy: ``[tool.herness.licences]`` in the pyproject (allowed, aliases, approved, report_only).
Each component is classified allowed, approved, report-only (LC002) or denied (LC001).
Exit codes (R-73): 0 pass, 1 denied components in enforce mode, 2 usage or input error.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import json
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_NAME_RUN_RE = re.compile(r"[-_.]+")
_APPROVAL_KEYS = frozenset({"package", "licence", "approved_by", "approved_on", "reason"})
UNKNOWN = "UNKNOWN"


class _InputError(Exception):
    """The SBOM or the licence table is unreadable or has an unexpected shape."""


@dataclass(frozen=True)
class Approval:
    """A reviewed exception: ``package`` glob on the normalised name, exact ``licence`` text."""

    package: str
    licence: str


@dataclass(frozen=True)
class Policy:
    """The ``[tool.herness.licences]`` table."""

    allowed: frozenset[str]
    aliases: Mapping[str, str]
    approved: tuple[Approval, ...]
    report_only: tuple[str, ...]


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


def _approval(raw: object) -> Approval:
    entry = _as(dict, raw, "approved entry")
    if set(entry) != _APPROVAL_KEYS:
        msg = "approved entry: needs exactly package, licence, approved_by, approved_on, reason"
        raise _InputError(msg)
    on = entry["approved_on"]
    if not isinstance(on, datetime.date | str):
        msg = "approved entry: approved_on must be a date"
        raise _InputError(msg)
    for key in ("package", "licence", "approved_by", "reason"):
        _as(str, entry[key], f"approved {key}")
    return Approval(entry["package"].lower(), entry["licence"])


def load_policy(path: Path) -> Policy:
    """Read ``[tool.herness.licences]``; a missing or malformed table is an input error."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        msg = f"cannot read {path.name}: {exc}"
        raise _InputError(msg) from exc
    table = _as(
        dict, data.get("tool", {}).get("herness", {}).get("licences"), "[tool.herness.licences]"
    )
    aliases = _as(dict, table.get("aliases", {}), "licences.aliases")
    return Policy(
        allowed=frozenset(_strs(table.get("allowed"), "licences.allowed")),
        aliases={key.casefold(): _as(str, value, "alias") for key, value in aliases.items()},
        approved=tuple(_approval(raw) for raw in _as(list, table.get("approved", []), "approved")),
        report_only=tuple(g.lower() for g in _strs(table.get("report_only", []), "report_only")),
    )


def _wrapped(text: str) -> bool:
    """True when the parenthesis opening ``text`` closes at its last character."""
    depth = 0
    for index, char in enumerate(text):
        depth += (char == "(") - (char == ")")
        if depth == 0:
            return index == len(text) - 1
    return False


def _strip_outer(text: str) -> str:
    text = text.strip()
    while text.startswith("(") and _wrapped(text):
        text = text[1:-1].strip()
    return text


def _split_top(text: str, operator: str) -> list[str]:
    """Split on ``operator`` outside parentheses."""
    parts: list[str] = []
    depth = start = index = 0
    while index < len(text):
        char = text[index]
        if depth == 0 and text.startswith(operator, index):
            parts.append(text[start:index])
            index += len(operator)
            start = index
            continue
        depth += (char == "(") - (char == ")")
        index += 1
    parts.append(text[start:])
    return parts


def evaluate(text: str, allowed: frozenset[str]) -> bool:
    """True when some ``OR`` alternative has every ``AND`` part in ``allowed`` (minus ``WITH``)."""
    text = _strip_outer(text)
    alternatives = _split_top(text, " OR ")
    if len(alternatives) > 1:
        return any(evaluate(alternative, allowed) for alternative in alternatives)
    parts = _split_top(text, " AND ")
    if len(parts) > 1:
        return all(evaluate(part, allowed) for part in parts)
    return text.split(" WITH ", 1)[0].strip() in allowed


def _entry_text(raw: object, aliases: Mapping[str, str]) -> str | None:
    entry = _as(dict, raw, "licenses entry")
    if "expression" in entry:
        return _as(str, entry["expression"], "licence expression")
    if "license" not in entry:
        return None
    licence = _as(dict, entry["license"], "license")
    if "id" in licence:
        return _as(str, licence["id"], "license.id")
    if "name" in licence:
        name = _as(str, licence["name"], "license.name")  # a trove classifier: its last segment
        key = name.rsplit("::", 1)[-1] if name.startswith("License ::") else name
        return aliases.get(key.strip().casefold(), name)
    return None


def licence_texts(component: Mapping[str, Any], aliases: Mapping[str, str]) -> list[str]:
    """Licence texts of a component; alias keys match case-insensitively; none → UNKNOWN."""
    folded = {key.casefold(): value for key, value in aliases.items()}
    texts = [
        text
        for raw in _as(list, component.get("licenses", []), "licenses")
        if (text := _entry_text(raw, folded)) is not None
    ]
    return texts or [UNKNOWN]


def classify(name: str, texts: list[str], policy: Policy) -> str:
    """Return ``allowed``, ``approved``, ``report-only`` or ``denied``."""
    if any(evaluate(text, policy.allowed) for text in texts):
        return "allowed"
    if any(
        fnmatch.fnmatchcase(name, approval.package) and approval.licence in texts
        for approval in policy.approved
    ):
        return "approved"
    if any(fnmatch.fnmatchcase(name, glob) for glob in policy.report_only):
        return "report-only"
    return "denied"


def _identity(component: Mapping[str, Any]) -> tuple[object, ...]:
    ref = component.get("bom-ref")
    if ref is not None:
        return ("ref", ref)
    return ("name", component.get("name"), component.get("version"))


def load_components(path: Path) -> list[dict[str, Any]]:
    """Components of the SBOM except ``metadata.component`` (Herness itself)."""
    try:
        doc = _as(dict, json.loads(path.read_text(encoding="utf-8")), path.name)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"cannot read {path.name}: {exc}"
        raise _InputError(msg) from exc
    metadata = doc.get("metadata")
    root = metadata.get("component") if isinstance(metadata, dict) else None
    skip = _identity(root) if isinstance(root, dict) else None
    components = [
        _as(dict, raw, "component") for raw in _as(list, doc.get("components"), "components")
    ]
    for component in components:
        _as(str, component.get("name"), "component name")
    return [c for c in components if skip is None or _identity(c) != skip]


def _line(label: str, name: str, component: Mapping[str, Any], texts: list[str]) -> str:
    version = component.get("version", "")
    return f"{label} {name} {version}: {' | '.join(texts)}\n"


def run(args: argparse.Namespace) -> int:
    """Run the gate for parsed arguments; raises _InputError on bad input."""
    policy = load_policy(args.pyproject)
    denied_label = "LC001 denied" if args.mode == "enforce" else "LC001 warning denied"
    labels = {"approved": "approved", "report-only": "LC002 report-only", "denied": denied_label}
    denied = 0
    for component in load_components(args.sbom):
        name = normalise(component["name"])
        texts = licence_texts(component, policy.aliases)
        decision = classify(name, texts, policy)
        if decision != "allowed":
            sys.stdout.write(_line(labels[decision], name, component, texts))
        denied += decision == "denied"
    return 1 if denied and args.mode == "enforce" else 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; see the module docstring for exit codes."""
    parser = argparse.ArgumentParser(prog="check_licences", description=__doc__)
    parser.add_argument("--sbom", type=Path, required=True)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--mode", choices=("enforce", "report"), default="enforce")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        return run(args)
    except _InputError as exc:
        sys.stderr.write(f"input error: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
