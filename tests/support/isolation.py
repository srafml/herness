"""Truth isolation scan (design §4.4, R-64, U11-34, TH11-02).

Nothing under `herness/` or `app/` may reference a truth file except the one allowlisted
loader (`herness/eval/truth.py`, R-64): pipelines and models must not be able to read the
planted answers they are being evaluated against.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

TRUTH_TOKENS: tuple[str, ...] = (
    "truth.json",
    "truth_labels",
    "/truth/",
    "\\truth\\",
    "t2_members",
    "t3_pairs",
)
ISOLATION_ALLOWLIST: frozenset[str] = frozenset({"herness/eval/truth.py"})

_SCAN_SUFFIXES: tuple[str, ...] = (".py", ".sql", ".yaml", ".md", ".j2")


def find_truth_references(roots: Sequence[Path]) -> list[tuple[str, int, str]]:
    """Lines under `roots` naming a truth-file token, outside `ISOLATION_ALLOWLIST`."""
    findings: list[tuple[str, int, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
                continue
            rel = path.relative_to(root.parent).as_posix()
            if rel in ISOLATION_ALLOWLIST:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for line_no, line in enumerate(text.splitlines(), start=1):
                findings.extend((rel, line_no, token) for token in TRUTH_TOKENS if token in line)
    return findings
