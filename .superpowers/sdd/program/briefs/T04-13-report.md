# T04-13 report — Scoring runner, validate and metrics steps

Status: DONE_WITH_CONCERNS — final commit 6f88f5b feat(metrics): scoring runner, validate and metrics steps (T04-13); all pre-commit hooks passed incl. pytest-unit

## Built
- `herness/metrics/scoring.py` (368/390): `STEPS`, `run_scoring` (U04-56), `missing_required_columns` + `_validate_step` (U04-57), `run_metrics_step` (U04-58), re-export of `run_check_step`, `ScoringReport`. Step table `_STEP_FUNCS`: funding/org/levers/portfolio = `None` with `# T04-21:` markers; a requested unbuilt step -> report warning "step <s> is not available yet; skipped" + log `metrics.scoring.step_unavailable`, never checkpointed.
- `herness/metrics/_scoring_checks.py` (111/130, NEW private sibling; module-map row added to docs/impl/04 §2 in the same commit): `CHECKS` table (name, severity, input tables), `existing_tables` (information_schema pre-check), `run_check_step` (U04-59).
- `herness/metrics/sql/checks.sql.j2` (47/200): the 7 base checks in U04-60 order (score_fact_duration_negative, score_metric_ratio_range, score_metric_pct_range, score_metric_negative, score_metric_count_integral, score_evidence_coverage [metrics.* part], score_work_item_cycle); `-- T04-21:` marker in the file header (before the first `-- @check`, so it never reaches recorded SQL). Only bind: `s_count_metrics`.
- docs/impl/04 §2: row for `_scoring_checks.py` (budget 130).
- Tests: tests/unit/metrics/test_metrics_scoring.py (583 lines, 29 tests: UT04-110, -111, -112, -113, -114, -115, -116, -118, -121), tests/fault/metrics/test_metrics_scoring_fault.py (216 lines; FT04-01 metrics part, FT04-03 x2), helpers tests/support/metrics_scoring.py (84), tests/support/scoring_kill.py (85, FT04-01 child).
- pyproject.toml: no contract change needed (lint-imports 13 kept).

## Numbers provenance
- Every metric row: `run_recorded(..., "metrics", into=IntoSpec("metrics.metric_value","append","query_id"))` after `CREATE OR REPLACE TABLE` with the U04-58 DDL, all inside the step transaction.
- Check rows: value/n_bad copied from the recorded check SELECT (producer "score"); `passed` computed by DuckDB in the INSERT (`coalesce(v = 0, false)` ... RETURNING passed). `score_metric_disabled` value = `len(<bound list>)` computed in SQL, details via `json_object`. Report `row_counts` from `SELECT count(*)`.
- Checkpoint: `{**state, "scoring": {build_id, config_hash, steps_done}}` with `config_hash(cfg)`; mismatched build/hash or malformed state -> rerun.
- Transactions: validate's dq writes in their own txn; each step BEGIN/COMMIT, ROLLBACK on any exception; QueryError/duckdb.Error -> SchemaViolation("<step> failed: ..."); log step_failed. FT04-03 shows no partial table/evidence visible.
- Missing input tables for a check -> passed=true, details {"skipped": true}, value NULL (information_schema pre-check).
- No herness.store._warehouse_rw import (UT04-121 AST scan).

## Deviations / rulings applied
- w21/w22 rulings applied verbatim (None step entries, 7 base checks, pre-check, _build_stages.py untouched, FT04-01 kill during `check` via child-process OS kill; the child holds the check transaction open after the real checks so the kill is deterministic — test harness only, no fault point).
- DEVIATION (safety): when `check` returns failed error checks, the dq rows are committed but `check` is NOT checkpointed before raising `SchemaViolation("scoring invariants failed: ...")`. U04-56's literal order (steps 8 then 9) would checkpoint it, so a job retry would skip `check` and could promote a violating build.
- `yielded` goes into `ScoringReport.flags`; should_yield() is consulted only after an executed step when more steps remain.
- `report.steps_done` = ["validate", *done steps in STEPS order].
- Warn-severity failures (score_work_item_cycle) -> report warning + `check_failed` at WARNING + counter; only error checks go to failed_checks.
- Observation: a check's query_id is fixed per (SQL, params, build); re-running a check after the same build's data changed raises the evidence hash conflict (TH04-06 behaviour of run_recorded). Real reruns see identical data, so this only constrains tests.

## Tests / gates
- RED: without scoring.py, test_metrics_scoring.py fails at collection (ImportError).
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/metrics tests/fault/metrics/test_metrics_scoring_fault.py -q -p no:logging` -> 630 passed (241 s). tests/unit/model/test_model_build_handler.py + test_import_contracts: 7 passed (UT02-78 loader now resolves the real run_scoring).
- Coverage (card tests): scoring.py 100% line/branch; _scoring_checks.py 100% line/branch.
- ruff check/format clean, mypy 0 issues, lint-imports 13 kept, check_type_ownership 0, check_module_size 0.
- Test speed: unit tests memoize `validate_catalog` per catalog object and use a 3-metric catalog; UT04-110/UT04-118 share one shipped-catalog run per module (~24 s).

## Concerns
- The first `wip(T04-13)` commit did not land (pre-commit's full unit hook ran 18 min; the commit was not created, cause unclear from truncated output — likely the module-size hook before the §2 row existed). Everything is in the single final commit.
- Report copied to D:herness.superpowerssddprogrambriefsT04-13-report.md with cp (heredoc form was refused by worktree isolation).

## Carry-overs
- T02-19 owner: `stage_score` ignores the report `yielded` flag (must return "yield" when "yielded" in report.flags); replace `_hook` loader with the plain lazy import (`# T04-13:` marker in _build_stages.py) — not edited here.
- T04-21: swap the None step entries; add the score.* checks + score.* half of score_evidence_coverage (CHECKS table + checks.sql.j2 segments); FT04-01 funding half; ST04-06.
- T04-22: IT04-01 / IT04-11.

## Fix round 1 (review briefs/T04-13-review.md), base 6f88f5b
1. Needs-fixes, hash conflict on a same-build retry after a config fix: `run_check_step` now renders each check with the template context `{"config_hash": config_hash(get_config())}`. render_named puts the context into `params.template`, so the value is part of the impl 00 query_id (sql, params, build_id). Evidence is therefore per (build, check, config). No evidence rows are cleared, and run_recorded's hash-conflict rule is unchanged: within one build and config the check inputs are deterministic. New test `test_ut04_115_same_build_retry_after_config_change` uses the real `load_config` (repo config/, second config via `--set weights.cost_per_engineer_hour.value=150`) and the real `config_hash`. The steps it checks:
   - check fails;
   - retry with the new config;
   - the checkpoint resets and metrics reruns;
   - check passes with a new query_id;
   - both evidence rows exist, each holding its own config_hash in params.template.

   Only the catalog stays the small test catalog: the hash covers the whole config, and the catalog only decides which metrics run.
2. Minor 1: validate-step DuckDB errors (the required-column reads and the dq writes) become `SchemaViolation("validate failed: ...")` and log `metrics.scoring.step_failed` with step=validate. The test was renamed to `test_ut04_121_validate_write_error_is_schema_violation`, and a read-error test was added.
3. Minor 2: new `_rollback` helper, used by the step runner and the validate write. A failing ROLLBACK is logged as `metrics.scoring.rollback_failed` and added as a note on the original exception, which stays the one raised. Covered by `test_ut04_121_rollback_failure_keeps_original_error`.
4. Minor 5: `steps` given as a bare str raises `ConfigError("unknown scoring step <str>")`. Covered by `test_ut04_111_bare_string_steps_rejected`.
- Spec note / deviation (Minor 4): `ctx.should_yield()` is consulted only between steps, after an executed step while more requested steps remain. It is not consulted after the last step, where yielding would only repeat a finished run.
- Carry-over to the T02-19 owner, second item: `build._save_state` / `_yield` (build.py:315-320) replace the whole job state, which wipes the scoring checkpoint (`state["scoring"]`). run_scoring merges `{**state, "scoring": ...}`, but the build runner's later saves drop it.
- Sizes: scoring.py 390/390, _scoring_checks.py 118/130.
- Tests: `uv run pytest tests/unit/metrics tests/fault/metrics/test_metrics_scoring_fault.py` gives 634 passed. Coverage for scoring.py and _scoring_checks.py is 100% line and branch. ruff, format, mypy, lint-imports (13 kept), module-size and type-ownership are clean.

## Fix round 2 (re-review round 1, Important 1), base 7850c4d
- **Input-scoped check query_ids.** `run_check_step` now renders each check with the context `{"inputs": "in_" + 16 hex}`. DuckDB computes the value: sha256 over the comma-joined, sorted, distinct `query_id`s of that check's input tables, which are the CHECKS entry's tables (`_inputs`, _scoring_checks.py). render_named copies the context into `params.template`, so it is part of the impl 00 query_id (sql, params, build_id).
  - Every input row is pinned by its own query_id through run_recorded's evidence hash rule, so a check's query_id pins its exact inputs. A rerun over the same inputs is deterministic. A rerun over other inputs records new evidence instead of "nondeterministic result". No evidence rows are cleared.
  - The digest is taken after the information_schema pre-check, so only for present tables.
- **config_hash removed from the check context.** The digest makes it redundant. This also resolves re-review Minor 1: config is no longer read twice per run, because `_scoring_checks` no longer calls get_config/config_hash. The catalog-derived bind (`s_count_metrics`) is already part of the params.
- **Input tables without a query_id column.** Every U04-60 base check reads only fact tables, closures and `metrics.metric_value`, and all of them carry `query_id`. `score_evidence_coverage` also reads `meta.evidence`, but only as a membership test for those pinned query_ids. Evidence rows are never deleted or edited, so for fixed inputs its result is fixed too. T04-21 must give each score.* check its input tables in CHECKS. The score.* tables carry `query_ids` lists, so `_inputs` will need the list column (for example `unnest(query_ids)`). Noted as a T04-21 carry-over.
- **Spec note / deviation (re-review Minor 2).** U04-59 step 2 says `render_named("checks:<name>", {}, ...)`. T04-13 passes `{"inputs": <digest>}` instead. Reason: with `{}` the check query_id is fixed per build, while `metrics.metric_value` can be rewritten under another config inside the same building build (partial `score_steps`). A later check run would then hit the TH04-06 hash conflict forever, or silently record a result under another config's label. The digest is spec-neutral otherwise: same evidence rules and no rows cleared.
- **Tests.**
  - `test_ut04_115_partial_steps_across_configs`, with real `load_config` / `config_hash` and per-config catalogs (B lowers `change_failure_rate.min_sample_size`, so B's metric query_ids differ). Config A full run, then B `steps=["metrics"]`, then A `steps=["check"]`: it succeeds with a new check query_id, and that query_id's `params.template.inputs` equals the digest recomputed in Python from the metric_value it read. The fact-check digest equals the digest of the fact tables. A B `steps=["check"]` run over the same inputs reuses the same query_id (no pass recorded over inputs it did not read).
  - `test_ut04_115_same_build_retry_after_config_change` was rewritten for the digest. The check fails, the config fix reruns metrics under new query_ids, and the check passes under a new query_id pinned to those inputs.
- **Sizes:** _scoring_checks.py 127/130, scoring.py 390/390 (unchanged).
- **Test run:** `uv run pytest tests/unit/metrics tests/fault/metrics/test_metrics_scoring_fault.py tests/unit/model/test_model_build_handler.py` gives 641 passed. scoring.py and _scoring_checks.py have 100% line and branch coverage. ruff, format, mypy, lint-imports (13 kept), module-size and type-ownership are clean.
- **Remaining limit:** if rows are edited in place while their query_id stays the same (tampering, as in the UT04-115 corruption tests), a second check over those same query_ids conflicts. That is intended TH04-06 behaviour.
- **Out-of-card carry-over (owners: facts card / T02-19).** Re-running `enrich` on an existing `building` build_id with changed LLM output or `depth` makes stage 400 hit its own hash conflict (facts.py:114-117), and that build can never score again.
- **Carry-over to T04-21:** CHECKS entries for score.* checks need their input tables. `_inputs` reads a `query_id` column, so score tables with `query_ids` lists need the list unnested.
