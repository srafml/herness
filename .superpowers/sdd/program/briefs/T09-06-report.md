# T09-06 report: Number formats and SVG charts

Status: DONE_WITH_CONCERNS
Commit: 365bd5e feat(reports): add table-cell formatter and SVG charts (T09-06)
Worktree: D:\herness\.claude\worktrees\agent-a6262c54a8f79f7e0 (branch worktree-agent-a6262c54a8f79f7e0)

## Built
- `herness/reports/_format.py` (47 lines, budget 60)
  - `format_value(value, fmt)` (U09-102): None -> "—"; `Decimal(str(value))` failure (DecimalException / ValueError for huge int) or non-finite -> "n/a"; else `herness.core.numbers.format_value(Decimal, "", fmt)`. No formatting rule defined here.
  - `confidence_label(confidence)` (U09-12): compares `Decimal(str(x))` with Decimal("0.7") / Decimal("0.4") (avoids float/Decimal edge mismatch); NaN -> "low" ("other numbers"); None -> "unknown".
- `herness/reports/charts.py` (270 lines, budget 300): `bar_h`, `step_budget`, `sparkline`, `dot_z`. Each returns one `<svg role="img" class=... viewBox=... width height>` with a `<title>` first child; no script, href, xmlns URL, url( or event attributes; colours only as `style="fill|stroke:var(--c-bar|--c-accent|--c-muted|--c-axis)"` (inline style attributes, permitted by the report CSP `style-src 'unsafe-inline'`; `var()` in presentation attributes is not reliably supported). Text is stripped of XML-1.0-invalid characters (so random input stays well-formed), cut (title 120, labels 60 with "…") and escaped with `xml.sax.saxutils.escape`; the only attribute value built with quoteattr is the fixed svg class.
- Tests: `tests/unit/reports/test_format.py`, `tests/unit/reports/test_charts.py` (module-level `pytestmark = pytest.mark.unit`, IDs in names and docstrings, Hypothesis under the repo "commit" profile).
- No pyproject change needed: `herness.reports` already in the L5 layer; mypy `files` covers `herness`.

## Interpretations
- Parameter constraints are clamped (spec says "Errors: None"): bar_h width 200–1600, bar_height 8–40, max_items 1–100; step_budget width 200–1600, height 100–800; dot_z width 200–1600, clip 1–10 (non-finite clip -> 3.0); sparkline (no range given) width 16–1600, height 8–800.
- bar_h height uses the number of drawn items (first max_items), not len(items), so truncated inputs do not leave empty space. Non-finite values (NaN, ±inf, Decimals beyond float range) are width 0 with class `neg`. Highlighted rows get class `hl` on both rect and label.
- step_budget: arithmetic in Decimal under a trap-free context with unbounded exponent (no overflow raise); negative, non-finite or unconvertible effort/impact/budget -> 0; x range max(budget, total effort) (1 if 0), y range total impact (1 if 0); path is `M left bottom` then `H x V y` per step; budget line `class="budget"` with `stroke-dasharray="4 3"`; first 500 steps only.
- sparkline: last 52 points kept; non-finite values are gaps like None; an isolated point inside a ≥2-value series is drawn as a small circle (otherwise a 1-point polyline would be invisible); min–max scale with 2 px padding, constant series at height/2.
- dot_z: first 50 rows; NaN/None -> empty row (label only); ±inf is clipped; zero line `class="zero"`.

## Concern (spec vs impl 00)
- U09-102 "Errors" says "an unknown `fmt` is formatted as `plain` by impl 00", but `herness.core.numbers.format_value` returns "n/a" for a format not in NUMBER_FORMATS. I delegated unchanged (invariant: no formatting rule in this module), so `format_value(x, "bogus")` -> "n/a"; test UT09-19 `test_ut09_19_unknown_format_is_delegated` pins equality with impl 00 rather than a value. Controller should rule whether the spec text or impl 00 changes.

## TDD evidence
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/reports/test_format.py` -> `ImportError: cannot import name '_format' from 'herness.reports'`; same collection error for test_charts.py (`charts`).
- GREEN: `PYTHONUTF8=1 uv run pytest -k "UT09_19 or UT09_20 or UT09_21 or UT09_28 or UT09_29 or UT09_30 or UT09_31 or PT09_04 or PT09_07" -p no:logging` -> 36 passed, 1471 deselected.
- Coverage (`--cov=herness.reports._format --cov=herness.reports.charts --cov-branch`): _format 100 % (27 stmts, 10 branches), charts 100 % (161 stmts, 26 branches).
- `pytest -m "(unit or integration) and not slow"`: 1501 passed, 5 deselected, 1 xfailed (pre-existing IT00-02 xfail).
- `pytest tests/unit/reports --require-test-ids`: 65 passed.

## Gates
ruff format (2 files reformatted), ruff check --fix (1 auto-fix, 0 remaining), mypy strict: no issues in 71 files, lint-imports: 11 kept 0 broken, check_module_size: clean, check_type_ownership: clean. PLR0913: bar_h has exactly 6 args (max-args 6), no noqa needed.
