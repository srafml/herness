"""Tests for the Markdown report templates (impl 09 U09-28 Markdown part, T09-10; UT09-92).

``render_run`` and ``build_environment`` arrive with T09-11, so this module builds a test-local
environment with U09-23's postconditions verbatim and wraps ``jinja2.TemplateError`` into
``SchemaViolation("report template error: <template name>")`` as U09-24 does. The context ``r``
carries every ``ReportData`` field plus ``evidence`` (``EvidenceEntry`` list) and ``manifest``.
"""

from __future__ import annotations

import dataclasses
import re
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import jinja2
import pytest

from herness.core.errors import SchemaViolation
from herness.core.types import Coverage
from herness.reports._data import (
    Card,
    Caveats,
    Header,
    LeverRow,
    MethodInfo,
    ReportBanner,
    ReportData,
    RetroItem,
    Retrospective,
    RunAppendix,
)
from herness.reports._data_slots import Cell, PortfolioBlock, Scorecard, Table, TextBlock
from herness.reports._evidence import EvidenceEntry
from herness.reports._markup import Segment, md_escape, segments_to_md

pytestmark = pytest.mark.unit

Q1 = "q_0123456789abcdef"
Q2 = "q_fedcba9876543210"
Q3 = "q_00000000000000aa"
RUN_ID = "run_01J9ZQ4Y8M6V3K2N1P0R5T7W9X"
KINDS = ("funding_review.md.j2", "org_review.md.j2")
HOSTILE = "<script>alert(1)</script> ![img](http://x) a|b [x](http://evil) `tick`"
_ANCHOR_RE = re.compile(r'<a id="ev-q_[0-9a-f]{16}"></a>')
_T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _environment() -> jinja2.Environment:
    """U09-23 postconditions verbatim (T09-11 replaces this with ``build_environment``)."""
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
    env.globals["REPORT_CSP"] = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"
    env.globals["TEMPLATE_VERSION"] = "1"
    return env


def _render(name: str, r: object) -> str:
    """Render ``name``; any template error becomes SchemaViolation naming it (as U09-24)."""
    try:
        return _environment().get_template(name).render(r=r)
    except jinja2.TemplateError as exc:
        msg = f"report template error: {name}"
        raise SchemaViolation(msg) from exc


def _text(text: str, qid: str | None = None) -> list[Segment]:
    segs = [Segment("text", text, None, "")]
    if qid is not None:
        segs.append(Segment("number", "1.2k", qid, "n · single row"))
    return segs


def _cell(text: str, qid: str | None = None) -> Cell:
    return Cell(text, qid, "t")


def _table(columns: tuple[str, ...], marked: bool = False) -> Table:
    row = [
        _cell(HOSTILE if i == 0 else f"v{i}", Q1 if i == 1 else None) for i in range(len(columns))
    ]
    return Table(columns, [row], [marked])


def _data(*, publishable: bool = True, kind: str = "funding_review") -> ReportData:
    cov = Coverage(planned_tasks=3, done_tasks=2, dead_tasks=1, must_cover_total=2,
                   must_cover_done=1, verified_findings=4, rejected_findings=1,
                   publishable=publishable)  # fmt: skip
    header = Header(_text("Funding | review " + HOSTILE), RUN_ID, kind, "b1", _T0, "standard",
                    "local", _T0, cov, True, 5, 0)  # fmt: skip
    block = TextBlock("sections[0].paragraphs[0]", [
        *_text(HOSTILE + " rose to ", Q1), Segment("uncited", "42 things", None, ""),
        Segment("number", "n/a", None, "x")])  # fmt: skip
    sections = {sid: [block] for sid in ("executive_summary", "recommendations", "portfolio",
                                         "org_scorecards", "actions", "retrospective",
                                         "risks_and_caveats", "method")}  # fmt: skip
    card = Card(
        1,
        "fund",
        "epic",
        "epic-1",
        _text("Fund " + HOSTILE),
        _text("Saves ", Q2),
        (("priority", _cell("1.20", Q1)), ("confidence", _cell("high"))),
        ("fnd_1",),
    )
    retro_item = RetroItem("rec_1", "epic-9", _text("Prior ", Q3), "accept",
                           _table(("measurement", "metric", "baseline")))  # fmt: skip
    return ReportData(
        header=header, publishable=publishable,
        banners=[ReportBanner("unconfirmed_weights", "Weights `x` | placeholders.", ("w.a",)),
                 ReportBanner("partial_run", "Partial.", ())],
        sections=sections, section_titles={"executive_summary": "Summary " + HOSTILE},
        cards=[card],
        funding_table=_table(("rank", "candidate_id", "title"), marked=True)
        if kind == "funding_review" else None,
        org_table=None if kind == "funding_review" else _table(("entity_id", "composite")),
        portfolio_blocks=[PortfolioBlock("base", _cell("$1.0M"), "optimal", True,
                                         _table(("order_rank", "candidate_id")), "portfolio-0")],
        scorecards=[Scorecard("team", "t1", _table(("metric", "value")), ["spark-0-0"])],
        levers=[LeverRow("team", "t1", "mttr", "reduce", _cell("$10k", Q1), HOSTILE, True,
                         ("rec-1",))],
        retro=Retrospective([block], [retro_item], {"improved": 1}, None),
        caveats=Caveats(_table(("check_name", "severity")), "3.5", ["w.a"],
                        [("analyst", "obj|x", "boom")], ["fnd_2"], [("sections[0]", "bad")],
                        {"f1": ["a", "b"]}, [TextBlock("caveats[0]", _text("Ninety days"))]),
        method=MethodInfo("standard", "local", True, 5, 0, {"writer": 2}),
        run_appendix=RunAppendix([(1, "candidate", "epic-1")], [("writer", "done", 2)], 10, 20,
                                 "$0.10", "cfg1"),
        charts={"funding_priority": "<svg></svg>"}, numbers_total=3, number_query_ids=[Q1, Q2],
    )  # fmt: skip


def _entry(qid: str = Q1, sql: str = "SELECT 1", *, found: bool = True,
           sample: list[dict[str, str]] | None = None) -> EvidenceEntry:  # fmt: skip
    rows = [{"a": HOSTILE, "b": "2"}] if sample is None else sample
    return EvidenceEntry(qid, sql if found else "", {"n": 5, "who": "<x>|y"} if found else {},
                         3 if found else None, "2026-09-26T12:00:00.000000Z" if found else None,
                         "b1", rows if found else None, list(rows[0]) if rows else [],
                         ["rec-1", "executive_summary"], found)  # fmt: skip


def _context(data: ReportData | None = None, evidence: list[EvidenceEntry] | None = None,
             drop: str = "") -> SimpleNamespace:  # fmt: skip
    """``r``: every ReportData field, ``evidence`` and ``manifest``; ``drop`` removes fields."""
    fields = {f.name: getattr(data or _data(), f.name) for f in dataclasses.fields(ReportData)}
    fields["evidence"] = [_entry(), _entry(Q2, found=False), _entry(Q3, sample=[])] \
        if evidence is None else evidence  # fmt: skip
    fields["manifest"] = SimpleNamespace(run_id=RUN_ID, template_version="1")
    return SimpleNamespace(**{k: v for k, v in fields.items() if k != drop})


def _fences(md: str) -> list[str]:
    return re.findall(r"^(`{3,})sql$", md, flags=re.MULTILINE)


@pytest.mark.parametrize("name", KINDS)
@pytest.mark.parametrize("field", ["cards", "evidence", "method", "publishable"])
def test_ut09_92_missing_variable_is_schema_violation(name: str, field: str) -> None:
    """UT09-92 a context missing one variable raises SchemaViolation naming the template."""
    with pytest.raises(SchemaViolation, match=re.escape(f"report template error: {name}")):
        _render(name, _context(drop=field))


@pytest.mark.parametrize("name", KINDS)
def test_ut09_92_full_context_renders(name: str) -> None:
    """UT09-92 a full realistic context renders every slot without UndefinedError."""
    kind = name.removesuffix(".md.j2")
    md = _render(name, _context(_data(kind=kind)))
    headings = [line for line in md.splitlines() if line.startswith("#")]
    assert all(re.match(r"#{1,3} ", h) for h in headings)
    titles = ["Executive summary", "Portfolio", "Org scorecards", "Recommended actions",
              "Did last quarter's recommendations work?", "Data quality and caveats", "Method",
              "Evidence appendix", "Run appendix"]  # fmt: skip
    positions = [md.index(f"## {t}") for t in titles[1:]]
    assert positions == sorted(positions)
    assert "## Summary " in md  # the draft's own section title wins over the default
    assert f"[1.2k](#ev-{Q1})" in md
    assert "Sample not stored for this build" not in md.split(f'id="ev-{Q3}"')[1]
    assert "Evidence not found for this build." in md
    assert "<svg" not in md
    assert md.endswith("\n")


def test_ut09_92_recommendation_tables_by_kind() -> None:
    """UT09-92 funding renders the funding table with the unconfirmed mark; org the org table."""
    funding = _render("funding_review.md.j2", _context(_data()))
    org = _render("org_review.md.j2", _context(_data(kind="org_review")))
    assert "| rank | candidate\\_id | title |" in funding
    assert "_(unconfirmed)_" in funding
    assert "| entity\\_id | composite |" in org
    assert "Funding: top-N recommendations" in funding
    assert "Org: top-N orgs/teams to improve" in org


@pytest.mark.parametrize("name", KINDS)
def test_ut09_92_watermark_leading_line(name: str) -> None:
    """UT09-92 ``> **Not for decision**`` leads the file only when r.publishable is false."""
    kind = name.removesuffix(".md.j2")
    hidden = _render(name, _context(_data(publishable=False, kind=kind)))
    shown = _render(name, _context(_data(publishable=True, kind=kind)))
    assert hidden.splitlines()[0] == "> **Not for decision**"
    assert "Not for decision" not in shown


@pytest.mark.parametrize(
    ("sql", "fence"),
    [
        ("SELECT 1", "```"),
        ("SELECT `a` FROM t", "```"),
        ("SELECT ```a``` FROM t", "````"),
        ("SELECT 1 -- `````x`` ` ", "``````"),
        ("```", "````"),
        ("SELECT 1 -- x````", "`````"),
        ("SELECT 1\n````\nFROM t", "`````"),
    ],
)
def test_ut09_92_sql_fence_longer_than_backtick_runs(sql: str, fence: str) -> None:
    """UT09-92 the SQL fence is max(3, longest backtick run + 1) backticks and closes once."""
    md = _render("funding_review.md.j2", _context(evidence=[_entry(sql=sql)]))
    assert _fences(md) == [fence]
    body = md.split(f"{fence}sql\n", 1)[1]
    assert body.startswith(f"{sql}\n{fence}\n")


@pytest.mark.parametrize("name", KINDS)
def test_ut09_92_anchor_precedes_each_evidence_entry(name: str) -> None:
    """UT09-92 each evidence entry starts with its ``<a id="ev-q_…"></a>`` anchor, in order."""
    kind = name.removesuffix(".md.j2")
    md = _render(name, _context(_data(kind=kind)))
    anchors = _ANCHOR_RE.findall(md)
    assert anchors == [f'<a id="ev-{q}"></a>' for q in (Q1, Q2, Q3)]
    for qid in (Q1, Q2, Q3):
        after = md.split(f'<a id="ev-{qid}"></a>\n', 1)[1]
        assert after.startswith(f"### {md_escape(qid)}\n")


def test_ut09_92_invalid_query_id_gets_no_anchor() -> None:
    """UT09-92 an id not matching ``q_`` + 16 hex digits emits no raw HTML and no link."""
    bad = 'q_"><script>x</script>'
    data = dataclasses.replace(_data(), cards=[], levers=[])
    md = _render("funding_review.md.j2", _context(data, evidence=[_entry(bad)]))
    assert "<script" not in md
    assert "<a " not in md
    assert '(#ev-q_\\"' not in md


@pytest.mark.parametrize("name", KINDS)
@pytest.mark.parametrize("publishable", [True, False])
def test_ut09_92_no_raw_html_besides_anchors(name: str, publishable: bool) -> None:
    """UT09-92 hostile model text: no raw HTML, image or foreign link; cell pipes escaped."""
    kind = name.removesuffix(".md.j2")
    md = _render(name, _context(_data(publishable=publishable, kind=kind)))
    rest = _ANCHOR_RE.sub("", md)
    assert re.search(r"<[A-Za-z/!?]", rest) is None
    assert "<script" not in md
    assert re.search(r"(?<!\\)!\[", md) is None
    links = re.findall(r"(?<!\\)\]\(([^)]*)\)", md)
    assert links
    assert all(re.fullmatch(r"#ev-q_[0-9a-f]{16}", link) for link in links)
    table_lines = [line for line in md.splitlines() if line.startswith("|")]
    assert any("a\\|b" in line for line in table_lines)
    assert all(line.endswith("|") for line in table_lines)
    assert "&lt;script&gt;" in md


def test_ut09_92_segments_match_segments_to_md() -> None:
    """UT09-92 a text block renders exactly as ``segments_to_md`` (image-free text)."""
    segs: list[Any] = [Segment("text", "a|b <i>*x*", None, ""),
                       Segment("number", "1.2k", Q1, "t"), Segment("number", "n/a", None, ""),
                       Segment("uncited", "9 [x]", None, "")]  # fmt: skip
    block = TextBlock("sections[0].paragraphs[0]", segs)
    data = dataclasses.replace(_data(), sections={**_data().sections, "method": [block]})
    md = _render("funding_review.md.j2", _context(data))
    assert f"\n{segments_to_md(segs)}\n" in md


@pytest.mark.parametrize("name", KINDS)
def test_ut09_92_empty_states(name: str) -> None:
    """UT09-92 empty slots render their empty-state lines, still without UndefinedError."""
    base = _data(kind=name.removesuffix(".md.j2"))
    empty_table = Table(("a",), [], [])
    data = dataclasses.replace(
        base, header=base.header._replace(data_as_of=None), banners=[], cards=[
            base.cards[0]._replace(extras=(), finding_ids=())],
        funding_table=empty_table if base.funding_table else None,
        org_table=empty_table if base.org_table else None,
        portfolio_blocks=[base.portfolio_blocks[0]._replace(table=empty_table, custom=False)],
        levers=[], retro=Retrospective([], [], {}, "No measured outcomes yet."),
        caveats=Caveats(empty_table, None, [], [], [], [], {}, []),
        method=base.method._replace(role_calls={}),
    )  # fmt: skip
    md = _render(name, _context(data, evidence=[]))
    for line in ("| Data as of | n/a |", "_No candidates selected._", "_No action levers._",
                 "_No measured outcomes yet._", "_No evidence cited._"):  # fmt: skip
        assert line in md
    assert "Findings:" not in md
    assert "| Measure | Value |" not in md
    assert "### Failed data quality checks" not in md
    assert "_No scored candidates._" in md or "_No scored entities._" in md


def test_ut09_92_evidence_without_row_count_or_time() -> None:
    """UT09-92 a found entry with no row count, time, params or sample shows n/a and the note."""
    entry = dataclasses.replace(_entry(), row_count=None, executed_at=None, params={},
                                result_sample=None, sample_columns=[])  # fmt: skip
    md = _render("funding_review.md.j2", _context(evidence=[entry]))
    assert "- Row count: n/a\n- Executed at: n/a\n" in md
    assert "_Sample not stored for this build_" in md
    assert "| parameter | value |" not in md


def test_ut09_92_fence_scan_stops_at_first_missing_run() -> None:
    """UT09-92 hostile 20,000-char alternating-backtick SQL renders fast with a 3-tick fence."""
    sql = "`a" * 10_000
    start = time.perf_counter()
    md = _render("funding_review.md.j2", _context(evidence=[_entry(sql=sql)]))
    assert time.perf_counter() - start < 1.0
    assert _fences(md) == ["```"]
    assert f"```sql\n{sql}\n```\n" in md


FOLD = "one\ntwo\r\nthree\rfour"


def test_ut09_92_newlines_folded_in_headings_and_cells() -> None:
    """UT09-92 LF, CRLF and CR in headings, cells and inline segments fold to spaces."""
    base = _data()
    fold_table = Table(("col",), [[_cell(FOLD)], [_cell(FOLD, Q1)]], [False, False])
    data = dataclasses.replace(
        base, header=base.header._replace(title=_text(FOLD)),
        section_titles={"executive_summary": FOLD},
        cards=[base.cards[0]._replace(headline=_text(FOLD, Q1))],
        funding_table=fold_table,
    )  # fmt: skip
    md = _render("funding_review.md.j2", _context(data))
    assert "\r" not in md
    folded = "one two  three four"
    lines = md.splitlines()
    assert f"# {folded}" in lines
    assert f"## {folded}" in lines
    assert f"### 1. {folded}[1.2k](#ev-{Q1})" in lines
    assert f"| {folded} |" in lines
    assert f"| [{folded}](#ev-{Q1}) |" in lines
    assert not any(line.startswith(("two", "three", "four")) for line in lines)
    assert all(line.endswith("|") for line in lines if line.startswith("|"))
