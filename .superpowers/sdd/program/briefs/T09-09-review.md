# T09-09 review: HTML report templates

Reviewed head 0b1c18a (base 84384c1), worktree agent-a16e2ad05f9ea59e7. Read-only review.

### Spec Compliance
- ✅ Spec compliant (U09-28 HTML part, design §5.2 slots 0-11, controller rulings honoured)

U09-28 `base.html.j2` rules:
- ✅ `<!DOCTYPE html>` (base.html.j2:1); rendered output starts `<!DOCTYPE html>\n<html` (trim_blocks drops the comment/import/set lines).
- ✅ CSP meta `{{ REPORT_CSP }}` is the first element of `<head>` (base.html.j2:8), charset meta next (:9). The attribute is autoescaped (`'` → `&#39;`), which is valid HTML and decodes to the exact policy.
- ✅ `<title>` = escaped title (:11): `h.title|striptags` turns the segments_to_html Markup into plain text, which autoescape re-escapes (no double-escape, no markup).
- ✅ One inline `<style>` (:12-92); light `:root` vars (:13-26) and dark via `@media (prefers-color-scheme: dark)` (:27-41); `--c-accent/--c-axis/--c-bar/--c-muted` defined in both; test cross-checks every `var(--c-*)` in charts.py and the CSS.
- ✅ `@media print` expands `details` and hides summary markers (:82-91).
- ✅ No `<script>`, `<link>`, `<img>`, `url(` (grep and rendered-output scan).
- ✅ Blocks `header` (:98), `banners` (:116), `content` (:126), `evidence_appendix` (:128), `run_appendix` (:138).
- ✅ Watermark: `not-for-decision` on `<body>` (:94) and fixed-position caption `.watermark` (:75-79, :95-97), both only when `not r.publishable`; placed outside overridable blocks.

`funding_review.html.j2` / `org_review.html.j2`:
- ✅ Extend base; slots 2-9 in §5.2 order (exec summary, recommendations, portfolio, scorecards, actions, retrospective, caveats, method); slot 3 = cards, then chart (`funding_priority` / `org_z`), then table — matches the controller ruling and design "chart followed by its data table". Unconfirmed funding rows marked (class + title + footnote).

`partials/components.html.j2` macros:
- ✅ `text(block)` renders `block.segments` (Markup precomputed by caller, per ruling) (:3-5).
- ✅ `num(cell)` exactly `<a class="num" href="#ev-{{ cell.query_id }}" title="{{ cell.title }}">{{ cell.text }}</a>` or plain text when `query_id is none` (:11-13).
- ✅ `table(columns, rows)` (+ optional `marked`) (:17-30), `card(card)` with `id="rec-{{ card.rank }}"` (:34-50), `banner(b)` (:51-58), `empty(text)` (:14-16).
- ✅ `evidence_entry(e)` (:62-100): `<a id="ev-{{ e.query_id }}"></a>`, SQL in `<pre><code>`, params table, build, row count, sample table of escaped cells or "Sample not stored for this build", used-by as `#rec-N` link or plain text (`used_by` macro :59-61 only links `rec-<digits>`).
- ✅ Attribute names match `_data.py` (Header, Card, LeverRow, RetroItem, Retrospective, Caveats, MethodInfo, RunAppendix, ReportBanner), `_data_slots.py` (Cell, TextBlock, Table, PortfolioBlock, Scorecard) and `_evidence.py` (EvidenceEntry); the tests render real instances of these under StrictUndefined.

Security:
- ✅ TH09-01: autoescape on; no `|safe` anywhere (test enforces with a regex over all `*.html.j2`); model-text `<script>` renders escaped; a non-Markup text block is escaped.
- ✅ TH09-23: sample cell `<b>t1</b>` renders `&lt;b&gt;t1&lt;/b&gt;`.
- ✅ TH09-03: rendered output has no `http:`/`https:`/`//`, `url(`, script, link, img; charts use inline `style=` attributes, permitted by `style-src 'unsafe-inline'`.
- ✅ |safe invariant: only Markup from the context (segments_to_html, charts wrapped by the caller) is output unescaped.

- ⚠️ Cannot verify from diff: real `render_run` context construction (T09-11) — whether U09-24 converts segments/charts exactly as the test's `_context()` does; IT09-01 golden and full ST09-03 run end to end are deferred to T09-11 by the card.
- ⚠️ `@media print` `details::details-content` is a recent pseudo-element; WeasyPrint (U09-26 PDF) support is unverified. Moot today because the only `<details>` is rendered `open`.

### Strengths
- Tight, readable templates; slot macros shared by both kinds keep the review templates at 27/24 lines.
- UT09-92 test removes every context key one at a time for both templates and asserts `SchemaViolation` names the template, plus a nested-attribute case; builds the context from real ReportData/EvidenceEntry objects, so attribute drift in T09-08 would be caught.
- Good empty-state coverage and anchor checks (num, rec-N, ev-q_…, used-by).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/reports/templates/partials/components.html.j2:129,132 — the trend column is inserted with a hard-coded slice `[:5]`/`[5:]`, silently coupled to `SCORECARD_COLUMNS` order in `_data_slots.py:73` (z_score at index 4). A column reorder would misplace the sparkline with no test failure. Consider locating `"z_score"` by index or documenting the coupling.
2. herness/reports/templates/partials/components.html.j2:53 — `b.text|replace("`", "")` silently strips backticks from banner text (the `off_network_profile` banner, `_data.py:41`). Undocumented lossy transform; rendering the profile as `<code>` or leaving the text as-is would be clearer. Cosmetic.
3. tests/unit/reports/test_templates_html.py:326 — the print-CSS check is only `"details" in` the print block; it does not pin the summary-marker hiding or content expansion rules of U09-28.
4. herness/reports/templates/partials/components.html.j2:98 — an evidence entry with an empty `used_by` renders a dangling "Used by: " line (collector always records a place today, so latent only).
5. herness/reports/templates/partials/components.html.j2 is 214/220 lines — 6 lines of headroom; budgets for .j2 are not machine-enforced (`check_module_size` scans .py only), as the report notes.

### Gate results (run by reviewer in the worktree)
- `PYTHONUTF8=1 uv run pytest tests/unit/reports/test_templates_html.py -q -p no:logging` → 13 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/reports -q -p no:logging` → 264 passed, 1 skipped (symlink privilege, pre-existing).
- `uv run ruff check .` → All checks passed. `uv run ruff format --check .` → 524 files already formatted.
- `uv run mypy` → Success: no issues found in 211 source files.
- `uv run python -m tools.check_module_size` → exit 0.
- Line budgets (hand count): base.html.j2 165/250, funding_review.html.j2 27/200, org_review.html.j2 24/200, partials/components.html.j2 214/220. All within.
- Test conventions: `pytestmark = pytest.mark.unit`; all 13 functions named `test_ut09_92_*` with docstrings starting `UT09-92`.
- Diff scope: only the 4 templates + 1 test file; no pyproject change (hatch `packages=["herness"]` ships .j2, per report).

### Assessment
**Task quality:** Approved
**Reasoning:** Every U09-28 HTML content rule, macro contract and §5.2 slot order is met, autoescape and the no-`|safe` invariant hold with real ReportData/EvidenceEntry objects, and all gates are green; remaining items are minor polish.
