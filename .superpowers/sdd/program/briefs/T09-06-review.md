# T09-06 review: Number formats and SVG charts

Reviewed: worktree agent-a6262c54a8f79f7e0, commit 365bd5e (base 851d67d). Read-only.

### Spec Compliance
- ✅ Spec compliant (with the sub-controller's ruling on U09-102 unknown `fmt` -> impl 00 "n/a"; recorded as a spec note, not re-raised).

Per unit:
- ✅ U09-12 `confidence_label` (_format.py:39-47): >=0.7 high, >=0.4 medium, other numbers low, None unknown. Decimal comparison avoids float/Decimal edge drift; NaN/sNaN -> low (no raise).
- ✅ U09-102 `format_value` (_format.py:21-36): None -> "—"; `Decimal(str(value))` failure (InvalidOperation, or ValueError for ints over the str digit limit) or non-finite -> "n/a"; else delegates to `herness.core.numbers.format_value(number, "", fmt)`. No formatting rule defined locally (invariant holds).
- ✅ U09-19 `bar_h` (charts.py:110-134): one rect per drawn item (first max_items), width = value/max*span, negative/NaN -> width 0 + class `neg`, all-zero -> zero widths, labels cut to 60 with `…`, `hl` class for highlight, label column 38 %, 1-decimal coordinates, height (n x (bar_height+6) + 24).
- ✅ U09-20 `step_budget` (charts.py:137-173): one `<path>` from (left,bottom) through cumulative points (H/V steps), dashed `<line class="budget">` at x = budget, x range max(budget, total effort), y range total impact, empty steps -> axes + budget line only, negatives -> 0, <= 500 steps.
- ✅ U09-21 `sparkline` (charts.py:199-230): 0 -> `<svg class="empty">` with only title; 1 -> one circle; >= 2 -> polylines broken at None; constant -> flat at height/2; min-max scale with 2 px padding; <= 52 points.
- ✅ U09-22 `dot_z` (charts.py:233-270): one row per label (<= 50), zero line, z clipped to ±clip with class `clipped`, None -> empty row, linear scale -clip..+clip, clip clamped 1-10.
- ✅ §3.5 common rules: `role="img"`, `<title>` first child, fixed viewBox, no script/href/external ref/event attributes, colours only `var(--c-...)`, text escaped with `xml.sax.saxutils.escape`, `quoteattr` for the class attribute, no numeric labels.

Per test ID (all have >= 1 function; names contain the ID with `_`, docstring first line starts with the ID; module-level `pytestmark = pytest.mark.unit`):
- ✅ UT09-19 (3 functions, test_format.py) ✅ UT09-20 (2) ✅ UT09-21 (parametrized, 10 cases incl. the 5 spec rows) ✅ PT09-04 (Hypothesis, incl. huge ints and random fmt text)
- ✅ UT09-28 (2, test_charts.py) ✅ UT09-29 (3) ✅ UT09-30 (1, all four cases) ✅ UT09-31 (1) ✅ PT09-07 (5 Hypothesis tests: one per chart plus a hostile-label test)

Acceptance: card tests 36 passed (re-run: `PYTHONUTF8=1 uv run pytest tests/unit/reports/test_format.py tests/unit/reports/test_charts.py -p no:logging`). Chart output has no `script`, `href`, `url(` for Hypothesis inputs (inputs filtered of those words, output checked as a raw substring; the hostile-label test adds markup and checks structurally that no script element, href/on* attribute or `url(` value appears). TH09-01: labels and titles escaped; markup in labels round-trips as text (UT09-31 asserts `team <a>` as parsed text).

Gates re-run: coverage _format 100 % line / 100 % branch (27 stmts, 10 br), charts 100 % / 100 % (161 stmts, 26 br); ruff check clean (incl. C901, PLR0913; bar_h has 6 args); ruff format clean; mypy clean on both modules; lint-imports 11 kept 0 broken (charts imports stdlib only, _format imports L0 `herness.core.numbers`). Module budgets: _format.py 47/60, charts.py 270/300.

Builder interpretations, judged:
- Clamping of out-of-range sizes (spec says Errors: None): accepted; ranges match the spec where it gives them; sparkline (16-1600, 8-800) and dot_z width (200-1600) ranges are invented but harmless.
- bar_h height from drawn bars (first max_items) rather than len(items): accepted; spec's "one rect per item (first max_items)" makes len(items) mean the drawn items. Spec note: tighten wording.
- sparkline keeps the last 52 points: accepted (trend chart; newest data matters).
- sparkline isolated point in a >= 2-value series drawn as a small circle: accepted (a 1-point polyline is invisible); UT09-30 pins it.
- Decimal maths in step_budget under a trap-free, unbounded-exponent context: accepted; no overflow, NaN from bad text maps to 0.
- Inline `style="fill:var(--c-...)"`: accepted. Spec REPORT_CSP (impl 09 line 705 and TH09-03) is `default-src 'none'; style-src 'unsafe-inline'; img-src data:`, so inline style attributes are allowed; spec §3.5 requires colours only as CSS variables, which holds.
- XML-1.0-invalid character stripping: accepted and verified (probe: `_text('\ud800x￾\x00y', 60)` -> `xy`; pattern excludes U+FFFE/U+FFFF and surrogates).

- ⚠️ Cannot verify from diff: rendering of the inline SVG (no `xmlns`) inside the report template and PDF path (T09-08/T09-09 and later); the `--c-bar/--c-accent/--c-muted/--c-axis` variables being defined in `base.html.j2` (later card).

### Strengths
- Tight, pure modules well under budget; all non-raising paths covered by Hypothesis with huge ints, sNaN Decimals, non-finite floats and random sizes.
- Escaping plus invalid-char stripping keeps any label text well-formed; no attribute is built from data.
- Sparkline scale uses halved operands so `high - low` cannot overflow for extreme finite floats.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/reports/charts.py:34 — `_NON_XML` embeds raw invisible code points (U+D7FF, U+E000, U+FFFD) in the source literal; correct at runtime but unreadable and fragile under editors/formatters. Prefer `퟿`, ``, `�` escapes.
2. herness/reports/charts.py:127-129 — `+inf` values (and Decimals beyond float range) are drawn as width 0 with class `neg`; the spec names only negative and NaN. Harmless in practice (priority is finite), but a comment or spec note would make the choice explicit.
3. tests/unit/reports/test_charts.py:157 — UT09-31 asserts the clipped dot has the largest cx among dots, not that it sits exactly at the right edge (`label_w + span`, 632.0 for width 640). Probe confirms the edge position; an exact assertion would pin "dot at edge".
4. Spec note (not a code defect): U09-19 Algorithm "Height = len(items) x ..." should read "number of drawn items"; U09-102 Errors text on unknown `fmt` is stale (already ruled).

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit postcondition and all nine test IDs are met, with 100 % line/branch coverage, clean gates and in-budget modules; the listed interpretations are sound and consistent with the spec's CSP and the §3.5 rules. Remaining items are readability and test-precision polish.
