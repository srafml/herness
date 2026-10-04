# T02-18 report: Build handler and build stage

Status: DONE_WITH_CONCERNS (see Concerns). Worktree agent-a2c7ca69f69fbb045, base de08abc.
Commit: see the final line of this report (updated after the final commit).

## Files (lines / budget)
- herness/model/meta.py 182 / 240 (U02-88..U02-92)
- herness/model/build.py 368 / 400 (U02-95..U02-99: STAGE_ORDER (re-exported), BuildPipelinePayload, SqlRangeResult, run_sql_range, _BuildRun, _stage_build, run_build_pipeline)
- herness/model/_build_support.py 162 / 200 NEW private sibling (module-map row added in docs/impl/02-data-model.impl.md §2, same commit): STAGE_ORDER, resolve_build (U02-98 step 3), delete_orphans (T02-21 stand-in), write_metrics (§8.2), memory_limit conversion (DbSettings, resolve_memory_limit, db_settings)
- tests/support/lake_small.py 452: lake_small row spec, write_lake (real LakeWriter), install, load_config, core_snapshot, `--regen` helper (`PYTHONUTF8=1 uv run python -m tests.support.lake_small --regen`)
- tests/fixtures/lake_small/raw/** (12 parquet files, ~89 KB, synthetic, fixtures-pii-scan clean) + mappings.yaml
- tests/fixtures/golden/lake_small_counts.json, lake_small_core.json (sorted rows; text hashed sha256 -> 16 base32 chars so detect-secrets does not flag hex strings)
- tests: tests/unit/model/test_model_meta.py (77), tests/unit/model/test_model_build_payload.py (106), tests/integration/model/test_model_meta_rows.py (155), tests/integration/model/test_model_build_pipeline.py (389), tests/security/test_st02_build.py (173); relabelled tests/integration/model/test_model_sql_settings.py

## Test IDs -> functions
- UT02-65: test_ut02_65_stage_order, _invalid_payloads (14 cases incl. [build, score], build with ID, enrich_stage without enrich, "Bad Stage"), _valid_payloads ([enrich, score, dq, promote] with ID; [enrich] + enrich_stage="link"), _memory_limit_resolution, _memory_limit_rejects
- UT02-67: test_ut02_67_git_sha_from_env, _git_sha_from_git, _git_missing_is_unknown (PATH empty), _git_failures_are_unknown
- UT02-69: test_ut02_69_dataset_kind (5 cases)
- IT02-21: test_it02_21_lake_small_build (through register_handler/resolve_handler/run_handler; golden counts + core snapshot + meta.build fields + state/heartbeats/metrics), _deletions_are_honoured, _metric_failure_is_logged, _payload_errors; meta row tests _insert_build_row, _insert_build_row_rejects, _update_build_row, _update_build_row_errors, _collect_row_counts, _collect_row_counts_error; relabelled test_it02_21_setup_ddl_schemas_and_tables (U02-107 DDL details)
- IT02-22: test_it02_22_broken_230_fails_build (BuildSqlError names 230_incident.sql, status failed, CURRENT unchanged, no literal, no chained cause), _failure_before_meta_row (mark_failed_error logged and suppressed), _render_failure, _sql_range_bounds
- IT02-27: test_it02_27_yield_leaves_orphan_next_run_deletes_it, _yield_before_first_stage, _yield_inside_setup_files, _resume_after_build_stage, _resolve_build_rules
- ST02-12: test_st02_12_build_opens_no_socket_and_no_autoload (install_socket_guard with a loopback-only allowlist, proved by a blocked example.com connect; recording sys.addaudithook sees zero socket.* events; duckdb.connect wrapped: writer, inspector and reader connections all report autoinstall/autoload false)
- ST02-14: test_st02_14_no_row_values_in_logs_meta_or_errors (sentinels in ticket text + failing timestamp/priority casts; then 230 failing on a literal: no sentinel in captured logs, meta.* of both builds, BuildSqlError text/context/details; db_error shows '?')

## Rulings applied
- memory_limit: percentage -> MiB of psutil total RAM (1..100 %, clamped up to >= 256 MiB); a DuckDB size passes through when >= 256 MiB, else ConfigError; passed to open_for_build as a converted view (DbSettings); herness.store unchanged. Unit-tested under UT02-65.
- make_build_pipeline_handler not built; tests register a one-argument wrapper of run_build_pipeline and go through resolve_handler + run_handler (herness.core.jobs.handlers; jobs/__init__.py untouched).
- Stages dispatched by table (_STAGE_UNITS = {"build": _stage_build}); enrich/score (# T02-19), dq (# T02-20), promote (# T02-21) markers; a payload naming a missing stage is rejected with ConfigError before any work. No BEGIN anywhere: statements autocommit; no transaction across stages or around run_sql_range.
- meta.build: config_hash(cfg), list_watermarks() (values via clock.format_utc), git_sha(env=os.environ, repo_dir=repo root), dataset_kind(cfg.profile, inventory). Union of deleted_record_ids over present entities registered through register_reference_tables after 000-099 and before 100-299 (tested: deleted record absent from core.incident).
- No SQL from payload strings; identifiers only from fixed lists / regex-checked catalog names (meta.collect_row_counts).
- Failure path: HernessError in a stage -> update_build_row(status=failed, finished_at) best effort (errors logged model.build.mark_failed_error, suppressed), model.build.failed, metrics(status=failed), re-raise; CURRENT untouched. BuildSqlError raised `from None` so raw DuckDB text (possible literals) is not chained.
- IT02-22 / ST02-14 SQL dir: tests monkeypatch herness.model.build.discover_sql_files to a tmp copy; no production seam, nothing from payload.
- cast_stats / staging SQL untouched (no defect observed).
- lake_small: card-made committed fixture + goldens + documented regen helper; IT02-21 partial in test_model_sql_settings.py relabelled as the U02-107 DDL-detail part.

## Spec notes
1. build.memory_limit: DuckDB 1.5.5 rejects '%' ("Unknown unit for memory"); converted at build time as above; recorded in the §2 row of _build_support.py.
2. Private sibling herness/model/_build_support.py (module-map row, budget 200).
3. cleanup_builds (U02-105, T02-21) does not exist: _build_support.delete_orphans is a minimal stand-in (status building + finished_at NULL, not CURRENT, not this build -> delete_build_files, log model.build.orphan_deleted), marked `# T02-21:` for replacement; needed for IT02-27 "deletes the orphan".
4. llm_factory is typed `object | None` (impl 03 LlmFactory does not exist in the tree, and L2 cannot import L3); marked `# T02-19:`.
5. JobOutcome result for [build]: dq/enrich/scoring null (not run); yield result = {"build_id"}; finished_at on completion/failure = clock.now() at that moment; started_at = the job's start `now` (also used for new_build_id).
6. BuildSqlError.statement_index is 1-based (0 when extract_statements itself fails).
7. model.build.render_failed is logged with build_id and file (no line field; the line is in the ConfigError message).

## Concerns
- build.py is 368/400 after the split; T02-19..T02-21 (enrich/score/dq/promote units, make_build_pipeline_handler) will need their own sibling module; they cannot all fit in build.py.
- Acceptance check "herness build (T09-23 cmd_data) on lake_small" cannot run: no CLI in the tree; IT02-21 asserts the equivalent (building file with finished_at set).
- ST02-12 observes Python-level sockets and connection settings; DuckDB C++ extension downloads are invisible to audit hooks — covered by autoinstall/autoload false on every connection plus UT02-61's static scan.

## Coverage (tests/unit/model + tests/integration/model + ST02 tests, branch)
- _build_support.py 99 % (1 partial branch), build.py 99 % line before the last test (the one missing line, yield inside 000-099, is now covered by test_it02_27_yield_inside_setup_files), meta.py 100 %.

## Gates
ruff format/check clean; mypy clean (249 files); lint-imports 13 kept; check_type_ownership ok; check_module_size ok. pre-commit (incl. full pytest-unit: 6825 passed) on commit.
Tests: tests/unit/model tests/integration/model tests/security/test_st02_build.py -> 280 passed, 1 skipped (symlinks).
Final commit: b1d3ce2 feat(model): add build pipeline handler and build stage (T02-18)

## Fix round 1 (review T02-18-review.md, 4 Minor)
1. _build_support.resolve_memory_limit: absolute sizes must be finite and within 256 MiB .. total physical RAM, else ConfigError naming build.memory_limit ("99999999999GB", a 400-digit value, "65GiB" on 64 GiB rejected). Test: test_ut02_65_memory_limit_rejects (3 new cases).
2. build._run_stages: duckdb.Error / OSError from a stage (CHECKPOINT, scan_lake, row counts ...) become SchemaViolation("build stage <name> failed", build_id, error_type) raised `from None`; the failure path (mark failed, model.build.failed, failed metric) now runs for them. Test: test_it02_22_non_herness_error_runs_failure_path[IOException, OSError].
3. build._parse_payload: error labels come only from the model's own field names; an unexpected key is reported as "extra field", other unknown locations as "payload". Test: test_it02_21_extra_payload_key_not_echoed.
4. meta.collect_row_counts: skipped tables are logged once as model.build.row_count_skipped (WARNING, schemas + count; no names). Spec note: new log event not in the §8.1 table. Test: test_it02_21_collect_row_counts (log assertion).
Sizes: build.py 381/400, _build_support.py 166/200, meta.py 187/240. Coverage: meta 100 %, build 99 % (1 partial branch), _build_support 99 % (1 partial branch).
Tests: tests/unit/model tests/integration/model tests/security/test_st02_build.py -> 286 passed, 1 skipped. RED before the fix: 7 failed.
Gates: ruff, ruff format --check, mypy, lint-imports, check_module_size, check_type_ownership clean.
Fix round 1 commit: c435761 fix(model): harden build memory limit and failure path (T02-18)
