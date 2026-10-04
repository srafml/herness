# T04-03 Catalog and validator — build report

Status: DONE_WITH_CONCERNS
Commit: 1dafaf8 feat(metrics): add metric catalog and validator (T04-03) (worktree-agent-a6a98954e19527ae3, base cdadffe). All pre-commit hooks passed; no --no-verify.

## What was built
- `herness/metrics/catalog.py` (359/380): `MetricCatalog` (frozen slots dataclass; `version = sha256_hex(canonical_json(config.model_dump(mode="json")))[:12]`; `defaults`/`scoring` properties; `get` -> ToolInputError "unknown metric <name>; known: <enabled names>"; `names(enabled_only=True)` sorted; `describe()` with the 9 design keys, sorted by name), `load_catalog` (U04-24 steps 1-7, 1 MiB cap, field paths only in errors, logs `metrics.catalog.loaded`/`metrics.catalog.rejected`, ConfigError carries the error issues), `catalog_from_config` (no caching), `validate_catalog` (U04-26 steps 1-10; issues file `metrics.yaml`, paths `metrics.<name>.<field>` / `scoring.org.metrics.<m>` / `scoring.levers.templates.<k>`), `SCORE_UNITS` (27 entries per §4.3) + `unit_for`, `METRIC_FLAGS`, `SOURCE_FILTERS`, `LOW_COVERAGE_THRESHOLD`, `metrics_owner_validator` (U04-83: catalog issues, then one ConfigIssue per WeightIssue with file `weights.yaml`, then the snapshot `warn` if any; logs `metrics.weights.payload_invalid` / `metrics.weights.confirmation_rejected`). Re-exports `Period`, `EntityType`, `Unit`, `MetricDef`.
- `herness/metrics/_catalog_checks.py` (129, new private sibling, no module-map row yet -> default budget 400): steps 2-3 (forbidden words, `$ -- /* ;`), steps 4-5 (sqlglot parse, one statement, root Select/SetOperation for the wrapper's `FROM (...) q`, forbidden node types, schema allowlist core/enrich/metrics or CTE, forbidden functions, exact output column contract) and step 9 (lever placeholders).
- `pyproject.toml`: `herness.metrics.catalog` and `herness.metrics._catalog_checks` added to the "settings modules are leaves" forbidden list (UT00-58 green).
- Validator design points: steps 4-6 are skipped for a metric whose raw checks (1-3) fail (never render unsafe SQL); render window = `default_window("week", 2024-01-01, weights.business_timezone, defaults.windows)`, every `m.filters` key present (`[1]` for priority, `["x"]` otherwise), `entity_ids=["x"]`; problems per metric are deduplicated. Snapshot weights are read with `WeightsConfig.model_validate_json(json.dumps(section))` because the YAML snapshot turns integer map keys into strings (strict python-mode validation rejects them).
- `# T09-20:` marker at the end of catalog.py where `register_owner_validator("metrics", metrics_owner_validator)` belongs; production code does not register.

## Carry-overs
(a) DONE: `CatalogView` Protocol removed from `_binds.py` (175 -> 164/200) and from `render.__all__`; `_binds.py` and `render.py` annotate `"MetricCatalog"` under `TYPE_CHECKING` (catalog imports render -> _binds, so a runtime import would cycle); `context.py` imports `MetricCatalog` at runtime (no cycle; keeps it at 80/80). render.py 299/300.
(b) DONE: `test_ut04_16_wrapper_threshold_matches_low_coverage` (wrapper `q.coverage < 0.8` == LOW_COVERAGE_THRESHOLD) and `test_ut04_16_macro_filter_table_matches_source_filters` (`_macros` exported `FILTER` == SOURCE_FILTERS). ID UT04-16 because U04-28's Tests row names UT04-16.

## Tests (tests/unit/metrics/test_metrics_catalog.py, 651 lines, 82 cases)
UT04-13, UT04-14, UT04-16, UT04-17, UT04-18, UT04-21, UT04-22, UT04-117, UT04-120, ST04-02, ST04-04, ST04-10 all present.
- RED: not captured — the implementation was written before the tests (TDD order not followed). The first test run failed 1 case (a scorecard fixture naming a metric absent from the catalog: a test-setup error, fixed in the test).
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging` -> 441 passed, 1 xfailed.
- Coverage (branch): catalog.py 100 %, _catalog_checks.py 100 %, _binds.py/context.py/render.py 100 %.
- Full: `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging --require-test-ids` -> 4064 passed, 5 skipped (platform symlink / coverage.json), 2 xfailed.
- Gates: ruff format/check clean, mypy 0 issues (147 files), lint-imports 13 kept, check_type_ownership 0, check_module_size 0.

## Deviations / rulings needed
1. UT04-13 "shipped config/metrics.yaml": `test_ut04_13_shipped_metrics_yaml_loads` is `xfail(strict=True, raises=ConfigError, reason=T04-08)`; it will XPASS-fail when T04-08 fills `metrics:` (and trims/aligns `scoring.org.metrics`), which forces removal of the marker. Meanwhile `test_ut04_13_load_and_key_order_keeps_version` loads the shipped file with fixture entries. Committed YAML unchanged.
2. LEVER_PLACEHOLDERS (U04-71) belongs to `herness.metrics.levers` (T04-18), which does not exist. Held as `LEVER_PLACEHOLDERS` in `_catalog_checks.py` with a `# T04-18:` marker; levers.py will import the catalog, so T04-18 should import this constant (re-export) or add an equality test rather than define a second copy.
3. Module map: `_catalog_checks.py` needs a §2 row, e.g. `| herness/metrics/_catalog_checks.py | Pure SQL and lever-template checks behind validate_catalog (private split of catalog.py) | raw_sql_problems, sql_problems, template_problems, LEVER_PLACEHOLDERS | L3 | sqlglot | 140 |`. catalog.py's row keeps `sqlglot` as a dependency only indirectly now.
4. Performance: U04-26 limit "≤ 200 metrics × 5 grains; < 2 s" is NOT met. Measured 200 metrics × 4 grains = 18.8 s (~23 ms per render), dominated by T04-04's per-render fresh Jinja environment (compiles `_macros.sql.j2` and the wrapper every call) plus two sqlglot parses per render. A shared jinja2 BytecodeCache only cut it to 7.9 s. The real catalog (28 metrics, ~100 renders) takes ~2.3 s. Needs a ruling (relax the bound, or a render-side template cache in T04-04 — render.py has 1 line of budget left).
5. Step 5 checks column names and order only; the declared types (`coverage DOUBLE`, `estimated_count BIGINT`, `unweighted BOOLEAN`) cannot be checked statically without executing — left to the wrapper's CASTs / runtime.
6. ST04-04 "fact and score outputs contain no text column (schema scan)": implemented as a scan of every `herness/metrics/sql/*.sql.j2` plus `herness/model/sql/400_facts.sql` when present; facts (T04-05) and score templates do not exist yet, so today only the wrapper and macros are scanned.
7. U04-83 snapshot `warn` is appended after the weight issues (spec's postcondition names "U04-26 issues, followed by one ConfigIssue per WeightIssue"; the warn's position is unspecified). The unreadable-snapshot warn carries file `weights.yaml`.
8. UT04-120 uses a monkeypatched `list_review_items` ("fake ops store"), asserting the call `kind="weight_change", status="approved", limit=5000`; it registers the validator itself and compares with `run_owner_validators`.

## Fix round 1 (review T04-03-review.md)
Commit: 9ac2ace fix(metrics): close T04-03 review findings (T04-03)
1. Important, fixed: `metrics_owner_validator(cfg, *, offline)` now matches `OwnerValidator` exactly (the `/` is removed). catalog.py adds `_OWNER_VALIDATOR: Final[OwnerValidator] = metrics_owner_validator`, which mypy checks. With `/` re-added temporarily, mypy failed with "Incompatible types in assignment ... variable has type OwnerValidator"; reverted. catalog.py imports `OwnerValidator` from herness.core.config_validate (L0), so import-linter stays green. Still not registered; the `# T09-20:` marker is kept.
2. Fixed: `unit_for` no longer uses `assert`; explicit branches now return the metric's unit or raise ToolInputError.
3. Fixed: the `LAST` content must fullmatch `cfg_[0-9a-f]{16}` before it is used as a file name; anything else returns the unreadable-snapshot warn. New test `test_ut04_120_last_must_name_a_snapshot_hash` covers `../../secrets`, `cfg_ABC`, `cfg_…yaml` and empty content.
4. Fixed: the FILTER table is read with `getattr(module, "FILTER", None)` plus an `isinstance(table, dict)` check.
5. Fixed: the ST04-04 scan has a searchable `# T04-05:` marker. `.replace(";", "")` is removed; the scan checks only the text-column rule, because macros, checks.sql.j2 and 400_facts.sql carry `;`, `--` and `/*` legitimately.
Not changed, per the ruling: render.py and the performance note. Minors 8 and 9 are parked.
Line counts: catalog.py is 367/380.
Gates: ruff clean, mypy 0 issues, lint-imports 13 kept, type-ownership 0, module-size 0. Branch coverage of catalog.py and _catalog_checks.py is 100%. Full suite: 4068 passed, 5 skipped, 2 xfailed (--require-test-ids).
