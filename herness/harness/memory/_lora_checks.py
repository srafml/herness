"""Path, golden and secrets-sink checks of the LoRA export (impl 07 U07-92, T07-20).

Private sibling of `lora` (kept apart for the §2 line budget; only `lora` imports it).
TH07-24: the export path stays at or below the export root with no symlinked or junction
component. TH07-19: a pair whose redacted question is within the configured cosine of a
redacted golden question is excluded. TH07-05/TH07-19: a qa_pair reaches the JSONL only when
its question survives the redactor and the injection scan, and its SQL is exactly one DuckDB
query that carries no redaction token, trips no injection pattern and is unchanged by the
redactor and by the known-secret scrub.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final, Protocol

import numpy as np
import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from herness.core.errors import ModelUnavailable, PermissionDenied, ToolInputError
from herness.core.logging import get_logger
from herness.core.redact import RedactionFailed, Redactor
from herness.core.redact_patterns import TOKEN_PATTERN
from herness.core.secrets import scrub_secrets
from herness.harness.memory.policy import InjectionScanner
from herness.harness.memory.settings import LoraConfig

__all__ = ["Embeds", "contained", "goldens", "near", "redacted", "safe_pair", "scrubbed"]

DENIED: Final = "export path outside data/models/lora_data"
_log = get_logger("memory")


class Embeds(Protocol):
    """The memory embedder's `embed` (unit vectors; U07-48)."""

    def embed(self, text: str) -> np.ndarray: ...


class _Checks(Protocol):
    @property
    def redactor(self) -> Redactor: ...
    @property
    def scanner(self) -> InjectionScanner: ...
    @property
    def embedder(self) -> Embeds: ...
    @property
    def config(self) -> LoraConfig: ...


def contained(out_dir: Path, root: Path) -> Path:
    """Absolute `out_dir` when at or below `root`, no component between them is a symlink or
    junction (checked on the unresolved path) and it resolves inside `root`; else
    `PermissionDenied`. The rejection log names no path."""
    base, target = Path(os.path.abspath(root)), Path(os.path.abspath(out_dir))
    ok = target.is_relative_to(base)
    probe = base
    for part in target.relative_to(base).parts if ok else ():
        probe /= part
        ok = ok and not (probe.is_symlink() or probe.is_junction())
    if not (ok and target.resolve().is_relative_to(base.resolve())):
        _log.warning("memory.lora.rejected", reason="path")
        raise PermissionDenied(DENIED)
    return target


def redacted(text: object, deps: _Checks) -> str | None:
    """`text` through the redactor; None for a non-string, blank text or a redaction failure."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        found = deps.redactor.redact(text)
    except RedactionFailed:
        return None
    return None if found is None else found.text


def scrubbed(text: str) -> str:
    """`text` after the log scrubber's known-secret and credential masking."""
    return str(scrub_secrets(None, "lora", {"v": text}).get("v", ""))


def _sql_ok(sql: str, deps: _Checks) -> bool:
    try:
        parsed = sqlglot.parse(sql, read="duckdb")
    except SqlglotError:
        return False
    one_query = len(parsed) == 1 and isinstance(parsed[0], exp.Query)
    clean = TOKEN_PATTERN.search(sql) is None and not deps.scanner.scan(sql)
    return one_query and clean and redacted(sql, deps) == sql and scrubbed(sql) == sql


def safe_pair(question: object, sql: object, deps: _Checks) -> tuple[str, str] | None:
    """(redacted question, sql) when both are safe to export, else None."""
    user = redacted(question, deps)
    if user is None or not isinstance(sql, str) or deps.scanner.scan(user):
        return None
    return (user, sql) if _sql_ok(sql, deps) else None


def _unit(vector: np.ndarray) -> np.ndarray:
    v = np.asarray(vector, dtype=np.float64)
    norm = float(np.linalg.norm(v))
    if not norm > 0.0:
        msg = "lora embedding invalid output"
        raise ModelUnavailable(msg)
    return v / norm


def goldens(questions: Sequence[str], deps: _Checks) -> np.ndarray | None:
    """Each golden question redacted and embedded once, as unit rows; None for none."""
    if isinstance(questions, str):
        msg = "golden_questions must be a sequence of questions, not one string"
        raise ToolInputError(msg)
    texts = [t for t in (redacted(q, deps) for q in questions) if t is not None]
    return np.stack([_unit(deps.embedder.embed(t)) for t in texts]) if texts else None


def near(text: str, golden: np.ndarray | None, deps: _Checks) -> bool:
    """True when `text` is at or above the golden-exclusion cosine of any golden row."""
    if golden is None:
        return False
    best = float(np.max(golden @ _unit(deps.embedder.embed(text))))
    return best >= deps.config.golden_exclusion_cosine
