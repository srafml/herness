# Review: T11-02 Coverage, SQL coverage and truth isolation gates

## Spec Compliance

- U11-32 `tests/support/coverage_gate.py` — ✅. `CoverageTarget`, `CoverageViolation`, `COVERAGE_TARGETS` match design §4.3 verbatim (verified byte-for-byte against brief and against `test_ut11_38_targets_match_design_4_3`, coverage_gate.py:86-94). `check_coverage` sums counters per group, reports `no files`/`line`/`branch` violations per the algorithm (coverage_gate.py:117-140).
- U11-33 `tests/support/sql_coverage.py` — ✅. `tables_created`/`tables_referenced_by_tests` regexes match the brief's patterns; TEMP/TEMPORARY exclusion is structurally equivalent to the brief's (documented deviation, sql_coverage.py:207-211).
- U11-34 `tests/support/isolation.py` — ✅. `TRUTH_TOKENS` and `ISOLATION_ALLOWLIST` match the brief verbatim (isolation.py:159-167); `find_truth_references` walks the given roots for the five suffixes, skips the allowlisted path, reports every `(path, line, token)` hit.
- Test IDs UT11-38..UT11-42, ST11-02 — ✅. All present, named `test_<id>_...`, docstrings start with the ID, `pytestmark` set. Re-ran the acceptance check directly in the worktree:
  `PYTHONUTF8=1 uv run pytest --select-test-ids=UT11-38,UT11-39,UT11-40,UT11-41,UT11-42,ST11-02 -q -p no:logging` → `12 passed, 1 skipped, 560 deselected` — matches the report exactly (skip is the `HERNESS_COVERAGE_JSON`-gated test, which correctly skips outside a coverage run).
- Module budgets — ✅. Verified by direct line count: coverage_gate.py 92/150, sql_coverage.py 49/100, isolation.py 41/80.
- R-64 (nothing under `herness/` except `herness/eval/truth.py` references truth files) — ✅. `herness/eval/truth.py` exists (T11-04, confirmed present in worktree) and is the sole allowlist entry; UT11-42 passes against the real `herness/` tree.
- "Blocked by: none" — ✅ correctly not blocked; UT11-41 and UT11-42's real-repo assertions are honestly reported as currently vacuous (`herness/model/sql/` and `app/` do not exist yet), and this is disclosed in the report's Concerns section rather than hidden.

⚠️ Cannot fully verify from diff: `check_coverage`'s treatment of a group with a branch target but 0 total branches (treated as 100%, coverage_gate.py:132-135) is not literally specified by the brief's "branch % likewise" wording (which would divide by zero); the report discloses this as a deliberate, reasonable design choice. Not a defect, but flagging since it's an interpretation, not a spec-cited rule.

## Strengths

- Deviations from ambiguous parts of the brief (TEMP/TEMPORARY regex structure, 0-branch handling, vacuous real-repo assertions, `CoverageViolation.group` rendering) are explicitly called out in the report rather than silently decided.
- Security test (`tests/security/test_st11_02_truth_isolation.py`) uses `pytest.mark.unit`, matching the existing convention in `tests/security/test_st10_*.py` (no dedicated `security` marker exists in `pyproject.toml`'s marker list, so this is correct, not an oversight).
- Regexes and token lists were checked character-for-character against the brief and match exactly.
- RED/GREEN evidence in the report (collection errors when modules are absent, clean pass when restored) is genuine TDD evidence, not just an assertion.

## Issues

### Critical (Must Fix)
None.

### Important (Should Fix)
None.

### Minor (Nice to Have)
- `coverage_gate.py:101-103` `_normalize` lower-cases neither drive letters nor strips any repo-root prefix — it only backslash→forward-slash converts and runs through `PurePosixPath`. The brief's "Normalize paths to POSIX repo-relative" is satisfied only if `coverage.py`'s JSON already emits repo-relative paths (true today given `source = ["herness"]` and no `relative_files` override changing that), but the function itself would silently mis-bucket an absolute path if the JSON ever changed format. Low risk, no test exercises this edge, not blocking.

## Assessment

**Task quality:** Approved

**Reasoning:** All three units match their brief signatures, algorithms and constants exactly (verified line-by-line against the spec table), the required test IDs all pass on a direct re-run in the worktree (12 passed, 1 skipped, matching the report), R-64 isolation holds against the real repo, and every deviation from ambiguous spec wording is disclosed rather than hidden. No critical or important findings.
