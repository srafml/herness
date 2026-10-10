"""Integration tests of ``render_run`` (impl 09 U09-24, flow F09-01; T09-11).

Every test renders through the real ops store, the T11-17 ``tiny_build`` warehouse (copied
under a fixed build id with a fixed score layer, see ``tests.support.report_env``) and the
committed drafts. Goldens live in ``tests/fixtures/reports/golden/`` and change only with
``pytest --snapshot-update`` (``tests.support.snapshot``).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import types
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final

import duckdb
import pytest
from tests.support.fake_clock import FakeClock
from tests.support.harness_fakes import FakeOps
from tests.support.report_env import (
    BUILD_ID,
    F1,
    FO_RUN,
    FUND_RUN,
    GOLDEN,
    NOW,
    OPS_EVIDENCE,
    ORG_RUN,
    ReportEnv,
    fixture_draft,
    number,
)  # fmt: skip
from tests.support.snapshot import Snapshot

from herness.core import config
from herness.core.errors import NotFound, ReportContractError
from herness.core.numbers import find_uncited
from herness.core.types import ReportDraft, ReportManifest, VerifiableItem
from herness.harness.llm.settings import SqlSettings, VerifierSettings
from herness.harness.verifier import Verifier
from herness.harness.warehouse import WarehousePool
from herness.reports import contract, render

pytestmark = pytest.mark.integration

KINDS: Final = ((FUND_RUN, "funding_review"), (ORG_RUN, "org_review"))
E1, E2, E3, E4, E5 = (e.qid for e in OPS_EVIDENCE)
EVIL: Final = "<script>alert(1)</script> <img src=x onerror=a>"


class _Links(HTMLParser):
    """``a.num`` hrefs, every ``id`` and the text of each ``p.text`` without its anchors."""

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.ids: set[str] = set()
        self.texts: list[str] = []
        self._depth = 0
        self._in_num = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        got = dict(attrs)
        if got.get("id"):
            self.ids.add(str(got["id"]))
        if tag == "a" and got.get("class") == "num":
            self.hrefs.append(str(got.get("href")))
            self._in_num = True
        if tag == "p" and got.get("class") == "text":
            self._depth += 1
            self.texts.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._in_num = False
        if tag == "p" and self._depth:
            self._depth -= 1

    def handle_data(self, data: str) -> None:
        if self._depth and not self._in_num:
            self.texts[-1] += data


def _draft(name: str, **changes: Any) -> dict[str, Any]:
    return fixture_draft(name) | changes


def _patterns() -> tuple[re.Pattern[str], ...]:
    return config.get_config().app.reports.compiled_numeral_patterns


# --- IT09-01, IT09-18, IT09-02 ------------------------------------------------------------------


@pytest.mark.parametrize(("run_id", "kind"), KINDS, ids=["funding", "org"])
def test_it09_01_goldens_byte_identical(report_env: ReportEnv, snapshot: Snapshot, run_id: str,
                                        kind: str) -> None:  # fmt: skip
    """IT09-01 HTML and MD equal the committed goldens byte for byte (FakeClock ``now``)."""
    with FakeClock(NOW):
        manifest = render.render_run(run_id, ["html", "md"])
    assert manifest.rendered_at == NOW
    html, md = report_env.out(run_id, "report.html"), report_env.out(run_id, "report.md")
    snapshot.check(GOLDEN / f"{kind}.html", html)
    snapshot.check(GOLDEN / f"{kind}.md", md)
    assert len(html) < 5 * 1024 * 1024
    assert manifest.warnings == []
    assert str(report_env.data_root) not in html.decode() + md.decode()


def test_it09_18_render_twice_identical(report_env: ReportEnv) -> None:
    """IT09-18 the same draft, build and ``now`` give identical bytes and manifest."""
    first = report_env.render(FUND_RUN)
    files = {n: report_env.out(FUND_RUN, n) for n in ("report.html", "report.md", "manifest.json")}
    second = report_env.render(FUND_RUN)
    assert first == second
    for name, data in files.items():
        assert report_env.out(FUND_RUN, name) == data


@pytest.mark.parametrize(("run_id", "kind"), KINDS, ids=["funding", "org"])
def test_it09_02_every_number_links_to_evidence(report_env: ReportEnv, run_id: str,
                                                kind: str) -> None:  # fmt: skip
    """IT09-02 every ``a.num`` resolves to an ``ev-`` anchor; model text has no bare numerals."""
    manifest = report_env.render(run_id)
    parser = _Links()
    parser.feed(report_env.out(run_id, "report.html").decode())
    assert parser.hrefs
    for href in parser.hrefs:
        assert href.startswith("#ev-")
        assert href[1:] in parser.ids
    for text in parser.texts:
        assert not find_uncited(text, _patterns()), text
    assert manifest.numbers_linked == manifest.numbers_total > 0
    assert manifest.kind == kind


def test_it09_02_manifest_matches_files(report_env: ReportEnv) -> None:
    """IT09-02 manifest.json equals the return value and hashes every artefact in out_dir."""
    manifest = report_env.render(FUND_RUN)
    out = report_env.reports_root / FUND_RUN
    assert ReportManifest.model_validate_json(report_env.out(FUND_RUN, "manifest.json")) == manifest
    names = {p.name for p in out.iterdir()} - {"manifest.json", "draft.json"}
    assert names == set(manifest.files) == {"report.html", "report.md"}
    for name, digest in manifest.files.items():
        assert hashlib.sha256(report_env.out(FUND_RUN, name)).hexdigest() == digest
    assert not list(out.glob("*.tmp"))
    assert manifest.evidence_entries > 0
    assert manifest.build_id == BUILD_ID


# --- IT09-03 ------------------------------------------------------------------------------------


SHARED: Final = (
    "Incidents rose by 42 percent", "Opened in 2025 and closed 2026-01-03", "Q3 2026 was calm",
    "INC0012345 and CHG77 and PRB9", "Ticket ABC-123 and v2", "no digits at all",
    "3.5 hours and 1,200 dollars", "The [[n1]] marker only",
)  # fmt: skip


def test_it09_03_renderer_and_verifier_agree(report_env: ReportEnv, tmp_path: Path) -> None:
    """IT09-03 U09-09 and the Verifier make identical uncited decisions (R-16)."""
    patterns = config.get_config().app.reports.allowed_numeral_patterns
    pool = WarehousePool(tmp_path, SqlSettings())
    verifier = Verifier(FakeOps({}), pool, VerifierSettings(), None,
                        allowed_numeral_patterns=patterns, sql=SqlSettings())  # fmt: skip
    base = fixture_draft("funding")
    try:
        for text in SHARED:
            refs = [number("n1", E1)] if "[[n1]]" in text else []
            item = VerifiableItem(where="x", text=text, numbers=refs, finding_ids=[])
            spans = verifier.verify_numbers(item, BUILD_ID).items[0].uncited
            para = {"text": text, "numbers": refs, "finding_ids": [F1] if refs else []}
            draft = ReportDraft.model_validate(base | {"title": "T", "recommendations": [],
                "caveats": [], "prior_outcomes_commentary": None,
                "sections": [{"id": "method", "title": "M", "paragraphs": [para]}]})  # fmt: skip
            hits = contract.scan_draft_uncited(draft, _patterns())
            assert [(h.text, h.start, h.end) for h in hits] == [
                (s.text, s.start, s.end) for s in spans
            ], text
    finally:
        pool.close_all()


# --- IT09-05 ------------------------------------------------------------------------------------


def _bad_drafts() -> list[tuple[str, dict[str, Any], str]]:
    base = fixture_draft("funding")
    marker = copy.deepcopy(base)
    marker["sections"][0]["paragraphs"][1]["text"] = "Failed changes stayed at [[n9]]."
    unknown_q = copy.deepcopy(base)
    unknown_q["recommendations"][1]["numbers"][0]["query_id"] = "q_ffffffffffffffff"
    unknown_rec = copy.deepcopy(base)
    unknown_rec["recommendations"][0]["rec_id"] = "rec_01JA0000000000000000000099"
    unverified = copy.deepcopy(base)
    unverified["sections"][0]["paragraphs"][1]["finding_ids"] = ["fnd_01JA0000000000000000000099"]
    return [
        ("marker", marker, "sections[0].paragraphs[1]: marker [[n9]] has no NumberRef"),
        ("query", unknown_q, "recommendations[1].headline: unknown query_id q_ffffffffffffffff"),
        ("rec", unknown_rec, "recommendations[0].rec_id: unknown rec_id"),
        (
            "finding",
            unverified,
            "sections[0].paragraphs[1]: finding fnd_01JA0000000000000000000099",
        ),
        ("schema", base | {"schema_version": "2"}, "schema_version"),
    ]


@pytest.mark.parametrize(("case", "where"), [(c, w) for c, _, w in _bad_drafts()])
def test_it09_05_contract_failures_name_field(report_env: ReportEnv, case: str,
                                              where: str) -> None:  # fmt: skip
    """IT09-05 each broken draft fails with ReportContractError naming the field path."""
    draft = next(d for c, d, _ in _bad_drafts() if c == case)
    report_env.write_draft(draft)
    with pytest.raises(ReportContractError) as info:
        report_env.render(FUND_RUN)
    assert where in info.value.details["where"]
    assert not (report_env.reports_root / FUND_RUN / "manifest.json").exists()


# --- IT09-06, IT09-07, IT09-08, IT09-27 -----------------------------------------------------------


def test_it09_06_build_without_result_sample(report_env: ReportEnv) -> None:
    """IT09-06 a v1 ``meta.evidence`` (no result_sample): "Sample not stored"; render succeeds."""
    with duckdb.connect(str(report_env.warehouse)) as con:
        con.execute("ALTER TABLE meta.evidence DROP COLUMN result_sample")
    manifest = report_env.render(FUND_RUN)
    html = report_env.out(FUND_RUN, "report.html").decode()
    entry = html.split('<a id="ev-q_00000000000000f1"></a>', 1)[1].split("</article>", 1)[0]
    assert "Sample not stored for this build" in entry
    md = report_env.out(FUND_RUN, "report.md").decode()
    assert "_Sample not stored for this build_" in md.split('id="ev-q_00000000000000f1"', 1)[1]
    assert manifest.numbers_linked == manifest.numbers_total


def test_it09_07_partial_unconfirmed_not_publishable(report_env: ReportEnv) -> None:
    """IT09-07 (render part) banners and watermark for a partial run with two dead tasks."""
    dead = [{"task_id": f"tsk_{n}", "role": "analyst", "objective": "dig", "last_error": "boom"}
            for n in (8, 9)]  # fmt: skip
    report_env.write_draft(_draft("org", dead_tasks=dead))
    manifest = report_env.render(ORG_RUN)
    html = report_env.out(ORG_RUN, "report.html").decode()
    md = report_env.out(ORG_RUN, "report.md").decode()
    assert manifest.publishable is False
    for code in ("unconfirmed_weights", "partial_run"):
        assert f"banner-{code}" in html
    assert 'class="watermark"' in html
    assert "not-for-decision" in html
    assert md.startswith("> **Not for decision**\n")
    assert "Dollar weights are placeholders" in md
    assert "This run is partial" in md
    assert md.count("| analyst | dig | boom |") == 2


def test_it09_08_portfolio_custom_first(report_env: ReportEnv) -> None:
    """IT09-08 the custom scenario leads the portfolio section; its numbers link its query ids."""
    custom = {"scenario": {"name": "custom budget"}, "budget_usd": "900",
              "solver_status": "OPTIMAL", "rows": [
                  {"candidate_id": "epic-2", "selected": True, "order_rank": 1,
                   "expected_impact_usd": "700", "query_ids": [E2]}]}  # fmt: skip
    report_env.write_draft(_draft("funding", portfolio_custom=[custom]))
    report_env.render(FUND_RUN)
    html = report_env.out(FUND_RUN, "report.html").decode()
    section = html.split('<section id="portfolio">', 1)[1].split("</section>", 1)[0]
    assert section.index('<span class="tag">custom</span>') < section.index("Scenario base")
    custom_block = section.split("Scenario base", 1)[0]
    assert f'href="#ev-{E2}"' in custom_block
    assert f'<a id="ev-{E2}"></a>' in html


def test_it09_27_findings_only(report_env: ReportEnv) -> None:
    """IT09-27 findings_only: banner and the run's three verified findings, all linked (R-49)."""
    draft = _draft("funding", run_id=FO_RUN, mode="findings_only", recommendations=[],
                   sections=[{"id": "executive_summary", "title": "Summary", "paragraphs": []}],
                   prior_outcomes_commentary=None, query_ids=[])  # fmt: skip
    report_env.write_draft(draft)
    manifest = report_env.render(FO_RUN)
    html = report_env.out(FO_RUN, "report.html").decode()
    md = report_env.out(FO_RUN, "report.md").decode()
    for text in (html, md):
        assert "The report writer did not finish" in text
        assert "Verified findings" in text
    for claim in ("Incidents reached", "Failed changes numbered", "High priority incidents"):
        assert claim in html
        assert claim in md
    for qid in (E1, E4, E5):
        assert f'href="#ev-{qid}"' in html
    assert "banner-findings_only" in html
    assert manifest.numbers_linked == manifest.numbers_total


# --- security: ST09-01, ST09-03, ST09-16, ST09-17, ST09-18, ST09-26 -------------------------


def _hostile_draft(text: str) -> dict[str, Any]:
    draft = fixture_draft("funding")
    draft["title"] = text
    draft["sections"][1]["paragraphs"][0]["text"] = text
    draft["recommendations"][1]["headline"] = text[:120]
    draft["caveats"] = [text]
    return draft


def test_st09_01_draft_html_escaped(report_env: ReportEnv) -> None:
    """ST09-01 script and img tags in draft text are shown escaped; no element is created."""
    report_env.write_draft(_hostile_draft(EVIL))
    report_env.render(FUND_RUN, strict=False)  # "alert(1)" holds an uncited numeral
    html = report_env.out(FUND_RUN, "report.html").decode()
    md = report_env.out(FUND_RUN, "report.md").decode()
    assert "<script" not in html.lower()
    assert "<img" not in html.lower()
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &lt;img src=x onerror=a&gt;" in html
    assert "<script" not in md
    assert "<img" not in md


@pytest.mark.parametrize(("run_id", "kind"), [(FUND_RUN, "funding_review"),
                                              (ORG_RUN, "org_review")])  # fmt: skip
def test_st09_03_static_scan(report_env: ReportEnv, run_id: str, kind: str) -> None:
    """ST09-03 no script, no URL, no url(); the CSP meta is the first element of head."""
    report_env.render(run_id)
    for html in (report_env.out(run_id, "report.html").decode(),
                 (GOLDEN / f"{kind}.html").read_text(encoding="utf-8")):  # fmt: skip
        assert "<script" not in html.lower()
        assert not re.search(r"https?://|//", html)
        assert "url(" not in html
        head = html.split("<head>", 1)[1].lstrip()
        assert head.startswith(f'<meta http-equiv="Content-Security-Policy" content="'
                               f'{render.REPORT_CSP.replace("'", "&#39;")}">')  # fmt: skip


def test_st09_16_uncited_number_strict_refused(report_env: ReportEnv) -> None:
    """ST09-16 an uncited number under strict mode refuses the render; nothing is written."""
    draft = fixture_draft("funding")
    draft["sections"][1]["paragraphs"][0]["text"] = "Incidents rose by 42 percent."
    report_env.write_draft(draft)
    with pytest.raises(
        ReportContractError, match="1 numbers in the draft have no evidence"
    ) as info:
        report_env.render(FUND_RUN)
    assert info.value.details["code"] == "uncited"
    assert info.value.details["where"] == "sections[1].paragraphs[0]"
    assert not list((report_env.reports_root / FUND_RUN).glob("report.*"))
    manifest = report_env.render(FUND_RUN, strict=False)
    assert manifest.uncited == [{"where": "sections[1].paragraphs[0]", "text": "42"}]
    assert '<mark class="uncited">42</mark>' in report_env.out(FUND_RUN, "report.html").decode()


def test_st09_18_sample_cell_escaped(report_env: ReportEnv) -> None:
    """ST09-18 the evidence sample cell ``<b onmouseover=x>`` is escaped in HTML and MD."""
    report_env.render(ORG_RUN)
    html = report_env.out(ORG_RUN, "report.html").decode()
    md = report_env.out(ORG_RUN, "report.md").decode()
    assert "<b onmouseover" not in html
    assert "<b onmouseover" not in md
    assert "<td>&lt;b onmouseover=x&gt;</td>" in html
    assert "| &lt;b onmouseover=x&gt; |" in md


def test_st09_26_template_syntax_literal(report_env: ReportEnv) -> None:
    """ST09-26 ``{{ 7*7 }}`` and ``{% for %}`` in draft text render literally."""
    report_env.write_draft(_hostile_draft("Total {{ 7*7 }} and {% for x in y %}"))
    report_env.render(FUND_RUN, strict=False)  # the sevens are uncited numerals
    html = report_env.out(FUND_RUN, "report.html").decode()
    md = report_env.out(FUND_RUN, "report.md").decode()
    assert "Total {{ 7*7 }} and {% for x in y %}" in html
    seven = "_[uncited]_ 7"
    assert f"Total \\{{\\{{ {seven}\\*{seven} \\}}\\}} and \\{{% for x in y %\\}}" in md
    assert "Total 49" not in html + md


@pytest.mark.parametrize("bad", ["../../x", "run_../../etc", "", "RUN_01JA0000000000000000000001"])
def test_st09_17_invalid_run_id_refused_first(report_env: ReportEnv, bad: str,
                                              monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """ST09-17 (render part) an invalid run_id is refused before any store or file access."""

    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("touched")  # noqa: EM101 - test guard

    monkeypatch.setattr(render.ops, "get_run", boom)
    monkeypatch.setattr(render.contract, "load_draft", boom)
    monkeypatch.setattr(render, "_prepare_out_dir", boom)
    with pytest.raises(ReportContractError, match="invalid run_id") as info:
        report_env.render(bad)
    assert info.value.details["code"] == "invalid_run_id"


def test_st09_17_link_out_dir_refused(report_env: ReportEnv, tmp_path: Path) -> None:
    """ST09-17 (render part) a symlink or junction ``out_dir`` is refused; nothing written."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "link"
    winapi: Any = sys.modules.get("_winapi")
    if winapi is not None and hasattr(winapi, "CreateJunction"):
        winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ReportContractError, match="must not be a symlink"):
        report_env.render(FUND_RUN, out_dir=link)
    assert list(target.iterdir()) == []


def test_it09_18_out_dir_created(report_env: ReportEnv, tmp_path: Path) -> None:
    """IT09-18 (out_dir) a given ``out_dir`` is created with parents and holds the same bytes."""
    out = tmp_path / "a" / "b"
    manifest = report_env.render(FUND_RUN, out_dir=out)
    default = report_env.render(FUND_RUN)
    assert manifest == default
    assert (out / "report.html").read_bytes() == report_env.out(FUND_RUN, "report.html")


# --- PDF through a fake engine: ST09-22 (integration), IT09-19 ------------------------------------


def _fake_weasyprint(seen: dict[str, Any]) -> types.ModuleType:
    module = types.ModuleType("weasyprint")

    class HTML:
        def __init__(self, *, string: str, base_url: object, url_fetcher: Any) -> None:
            seen.update(html=string, base_url=base_url, fetcher=url_fetcher)

        def write_pdf(self) -> bytes:
            return b"%PDF-1.7 fake\n" + hashlib.sha256(seen["html"].encode()).digest()

    module.HTML = HTML  # type: ignore[attr-defined]
    module.default_url_fetcher = lambda url, *a, **k: {"string": b"", "url": url}  # type: ignore[attr-defined]
    return module


def test_it09_19_pdf_with_fake_engine(report_env: ReportEnv,
                                      monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """IT09-19 (fake engine) PDF written and hashed; SVG charts and print CSS reach the engine."""
    seen: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "weasyprint", _fake_weasyprint(seen))
    manifest = report_env.render(FUND_RUN, ["html", "md", "pdf"])
    pdf = report_env.out(FUND_RUN, "report.pdf")
    assert pdf.startswith(b"%PDF")
    assert manifest.files["report.pdf"] == hashlib.sha256(pdf).hexdigest()
    html = seen["html"]
    assert html == report_env.out(FUND_RUN, "report.html").decode()
    assert seen["base_url"] is None
    assert html.count("<svg") >= 2  # funding priority, portfolio and sparkline charts inline
    css_print = html.split("@media print", 1)[1].split("</style>", 1)[0]
    assert "details > .details-body { display: block !important; }" in css_print
    assert "content-visibility: visible" in css_print
    assert "<details open>" in html
    only_pdf = report_env.render(FUND_RUN, ["pdf"])
    assert set(only_pdf.files) == {"report.pdf"}
    assert not (report_env.reports_root / FUND_RUN / "report.html").exists()


def test_it09_19_pdf_real_engine(report_env: ReportEnv) -> None:
    """IT09-19 with WeasyPrint installed: a PDF is produced and the manifest has its hash."""
    pytest.importorskip("weasyprint")
    manifest = report_env.render(FUND_RUN, ["html", "pdf"])
    pdf = report_env.out(FUND_RUN, "report.pdf")
    assert pdf.startswith(b"%PDF")
    assert manifest.files["report.pdf"] == hashlib.sha256(pdf).hexdigest()


def test_it09_02_metrics_recorded(report_env: ReportEnv) -> None:
    """IT09-02 (observability) a render records the three render metrics (R-12)."""
    report_env.render(FUND_RUN, strict=False)
    with pytest.raises(NotFound):
        report_env.render("run_01JA0000000000000000000009")
    rows = render.ops.core.read_all("SELECT name, labels FROM metric_sample ORDER BY name", ())
    names = [row["name"] for row in rows]
    assert "herness_reports_render_seconds" in names
    labels = [json.loads(row["labels"]) for row in rows if row["name"].endswith("renders_total")]
    assert {"result": "ok"} in labels
    assert {"result": "error"} in labels
    uncited = [row for row in rows if row["name"] == "herness_reports_uncited_total"]
    assert uncited == []  # the fixture draft has no uncited numeral


def test_it09_02_html_size_warning(report_env: ReportEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """IT09-02 (manifest) ``report.html exceeds 5 MB`` is warned past the limit, still written."""
    monkeypatch.setattr(render, "_HTML_LIMIT", 1000)
    manifest = report_env.render(FUND_RUN)
    assert manifest.warnings == ["report.html exceeds 5 MB"]
    assert ReportManifest.model_validate_json(report_env.out(FUND_RUN, "manifest.json")) == manifest


def test_it09_02_evidence_not_found_warning(report_env: ReportEnv) -> None:
    """IT09-02 (manifest) a score row citing an id found nowhere: warned, not counted as linked."""
    missing = "q_0000000000000abc"
    with duckdb.connect(str(report_env.warehouse)) as con:
        con.execute("UPDATE score.funding SET query_ids = [$q] WHERE candidate_id = 'epic-2'",
                    {"q": missing})  # fmt: skip
    manifest = report_env.render(FUND_RUN)
    assert manifest.warnings == [f"evidence not found: {missing}"]
    assert manifest.numbers_linked < manifest.numbers_total
    html = report_env.out(FUND_RUN, "report.html").decode()
    assert "Evidence not found for this query in the ops store or the build." in html
