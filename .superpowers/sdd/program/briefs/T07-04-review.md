# T07-04 review — Closed-loop and chat data access (head 43bb811, base cd44be5)

### Spec Compliance
- ✅ U07-31 insert_recommendations / run_recommendations: conn keyword-only; `conn=None` → `ConfigError("insert_recommendations requires the caller's transaction")` (C1 verbatim); 1–50 rows; every row validated before the first INSERT; `SchemaViolation("insert_recommendations: invalid <column>")`; constant 14-column INSERT with JSON via `dump_json`; read ordered by `rec_id` with the three JSON columns parsed. The `= None` default is what makes the C1 ConfigError reachable, so the gap from the spec's "no default" table is intended.
- ✅ U07-32 insert_decision / latest_decisions: decision set, reason 1–1000, decided_by `^[0-9a-f]{32}$`, fixed-width timestamps, `SchemaViolation("insert_decision: invalid <column>")`; append-only (no update/delete); ROW_NUMBER over `decided_at DESC, rowid DESC`, and the test covers the equal-decided_at tie and the older-decided_at/newer-rowid case; unknown rec → FK SchemaViolation through run_write (tested).
- ✅ U07-33 insert_outcome (INSERT OR IGNORE, `rowcount == 1`), outcome_exists, latest_outcomes (`measurement DESC, measured_at DESC`), treated_targets (latest decision accepted, `effective_at >= start AND < end`; both edges tested). due_measurements: expected_metric NOT NULL, latest decision accepted, per-metric weeks with default, due = effective_at date + 7×weeks (00:00 UTC) ≤ now, no outcome row per measurement (EXISTS), sorted by (due, rec_id, measurement). UT07-10 covers the per-metric and default cases, latest-decision-wins, due == now, and the ordering tie.
- ✅ U07-34 recent_runs_with_recommendations (own SQL per ruling 1: kind filter, all statuses, in DISTINCT recommendation.run_id, exclude, `started_at DESC, run_id DESC`, LIMIT 0–10), accepted_since (latest accepted with `decided_at >= ?`, `created_at DESC`), outcomes_for_similarity (latest outcome, `ORDER BY measured_at DESC … LIMIT 5000`), rec_memory_ids (bound ids and kinds, subset check, `ORDER BY created_at, memory_id`, EXPLAIN confirms ix_memory_rec). Error text is `"<function>: invalid argument"`, verbatim.
- ✅ U07-36 dead_task_count, run_findings_for_promotion (`status='verified' OR (status='rejected' AND json_extract(verification,'$.passed') = 0)`, ORDER BY finding_id), task_spec (None for an unknown task), recent_done_runs (`status='done' AND finished_at >= ?`, ORDER BY finished_at, run_id). Ids go through `ToolInputError("invalid id: <function>")`, verbatim. No `run`/`task` reader import (ruling 1).
- ✅ Package: the 07 closed_loop import + `__all__` block sits between `# 07 memory` and `# 09 ui_reads` (card order), `# isort: split`, and there is no `get_run`. UT02-68 passes (8 selected tests).
- ✅ TH07-09 / C2 / C7: every statement is a module constant. The only runtime `.format` inserts `marks(n)` placeholders; `SIMILARITY_MAX`/`_HAS_OUT` are constants; ids are regex-validated; error messages carry no row content (tested for C5).
- ✅ Rulings 2–4: row TypedDicts are area-local, C8 app-side NOT NULL/range checks are present (reason, query_id, confidence), and the private sibling `_closed_loop_rows.py` has its pyproject ignore and §2 row (360).
- ⚠️ Cannot verify from the diff:
  - task_spec returns the raw stored JSON object, not `TaskSpec.model_dump(mode="json")`. Fields defaulted by the model but missing from the stored JSON will differ from what `runs.get_task` would give. The U07-90 caller must not rely on the defaults (a consequence of ruling 1).
  - `herness/store/ops/__init__.py` is at 386/400. The next area block will need a budget ruling.
  - The TDD RED step was not captured (builder concern).

### Strengths
- Tight, readable SQL constants. Latest-decision and latest-outcome logic is shared through one window-function fragment, so every "latest accepted" reader is consistent.
- Tests exercise real ordering and tie-break edges (rowid tie, half-open window, due == now, duplicate ids, 51 rows, rollback of a bad batch) and cover the C5 mapping without leaking the sqlite message.
- Coverage 100 % line and branch on both new modules (re-run: 68 passed). mypy (project scope) 0 errors, ruff check/format clean, lint-imports 13 kept, check_module_size exit 0.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/store/ops/closed_loop.py:224-226 and :203-205 — invalid week pairs, `now`, `outcome_exists` measurement and `treated_targets` arguments raise `ToolInputError`. U07-33's Errors row lists only `SchemaViolation` and `StoreBusy`. The builder disclosed the choice and the spec does not bind the class for week pairs. Confirm with the U07-86 caller, or switch to SchemaViolation.
2. tests/unit/store/ops/test_store_ops_closed_loop.py:620 (the re-export test labelled UT07-68) and :592/:612/:660 (C3/C5 plumbing tests labelled UT07-67) borrow unrelated harness-level IDs. `test_cv_`/`test_rf_`, or the unit's own UT row, would be more honest. Store-level tests under UT07-63/64/74/78/84 also share IDs that later L4 cards own, so `-k UT07_63` will mix levels.
3. tests/unit/store/ops/test_store_ops_closed_loop.py:255, :395 — two mypy `arg-type` errors (`**{column: value}`). Tests are outside the mypy `files` scope, so the gate is unaffected.
4. herness/store/ops/closed_loop.py:250-252 — `outcomes_for_similarity` keys rows by `SimilarityRow.__annotations__` order. This is an implicit coupling to the SELECT column order in `_closed_loop_rows.py:95-100`. An explicit column tuple (like REC_COLUMNS) would be safer.
5. herness/store/ops/_closed_loop_rows.py:316-319 — reads use `core.connection().execute` instead of `core.read_one`/`read_all` (C1 wording). This mirrors the T07-03 `_memory_rows.query` precedent, and C5 mapping is done locally.
6. herness/store/ops/_closed_loop_rows.py:165 vs :194 — `OutcomeRow.query_id: str` but `SimilarityRow.query_id: str | None`. The inner join makes it non-null. Cosmetic.
7. Report accuracy: the report claims 76 test cases; pytest collects 68.
8. insert_outcome has no FK (unknown rec_id) test. Parity with insert_decision's test would be cheap.

### Assessment
**Task quality:** Approved
**Reasoning:** All five units meet their preconditions, algorithms, orderings and verbatim messages, and the sub-controller rulings are implemented correctly. Gates are green with 100 % coverage; the remaining items are labelling, typing polish and one error-class choice to confirm with the caller.
