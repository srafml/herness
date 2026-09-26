"""Tests for the HTML report templates (impl 09 U09-28, T09-09).

``render.py`` (T09-11) does not exist yet, so the environment below is built exactly per the
U09-23 postconditions and a local wrapper converts ``jinja2.TemplateError`` to
``SchemaViolation`` as U09-24 does. The context ``r`` is built from real ``ReportData`` slot
objects and evidence entries; every ``list[Segment]`` becomes ``segments_to_html`` Markup and
every chart string Markup, as U09-24 hands them to the templates.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any, Final

import jinja2
import markupsafe
import pytest

from herness.core.errors import SchemaViolation
from herness.core.types import Coverage, NumberRef
from herness.reports import _data as d
from herness.reports import charts
from herness.reports._data_slots import (
    FUNDING_COLUMNS,
    Cell,
    PortfolioBlock,
    Scorecard,
    Table,
    TextBlock,
)
from herness.reports._evidence import EvidenceEntry
from herness.reports._markup import Segment, md_escape, segment_text, segments_to_html

pytestmark = pytest.mark.unit

REPORT_CSP: Final = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
Q1: Final = "q_0123456789abcdef"
Q2: Final = "q_fedcba9876543210"
Q3: Final = "q_00000000000000aa"
RUN_ID: Final = "run_01J9ZQ4Y8M6V3K2N1P0R5T7W9X"
EVIL: Final = "<script>alert(1)</script>"
TEMPLATES: Final = ("funding_review.html.j2", "org_review.html.j2")
_DIR: Final = Path(str(resources.files("herness.reports") / "templates"))


def _env() -> jinja2.Environment:
    """U09-23 postconditions, verbatim."""
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("herness.reports", "templates"),
        autoescape=jinja2.select_autoescape(
            enabled_extensions=("html.j2",), default_for_string=False, default=False
        ),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        auto_reload=False,
    )
    env.filters["md"] = md_escape
    env.globals.update(REPORT_CSP=REPORT_CSP, TEMPLATE_VERSION="1")
    return env


def _render(name: str, r: dict[str, Any]) -> str:
    """U09-24's conversion: any template error → SchemaViolation naming the template."""
    try:
        return _env().get_template(name).render(r=r)
    except jinja2.TemplateError as exc:
        msg = f"report template error: {name}"
        raise SchemaViolation(msg) from exc


def _ref(ref_id: str, value: object, unit: str, qid: str) -> NumberRef:
    return NumberRef.model_validate(
        {"id": ref_id, "value": value, "unit": unit, "query_id": qid, "column": "n",
         "row_key": {"team": "t1"}}
    )  # fmt: skip


def _html(segments: list[Segment]) -> Any:
    """Markup in place of a ``list[Segment]`` field: the HTML context is untyped by design."""
    return segments_to_html(segments)


def _block(where: str, text: str, refs: tuple[NumberRef, ...] = ()) -> TextBlock:
    return TextBlock(where, segment_text(text, refs))


def _html_block(block: TextBlock) -> TextBlock:
    return TextBlock(block.where, _html(block.segments))


def _cell(text: str, qid: str | None = Q1) -> Cell:
    return Cell(text, qid, "n · team=t1" if qid else "")


def _table(columns: tuple[str, ...], n: int = 1, marked: bool = False) -> Table:
    rows = [[_cell(f"{c}-{i}", Q1 if j else None) for j, c in enumerate(columns)] for i in range(n)]
    return Table(columns, rows, [marked] * n)


def _data(kind: str = "funding_review", publishable: bool = False) -> d.ReportData:
    """A real ``ReportData`` with every slot filled (segments still raw)."""
    n1, n2 = _ref("n1", 3, "count", Q1), _ref("n2", "1200.50", "usd", Q2)
    cover = Coverage(planned_tasks=4, done_tasks=3, dead_tasks=1, must_cover_total=2,
                     must_cover_done=2, verified_findings=5, rejected_findings=1,
                     publishable=publishable)  # fmt: skip
    now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    header = d.Header(segment_text("Quarterly review [[n1]]", (n1,)), RUN_ID, kind, "b1", now,
                      "standard", "local", now, cover, True, 3, 0)  # fmt: skip
    sections = {sid: [_block(f"sections[0].paragraphs[{i}]", f"{sid} text [[n1]]", (n1,))]
                for i, sid in enumerate(("executive_summary", "recommendations", "portfolio",
                                         "org_scorecards", "actions", "risks_and_caveats",
                                         "method"))}  # fmt: skip
    sections["executive_summary"].append(_block("x", f"{EVIL} rose to [[n2]]", (n2,)))
    retro_block = _block("prior_outcomes_commentary", "Last time [[n1]]", (n1,))
    sections["retrospective"] = [retro_block]
    card = d.Card(1, "fund", "epic", "epic-1", segment_text("Fund [[n2]]", (n2,)),
                  segment_text("Saves [[n2]] a year", (n2,)),
                  (("priority", _cell("1.20")), ("confidence", _cell("high"))),
                  ("fnd_1",))  # fmt: skip
    portfolio = PortfolioBlock("base", _cell("$1.2M", None), "optimal", False,
                               _table(("order_rank", "candidate_id", "expected_impact_usd")),
                               "portfolio-0")  # fmt: skip
    score = Scorecard("team", "t1", _table(("metric", "value", "peer_group", "peer_median",
                                             "z_score", "sample_size", "composite", "rank",
                                             "flags")), ["spark-0-0"])  # fmt: skip
    lever = d.LeverRow("team", "t1", "mttr", "reduce", _cell("$40K"), "Cut MTTR by 2", True,
                       ("rec-1",))  # fmt: skip
    outcomes = _table(("measurement", "metric", "baseline", "actual", "delta", "verdict"))
    retro = d.Retrospective([retro_block], [d.RetroItem("rec_1", "epic-9", segment_text("Old", ()),
                            "accept", outcomes)], {"improved": 1}, None)  # fmt: skip
    caveats = d.Caveats(_table(("check_name", "severity", "value", "threshold")), "4.5",
                        ["weights.mttr"], [("analyst", "size teams", "boom")], ["fnd_2"],
                        [("sections[1]", "uncited")], {"stale": ["team t2"]},
                        [_block("caveats[0]", "Data is young")])  # fmt: skip
    method = d.MethodInfo("standard", "local", True, 3, 0, {"analyst": 4, "writer": 1})
    appendix = d.RunAppendix([(1, "team", "t1")], [("analyst", "done", 3)], 1000, 200,
                             "$0.12", "cfg1")  # fmt: skip
    chart_map = {
        "funding_priority": charts.bar_h([("a", 1.0)], title="Priority by candidate"),
        "org_z": charts.dot_z([("t1 · mttr", 1.5)], title="z-score by metric"),
        "portfolio-0": charts.step_budget(
            [(Decimal(1), Decimal(2))], budget=Decimal(3), title="Impact vs budget: base"
        ),
        "spark-0-0": charts.sparkline([1.0, 2.0], title="mttr: t1"),
    }
    banners = d.derive_banners([], unconfirmed_keys=["weights.mttr"], dq_failed=True,
                               run_status="partial", dead_tasks=1, profile="cloud")  # fmt: skip
    is_fund = kind == "funding_review"
    return d.ReportData(
        header, publishable, banners, sections, {"executive_summary": "Summary"}, [card],
        _table(FUNDING_COLUMNS, 2, marked=True) if is_fund else None,
        None if is_fund else _table(("entity_id", "composite", "rank")), [portfolio], [score],
        [lever], retro, caveats, method, appendix, chart_map, 4, [Q1, Q2, Q1, Q2],
    )  # fmt: skip


def _evidence() -> list[EvidenceEntry]:
    sample = [{"team": "<b>t1</b>", "n": "3"}]
    return [
        EvidenceEntry(Q1, "SELECT n FROM t WHERE a < $x", {"x": 1}, 1, "2026-09-25T12:00:00Z",
                      "b1", sample, ["team", "n"], ["executive_summary", "rec-1"], True),
        EvidenceEntry(Q2, "SELECT 1", {}, 1, None, "b1", None, [], ["portfolio: base"], True),
        EvidenceEntry(Q3, "", {}, None, None, "b1", None, [], ["query_ids[0]"], False),
    ]  # fmt: skip


def _context(data: d.ReportData, evidence: list[EvidenceEntry]) -> dict[str, Any]:
    """The HTML ``r`` of U09-24: ReportData fields (segments → Markup), evidence, manifest."""
    retro = data.retro._replace(
        blocks=[_html_block(b) for b in data.retro.blocks],
        items=[i._replace(summary=_html(i.summary)) for i in data.retro.items],
    )
    r = {name: getattr(data, name) for name in d.ReportData.__dataclass_fields__}
    r.update(
        header=data.header._replace(title=_html(data.header.title)),
        sections={k: [_html_block(b) for b in v] for k, v in data.sections.items()},
        cards=[c._replace(headline=_html(c.headline), summary=_html(c.summary))
               for c in data.cards],
        retro=retro, caveats=data.caveats._replace(
            blocks=[_html_block(b) for b in data.caveats.blocks]),
        charts={k: markupsafe.Markup(v) for k, v in data.charts.items()},  # noqa: S704 - chart SVG
        evidence=evidence, numbers_linked=3,
    )  # fmt: skip
    return r


def _full(kind: str = "funding_review", publishable: bool = False) -> str:
    return _render(f"{kind}.html.j2", _context(_data(kind, publishable), _evidence()))


def _unused(name: str) -> set[str]:
    """Context names a template does not read (``number_query_ids`` feeds the manifest)."""
    other = "org_table" if name.startswith("funding") else "funding_table"
    return {other, "number_query_ids"}


@pytest.mark.parametrize("name", TEMPLATES)
def test_ut09_92_missing_variable_names_template(name: str) -> None:
    """UT09-92 a context missing one variable raises SchemaViolation naming the template."""
    kind = name.removesuffix(".html.j2")
    full = _context(_data(kind), _evidence())
    for key in sorted(set(full) - _unused(name)):
        with pytest.raises(SchemaViolation, match=re.escape(name)):
            _render(name, {k: v for k, v in full.items() if k != key})


@pytest.mark.parametrize("name", TEMPLATES)
def test_ut09_92_missing_nested_attribute(name: str) -> None:
    """UT09-92 a slot object missing an attribute is a SchemaViolation too (StrictUndefined)."""
    r = _context(_data(name.removesuffix(".html.j2")), _evidence())
    r["method"] = {"depth": "standard"}
    with pytest.raises(SchemaViolation, match=re.escape(name)):
        _render(name, r)


@pytest.mark.parametrize("kind", ["funding_review", "org_review"])
def test_ut09_92_full_context_renders_offline(kind: str) -> None:
    """UT09-92 the fixture context renders; ST09-03 static scan is clean."""
    html = _full(kind)
    assert html.startswith("<!DOCTYPE html>\n<html")
    assert "<script" not in html.lower()
    assert not re.search(r"https?:|//", html)
    assert "url(" not in html
    assert "<link" not in html.lower()
    assert "<img" not in html.lower()
    head = html.split("<head>", 1)[1].lstrip()
    assert head.startswith('<meta http-equiv="Content-Security-Policy" content="')
    assert "default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; img-src data:" in head
    assert '<meta charset="utf-8">' in html
    assert html.count("<style>") == 1


def test_ut09_92_slot_order_and_anchors() -> None:
    """UT09-92 slots 0 to 11 in design §5.2 order; card, number and evidence anchors."""
    html = _full()
    ids = ["executive_summary", "recommendations", "portfolio", "org_scorecards", "actions",
           "retrospective", "risks_and_caveats", "method", "evidence", "run"]  # fmt: skip
    positions = [html.index(f'<section id="{i}"') for i in ids]
    assert positions == sorted(positions)
    assert html.index('id="rec-1"') < html.index("<svg") < html.index("<table", html.index("<svg"))
    assert f'<a class="num" href="#ev-{Q1}" title="n · team=t1">1.20</a>' in html
    for qid in (Q1, Q2, Q3):
        assert f'<a id="ev-{qid}"></a>' in html
    assert '<a href="#rec-1">rec-1</a>' in html
    assert "Used by: portfolio: base</p>" in html
    assert "Budget $1.2M" in html
    assert 'href="#ev-None"' not in html


def test_ut09_92_org_review_slot_three() -> None:
    """UT09-92 the org review renders the org table and the z-score chart, not funding."""
    html = _full("org_review")
    assert "<title>z-score by metric</title>" in html
    assert "Priority by candidate" not in html
    assert "composite" in html


def test_ut09_92_watermark_only_when_not_publishable() -> None:
    """UT09-92 'Not for decision' watermark iff r.publishable is false."""
    draft = _full(publishable=False)
    assert re.search(r'<body class="[^"]*\bnot-for-decision\b', draft)
    assert "Not for decision" in draft
    final = _full(publishable=True)
    assert "not-for-decision" not in final.split("<body", 1)[1]
    assert "Not for decision" not in final


def test_ut09_92_model_text_and_samples_escaped() -> None:
    """UT09-92 TH09-01/TH09-23: model text and sample cells are escaped; no |safe."""
    html = _full()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<td>&lt;b&gt;t1&lt;/b&gt;</td>" in html
    assert "<b>t1</b>" not in html
    assert "a &lt; $x" in html
    assert "Sample not stored for this build" in html
    for path in _DIR.rglob("*.html.j2"):
        assert not re.search(r"\|\s*safe\b", path.read_text(encoding="utf-8")), path.name


def test_ut09_92_plain_string_text_is_escaped() -> None:
    """UT09-92 a text block that is not Markup is escaped, never trusted."""
    r = _context(_data(), _evidence())
    r["sections"]["method"] = [TextBlock("m", EVIL)]  # type: ignore[arg-type]
    html = _render("funding_review.html.j2", r)
    assert EVIL not in html
    assert "<script" not in html


def test_ut09_92_empty_states() -> None:
    """UT09-92 empty slots render their empty-state text."""
    data = _data()
    r = _context(data, [])
    r.update(cards=[], portfolio_blocks=[], scorecards=[], levers=[],
             retro=data.retro._replace(blocks=[], items=[], verdict_counts={},
                                       empty_text="No measured outcomes yet."))  # fmt: skip
    html = _render("funding_review.html.j2", r)
    assert "No measured outcomes yet." in html
    for text in ("No evidence was cited.", "No funding recommendations in this run.",
                 "No portfolio scenarios for this build.", "No scorecards for this run.",
                 "No action levers for this build."):  # fmt: skip
        assert text in html


def _css_block(css: str, start: str) -> str:
    at = css.index(start)
    return css[at : css.index("}", at)]


def test_ut09_92_chart_colours_defined_light_and_dark() -> None:
    """UT09-92 every var(--c-*) of charts.py and the CSS is defined for light and dark."""
    source = Path(charts.__file__).read_text(encoding="utf-8")
    base = (_DIR / "base.html.j2").read_text(encoding="utf-8")
    used = set(re.findall(r"var\((--c-[a-z-]+)\)", source + base))
    assert {"--c-bar", "--c-accent", "--c-muted", "--c-axis"} <= used
    light = _css_block(base, ":root {")
    dark = _css_block(base, "@media (prefers-color-scheme: dark) {\n  :root {")
    for name in sorted(used):
        assert f"{name}:" in light, name
        assert f"{name}:" in dark, name
    assert "details" in base.split("@media print", 1)[1]
