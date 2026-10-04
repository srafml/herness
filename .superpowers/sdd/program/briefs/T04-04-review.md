# T04-04 review: Windows, rendering and macros

Reviewed: diff 6f4ad86..e6bef8a (worktree agent-a1b97ebc42c67b5df), brief, builder report, spec docs/impl/04-metrics-and-scoring.impl.md (U04-28..U04-40, §11 rows), global constraints, reviewer rules.

Gates re-run in the worktree (PYTHONUTF8=1):
- `uv run pytest tests/unit/metrics tests/security/test_st04_render.py -q -p no:logging` -> 352 passed
- `uv run python -m tools.check_module_size` -> exit 0 (render.py 281/300, windows.py 163/200, _macros 132/260, wrapper 35/60, _binds.py 175 default budget)
- `uv run lint-imports` -> 8 kept, 0 broken
- ruff check: all passed; ruff format --check: 103 files formatted; mypy: no issues (44 files)

### Spec Compliance
- ✅ U04-29 Window: frozen dataclass, fields as spec; start<end and ZoneInfo checked in `__post_init__` -> ConfigError; `start_ts`/`end_ts`/`as_of_ts` = local midnight (fold=0) -> UTC; `binds()` has exactly the 7 names/values.
- ✅ U04-30 resolve_as_of: positional; naive -> `ConfigError("naive build start time")`; override else local date.
- ✅ U04-31 default_window / DEFAULT_PERIOD_COUNTS {week 26, month 24, quarter 8}: week end = Monday on/before as_of, month/quarter first day, t12w = 84 days, t12m day-clamped (2024-02-29 -> 2023-02-28); missing key -> ConfigError; is_custom=False.
- ✅ U04-32 custom_window: exact ToolInputError messages; 36 calendar-month cap with clamping; `MAX_WINDOW_MONTHS = 36`; is_custom=True.
- ✅ U04-33 make_environment: ImmutableSandboxedEnvironment, PackageLoader("herness.metrics","sql"), StrictUndefined, autoescape False, trim/lstrip blocks, no extensions, globals emptied except (sandbox-safe) `range`, new env per call.
- ✅ U04-34 BIND_TYPES: every name and type of the spec list checked one by one (window, request, f_*, d_*, s_*, all U04-37 w_* names, step binds). Read-only MappingProxyType.
- ✅ U04-35 macros: `p`, `entity_col`, `entity_join`, `period_start`, `period_start_date`, `period_spine`, `filter_clause`, `entity_filter`, `owner_team`, `cat_at_join`, `cat_at`, `lkp` match the spec text (verified against the literal expected strings in tests); grain table incl. `oc_/sv_/ow_/wc_` suffixing; exact fail messages ("template uses unknown parameter <name>", "grain <g> not supported by source <s>"). `team_bucket`/`observed_days` are owned by T04-16 (spec card line 2404) and `period_end_date` by T04-10 (line 2318), so the deferral is correct.
- ✅ U04-36 default_binds / U04-37 weight_binds: all keys, sorted/paired lists as specified.
- ✅ U04-38 RenderedQuery / render_metric_query: steps 1-10 followed; bind = exactly the used names (sorted); used name without candidate -> ConfigError; static_flags per spec; template shape per spec (see Minor 2 on dedup).
- ✅ U04-39 render_named: allowlist + `checks:<name>` split on `-- @check <name>` lines; identifiers-only context; bind used names only; template = {name, **context}.
- ✅ U04-40 metric_wrapper: 11 output columns in order and types; NULL entity rows dropped; coalesce sample_size; min_n rule; sorted distinct flags (list_distinct drops the NULL CASE arms) incl. static, insufficient_sample, low_coverage, estimate, unweighted, partial_period (custom + week/month/quarter only); identity columns from binds; ORDER BY entity_id, period_start.
- ✅ Tests: UT04-23, UT04-24, UT04-25, UT04-26, UT04-27, UT04-28, UT04-29, PT04-12, ST04-01 (render part, executed on DuckDB incl. no rows / no error / table intact), ST04-03 (SSTI payloads incl. both spec payloads -> ConfigError chained from TemplateError), plus UT04-64/UT04-68 wrapper parts. Every function has an ID in name and docstring; module pytestmark set.
- ✅ Acceptance "rendered SQL for every grain parses with sqlglot": 4 grains x 5 periods and all 17 supported (source, grain) pairs with all filters.
- ✅ Threats: TH04-01: every runtime value is a `CAST($name AS type)` placeholder; `entity_type`/`period`/filter keys checked against their Literals before any rendering; render_named context values restricted to `^[a-z][a-z0-9_]{0,63}$`/int/bool; aliases validated. TH04-03: sandbox + StrictUndefined + empty globals; underscore attrs and mutating calls blocked. TH04-07: 36-month cap here (500-ID cap / timeout / row cap belong to U04-51 / compute).
- `RenderState.alias`: justified. U04-35 Preconditions require aliases to match `^[a-z][a-z0-9_]{0,15}$` "else rs.fail"; the Jinja sandbox has no regex facility, so a Python-side validator on the render state is the only faithful way to implement it. Keep.

- ⚠️ Carry-overs (program rulings, not defects): `CatalogView` Protocol in place of `MetricCatalog` (T04-03); the FILTER table in `_macros.sql.j2` must be cross-checked against `SOURCE_FILTERS` when T04-03 lands; `_binds.py` module-map row to be added by the controller; StepContext binds `s_count_metrics`/`s_unconfirmed_models` to be added to BIND_TYPES by T04-12 (render correctly refuses unknown names meanwhile).
- ⚠️ VI04-04/VI04-05 were verified by an uncommitted scratch script (report claim). The committed tests do execute rendered SQL on DuckDB for the incident source and the spine, which covers the key `AT TIME ZONE CAST($tz AS VARCHAR)` / `timezone()` / `CAST($x AS VARCHAR[])` behaviour.

### Strengths
- Tight, faithful macro text; tests pin the exact spec strings, not just "contains".
- Defence in depth: request identifiers re-checked in render, identifiers-only context for named templates, sandbox errors re-raised as ConfigError with the cause chained.
- Security tests execute injected binds against DuckDB and assert SQL text identity with a benign baseline.
- Window arithmetic is small, pure and well covered (DST, leap day, year crossing, exact 36-month boundary).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/metrics/render.py:130-135: `_guard` converts only `jinja2.TemplateError`. Non-Jinja exceptions raised while rendering a (config) template, e.g. `OverflowError` from the sandbox's `safe_range` (`{{ range(10**6) }}`), `ZeroDivisionError`, or a `TypeError` in an expression, escape as raw exceptions, while U04-38/U04-39 list only `ConfigError` for render failures. Fix: in `_guard` also catch `(ArithmeticError, TypeError, ValueError, LookupError)` (ConfigError is a FatalError, so it is not caught) and re-raise as `ConfigError(...) from exc`; add one test with `{{ range(10**6) }}`.
2. herness/metrics/render.py:216 and :234: the `template["filters"]` values are de-duplicated (`_sorted_unique`), while U04-38 specifies `template = {..., "filters": {k: sorted values}}` (only the `f_<k>` binds are "sorted unique"), and U04-51 does not de-duplicate. Either use `sorted(filters[key])` for the template (keep `_sorted_unique` for binds) or get a ruling that the template uses unique values; the template feeds the recorded query identity, so choose deliberately.
3. herness/metrics/sql/metric_wrapper.sql.j2:17: `0.8` is hard-coded; U04-28 `LOW_COVERAGE_THRESHOLD` lives in catalog.py (T04-03, not on this branch). When T04-03 lands, add a test asserting the wrapper literal equals `LOW_COVERAGE_THRESHOLD` (same for the `FILTER` dict at herness/metrics/sql/_macros.sql.j2:20 vs `SOURCE_FILTERS`).
4. tests/security/test_st04_render.py:79: payload `{{ rs._used }}` targets an attribute that does not exist (`RenderState.used` is public), so it only exercises StrictUndefined/underscore blocking. Add `{{ rs.used.add('x') }}` and `{{ rs.sources.clear() }}` to prove the immutable sandbox protects the render state's mutable sets.

### Assessment
**Task quality:** Approved
**Reasoning:** All twelve unit specs are met with exact values and messages, every listed test row has tests that exercise real behaviour (including execution on DuckDB), and all gates (tests, ruff, format, mypy, import contracts, module budgets) pass; the remaining items are minor robustness/test polish and carry-overs.

Verdict: Approved

## Re-review (fix round 1)

Commit eb3c9f5 (diff e6bef8a..eb3c9f5). Scope: Minors 1, 2 and 4. Minor 3 is a recorded carry-over.

- Minor 1 ✅ `_guard` (render.py) re-raises `ConfigError` unchanged and converts `TemplateError`, `ArithmeticError`, `TypeError`, `ValueError` and `LookupError` to `ConfigError` with the cause chained. New test `test_ut04_26_non_jinja_render_errors_become_config_error` asserts that the `OverflowError` from `range(10**6)` is the cause.
- Minor 2 ✅ `template["filters"]` is now `sorted(values)`, keeping duplicates. The `f_<k>` binds stay sorted and unique, and the mixed-type check is shared through `_sorted`. New test `test_ut04_26_template_filters_are_sorted_not_deduped` checks both.
- Minor 4 ✅ `{{ rs.used.add('x') }}` and `{{ rs.sources.clear() }}` were added to the ST04-03 payloads.
- No regressions: test_metrics_render.py and test_st04_render.py give 116 passed at eb3c9f5. ruff and mypy are clean on render.py. render.py is 297 lines, within its 300-line budget, but close to it.

Verdict: Approved
