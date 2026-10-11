"""Comparison of finished eval runs (U11-71; design 11 §5.3.5) and the table text helpers
shared with the report.

Private sibling of `herness.eval.report` (§2 module map), which re-exports `ComparisonTable`
and writes the files (`compare`). `build_comparison` only reads what the runs stored, each
run's `summary.json` and `results.jsonl` (no pipeline re-runs; every number comes from the
stored metrics, TH11-12). Run ids are checked against `run_<ulid>` before any path is joined.
Rows keep the given order (duplicates dropped) and are labelled by `(profile, depth)`;
deltas and flips are against the first run; a numeric question whose reference value or
matched NumberRef value differs between runs on the same build is a harness defect (spec 00
principle 1). Deltas render as percentage points for the rates and as plain differences
otherwise, in this comparison and in the report's baseline diff alike.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from herness.core.errors import ConfigError
from herness.eval.metrics import EvalMetrics
from herness.eval.results import QuestionResult, ResultsLog

__all__ = [
    "DIFF_FIELDS", "RUN_ID_RE", "ComparisonDelta", "ComparisonRow", "ComparisonTable", "Flip",
    "build_comparison", "comparison_lines", "comparison_view", "delta_text", "fmt",
    "load_summary", "md_cell", "md_table",
]  # fmt: skip

RUN_ID_RE: Final = re.compile(r"^run_[0-9A-HJKMNP-TV-Z]{26}$")
DIFF_FIELDS: Final = (
    "answer_correctness", "unsupported_number_rate", "verifier_rejection_rate",
    "tool_call_success_rate", "retries_per_run", "fallbacks_per_run", "repairs_per_run",
    "guard_stops_per_run", "tokens_in", "tokens_out", "cost_usd", "chat_latency_p50_s",
    "chat_latency_p95_s", "review_latency_p50_s", "review_latency_p95_s",
    "review_latency_max_s", "judge_mean",
)  # fmt: skip
_RATES: Final = frozenset({
    "answer_correctness", "unsupported_number_rate", "verifier_rejection_rate",
    "tool_call_success_rate",
})  # fmt: skip

type Number = int | float | Decimal


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ComparisonRow(_Model):
    """One compared run: its id, `(profile, depth)`, build and stored metrics."""

    run_id: str
    profile: str
    depth: str
    build_id: str
    metrics: dict[str, Any]


class ComparisonDelta(_Model):
    """A later run's §5.3.4 scalar metrics minus the first run's (None when either is)."""

    run_id: str
    metrics: dict[str, Number | None]


class Flip(_Model):
    """A `(question_id, repeat)` whose status differs from the first run's."""

    question_id: str
    repeat: int
    from_run: str
    to_run: str
    before: str
    after: str


class ComparisonTable(_Model):
    """The comparison of stored eval runs (U11-71); `comparison.json` is its JSON dump."""

    run_ids: list[str]
    newest_run_id: str
    rows: list[ComparisonRow]
    deltas: list[ComparisonDelta]
    flips: list[Flip]
    harness_defects: list[str]
    suite_changed: bool


class _Head(BaseModel):
    """The `summary.json` keys a comparison reads (other keys are ignored)."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    profile: str
    depth: str
    build_id: str
    suite_sha256: str
    mock: bool
    started_at: str


@dataclass(frozen=True)
class _Stored:
    run_id: str
    head: _Head
    metrics: EvalMetrics
    results: list[QuestionResult]


def _run_dir(run_id: str, data_root: Path) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        msg = "run ids must be run_<ulid>"
        raise ConfigError(msg)
    return data_root / "reports" / "eval" / run_id


def load_summary(run_id: str, data_root: Path) -> dict[str, Any]:
    """The stored `summary.json` of `run_id` (checked as `run_<ulid>` first)."""
    path = _run_dir(run_id, data_root) / "summary.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"eval run not found: run_id={run_id}"
        raise ConfigError(msg) from exc
    except (OSError, ValueError) as exc:
        msg = f"summary is not valid: run_id={run_id}"
        raise ConfigError(msg) from exc
    if not isinstance(data, dict):
        msg = f"summary is not valid: run_id={run_id}"
        raise ConfigError(msg)
    return data


def _load(run_id: str, data_root: Path) -> _Stored:
    summary = load_summary(run_id, data_root)
    try:
        head = _Head.model_validate(summary)
        metrics = EvalMetrics.model_validate(summary.get("metrics"))
    except ValidationError as exc:
        msg = f"summary is not valid: run_id={run_id}"
        raise ConfigError(msg) from exc
    log = ResultsLog(_run_dir(run_id, data_root) / "results.jsonl")
    results = [r for r in log.read_all() if r.id in metrics.question_status]
    return _Stored(run_id, head, metrics, results)


def build_comparison(run_ids: Sequence[str], data_root: Path) -> ComparisonTable:
    """Compare the stored runs `run_ids` (U11-71); nothing is written here."""
    ids = list(dict.fromkeys(run_ids))
    for run_id in ids:
        _run_dir(run_id, data_root)  # every id is checked before any file is read
    if len(ids) < 2:  # noqa: PLR2004 - a comparison needs two runs
        msg = "compare needs at least two distinct runs"
        raise ConfigError(msg)
    runs = [_load(run_id, data_root) for run_id in ids]
    if len({run.head.mock for run in runs}) > 1:
        msg = "cannot compare mock and real runs"
        raise ConfigError(msg)
    first = runs[0]
    return ComparisonTable(
        run_ids=ids,
        newest_run_id=max(runs, key=lambda r: (r.head.started_at, r.run_id)).run_id,
        rows=[
            ComparisonRow(
                run_id=r.run_id,
                profile=r.head.profile,
                depth=r.head.depth,
                build_id=r.head.build_id,
                metrics=r.metrics.model_dump(mode="json"),
            )
            for r in runs
        ],
        deltas=[
            ComparisonDelta(
                run_id=r.run_id,
                metrics={
                    name: _delta(getattr(first.metrics, name), getattr(r.metrics, name))
                    for name in DIFF_FIELDS
                },
            )
            for r in runs[1:]
        ],
        flips=[flip for r in runs[1:] for flip in _flips(first, r)],
        harness_defects=_defects(runs),
        suite_changed=any(r.head.suite_sha256 != first.head.suite_sha256 for r in runs),
    )


def _delta(before: Any, after: Any) -> Number | None:  # noqa: ANN401 - one metric's type
    """`after - before` of one metric (both int, float or Decimal); None if either is."""
    return None if before is None or after is None else after - before


def _flips(first: _Stored, other: _Stored) -> list[Flip]:
    """Status changes over the `(question_id, repeat)` keys present in both runs."""
    before = {r.id_key: r.status for r in first.results}
    return [
        Flip(question_id=r.id, repeat=r.repeat, from_run=first.run_id, to_run=other.run_id,
             before=before[r.id_key], after=r.status)
        for r in sorted(other.results, key=lambda r: r.id_key)
        if r.id_key in before and before[r.id_key] != r.status
    ]  # fmt: skip


type _Numbers = dict[tuple[str, int], tuple[tuple[Decimal | None, Decimal | None], ...]]


def _defects(runs: Sequence[_Stored]) -> list[str]:
    """Question ids whose numeric checks disagree with the first run on the same build."""
    groups: dict[str, list[_Stored]] = {}
    for run in runs:
        groups.setdefault(run.head.build_id, []).append(run)
    names: set[str] = set()
    for group in groups.values():
        base = _numbers(group[0])
        for other in group[1:]:
            cur = _numbers(other)
            names |= {key[0] for key in base.keys() & cur.keys() if base[key] != cur[key]}
    return sorted(names)


def _numbers(run: _Stored) -> _Numbers:
    """Per graded `(question_id, repeat)`: (reference value, matched NumberRef value) of
    each numeric check; the matched value is None when the check did not pass."""
    out: _Numbers = {}
    for r in run.results:
        checks = tuple(_numeric(c.status, c.detail) for c in r.checks if c.check == "numeric")
        if checks:
            out[r.id_key] = checks
    return out


def _numeric(status: str, detail: Mapping[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    reference = _decimal(detail.get("reference"))
    candidates = detail.get("candidates")
    if status != "passed" or reference is None or not isinstance(candidates, list):
        return reference, None
    values = [
        value for c in candidates
        if isinstance(c, Mapping) and (value := _decimal(c.get("value"))) is not None
    ]  # fmt: skip
    # `grade_numeric` passes when a candidate is within tolerance; the closest one matched.
    return reference, min(values, key=lambda v: abs(v - reference), default=None)


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


# --- text helpers (report.md, comparison.md, the HTML view) ------------------------------------


def fmt(value: object) -> str:
    """A metric value as table text (`-` for None)."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(round(value, 6))
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value)


def delta_text(name: str, delta: Number | None) -> str:
    """A delta: signed percentage points for a rate (0..1), else a signed difference."""
    if delta is None:
        return "-"
    if name in _RATES:
        return f"{float(delta) * 100:+.1f} pp"
    text = fmt(delta)
    return f"+{text}" if delta > 0 else text


def md_cell(value: str) -> str:
    """A Markdown table cell: backslash and `|` escaped, line breaks flattened."""
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def md_table(head: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """A Markdown table (header cells are trusted text, body cells are escaped)."""
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    lines += ["| " + " | ".join(md_cell(v) for v in row) + " |" for row in rows]
    return [*lines, ""]


def _code(value: str) -> str:
    """`value` as an inline code span, so stored text is never read as markup."""
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    ticks = "`" * (longest + 1)
    pad = " " if longest else ""
    return f"{ticks}{pad}{value}{pad}{ticks}"


def comparison_view(table: ComparisonTable) -> dict[str, Any]:
    """Plain strings of `table` for both renderings (escaped by each renderer)."""
    labels = [f"#{n}" for n in range(1, len(table.rows) + 1)]
    deltas = [d.metrics for d in table.deltas]
    return {
        "suite_changed": table.suite_changed,
        "labels": labels,
        "runs": [
            (label, r.run_id, r.profile, r.depth, r.build_id)
            for label, r in zip(labels, table.rows, strict=True)
        ],
        "metrics": [
            (
                name,
                [fmt(r.metrics.get(name)) for r in table.rows]
                + [delta_text(name, d.get(name)) for d in deltas],
            )
            for name in DIFF_FIELDS
        ],
        "flips": [
            (f.question_id, str(f.repeat), f.from_run, f.to_run, f.before, f.after)
            for f in table.flips
        ],
        "harness_defects": list(table.harness_defects),
    }


def comparison_lines(view: Mapping[str, Any], *, level: int) -> list[str]:
    """The comparison in Markdown, headed at `level` (1 in `comparison.md`, 2 in a report)."""
    h = "#" * level
    out = [f"{h} Comparison", "",
           "Deltas are against run #1; numbers come from the stored runs only.", ""]  # fmt: skip
    if view["suite_changed"]:
        out += ["**Suite changed:** the runs used different golden suites; flips cover only "
                "the questions present in both runs.", ""]  # fmt: skip
    out += [f"{h}# Runs", ""]
    out += md_table(("#", "Run", "Profile", "Depth", "Build"),
                    [(n, *map(_code, rest)) for n, *rest in view["runs"]])  # fmt: skip
    if view["harness_defects"]:
        out += [
            f"{h}# Harness defects",
            "",
            "Numeric questions that disagree on the same build (spec 00 principle 1):",
            "",
        ]
        out += [f"- {_code(q)}" for q in view["harness_defects"]] + [""]
    labels = view["labels"]
    out += [f"{h}# Metrics", ""]
    out += md_table(("Metric", *labels, *(f"Δ {label}" for label in labels[1:])),
                    [(name, *cells) for name, cells in view["metrics"]])  # fmt: skip
    out += [f"{h}# Flips", ""]
    if not view["flips"]:
        return [*out, "None.", ""]
    rows = [(_code(q), rep, _code(a), _code(b), _code(x), _code(y))
            for q, rep, a, b, x, y in view["flips"]]  # fmt: skip
    return out + md_table(("Question", "Repeat", "From", "To", "Before", "After"), rows)
