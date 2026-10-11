"""The classifier section of `report.md` and `report.html` (U11-70 with U11-67; T11-35).

Private sibling of `herness.eval.classifier` (§2 module map), imported by
`herness.eval.report`: `classifier_view` turns a `ClassifierReport` into plain table rows
(failed gate checks first), `classifier_lines` renders them as Markdown with every stored
identifier in an inline code span and every cell escaped. The HTML section is part of the
autoescaped report template. The full report (CIs, confusion matrices, reliability bins)
stays in `classifier.json`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final

from herness.eval._report_compare import fmt, md_table

if TYPE_CHECKING:
    from herness.eval._classifier_metrics import DeciderMetrics
    from herness.eval.classifier import ClassifierReport

__all__ = ["classifier_lines", "classifier_view"]

METRIC_HEAD: Final = ("N", "Accuracy (95 % CI)", "Macro-F1", "MAE", "Within one", "ECE",
                      "Coverage")  # fmt: skip


def _ci(m: DeciderMetrics, name: str) -> str:
    value = getattr(m, name)
    interval = m.ci.get(name)
    if value is None or interval is None:
        return fmt(value)
    return f"{fmt(value)} [{fmt(interval[0])}, {fmt(interval[1])}]"


def _metrics(m: DeciderMetrics) -> list[str]:
    names = ("macro_f1", "mae", "within_one", "ece", "coverage_at_threshold")
    return [fmt(m.n), _ci(m, "accuracy"), *(fmt(getattr(m, name)) for name in names)]


def classifier_view(report: ClassifierReport) -> dict[str, Any]:
    """Plain rows for both renderings."""
    checks = sorted(report.gate_check, key=lambda c: c.passed)
    return {
        "passed": report.passed,
        "version": report.version,
        "question_set_version": report.question_set_version,
        "head": METRIC_HEAD,
        "checks": [
            (
                c.check,
                c.question or "-",
                c.metric or "-",
                fmt(c.stored),
                fmt(c.recomputed),
                c.passed,
                c.detail or "-",
            )
            for c in checks
        ],
        "deciders": [
            (decider, qid, m.status, m.decider_version or "-", *_metrics(m))
            for decider, questions in report.deciders.items()
            for qid, m in sorted(questions.items())
        ],
        "synthetic": None
        if report.synthetic is None
        else [(qid, m.status, *_metrics(m)) for qid, m in sorted(report.synthetic.items())],
    }


def _code(value: str) -> str:
    """`value` as an inline code span, so stored text is never read as markup."""
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    ticks = "`" * (longest + 1)
    pad = " " if longest else ""
    return f"{ticks}{pad}{value}{pad}{ticks}"


def classifier_lines(view: Mapping[str, Any]) -> list[str]:
    """The classifier section in Markdown (level-2 heading, as the report's sections)."""
    verdict = "passed" if view["passed"] else "FAILED"
    out = ["## Classifier", "",
           f"Laya {_code(view['version'])} on question set "
           f"{_code(view['question_set_version'])}: gate cross-check {verdict}.", "",
           "### Gate checks", ""]  # fmt: skip
    out += md_table(
        ("Check", "Question", "Metric", "Stored", "Recomputed", "Passed", "Detail"),
        [(c, _code(q), m, s, r, fmt(ok), d) for c, q, m, s, r, ok, d in view["checks"]],
    )
    out += ["### Deciders", ""]
    out += md_table(
        ("Decider", "Question", "Status", "Version", *view["head"]),
        [(d, _code(q), s, _code(v), *rest) for d, q, s, v, *rest in view["deciders"]],
    )
    if view["synthetic"] is not None:
        out += ["### Synthetic truth check (informational)", ""]
        out += md_table(
            ("Question", "Status", *view["head"]),
            [(_code(q), *rest) for q, *rest in view["synthetic"]],
        )
    return out
