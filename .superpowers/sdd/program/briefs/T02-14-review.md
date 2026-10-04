# Review: T02-14 Monitoring and generic staging

Commits reviewed: b338534..9e69e9a (9e69e9a "feat(model): monitoring and generic staging SQL (T02-14)").

## Spec compliance

- U02-111 `130_stg_monitoring.sql` ✅ — `stg.mon_event`/`stg.mon_metric_daily` built exactly per the spec's column table (docs/impl/02-data-model.impl.md:2638-2648): `service`→`service_name`, `title`→`alert_name`, `severity_raw` mapped through enum `monitoring.severity` typed via `m.typed`/`m.enum`, `ts`/`end_ts` typed `ts_utc`, `date` typed `to_date`, `value` typed `to_double`; all other listed columns pass through raw. `stg.cast_stats` rows written via `m.cast_stats`, preceded by `DELETE FROM stg.cast_stats WHERE table_name IN (...)` (herness/model/sql/130_stg_monitoring.sql:60) — correctly guards the INSERT-only cast_stats table for idempotent re-runs, matching the pattern already used in 110/120.
- U02-112 `140_stg_files.sql` ✅ — one `stg.files_<entity>` table per `extra_entities["files"]` entity via Jinja loop, `record_id`/`source_key`/`source_updated_at` plus every lake column except the 8 metadata columns and `_payload` (herness/model/sql/140_stg_files.sql:79, matches `META_COLUMNS` in herness/store/lake.py:35-43 exactly), no casting (all VARCHAR, the lake's own type). Absent entity gives metadata-only table (empty), verified by test and by reading the `latest()` macro's absent branch.
- U02-113/U02-114 `150_stg_mongodb.sql`/`160_stg_snowflake.sql` ✅ — same shape as 140, confirmed by direct comparison; parametrized correctly by source name.
- U02-115 `170_stg_dataverse.sql` — ✅ correctly excluded. Verified against the actual task card (docs/impl/02-data-model.impl.md:3895-3919): T02-14's Units are U02-111...U02-114 and Files are 130/140/150/160 only; T02-15's Units are U02-115/116/117 with Files 170/200/220. The brief's merged unit-spec table (grouping U02-113/114/115 together) is the source of the ambiguity the build agent flagged; the task card itself is unambiguous and matches the controller ruling. No action needed.
- IT02-33 ✅ — 7 test functions across the two new test files, all correctly ID-tagged (function name contains `it02_33`, docstring first line starts with "IT02-33"), `pytestmark = pytest.mark.integration` set at module level in both files. Confirmed by running `tests/integration/model` (33 passed, includes these 7).
- Acceptance check "build with `extra_entities` for each source renders" ✅ — `test_it02_33_extra_entities_for_each_source_renders` renders 130-160 together with entities for files/mongodb/snowflake and asserts each dynamic table name appears in the rendered SQL text.
- Test ID reuse note ✅ — verified IT02-08 is already used by T02-13's tests (test_model_stg_jira.py, test_model_stg_servicenow.py, test_model_stg_servicenow_ci.py) and IT02-16/17 are reassigned to a later card per the spec's own Tests row at docs/impl/02-data-model.impl.md:2680 (T02-16, `260`/`270`) — the report's account of the ID history is accurate, and reviewer-rules' "accept if docstrings explain" is satisfied (both new test files carry a module docstring plus per-test docstring noting the reassignment).

## Identifier safety / injection check (named risk: generic tables built from render-time entity names)

Traced the full path: `extra_entities` entity names originate from config (`RenderContext.extra_entities`, herness/model/render_context.py) and are validated a first time in `scan_lake` → `_scan_entity` → `lake_glob` → `validate_name` (herness/store/lake.py:65-70, `NAME_RE = ^[a-z][a-z0-9_]{0,63}$`) before any lake I/O. The SQL templates then re-validate at render time: every use of `entity` in `140_stg_files.sql`/`150_stg_mongodb.sql`/`160_stg_snowflake.sql` reaches SQL text only through the `ident` filter (`'files_' ~ entity | ident`, `_IDENT_RE = ^[a-z_][a-z0-9_]{0,127}$` in herness/model/sqlfiles.py:40/136-140), which raises `ConfigError` rather than emitting unsafe text. Column names discovered from the lake schema go through the same `ident` filter (and again through `raw()`'s internal `_ident` call). This is defense-in-depth, not a single point of failure — no injection risk found.

Sandbox check (named risk: T02-11 sandbox denies str/list methods): the new templates use only `.get()` (allowed on `Mapping` via `_MAPPING_READS` and on `LakeInventory`/`_LakeProxy` via `_ALLOWED_ATTRS`, sqlfiles.py:76-80), `.columns` (an allowed `EntityInventory` dataclass field), `in` membership tests against a literal tuple, and the `sort` Jinja filter — none of these are denied str/list methods, and rendering succeeded in the test run.

## Metadata/`_payload` exclusion check

`META` tuple in 140/150/160 (`_record_id`, `_source`, `_entity`, `_source_key`, `_source_updated_at`, `_fetched_at`, `_deleted`, `_payload`) matches `META_COLUMNS` in herness/store/lake.py:35-43 exactly (8/8). Confirmed by test assertion (`"_payload" not in names`) and by direct comparison.

## Verification performed

- Read the full diff (b338534..9e69e9a) against `_macros.jinja`, `lakeinfo.py`, `render_context.py`, `sqlfiles.py`, `010_macros.sql`, `store/lake.py` (all pre-existing, unmodified by this diff) to confirm the new SQL correctly uses existing infrastructure.
- Ran `PYTHONUTF8=1 uv run pytest tests/integration/model -q -p no:logging`: 33 passed (matches report's claim of the IT02-33 additions plus pre-existing T02-13 tests, none regressed).
- Ran `uv run ruff check herness/model/sql tests/integration/model` and `uv run ruff format --check herness/model tests/integration/model`: clean.
- Confirmed the task card (docs/impl/02-data-model.impl.md:3895-3919) matches the controller's ruling on U02-115/170_stg_dataverse.sql scope.
- Confirmed IT02-08 and IT02-16/17 ID-reuse claims against actual test files and spec text.

## Findings

No Critical or Important findings.

### Minor
- `tests/integration/model/_stg_lake.py:176-183` (`build()`'s `# noqa: PLR0913`): pushes the helper to 7 keyword args; acceptable per the report's stated rationale (mirrors `RefData`/`RenderContext` fields one-for-one) and is test-only infrastructure, not production code — not a blocker.
- Generic-staging tables (140/150/160) are invisible to `tools/sql_coverage`'s `tables_created()` regex because their names are template-time expressions (`stg.{{ ... | ident }}`), so `UT11-41` cannot force future coverage of new dynamically-named tables. This is a pre-existing tool limitation correctly flagged by the report as a concern, not something this card introduced or was asked to fix.
- The brief's own "Unit specs (verbatim)" table (T02-14-review inputs) merges U02-113/114/115 and duplicates the Module map row four times — a brief-generation artifact, not a code defect; the build agent correctly deferred to the actual task card and flagged the discrepancy for confirmation, which this review confirms was the right call.

## Verdict

**Approved**
