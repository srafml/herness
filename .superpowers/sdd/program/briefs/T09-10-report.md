# T09-10 report: Markdown report templates

Status: DONE_WITH_CONCERNS (concerns are carry-overs for T09-11 / T09-09, not defects)
Worktree: D:\herness\.claude\worktrees\agent-a03d134cb492941ae (branch worktree-agent-a03d134cb492941ae, base 84384c1)
Final commit: 3e13d5e feat(reports): Markdown report templates (T09-10) (wip checkpoint 13b80fb squashed in with `git reset --soft 84384c1`).

## Files
| File | Lines | Budget |
|------|-------|--------|
| herness/reports/templates/base.md.j2 | 162 | 200 |
| herness/reports/templates/partials/components.md.j2 | 128 | 200 |
| herness/reports/templates/funding_review.md.j2 | 12 | 200 |
| herness/reports/templates/org_review.md.j2 | 12 | 200 |
| tests/unit/reports/test_templates_md.py | ~290 | test |

`tools.check_module_size` exits 0 (it only measures .py; template sizes counted by hand above).
No *.html.j2 created or touched. No Python module, pyproject or _data*.py change.

## Design
- base.md.j2: leading `> **Not for decision**` line when `not r.publishable`; blocks `header` (slot 0:
  title + field table incl. coverage and gate-2 stats), `banners` (slot 1), `content` (slot 2
  executive summary, nested block `recommendations` for slot 3, then nested blocks `portfolio`,
  `org_scorecards`, `actions`, `retrospective`, `risks_and_caveats`, `method` for slots 4-9),
  `evidence_appendix` (slot 10), `run_appendix` (slot 11). Slots 4-9 live in base (not duplicated
  in both kind templates) because the outline is identical for both kinds.
- funding_review.md.j2 / org_review.md.j2: extend base, fill `recommendations`: section paragraphs,
  `r.cards`, then `r.funding_table` / `r.org_table` (charts omitted in Markdown; the data tables
  carry the evidence links, design 5.2).
- partials/components.md.j2 macros: `one` (str -> newline-folded -> md), `ok` (query-id check
  q_ + 16 lowercase hex, pure Jinja), `ev` (evidence link), `num(cell)`, `stamp(dt)` (isoformat or
  n/a), `segs(segments, inline)` (segments_to_md in template form), `text(block)`, `empty`,
  `table(t)` (marked rows get ` _(unconfirmed)_` on the first cell), `pairs`, `bullets`,
  `banner(b)`, `section(r, sid, default)`, `card(card)`, `evidence_entry(e)`.
- Every interpolated value goes through `md` (via `one`/`segs`); ints/dates are `|string`ed first.
  Only raw HTML: `<a id="ev-{{ e.query_id }}"></a>` emitted only when `ok(e.query_id)`; links
  `(#ev-q_…)` likewise only for ids passing `ok`, otherwise escaped plain text.
- Fence (ruling): `namespace(run=0)`; `for n in range(1, sql.count('`')+1) if '`'*n in sql` sets
  run=n; fence = '`' * max(3, run+1); block is `<fence>sql` / raw SQL / `<fence>`. No new filter.
- Headings: `#` title, `##` slots/appendices, `###` cards, scenarios, scorecards, retro items,
  caveat sub-lists, evidence entries.
- Default section titles from design 5.2 (Executive summary; Funding: top-N recommendations; Org:
  top-N orgs/teams to improve; Portfolio; Org scorecards; Recommended actions; Did last quarter's
  recommendations work?; Data quality and caveats; Method); `r.section_titles[sid]` wins when the
  draft has that section.

## Context attribute names used (carry-over for T09-11 to reconcile with T09-09)
- `r.<ReportData field>` exactly as in _data.py: header, publishable, banners, sections,
  section_titles, cards, funding_table, org_table, portfolio_blocks, scorecards, levers, retro,
  caveats, method, run_appendix. NOT read: charts, numbers_total, number_query_ids.
- `r.evidence`: list[EvidenceEntry] (every field read: query_id, sql, params,
  row_count, executed_at, build_id, result_sample, sample_columns, used_by, found).
- `r.manifest`: NOT read by the Markdown set (header already carries run_id/build/rendered_at;
  template version comes from the `TEMPLATE_VERSION` global). T09-11 may still provide it for HTML.
- Globals: `TEMPLATE_VERSION` (run appendix). Filter: `md` only.

## Carry-overs / spec notes
1. Text blocks are rendered from `Segment` lists in-template (`segs` macro), not from a
   precomputed segments_to_md string, so T09-11 needs no Markdown precompute. Output equals
   `segments_to_md` for image-free text (tested). Divergence: `strip_images` has no Jinja
   equivalent (no regex, fixed filter list), so `![alt](url)` in model text is shown escaped
   (`\!\[alt\]\(url\)`) instead of as its alt text. Still no image syntax/foreign link (tested).
   If T09-11 prefers exact segments_to_md output, it can add precomputed strings and swap the
   `segs` body; or U09-23 could add an `md_segments` filter (spec change).
2. In `inline` mode (headings, table cells) newlines are folded to spaces before escaping; in
   paragraphs they are kept as in segments_to_md.
3. Card anchors `rec-N` are HTML-only (MD may emit no raw HTML besides evidence anchors), so MD
   evidence "Used by" lists locations as plain escaped text, and lever `cards` are plain text.
4. Datetimes print as `dt.isoformat()` (e.g. `2026-09-26T12:00:00+00:00`, `+` escaped);
   T09-09 may choose a different display format; align for the golden files in T09-11.
5. Packaging: pyproject `[tool.hatch.build.targets.wheel] packages = ["herness"]` ships every
   tracked file under herness/, so `templates/` is included; PackageLoader works from the
   editable install (tests use it). No pyproject change.
6. The test-local environment/wrapper (`_environment`, `_render`) should be replaced by
   `render.build_environment` and the U09-24 conversion once T09-11 lands.

## Tests (tests/unit/reports/test_templates_md.py, all UT09-92)
- missing variable (cards, evidence, method, publishable) x both kinds -> SchemaViolation
  "report template error: <name>" (8)
- full realistic context from real ReportData/Header/Card/.../EvidenceEntry renders both kinds;
  heading levels 1-3, slot order, draft title wins, evidence link, not-found entry (2)
- recommendation tables per kind + unconfirmed mark + default slot-3 titles (1)
- watermark leading line when not publishable, absent when publishable (2)
- fence rule for SQL with 0, 1, 3, 5+2+1 and bare 3 backticks (5)
- anchor before each evidence entry, in order (2); invalid id -> no anchor/link (1)
- hostile model text (`<script>`, `![img](http://x)`, `[x](http://evil)`, `|`, backticks),
  publishable true/false x both kinds: no raw HTML besides anchors, no image, only `#ev-q_` links,
  pipes escaped in cells (4)
- segs == segments_to_md for image-free segments (1); empty states (2); evidence n/a fields (1)

RED: `uv run pytest tests/unit/reports/test_templates_md.py` -> 26 failed (templates dir missing).
GREEN: 29 passed. `uv run pytest tests/unit/reports -q -p no:logging` -> 280 passed, 1 skipped.
Broad run `pytest -m "(unit or integration) and not slow"` (harness dir and the two known-red
tests deselected) -> 5003 passed, 12 skipped, 2 xfailed.
Gates: ruff format/check clean, mypy clean on the test file, lint-imports 13 kept,
check_module_size exit 0, `--require-test-ids` ok. Commits used `SKIP=pytest-unit` (known-red hook).

## Fix round 1 (review: Approved with Minors; M2, M3, M5)
Commit: 18496d1 fix(reports): harden Markdown fence and fold tests (T09-10 review) (on top of 3e13d5e, no rewrite).
- M5: components.md.j2 fence scan now uses `namespace(run=0, done=false)`; the loop
  (`for n in range(1, count+1) if not ns.done`) sets `done` at the first n whose run is absent,
  so no further substring checks run (runs are contiguous). Pure Jinja, no new filter.
  New test `test_ut09_92_fence_scan_stops_at_first_missing_run`: 20,000-char alternating
  backtick SQL renders in < 1 s with a ``` fence, SQL intact.
- M3: fence cases added: SQL ending in four backticks -> 5-tick fence; SQL with a
  backtick-only middle line (````) -> 5-tick fence; block closed, SQL intact.
- M2: `test_ut09_92_newlines_folded_in_headings_and_cells`: LF, CRLF and CR in the header title,
  a section title, a card headline (inline segments with a number link) and table cells
  (plain and linked) fold to spaces; no `\r` in output, no orphan continuation lines, every
  table line ends with `|`. Mutation-checked: removing the fold in `one` or in inline `segs`
  makes it fail.
- components.md.j2 now 129 lines (budget 200).
Tests: test_templates_md.py 33 passed; tests/unit/reports 284 passed, 1 skipped. ruff, ruff
format, mypy clean; pre-commit hooks passed (pytest-unit skipped as instructed).
