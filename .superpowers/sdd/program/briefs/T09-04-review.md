# T09-04 review — ui_reads ops area (U09-52)

Commit under review: 2bb1e9b (base 6b70189), worktree agent-ab6109db9504ef8b4.

### Spec Compliance
- ✅ Spec compliant.
  - ✅ All 23 U09-52 functions are present with the exact names, parameter kinds, defaults and return types (herness/store/ops/ui_reads.py:132-349).
  - ✅ Query shapes match the spec: evidence and finding `IN` reads are chunked (:119-129); `recommendation WHERE run_id = ?` (:151); PK reads (:157, :326); `GROUP BY status` and `GROUP BY role, status` (:180, :186); the task projection selects `json_extract(spec,'$.objective')` and orders by `created_at` (:194); the run projection is ordered `started_at DESC` (:208); recommendation filters bind as NULL-tolerant predicates (:227-229); entity findings `DESC` and run findings `created_at, finding_id` (:241, :250); both job payload paths (:257-258); latest decision uses a `row_number()` window (:268); outcomes are ordered by measurement (:282); memory and resilience events are newest first; `source_health` returns up to 200 rows (:321); oldest queued is `min(created_at)` (:337); failed jobs use `status='failed' AND finished_at >= ?` (:346).
  - ✅ All 11 `Ui*Row` TypedDicts exist. Every selected column matches the DDL in migrations 001-004. JSON columns are parsed with `core.load_json`: evidence.params and result_sample, recommendation.numbers, confidence_basis and finding_ids, finding.numbers and query_ids, run.token_usage, outcome.details, memory_item.data and provenance, resilience_event.detail, job.last_error. `task.last_error` and `source_health.last_error` are plain TEXT and are correctly returned unparsed.
  - ✅ `IN` placeholders are built only from `len(chunk)`, with at most 500 per statement. Ids are de-duplicated. Empty input runs no statement.
  - ✅ Limits are clamped to 1..500 and also passed as `read_all` `max_rows` (:112-116).
  - ✅ The module is read-only: only `core.read_one` and `core.read_all` are used, and the test checks `total_changes` does not change.
  - ✅ Imports are only `.core`, `herness.core.time as clock` and stdlib, so ops-areas-acyclic holds (lint-imports: 13 kept, 0 broken).
  - ✅ There is no f-string, `%` or `.format` in the module (grep and AST test). Every public name defined in the module starts with `ui_` or `Ui`, and all 34 are re-exported.
  - ✅ The `ops/__init__.py` import block is placed after `# 10 privacy`, behind `# isort: split`, headed `# 09 ui_reads`. Its `__all__` block sits at the end of the `# noqa: RUF022` list. This matches the controller note ("new areas in card-number order after privacy").
  - ✅ UT09-44: all 22 tests carry the ID in both the function name and the docstring, and `pytestmark` is set. The chunking test asserts 1,200 ids produce placeholder counts [500, 500, 200] and that duplicates collapse (spy on `core.read_all`, test file :209-216). Caps are asserted: `limit=10_000` binds 500 with max_rows 500, `limit=0`/`-5` binds 1, and `source_health` stops at 200.
- ⚠️ Cannot verify from diff alone:
  - The retrospective caller (spec §490) must convert `run.started_at` (TEXT) to a datetime before computing `created_from`/`created_to`, because the builder typed those filters as `datetime`. The spec leaves them untyped, so `datetime` is a sound choice. The check belongs to the renderer card.

### Builder's stated deviations (judged)
- **`finding.claim` not parsed:** accepted. Spec 02 §219 and 003_runs_evidence.sql:55 both define `claim` as plain TEXT with no `json_valid`. The U09-52 text "claim … parsed" is a spec slip. Returning the stored text is correct, and callers render it through `render_marked_markdown`.
- **Extra ORDER BY tie-breakers (id / rowid):** accepted. The output is now deterministic with no conflict with the spec. The `rowid DESC` tie-break in `ui_latest_decisions` is valid because decision_log is a STRICT rowid table.
- **Oldest-queued age clamped to 0:** accepted. It is a reasonable guard against clock skew and returns None only when no job is queued.

### Gates (re-run by reviewer)
ruff check clean · ruff format --check clean (335 files) · mypy 0 issues (144 files) · lint-imports 13 kept / 0 broken · check_module_size exit 0 (ui_reads.py 349/350, `__init__.py` 286/400) · `pytest tests/unit/store -q -p no:logging` 524 passed.

### Strengths
- A small `_chunked` / `_select` / `_parsed` core keeps every query as constant SQL, with only the placeholder list varying.
- The tests use the real migrated store and a `read_all` spy, so chunking and caps are measured rather than assumed. There is also an injection sweep over every filter.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. herness/store/ops/ui_reads.py:340 — `ui_oldest_queued_age_s` subtracts the result of `parse_utc` from a caller-supplied `now`. A naive `now` raises a bare `TypeError`. Passing `now` through `clock.ensure_utc` would give the project's error type.
2. herness/store/ops/ui_reads.py:151 and :128 — `ui_run_rec_ids` and the chunked reads rely on the `read_all` default `max_rows=100_000`. The spec states no cap for `ui_run_rec_ids`, so this is acceptable, but the bound is implicit.
3. herness/store/ops/ui_reads.py:32 — `UiRunRow.token_usage` is typed `dict[str, object]`, but it comes from an unchecked cast of `load_json`. The other JSON fields use `object`. The typing is inconsistent but harmless, since the DDL default is `{}`.
4. herness/store/ops/ui_reads.py — the module is at 349/350 lines, leaving no headroom. Later cards that extend it will need to split it or get a budget ruling.

### Assessment
**Task quality:** Approved
**Reasoning:** Every U09-52 function, row type, cap and chunking rule is implemented against the real DDL with bound parameters only, and every gate passes. The three stated deviations are correct readings of the DDL, and the four minors are polish.
