# T09-09 report: HTML templates

Status: DONE_WITH_CONCERNS — final commit 0b1c18a feat(reports): add HTML report templates (T09-09); checkpoint 737600e
Worktree: D:\herness\.claude\worktrees\agent-a16e2ad05f9ea59e7 (base 84384c1)

## Implemented
- herness/reports/templates/base.html.j2 (165/250): DOCTYPE; CSP meta first in head (`{{ REPORT_CSP }}`), charset, viewport, `<title>` = `h.title|striptags` (escaped); one inline `<style>` with CSS variables for light (`:root`) and dark (`@media (prefers-color-scheme: dark) { :root {...} }`), `@media print` expanding `<details>` (`::details-content`, `.details-body`) and hiding summary markers; no script/link/img/url(). Blocks `header`, `banners`, `content`, `evidence_appendix`, `run_appendix`; `not-for-decision` class on body + fixed-position "Not for decision" caption when `not r.publishable`; footer with TEMPLATE_VERSION.
- funding_review.html.j2 (27/200), org_review.html.j2 (24/200): extend base; slots 2-9 in design §5.2 order via macros; slot 3 renders `r.cards`, then chart (`funding_priority` / `org_z`), then its table (design: chart followed by its data table).
- partials/components.html.j2 (214/220): macros text, blocks, num, empty, table(columns, rows, marked=()), chart, card (id rec-N), banner, used_by, evidence_entry (anchor `<a id="ev-q_…"></a>`, SQL in pre/code, params table, build, row count, executed, sample table of escaped cells or "Sample not stored for this build", used-by back-links `#rec-N` or plain text), section (caller-aware), and slot macros portfolio, scorecards (trend sparkline column after z_score), actions, retrospective, caveats, method.
- No `|safe` anywhere (model text and charts arrive as Markup; test enforces it).
- Wheel: hatch `packages = ["herness"]` already ships the .j2 files (verified with `uv build --wheel`: all four templates listed). No pyproject change.

## Context `r` contract (for T09-11 / U09-24)
`r` is a mapping (or namespace) with every ReportData field under its own name, plus two extra names; in the HTML context every `list[Segment]` value becomes `segments_to_html(...)` Markup and every chart string `Markup`:
- ReportData: `r.header` (Header; `.title` → Markup; reads run_id, kind, build_id, data_as_of (datetime|None), depth, profile, rendered_at (aware datetime), coverage.{planned_tasks,done_tasks,dead_tasks,must_cover_total,must_cover_done,verified_findings,rejected_findings}, gate2_passed, gate2_numbers, gate2_failed), `r.publishable`, `r.banners` (ReportBanner code/text/details), `r.sections[sid]` (TextBlock; `.segments` → Markup) for executive_summary, recommendations, portfolio, org_scorecards, actions, risks_and_caveats, method, `r.section_titles` (.get(sid, default)), `r.cards` (Card; `.headline`, `.summary` → Markup; rank, kind, target_type, target_id, extras, finding_ids), `r.funding_table` (funding template) / `r.org_table` (org template), `r.portfolio_blocks`, `r.scorecards`, `r.levers`, `r.retro` (`.blocks` TextBlocks → Markup; `.items[].summary` → Markup; verdict_counts, empty_text), `r.caveats` (`.blocks` → Markup; dq_failed, unmapped_share, unconfirmed_keys, dead_tasks, contested, removed, flags), `r.method`, `r.run_appendix`, `r.charts` (dict key → Markup; keys funding_priority, org_z, portfolio-N, spark-N-M), `r.numbers_total`.
- Not read by templates: `r.number_query_ids` (feeds numbers_linked only).
- Evidence: `r.evidence` = list[EvidenceEntry] from load_evidence_entries.
- Manifest field: `r.numbers_linked` (int; count of number_query_ids whose entry is found; computed before rendering).
- Globals: REPORT_CSP, TEMPLATE_VERSION.
A reference conversion is `_context()` in tests/unit/reports/test_templates_html.py.

## Tests (tests/unit/reports/test_templates_html.py, pytestmark unit, 13 functions all UT09_92)
Test-local environment built verbatim per U09-23 postconditions + wrapper TemplateError → SchemaViolation("report template error: <name>"). Covers: every context key removed one at a time → SchemaViolation naming each template; nested missing attribute; full fixture renders for both kinds; ST09-03-style scan (no <script, no http(s):, no //, no url(, no link/img, CSP meta first in head, one style block); slot order 2-11; num() anchors, rec-N card anchors, ev-q_… anchors, used-by links; org slot 3; watermark iff not publishable; model text and sample cells escaped (TH09-01, TH09-23); plain-str text escaped; no |safe in templates; empty states; every var(--c-*) in charts.py and the CSS defined in both light and dark blocks.

RED: `PYTHONUTF8=1 uv run pytest tests/unit/reports/test_templates_html.py -q -p no:logging` → 13 failed (templates package dir missing).
GREEN: same command → 13 passed; `pytest tests/unit/reports -q` → 264 passed, 1 skipped (symlink privilege).
Gates: ruff check/format clean, mypy clean on the test file, check_module_size exit 0 (it scans .py only; template counts checked by hand), pre-commit hooks pass (SKIP=pytest-unit per KNOWN-RED ruling).

## Line counts vs budgets
base.html.j2 165/250; funding_review.html.j2 27/200; org_review.html.j2 24/200; partials/components.html.j2 214/220.

## Concerns / spec gaps
- Spec gap: U09-28 says text(block) renders Markup "already computed in the context"; ReportData stores `list[Segment]`, so T09-11 must convert per format (HTML: segments_to_html; MD: segments_to_md) keeping attribute names. T09-10 (parallel) may pick a different convention; the two should be reconciled in T09-11.
- Design §5.2 "chart followed by its data table" vs U09-28 "the table with its chart": templates render cards → chart → table.
- check_module_size does not scan .j2 files, so template budgets are not machine-enforced.
