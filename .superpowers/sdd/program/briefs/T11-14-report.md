# T11-14 report: `generate`, CLI and verify

Status: DONE_WITH_CONCERNS (concerns at the end). Branch `claude/w32-c11-T11-14`, base 08d93ab.
Commits: wip 9e9cd08 `wip(T11-14): generate, main, verify and the card's tests green` (all code, tests and the spec row; every pre-commit hook passed, pytest-unit included); final commit `feat(synth): T11-14 generate CLI, overwrite marker and --verify` (SHA at the end of this file).

## What was built, per unit
- **U11-22 `tools/synth/verify.py`** (212/250): `VerifyReport(ok, problems, row_counts, dirty)` is a frozen dataclass. `verify_root(root)` and `content_hashes(root)` run in an in-memory DuckDB with `TimeZone=UTC` and `allowed_directories=[<root>/data/raw/]`, then `enable_external_access=false` and `lock_configuration=true`. Extension autoload and autoinstall are off. For each `source/entity` it reads `read_parquet($glob, hive_partitioning=true, union_by_name=true)` with glob `<raw>/<s>/<e>/**/[!.]*.parquet`, passed as a bound parameter. The checks are:
  - The 8 metadata columns exist with their spec 02 §3.1 types (VARCHAR ×4, TIMESTAMP WITH TIME ZONE ×2, BOOLEAN, VARCHAR `_payload`).
  - No NULL in the 7 non-nullable metadata columns. This is stronger than the spec, which names only `_record_id`.
  - `_record_id = _source||':'||_entity||':'||_source_key`, and `_source`/`_entity` equal the path.
  - `_deleted = (_payload IS NULL)`: tombstones have a NULL payload and live rows do not.
  - No dot-prefixed file or directory under `data/raw` (Python walk).
  - Row counts equal `truth.row_counts` for the union of lake and truth keys.
  - `duplicate_rows` = rows − distinct `(_record_id,_source_updated_at,_payload)`, `later_versions` = distinct live versions − distinct live ids, and `tombstones` = count of `_deleted`. All three equal `truth.dirty`. On the tiny root this recomputation matches dirty.py exactly (104/88/5).

  Truth is read through `herness.eval.truth.load_truth`. Problems name keys, columns, counts and paths only. `content_hashes` is `md5(string_agg(v, ',' ORDER BY v))` with `v = _record_id || CAST(_source_updated_at AS VARCHAR) || md5(coalesce(_payload,''))`, one hash per `source/entity`.
- **U11-23 `tools.synth_data.generate(seed, scale, root, **overrides)`** follows the steps in order:
  1. Reject unknown override keys with `SynthUsageError`. Defaults follow U11-23, with `workers=os.cpu_count()`. Seed must be an int ≥ 0 and workers an int ≥ 1.
  2. `load_params`.
  3. Root check. `RootNotEmpty(ConfigError)` is declared in synth_data. With `overwrite`, the root must hold a regular, non-symlink `.synth_root`, otherwise `SynthUsageError`. The contents are then deleted: directories with rmtree, while links and files are unlinked and never followed. A refusal logs `synth.generate.refused`.
  4. `build_catalog`, then log `synth.generate.started`.
  5. `plan_shards` and `run_all_shards`.
  6. `write_service_costs` when `files` is in sources, then `write_name_directory` and `write_synth_mappings`.
  7. Build the manifest and run `write_truth`.
  8. Write the `.synth_root` marker as JSON `{generator_version, params_hash, scale, seed}`, tmp-then-`os.replace`.
  9. With `verify`, run `verify_root`. If it is not ok, log `synth.verify.failed` (root, n_problems, first_problem) and raise `SchemaViolation`.
  10. Log `synth.generate.completed`. After step 3, the root is left as is on error.
- **Manifest assembly (`tools/synth/_manifest.py`, 169/180, new private sibling)** builds every Plants field from real data, with no placeholders:
  - **T1:** team `servicenow:sys_user_group:<t1_team>`, its catalog name, 3 services `servicenow:cmdb_ci:<sys_id>` (the `core.service` id form of impl 02 200_org_team_service.sql), `mttr_hours`/`mttr`, 2.0→3.0.
  - **T2:** epic key and `jira:issue:<id_base+seq>` from `plant_range(planned_counts_t2, jira/issue/epic_month)`, built the same way as `epic_issue`. The decoy is seq+1.
  - **T2c:** template `T2C_FAMILY`.
  - **T3:** CI C3; owning team = support team of S3 (the team C3's changes and follow-ups are assigned to); `source_field_share` = the realized share of `caused_by` flags in the change schedule (0.2994 at tiny).
  - **T4:** `agg.generated_noise_ratio`.
  - **T5:** team, service, peak_window, 2.8.
  - **T6:** E6p/E6u keys and ids from `plant_range(planned_counts_t6, …)`, metric `incident_count`, effect −`T6_DROP` (−0.4) and 0.0, `effective_at`.

  `question_set_version` comes from `load_config("synth", paths.data=<root>/data).decisions.question_set_version`, which is `qs-2026-10-01.1`. `dataset_root` = `<resolved root>/data` (posix). I checked by hand that the 4 epic keys/ids match the lake's Epic rows (SEQU-100/30100 "Rework database connection pooling…", SEQU-101 decoy, LANT-102/10102 and SEQU-103/30103 "Reduce incident volume…").
- **U11-24 `main(argv)`** (synth_data.py 250/250):
  - Parsing: the default form uses argparse without `choices=` and without `type=`. Seed, workers and dates are converted by the tool, so a bad value raises `SynthUsageError` and exits 3. `pii-corpus` (`--seed`, `--n` default 5000, `--out`) and `api-pages` (`--seed --source --entity --rows --out`) parse when `argv[0]` names them, and both raise `SynthUsageError("command: <name> is not implemented until T11-15")`, exit 3.
  - Default `--root` is `data/synth/<seed>-<scale>` with `5m`→`full`.
  - Exit codes: `SystemExit` from argparse returns its code (2 for usage errors, 0 for `--help`). `ConfigError` (incl. `SynthUsageError`, `RootNotEmpty`) → 3. Any other exception (`SchemaViolation`, `FatalError`, …) → 1.
  - On failure: `synth.generate.failed` (seed, scale, error_type, exit_code) plus one stderr line `synth_data: <Type>: <message>`. For non-Herness errors the message is only "unexpected error".
  - On success: one JSON line on stdout, `{"root","rows","seconds","params_hash"}`, written with `sys.stdout.write`.
  - `main` calls `configure_logging("INFO")`, so logs go to stderr as JSON and never to stdout.

## Files (lines / budget)
tools/synth_data.py 250/250 · tools/synth/verify.py 212/250 · tools/synth/_manifest.py 169/180 (new §2 row in docs/impl/11-testing-eval-synthetic-data.impl.md, after the verify row) · tools/synth/shards.py 326/350 (+3: worker logging, see deviations) · tests: tests/unit/tools/synth/test_synth_verify.py, tests/unit/tools/test_synth_data.py, tests/integration/tools/synth/test_synth_data_cli.py, tests/security/test_st11_07_overwrite_marker.py. No `__init__.py` was needed: the existing layout has none and the basenames are unique. `check_module_size` exit 0.

## Tests
- UT11-26 (6 functions incl. parametrized dropped column: single-file `_source_key` missing, multi-file `_payload` → NULL live rows; dot file; extra row; content_hashes sensitivity) + rf: record-id mismatch, wrong `_deleted` type, missing truth.
- UT11-27 (non-empty root → RootNotEmpty and files unchanged; overwrite without marker → SynthUsageError, nothing deleted); UT11-28 (overwrite regenerates: stray file gone, identical content_hashes and byte-identical truth.json, equal manifest); UT11-29: valid 0 with exactly one JSON line and no made-up names in stdout/stderr (TH11-07), `--scale huge` 3, non-empty root 3, root corrupted after truth then `--verify` 1 (asserts `synth.verify.failed` and `synth.generate.failed` exit_code 1 via a recording logger), `--bogus` 2. Plus rf: missing `--seed`/option value → 2; bad values (`--seed seven`, `--workers 0`, bad date, `--dirty filthy`, `--fetch-mode hourly`, bad source) → 3 with root untouched; subcommands → 3; default root normalizes `5m`; layout/marker/manifest; plants name lake records; unknown override.
- IT11-01 tiny (main with `--workers 1` vs `--workers 8`, real spawn pool; equal content_hashes) + small variant `slow` (written, not run). IT11-02 tiny (subprocess `python tools/synth_data.py --seed 7 --scale tiny --verify`, exit 0, exactly one stdout JSON line, verify_root ok) + small `slow` (not run).
- ST11-07: `--overwrite` on a dir with a sentinel and no marker → exit 3, tree unchanged, stdout empty, sentinel text not echoed; `.synth_root` as a directory or as a symlink does not authorize deletion.
- Unit tests swap `run_all_shards` for an in-process runner (unit marker: no subprocess; same pattern as T11-13's truth_writer test). The spawn-pool path is covered by IT11-01, IT11-02 and the acceptance run. In-process tiny generation takes ≈2.3 s, the same as the 4-worker pool.

RED (before implementation): `uv run pytest tests/unit/tools/synth/test_synth_verify.py tests/unit/tools/test_synth_data.py tests/security/test_st11_07_overwrite_marker.py tests/integration/tools/synth/test_synth_data_cli.py -q -p no:logging -m "not slow"` →
```
E   ModuleNotFoundError: No module named 'tools.synth_data'   (x3 collection errors)
E   ModuleNotFoundError: No module named 'tools.synth.verify'
4 errors during collection
```
Second RED (IT11-02 before the worker-logging fix): `assert len(lines) == 1` failed, because the spawn workers' unconfigured structlog printed `config.validate.issue` warnings (31 KB) to the inherited stdout.
Third RED (first wip commit's pytest-unit hook, whole unit suite): `test_ut11_29_corrupted_root_with_verify_exits_1` asserted `"synth.verify.failed" in capsys.err`, and that is order-dependent under the full suite's logging state. I fixed the test by recording `synth_data._log` calls instead; the code was unchanged. The re-commit passed.

GREEN:
- Card tests: `uv run pytest <4 card files> -q -p no:logging -m "not slow"` → 36 passed, 2 deselected in 25.8 s. After merging the TH11-07 check into UT11-29 the count is 35 functions; IT11-01 tiny 7.9 s, IT11-02 tiny 4.1 s.
- `uv run pytest tests/unit/tools tests/integration/tools tests/security/test_st11_07_overwrite_marker.py -q -p no:logging -m "not slow"` → 356 passed, 2 deselected in 65 s. I added `-m "not slow"` so that the small variants do not run.
- `uv run pytest -m unit -x -q tests/unit/tools tests/unit/eval tests/unit/core tests/security/test_st11_07_overwrite_marker.py` (logging plugin on) → 2795 passed in 225 s.
- Pre-commit pytest-unit (whole unit suite) passed on wip 9e9cd08. The first attempt reported 1 failed and 10282 passed in 26.5 min; that failure is the third RED above.
- `--require-test-ids --collect-only` on the 4 files: 37 collected, no ID error. `-k "UT11_26 or … or ST11_07"` selects 20.

## Acceptance
`uv run python tools/synth_data.py --seed 7 --scale tiny --verify --root /tmp/w32-c11/builder/acc` exited 0 in three runs, with wall times 4.17 s, 4.37 s and 4.21 s (bash `time`/`date`; the first measurement was 4.06 s). That is under 5 s on 4 CPUs with the default 4 spawn workers. The `seconds` field in the summary reads ≈3.0 s; the remaining ≈1.1 s is interpreter, uv and import start-up. Of the ≈3.0 s, about 2.2 s is the shard pool (spawn and worker imports included), and truth writing, the two synth config loads (~0.13 s each) and verify (~0.35 s) take the rest.
Tiny root: 0 dot-prefixed files under data/raw. `servicenow/cmn_department` holds 3 rows and `servicenow/task_sla` 1376 rows (dt=2026-09-01). Row counts by entity: jira/issue 163, monitoring/event 430, monitoring/metric_daily 7270, change_request 331, cmdb_ci 91, cmdb_ci_service 20, cmdb_rel_ci 70, incident 1551, problem 41, sys_user_group 15, for 11361 rows in total. dirty: bad_timestamp 4, future_ts 1, resolved_before_opened 1, missing_service 125, duplicate_rows 104, later_versions 88, tombstones 5, unknown_enum 1.
I also ran these by hand with `--verify`, and all exited 0: `--dirty heavy`, `--dirty none`, `--fetch-mode daily`, `--sources jira,monitoring`, `--sources servicenow --seed 42`. Error paths by hand: `--scale huge` → 3 ("scale: must be one of …"), non-empty root → 3 (refused plus failed events), `--bogus` → 2, `pii-corpus` → 3, `--params /nonexistent.yaml` → 3. stdout was empty in every error case.

## Gates
ruff format (no change), ruff check (all passed), mypy (no issues, 389 files), lint-imports 15 kept / 0 broken, check_type_ownership exit 0, check_module_size exit 0. detect-secrets passed in the hook with no baseline change.

## Deviations
1. **tools/synth/shards.py `_init_worker`** (a T11-13 file) now calls `configure_logging("ERROR")` in each spawn worker before `init_config`. Without it, structlog's defaults in the workers print the synth config warnings to the inherited stdout, and the CLI's one-line JSON summary (U11-24) would be impossible. ERROR is used because the parent logs the same config warnings once. Worker logging at ERROR level lets any future `synth.shard.failed` (spec §7, T11-13 carry-over) through. Correction (fix round 1): no such event is emitted anywhere in tools/synth/ today.
2. **Exit code for a failed `--verify` = 1** (U11-24, F11-01 step 10, UT11-29, R-46). U11-22's Errors cell and F11-03 still say "exits 3". That is a spec inconsistency for the controller to fix in the text.
3. **Every `ConfigError` → 3** (U11-24, R-46), including one raised in write_truth/synth redactor setup, where F11-01 step 8 says exit 1.
4. **The `--seed`, `--workers`, `--start` and `--end` values are converted by the tool, not by argparse `type=`**, so a bad value exits 3 (R-46: only unknown option or missing value is 2). A missing required `--seed`/`--scale` is detected by argparse and exits 2.
5. **`_manifest.py` private sibling** (budget 180) holds the manifest assembly, the qs-version load and the marker writer. The `.synth_root` constant `MARKER` lives there and is re-exported by synth_data.
6. **`content_hashes` "ORDER BY 1"** is read as ordering by the aggregated value (`ORDER BY v`), because `ORDER BY 1` inside an aggregate would order by the constant 1. The separator is ',' and timestamps are DuckDB's VARCHAR of TIMESTAMPTZ in UTC. UT11-30 (T11-15) uses this same function, so this is consistent.
7. **Fewer wip checkpoints than asked.** There is one wip commit with everything, because the 27-minute unit hook made per-unit checkpoints expensive.

## Spec notes for later cards
- **T11-15**: the `pii-corpus`/`api-pages` parsing stays in `_sub_parser()` in tools/synth_data.py. `_run` raises `SynthUsageError("command: <name> is not implemented until T11-15", key="command")`; replace that with the calls to `write_pii_corpus`/`write_api_pages` and validate `--n`/`--rows`/`--source` as `SynthUsageError` (exit 3). The `test_rf_subcommands_parse_and_exit_3_until_t11_15` test must then change. synth_data.py is at 250/250, so T11-15 needs either a budget increase or more code in the new modules. For example, 6 lines could move into `_manifest`, or the parser helpers could become a private sibling.
- **T11-15 fixtures**: `truth.json` `dataset_root` is the absolute `<root>/data` path of the generating machine, so committed `tests/fixtures/truth/7-tiny/` would carry it. Strip or relativize it when copying, or rule on the field's semantics (DD11-06 is still open).
- **T11-16**: `truth_writer.write_synth_mappings` writes `service_overrides.service_id` as `servicenow:cmdb_ci_service:<sys_id>`, but `core.service.service_id` (and the truth plant ids here) use `servicenow:cmdb_ci:<sys_id>`. Reconcile when wiring the synth profile.
- T3 `owning_team_id` = support team of S3; `source_field_share` = the realized share (≈0.3, not the constant). T6 effects: −0.4 and 0.0.

## Concerns
- synth_data.py sits exactly at its 250-line budget, which leaves T11-15 no room.
- The shards.py worker-logging change touches a T11-13 file (deviation 1).
- The spec text inconsistencies on exit codes (U11-22 Errors, F11-03, F11-01 step 8) were resolved toward U11-24/R-46 and still need correcting in the spec.
- The small-scale IT11-01/IT11-02 variants are written but were not run here. Small-scale verify of dirty re-computation (e.g. dimension duplicates) is therefore unexercised.

## Final commit
31e311c `feat(synth): T11-14 generate CLI, overwrite marker and --verify`. It is an empty closing commit: the code is in wip 9e9cd08 and the report lives outside the repo. Every pre-commit hook passed, pytest-unit included. Working tree clean.

## Fix round 1 (review T11-14-review.md, controller-selected Minor findings; test-only)
Production files are unchanged. tools/synth_data.py, verify.py, _manifest.py and shards.py are byte-identical to 31e311c.
- **M1** `test_ut11_26_content_hashes_react_to_payload`: rewrites one live row's `_payload` in `servicenow/problem` (still a JSON object, same record id and timestamp). Only that entity's hash changes.
- **M2** `test_ut11_29_unexpected_error_exits_1_without_its_text`: `generate` is patched to raise `ValueError("<planted ticket text with a name>")`. The test checks exit 1, empty stdout, `ValueError` named in stderr, and the text absent from stdout and stderr.
- **M4** `test_ut11_26_dot_prefixed_directory_is_a_problem`: an empty `data/raw/servicenow/incident/.tmp/` directory yields a dot-prefixed problem.
- **M5** renamed `test_rf_record_id_mismatch_and_live_null_payload_are_problems` → `test_rf_record_id_mismatch_is_a_problem`, with a matching docstring. Live NULL payload stays covered by the parametrized dropped-`_payload` UT11-26 case.
- Report: the Deviations 1 sentence about `synth.shard.failed` is corrected.

RED per mutation. Each mutation was applied by hand, the new test was run, then `git checkout -- <file>`; afterwards `git status --short` showed only the two test files.
- M1 (`md5(coalesce(_payload, ''))` → `md5('')` in verify.py): `FAILED test_synth_verify.py::test_ut11_26_content_hashes_react_to_payload`, `AssertionError: assert '4b89e4ff656f213acc381b828155b676' != '4b89e4ff656f213acc381b828155b676'`
- M2 (`"unexpected error"` → `str(exc)` in synth_data.py): `FAILED test_synth_data.py::test_ut11_29_unexpected_error_exits_1_without_its_text`, `'Jane Example repor...tage in ticket text' is contained here: ...ValueError: Jane Example reported the checkout outage in ticket text`
- M4 (dot walk restricted to files, `if p.is_file()`): `FAILED test_synth_verify.py::test_ut11_26_dot_prefixed_directory_is_a_problem`, `AssertionError: assert True is False` (`VerifyReport(ok=True, problems=())`)

GREEN: `uv run pytest tests/unit/tools/synth/test_synth_verify.py tests/unit/tools/test_synth_data.py tests/security/test_st11_07_overwrite_marker.py tests/integration/tools/synth/test_synth_data_cli.py -q -p no:logging -m "not slow"` → 38 passed, 2 deselected in 23.89 s. ruff format and check are clean on the touched files; mypy reports no issues (389 files).

Fix round 1 commit: 2992600 `test(synth): T11-14 fix round 1 — content_hashes payload sensitivity, error hygiene, dot directories` (all pre-commit hooks passed, pytest-unit included; `git diff --stat 31e311c..HEAD` touches only the two test files).
