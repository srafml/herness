# T02-20 review (DQ checks and gate) — verify agent

Worktree agent-a0c0a65062a11a6a1, head 8e01bf7, base 3f61b67. Read-only review; tests/gates re-run by the reviewer.

### Spec Compliance
- ✅ U02-93 `DqOutcome` (herness/model/dq.py:26-38): frozen, slots, fields as spec; invariant `passed <=> failed_errors == () and checks > 0` enforced in `__post_init__` (ValueError); tested (test_model_dq_checks.py `test_it02_28_outcome_invariant`).
- ✅ U02-94 `evaluate_gate` (dq.py:48-84): SELECT per spec step 1; zero rows -> `SchemaViolation("no DQ results for build")`; errors/warnings split by severity; `model.dq.evaluated` INFO with counts; one `model.dq.check_failed` per failure at ERROR/WARNING. Extra fail-closed cases (unknown severity, NULL passed, unreadable table -> SchemaViolation, `from None`) are stricter than spec and consistent with TH02-17 — accepted.
- ✅ U02-102 stage dq (herness/model/_build_dq.py:51-71): steps 1-7 in order (connection, prev counts from CURRENT via `open_readonly` only when CURRENT != this build, `register_prev_row_counts`, 900-999, row_counts over core/enrich/metrics/score written to meta.build, gate, fail -> `update_build_row(status="failed", finished_at)` + `DqGateFailed`). CURRENT never touched. `_run_stages` failure path then also runs `_fail` (logs `model.build.failed`, metrics).
- ✅ U02-127 900_dq_checks.sql: every check family of the U02-127 table present (12 row_count_drop, incident_service_null with severity switch, work_item_service_null, cast_fail per cast_stats column with non_null > 0, 11 future_timestamp, resolved_before_opened, 9 duplicate_key, decision_coverage_incident, metric_daily_unmapped_service); `dq900` producer rows deleted first, other producers kept. All statements static: table/column names are template constants through `ident`/`sqlstr`; thresholds only `dq.<field> | num` (DqSettings); no data value becomes SQL text (the only data-derived strings are cast_fail check *names*, built as string values, not SQL). INSERT without column list matches meta.dq_result column order (000_settings.sql:36-43).
- ✅ IT02-28 (test_model_dq_checks.py): empty-build baseline, crafted failing rows per check family, incident_service_null severity boundaries (warn pass / warn fail / error), thresholds from config, re-run idempotence + spec 04 row counted, fail-closed gate, outcome invariant. Values, thresholds, severities, passed and details asserted.
- ✅ IT02-29 (test_model_build_dq.py:111): lake_small, previous build CURRENT with 10 incidents, new lake 9 -> `DqGateFailed` with exactly `row_count_drop:core.incident`, value 0.1 / threshold 0.05 / details prev 10 cur 9, new build `failed`, CURRENT unchanged, ERROR check_failed + model.build.failed logged. "Pipeline to promote" half = T02-21 carry-over per ledger ruling (w22-s02).
- ✅ IT02-30 (test_model_build_dq.py:150 +2): lake_small warn-only failure (decision_coverage_incident) -> gate passes, warnings logged at WARNING, build not failed (completed unpromoted), `dq` + row_counts in result and meta.build. "promoted" half = T02-21 carry-over per ruling.
- ✅ M5 (herness/model/_build_stages.py:85-95, 132-141, 164-168): `_hook_errors` wraps run_enrichment (YieldRequested passthrough), materialize_facts, run_scoring; non-HernessError -> `FatalError("build stage <name> failed", error_type=<class>)` `from exc`; HernessErrors unchanged. Regression tests assert type, message, error_type, `__cause__`, no hook text in error/context/details/logs, build `failed`; they cannot pass without the wrap (the `_run_stages` generic path only maps duckdb.Error/OSError to SchemaViolation).
- ✅ Module map / size / layering: build.py 398/400; _build_dq.py 71/120 with new §2 row in the same commit (docs/impl/02-data-model.impl.md:92); _build_stages.py 190/200 with amended row; dq.py 84/120; 900 SQL 149/400. lint-imports 13 kept 0 broken; check_module_size exit 0.
- ✅ Test IDs on every new test function and first docstring line; `pytestmark = integration` set.
- ⚠️ ST02-17 (--from-stage promote re-runs dq) and TH02-17's "DQ always re-run before promotion" depend on T02-21 (promote must call `_build_dq.stage_dq` when dq did not run in the job) — carry-over recorded in report; not verifiable here.
- ⚠️ IT02-31 (T11-19) not in this card.
- ⚠️ BT02-01 cost not measured (bench not run). By inspection: ~40 single-pass aggregates, one 1-row CROSS JOIN (dq_start) per future_timestamp branch, EXISTS semi-joins on record_id for coverage, count(DISTINCT row(...)) on metric_daily; no pathological cross joins. Note BT02-01 covers 000–299 only; the dq stage is outside that budget anyway.

### Deviations judged
1. Stage returns `StageStatus` not `DqOutcome`: accepted — the stage table contract is StageStatus and 900–999 can yield; outcome carried in `result["dq"]` (U02-98 step 8 keys) and documented in the §2 row.
2. Keyword-only `build_id` on `evaluate_gate`: accepted — positional `con` unchanged, optional, only adds §8.1 log field.
3. Extra fail-closed cases: accepted (stricter, TH02-17).
4. SQL readings: cast_fail name `cast_fail:stg.<t>.<c>` follows the macro's `'stg.<table>'` table_name (spec §cast_stats row) — accepted; row_count_drop with table missing from prev -> prev 0, value 0 — matches "else 0"; duplicate_key formula verbatim — accepted with Minor note below.
5. M5 tests labelled IT02-25/IT02-26: accepted — they test U02-100/U02-101 (the rows' units) and follow existing practice (`test_it02_25_missing_hook_fails_build` in test_model_build_enrich_score.py:362).

### Strengths
- Clean, fully static SQL with a small `share` macro; one row per check guaranteed and asserted.
- Gate fails closed on every malformed input, never leaks driver text (`from None`).
- Strong tests: exact value/threshold/details assertions, log-level assertions, secret-text leak checks for M5.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
- herness/model/sql/900_dq_checks.sql:67 — comment "repeated stats rows are summed" and report note 5 say cast_stats re-runs duplicate rows; staging files already delete their own cast_stats rows first (110_stg_servicenow.sql:211-215). The sum is harmless but the rationale is inaccurate.
- herness/model/sql/900_dq_checks.sql:106-110 — duplicate_key: a single NULL single-column key counts as one "duplicate" (count(DISTINCT) skips NULL), whereas metric_daily's `row(...)` form does not treat NULL parts that way; verbatim to the spec formula, but the check name then over-reports (a NULL key is a different defect). Worth a spec note.
- herness/model/sql/900_dq_checks.sql:85 — `max(started_at)` over meta.build: with no meta.build row started_at is NULL and all future_timestamp counts are 0 (silent pass). Unreachable in the pipeline (stage build inserts the row), fine for now.
- herness/model/_build_dq.py:68 — build marked failed here and again by `_fail` (build.py:327-335); harmless duplicate update.

### Gates run by reviewer
- Card tests `-k "IT02_28 or IT02_29 or IT02_30 or IT02_25 or IT02_26"` tests/integration/model --require-test-ids: 26 passed.
- tests/unit/model + tests/integration/model (-m "not slow"): 320 passed, 1 skipped (symlinks).
- ruff check: pass; ruff format --check: 813 formatted; mypy: 0 issues (306 files); lint-imports: 13 kept, 0 broken; check_module_size: exit 0.

### Assessment
**Task quality:** Approved
**Reasoning:** All four units and IT02-28..30 (through stage dq, promote half carried to T02-21 per ruling) match the spec; SQL is static and config-driven, the gate fails closed, M5 wrap is correct and regression-tested; only minor comment/semantics notes remain.
