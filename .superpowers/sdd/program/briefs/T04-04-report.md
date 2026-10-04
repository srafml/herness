# T04-04 report: Windows, rendering and macros

Status: DONE_WITH_CONCERNS
Commit: e6bef8a feat(metrics): add windows, sandboxed rendering, macros and metric wrapper (T04-04)
Worktree/branch: D:\herness\.claude\worktrees\agent-a1b97ebc42c67b5df (worktree-agent-ab0823b1ca9b57912)

## What was implemented
- `herness/metrics/windows.py` (U04-29..U04-32): frozen `Window` (start<end and ZoneInfo checks -> ConfigError; `start_ts`/`end_ts`/`as_of_ts` = local midnight -> UTC; `binds()`), `resolve_as_of`, `default_window` (+ `DEFAULT_PERIOD_COUNTS`), `custom_window` (+ `MAX_WINDOW_MONTHS = 36`, ToolInputError messages verbatim), calendar-month arithmetic with day clamping.
- `herness/metrics/render.py` (U04-33, U04-38, U04-39): `make_environment` (ImmutableSandboxedEnvironment, PackageLoader("herness.metrics","sql"), StrictUndefined, autoescape off, trim/lstrip blocks, globals emptied except `range`, new env per call), `RenderState`, `RenderedQuery`, `render_metric_query` (steps 1-10 of U04-38; sqlglot detects optional columns), `render_named` (allowlist + `checks:<name>` split on `-- @check <name>` lines). All Jinja `TemplateError`s (incl. sandbox `SecurityError`, `UndefinedError`, `TemplateNotFound`) are re-raised as `ConfigError` with the cause chained.
- `herness/metrics/_binds.py` (private split, re-exported by render): `BIND_TYPES` (U04-34), `default_binds` (U04-36), `weight_binds` (U04-37), `CatalogView` protocol.
- `herness/metrics/sql/_macros.sql.j2` (U04-35): `p`, `entity_col`, `entity_join` (full grain table, `oc_<a>`/`sv_<a>`/`ow_<a>`/`wc_<a>` suffixing), `period_start`, `period_start_date`, `period_spine`, `filter_clause` (U04-28 column table incl. org closure and owner-team EXISTS forms), `entity_filter`, `owner_team`, `cat_at_join`, `cat_at`, `lkp`.
- `herness/metrics/sql/metric_wrapper.sql.j2` (U04-40): identity columns from binds, min_n rule, sorted distinct flags (static, insufficient_sample, low_coverage, estimate, unweighted, partial_period for custom week/month/quarter windows), NULL entity rows dropped, ORDER BY entity_id, period_start.
- pyproject.toml: `settings modules are leaves` contract now also forbids settings -> `_binds`, `render`, `windows` (spec §2 "settings is light").
- Tests: `tests/unit/metrics/test_metrics_windows.py` (UT04-27/28/29), `tests/unit/metrics/test_metrics_render.py` (UT04-23..26, UT04-64/68 wrapper parts, ST04-03 env settings, PT04-12), `tests/security/test_st04_render.py` (ST04-01 render part incl. execution on DuckDB, ST04-03 SSTI payloads), helper `tests/support/metrics_render.py` (partial catalog fixture: shipped defaults/scoring + hand-written MetricDefs, tiny in-memory DuckDB).

## Verification items
- VI04-04 (DuckDB 1.5.5, `enable_external_access=false`): `AT TIME ZONE CAST($tz AS VARCHAR)` and `timezone(tz, TIMESTAMP)` work (NY DST checked). Every supported (source, grain) render, default and custom windows, also executed against empty tables with external access disabled (scratch script, not committed).
- VI04-05: `CAST($x AS VARCHAR[])` binds NULL (IS NULL true) and `[]` (list_contains false); unused named parameters are rejected ("excess parameters: extra") and missing ones too -> `bind` must be exactly the used names, which render guarantees.

## Evidence
RED: `PYTHONUTF8=1 uv run pytest tests/unit/metrics/test_metrics_windows.py tests/unit/metrics/test_metrics_render.py tests/security/test_st04_render.py -q -p no:logging`
-> `ModuleNotFoundError: No module named 'herness.metrics.render'` ... `3 errors during collection`.
GREEN: same command -> `137 passed`; with `--cov ... --cov-branch`: windows.py, render.py, _binds.py 100 % line and branch.
Full: `uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` -> `908 passed, 5 deselected, 1 xfailed` (pre-existing IT00-02 xfail).
`uv run pytest --require-test-ids tests/unit/metrics tests/security/test_st04_render.py` -> 352 passed.
Gates: ruff check "All checks passed!"; ruff format --check "103 files already formatted"; mypy "Success: no issues found in 44 source files"; lint-imports "Contracts: 8 kept, 0 broken."; check_type_ownership exit 0; check_module_size exit 0.
Acceptance "rendered SQL for every grain parses with sqlglot": test_ut04_23_rendered_metric_parses_for_every_grain (4 grains x 5 periods) and test_ut04_23_every_source_grain_parses (all 17 supported (source, grain) pairs with every supported filter).

## Line counts vs budgets
windows.py 163/200; render.py 281/300; _binds.py 175 (unlisted private module, default budget); _macros.sql.j2 132/260; metric_wrapper.sql.j2 35/60.

## Deviations and concerns
1. `MetricCatalog` (T04-03, `herness.metrics.catalog`) is not on this branch. `default_binds` / `render_metric_query` are typed against a structural `CatalogView` protocol (`defaults`, `scoring`, `get(name)`, `names(enabled_only=)`), which U04-23's `MetricCatalog` satisfies. Tests use a `FakeCatalog`. When T04-03 lands the annotation can be switched to `MetricCatalog` (or kept).
2. `SOURCE_FILTERS` (U04-28, catalog.py) also isn't here; the filter-column table is encoded in `_macros.sql.j2` (FILTER dict). T04-03 should keep its `SOURCE_FILTERS` key sets consistent with it (or a test cross-check can be added then).
3. `RenderState` has one method beyond the U04-35 list: `rs.alias(name)` validates aliases against `^[a-z][a-z0-9_]{0,15}$` (Jinja has no regex, and the spec requires the check via `rs.fail`). Derived aliases (`ow_<a>`, `oc_<a>` ...) are built from a validated alias and not re-validated.
4. BIND_TYPES / default_binds / weight_binds moved to private `herness/metrics/_binds.py` (re-exported from `render`) so render.py fits its 300-line budget; module map rows for render.py are otherwise unchanged.
5. Macros `period_end_date` (T04-10), `team_bucket` and `observed_days` (card at spec line 2404) are left to their owner cards. StepContext binds `s_count_metrics` / `s_unconfirmed_models` (T04-12) are not in BIND_TYPES yet; the owning card must add them (render refuses unknown names).
6. Choices where the spec is silent: event/metric_daily org grain join is `LEFT JOIN core.service sv_<a> ... JOIN metrics.org_closure oc_<a>`; closure joins are inner (NULL org rows would be dropped by the wrapper anyway). `team_id`/`org_id` filters on event/metric_daily use correlated EXISTS subqueries (owner-team rows / service org closure) because filter_clause sits in WHERE. `s_lower_better`, `s_lever_models_*`, `s_metric_units_*` use enabled metrics only. `render_metric_query` also checks entity_type/period/filter keys against their literals and requires the six contract output columns (ConfigError), as defence in depth.
7. The wrapper and render tests for U04-40 carry IDs UT04-64/UT04-68 (the IDs U04-40 lists); the compute-level parts of those tests stay with T04-08.

## Fix round 1 (review findings)

Fixed the four Minor findings from T04-04-review.md that were actionable on this branch (Minor 3, the `LOW_COVERAGE_THRESHOLD`/`SOURCE_FILTERS` carry-over, is explicitly deferred to T04-03 by the review itself and left untouched):

1. `herness/metrics/render.py` `_guard`: now also catches `ArithmeticError`, `TypeError`, `ValueError` and `LookupError` (in addition to `jinja2.TemplateError`), re-raising as `ConfigError(...) from exc`; `ConfigError` is caught first and re-raised unchanged so it is never re-wrapped even though it structurally cannot subclass any of those (confirmed: `ConfigError` -> `FatalError` -> `HernessError` -> `Exception`). New test `test_ut04_26_non_jinja_render_errors_become_config_error` renders `{{ range(10**6) }}`, which raises the sandbox's `OverflowError`, and asserts the result is a `ConfigError` with `__cause__` the original `OverflowError`.
2. `herness/metrics/render.py` `render_metric_query`: split the single `values` dict into `unique_values` (via `_sorted_unique`, used for the `f_<k>` binds) and `sorted_values` (via a new `_sorted` helper — `sorted()` with the same "filter values must share one type" `ConfigError` on mixed types — used for `RenderedQuery.template["filters"]`). Binds stay sorted-unique; the template now carries sorted values with duplicates preserved, matching U04-38. New test `test_ut04_26_template_filters_are_sorted_not_deduped` renders with `filters={"priority": [2, 1, 2]}` and asserts `template["filters"] == {"priority": [1, 2, 2]}` while `bind["f_priority"] == [1, 2]`.
3. `tests/security/test_st04_render.py::test_st04_03_template_escape_attempts`: added `{{ rs.used.add('x') }}` and `{{ rs.sources.clear() }}` to the SSTI payload list (kept the original `{{ rs._used }}` probe, which still exercises underscore-attribute blocking on a nonexistent name). The two new payloads target the real `RenderState.used` / `RenderState.sources` attributes and prove `ImmutableSandboxedEnvironment` blocks the mutating `set.add`/`set.clear` calls (both raise `SecurityError`, a `jinja2.TemplateError` subclass, satisfying the test's existing `__cause__` assertion).

TDD: all four new/changed test cases were run and observed failing against the pre-fix code (the two new UT04-26 tests raised uncaught `OverflowError` / asserted-wrong dict; the ST04-03 payloads already passed even pre-fix, since they are new assertions of existing, already-correct sandbox behavior, not a bug fix) before the render.py changes were made, then passing after.

Evidence:
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics tests/security/test_st04_render.py -q -p no:logging` -> `356 passed` (352 baseline + 4 added: 2 in test_metrics_render.py, 2 new parametrize cases in test_st04_render.py).
- `uv run ruff check .` -> All checks passed.
- `uv run ruff format --check .` -> 103 files already formatted.
- `uv run mypy` -> Success: no issues found in 44 source files.
- `uv run lint-imports` -> Contracts: 8 kept, 0 broken.
- `uv run python -m tools.check_module_size` -> exit 0.

Line counts vs budgets: render.py 297/300 (was 281/300); all other card files unchanged.

Commit: `eb3c9f5` `fix(metrics): review fixes for rendering (T04-04)`.
