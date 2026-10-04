# T11-25 report: Golden suite model, loader and resolution

Status: DONE. Commit c371268 `feat(eval): golden suite model, loader and resolution (T11-25)` on the worktree branch (base ee452a6). All pre-commit hooks passed.

## Built
- `herness/eval/golden.py` (400 lines, budget 400, `tools.check_module_size` exit 0)
  - U11-53 models (`extra="forbid"`): `Suite` (`version: Literal[3]`), `SuiteDefaults`, `EvalQuestion`, `Expected`, `NumericExpected` (`unit` = `herness.metrics.settings.Unit`, the same 11-value literal as `NumberRef.unit`), `Tolerance` (exactly one of abs/rel), `EntitiesExpected` (check pattern verbatim), `RulesExpected` (with DD11-08 `max_number_refs`, `number_signs`), `MentionRule`, `ClaimRule` (pattern compiled with `re.IGNORECASE` at validation; `.regex` property), `SignRule`, `RubricExpected`, `ReferenceResult`, `ResolvedQuestion`. `Expected` rejects "nothing set" and merges top-level `must_mention`/`must_not_claim` into `rules` (then clears the top-level lists so a grader cannot count them twice).
  - U11-54 `load_suite(path)`: reads at most 1 MB + 1 byte (no big read into memory), parses with `herness.eval.scripted._parse` (safe loader plus the `MAX_EXPANDED_NODES` alias budget), applies `defaults.datasets` / `defaults.tolerance` to questions/numeric blocks that omit them, validates, rejects duplicate ids, sets `sha256` to the SHA-256 of the file bytes. Every failure is a `ConfigError`. Validation errors name the question (`question_id` context plus message; `#<index>` if it has no id). Hints come from `exc.errors(include_input=False)`, so input values never appear.
  - U11-55 `resolve(...)` (signature as specified): dataset skip (`dataset`, `truth_ref_on_real`). Each reference block (placeholders_sql, entities, numeric) goes through `SqlGuard.check` -> `query_id(sql, {}, build_id)` -> `cache` -> `_run_sql` on a per-call `con.cursor()`, with a `threading.Timer(QUERY_TIMEOUT_S, cur.interrupt)` and `fetchmany(MAX_REFERENCE_ROWS)`. Then the truth_ref check and placeholders. Every failure is a `SuiteError(reason, hint=..., question_id=...)`. The reasons are: `reference SQL rejected` / `timed out` / `failed`, `empty reference`, `truth_ref mismatch`, `unresolved placeholder`, `no display name for entity`, `truth manifest not available`, and plant_value's own messages.
- `herness/eval/truth.py`: docstring only. `SuiteError` stays defined here, because golden imports truth and moving it would create a cycle. golden re-exports it (`from herness.eval.truth import SuiteError`, listed in `__all__`), so there is still one class and every existing import keeps working.
- Tests: `tests/unit/eval/test_golden.py`, `tests/unit/eval/_golden_fixtures.py` (in-memory DuckDB stand-in warehouse and a truth manifest from the `test_truth` payload), `tests/security/test_st11_golden.py`.

## Decisions / deviations
1. **SqlGuard construction.** The spec names `herness.harness.tools.SqlGuard`; the tree has `herness.harness.sql_guard.SqlGuard`. It needs a schema map and blocked columns, and `resolve` only gets a connection. So golden builds the schema from `information_schema.columns` on the cursor, restricted to `ALLOWED_SCHEMAS`, and uses the default `SqlSettings().blocked_columns`. It leaves out schemas with no tables. Reason: an empty schema entry made sqlglot's `MappingSchema` fail depending on frozenset iteration order, i.e. per hash seed. That caused flaky failures until fixed. The guard is rebuilt on each `resolve` call.
2. **`{T...}` display names.** `owning_team` gets a display-name lookup as well as `team`/`service`/`ci`/`org` (O07 "{T3 owning team}" shows a team). The spec lists only the four attributes; this adds one. The lookup order is core.team.name -> core.service.name -> core.org.name -> core.work_item.key (`? IN (record_id, key)`). Missing display tables are skipped. If nothing is found, the result is a `SuiteError` rather than silently using the raw id. `ci` resolves only if the CI id is in one of those four tables (no `core.ci` table exists). List plant values (e.g. `peak_window`) are joined with ", ".
3. **Placeholder collection.** Placeholders are collected from the question text and every rule string (mention strings and entity/with_any, claim entity/pattern). Only `question.question` is substituted into `text`; the grader applies `placeholders` to the rules. `truth_values` holds every truth path used, keyed by path: truth_ref paths and the aliased `{T...}` paths (e.g. `T5.team_id`).
4. **Duplicate-id check.** It runs in `load_suite` after validation, not in a model validator, so the `ConfigError` names the id. The version check is `Literal[3]` (the spec says `version: int`, must equal 3).
5. **Private helper reuse.** `herness.eval.scripted._parse` is imported rather than duplicated. There is no public YAML helper, and duplicating it would break the line budget. Ruff has no private-import rule enabled.
6. **Test overrides.** `QUERY_TIMEOUT_S` / `MAX_REFERENCE_ROWS` are module constants, overridden in tests with monkeypatch. `config/eval.yaml` was not touched.

## Test-ID -> functions
- UT11-45: `test_ut11_45_expected_and_tolerance_invariants`, `test_ut11_45_hint_never_echoes_input`
- UT11-46: `test_ut11_46_entity_checks`, `test_ut11_46_rule_parts`
- UT11-47: `test_ut11_47_load_suite_defaults_merge_and_hash`, `test_ut11_47_version_2_rejected`
- UT11-48: `test_ut11_48_duplicate_ids_and_oversized_file`, `test_ut11_48_other_load_failures`
- UT11-49: `test_ut11_49_shared_reference_uses_cache` (execution counter == 1), `test_ut11_49_reference_failures` (SQL error, empty, 1,000-row cap, timeout via interrupt)
- UT11-50: `test_ut11_50_placeholders`, `test_ut11_50_placeholder_sources`, `test_ut11_50_unresolved_placeholders`, `test_ut11_50_missing_display_table_is_skipped`
- UT11-51: `test_ut11_51_dataset_skips`
- UT11-52: `test_ut11_52_truth_ref_mismatch`, `test_ut11_52_suite_error_is_reexported`
- ST11-06: `test_st11_06_hostile_reference_sql[DELETE / COPY / read_csv]` (each SQL in numeric, entities and placeholders_sql blocks -> SuiteError "rejected"; row counts unchanged on a *writable* in-memory warehouse; no `x.csv` or other file created in cwd)
- ST11-12 (suite part): `test_st11_12_two_megabyte_suite_rejected`, `test_st11_12_million_aliases_rejected`, `test_st11_12_alias_bomb_under_size_cap_rejected`, `test_st11_12_safe_load_rejects_python_tags` (each ConfigError < 2 s). Note: the 10^6-alias file is about 3 MB, so the size cap rejects it first. The sub-1 KB billion-laughs test covers the alias budget directly.

## Evidence
- RED: `pytest tests/unit/eval/test_golden.py tests/security/test_st11_golden.py` -> `ImportError: cannot import name 'golden' from 'herness.eval'` (2 collection errors).
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/eval tests/security/test_st11_golden.py -q -p no:logging --cov=herness.eval.golden --cov-branch` -> 60 passed. golden.py coverage **100 % line, 100 % branch** (293 stmts, 52 branches).
- Full fast suite: `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging --require-test-ids` -> 3181 passed, 5 skipped (symlink privileges / coverage.json), 1 xfailed (pre-existing), 0 failed.
- `ruff format` / `ruff check`: clean. `mypy` (strict): no issues in 113 files. `lint-imports`: 12 kept, 0 broken. `check_module_size`: exit 0. `check_type_ownership`: exit 0. pre-commit hooks all passed. detect-secrets did not flag anything, and `.secrets.baseline` was not changed.

## Concerns
- golden.py is at exactly its 400-line budget, which is also the ENG hard limit. Any later growth needs a split, such as moving the models to `herness/eval/golden_models.py`.
- The `owning_team` display lookup is an addition to the spec (decision 2).
- `ci` display depends on CI ids appearing in core.service / core.team / core.org / core.work_item.
