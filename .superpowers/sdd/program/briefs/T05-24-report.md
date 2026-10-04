# Report: T05-24 Verifier helpers

## Status
DONE

## What was built
`herness/harness/verifier.py` (148 lines, budget 400): the three pure
functions from U05-66 (impl 05 §5.6 steps 6-7):

- `compare_value(claimed, actual, *, unit, duckdb_type, rel_tol) -> bool`
- `canonical_cell_text(value) -> str | int | float | bool | Decimal | None`
- `row_matches(row, row_key) -> bool`

## Partial file: kept, not rewritten
The untracked 148-line partial file left by the previous build agent was
checked line-by-line against the U05-66 algorithm table (all 5
`compare_value` rules, the `canonical_cell_text` type table, and all
`row_matches` key/cell cases including the numeric-key/string-cell,
string-key/numeric-cell, string-key/datetime-cell and default
`canonical_cell_text` fallback branches). It matched the spec exactly,
including:
- unifying `Decimal(str(claimed))` (rule 3) and the float/int split
  `Decimal(repr(claimed))`/`Decimal(claimed)` (rule 5) into one
  `_claim_decimal` helper — equivalent because `str(float) == repr(float)`
  in Python 3.
- wrapping the whole comparison in `decimal.localcontext(prec=60)`, a
  defensive addition not in the spec but not contradicting it (pure,
  deterministic, avoids `InvalidOperation` on wide DECIMAL columns).
- catching `(ArithmeticError, ValueError, TypeError)` around the numeric
  comparison, matching "Errors: None (invalid numeric text compares as
  False)".

No changes were needed to the implementation. I only added the test file.

## Tests
New file `tests/unit/harness/test_verifier.py`, `pytestmark = pytest.mark.unit`,
63 test functions, all IDed:

- **UT05-116** (61 functions): int exact match/mismatch (incl. str claim,
  zero, negative), count/rank units; usd/DECIMAL ROUND_HALF_EVEN ties in
  both directions (float and Decimal actuals) and a non-usd DECIMAL type;
  DOUBLE quantize-match, 0.5% relative tolerance, outside-both, the
  `< 1e-9` tiny floor, int claim on a DOUBLE column; NULL and every
  non-numeric `actual` type (bool, str, date, datetime, arbitrary object)
  mismatching; unparseable claimed text returning False instead of
  raising; `canonical_cell_text` for every scalar type incl. naive vs.
  aware datetimes and microsecond truncation; `row_matches` for None/bool
  keys, numeric keys (incl. a signaling-NaN Decimal cell to exercise the
  `ArithmeticError` catch), string keys (numeric-parse and text paths),
  and date/datetime row keys (ISO date, seconds form, isoformat-with-Z
  form, same-second-different-microsecond, and true mismatch).
- **PT05-04** (2 functions): `@given` over `st.floats` (bounded
  ±1e6, finite) and `d ∈ 0..6`; builds `claimed = str(round_half_even(actual, d))`
  and asserts `compare_value(claimed, actual, ...)` is `True` for both
  `duckdb_type="DOUBLE"` and `duckdb_type="DECIMAL(38,10)"`. No explicit
  `@settings` — uses whatever default/registered profile the suite picks
  up, matching the existing PT05-03 pattern in `test_llm_pricing.py`
  (no local hypothesis profile registration exists yet in this branch).

Coverage of `herness/harness/verifier.py`: **100% line, 100% branch**
(`pytest --cov=herness.harness.verifier --cov-branch`), comfortably above
the 90%/85% requirement.

## Gate results
- `uv run ruff format --check` on both files: clean (one `# noqa: DTZ001`
  added for the intentional naive-`datetime` test case).
- `uv run ruff check`: clean.
- `uv run mypy` on both files: `Success: no issues found in 2 source files`.
- `uv run lint-imports`: `Contracts: 8 kept, 0 broken` (no pyproject
  changes needed — `herness.harness` was already registered as L4 by an
  earlier card).
- `uv run python -m tools.check_module_size`: no output (pass) — file is
  148 lines against the 400-line budget from the module map.
- `uv run python -m tools.check_type_ownership`: pass (only pre-existing
  `INFO pending owner 06/07/09` lines, unrelated to this card).
- `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`:
  **673 passed, 5 deselected, 1 xfailed** (the pre-existing
  `test_it00_02_check_scripts_pass_on_repo` xfail, unrelated to this card).
- `uv run pytest -k UT05_116` -> 61 passed; `-k PT05_04` -> 2 passed.
- `uv run pytest tests/unit/harness/test_verifier.py`: 63 passed.

## Deviations / concerns
- `uv run pytest --require-test-ids` is not yet a recognized pytest
  option on this branch (`unrecognized arguments: --require-test-ids`):
  the repo-wide test-ID-enforcement plugin is owned by impl 11 (IT11-31),
  which has not landed yet. This is expected given the branch state, not
  something this card introduces or can fix; every test function in the
  new file nonetheless carries its ID in both the function name
  (`test_ut05_116_...` / `test_pt05_04_...`) and the docstring's first
  line, per the global constraint.
- No other deviations. The brief's Goal, Units, Files, and Tests rows
  were all satisfied as specified; the module currently contains only
  the three U05-66 functions (no `Verifier` class), consistent with this
  card's scope — the module map's `Verifier` export is for a later card
  per the brief's own Goal wording.

## Commit
`8e63723` — `feat(harness): add verifier helpers (T05-24)`
Files: `herness/harness/verifier.py` (new), `tests/unit/harness/test_verifier.py` (new).
