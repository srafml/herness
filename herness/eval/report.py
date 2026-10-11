"""Eval report files and baselines (U11-70, U11-72; design 11 §4.6, flow F11-07 step 12).

`write_report` writes `summary.json`, `report.md` and `report.html` (plus `classifier.json`
when given) into the run directory, each atomically (temp file + `os.replace`). Model text is
scrubbed of known secrets, capped, and shown as plain text: a code fence in Markdown and an
autoescaped `<pre>` in HTML (TH11-05); the HTML loads nothing external. Baselines under
`tests/eval/baselines/<name>.json` hold aggregates only and are refused on real builds
(TH11-14) and, for a mock run, unless the name ends in `-mock` (TH11-10). A baseline of
another suite comes back with `suite_changed` set, which turns the correctness drop check
into the detail `suite changed` (TH11-03). `compare` (U11-71) compares stored runs and
writes `comparison.json` and `comparison.md` into the newest one; the model, loading and
Markdown live in the private sibling `_report_compare`. `write_report` adds the comparison
section when given one (`--compare`).
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import jinja2
from pydantic import BaseModel

from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.core.secrets import scrub_secrets
from herness.eval._report_compare import (
    DIFF_FIELDS,
    ComparisonTable,
    build_comparison,
    comparison_lines,
    comparison_view,
    delta,
    delta_text,
    fmt,
    md_cell,
    md_table,
)
from herness.eval.metrics import EvalMetrics

if TYPE_CHECKING:
    from herness.eval.metrics import ThresholdResult
    from herness.eval.results import QuestionResult
    from herness.eval.runner import EvalRun

__all__ = [
    "BASELINES_DIR", "BASELINE_NAME_RE", "PROVENANCE_KEYS", "ComparisonTable",
    "StoredBaseline", "baseline_mismatch", "check_baseline_target", "compare",
    "load_baseline", "read_baseline", "save_baseline", "write_report",
]  # fmt: skip

BASELINE_NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
BASELINES_DIR: Final = Path("tests") / "eval" / "baselines"
BASELINE_SCHEMA: Final = 1
PROVENANCE_KEYS: Final = (
    "run_id", "profile", "depth", "build_id", "dataset_kind", "git_sha", "config_hash",
    "suite_sha256", "judge_model", "judge_prompt_sha256",
)  # fmt: skip
_BASELINE_KEYS: Final = (
    "profile", "depth", "suite_version", "suite_sha256", "dataset_seed", "dataset_scale",
)  # fmt: skip
_META_KEYS: Final = (
    "run_id", "profile", "depth", "build_id", "dataset_kind", "dataset_seed", "dataset_scale",
    "suite_version", "suite_sha256", "git_sha", "config_hash", "judge_model",
    "judge_prompt_sha256", "mock", "baseline", "compare_profile", "started_at", "finished_at",
)  # fmt: skip
_FAILING: Final = frozenset({"failed", "error", "suite_error"})
_TEXT_MAX: Final = 20_000
_TRUNCATED: Final = "\n[truncated]"
_WITHHELD: Final = "[withheld]"
_TEMPLATE: Final = "report.html.j2"

_log = get_logger("eval.report")


# --- U11-70 write_report ------------------------------------------------------------------------


def write_report(  # noqa: PLR0913 - signature fixed by U11-70 (keyword-only after out_dir)
    eval_run: EvalRun,
    out_dir: Path,
    *,
    results: Sequence[QuestionResult],
    metrics: EvalMetrics,
    thresholds: Sequence[ThresholdResult],
    baseline: EvalMetrics | None,
    classifier: BaseModel | Mapping[str, Any] | None,
    comparison: ComparisonTable | None = None,
) -> list[Path]:
    """Write the run's report files atomically; returns their paths in write order. With
    `comparison` (`--compare`) both reports end with the comparison section."""
    summary: dict[str, Any] = {
        **eval_run.summary,
        "metrics": metrics.model_dump(mode="json"),
        "thresholds": [row.model_dump(mode="json") for row in thresholds],
    }
    missing = [key for key in PROVENANCE_KEYS if key not in summary]
    if missing:
        msg = f"summary lacks provenance keys: {', '.join(missing)}"
        raise ConfigError(msg)
    view = _view(summary, results, metrics, thresholds, baseline)
    view["comparison"] = None if comparison is None else comparison_view(comparison)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        _write(out_dir / "summary.json", _json(summary)),
        _write(out_dir / "report.md", _markdown(view)),
        _write(out_dir / "report.html", _environment().get_template(_TEMPLATE).render(view)),
    ]
    if classifier is not None:
        body = (
            classifier.model_dump(mode="json") if isinstance(classifier, BaseModel) else classifier
        )
        paths.append(_write(out_dir / "classifier.json", _json(body)))
    return paths


@functools.cache
def _environment() -> jinja2.Environment:
    """Autoescaping environment over the package template (no filesystem paths, TH11-05)."""
    return jinja2.Environment(
        loader=jinja2.PackageLoader("herness.eval", "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def _view(
    summary: Mapping[str, Any],
    results: Sequence[QuestionResult],
    metrics: EvalMetrics,
    thresholds: Sequence[ThresholdResult],
    baseline: EvalMetrics | None,
) -> dict[str, Any]:
    """Plain values for both renderings: failures first, then passes."""
    rows = sorted(thresholds, key=lambda row: row.passed)
    ordered = sorted(results, key=lambda r: (_rank(r.status), r.id, r.repeat))
    return {
        "run_id": str(summary["run_id"]),
        "passed": bool(summary.get("passed")),
        "exit_code": _fmt(summary.get("exit_code")),
        "meta": [(key, _fmt(summary.get(key))) for key in _META_KEYS if key in summary],
        "thresholds": [
            {
                "check": row.check,
                "passed": row.passed,
                "actual": _fmt(row.actual),
                "limit": _fmt(row.limit),
                "detail": row.detail,
            }
            for row in rows
        ],
        "metrics": [(name, _fmt(getattr(metrics, name))) for name in DIFF_FIELDS],
        "baseline": None if baseline is None else _diff(baseline, metrics),
        "questions": [
            {
                "id": r.id,
                "repeat": r.repeat,
                "status": r.status,
                "error": _clean(r.error or ""),
                "checks": ", ".join(f"{c.check} {c.status}" for c in r.checks) or "-",
                "text": _clean(r.final_text),
            }
            for r in ordered
        ],
    }


def _rank(status: str) -> int:
    return 0 if status in _FAILING else 1 if status == "skipped" else 2


def _diff(baseline: EvalMetrics, metrics: EvalMetrics) -> dict[str, Any]:
    rows = []
    for name in DIFF_FIELDS:
        before, after = getattr(baseline, name), getattr(metrics, name)
        rows.append((name, _fmt(before), _fmt(after), delta_text(name, delta(before, after))))
    return {"suite_changed": baseline.suite_changed, "rows": rows}


_fmt, _cell, _table = fmt, md_cell, md_table  # shared with the comparison text (T11-38)


def _clean(text: str) -> str:
    """Known secrets masked (impl 10 scrubber), then capped at `_TEXT_MAX` characters."""
    scrubbed = scrub_secrets(None, "report", {"t": text}).get("t")
    clean = scrubbed if isinstance(scrubbed, str) else _WITHHELD
    return clean if len(clean) <= _TEXT_MAX else clean[:_TEXT_MAX] + _TRUNCATED


def _fence(text: str) -> str:
    """A code fence longer than any backtick run in `text`, so the text stays literal."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _markdown(view: Mapping[str, Any]) -> str:
    verdict = "PASSED" if view["passed"] else "FAILED"
    out = [f"# Eval report {view['run_id']}", "",
           f"Result: **{verdict}** (exit code {view['exit_code']})", "", "## Run", ""]  # fmt: skip
    out += _table(("Field", "Value"), view["meta"])
    out += ["## Thresholds", ""]
    out += _table(
        ("Check", "Passed", "Actual", "Limit", "Detail"),
        [(t["check"], _fmt(t["passed"]), t["actual"], t["limit"], t["detail"])
         for t in view["thresholds"]],
    )  # fmt: skip
    out += ["## Metrics", ""]
    out += _table(("Metric", "Value"), view["metrics"])
    if view["baseline"] is not None:
        out += ["## Baseline diff", ""]
        if view["baseline"]["suite_changed"]:
            out += ["The baseline was taken on another suite: the correctness drop check is off.",
                    ""]  # fmt: skip
        out += _table(("Metric", "Baseline", "This run", "Delta"), view["baseline"]["rows"])
    out += ["## Questions", ""]
    for q in view["questions"]:
        out += [f"### {q['id']} (repeat {q['repeat']}): {q['status']}", "",
                f"Checks: {_cell(q['checks'])}", ""]  # fmt: skip
        for label, text in (("Error", q["error"]), ("Final text", q["text"])):
            if text:
                fence = _fence(text)
                out += [f"{label}:", "", f"{fence}text", text, fence, ""]
    if view["comparison"] is not None:
        out += comparison_lines(view["comparison"], level=2)
    return "\n".join(out)


def _json(body: object) -> str:
    return json.dumps(body, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write(path: Path, text: str) -> Path:
    """Write `text` atomically: a temp file in the same directory, then `os.replace`."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    return path


# --- U11-71 compare ------------------------------------------------------------------------------


def compare(run_ids: Sequence[str], *, data_root: Path) -> ComparisonTable:
    """Compare the stored eval runs `run_ids` (U11-71): rows, deltas against the first run,
    flips and harness defects; writes `comparison.json` and `comparison.md` into the newest
    run's directory (atomically) and returns the table. Never re-runs a pipeline."""
    table = build_comparison(run_ids, data_root)
    out = data_root / "reports" / "eval" / table.newest_run_id
    _write(out / "comparison.json", _json(table.model_dump(mode="json")))
    _write(out / "comparison.md", "\n".join(comparison_lines(comparison_view(table), level=1)))
    return table


# --- U11-72 save_baseline, load_baseline ---------------------------------------------------------


def _baseline_path(name: str, repo_root: Path) -> Path:
    if not BASELINE_NAME_RE.fullmatch(name):
        msg = "baseline name must match ^[a-z0-9][a-z0-9-]{0,63}$"
        raise ConfigError(msg)
    return repo_root / BASELINES_DIR / f"{name}.json"


def _refuse(name: str, reason: str, msg: str) -> None:
    _log.warning("eval.baseline.refused", name=name, reason=reason)
    raise ConfigError(msg)


def check_baseline_target(name: str, *, dataset_kind: object, mock: object) -> None:
    """Refuse a baseline name, a real build (TH11-14) or a mock run's non-`-mock` name
    (TH11-10) with `ConfigError`; the runner calls it before the run starts."""
    _baseline_path(name, Path())
    if dataset_kind != "synthetic":
        _refuse(name, "real_build", "baselines are saved from synthetic builds only")
    if mock is True and not name.endswith("-mock"):
        _refuse(name, "mock_name", "a mock run's baseline name must end in -mock")


def save_baseline(name: str, summary: Mapping[str, Any], *, repo_root: Path) -> Path:
    """Write `tests/eval/baselines/<name>.json` with aggregates only (U11-72): the metrics
    (validated, so no text key passes), profile, depth, suite version and SHA-256, dataset
    seed and scale."""
    path = _baseline_path(name, repo_root)
    check_baseline_target(name, dataset_kind=summary.get("dataset_kind"), mock=summary.get("mock"))
    try:
        metrics = EvalMetrics.model_validate(summary.get("metrics"))
    except ValueError as exc:
        msg = f"baseline metrics are not valid: name={name}"
        raise ConfigError(msg) from exc
    body: dict[str, Any] = {"schema": BASELINE_SCHEMA, "name": name,
                            "metrics": metrics.model_dump(mode="json")}  # fmt: skip
    body |= {key: summary.get(key) for key in _BASELINE_KEYS}
    path.parent.mkdir(parents=True, exist_ok=True)
    _write(path, _json(body))
    _log.info("eval.baseline.saved", name=name, run_id=summary.get("run_id"))
    return path


@dataclass(frozen=True)
class StoredBaseline:
    """A loaded baseline: its name, the run it was taken on and its metrics (U11-72)."""

    name: str
    profile: object
    depth: object
    dataset_scale: object
    metrics: EvalMetrics


def read_baseline(
    name: str, *, repo_root: Path, suite_sha256: str | None = None
) -> StoredBaseline | None:
    """`load_baseline` plus the stored profile, depth and scale (comparability, T11-31
    note 7). A `suite_changed` key in the stored metrics is refused: only the loader sets it."""
    path = _baseline_path(name, repo_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        if "suite_changed" in data["metrics"]:
            raise ValueError(name)  # noqa: TRY301 - reported as an invalid baseline below
        metrics = EvalMetrics.model_validate(data["metrics"])
        stored = data.get("suite_sha256")
        head = (data.get("profile"), data.get("depth"), data.get("dataset_scale"))
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        msg = f"baseline is not valid: name={name}"
        raise ConfigError(msg) from exc
    if suite_sha256 is not None and stored != suite_sha256:
        metrics = metrics.model_copy(update={"suite_changed": True})
    return StoredBaseline(name, *head, metrics=metrics)


def load_baseline(
    name: str, *, repo_root: Path, suite_sha256: str | None = None
) -> EvalMetrics | None:
    """The stored baseline metrics, or None when the file is missing; with `suite_sha256`
    (the current suite) a baseline of another suite has `suite_changed` set (TH11-03)."""
    found = read_baseline(name, repo_root=repo_root, suite_sha256=suite_sha256)
    return found.metrics if found is not None else None


def baseline_mismatch(
    base: StoredBaseline, *, profile: str, depth: str, dataset_kind: str, mock: bool
) -> str | None:
    """Why `base` is not comparable with the run, or None (T11-31 note 7)."""
    if dataset_kind != "synthetic":
        return "run is not synthetic"
    if (base.profile, base.depth) != (profile, depth):
        return f"taken at {base.profile}-{base.depth}, run is {profile}-{depth}"
    if mock != base.name.endswith("-mock"):
        return "mock runs use -mock baselines only" if mock else "a -mock baseline needs a mock run"
    return None
