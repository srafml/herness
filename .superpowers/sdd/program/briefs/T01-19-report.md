# T01-19 report: Monitoring connector base

Worktree D:\herness\.claude\worktrees\agent-aaca957a1034161b7, branch worktree-agent-aaca957a1034161b7, base e41d62c.
Status: DONE_WITH_CONCERNS (see Concerns). Final commit: 9e2c1ec feat(connectors): monitoring connector base (T01-19).

## Implemented (U01-78 to U01-81)
- herness/connectors/monitoring/__init__.py (1 line, budget 10): package marker.
- herness/connectors/monitoring/base.py (319 lines, budget 320):
  - `MonitoringAdapter` Protocol (tool, check, events, daily_metrics).
  - `EventRow` / `MetricRow` TypedDicts, `EVENT_COLUMNS`, `METRIC_COLUMNS` (spec values), plus public `TOOLS`
    = (prometheus, datadog, splunk, dynatrace).
  - `event_batch` / `metric_batch`: built through `RowBatcher` (same path as mongodb/snowflake: metadata
    columns then the column tuple, all field columns string, keys validated by `record_id`). Event
    `_source_key = tool:event_key`, `_source_updated_at = ts`; metric key `tool|metric|service|date`,
    `_source_updated_at = date + 1 day 00:00 UTC`; ts/end_ts ISO with Z; date ISO; value json.dumps(float), None
    for NaN/inf/None. Every precondition (1 <= rows <= batch_rows, known tool, aware ts/end_ts/fetched_at,
    non-empty event_key/service/metric_name, str types for text fields and payload, date not datetime,
    numeric non-bool value, key passes record_id) -> `SchemaViolation("monitoring row", source="monitoring",
    tool=<tool or "unknown">)`; row values never echoed. `source_tool` always the validated tool argument
    (a row key named source_tool is ignored).
  - `floor_day`, `complete_days` (naive -> ConfigError).
  - `MonitoringConnector` (`@register("connector","monitoring")`): ctor validates >= 1 adapter, each
    `adapter.tool` in TOOLS and unique (ConfigError); entities = configured subset of (event, metric_daily);
    watermark_field ts/date; check() in order, first error propagates; tools() in adapter (config) order;
    stream_key/sync_tool raise ConfigError eagerly for unknown tool or entity (context tool only if known);
    sync_tool: since=None -> backfill_for(entity).resolve_start(until), naive bounds -> ConfigError,
    since >= until -> empty, event -> events(), metric_daily -> daily_metrics(); adapter batches are checked
    (metadata columns present, `_source`=monitoring, `_entity`=entity, `source_tool`=tool) else
    SchemaViolation before the runner sees them. sync() chains sync_tool over tools, until=None -> clock().
  - No network call, no HTTP client, no httpx import in the base (ST01-14 lint passes untouched).

## Outside-Files edits (sub-controller rulings / direct consequences)
- herness/core/registry.py (146/160): one `_BUILTINS` row ("connector","monitoring") ->
  "herness.connectors.monitoring.base:MonitoringConnector".
- herness/connectors/mapping_check.py (233/260): `# T01-19` marker removed; "monitoring" dropped from
  `_SKIPPED`; `_fetched` returns EVENT_COLUMNS / METRIC_COLUMNS per entity for MonitoringSettings.
- tests/unit/connectors/test_mapping_check.py: UT01-58 skip test now files+jira only; new UT01-58 test: shipped
  130_stg_monitoring.sql gives [] and a fixture SQL reading `e.priority` gives one issue.
- tests/unit/core/test_config_validate.py (UT10-19 C03): the expectation listed `sources.sources.monitoring`
  as "no implementation"; with the registry row it now resolves, so that one tuple was removed (the
  splunk adapter C03 error stays). Found by the pre-commit unit hook.
- tests/unit/connectors/test_connector_factory.py: new UT01-94 test: the shipped `_BUILTINS` row resolves the real
  MonitoringConnector; built via build_connector with a fake prometheus adapter, no network calls; satisfies
  Connector and SupportsToolStreams, not SupportsKeyListing.
- No import-linter change needed (contracts are package-level; lint-imports 13 kept).

## Tests (UT01-79)
- tests/unit/connectors/test_monitoring_base.py, test_monitoring_connector.py, helper _monitoring_data.py
  (all names contain ut01_79, docstrings start "UT01-79", pytestmark unit).
- Runner test (real SyncRunner + FakeLake + migrated temp ops store, two fake adapters, backfill.start
  2026-02-20, clock 2026-03-01T12:00Z): watermarks exist for each (tool, entity) under `monitoring:<tool>`:
  event field `ts` = latest event ts; metric_daily field `date` = 2026-03-01T00:00Z (end of last complete day);
  no `monitoring` (unkeyed) watermark; sync_slice rows keyed monitoring:<tool> per entity, all done (UT01-93
  shape). Second runner test: a tool returning no events still gets its event watermark (= end) on first run
  (the T01-12 health carry-over is satisfied by the backfill path; incremental runs with zero rows keep it).
- RED: `pytest tests/unit/connectors/test_monitoring_base.py tests/unit/connectors/test_monitoring_connector.py`
  with base.py absent -> `ModuleNotFoundError: No module named 'herness.connectors.monitoring.base'`, 2 errors.
- GREEN: `pytest tests/unit/connectors -k "ut01_79 or ut01_92 or ut01_93"` 73 passed (63 UT01-79 + UT01-92/93); `pytest tests/unit/connectors -q` 815 passed, 4 skipped (host skips).
- Coverage (connectors unit run): monitoring/base.py 100% line, 100% branch (183 stmts, 40 branches);
  __init__.py 100%.
- Gates: ruff format --check, ruff check, mypy (335 files), lint-imports (13 kept), check_type_ownership 0,
  check_module_size 0; UT00-58, UT10-19, ST01-14 lint tests pass.

## Deviations
- `event_batch` / `metric_batch` take an extra keyword-only `batch_rows: int = DEFAULT_BATCH_ROWS` (additive):
  the spec precondition "1 <= rows <= batch_rows" needs a bound and the adapter convention carries
  `batch_rows`. Default equals the spec default 10,000.
- `SchemaViolation("monitoring row", tool)` rendered as message "monitoring row" with context
  source="monitoring", tool=<known tool | "unknown"> (HernessError takes context as keywords only).
- Extra public `TOOLS` constant (not in the module-map export list).
- String fields: the other connector row builders (RowBatcher/flatten_record) do no redaction or length bound
  of field values; the same applies here (keys bounded to 512 chars and control-char free by record_id; text
  fields must be str or None). No truncation was invented.

## Spec notes
- Controller ruling to record: Splunk exports over 64 MiB are refused by the shared egress cap; the cap stays;
  the Splunk adapter (T01-21) must page exports so no single response exceeds it.
- Design 01 §5.3 "one watermark per entity for all adapters" is superseded by R-62 (per tool); implemented per tool.

## Concerns
- base.py is at 319/320 lines (no headroom; compacted with `# fmt: skip` for __all__/EVENT_COLUMNS and a
  `_DT` alias).
- The checkpoint `wip` commit was not made: the first commit attempt failed the pre-commit unit hook (UT10-19
  C03, fixed), the second failed module-size (fixed); all work lands in the single final commit.

## Final commit
- 9e2c1ec feat(connectors): monitoring connector base (T01-19), on base e41d62c. All pre-commit hooks passed
  (ruff, detect-secrets, module-size, type-ownership, pytest-unit).

## Fix round 1 (review minors m1, m2)
- m1: `_same` now requires `null_count == 0` before `pc.all(pc.equal(...))`, so an all-null or partly-null
  `_source` / `_entity` / `source_tool` column in an adapter batch raises SchemaViolation("monitoring row").
  New UT01-79 test `test_ut01_79_null_metadata_rejected` (3 columns x all_null/part_null).
- m2: `_build` also maps OverflowError (date 9999-12-31 + 1 day) to SchemaViolation("monitoring row").
  New UT01-79 test `test_ut01_79_metric_date_overflow_is_schema_violation`.
- base.py 320/320 lines; coverage 100% line / 100% branch. Card tests 80 passed; tests/unit/connectors
  822 passed, 4 skipped; ruff, mypy, lint-imports, check_type_ownership, check_module_size clean.
- Fix commit: 655ed30 fix(connectors): T01-19 review round 1 (null metadata, date overflow); all hooks passed.
