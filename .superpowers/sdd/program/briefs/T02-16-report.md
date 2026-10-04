# T02-16 report — core incident, change, problem

Worktree: D:\herness\.claude\worktrees\agent-ab7413d200c3b64b0 (branch worktree-agent-ab7413d200c3b64b0, base ce7dd88)

## Built
- `herness/model/sql/230_incident.sql` (56 lines) — `core.incident` per U02-118. service_id = rid(cmdb_ci, business_service), else the single `core.service` that is the parent of a `stg.sn_rel_ci` row whose child is the incident CI (count(DISTINCT service)=1, else NULL; non-service parents ignored). team_id = latest version's assignment group (OI-11). sla_breached = bool_or(has_breached) over task_sla rows matched on `task` = incident `source_key` (sys_id) when any exist, else NOT made_sla. acknowledged_at / customer_impact_minutes passed through from staging (custom-field driven there). content_hash NULL. All columns explicitly CAST so empty lakes keep types.
- `herness/model/sql/240_change.sql` (44 lines) — `core.change` per U02-119; outcome = coalesce(staging outcome, 'canceled' when lower(state) in canceled/cancelled).
- `herness/model/sql/250_problem.sql` (20 lines) — `core.problem` per U02-120.
- `tests/integration/model/_core_build.py` (45 lines): `build_core` gained a `custom_fields` kwarg (passed to the render context).
- Tests: `tests/integration/model/test_model_core_incident.py` (285 lines), `tests/integration/model/test_model_core_change_problem.py` (248 lines).

## Test mapping
- IT02-12: test_it02_12_sla_impact_ack_with_custom_fields, test_it02_12_custom_fields_absent, test_it02_12_no_task_sla_entity (R-60), test_it02_12_columns_and_references (full row + column types, latest assignment group), test_it02_12_empty_lake.
- IT02-13: test_it02_13_service_from_ci_relation (one service / two services / none / business_service wins / non-service parent ignored; re-run idempotent).
- IT02-14: test_it02_14_outcome_mapping_unknown_and_canceled (mapped codes, unknown -> NULL and counted as `stg.cast_stats` (stg.sn_change_request, outcome) failed=2/non_null=6, canceled/CANCELLED state -> canceled, staging outcome wins over state, re-run does not double counts), test_it02_14_change_columns (full row + types, CI-relation fallback, unknown type NULL).
- IT02-15: test_it02_15_problem_columns, test_it02_15_empty_lake.

## RED / GREEN
- RED (tests before SQL): `PYTHONUTF8=1 uv run pytest tests/integration/model/test_model_core_incident.py tests/integration/model/test_model_core_change_problem.py -q -p no:logging` -> 10 failed (core tables missing).
- GREEN: same command -> 10 passed. `tests/integration/model tests/unit/model` -> 207 passed, 1 skipped (symlinks). tests/unit/test_sql_coverage.py 4 passed.
- Gates: ruff format/check clean, mypy 0 issues, lint-imports 13 kept, check_module_size exit 0.

## Deviations / spec notes
- IT02-14 "unknown NULL and counted": per U02-109 and §3.10 Casting ("Unmapped enum values count as cast failures of their column"), the count lands in `stg.cast_stats` written by 110 staging; 240 only consumes the staging outcome. Tested end to end through 240; no staging file changed.
- A known close code on a canceled change keeps its mapped outcome (rule: staging outcome first). An unknown code on a canceled change yields `canceled` (staging outcome NULL -> state rule) while still counted as a cast failure.
- The CI->service fallback CTE is duplicated in 230 and 240 (8 lines) rather than added to `_macros.jinja` (U02-106, not in this card's files).
- Join keys (task, CI child) are trimmed to match `rid()` trimming.
- Commits used SKIP=pytest-unit (KNOWN-RED ST05-13(a) / IT00-01 on this base).

## Concerns
- None blocking.

## Commits
- 6bb3a86 wip(T02-16): failing tests IT02-12..IT02-15
- 80d2801 feat(model): T02-16 core incident, change, problem
