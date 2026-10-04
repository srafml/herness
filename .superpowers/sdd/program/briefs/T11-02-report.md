# Report: T11-02 Coverage, SQL coverage and truth isolation gates

Status: DONE

Commit: bcf814a — test(support): add coverage, SQL coverage and truth isolation gates (T11-02)

## What was implemented

- `tests/support/coverage_gate.py` (U11-32): `CoverageTarget`, `CoverageViolation`
  dataclasses; `COVERAGE_TARGETS` (the 7 design §4.3 rows verbatim); `check_coverage`
  sums `coverage.py` JSON `files.<path>.summary` counters (POSIX-normalized) per group,
  reports a `no files` violation when a group's statement total is 0, a `line` violation
  when line% < target, and a `branch` violation when target.branch is set and branch%
  < target (a group with 0 branches is treated as 100% branch coverage rather than a
  false violation). 92 lines (budget 150).
- `tests/support/sql_coverage.py` (U11-33): `tables_created(sql_dir)` strips `--` and
  `/* */` comments, then matches `CREATE [OR REPLACE] [TEMP[ORARY]] TABLE|VIEW
  [IF NOT EXISTS] schema.table` case-insensitively, excluding TEMP/TEMPORARY and
  lower-casing the rest; `tables_referenced_by_tests(tests_dir)` scans every
  `tests/**/test_*.py` for `\b(stg|core|enrich|metrics|score|meta)\.([a-z_0-9]+)\b`
  tokens. Both return the empty set for a missing directory rather than erroring
  (`herness/model/sql/` does not exist yet at this branch head, so the real-repo
  UT11-41 gate trivially holds: created − referenced = ∅). 49 lines (budget 100).
- `tests/support/isolation.py` (U11-34, R-64): `TRUTH_TOKENS`, `ISOLATION_ALLOWLIST =
  {"herness/eval/truth.py"}`, `find_truth_references(roots)` walks `*.py/.sql/.yaml/.md/
  .j2` files under each root, skips the allowlisted path (relative to the root's
  parent), and reports every `(rel_path, line_no, token)` hit. 40 lines (budget 80).

## Tests

- `tests/unit/test_coverage_targets.py` — UT11-38 (no-violation at exact target,
  violation below target listing group/measure/actual/target, no branch violation for
  the branch-less eval group, `COVERAGE_TARGETS` matches design §4.3 verbatim, and the
  `HERNESS_COVERAGE_JSON`-driven real gate that skips with reason `coverage.json not
  provided` when unset) and UT11-39 (`no files` violation when the eval group has no
  files).
- `tests/unit/test_sql_coverage.py` — UT11-40 (`tables_created` excludes TEMP/
  TEMPORARY and comment-only mentions, lower-cases the rest; missing dir → empty set;
  `tables_referenced_by_tests` only scans `test_*.py`) and UT11-41 (the real-repo gate:
  `tables_created(herness/model/sql) - tables_referenced_by_tests(tests) == ∅`).
- `tests/unit/test_truth_isolation.py` — UT11-42: `find_truth_references([herness/,
  app/])` on the real repo is empty (the only reference is in the allowlisted
  `herness/eval/truth.py`; `app/` does not exist yet and is silently skipped).
- `tests/security/test_st11_02_truth_isolation.py` — ST11-02: a planted
  `truth_labels` reference under a temp copy of `herness/` is reported with the
  correct `(path, line, token)`; a copy of the allowlisted `herness/eval/truth.py`
  itself is never reported.

## RED evidence

Moved the three new `tests/support/*.py` modules aside and ran:
```
PYTHONUTF8=1 uv run pytest --select-test-ids=UT11-38,UT11-39,UT11-40,UT11-41,UT11-42,ST11-02 -q -p no:logging
```
→ 4 collection errors, e.g. `ModuleNotFoundError: No module named 'tests.support.coverage_gate'`
(and `.sql_coverage`, `.isolation` ×2), `Interrupted: 4 errors during collection`.

## GREEN evidence

Restored the modules and reran the same command:
```
.......s.....                                                            [100%]
SKIPPED [1] tests\unit\test_coverage_targets.py:111: coverage.json not provided
12 passed, 1 skipped, 560 deselected in 0.30s
```
(The one skip is the real `HERNESS_COVERAGE_JSON`-driven gate test, which correctly
skips outside a coverage run per U11-32's algorithm; it is not one of the 6 required
IDs' minimum pass count — all 6 IDs have at least one passing function.)

## Gate outputs

- `uv run ruff format .` → 92 files left unchanged (after formatting the new files).
- `uv run ruff check --fix .` → All checks passed! (one `PERF401` auto-suggestion in
  `isolation.py` was fixed by hand with a comprehension inside `list.extend`).
- `uv run mypy` → Success: no issues found in 35 source files (tests/ is outside the
  `files = ["herness", "tools"]` mypy scope per pyproject.toml; unchanged by this card).
- `uv run lint-imports` → Contracts: 8 kept, 0 broken (no new herness/tools packages;
  no import-linter contract update needed).
- `uv run python -m tools.check_type_ownership` → exit 0 (INFO-only pending-owner
  notices for 06/07/09, pre-existing).
- `uv run python -m tools.check_module_size` → no output (no violations); the three
  new files are 92/49/40 lines, well under their 150/100/80 budgets (the tool only
  scans `herness/app/tools`, so `tests/support/*.py` isn't gated by it, but the
  budgets are met regardless).
- `PYTHONUTF8=1 uv run pytest --collect-only -q --require-test-ids` → exit 0, 573
  tests collected, no untagged test reported.
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`
  → 566 passed, 1 skipped (the coverage-json gate, as above), 5 deselected, 1 xfailed
  (pre-existing `test_it00_02_check_scripts_pass_on_repo` xfail, unrelated to this
  card).
- Acceptance check `pytest --select-test-ids=UT11-38,UT11-39,UT11-40,UT11-41,UT11-42,ST11-02`
  → 12 passed, 1 skipped, 560 deselected.

## Deviations from the brief / design choices left open by the brief

The brief left several concrete choices unspecified; documenting them since they are
observable in test assertions:

- `CoverageViolation.group` is rendered as the target's prefixes joined with `, `,
  trailing `/` stripped (e.g. `"herness/store, herness/model"`,
  `"herness/eval, herness/reports, herness/cli.py"`). Not specified verbatim by the
  brief/spec; chosen for readability in gate output.
- For a group with `branch` target set but 0 total branches in the report, branch% is
  treated as 100 (vacuously satisfied) rather than raising or reporting a violation —
  reasonable since straight-line code has nothing to violate, and it keeps `check_coverage`
  total (no ZeroDivisionError).
- `tables_created`'s TEMP/TEMPORARY exclusion is implemented as an optional capture
  group in one combined regex rather than a second pass, functionally equivalent to
  "TEMP/TEMPORARY tables excluded" applied to the brief's base regex.
- Both `tables_created` and `find_truth_references` return the empty result for a
  missing root directory instead of raising, since `herness/model/sql/` and `app/`
  do not exist yet at this branch head (later cards create them); this keeps
  UT11-41/UT11-42's real-repo assertions meaningful today without a special case.

No test in the brief contradicted the spec; no NEEDS_CONTEXT was required —
`herness.eval.truth` (T11-04) already exists in the tree with `truth.py` matching
`ISOLATION_ALLOWLIST` exactly.

## Concerns

None blocking. Two things the controller may want to note for later cards:
- UT11-41 and UT11-42's real-repo assertions are currently vacuous (no `herness/model/sql/*.sql`
  files and no `app/` package exist yet), so they will start exercising real content
  only once T-cards for impl 02 (SQL) and impl 09 (`app/`) land. The gate logic itself
  is fully tested via UT11-40 and ST11-02 with constructed fixtures.
- `tests/support/coverage_gate.py`'s `HERNESS_COVERAGE_JSON`-driven test currently
  skips in this environment (no coverage.json produced); CI's phase-gate runner
  (T11's `tools/phase_gate.py`, later card) is expected to set that env var when
  running the real coverage gate.
