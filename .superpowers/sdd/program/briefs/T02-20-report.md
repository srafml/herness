# T02-20 report: DQ checks and gate (impl 02 U02-93, U02-94, U02-102, U02-127; TH02-17 gate part; T02-19 M5 carry-over)

Worktree: D:\herness\.claude\worktrees\agent-a0c0a65062a11a6a1 (branch worktree-agent-a0c0a65062a11a6a1, base 3f61b67)
Commits: 16cd526 wip(T02-20) checkpoint; 8e01bf7 feat(model): T02-20 DQ checks and gate (all pre-commit hooks passed, incl. pytest-unit)

## Files
- NEW herness/model/sql/900_dq_checks.sql (149 lines, budget 400): U02-127. Deletes its own `dq900` rows, then one static INSERT per check family (row_count_drop x12, incident_service_null, work_item_service_null, cast_fail per stg.cast_stats column, future_timestamp x11, resolved_before_opened, duplicate_key x9, decision_coverage_incident, metric_daily_unmapped_service). Table/column names are template constants passed through `ident`/`sqlstr`; thresholds only via `dq.<field> | num`; nothing from data becomes SQL text.
- NEW herness/model/dq.py (84 / 120): `DqOutcome` (frozen, invariant enforced in `__post_init__`), `evaluate_gate(con, *, build_id=None)`.
- NEW herness/model/_build_dq.py (71 / 120, new §2 row): `stage_dq` (U02-102) + previous-count reader.
- herness/model/build.py (398 / 400): import, stage-table entry `"dq"`, comment/docstring. Result `"dq"` key was already present (None); stage_dq sets it via `run.result`.
- herness/model/_build_stages.py (190 / 200): M5 wrap `_hook_errors(stage, passthrough)` around run_enrichment (YieldRequested passes), materialize_facts and run_scoring.
- docs/impl/02-data-model.impl.md §2: new `_build_dq.py` row; `_build_stages.py` row amended for the M5 wrap.
- tests/integration/model/test_model_dq_checks.py (NEW, IT02-28, build_harness on crafted tables)
- tests/integration/model/test_model_build_dq.py (NEW, IT02-29, IT02-30, M5 regressions labelled IT02-25/IT02-26)
- tests/integration/model/test_model_build_pipeline.py: "not available" assertion switched to stage promote.

## Decisions / spec notes
1. Stage unit returns `StageStatus` (`done`/`yield`), not `DqOutcome`: the stage table contract is StageStatus and 900-999 may yield. The outcome goes to `result["dq"] = {checks, failed_errors: [...names], failed_warnings: [...names]}` (spec §U02-98 step 8 names the keys; lists of check names chosen). T02-21's promote can call `stage_dq` when dq did not run in the job.
2. `evaluate_gate` gains keyword-only `build_id` (for the §8.1 log fields `build_id`); positional `con` as spec.
3. Gate fails closed beyond the spec: rows with severity outside {error, warn} or NULL `passed` -> SchemaViolation("invalid DQ result row"); a DuckDB read error -> SchemaViolation("cannot read DQ results", from None).
4. Gate failure: `update_build_row(status="failed", finished_at)` then `DqGateFailed(build_id, failed_errors)`; `_run_stages` failure path runs too (logs model.build.failed, metrics). CURRENT never touched. On pass a CHECKPOINT follows.
5. SQL interpretations: row_count_drop prev missing for a table -> prev 0, value 0 (details prev 0); growth gives a negative value (passes). incident_service_null passed = v <= warn (and <= error). cast_fail aggregates repeated stg.cast_stats rows per (table, column) (cast_stats is INSERT-only, re-runs duplicate rows; ratio unchanged) and check name is `cast_fail:stg.<table>.<column>` (table_name already carries `stg.`). resolved_before_opened details `rows` = incidents with both timestamps. duplicate_key: NULL single-column keys count as duplicates (count(DISTINCT) skips NULL; spec formula verbatim); metric_daily uses count(DISTINCT row(...)). decision_coverage: covered/with_text, 1.0 with no incidents, 0.0 with incidents but no text. future_timestamp uses max(meta.build.started_at) (no row -> 0 counts).
6. Previous counts: `CURRENT` naming another build -> `open_readonly` (default threads/memory), `CAST(row_counts AS VARCHAR)`, json -> dict else None; `register_prev_row_counts` filters.
7. M5: non-HernessError (except YieldRequested) from a hook -> `FatalError("build stage <name> failed", error_type=<class>)` raised `from exc` (cause kept, no raw text in message/context/details/logs). HernessErrors pass unchanged. CHECKPOINT between facts and scoring stays outside the wrap (still SchemaViolation via _run_stages).
8. BT02-01: checks are ~40 single-pass aggregate statements over core tables (no joins except EXISTS on enrich/record_id); negligible against the build.

## Carry-overs
- T02-21: IT02-29/IT02-30 "promoted" half (pipeline to promote; IT02-30 "promoted"); ST02-17 (--from-stage promote re-runs dq; empty dq_result blocks — evaluate_gate already raises on empty); promote must call `_build_dq.stage_dq` when dq did not run in this job.
- IT02-31 (T11-19, dirty synthetic lake) not in this card.
- Spec 04 invariant rows: gate counts every producer (tested with a stand-in row).

## Tests
- RED: IT02-28 with 900_dq_checks.sql removed: 7 failed, 2 passed (the two gate/outcome-only tests). M5 with the wrap disabled (except narrowed): 3 failed (enrich, materialize_facts, run_scoring).
- GREEN: card tests (-k IT02_28/29/30) 13 passed; test_model_build_dq + test_model_dq_checks + test_model_build_pipeline with --require-test-ids 38 passed; tests/unit/model + tests/integration/model (unit or integration, not slow) 320 passed, 1 skipped (symlinks) (run before the final constant rename; the touched files were rerun after it: 38 passed).
- Coverage (branch): dq.py 100 %, _build_dq.py 100 %, _build_stages.py 90 % from these files (missing lines are the loader seams covered by unit tests).
- Gates: ruff check PASS, ruff format --check PASS (813), mypy PASS (306), lint-imports 13 kept 0 broken, check_type_ownership 0, check_module_size 0. Full suite not run (per dispatch).

## Fix round 1
- b780911 fix(model): T02-20 correct cast_fail comment — 900_dq_checks.sql cast_fail comment now gives the true reason for summing per (table, column): defensive, keeps the check name unique if several stats rows name one column (staging files delete their own cast_stats rows, so re-runs do not duplicate; decision 5 above overstated this). No behaviour change; IT02_28 9 passed; all hooks passed.
