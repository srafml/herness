# T09-04 report — ui_reads ops area (U09-52)

Status: DONE
Commit: 2bb1e9b feat(store): add ui_reads ops area (T09-04) (worktree-agent-ab6109db9504ef8b4, base 6b70189)
Note: resumed from WIP of a previous build agent (killed by usage limit before suite+commit). WIP reviewed against brief, DDL and constraints; found sound, committed without code changes (ruff format/fix made no changes).

## Files
- herness/store/ops/ui_reads.py (new, 349 / budget 350)
- herness/store/ops/__init__.py (+`# 09 ui_reads` import block and __all__ block after `# 10 privacy`, `# isort: split` separator; 286 / budget 400). UT02-68 layout test passes unchanged (8 passed).
- tests/unit/store/ops/test_store_ops_ui_reads.py (new, 22 tests, all UT09-44)

## What was built
Eleven functional-form TypedDicts (UiRunRow, UiTaskRow, UiEvidenceRow, UiRecommendationRow, UiDecisionRow, UiOutcomeRow, UiFindingRow, UiMemoryItemRow, UiResilienceEventRow, UiSourceHealthRow, UiJobLiteRow) with spec 02 DDL column names; 23 ui_* functions exactly as the U09-52 table. Imports only `herness.store.ops.core`, `herness.core.time` (as clock) and stdlib — ops-areas-acyclic kept.

Helpers: `_chunked` (dedupe + sort ids, 500 per statement, placeholders `"?" * len(chunk)`, extra params bound after the ids; empty input runs no statement), `_select` (appends ` LIMIT ?`, clamps limit to 1..500, passes the same value as core.read_all max_rows), `_parsed` (row -> dict, named JSON columns through core.load_json with `table.column` field label).

## Per-function notes
- ui_evidence_ids_present / ui_get_evidence_rows: chunked on evidence.query_id; params, result_sample parsed.
- ui_run_rec_ids, ui_get_recommendation (PK): numbers, confidence_basis, finding_ids parsed.
- ui_finding_ids_with_status (status bound after each chunk), ui_finding_query_ids (non-list JSON -> []).
- ui_finding_status_counts, ui_task_status_counts (ordered role, status).
- ui_list_tasks: projection with json_extract(spec,'$.objective') AS objective; ORDER BY created_at, task_id; status NULL-tolerant.
- ui_list_runs: ORDER BY started_at DESC, run_id DESC; token_usage parsed; cost_usd kept as TEXT.
- ui_list_recommendations: all five filters as `(? IS NULL OR col op ?)`; created_from/created_to are datetimes formatted via clock.format_utc, both inclusive; newest first.
- ui_list_findings_for_entity (DESC), ui_list_run_findings (created_at, finding_id): numbers, query_ids parsed; claim kept as TEXT (it is TEXT, not JSON, in the DDL — the spec's "claim parsed" read as "numbers/query_ids parsed", claim returned raw).
- ui_jobs_for_run: matches json_extract(payload,'$.run_id') OR '$.request.run_id'; newest created_at first; last_error parsed; no payload/result columns.
- ui_latest_decisions: row_number() OVER (PARTITION BY rec_id ORDER BY decided_at DESC, rowid DESC) — decided_at tie goes to the later insert.
- ui_list_outcomes: ORDER BY rec_id, measurement; details parsed; grouped per rec_id.
- ui_list_memory_items (data, provenance parsed), ui_list_resilience_events (detail parsed): NULL-tolerant filters, newest first.
- ui_list_source_health: ORDER BY source, capped 200.
- ui_job_exists (PK), ui_job_status_counts, ui_oldest_queued_age_s (min created_at of queued; clamped >= 0; None when no queued job), ui_failed_jobs_since (finished_at >= format_utc(since), newest first).

## Spec conflicts / ambiguities and resolutions
- `claim` listed among parsed columns for findings, but finding.claim is plain TEXT in 003_runs_evidence.sql: returned as stored.
- Stable tie-breakers (id columns / rowid) added to every ORDER BY for deterministic output; not in spec, no behavioural conflict.
- ui_oldest_queued_age_s clamps negative ages (created_at after `now`) to 0.0.
- No helper needed another area's function; nothing re-implemented from another area.

## Tests
- RED: with ui_reads.py and the __init__ block removed, `pytest tests/unit/store/ops/test_store_ops_ui_reads.py` -> 1 error during collection (ImportError).
- GREEN: 22 passed; coverage ui_reads.py 100% line, 100% branch (132 stmts, 10 branches).
- Covered: 1,200 ids -> statements of 500/500/200 placeholders; duplicates collapsed; empty input runs no query; limit clamping (10_000 -> 500 bound and max_rows 500; 0 / -5 -> 1); source_health 200 cap; NULL-tolerant filters; window-function latest decision incl. tie; both job payload paths; oldest queued age; injection-looking values in every filter return nothing and change nothing (total_changes); AST check for no f-string / `%` / `.format(`; every public name ui_/Ui and re-exported in ops.__all__.
- Gates: ruff check/format clean; mypy 0 issues (144 files); lint-imports 13 kept 0 broken; check_type_ownership 0; check_module_size 0; full suite `-m "(unit or integration) and not slow"`: 3863 passed, 5 skipped, 1 xfailed (pre-existing). Pre-commit hooks all passed.

## Concerns
None blocking. ui_reads.py is at 349/350 lines — no headroom for later additions.
