# 01 — Connectors: Implementation Specification

Status: Draft v1 · 2026-09-24
Design spec: [`docs/specs/01-connectors.md`](../specs/01-connectors.md) (Draft v2), with shared contracts from [`00-overview-and-contracts.md`](../specs/00-overview-and-contracts.md)
Phase: 1 (settings, runner, watermarks, backfill, reconciliation, deletion filter, files connector); 6 (HTTP layer, auth, ServiceNow, Jira, monitoring adapters, MongoDB, Snowflake, Dataverse)
Depends on implementation specs: 00 (core ids, errors, time, logging), 02 (`LakeWriter`, ops core API `connection()`/`run_write()`/`read_one()`/`read_all()`/`dump_json()`/`load_json()`, ops migration 001, staging SQL renderer), 08 (`herness.core.resilience`, `herness.core.jobs`, `record_metric_samples`), 09 (CLI wiring of `herness sync`), 10 (config loader and `sources.yaml` root assembly, registry, secrets, egress client factories, socket guard), 11 (fixtures, synthetic data, fakes)
Standards: [`ENG-STANDARDS.md`](ENG-STANDARDS.md) (cited as `ENG §n`). Consistency rulings: [`DECISIONS.md`](DECISIONS.md) (cited as `R-nn`); this revision applies R-03, R-06, R-08–R-12, R-39, R-40, R-42, R-46, R-59, R-62 and R-63.

Cross-spec references to units of other implementation specs are written `X:<NN>/<qualified symbol or artifact>` until the consistency pass resolves them to task IDs.

---

## 1. Scope and traceability

This spec builds everything under `herness/connectors/` plus the ops-store area `herness/store/ops/ingest.py` (R-08): every function over the three ingestion tables this component owns (`watermark`, `sync_slice`, `file_ingest`), the deletion-set read over `deletion_request`, and every other ingest-area function another spec references (R-09). It also owns the Jira raw column-name contract that the connector writes to the lake (R-59, §4.5). It covers the section models for `config/sources.yaml: sources`, the shared protocols and lake-row helpers, the sync runner (incremental, backfill, reconciliation, deletion filtering, schema-drift handling, orphan temp cleanup), the `sync` and `reconcile` job handlers, the behavior of `herness sync`, the HTTP and auth layer used by live sources, and one connector per source: Files (Phase 1), ServiceNow, Jira, monitoring (Prometheus/Mimir, Datadog, Splunk, Dynatrace), MongoDB, Snowflake and Dataverse (Phase 6). It does not build the lake writer, retry and breaker mechanics, the job queue, secret storage, the deletion procedure, or staging SQL; it consumes them.

### 1.1 Traceability matrix

| Design § | Requirement (short) | Impl § | Units | Tasks | Tests |
|----------|---------------------|--------|-------|-------|-------|
| 01 §1 | Connectors copy source records into the lake incrementally, idempotently, resumably; no typing or joins | 1, 2, 5 | U01-16, U01-36–U01-45 | T01-06, T01-07, T01-08 | UT01-29–UT01-44, FT01-01 |
| 01 §2 | Register every connector; overlap fetch; watermark after commit; tombstones; slices; deletion filter; spec 00 errors; `Retry-After`; monitoring events and daily aggregates only | 3, 5, 6 | U01-16–U01-18, U01-33, U01-38–U01-45, U01-55, U01-60, U01-78 | T01-03, T01-05, T01-06, T01-07, T01-08, T01-11, T01-14, T01-19 | UT01-25, UT01-31, UT01-40, UT01-60, UT01-61, UT01-79, UT01-94 |
| 01 §3.1 | `Connector` / `SupportsKeyListing` contract: ≤ `batch_rows` batches with the 8 metadata columns, ascending order, `since` inclusive, `until` exclusive, no file I/O / ops writes / deletion filtering in `sync()` | 3.3, 3.4 | U01-16–U01-21, U01-25 | T01-03 | UT01-14, UT01-18, UT01-94, PT01-03 |
| 01 §3.2 | `SyncRunner`, `SyncResult`, one `LakeWriter` per checkpoint, `abort()` on exception, `SourceSettings`, CLI behavior | 3.7, 3.10, 5 | U01-05, U01-36–U01-42, U01-54 | T01-01, T01-06, T01-11 | UT01-29–UT01-36, UT01-57 |
| 01 §3.3 | Monitoring connector with entities `event`, `metric_daily`; `MonitoringAdapter` protocol; `source_tool` column | 3.18 | U01-18, U01-78–U01-85 | T01-06, T01-19, T01-20, T01-21 | UT01-79–UT01-83, UT01-92 |
| 01 §4.1 | Lake row rules: `_source_key`, `_record_id`, `_source_updated_at` parsing, `_payload`, flattening, tombstones | 3.4 | U01-19–U01-26 | T01-03 | UT01-14–UT01-19, PT01-01, PT01-04, PT01-05 |
| 01 §4.2 ServiceNow | Entities, fields, `sys_id`, `sys_updated_on` | 3.16 | U01-07, U01-66–U01-70 | T01-02, T01-16 | UT01-67–UT01-71 |
| 01 §4.2 Jira | `issue` row with complete `changelog`, `issuelinks`, `remotelinks`; raw column-name contract owned here (R-59) | 3.17, 4.5 | U01-08, U01-71–U01-77, U01-93 | T01-02, T01-17, T01-18 | UT01-72–UT01-78, UT01-96, IT01-11 |
| 01 §4.2 Monitoring | `event` and `metric_daily` columns and source keys | 3.18 | U01-80, U01-81 | T01-19 | UT01-79 |
| 01 §4.2 MongoDB, Snowflake, Dataverse, Files | Config-defined entities (`key_field`, `updated_field`, fields); `--check-mapping` | 3.13, 3.19–3.21, 3.11 | U01-10–U01-13, U01-46–U01-49, U01-56, U01-86–U01-91 | T01-02, T01-09, T01-12, T01-22–T01-24 | UT01-58, UT01-84–UT01-91, IT01-07 |
| 01 §5.1 | Incremental sync steps 1–5, overlap, settle; open breaker ends the job `done` with `skipped_open_circuit` (R-39) | 5 (F01-01) | U01-38–U01-40, U01-51 | T01-06, T01-11 | UT01-29–UT01-36, UT01-54, IT01-02, IT01-03 |
| 01 §5.2 | Orphan temp cleanup > 1 h; schema drift → new writer; `schema_drift` log | 3.6, 5 (F01-01) | U01-34, U01-35, U01-40 | T01-05, T01-06 | UT01-26, UT01-27, UT01-34, IT01-04, FT01-07 |
| 01 §5.3 | Watermark storage, invariant, monitoring per tool (R-62), files use `file_ingest`; watermark listing for the build (R-09) | 3.5, 4 | U01-27, U01-28, U01-39, U01-92 | T01-04, T01-06 | UT01-20, UT01-31, UT01-36, UT01-92, UT01-95 |
| 01 §5.4 | Tombstones from source deletes; weekly key reconciliation; safety valve | 3.9, 5 (F01-03) | U01-44, U01-45, U01-69 | T01-08, T01-16 | UT01-40–UT01-44, UT01-70, IT01-05, ST01-13 |
| 01 §5.5 | Parallel date slices, `sync_slice` states, restart, final watermark, `backfill.start`; `--full` = backfill from `backfill.start` to now (R-63) | 3.8, 5 (F01-02) | U01-30, U01-31, U01-41, U01-43, U01-54 | T01-04, T01-07, T01-11 | UT01-22, UT01-23, UT01-37–UT01-39, UT01-57, UT01-93, FT01-03 |
| 01 §5.6 | Deletion set loaded before each run and at each checkpoint; `pc.is_in` filter; never logged by value | 3.6, 5 | U01-29, U01-33, U01-40 | T01-04, T01-05, T01-06 | UT01-21, UT01-25, UT01-31, UT01-42, PT01-06, IT01-09, ST01-11, BT01-02 |
| 01 §5.7 | ServiceNow Table API, query, paging, offset drift, windows, rate limits, auth | 3.14–3.16 | U01-58–U01-70 | T01-14, T01-15, T01-16 | UT01-60–UT01-71, IT01-10 |
| 01 §5.8 | Jira Cloud and DC search, JQL, changelog, remote links, discovery, rate limits, auth | 3.17 | U01-71–U01-77 | T01-17, T01-18 | UT01-72–UT01-78 |
| 01 §5.9 Prometheus/Mimir | `query_range` `step=1d`, tenant header, no events | 3.18 | U01-82 | T01-20 | UT01-80, UT01-09 |
| 01 §5.9 Datadog | Events search cursor; metrics rollup; keys; `X-RateLimit-Reset` | 3.18 | U01-83 | T01-21 | UT01-81 |
| 01 §5.9 Splunk | Export streaming; aggregation rule; `ConfigError` on forbidden SPL | 3.18 | U01-84 | T01-21 | UT01-82, UT01-08 |
| 01 §5.9 Dynatrace | Problems with `nextPageKey`; metrics `resolution=1d`; `Api-Token` | 3.18 | U01-85 | T01-20 | UT01-83 |
| 01 §5.9 MongoDB | `find` with bounds, sort, `secondaryPreferred`, `maxTimeMS`, index warning, `str(_id)` | 3.19 | U01-86, U01-87 | T01-22 | UT01-84, UT01-85 |
| 01 §5.9 Snowflake | Bounded ordered SELECT, Arrow batches, `QUERY_TAG`, timeout, EXPLAIN scan guard, resource monitor | 3.20 | U01-88, U01-89 | T01-23 | UT01-86–UT01-88, ST01-16 |
| 01 §5.9 Dataverse | OData query, `Prefer` headers, `@odata.nextLink`, FormattedValue, MSAL token cache, 429 | 3.21 | U01-65, U01-90, U01-91 | T01-15, T01-24 | UT01-89–UT01-91 |
| 01 §5.10 | Files inbox, DuckDB readers, SHA-256 fingerprint, key and updated fields, delta and snapshot modes, never move or delete | 3.11, 3.12, 5 (F01-04) | U01-46–U01-50 | T01-09, T01-10 | UT01-45–UT01-53, IT01-01, FT01-02 |
| 01 §6 | Error mapping; page-level retry; breaker keys; crash safety; entity isolation | 6 | U01-51, U01-59, U01-60, U01-61 | T01-11, T01-14 | UT01-54, UT01-55, UT01-60, UT01-61, FT01-04–FT01-06 |
| 01 §7 | `sources.yaml: sources` shape, common keys, `secret:` refs, unknown keys, guard validations, `hosts` allowlist for SDK sources (R-06) | 9, 3.1, 3.2 | U01-01–U01-15 | T01-01, T01-02 | UT01-01–UT01-13, UT01-97, ST01-07 |
| 01 §8 | Throughput per source, incremental < 5 min, RSS < 1.5 GB, deletion filter < 5 % | 10 | U01-33, U01-40 | T01-25 | BT01-01–BT01-06 |
| 01 §9 | Dedicated read-only identities, secrets never exposed, TLS always verified, folder ACLs, deletion honored, clients only from `herness.core.egress` or listed SDK hosts (R-06) | 7 | U01-05, U01-15, U01-58, U01-63–U01-65, U01-86 | T01-01, T01-14, T01-15, T01-22 | ST01-01–ST01-17 |
| 01 §10 | Acceptance tests table | 11 | all | all | every test in §11 |
| 01 §11 | Open questions Q1–Q11 | 13 | — | T01-16, T01-17, T01-24 (blocked items) | — |
| 01 §12 | Spec and package dependencies | 14 | — | — | — |
| 01 §13 | Resolved contract changes C1–C9 | 13.1 | U01-16, U01-17, U01-30, U01-32 | T01-03, T01-04 | UT01-22, UT01-24 |
| 01 §8 "Performance targets" | See row 01 §8 above | 10 | — | T01-25 | BT01-01–BT01-06 |

---

## 2. Module map

Default layer imports per ENG §2.1: L2 may import L0 (`herness.core.*`) and L1 (`herness.store.*`). All line budgets are hard caps for the file (ENG §2.4 limit is 400).

| Path | Purpose | Public symbols | Layer | Extra imports | Line budget |
|------|---------|----------------|-------|---------------|-------------|
| `herness/connectors/__init__.py` | Package marker. No logic, no imports of submodules | — | L2 | — | 10 |
| `herness/connectors/settings_base.py` | Common pydantic section models and closed-set constants | `AuthSettings`, `ReconcileSettings`, `BackfillSettings`, `EntitySettings`, `SourceSettings`, `AUTH_METHODS`, `CONCURRENCY_DEFAULTS`, `CONCURRENCY_CAPS`, `DAILY_METRIC_NAMES`, `SECRET_REF_PATTERN`, `SERVICENOW_ENTITIES` | L2 | none: settings import rule (R-03, ENG §2.1): standard library, pydantic, `herness.core.types`, `herness.core.errors` only | 260 |
| `herness/connectors/settings.py` | Per-source section models, the `sources` section and its versioned wrapper, host allowlist | `ServiceNowSettings`, `ServiceNowEntity`, `JiraSettings`, `JiraEntity`, `MonitoringSettings`, `MonitoringAdapterSettings`, `MetricQuery`, `validate_spl`, `MongoSettings`, `MongoEntity`, `SnowflakeSettings`, `SnowflakeEntity`, `DataverseSettings`, `DataverseEntity`, `FilesSettings`, `FilesEntity`, `SourcesSection`, `SourcesConfig`, `allowed_hosts`, re-export `SourceSettings` | L2 | none beyond `herness.connectors.settings_base`: settings import rule (R-03); `dq` and `build` are added by X:10 when it assembles the file model (§13 D-16) | 390 |
| `herness/connectors/base.py` | Shared protocols, metadata constants, `record_id`, `split_range`, re-export of `http_client` | `Connector`, `SupportsKeyListing`, `SupportsToolStreams`, `METADATA_FIELDS`, `METADATA_SCHEMA`, `KEY_SCHEMA`, `DEFAULT_BATCH_ROWS`, `DEFAULT_CHECKPOINT_ROWS`, `UNORDERED_SOURCES`, `record_id`, `split_range`, `http_client` | L2 | `pyarrow` | 170 |
| `herness/connectors/rows.py` | Pure helpers that turn source records into lake batches | `to_snake`, `flatten_record`, `parse_source_timestamp`, `parse_arrow_timestamps`, `RowBatcher`, `tombstone_batch` | L2 | `pyarrow` | 320 |
| `herness/store/ops/ingest.py` | Ingest area of the ops store (R-08): functions for `watermark`, `sync_slice`, `file_ingest`, and the deletion-set read; re-exported by `herness/store/ops/__init__.py` in the spec 01 `__all__` block | `Watermark`, `SliceRow`, `FileIngestRow`, `get_watermark`, `set_watermark`, `list_watermarks`, `deleted_record_ids`, `ensure_slices`, `mark_slice_running`, `mark_slice_done`, `mark_slice_failed`, `get_file_ingest`, `record_file_ingest` | L1 | — | 260 |
| `herness/connectors/deletion.py` | In-memory deletion set and batch filter | `DeletionFilter` | L2 | `pyarrow` | 90 |
| `herness/connectors/lakefiles.py` | Orphan temp cleanup and per-writer schema tracking | `cleanup_orphan_temp_files`, `SchemaTracker`, `SchemaDrift` | L2 | `pyarrow` | 160 |
| `herness/connectors/runner.py` | `SyncRunner` and `SyncResult`; incremental flow and the shared write loop | `SyncRunner`, `SyncResult` | L2 | `pyarrow` | 390 |
| `herness/connectors/backfill.py` | Slice planning and parallel slice execution | `run_backfill` | L2 | — | 260 |
| `herness/connectors/reconcile.py` | Key reconciliation with DuckDB anti-join and safety valve | `reconcile_entity`, `find_missing_keys` | L2 | `duckdb`, `pyarrow` | 260 |
| `herness/connectors/files.py` | Files connector: inbox listing, fingerprint, DuckDB readers | `FilesConnector`, `InboxFile`, `InboxFileChanged`, `fingerprint_file`, `MAX_INBOX_FILE_BYTES` | L2 | `duckdb`, `pyarrow` | 360 |
| `herness/connectors/files_ingest.py` | Runner path for files: fingerprint dedupe, commit, `file_ingest`, snapshot reconcile | `ingest_files` | L2 | `pyarrow` | 250 |
| `herness/connectors/jobs.py` | `sync` and `reconcile` job handlers; CLI payload builder | `handle_sync`, `handle_reconcile`, `register_job_handlers`, `build_sync_payload` | L2 | — | 320 |
| `herness/connectors/factory.py` | Builds a configured connector from the registry | `build_connector` | L2 | — | 90 |
| `herness/connectors/mapping_check.py` | `herness sync --check-mapping` | `check_mapping`, `MappingIssue`, `STAGING_FILES` | L2 | `sqlglot`, `herness.model.build` (import-linter exception: X:02 SQL renderer only) | 260 |
| `herness/connectors/health.py` | Per-source health for `herness doctor` | `source_health_report`, `SourceHealth` | L2 | — | 130 |
| `herness/connectors/http.py` | Obtains the source HTTP client from `herness.core.egress` (R-06), page fetch with retry, HTTP error mapping | `http_client`, `SourceHttp`, `JsonPage`, `CursorGuard`, `map_http_error`, `parse_retry_after`, `ForeignHostError`, `SourceNotFound`, `MAX_RESPONSE_BYTES`, `MAX_LINE_BYTES`, `MAX_PAGES_PER_STREAM` | L2 | `httpx` (types, `httpx.Auth`, exceptions; never constructs a client or transport) | 320 |
| `herness/connectors/auth.py` | Auth builders per method; OAuth and MSAL token caches | `build_auth`, `OAuthTokenAuth`, `MsalTokenProvider`, `StaticHeaderAuth` | L2 | `httpx`, `msal` | 280 |
| `herness/connectors/servicenow.py` | ServiceNow Table API connector | `ServiceNowConnector`, `build_sn_query`, `merge_by_time` | L2 | `httpx` (types only) | 390 |
| `herness/connectors/jira.py` | Jira Cloud and DC connector: search, keys, field discovery, raw column contract (R-59) | `JiraConnector`, `build_jql`, `flatten_issue`, `FieldCandidate`, `JIRA_FIELDS`, `JIRA_ISSUE_COLUMNS` | L2 | — | 390 |
| `herness/connectors/jira_changelog.py` | Complete changelog and remote links per search page; history and link projection | `fetch_changelogs`, `fetch_remote_links`, `ChangelogState`, `project_history`, `project_remote_link` | L2 | — | 270 |
| `herness/connectors/monitoring/__init__.py` | Package marker | — | L2 | — | 10 |
| `herness/connectors/monitoring/base.py` | `MonitoringAdapter` protocol, `MonitoringConnector`, row builders, day helpers | `MonitoringAdapter`, `MonitoringConnector`, `EventRow`, `MetricRow`, `event_batch`, `metric_batch`, `EVENT_COLUMNS`, `METRIC_COLUMNS`, `floor_day`, `complete_days` | L2 | `pyarrow` | 320 |
| `herness/connectors/monitoring/prometheus.py` | Prometheus and Mimir adapter | `PrometheusAdapter` | L2 | — | 210 |
| `herness/connectors/monitoring/datadog.py` | Datadog adapter | `DatadogAdapter` | L2 | — | 260 |
| `herness/connectors/monitoring/splunk.py` | Splunk export adapter (SPL is validated by `settings.validate_spl`) | `SplunkAdapter` | L2 | — | 250 |
| `herness/connectors/monitoring/dynatrace.py` | Dynatrace adapter | `DynatraceAdapter` | L2 | — | 250 |
| `herness/connectors/mongodb.py` | MongoDB connector | `MongoConnector` | L2 | `pymongo`, `bson` | 320 |
| `herness/connectors/snowflake.py` | Snowflake connector with scan guard | `SnowflakeConnector` | L2 | `snowflake.connector`, `pyarrow` | 340 |
| `herness/connectors/dataverse.py` | Dataverse Web API connector | `DataverseConnector` | L2 | — | 290 |

Registration: each connector class is decorated with `X:10/herness.core.registry.register("connector", <name>)`, each adapter with `register("monitoring_adapter", <tool>)`. The static `_BUILTINS` table of X:10 lists `"herness.connectors.<module>:<Class>"` for each. Lazy import means `import herness.connectors` never imports `pymongo`, `snowflake.connector`, `msal` or `duckdb`.

Import-linter exceptions this spec needs (listed in `pyproject.toml`): `herness.connectors.mapping_check -> herness.model.build` (independence contract between the L2 siblings). The former exception `herness.connectors.settings -> herness.model.settings` is removed (R-03). `herness.connectors.settings` and `herness.connectors.settings_base` are both listed in the settings import contract of ENG §2.1 (§13 O-15). No egress-lint exception: no module under `herness/connectors/` constructs an `httpx` client or transport (R-06, ENG §2.1). HTTP sources obtain their client from `herness.core.egress` (U01-58). Vendor SDKs (`pymongo`, `snowflake.connector`, `msal`) build their own network clients only for hosts listed in `sources.<name>.hosts` (U01-05, U01-15).

---

## 3. Unit specs

Conventions for every unit below:

- "Config models" are pydantic v2 models with `model_config = ConfigDict(extra="forbid", strict=True, frozen=True)`. Fields typed `float` are declared with `Field(strict=False)` so YAML integers are accepted (reason: `yaml.safe_load` yields `int` for `2`). Validators raise `ValueError` with a message that names the key path and never echoes a credential value; the spec 10 loader (X:10/herness.core.config.load_config) converts `pydantic.ValidationError` into `ConfigError`. This is the only place a built-in exception leaves a unit, and it never crosses the loader boundary.
- Clock: every I/O unit that needs the time takes `clock: Callable[[], datetime]` defaulting to X:00/herness.core.time.now_utc. Pure units take `now: datetime`.
- Timestamps written to the ops store use X:00/herness.core.time.format_fixed (fixed-width `YYYY-MM-DDTHH:MM:SS.ffffffZ`, spec 00 §8) and are read with X:00/herness.core.time.parse_fixed.
- Logging uses X:00/herness.core.logging.get_logger with `component="connectors"`; event names are listed in §8.
- "Emits metric" means one call to X:08/herness.store.ops.record_metric_samples (R-12), the only writer of `metric_sample`.
- Ops-store access uses only the impl 02 core API (R-10): X:02/herness.store.ops.core.connection, run_write, read_one, read_all, dump_json, load_json. No unit of this spec takes or builds a store object; the per-thread connection comes from `connection()`.
- "Error class (ids)" in an Errors cell means: the taxonomy class raised, and the identifiers its message carries. Messages never carry secret values, record text or personal data.

### 3.1 Common settings (`herness/connectors/settings_base.py`)

#### U01-01 herness.connectors.settings_base.AuthSettings

| Field | Type | Default | Constraint |
|-------|------|---------|------------|
| `method` | `str` | required | Member of `AUTH_METHODS[<auth key>]`; checked by the owning source model (U01-05 step 3), because the allowed set depends on the source |
| `credentials` | `str \| None` | `None` | Matches `SECRET_REF_PATTERN`; `None` only when `method == "none"` |
| `tenant_id` | `str \| None` | `None` | Required when `method == "msal_client_credentials"`, else must be `None`; GUID regex `^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$` |

| Item | Content |
|------|---------|
| Kind | class (config model) |
| Purpose | Holds the auth method and the `secret:` reference for one source or adapter. |
| Preconditions | Input is the parsed YAML mapping under `auth`. |
| Postconditions | `credentials` is a well-formed `secret:<name>` reference, or `None` with method `none`. |
| Invariants | Frozen; never holds a resolved secret value. |
| Algorithm | 1. Field validator on `credentials`: a value not matching `SECRET_REF_PATTERN` raises `ValueError("auth.credentials must be a secret:<name> reference")`; the message never includes the value. 2. Model validator: `method == "none"` with `credentials` set → `ValueError("auth.credentials must be empty for method none")`; `method != "none"` with `credentials` `None` → `ValueError("auth.credentials is required")`. 3. Model validator: `tenant_id` rule from the field table. |
| Side effects | None. |
| Errors | Plain-text or malformed credential → `ConfigError` via loader (path `sources.<source>.auth.credentials`); `tenant_id` rule → `ConfigError` (path `auth.tenant_id`). |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | TH01-03 (no plain-text secrets in config). |
| Tests | UT01-01, ST01-03 |

#### U01-02 herness.connectors.settings_base.ReconcileSettings

| Field | Type | Default | Constraint |
|-------|------|---------|------------|
| `schedule` | `str` | `"0 3 * * SUN"` | Five whitespace-separated fields, each matching `^[0-9A-Za-z*/,\-]+$`. Semantic cron validity is checked by the X:08 scheduler validation that X:10 `validate` runs |
| `max_delete_pct` | `float` | `2.0` | `0 < x ≤ 100` |

| Item | Content |
|------|---------|
| Kind | class (config model) |
| Purpose | Weekly reconciliation schedule and safety-valve threshold for one source. |
| Preconditions | — |
| Postconditions | Values within the constraints. |
| Invariants | Frozen. |
| Algorithm | Field validators apply the constraints; a violation raises `ValueError` naming `reconcile.schedule` or `reconcile.max_delete_pct`. |
| Side effects | None. |
| Errors | Constraint violation → `ConfigError` via loader. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | TH01-13 (valve threshold bounded). |
| Tests | UT01-05 |

#### U01-03 herness.connectors.settings_base.BackfillSettings

| Field | Type | Default | Constraint |
|-------|------|---------|------------|
| `start` | `datetime.date \| None` | `None` | `None` means `now − 1,096 days` (3 years, spec 02 §9); a date must be ≥ 1970-01-01 |
| `slice_days` | `int` | `30` | `1 ≤ x ≤ 366` |

Method `resolve_start(now: datetime) -> datetime`: returns `start` at 00:00:00 UTC, or `now − timedelta(days=1096)` truncated to 00:00:00 UTC when `start` is `None`.

| Item | Content |
|------|---------|
| Kind | class (config model) with one method |
| Purpose | Initial backfill range start and slice width. |
| Preconditions | `now` is timezone-aware (`resolve_start`). |
| Postconditions | `resolve_start` returns an aware UTC datetime < `now`. |
| Invariants | Frozen. |
| Algorithm | As in the field table and the method description. |
| Side effects | None. |
| Errors | Naive `now` → `ConfigError("naive datetime")`; result ≥ `now` → `ConfigError("backfill.start is in the future")`. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT01-05 |

#### U01-04 herness.connectors.settings_base.EntitySettings

| Field | Type | Default | Constraint |
|-------|------|---------|------------|
| `overlap_minutes` | `int \| None` | `None` | `0 ≤ x ≤ 1440`; `None` inherits the source value |
| `page_size` | `int \| None` | `None` | Bounds of the owning source (checked by the source model); `None` inherits |
| `backfill` | `BackfillSettings \| None` | `None` | Fields set here override source-level fields one by one (`model_fields_set`) |

| Item | Content |
|------|---------|
| Kind | class (config model; base of every per-source entity model) |
| Purpose | Per-entity overrides of source-level settings. |
| Preconditions | — |
| Postconditions | Unset fields are `None`. |
| Invariants | Frozen. |
| Algorithm | Field validators apply the constraints. |
| Side effects | None. |
| Errors | Constraint violation → `ConfigError` via loader. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT01-05 |

#### U01-05 herness.connectors.settings_base.SourceSettings

| Field | Type | Default | Constraint |
|-------|------|---------|------------|
| `SOURCE` | `ClassVar[str]` | set by subclass | Connector name: `servicenow`, `jira`, `monitoring`, `mongodb`, `snowflake`, `dataverse`, `files` |
| `enabled` | `bool` | `False` | — |
| `base_url` | `str \| None` | `None` | Parsed with `urllib.parse.urlsplit`: scheme `https`, or `http` only when the host is `127.0.0.1`, `localhost` or `::1`; no userinfo, query or fragment; a trailing `/` is stripped. Subclasses mark it required or forbidden |
| `auth` | `AuthSettings \| None` | `None` | `auth.method` ∈ `AUTH_METHODS[self.auth_key()]` |
| `page_size` | `int` | subclass default | Subclass bounds |
| `batch_rows` | `int` | `10000` | `1000 ≤ x ≤ 100000` |
| `checkpoint_rows` | `int` | `500000` | `batch_rows ≤ x ≤ 5000000` |
| `overlap_minutes` | `int` | `30` (subclass may change) | `0 ≤ x ≤ 1440` |
| `settle_seconds` | `int` | `60` | `0 ≤ x ≤ 3600` |
| `max_concurrency` | `int` | `CONCURRENCY_DEFAULTS[SOURCE]` | `1 ≤ x ≤ CONCURRENCY_CAPS[SOURCE]` |
| `schedule` | `str \| None` | `None` | Five-field rule of U01-02; `None` = not scheduled |
| `reconcile` | `ReconcileSettings` | `ReconcileSettings()` | — |
| `backfill` | `BackfillSettings` | `BackfillSettings()` | — |
| `timeout_s` | `float` | `60.0` | `1 ≤ x ≤ 600` |
| `verify` | `pathlib.Path \| None` | `None` | `None` = system trust store, verification on. A path must name an existing file. A boolean is rejected with `ValueError("TLS verification cannot be disabled; set verify to a CA bundle path")` |
| `entities` | `Mapping[str, EntitySettings]` | subclass | Subclass narrows the model type and the allowed names |
| `hosts` | `tuple[str, ...]` | `()` | Key `sources.<name>.hosts` (R-06). Each entry is lower-cased, then must match `^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$` (the X:10 C04 host rule) and must not be an IP literal; no port, no duplicates, at most 50 entries. Required non-empty for SDK sources (`mongodb`, `snowflake`, `dataverse`; checked by the subclasses, U01-10–U01-12); optional for the other sources, where the entries only widen the socket-guard allowlist (U01-15) and never the host the HTTP client reaches (U01-58) |

Methods:

| Method | Returns | Behavior |
|--------|---------|----------|
| `auth_key() -> str` | key into `AUTH_METHODS` | Default `SOURCE`; overridden by `JiraSettings` and adapter validation |
| `entity(name: str) -> EntitySettings` | entity model | `ConfigError(f"unknown entity {name} for source {SOURCE}")` when absent |
| `overlap_for(entity: str) -> timedelta` | overlap | Entity `overlap_minutes` if set, else the source value |
| `page_size_for(entity: str) -> int` | page size | Entity value if set, else the source value |
| `backfill_for(entity: str) -> BackfillSettings` | merged model | Source `backfill`, with each field in the entity backfill's `model_fields_set` replaced |
| `httpx_verify() -> bool \| str` | `httpx` `verify` value | `True` when `verify` is `None`, else `str(verify)` |

| Item | Content |
|------|---------|
| Kind | class (config model; base of every source model) |
| Purpose | Common keys of every source section (design 01 §7). Re-exported as `herness.connectors.settings.SourceSettings`. |
| Preconditions | Parsed YAML mapping for one source. |
| Postconditions | All constraints hold; TLS verification can never be off. |
| Invariants | Frozen; holds no secret values. |
| Algorithm | 1. Field validators per the table. 2. Model validator: `checkpoint_rows ≥ batch_rows`. 3. Model validator: when `auth` is set, `auth.method` must be in `AUTH_METHODS[self.auth_key()]`; the value `oauth_3lo` gets the message `"auth.method oauth_3lo is not supported in v1"` (see §13 O-8). 4. Field validator on `hosts`: lower-case each entry, then apply the host rule; a violation raises `ValueError("hosts entry is not a host name")` naming the index, never a derived value. |
| Side effects | `verify` validator calls `Path.is_file()` once. |
| Errors | Constraint → `ConfigError` via loader with the key path; `entity()` → `ConfigError(source, entity)`. |
| Concurrency | Immutable. |
| Complexity and limits | O(entities). |
| Security notes | TH01-01 (TLS cannot be disabled), TH01-02 (base URL shape), TH01-18 (`hosts` allowlist for SDK sources). |
| Tests | UT01-02, UT01-03, UT01-04, UT01-05, UT01-97, ST01-01 |

#### U01-06 herness.connectors.settings_base constants

| Constant | Type | Value |
|----------|------|-------|
| `SECRET_REF_PATTERN` | `re.Pattern[str]` | `^secret:[A-Za-z0-9][A-Za-z0-9_.-]{1,63}$` (the name part equals X:10 `SECRET_NAME`) |
| `AUTH_METHODS` | `Mapping[str, frozenset[str]]` | `servicenow`: `oauth_client_credentials`, `oauth_password`, `basic` · `jira:cloud`: `api_token` · `jira:datacenter`: `pat` · `prometheus`: `bearer`, `basic`, `none` · `datadog`: `api_and_app_key` · `splunk`: `bearer` · `dynatrace`: `api_token` · `mongodb`: `connection_string` · `snowflake`: `key_pair` · `dataverse`: `msal_client_credentials` |
| `CONCURRENCY_DEFAULTS` | `Mapping[str, int]` | `servicenow` 4, `jira` 2, `monitoring` 4, `prometheus` 4, `datadog` 2, `splunk` 2, `dynatrace` 2, `mongodb` 4, `snowflake` 2, `dataverse` 4, `files` 1 |
| `CONCURRENCY_CAPS` | `Mapping[str, int]` | `servicenow` 8, `jira` 4, `monitoring` 4, `prometheus` 4, `datadog` 2, `splunk` 2, `dynatrace` 2, `mongodb` 4, `snowflake` 2, `dataverse` 52, `files` 1 (design 01 §8; where that table gives one value it is both default and cap) |
| `DAILY_METRIC_NAMES` | `frozenset[str]` | `availability_pct`, `error_rate`, `request_count`, `alert_firing_minutes` (see §13 O-5) |
| `SERVICENOW_ENTITIES` | `frozenset[str]` | `incident`, `change_request`, `problem`, `cmdb_ci`, `cmdb_ci_service`, `cmdb_rel_ci`, `sys_user_group`, `cmn_department`, `task_sla` (spec 02 §3.1) |

| Item | Content |
|------|---------|
| Kind | constant |
| Purpose | Closed sets used by the settings validators and the runner. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Immutable (`types.MappingProxyType`, `frozenset`). |
| Algorithm | — |
| Side effects | None. |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH01-03 (secret reference shape). |
| Tests | UT01-01, UT01-04, UT01-11 |

### 3.2 Per-source settings (`herness/connectors/settings.py`)

Each source model subclasses `SourceSettings` (U01-05) and adds or narrows the listed fields. Unlisted common fields keep their U01-05 defaults.

`hosts` rule per source (R-06): `MongoSettings`, `SnowflakeSettings` and `DataverseSettings` require it as stated in U01-10–U01-12. `ServiceNowSettings`, `JiraSettings`, `MonitoringSettings` and `FilesSettings` accept it but do not need it: their HTTP client reaches only the `base_url` host whatever `hosts` holds (U01-58). Nothing is derived from `base_url`, `account` or the secret URI for SDK sources; a validator only checks that a required host is present.

#### U01-07 herness.connectors.settings.ServiceNowSettings, ServiceNowEntity

`ServiceNowSettings`: `SOURCE = "servicenow"`; `base_url` required; `auth` required; `page_size` default `1000`, bounds `100–10000`; `overlap_minutes` default `60`; `entities: Mapping[str, ServiceNowEntity]`, non-empty, keys ⊂ `SERVICENOW_ENTITIES`.

| `ServiceNowEntity` field | Type | Default | Constraint |
|-------|------|---------|------------|
| (inherited) | U01-04 | | |
| `fields` | `list[str]` | required | Non-empty; each `^[a-z][a-z0-9_]{0,79}$`; no duplicates |
| `window_hours` | `int` | `24` | `1 ≤ x ≤ 168` |
| `classes` | `list[str] \| None` | `None` | Required for entity `cmdb_ci`, forbidden for others; each `^[a-z][a-z0-9_]{0,79}$` |
| `filter` | `str \| None` | `None` | ≤ 1,000 chars; must not contain `^NQ`, `^EQ`, `ORDERBY`, `\n` or `\r` |

| Item | Content |
|------|---------|
| Kind | class (config model) ×2 |
| Purpose | ServiceNow source section. |
| Preconditions | — |
| Postconditions | For entity key `incident` whose `backfill` does not set `slice_days`, the model validator sets `slice_days = 7` on the entity backfill, keeping any other entity backfill fields (design 01 §5.5). |
| Invariants | Frozen. |
| Algorithm | 1. Field validators per the table. 2. Model validator walks `entities`: `classes` rule by key; the `incident` slice default (built with `model_copy(update=...)`). |
| Side effects | None. |
| Errors | Constraint → `ConfigError` via loader (path `sources.servicenow.entities.<name>.<field>`). |
| Concurrency | Immutable. |
| Complexity and limits | O(entities × fields). |
| Security notes | TH01-07: `^NQ` would start an OR query without the date bounds; `ORDERBY` would break the ascending order the runner relies on. |
| Tests | UT01-06, ST01-07 |

#### U01-08 herness.connectors.settings.JiraSettings, JiraEntity

`JiraSettings`: `SOURCE = "jira"`; `flavor: Literal["cloud", "datacenter"]` required; `auth_key()` returns `f"jira:{flavor}"`; `base_url` and `auth` required; `page_size` default `100`, bounds `1–100` for `cloud` and `1–1000` for `datacenter`; `overlap_minutes` default `60`; `jql_scope: str | None = None` (≤ 2,000 chars; must not contain `order by` case-insensitively, `\n` or `\r`); `fetch_remote_links: bool = False`; `entities: Mapping[str, JiraEntity]`, default `{"issue": JiraEntity()}`, key set must equal `{"issue"}`. `JiraEntity` adds no fields to U01-04.

| Item | Content |
|------|---------|
| Kind | class (config model) ×2 |
| Purpose | Jira source section. |
| Preconditions | — |
| Postconditions | `flavor` decides the allowed auth methods and page bounds. |
| Invariants | Frozen. |
| Algorithm | Field and model validators per the description; `oauth_3lo` is rejected by U01-05 step 3. |
| Side effects | None. |
| Errors | Constraint → `ConfigError` via loader. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | TH01-07 (`jql_scope` cannot reorder results or add lines). |
| Tests | UT01-02, UT01-66, ST01-07 |

#### U01-09 herness.connectors.settings.MonitoringSettings, MonitoringAdapterSettings, MetricQuery, validate_spl

`MonitoringSettings`: `SOURCE = "monitoring"`; `base_url` and `auth` must be `None` (they live on adapters); `entities` optional, default and only allowed keys `event` and `metric_daily` (both present by default; overrides allowed); a `reconcile` key present in the YAML (detected with `model_fields_set`) → `ValueError("monitoring is not reconciled")`; `adapters: Mapping[Literal["prometheus", "datadog", "splunk", "dynatrace"], MonitoringAdapterSettings]`; when `enabled` is true at least one adapter has `enabled: true`.

| `MonitoringAdapterSettings` field | Type | Default | Constraint |
|-------|------|---------|------------|
| `enabled` | `bool` | `False` | — |
| `base_url` | `str` | required | U01-05 URL rules |
| `auth` | `AuthSettings` | required | `auth.method` ∈ `AUTH_METHODS[<tool>]` |
| `page_size` | `int` | datadog `1000`, dynatrace `500`, prometheus and splunk `1000` (unused) | datadog `1–1000`; dynatrace `1–500` |
| `timeout_s` | `float` | `60.0` | `1–600` |
| `verify` | `Path \| None` | `None` | U01-05 rule |
| `tenant` | `str \| None` | `None` | Prometheus only; `^[A-Za-z0-9_.-]{1,150}$` |
| `event_query` | `str \| None` | `None` | datadog: search query ≤ 2,000 chars; splunk: passes `validate_spl`; dynatrace: optional `problemSelector` ≤ 2,000 chars; prometheus: must be `None` |
| `metric_queries` | `list[MetricQuery]` | `[]` | Names unique within the adapter |
| `max_concurrency` | `int` | `CONCURRENCY_DEFAULTS[<tool>]` | `1 ≤ x ≤ CONCURRENCY_CAPS[<tool>]` |

| `MetricQuery` field | Type | Default | Constraint |
|-------|------|---------|------------|
| `name` | `str` | required | ∈ `DAILY_METRIC_NAMES` |
| `query` | `str` | required | 1–4,000 chars; splunk: passes `validate_spl` |
| `agg` | `Literal["avg","sum","min","max","count"] \| None` | `None` | Required for datadog |
| `unit` | `str` | required | `^[a-z_]{1,20}$` |
| `step` | `str` | `"1d"` | Prometheus: `^(\d+)(s\|m\|h\|d)$` equal to exactly 86,400 s, else `ValueError("prometheus step must be 1d")` |
| `service_label` | `str` | `"service"` | `^[A-Za-z_][A-Za-z0-9_.]{0,63}$`; Prometheus label, Datadog tag key |
| `value_field` | `str` | `"value"` | Splunk result field that holds the value; same pattern |
| `service_dimension` | `str` | `"dt.entity.service"` | Dynatrace dimension key; same pattern |

`validate_spl(spl: str) -> None` (pure) raises `ValueError` (→ `ConfigError`) when: (a) the lower-cased text has no match for `\|\s*(stats|tstats|table)\b` and does not start with `| tstats`; (b) it matches `\|\s*(outputlookup|collect|sendemail|script|run|delete|map|sendalert|outputcsv|dbxquery|rest)\b`; (c) it contains a backtick (macro expansion); (d) it is longer than 4,000 chars.

| Item | Content |
|------|---------|
| Kind | class (config model) ×3, function |
| Purpose | Monitoring source section and its adapter and query models. |
| Preconditions | — |
| Postconditions | Every enabled adapter has a URL, credentials and valid queries. |
| Invariants | Frozen. |
| Algorithm | Field validators per the tables. The `MonitoringSettings` model validator applies the tool-specific rules to each adapter by its key (auth methods, `page_size`, `tenant`, `event_query`, `agg`, `step`, `validate_spl`). |
| Side effects | None. |
| Errors | Constraint → `ConfigError` via loader, path `sources.monitoring.adapters.<tool>.<field>`. |
| Concurrency | Immutable. |
| Complexity and limits | O(queries). |
| Security notes | TH01-07 (SPL cannot write, alert, run scripts or call REST endpoints); daily aggregates only (design 01 §2). |
| Tests | UT01-08, UT01-09, UT01-11, ST01-07 |

#### U01-10 herness.connectors.settings.MongoSettings, MongoEntity

`MongoSettings`: `SOURCE = "mongodb"`; `base_url` must be `None`; `hosts` required non-empty: every host the URI names, and for a `mongodb+srv://` URI the SRV name and every SRV target (R-06; checked against the URI at connect time, U01-86); `auth` required with method `connection_string` (the secret holds the full MongoDB URI); `database: str` (`^[A-Za-z0-9_-]{1,64}$`); `max_time_ms: int = 60000` (`1000–600000`); `page_size` default `1000`, bounds `1–10000`; `entities: Mapping[str, MongoEntity]` non-empty, keys `^[a-z][a-z0-9_]{0,63}$`.

| `MongoEntity` field | Type | Default | Constraint |
|-------|------|---------|------------|
| `collection` | `str` | required | `^[A-Za-z0-9_.-]{1,120}$`, not starting with `system.` |
| `key_field` | `str` | `"_id"` | `^[A-Za-z_][A-Za-z0-9_.]{0,127}$` |
| `updated_field` | `str` | required | same pattern |
| `fields` | `list[str]` | required | Non-empty; same pattern; no duplicates |
| `filter` | `dict[str, Any]` | `{}` | JSON-compatible; recursively no key in `{"$where", "$function", "$accumulator"}`; no top-level key equal to `updated_field` or `$or`; ≤ 64 keys per level; nesting ≤ 8 |

| Item | Content |
|------|---------|
| Kind | class (config model) ×2 |
| Purpose | MongoDB source section. |
| Preconditions | — |
| Postconditions | `filter` cannot run server-side JavaScript or override the time bounds and key-set paging. |
| Invariants | Frozen. `filter` is the only `Any` value; it is validated here and passed to `pymongo` unchanged. |
| Algorithm | Field validators per the table; the `filter` walk is iterative with an explicit stack. |
| Side effects | None. |
| Errors | Constraint → `ConfigError` via loader. |
| Concurrency | Immutable. |
| Complexity and limits | O(size of `filter`). |
| Security notes | TH01-07, TH01-18. |
| Tests | UT01-07, UT01-97, ST01-07 |

#### U01-11 herness.connectors.settings.SnowflakeSettings, SnowflakeEntity

`SnowflakeSettings`: `SOURCE = "snowflake"`; `base_url` must be `None`; `hosts` required and must contain `f"{account.lower()}.snowflakecomputing.com"` (else `ValueError("hosts must list the Snowflake account host")`); OCSP responder hosts such as `ocsp.snowflakecomputing.com` are listed by the operator when OCSP checks must succeed (§7.7); `account: str` (`^[A-Za-z0-9_.-]{1,255}$`); `auth` required with method `key_pair`; `warehouse`, `role: str` (identifier pattern `^[A-Za-z_][A-Za-z0-9_$]{0,254}$`); `statement_timeout_s: int = 900` (`1–86400`); `max_scan_gb: float = 50` (`0 < x ≤ 10000`); `page_size` unused (default `10000`); `entities: Mapping[str, SnowflakeEntity]` non-empty, keys `^[a-z][a-z0-9_]{0,63}$`.

| `SnowflakeEntity` field | Type | Default | Constraint |
|-------|------|---------|------------|
| `table` | `str` | required | Exactly three dot-separated parts, each matching the identifier pattern |
| `key_field` | `str` | required | Identifier pattern; member of `columns` |
| `updated_field` | `str` | required | Identifier pattern; member of `columns` |
| `columns` | `list[str]` | required | Non-empty; identifier pattern; no duplicates |
| `filter` | `str \| None` | `None` | ≤ 1,000 chars; no `;`, `--`, `/*`, `*/`; no word (case-insensitive, `\b`-bounded) from `insert, update, delete, merge, drop, alter, create, grant, revoke, call, put, get, copy, use, execute, truncate, undrop` |

| Item | Content |
|------|---------|
| Kind | class (config model) ×2 |
| Purpose | Snowflake source section. |
| Preconditions | — |
| Postconditions | Every identifier is safe to double-quote; `filter` is one read-only predicate. |
| Invariants | Frozen. |
| Algorithm | Field validators per the table. |
| Side effects | None. |
| Errors | Constraint → `ConfigError` via loader. |
| Concurrency | Immutable. |
| Complexity and limits | O(columns). |
| Security notes | TH01-07, TH01-17, TH01-18. |
| Tests | UT01-10, UT01-97, ST01-07 |

#### U01-12 herness.connectors.settings.DataverseSettings, DataverseEntity

`DataverseSettings`: `SOURCE = "dataverse"`; `base_url` and `auth` required; method `msal_client_credentials` with `tenant_id`; `hosts` required and must contain `login.microsoftonline.com`, the MSAL authority host (else `ValueError("hosts must list login.microsoftonline.com")`); the `base_url` host is reached through the egress-built client and is not listed; `page_size` default `5000`, bounds `1–5000`; `entities: Mapping[str, DataverseEntity]` non-empty, keys `^[a-z][a-z0-9_]{0,63}$`.

| `DataverseEntity` field | Type | Default | Constraint |
|-------|------|---------|------------|
| `entityset` | `str` | required | `^[a-z_][a-z0-9_]{0,127}$` |
| `key_field` | `str` | required | same pattern |
| `updated_field` | `str` | `"modifiedon"` | same pattern |
| `select` | `list[str]` | required | Non-empty; same pattern; no duplicates |

| Item | Content |
|------|---------|
| Kind | class (config model) ×2 |
| Purpose | Dataverse source section. |
| Preconditions | — |
| Postconditions | All OData names are plain identifiers, safe to place in URLs without further encoding. |
| Invariants | Frozen. |
| Algorithm | Field validators per the table. |
| Side effects | None. |
| Errors | Constraint → `ConfigError` via loader. |
| Concurrency | Immutable. |
| Complexity and limits | O(select). |
| Security notes | TH01-07, TH01-18. |
| Tests | UT01-02, UT01-13, UT01-97 |

#### U01-13 herness.connectors.settings.FilesSettings, FilesEntity

`FilesSettings`: `SOURCE = "files"`; `base_url` and `auth` must be `None`; `inbox: Path = Path("data/inbox")` (a relative path resolves against `X:10/HernessConfig.paths.data`'s parent directory, i.e. the repository root; the resolved inbox must not be a symlink); `max_concurrency` fixed `1`; `entities: Mapping[str, FilesEntity]` non-empty, keys `^[a-z][a-z0-9_]{0,63}$` (the key is the inbox sub-folder name).

| `FilesEntity` field | Type | Default | Constraint |
|-------|------|---------|------------|
| `pattern` | `str` | required | ≤ 200 chars; no `/`, `\`, `..`, NUL; lower-cased suffix in `.csv`, `.xlsx`, `.parquet` |
| `sheet` | `str \| None` | `None` | Only with `.xlsx`; 1–31 chars; none of `[]:*?/\` |
| `key_field` | `list[str]` | required | 1–10 items; each 1–128 chars |
| `updated_field` | `str \| None` | `None` | 1–128 chars |
| `mode` | `Literal["delta", "snapshot"]` | `"delta"` | — |

| Item | Content |
|------|---------|
| Kind | class (config model) ×2 |
| Purpose | Files source section. |
| Preconditions | — |
| Postconditions | Globs cannot leave the entity folder; the reader is chosen by the suffix. |
| Invariants | Frozen. |
| Algorithm | Field validators per the table. |
| Side effects | None. |
| Errors | Constraint → `ConfigError` via loader. |
| Concurrency | Immutable. |
| Complexity and limits | O(entities). |
| Security notes | TH01-08. |
| Tests | UT01-12, ST01-08 |

#### U01-14 herness.connectors.settings.SourcesSection, SourcesConfig

| `SourcesSection` field | Type | Default |
|-------|------|---------|
| `servicenow` | `ServiceNowSettings \| None` | `None` |
| `jira` | `JiraSettings \| None` | `None` |
| `monitoring` | `MonitoringSettings \| None` | `None` |
| `mongodb` | `MongoSettings \| None` | `None` |
| `snowflake` | `SnowflakeSettings \| None` | `None` |
| `dataverse` | `DataverseSettings \| None` | `None` |
| `files` | `FilesSettings \| None` | `None` |

| `SourcesConfig` field | Type | Default |
|-------|------|---------|
| `version` | `Literal[1]` | required |
| `sources` | `SourcesSection` | `SourcesSection()` |

`SourcesConfig` does not declare `dq` or `build`. Under R-03 a settings module imports only the standard library, pydantic, `herness.core.types` and `herness.core.errors`, so it cannot import X:02/herness.model.settings. X:10/herness.core.config, which may import every settings module, assembles the model of `config/sources.yaml` as a subclass of `SourcesConfig` that adds `dq` (X:02 `DqSettings`) and `build` (X:02 `BuildSettings`); `cfg.sources` is that subclass, so the methods below apply unchanged (§13 D-16, O-11).

Methods on `SourcesConfig`: `enabled_sources() -> list[tuple[str, SourceSettings]]` returns enabled sections in the fixed order `servicenow, jira, monitoring, mongodb, snowflake, dataverse, files`; `source(name: str) -> SourceSettings` returns the section (enabled or not) or raises `ConfigError(f"source {name} is not configured")`.

| Item | Content |
|------|---------|
| Kind | class (config model) ×2 |
| Purpose | Versioned connectors part of `config/sources.yaml`; base class of the file model X:10 assembles (`cfg.sources` in X:10/herness.core.config.HernessConfig). |
| Preconditions | — |
| Postconditions | Unknown source names or keys fail validation. |
| Invariants | Frozen. |
| Algorithm | Pydantic validation; methods as described. |
| Side effects | None. |
| Errors | Unknown key → `ConfigError` via loader; `source()` → `ConfigError(name)`. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT01-02 |

#### U01-15 herness.connectors.settings.allowed_hosts

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `cfg` | `SourcesConfig` | — | positional | validated |

Returns `frozenset[str]`.

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | Hostnames connectors may reach, for the socket guard (X:10/herness.core.egress.install_socket_guard). |
| Preconditions | Validated config. |
| Postconditions | Lower-cased hostnames without ports. |
| Invariants | — |
| Algorithm | 1. For each enabled source with `base_url`, add its host (these are the hosts the egress-built HTTP clients of U01-58 reach). 2. Monitoring: add the `base_url` host of each enabled adapter. 3. For each enabled source, add every entry of its `hosts` list (R-06: SDK sources `mongodb`, `snowflake`, `dataverse`). 4. Nothing else is derived: no host is computed from `account`, the secret URI or `tenant_id`. 5. Return the set. The socket guard allowlist is this set united with the egress allowlist and loopback (R-06; X:10 computes the union). |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(sources). |
| Security notes | TH01-02, TH01-15, TH01-18. |
| Tests | UT01-13, UT01-97 |

### 3.3 Protocols and constants (`herness/connectors/base.py`)

#### U01-16 herness.connectors.base.Connector

Defined in spec 00 §6. This unit restates the contract every connector in this spec meets.

| Member | Signature | Contract |
|--------|-----------|----------|
| `name` | `str` attribute | Registry name and `_source` value |
| `entities` | `tuple[str, ...]` attribute | Configured entity names in config order |
| `check()` | `-> None` | One cheap authenticated read; raises §6 taxonomy errors; no lake or ops writes |
| `sync(entity, since, until=None)` | `(str, datetime \| None, datetime \| None) -> Iterator[pa.RecordBatch]` | Batches of ≤ `batch_rows` rows; the eight metadata columns first with `METADATA_SCHEMA` types, then flattened fields. `since` inclusive; `until` exclusive; `since=None` = source beginning; `until=None` = no upper bound. Ascending `(_source_updated_at, _source_key)` for every connector not in `UNORDERED_SOURCES`. No ops-store access, no deletion filtering, no file I/O except the files connector reading its inbox |
| `watermark_field(entity)` | `(str) -> str` | Source field name stored in `watermark.field` |

| Item | Content |
|------|---------|
| Kind | protocol |
| Purpose | The ingestion edge every source implements. |
| Preconditions | `entity` ∈ `entities`, else the implementation raises `ConfigError(source, entity)`. |
| Postconditions | As in the member table. |
| Invariants | An instance is used by one thread at a time; backfill uses one instance per slice thread (U01-43). |
| Algorithm | — |
| Side effects | Network reads only. |
| Errors | §6 table. |
| Concurrency | Not thread-safe; one instance per thread. |
| Complexity and limits | Memory bounded by one page plus one batch. |
| Security notes | TH01-05. |
| Tests | UT01-94 |

#### U01-17 herness.connectors.base.SupportsKeyListing

| Member | Signature | Contract |
|--------|-----------|----------|
| `list_keys(entity)` | `(str) -> Iterator[pa.RecordBatch]` | Every current source key of the entity, any order, batches of schema `KEY_SCHEMA`, ≤ `batch_rows` rows; key-only queries |

| Item | Content |
|------|---------|
| Kind | protocol (`typing.runtime_checkable`) |
| Purpose | Optional capability used by reconciliation. |
| Preconditions | As U01-16. |
| Postconditions | The stream ends only after the last key; any failure raises instead of ending the stream early. |
| Invariants | — |
| Algorithm | — |
| Side effects | Network reads. |
| Errors | §6 table. |
| Concurrency | As U01-16. |
| Complexity and limits | Memory one page. |
| Security notes | TH01-13. |
| Tests | UT01-94 |

#### U01-18 herness.connectors.base.SupportsToolStreams

| Member | Signature | Contract |
|--------|-----------|----------|
| `tools()` | `-> tuple[str, ...]` | Enabled tool names in config order |
| `stream_key(tool)` | `(str) -> str` | `f"monitoring:{tool}"`: breaker key, `watermark.source`, `sync_slice.source` |
| `sync_tool(tool, entity, since, until)` | `(str, str, datetime \| None, datetime) -> Iterator[pa.RecordBatch]` | The `Connector.sync` batch contract for one tool |

| Item | Content |
|------|---------|
| Kind | protocol (`typing.runtime_checkable`) |
| Purpose | Lets the runner process a fan-out connector one tool at a time with per-tool watermarks and breakers (spec 02 §5.1; design 01 §6). Implemented only by `MonitoringConnector`. |
| Preconditions | `tool` ∈ `tools()`, else `ConfigError(tool)`. |
| Postconditions | — |
| Invariants | — |
| Algorithm | — |
| Side effects | Network reads. |
| Errors | §6 table. |
| Concurrency | As U01-16. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT01-79, UT01-92 |

#### U01-19 herness.connectors.base constants

| Constant | Value |
|----------|-------|
| `METADATA_FIELDS` | `("_record_id", "_source", "_entity", "_source_key", "_source_updated_at", "_fetched_at", "_deleted", "_payload")` |
| `METADATA_SCHEMA` | `pa.schema` in this order: `_record_id` `string` not null; `_source` `string` not null; `_entity` `string` not null; `_source_key` `string` not null; `_source_updated_at` `timestamp("us", tz="UTC")` not null; `_fetched_at` `timestamp("us", tz="UTC")` not null; `_deleted` `bool` not null; `_payload` `string` nullable |
| `KEY_SCHEMA` | `pa.schema([pa.field("_source_key", pa.string(), nullable=False)])` |
| `DEFAULT_BATCH_ROWS` | `10000` |
| `DEFAULT_CHECKPOINT_ROWS` | `500000` |
| `UNORDERED_SOURCES` | `frozenset({"files", "monitoring"})` |

| Item | Content |
|------|---------|
| Kind | constant |
| Purpose | One definition of the spec 02 §3.1 metadata columns for this component. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Equals the columns validated by X:02/herness.store.lake.LakeWriter.write (a unit test writes one batch through a real `LakeWriter`). |
| Algorithm | — |
| Side effects | None. |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT01-18 |

#### U01-20 herness.connectors.base.record_id

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `source` | `str` | — | positional | `^[a-z][a-z0-9_]{0,31}$` |
| `entity` | `str` | — | positional | `^[a-z][a-z0-9_]{0,63}$` |
| `source_key` | `str` | — | positional | 1–512 chars; no character below U+0020 |

Returns `str` = `f"{source}:{entity}:{source_key}"`.

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | Builds `record_id` per spec 00 §5. |
| Preconditions | As in the table. |
| Postconditions | Splitting the result on the first two `:` returns the inputs. |
| Invariants | — |
| Algorithm | 1. Validate the three inputs. 2. Return the joined string. |
| Side effects | None. |
| Errors | Invalid input → `SchemaViolation("invalid record key", source, entity)`; the key value is not in the message. |
| Concurrency | Pure. |
| Complexity and limits | O(len). |
| Security notes | TH01-05. |
| Tests | UT01-14, PT01-05 |

#### U01-21 herness.connectors.base.split_range

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `start` | `datetime` | — | positional | aware |
| `end` | `datetime` | — | positional | aware |
| `step` | `timedelta` | — | positional | > 0 |

Returns `list[tuple[datetime, datetime]]`.

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | Cuts `[start, end)` into consecutive half-open windows (ServiceNow windows, backfill slices). |
| Preconditions | Aware datetimes; `step > 0`. |
| Postconditions | Windows are contiguous and non-overlapping, each ≤ `step`; the first starts at `start`, the last ends at `end`; `start ≥ end` gives an empty list. |
| Invariants | — |
| Algorithm | 1. `cur = start`. 2. While `cur < end`: append `(cur, min(cur + step, end))`; `cur = cur + step`. |
| Side effects | None. |
| Errors | Naive input, `step ≤ 0`, or more than 100,000 windows → `ConfigError`. |
| Concurrency | Pure. |
| Complexity and limits | O(windows) ≤ 100,000. |
| Security notes | — |
| Tests | UT01-28, PT01-03 |

### 3.4 Row helpers (`herness/connectors/rows.py`)

#### U01-22 herness.connectors.rows.to_snake

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `name` | `str` | — | positional | 1–256 chars |

Returns `str` matching `^[a-z][a-z0-9_]{0,127}$`.

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | Lake column name for a source field (spec 02 §3.1). |
| Preconditions | Non-empty string. |
| Postconditions | Output matches the pattern; `to_snake(to_snake(x)) == to_snake(x)`. |
| Invariants | — |
| Algorithm | 1. `lead = name.startswith("_")`. 2. Replace every character that is not ASCII alphanumeric with `_`. 3. Insert `_` between a lowercase letter or digit and a following uppercase letter. 4. Insert `_` between an uppercase letter and a following uppercase letter that is itself followed by a lowercase letter (`HTTPStatus` → `HTTP_Status`). 5. Lower-case. 6. Collapse runs of `_`; strip leading and trailing `_`. 7. Empty result → error. 8. If `lead` or the first character is a digit, prefix `f_` (`_id` → `f_id`, `1x` → `f_1x`). 9. Longer than 128 chars → error. |
| Side effects | None. |
| Errors | Empty or too long → `SchemaViolation("unusable field name")`. |
| Concurrency | Pure. |
| Complexity and limits | O(len). |
| Security notes | No flattened column can collide with a `_`-prefixed metadata column. |
| Tests | UT01-15, PT01-01 |

#### U01-23 herness.connectors.rows.flatten_record

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `record` | `Mapping[str, object]` | — | positional | One JSON-decoded source record |
| `fields` | `Sequence[str] \| None` | `None` | keyword-only | When given: only these keys, in this order; missing keys give `None` |
| `display_pairs` | `bool` | `False` | keyword-only | Split `{value, display_value}` objects |

Returns `dict[str, str | None]`.

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | One-level flattening of REST/JSON records into string columns (design 01 §4.1). |
| Preconditions | Keys are strings. |
| Postconditions | Every value is `str` or `None`; keys are `to_snake` names in input order. |
| Invariants | — |
| Algorithm | 1. Iterate `fields` if given, else `record` keys in insertion order. 2. `col = to_snake(key)`; `v = record.get(key)`. 3. If `display_pairs` and `v` is a `dict` whose key set is exactly `{"value", "display_value"}`: `out[col] = text(v["value"])`, `out[col + "_display"] = text(v["display_value"])`. 4. Else `out[col] = text(v)`. 5. `text(x)`: `None` → `None`; `str` → itself; `bool` → `"true"`/`"false"`; `int` → `str(x)`; `float` → `json.dumps(x)`; `Decimal` → `str(x)`; aware `datetime` → ISO-8601 with `Z`; `dict`/`list` → `json.dumps(x, ensure_ascii=False, separators=(",", ":"), default=str)`; other → `str(x)`. 6. A column name produced twice → error. |
| Side effects | None. |
| Errors | Collision → `SchemaViolation(f"column collision {col}")`. |
| Concurrency | Pure. |
| Complexity and limits | O(size of record). |
| Security notes | TH01-05. |
| Tests | UT01-16, PT01-04 |

#### U01-24 herness.connectors.rows.parse_source_timestamp, parse_arrow_timestamps

`parse_source_timestamp(value, *, field, epoch_unit=None) -> datetime`:

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `value` | `object` | — | positional | Raw watermark field value |
| `field` | `str` | — | keyword-only | Field name for messages |
| `epoch_unit` | `Literal["s", "ms"] \| None` | `None` | keyword-only | Required to accept numbers |

`parse_arrow_timestamps(array: pa.Array, *, field: str) -> pa.TimestampArray` with type `timestamp("us", tz="UTC")`.

| Item | Content |
|------|---------|
| Kind | function (pure) ×2 |
| Purpose | Parse `_source_updated_at`, the only value a connector parses (design 01 §4.1). |
| Preconditions | — |
| Postconditions | Aware UTC, truncated to µs, year 1970–2100. |
| Invariants | — |
| Algorithm | `parse_source_timestamp`: 1. Aware `datetime` → convert to UTC. 2. Naive `datetime` → error. 3. `str` (stripped): matches `^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(\.\d{1,9})?$` → UTC (ServiceNow internal format, CSV text); matches `^\d{4}-\d{2}-\d{2}$` → 00:00 UTC; otherwise normalize a `+HHMM`/`-HHMM` suffix to `+HH:MM`, a trailing `Z` to `+00:00`, and fractional seconds beyond 6 digits to 6, then `datetime.fromisoformat`; a result without offset → error. 4. `int`/`float` (not `bool`) with `epoch_unit` → `datetime.fromtimestamp(v / (1000 if epoch_unit == "ms" else 1), UTC)`; without `epoch_unit` → error. 5. Other types → error. 6. Range check. `parse_arrow_timestamps`: 1. tz-aware timestamp → cast to `timestamp("us", "UTC")`. 2. naive timestamp → interpreted as UTC (Snowflake `TIMESTAMP_NTZ`, Parquet without zone) and cast. 3. `date32`/`date64` → midnight UTC. 4. `string` → element-wise `parse_source_timestamp`. 5. Any null → error. 6. Other types → error. |
| Side effects | None. |
| Errors | Missing, null, naive, out of range or unparseable → `SchemaViolation(f"unparseable timestamp in {field}")` (arrays add the count of bad rows; values never appear). |
| Concurrency | Pure. |
| Complexity and limits | O(n). |
| Security notes | TH01-05. |
| Tests | UT01-17 |

#### U01-25 herness.connectors.rows.RowBatcher

Constructor `RowBatcher(source, entity, *, batch_rows, columns=(), clock=now_utc)`:

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `source` | `str` | — | positional | connector name |
| `entity` | `str` | — | positional | — |
| `batch_rows` | `int` | — | keyword-only | ≥ 1 |
| `columns` | `Sequence[str]` | `()` | keyword-only | Expected flattened column names, emitted in every batch |
| `clock` | `Callable[[], datetime]` | `now_utc` | keyword-only | — |

| Method | Returns | Behavior |
|--------|---------|----------|
| `add(source_key: str, updated_at: datetime, payload: str \| None, fields: Mapping[str, str \| None])` | `pa.RecordBatch \| None` | Buffers one live row; returns a batch when the buffer reaches `batch_rows` |
| `add_tombstone(source_key: str, deleted_at: datetime)` | `pa.RecordBatch \| None` | Buffers one tombstone row (`_deleted` true; `_payload` and fields null) |
| `flush()` | `pa.RecordBatch \| None` | Emits the buffer, `None` when empty |
| `rows_emitted` | `int` (property) | Rows in emitted batches |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | Turns flattened rows into `pa.RecordBatch` objects with the metadata columns and a stable column set. |
| Preconditions | `updated_at`/`deleted_at` aware. |
| Postconditions | Each batch: `METADATA_SCHEMA` columns first, then `columns` in order, then other columns in first-seen order; a column seen once stays in every later batch (null-filled); field columns are `pa.string()`. |
| Invariants | Buffer length < `batch_rows` between calls. |
| Algorithm | 1. `add`: compute `record_id(source, entity, source_key)`; buffer metadata values with `_deleted=False` and the field mapping; append unseen field names to the column list. 2. When the buffer reaches `batch_rows`, build the batch: `_fetched_at = clock()` truncated to µs for all rows; each field column is built from the buffered mappings with `None` for absent keys; clear the buffer. 3. `add_tombstone`: as `add` with `_deleted=True`, `_payload=None`, empty fields. |
| Side effects | Calls `clock`. |
| Errors | Naive datetime → `SchemaViolation(source, entity)`; `record_id` errors propagate. |
| Concurrency | Not thread-safe; one per stream. |
| Complexity and limits | Memory ≤ `batch_rows` rows. |
| Security notes | TH01-05. |
| Tests | UT01-18 |

#### U01-26 herness.connectors.rows.tombstone_batch

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `source` | `str` | — | positional | — |
| `entity` | `str` | — | positional | — |
| `keys` | `pa.StringArray` | — | positional | 1–100,000 non-null keys |
| `deleted_at` | `datetime` | — | keyword-only | aware; deletion or detection time |
| `fetched_at` | `datetime` | — | keyword-only | aware |

Returns `pa.RecordBatch` with exactly `METADATA_SCHEMA`.

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | Tombstone rows for reconciliation (design 01 §4.1, §5.4). |
| Preconditions | As in the table. |
| Postconditions | `_deleted` all true; `_payload` all null; `_record_id` equals `record_id(source, entity, key)` per row. |
| Invariants | — |
| Algorithm | Vectorised: `_record_id` = `pc.binary_join_element_wise(pa.scalar(f"{source}:{entity}:"), keys, "")`; constant columns built with `pa.array` of the right length. |
| Side effects | None. |
| Errors | Violated precondition → `SchemaViolation(source, entity)`. |
| Concurrency | Pure. |
| Complexity and limits | O(n). |
| Security notes | — |
| Tests | UT01-19 |

### 3.5 Ops-store functions (`herness/store/ops/ingest.py`, re-exported by `herness.store.ops`)

This module is the ingest area of the ops store (R-08); this spec owns every function in it (R-09). Rules of impl 02 §2.3 apply: reads go through X:02/herness.store.ops.core.read_one and read_all on the calling thread's `connection()`; every write is one callback passed to X:02/herness.store.ops.core.run_write with an `op` name (one `BEGIN IMMEDIATE` transaction; `StoreBusy` retried by the X:08 `sqlite_write` policy inside `run_write`); JSON columns are written with X:02 `dump_json` and read with `load_json`. No function takes a store object (R-10). SQL texts are module constants. Datetimes are stored with `format_fixed` and read with `parse_fixed`. JSON columns hold compact JSON arrays of POSIX path strings relative to `paths.data`. The tables exist from impl 02 migration 001 (§4.1); this spec adds no migration (R-11 range 010–019 unused).

#### U01-27 herness.store.ops.Watermark, get_watermark

`Watermark` (frozen dataclass): `source: str`, `entity: str`, `field: str`, `value: datetime`, `updated_at: datetime`.

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `source` | `str` | — | positional | connector name or `monitoring:<tool>` |
| `entity` | `str` | — | positional | — |

Returns `Watermark | None`.

| Item | Content |
|------|---------|
| Kind | dataclass, function |
| Purpose | Read the watermark of one stream (spec 02 §5.1). |
| Preconditions | Ops store migrated. |
| Postconditions | `value` is aware UTC. |
| Invariants | — |
| Algorithm | `read_one("SELECT source, entity, field, value, updated_at FROM watermark WHERE source = ? AND entity = ?", (source, entity))`; no row → `None`; else parse both timestamps with `parse_fixed`. |
| Side effects | One read. |
| Errors | Stored value not in fixed-width format → `SchemaViolation("corrupt watermark", source, entity)`; `StoreBusy` from `read_one` (no retry here; the job layer decides). |
| Concurrency | Safe from any thread: X:02 `connection()` is per thread (R-10). |
| Complexity and limits | O(1) (primary key). |
| Security notes | — |
| Tests | UT01-20 |

#### U01-28 herness.store.ops.set_watermark

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `source` | `str` | — | positional | — |
| `entity` | `str` | — | positional | — |
| `field` | `str` | — | positional | watermark field name |
| `value` | `datetime` | — | positional | aware |
| `now` | `datetime` | — | keyword-only | aware |

Returns `bool` (true when the stored value changed).

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | The only writer of `watermark` (design 01 §5.3). Monotonic: a value never moves backwards. |
| Preconditions | Aware datetimes; naive → `SchemaViolation`. |
| Postconditions | Stored `value` = max(previous, `value`). |
| Invariants | `watermark.value` never decreases for a `(source, entity)`. |
| Algorithm | `run_write(fn, op="set_watermark")` where `fn(conn)` executes `INSERT INTO watermark(source, entity, field, value, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, entity) DO UPDATE SET field = excluded.field, value = excluded.value, updated_at = excluded.updated_at WHERE excluded.value > watermark.value` and returns `cursor.rowcount == 1`. Fixed-width text makes the text comparison a time comparison (spec 00 §8). |
| Side effects | One write. Idempotency key: `(source, entity)`; re-applying the same value is a no-op. |
| Errors | `StoreBusy` after the X:08 `sqlite_write` policy is exhausted (from `run_write`). |
| Concurrency | Safe from any thread; SQLite serialises writers. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT01-20 |

#### U01-29 herness.store.ops.deleted_record_ids

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `source` | `str` | — | positional | connector name (never `monitoring:<tool>`) |
| `entity` | `str` | — | positional | — |

Returns `list[str]`, sorted, unique.

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | The deletion set for one entity: `record_id`s with a `deletion_request` in `running` or `done` (design 01 §5.6). Read-only over the table impl 10 writes; it lives in the ingest area because the runner (U01-33) and the X:02 build reference it as an ingest function (§13 O-10). |
| Preconditions | — |
| Postconditions | Every returned id starts with `f"{source}:{entity}:"`. `pending` and `failed` requests are not returned. |
| Invariants | — |
| Algorithm | `read_all("SELECT DISTINCT record_id FROM deletion_request WHERE status IN ('running', 'done') AND substr(record_id, 1, ?) = ? ORDER BY record_id", (len(prefix), prefix), max_rows=1_000_000)` with `prefix = f"{source}:{entity}:"`. `substr` avoids `LIKE` wildcard escaping. |
| Side effects | One read. |
| Errors | `StoreBusy` from `read_all`; more than 1,000,000 ids → `SchemaViolation` from `read_all`. |
| Concurrency | Safe from any thread. |
| Complexity and limits | O(requests); 100k ids ≈ 4 MB. |
| Security notes | TH01-11. The ids are never logged. |
| Tests | UT01-21 |

#### U01-30 herness.store.ops.SliceRow, ensure_slices

`SliceRow` (frozen dataclass): `source`, `entity`, `slice_start: datetime`, `slice_end: datetime`, `status: Literal["pending", "running", "done", "failed"]`, `rows: int`, `files: tuple[str, ...]`, `attempts: int`, `last_error: str | None`, `updated_at: datetime`.

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `source` | `str` | — | positional | stream key |
| `entity` | `str` | — | positional | — |
| `slices` | `Sequence[tuple[datetime, datetime]]` | — | positional | output of `split_range` |
| `now` | `datetime` | — | keyword-only | aware |

Returns `list[SliceRow]` for exactly the given slice starts, ordered by `slice_start`.

| Item | Content |
|------|---------|
| Kind | dataclass, function |
| Purpose | Create or refresh the backfill plan rows in `sync_slice` (design 01 §5.5). |
| Preconditions | `slices` non-empty and contiguous. |
| Postconditions | One row per slice start. A row whose stored `slice_end` differs from the plan is reset to `pending` (`rows = 0`, `files = '[]'`), except a `done` row whose stored `slice_end` ≥ the planned end, which stays `done`. |
| Invariants | PK `(source, entity, slice_start)`. |
| Algorithm | 1. `run_write(fn, op="ensure_slices")`: `fn(conn)` executes, for each slice, `INSERT INTO sync_slice(source, entity, slice_start, slice_end, status, rows, files, attempts, last_error, updated_at) VALUES (?, ?, ?, ?, 'pending', 0, '[]', 0, NULL, ?) ON CONFLICT(source, entity, slice_start) DO UPDATE SET slice_end = excluded.slice_end, status = 'pending', rows = 0, files = '[]', updated_at = excluded.updated_at WHERE sync_slice.slice_end <> excluded.slice_end AND NOT (sync_slice.status = 'done' AND sync_slice.slice_end > excluded.slice_end)`. 2. After the commit, select the rows with `read_all` on `slice_start IN (...)` (chunks of 500 parameters); `files` decoded with `load_json`. |
| Side effects | Writes. Idempotency key `(source, entity, slice_start)`. |
| Errors | `StoreBusy` (from `run_write` or `read_all`). |
| Concurrency | Safe from any thread. |
| Complexity and limits | O(slices). |
| Security notes | — |
| Tests | UT01-22 |

#### U01-31 herness.store.ops.mark_slice_running, mark_slice_done, mark_slice_failed

| Function | Extra parameters | Statement |
|----------|------------------|-----------|
| `mark_slice_running(source, entity, slice_start, *, now)` | — | `UPDATE sync_slice SET status = 'running', attempts = attempts + 1, updated_at = ? WHERE source = ? AND entity = ? AND slice_start = ?` |
| `mark_slice_done(source, entity, slice_start, *, rows: int, files: Sequence[str], now)` | `rows ≥ 0`; POSIX relative paths, encoded with `dump_json` | `... SET status = 'done', rows = ?, files = ?, last_error = NULL, updated_at = ? WHERE ...` |
| `mark_slice_failed(source, entity, slice_start, *, error: str, now)` | error text | `... SET status = 'failed', last_error = ?, updated_at = ? WHERE ...`; `error` truncated to 500 chars |

All return `None`.

| Item | Content |
|------|---------|
| Kind | function ×3 |
| Purpose | Slice state transitions `pending → running → done \| failed` (design 01 §5.5). |
| Preconditions | The row exists (created by `ensure_slices`). |
| Postconditions | As in the statements. |
| Invariants | `attempts` increases only in `mark_slice_running`. |
| Algorithm | One `run_write` call each (`op` = the function name) executing one statement; `cursor.rowcount == 0` → error raised inside the callback, so the transaction rolls back. |
| Side effects | One write each. |
| Errors | Row missing → `SchemaViolation("slice not found", source, entity)`; `StoreBusy` (from `run_write`). |
| Concurrency | Safe from any thread. |
| Complexity and limits | O(1). |
| Security notes | `error` holds the error class name and its message only; taxonomy messages never contain record text (ENG §3.4). |
| Tests | UT01-23 |

#### U01-32 herness.store.ops.FileIngestRow, get_file_ingest, record_file_ingest

`FileIngestRow` (frozen dataclass): `fingerprint: str` (64 lower-case hex), `source: str` (`"files"`), `entity: str`, `path: str` (POSIX path relative to the inbox root), `size_bytes: int`, `mtime: datetime`, `rows: int`, `files: tuple[str, ...]`, `ingested_at: datetime`.

| Function | Returns | Statement |
|----------|---------|-----------|
| `get_file_ingest(fingerprint: str)` | `FileIngestRow \| None` | `read_one` of `SELECT fingerprint, source, entity, path, size_bytes, mtime, rows, files, ingested_at FROM file_ingest WHERE fingerprint = ?` |
| `record_file_ingest(row: FileIngestRow)` | `bool` (inserted) | `run_write(fn, op="record_file_ingest")`; `fn` executes `INSERT OR IGNORE INTO file_ingest(fingerprint, source, entity, path, size_bytes, mtime, rows, files, ingested_at) VALUES (...)` |

| Item | Content |
|------|---------|
| Kind | dataclass, function ×2 |
| Purpose | Fingerprint registry of ingested inbox files (design 01 §5.10). |
| Preconditions | `fingerprint` matches `^[0-9a-f]{64}$`, else `SchemaViolation`. |
| Postconditions | At most one row per fingerprint. |
| Invariants | PK `fingerprint`. |
| Algorithm | As in the table; `files` encoded with `dump_json` and decoded with `load_json`; `record_file_ingest` returns `cursor.rowcount == 1`. |
| Side effects | Read; write. Idempotency key `fingerprint`. |
| Errors | `StoreBusy` (from `read_one` or `run_write`). |
| Concurrency | Safe from any thread. |
| Complexity and limits | O(1). |
| Security notes | TH01-12 (ingest record). |
| Tests | UT01-24 |

#### U01-92 herness.store.ops.list_watermarks

No parameters. Returns `list[Watermark]` (U01-27) ordered by `(source, entity)`.

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | All stored watermarks, for the build's `meta.build.source_watermarks` (X:02 build job) and for health (U01-57). Added because impl 02 references it as an ingest-area function (R-09). |
| Preconditions | Ops store migrated. |
| Postconditions | One entry per `watermark` row; `value` and `updated_at` aware UTC. Monitoring rows appear per tool as `monitoring:<tool>` (R-62). |
| Invariants | — |
| Algorithm | `read_all("SELECT source, entity, field, value, updated_at FROM watermark ORDER BY source, entity", (), max_rows=10_000)`; each row parsed as in U01-27. |
| Side effects | One read. |
| Errors | A stored value not in fixed-width format → `SchemaViolation("corrupt watermark", source, entity)`; more than 10,000 rows → `SchemaViolation` from `read_all`; `StoreBusy` from `read_all`. |
| Concurrency | Safe from any thread (per-thread `connection()`, R-10). |
| Complexity and limits | O(streams × entities); at most 10,000 rows. |
| Security notes | — |
| Tests | UT01-95 |

### 3.6 Deletion filter and lake-file helpers

#### U01-33 herness.connectors.deletion.DeletionFilter

Constructor `DeletionFilter(source: str, entity: str)` (no store parameter, R-10).

| Method | Returns | Behavior |
|--------|---------|----------|
| `reload() -> int` | set size | Loads `deleted_record_ids(source, entity)` into a `pa.StringArray` and swaps it in |
| `apply(batch: pa.RecordBatch) -> tuple[pa.RecordBatch, int]` | filtered batch, rows dropped | Empty set → `(batch, 0)`; else `batch.filter(pc.invert(pc.is_in(batch["_record_id"], value_set=ids)))` |
| `size` | `int` property | Current set size |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | Drops records under a `running`/`done` deletion request before `LakeWriter.write` (design 01 §5.6). |
| Preconditions | `apply` before any `reload` raises `SchemaViolation("deletion set not loaded")`, so a missed reload can never let rows through. |
| Postconditions | No output row's `_record_id` is in the set; `rows_in = rows_out + dropped`. |
| Invariants | The array reference is replaced atomically. |
| Algorithm | As in the method table. |
| Side effects | `reload` reads the ops store. |
| Errors | Not loaded → `SchemaViolation(source, entity)`; `StoreBusy` from `reload`. |
| Concurrency | `reload` swaps the reference under a `threading.Lock`; `apply` reads the reference once per call, so concurrent `apply` calls are safe. |
| Complexity and limits | `apply` O(rows) with a hash set; < 5 % overhead at 100k ids (BT01-02). |
| Security notes | TH01-11. Dropped ids are never logged; only counts. |
| Tests | UT01-25, PT01-06, BT01-02 |

#### U01-34 herness.connectors.lakefiles.cleanup_orphan_temp_files

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `raw_root` | `Path` | — | positional | `<paths.data>/raw` |
| `source` | `str` | — | positional | connector name |
| `now` | `datetime` | — | keyword-only | aware |
| `max_age` | `timedelta` | `timedelta(hours=1)` | keyword-only | > 0 |

Returns `int` (files deleted).

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | Delete crash leftovers `.<name>.parquet.tmp-<ulid>` older than 1 h under `data/raw/<source>/` (design 01 §5.2). |
| Preconditions | — |
| Postconditions | No matching file older than `max_age` remains, except files that could not be deleted. |
| Invariants | Never deletes a file not matching `^\.[^\\/]+\.parquet\.tmp-[0-9A-HJKMNP-TV-Z]{26}$`, never follows symlinks, never deletes outside `raw_root / source`. |
| Algorithm | 1. `root = raw_root / source`; missing → return 0. 2. `os.walk(root, followlinks=False)`. 3. For each file name matching the pattern: skip symlinks and junctions; skip if the resolved path is not under `root.resolve()`; `lstat`; if `now − mtime > max_age`, `unlink`. 4. `PermissionError` or `FileNotFoundError` on unlink → log `connectors.lake.orphan_remove_failed` (WARNING) and continue. 5. Log `connectors.lake.orphans_removed` (INFO) with the count when > 0. |
| Side effects | Deletes files. |
| Errors | None raised; OS errors per file are logged. |
| Concurrency | Safe to run while another process writes new temp files: only files older than 1 h are touched, and a live writer rotates within `max_open_s` (600 s, spec 02). |
| Complexity and limits | O(files under the source). |
| Security notes | TH01-08 (no symlink traversal). |
| Tests | UT01-26, FT01-07 |

#### U01-35 herness.connectors.lakefiles.SchemaTracker, SchemaDrift

`SchemaDrift` (frozen dataclass): `added: tuple[str, ...]`, `removed: tuple[str, ...]`, `changed: tuple[str, ...]` (names whose Arrow type changed), each sorted.

`SchemaTracker.observe(schema: pa.Schema) -> SchemaDrift | None`.

| Item | Content |
|------|---------|
| Kind | class, dataclass |
| Purpose | Detects when a batch's columns differ from the current writer's columns, so each lake file has one schema (design 01 §5.2). |
| Preconditions | — |
| Postconditions | First call → `None` and the schema becomes the baseline. Later calls → `None` when the name→type mapping equals the baseline, else a `SchemaDrift` and the new schema becomes the baseline. |
| Invariants | Baseline is the schema of the last observed batch. |
| Algorithm | Compare `{f.name: f.type}` dictionaries (column order ignored). |
| Side effects | None. |
| Errors | None. |
| Concurrency | One per stream. |
| Complexity and limits | O(columns). |
| Security notes | — |
| Tests | UT01-27 |

### 3.7 Sync runner (`herness/connectors/runner.py`)

#### U01-36 herness.connectors.runner.SyncResult

| Field | Type | Meaning |
|-------|------|---------|
| `source` | `str` | Connector name (for monitoring: `monitoring`, aggregated over tools) |
| `entity` | `str` | Entity |
| `mode` | `Literal["incremental", "backfill", "reconcile"]` | Run mode |
| `rows` | `int` | Rows written (live and tombstone), after deletion filtering |
| `tombstones` | `int` | Rows written with `_deleted = true` |
| `skipped_deleted` | `int` | Rows dropped by the deletion filter |
| `files` | `tuple[Path, ...]` | Committed lake files |
| `watermark_before` | `str \| None` | Fixed-width text; for monitoring the minimum over tools (`None` if any tool had none) |
| `watermark_after` | `str \| None` | Same rule after the run |

Method `to_dict() -> dict[str, object]`: JSON-safe form for `JobOutcome.result` (paths as POSIX strings relative to `paths.data`).

| Item | Content |
|------|---------|
| Kind | class (frozen dataclass; design contract 01 §3.2) |
| Purpose | Outcome of one runner call. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `rows ≥ tombstones ≥ 0`; `skipped_deleted ≥ 0`; `watermark_after ≥ watermark_before` when both are set. |
| Algorithm | — |
| Side effects | None. |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | Contains counts and paths only. |
| Tests | UT01-29 |

#### U01-37 herness.connectors.runner.SyncRunner

Constructor:

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `connector` | `Connector` | — | positional | `connector.name == cfg.SOURCE` |
| `cfg` | `SourceSettings` | — | positional | validated |
| `clock` | `Callable[[], datetime]` | `now_utc` | keyword-only | — |
| `writer_factory` | `Callable[[str, str], LakeWriter]` | `lambda s, e: LakeWriter(s, e)` | keyword-only | X:02/herness.store.lake.LakeWriter defaults |
| `connector_factory` | `Callable[[], Connector] \| None` | `None` | keyword-only | Builds a fresh instance of the same connector for backfill slice threads; `None` → slices run sequentially on `connector` |
| `data_root` | `Path \| None` | `None` | keyword-only | `None` → X:10 `get_config().paths.data` |
| `progress` | `Callable[[str], None] \| None` | `None` | keyword-only | Called at each checkpoint with a short note (job heartbeat) |
| `should_stop` | `Callable[[], bool] \| None` | `None` | keyword-only | Polled before each backfill slice (job cancel/preempt) |

Public attributes after a run: `skipped_open: tuple[str, ...]` (stream keys skipped because their breaker was open), `stopped: bool` (a backfill left slices pending because `should_stop()` was true).

| Item | Content |
|------|---------|
| Kind | class (design contract 01 §3.2; the keyword-only parameters are additive, §13 D-5; the design's `ops` parameter is removed because ops access goes through the X:02 core API, R-10, §13 D-15) |
| Purpose | Runs incremental, backfill and reconcile flows for one source. |
| Preconditions | `connector.name == cfg.SOURCE`, else `ConfigError`. |
| Postconditions | — |
| Invariants | At most one open `LakeWriter` per stream; a writer is always committed or aborted before the runner method returns or raises; watermarks move only after `commit()` returned. |
| Algorithm | The constructor stores its parameters and sets `_cleaned = False`. Every public `run_*` first calls `cleanup_orphan_temp_files(data_root / "raw", connector.name, now=clock())` once per instance. |
| Side effects | See the methods. |
| Errors | See the methods. |
| Concurrency | One runner per job thread; backfill creates its own worker threads (U01-43). |
| Complexity and limits | — |
| Security notes | TH01-11, TH01-13. |
| Tests | UT01-35 |

#### U01-38 herness.connectors.runner.SyncRunner.run_incremental

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `entity` | `str` | — | positional | configured entity |

Returns `SyncResult` (mode `incremental`, or `backfill` when no watermark existed).

| Item | Content |
|------|---------|
| Kind | method |
| Purpose | Job kind `sync` for one entity (design 01 §5.1). |
| Preconditions | `cfg.entity(entity)` succeeds (`ConfigError` otherwise). |
| Postconditions | New records up to `until` are committed and the watermark reflects them. |
| Invariants | U01-37. |
| Algorithm | 1. Validate `entity`; run the one-time orphan cleanup. 2. `connector.name == "files"` → return `ingest_files(self, entity)` (U01-50). 3. `isinstance(connector, SupportsToolStreams)` → for each `tool` in `connector.tools()`: call `_incremental_stream(entity, key=connector.stream_key(tool), fetch=partial(connector.sync_tool, tool))`. `CircuitOpen` → append the key to `skipped_open`, log `connectors.sync.skipped_open_circuit`, continue. Any other `HernessError` → keep it, log `connectors.sync.failed`, continue with the next tool. After all tools: if a `FatalError` was kept, raise the first one; else if a `RetryableError` other than `CircuitOpen` was kept, raise the first one; else if every tool was skipped, raise the first `CircuitOpen`; else return the aggregate `SyncResult` (sums; files concatenated; watermark rule of U01-36). 4. Otherwise return `_incremental_stream(entity, key=connector.name, fetch=connector.sync)`. |
| Side effects | Lake files, `watermark`, logs, metrics. |
| Errors | `CircuitOpen`, and every §6 error, propagated after the writer is aborted. |
| Concurrency | Single thread. |
| Complexity and limits | See U01-40. |
| Security notes | TH01-11. |
| Tests | UT01-29, UT01-30, UT01-32, UT01-35, UT01-92 |

#### U01-39 herness.connectors.runner.SyncRunner._incremental_stream

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `entity` | `str` | — | positional | — |
| `key` | `str` | — | keyword-only | stream key: connector name or `monitoring:<tool>` |
| `fetch` | `Callable[[str, datetime \| None, datetime], Iterator[pa.RecordBatch]]` | — | keyword-only | `connector.sync` or a bound `sync_tool` |

Returns `SyncResult`.

| Item | Content |
|------|---------|
| Kind | method (private, carries the §5.1 logic) |
| Purpose | Incremental flow for one stream. |
| Preconditions | — |
| Postconditions | Watermark ≥ previous value. |
| Invariants | U01-37. |
| Algorithm | 1. X:08/herness.core.resilience.guard(key) (raises `CircuitOpen`; no source call happens). 2. `wm = get_watermark(key, entity)`. 3. `wm is None` → return `run_backfill` logic for this stream (U01-43) with `start = cfg.backfill_for(entity).resolve_start(now)` and `end = now`, where `now = clock()`. 4. `now = clock()`; `since = wm.value − cfg.overlap_for(entity)`; `until = now − timedelta(seconds=cfg.settle_seconds)`. 5. `since ≥ until` → return a zero `SyncResult` with the watermark unchanged. 6. Log `connectors.sync.started`. 7. `deletion = DeletionFilter(connector.name, entity)`; `deletion.reload()`. 8. `out = _write_stream(entity, fetch(entity, since, until), key=key, deletion=deletion, ordered=connector.name not in UNORDERED_SOURCES, field=connector.watermark_field(entity), cap=until, advance_watermark=True)`. 9. Read the watermark again for `watermark_after`; log `connectors.sync.completed`; emit metrics (§8); return the result. |
| Side effects | As U01-38. |
| Errors | As U01-38. |
| Concurrency | Single thread. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT01-29, UT01-30, UT01-32, UT01-35, IT01-02 |

#### U01-40 herness.connectors.runner.SyncRunner._write_stream

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `entity` | `str` | — | positional | — |
| `batches` | `Iterator[pa.RecordBatch]` | — | positional | connector output |
| `key` | `str` | — | keyword-only | stream key |
| `deletion` | `DeletionFilter` | — | keyword-only | loaded |
| `ordered` | `bool` | — | keyword-only | stream is ascending |
| `field` | `str` | — | keyword-only | watermark field |
| `cap` | `datetime` | — | keyword-only | upper bound for watermark values |
| `advance_watermark` | `bool` | — | keyword-only | false for backfill slices, reconcile and files |

Returns private frozen dataclass `StreamOutcome(rows: int, tombstones: int, skipped_deleted: int, files: tuple[Path, ...], max_committed: datetime | None)`.

| Item | Content |
|------|---------|
| Kind | method (private, shared write loop) |
| Purpose | Filter, drift-split, write, checkpoint and advance the watermark (design 01 §3.2, §5.1, §5.2, §5.6). |
| Preconditions | `deletion` loaded. |
| Postconditions | All written rows are committed; for ordered streams the watermark equals `min(max committed _source_updated_at, cap)` of the last checkpoint that committed rows; for unordered streams it equals `min(max over all commits, cap)` and is set once at the end. |
| Invariants | Watermark written only after `commit()` returned (design 01 §2). |
| Algorithm | 1. `writer = writer_factory(connector.name, entity)`; `tracker = SchemaTracker()`; `cp_rows = 0`; `cp_start = clock()`. 2. For each `batch`: a. skip empty batches; b. `batch, dropped = deletion.apply(batch)`; `skipped_deleted += dropped`; skip if now empty; c. `drift = tracker.observe(batch.schema)`; if `drift` is not `None`: log `connectors.schema_drift.detected` with `added`, `removed`, `changed`, emit the drift metric, and if `cp_rows > 0` run `checkpoint()`; d. `writer.write(batch)`; `cp_rows += n`; `rows += n`; `tombstones += pc.sum(batch["_deleted"])`; e. if `cp_rows ≥ cfg.checkpoint_rows` or `clock() − cp_start ≥ LAKE_MAX_OPEN_S` (600 s, the X:02 `LakeWriter` default `max_open_s`) run `checkpoint()`. 3. `checkpoint()`: `fs = writer.commit()`; add `fs.files`; update `max_committed`; call X:08/herness.core.resilience.fault_point("connector.before_watermark", source=key); if `advance_watermark and ordered and fs.max_source_updated_at is not None`: `set_watermark(key, entity, field, min(fs.max_source_updated_at, cap), now=clock())`; `deletion.reload()`; call `progress(f"{key}/{entity} rows={rows}")` when set; log `connectors.sync.checkpoint_committed`; open a new writer; reset `cp_rows`, `cp_start`. 4. End of stream: `fs = writer.commit()` (also when empty) and apply the same watermark rule as step 3 without opening a new writer. Then, if `advance_watermark and not ordered and max_committed is not None`: `set_watermark(..., min(max_committed, cap), ...)`. 5. Any exception (including `KeyboardInterrupt`) in steps 2–4: call `writer.abort()`; if `abort()` itself raises, log `connectors.lake.abort_failed` (ERROR) and re-raise the original exception; else re-raise. |
| Side effects | Lake files through `LakeWriter`; `watermark`; logs; metrics; `fault_point`. |
| Errors | Propagates connector errors, `LakeWriter` errors (X:02), `StoreBusy`. |
| Concurrency | One call per thread; backfill slices each call it with their own writer. |
| Complexity and limits | Memory: one batch plus the writer's buffer (X:02 `target_bytes`); rows per checkpoint ≤ `checkpoint_rows` + `batch_rows`. |
| Security notes | TH01-11 (filter before write), TH01-05. |
| Tests | UT01-31, UT01-33, UT01-34, UT01-36, FT01-01, IT01-04, BT01-02 |

#### U01-41 herness.connectors.runner.SyncRunner.run_backfill

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `entity` | `str` | — | positional | configured |
| `start` | `datetime` | — | positional | aware |
| `end` | `datetime` | — | positional | aware; `start < end ≤ now` |

Returns `SyncResult` (mode `backfill`).

| Item | Content |
|------|---------|
| Kind | method |
| Purpose | Job kind `sync` with `--backfill` (design 01 §5.5). |
| Preconditions | As in the table; violations → `ConfigError`. `connector.name == "files"` → `ConfigError("files does not backfill")`. |
| Postconditions | See U01-43. |
| Invariants | U01-37. |
| Algorithm | 1. Validate; one-time cleanup. 2. Tool streams: call `run_backfill` (U01-43) for each tool with `key = stream_key(tool)`, using the same per-tool error handling and aggregation as U01-38 step 3. 3. Otherwise call U01-43 with `key = connector.name`. |
| Side effects | As U01-43. |
| Errors | As U01-43. |
| Concurrency | See U01-43. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT01-37, UT01-93 |

#### U01-42 herness.connectors.runner.SyncRunner.run_reconcile

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `entity` | `str` | — | positional | configured |

Returns `SyncResult` (mode `reconcile`).

| Item | Content |
|------|---------|
| Kind | method |
| Purpose | Job kind `reconcile` (design 01 §5.4). |
| Preconditions | Connector implements `SupportsKeyListing` and is not `monitoring`; else `ConfigError(f"{name} is not reconciled")`. |
| Postconditions | See U01-44. |
| Invariants | Watermark never changes. |
| Algorithm | 1. Validate; one-time cleanup. 2. Return `reconcile_entity(self, entity)` (U01-44). |
| Side effects | As U01-44. |
| Errors | As U01-44. |
| Concurrency | Single thread. |
| Complexity and limits | — |
| Security notes | TH01-13. |
| Tests | UT01-43 |

### 3.8 Backfill (`herness/connectors/backfill.py`)

#### U01-43 herness.connectors.backfill.run_backfill

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `runner` | `SyncRunner` | — | positional | — |
| `entity` | `str` | — | positional | — |
| `start` | `datetime` | — | positional | aware |
| `end` | `datetime` | — | positional | aware, > `start` |
| `key` | `str` | — | keyword-only | stream key |
| `fetch_of` | `Callable[[Connector], Callable[[str, datetime \| None, datetime], Iterator[pa.RecordBatch]]]` | — | keyword-only | Maps a connector instance to its fetch function (`c.sync`, or `partial(c.sync_tool, tool)`) |
| `max_workers` | `int` | — | keyword-only | `cfg.max_concurrency`, or the adapter's `max_concurrency` for a tool |

Returns `SyncResult`.

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | Resumable parallel date-slice backfill (design 01 §5.5). |
| Preconditions | — |
| Postconditions | When every planned slice is `done`, the watermark is set to `min(end, max committed _source_updated_at of this call)`, or to `end` when this call committed no rows. Otherwise the watermark is unchanged. |
| Invariants | A `done` slice is never rerun; `running` and `failed` slices rerun from their start. |
| Algorithm | 1. `bf = cfg.backfill_for(entity)`; `slices = split_range(start, end, timedelta(days=bf.slice_days))`. 2. `plan = ensure_slices(key, entity, slices, now=clock())`; `todo = [r for r in plan if r.status != "done"]`. 3. `workers = min(max_workers, len(todo))` when `runner.connector_factory` is set, else `1`. 4. Run `todo` in a `ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"backfill-{key}")`. Each slice task: a. if `runner.should_stop` returns true → return "skipped" (slice stays as it is); b. `mark_slice_running`; c. `guard(key)`; d. `conn = runner.connector_factory()` or the shared connector; e. `deletion = DeletionFilter(connector.name, entity)`; `deletion.reload()`; f. `out = runner._write_stream(entity, fetch_of(conn)(entity, s0, s1), key=key, deletion=deletion, ordered=connector.name not in UNORDERED_SOURCES, field=connector.watermark_field(entity), cap=s1, advance_watermark=False)`; g. `mark_slice_done(rows=out.rows, files=<out.files as POSIX paths relative to data_root>)`; log `connectors.backfill.slice_completed`. On any exception: `mark_slice_failed(error=f"{type(exc).__name__}: {exc}")`; log `connectors.backfill.slice_failed`; re-raise. 5. Wait for all tasks. 6. If any task raised: raise the first `FatalError` if any, else the first other exception. 7. If any task was skipped: set `runner.stopped = True`; return the result with the watermark unchanged. 8. Set the watermark per the postcondition with `set_watermark`; log `connectors.sync.completed` (mode `backfill`); return the sums. |
| Side effects | `sync_slice`, lake files, `watermark`, logs, metrics. |
| Errors | Any slice error, re-raised after all slices finish. |
| Concurrency | Up to `workers` threads; each has its own connector (when a factory is given), `LakeWriter` and `DeletionFilter`; ops functions are thread-safe through the per-thread X:02 `connection()` (R-10). |
| Complexity and limits | Memory ≤ `workers` × (one page + one batch + writer buffer). |
| Security notes | TH01-11. |
| Tests | UT01-37, UT01-38, UT01-39, UT01-93, FT01-03 |

### 3.9 Reconciliation (`herness/connectors/reconcile.py`)

#### U01-44 herness.connectors.reconcile.reconcile_entity

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `runner` | `SyncRunner` | — | positional | — |
| `entity` | `str` | — | positional | — |
| `keys` | `Iterator[pa.RecordBatch] \| None` | `None` | keyword-only | `None` → `connector.list_keys(entity)`; files snapshot passes the file's keys |
| `deleted_at` | `datetime \| None` | `None` | keyword-only | Tombstone `_source_updated_at`; `None` → detection time `clock()` |

Returns `SyncResult` (mode `reconcile`).

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | Key reconciliation with safety valve (design 01 §5.4 steps 1–4). |
| Preconditions | — |
| Postconditions | Every live lake key missing from the source (and not under a deletion request) has a tombstone, unless the valve tripped. |
| Invariants | Watermark unchanged; never writes a tombstone for a `record_id` in the deletion set. |
| Algorithm | 1. `guard(connector.name)` (files: skipped). 2. `deletion = DeletionFilter(connector.name, entity)`; `reload()`. 3. `scratch = data_root / "tmp" / "reconcile"` (created); `key_path = scratch / f"{connector.name}-{entity}-{new_ulid()}.parquet"` (X:00/herness.core.ids.new_ulid). 4. Write the key stream to `key_path` with `pyarrow.parquet.ParquetWriter(KEY_SCHEMA, compression="zstd")`; each batch is cast to `KEY_SCHEMA` (a null key or other type → `SchemaViolation`); count `source_keys`. 5. `missing, live = find_missing_keys(data_root / "raw" / connector.name / entity, key_path, deleted=deletion ids, temp_dir=scratch)`. 6. If `live > 0` and `len(missing) × 100 > cfg.reconcile.max_delete_pct × live`: log `connectors.reconcile.aborted` (ERROR: `live_keys`, `missing_keys`, `max_delete_pct`), emit `herness_connectors_reconcile_aborted_total`, raise `SchemaViolation("reconcile safety valve", source, entity)`. 7. Otherwise write `missing` in chunks of `cfg.batch_rows` with `tombstone_batch(..., deleted_at=deleted_at or clock(), fetched_at=clock())`, each passed through `deletion.apply`, into one `LakeWriter`; `commit()`; `abort()` on error. 8. `finally`: delete `key_path` (`missing_ok=True`). 9. Log `connectors.reconcile.completed`; return `SyncResult(rows=n, tombstones=n, ...)` with `watermark_before = watermark_after =` the current watermark text. |
| Side effects | Scratch file, lake files, logs, metrics. |
| Errors | `CircuitOpen`; §6 connector errors; `SchemaViolation` (valve, bad key batch); DuckDB errors → `SchemaViolation("reconcile query failed")` from `find_missing_keys`. |
| Concurrency | Single thread. |
| Complexity and limits | Key file on disk; DuckDB memory capped at 1 GB with spill to `scratch` (RSS target, design 01 §8). |
| Security notes | TH01-13 (valve; an empty key listing trips it), TH01-11. |
| Tests | UT01-40, UT01-41, UT01-42, UT01-44, IT01-05, ST01-13 |

#### U01-45 herness.connectors.reconcile.find_missing_keys

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `lake_dir` | `Path` | — | positional | `data/raw/<source>/<entity>` |
| `keys_file` | `Path` | — | positional | Parquet with `KEY_SCHEMA` |
| `deleted` | `pa.StringArray` | — | keyword-only | deletion set |
| `temp_dir` | `Path` | — | keyword-only | DuckDB spill directory |
| `memory_limit` | `str` | `"1GB"` | keyword-only | DuckDB setting |

Returns `tuple[pa.StringArray, int]` = (missing keys sorted, live key count).

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | DuckDB anti-join of the lake's latest non-deleted keys against the source keys (design 01 §5.4 step 2). |
| Preconditions | — |
| Postconditions | No lake files (`[!.]*.parquet` under `lake_dir`) → `(empty, 0)`. |
| Invariants | Uses the staging dedupe rule of spec 02 §4.2. |
| Algorithm | 1. If no committed file exists → return `(empty, 0)`. 2. `con = duckdb.connect(":memory:", config={"memory_limit": memory_limit, "temp_directory": str(temp_dir), "threads": 4})`; `con.register("deleted_ids", pa.table({"record_id": deleted}))`. 3. Execute with bound parameters `$lake = f"{lake_dir.as_posix()}/**/[!.]*.parquet"` and `$keys = keys_file.as_posix()`: `WITH latest AS (SELECT _record_id, _source_key, _deleted FROM read_parquet($lake, hive_partitioning = true, union_by_name = true) QUALIFY row_number() OVER (PARTITION BY _record_id ORDER BY _source_updated_at DESC, _fetched_at DESC) = 1), live AS (SELECT _source_key FROM latest WHERE NOT _deleted AND _record_id NOT IN (SELECT record_id FROM deleted_ids))` then select `count(*)` from `live` and, separately, `SELECT l._source_key FROM live l ANTI JOIN read_parquet($keys) k ON l._source_key = k._source_key ORDER BY 1` fetched as Arrow. 4. Close the connection. |
| Side effects | Reads lake files; DuckDB may spill to `temp_dir`. |
| Errors | Any `duckdb.Error` → `SchemaViolation("reconcile query failed", lake_dir.name)` from the original. |
| Concurrency | Own connection; single thread. |
| Complexity and limits | O(lake rows) scan of five columns; memory ≤ `memory_limit`. |
| Security notes | Paths are bound parameters, never formatted into SQL. |
| Tests | UT01-40, UT01-44, IT01-05 |

### 3.10 Files connector (`herness/connectors/files.py`)

Module constants: `MAX_INBOX_FILE_BYTES = 1_073_741_824` (1 GiB); `FILE_READ_MEMORY_LIMIT = "1GB"`; `FINGERPRINT_CHUNK_BYTES = 1_048_576`. Error class `InboxFileChanged(SchemaViolation)`: the file changed while it was fingerprinted or read.

#### U01-46 herness.connectors.files.FilesConnector

Constructor `FilesConnector(settings: FilesSettings, *, inbox_root: Path, clock=now_utc)`; decorated `register("connector", "files")`. `name = "files"`; `entities = tuple(settings.entities)`.

| Method | Behavior |
|--------|----------|
| `check() -> None` | `inbox_root` exists, is a directory and is not a symlink or junction, else `ConfigError("inbox missing or not a directory")`; each entity folder missing → log `connectors.files.entity_folder_missing` (WARNING) |
| `watermark_field(entity) -> str` | The entity `updated_field`, else the literal `"mtime"` (files do not use `watermark`, design 01 §5.3) |
| `sync(entity, since, until=None) -> Iterator[pa.RecordBatch]` | For each `f` in `candidates(entity)` with `since ≤ f.mtime < until` (`None` bounds open), in `(mtime, name)` order: `yield from read_file(entity, f)` |
| `list_keys(entity) -> Iterator[pa.RecordBatch]` | Entity mode `snapshot` only (else `ConfigError(f"files entity {entity} is not in snapshot mode")`); newest candidate by `(mtime, name)` (none → `ConfigError("no snapshot file")`); yields its `_source_key` column as `KEY_SCHEMA` batches |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | Reads inbox drops (design 01 §5.10). |
| Preconditions | Validated settings; `inbox_root` resolved by the factory (U01-55). |
| Postconditions | Never moves, renames or deletes inbox files. |
| Invariants | Reads only regular files inside `inbox_root/<entity>/`. |
| Algorithm | As in the method table. |
| Side effects | Reads files. |
| Errors | `ConfigError` as listed; errors of U01-47–U01-49. |
| Concurrency | One instance per thread. |
| Complexity and limits | See U01-49. |
| Security notes | TH01-08, TH01-09, TH01-10. |
| Tests | UT01-45, UT01-94 |

#### U01-47 herness.connectors.files.InboxFile, FilesConnector.candidates

`InboxFile` (frozen dataclass): `path: Path` (resolved absolute), `rel_path: str` (POSIX, relative to `inbox_root`), `size_bytes: int`, `mtime: datetime` (aware, from `st_mtime_ns`), `mtime_ns: int`.

`candidates(entity: str) -> list[InboxFile]`.

| Item | Content |
|------|---------|
| Kind | dataclass, method |
| Purpose | Lists the files eligible for ingest. |
| Preconditions | — |
| Postconditions | Sorted by `(mtime, name)`; every entry is a regular file inside the entity folder, ≤ `MAX_INBOX_FILE_BYTES`, and unmodified for at least `settle_seconds`. |
| Invariants | — |
| Algorithm | 1. `folder = inbox_root / entity`; missing → `[]`. 2. For `p` in `folder.glob(pattern)` (non-recursive): skip names starting with `.` or `~$`; skip `p.is_symlink()` or `p.is_junction()` (reason `symlink`); `st = p.lstat()`; skip non-regular files (reason `not_regular`); skip if `p.resolve(strict=True)` is not under `folder.resolve()` (reason `outside_root`); skip if `clock() − mtime < settle_seconds` (reason `settling`, DEBUG only); skip if `st.st_size > MAX_INBOX_FILE_BYTES` (reason `too_large`). 3. Each skip except `settling` logs `connectors.files.rejected` (WARNING: `entity`, `file` = `rel_path`, `reason`). |
| Side effects | Directory listing, `lstat`. |
| Errors | `OSError` on the folder → `SourceUnavailable("inbox unreadable", entity)`. |
| Concurrency | Single thread. |
| Complexity and limits | O(files in folder). |
| Security notes | TH01-08, TH01-09. |
| Tests | UT01-45, UT01-50, ST01-08 |

#### U01-48 herness.connectors.files.fingerprint_file

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `f` | `InboxFile` | — | positional | — |

Returns `str` (64 lower-case hex).

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | SHA-256 of the file bytes (design 01 §5.10). |
| Preconditions | — |
| Postconditions | Renamed files with identical bytes give the same value. |
| Invariants | — |
| Algorithm | 1. Open with `open(f.path, "rb")`. 2. Read `FINGERPRINT_CHUNK_BYTES` chunks into `hashlib.sha256`. 3. If the total bytes read ≠ `f.size_bytes`, or `os.stat(f.path).st_mtime_ns ≠ f.mtime_ns` after reading → raise `InboxFileChanged`. 4. Return `hexdigest()`. |
| Side effects | Reads the file. |
| Errors | Changed → `InboxFileChanged(entity, rel_path)`; `OSError` → `SourceUnavailable("inbox file unreadable", rel_path)`. |
| Concurrency | Pure over the file. |
| Complexity and limits | O(size); constant memory. |
| Security notes | TH01-10. `hashlib.sha256` only (ENG §5.7). |
| Tests | UT01-46, ST01-10 |

#### U01-49 herness.connectors.files.FilesConnector.read_file

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `entity` | `str` | — | positional | — |
| `f` | `InboxFile` | — | positional | from `candidates` |

Returns `Iterator[pa.RecordBatch]`.

| Item | Content |
|------|---------|
| Kind | method |
| Purpose | Read one inbox file into lake batches with DuckDB (design 01 §5.10). |
| Preconditions | — |
| Postconditions | Batches carry the metadata columns plus the file's columns renamed with `to_snake`; CSV and XLSX values are strings (`all_varchar`), Parquet keeps Arrow types. |
| Invariants | — |
| Algorithm | 1. `con = duckdb.connect(":memory:", config={"memory_limit": FILE_READ_MEMORY_LIMIT, "threads": 2, "autoinstall_known_extensions": False, "autoload_known_extensions": False})`. 2. Suffix `.csv` → `SELECT * FROM read_csv($path, all_varchar = true, header = true)`; `.xlsx` → `LOAD excel` then `SELECT * FROM read_xlsx($path, all_varchar = true, sheet = $sheet)` (without the `sheet` argument when `sheet` is `None`); `.parquet` → `SELECT * FROM read_parquet($path)`. `$path = str(f.path)` is a bound parameter. 3. `reader = con.execute(sql, params).fetch_record_batch(cfg.batch_rows)`. 4. For each raw batch: a. rename columns with `to_snake` (collision → `SchemaViolation`); b. key columns = `to_snake` of each `key_field` (absent → `SchemaViolation("key field missing", entity)`); `_source_key` = the single key cast to string, or `pc.binary_join_element_wise(*keys, "\|")`; any null → `SchemaViolation(f"null key in {f.rel_path} at row {row_offset}")`; c. `_source_updated_at` = `parse_arrow_timestamps(column)` when `updated_field` is set (absent column → `SchemaViolation`), else `f.mtime` for every row; d. `_payload` = per row `json.dumps(dict(zip(original_names, values)), ensure_ascii=False, separators=(",", ":"), default=str)`; e. `_fetched_at = clock()`; `_deleted = False`; `_record_id` via `record_id`; f. yield metadata columns followed by the renamed columns. 5. After the reader is exhausted: re-`stat` the file; size or `mtime_ns` changed → `InboxFileChanged`. 6. Close the connection in `finally`. |
| Side effects | Reads the file. |
| Errors | `duckdb.Error` (I/O, invalid input, out of memory, missing `excel` extension) → `SchemaViolation(f"unreadable inbox file {f.rel_path}")` from the original; `InboxFileChanged`; key and timestamp errors as above. |
| Concurrency | Own DuckDB connection. |
| Complexity and limits | Memory ≤ 1 GB (DuckDB) + one batch; throughput targets BT01-04. |
| Security notes | TH01-09 (memory cap; decompression bombs fail as `SchemaViolation`), TH01-10. No macros or formulas are evaluated; values are read as text. |
| Tests | UT01-47, UT01-48, UT01-49, UT01-50, ST01-09, BT01-04 |

#### U01-50 herness.connectors.files_ingest.ingest_files

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `runner` | `SyncRunner` | — | positional | connector is `FilesConnector` |
| `entity` | `str` | — | positional | — |

Returns `SyncResult` (mode `incremental`, watermarks `None`).

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | The runner path for files: fingerprint dedupe, write, commit, `file_ingest` record, snapshot reconcile (design 01 §5.10). |
| Preconditions | — |
| Postconditions | Each new fingerprint is written to the lake and then recorded in `file_ingest`; known fingerprints are skipped. |
| Invariants | `record_file_ingest` runs only after the file's writer committed. |
| Algorithm | 1. `deletion = DeletionFilter("files", entity)`; `reload()`. 2. For each `f` in `connector.candidates(entity)`: a. `fp = fingerprint_file(f)`; `InboxFileChanged` → log `connectors.files.rejected` (reason `changed`) and continue. b. `get_file_ingest(fp)` not `None` → log `connectors.files.skipped_known` (INFO: `entity`, `fingerprint` first 12 hex) and continue. c. Wrap `connector.read_file(entity, f)` in a generator that, in snapshot mode, appends each batch's `_source_key` column to a list. d. `out = runner._write_stream(entity, wrapped, key="files", deletion=deletion, ordered=False, field=connector.watermark_field(entity), cap=f.mtime, advance_watermark=False)`; `InboxFileChanged` from the stream → the writer is already aborted; log `connectors.files.rejected` (reason `changed`) and continue. e. `fault_point("connector.before_watermark", source="files")`. f. `record_file_ingest(FileIngestRow(fp, "files", entity, f.rel_path, f.size_bytes, f.mtime, out.rows, files, clock()))`; log `connectors.files.ingested`; emit `herness_connectors_files_ingested_total`. g. Snapshot mode: `reconcile_entity(runner, entity, keys=<batches of pc.unique(concat(keys))>, deleted_at=f.mtime)`; add its tombstones to the totals. 3. Return the sums. |
| Side effects | Lake files, `file_ingest`, logs, metrics. |
| Errors | `SchemaViolation` from reading (the file is not recorded, so the next run retries it); the reconcile valve `SchemaViolation` (the file is already recorded; the operator reviews and runs `herness sync files --entity E --reconcile`); `StoreBusy`. |
| Concurrency | Single thread (`max_concurrency` 1). |
| Complexity and limits | Snapshot keys held in memory: ≤ 5M keys (≈ 250 MB); a larger snapshot raises `SchemaViolation("snapshot too large")`. |
| Security notes | TH01-10, TH01-11, TH01-12. |
| Tests | UT01-51, UT01-52, UT01-53, IT01-01, FT01-02, ST01-10, ST01-12 |

### 3.11 Jobs, factory, mapping check, health

#### U01-51 herness.connectors.jobs.handle_sync

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `ctx` | X:08/herness.core.jobs.JobContext | — | positional | job kind `sync`; payload read from `ctx.job.payload` (R-42) |

Returns X:08/herness.core.types.JobOutcome.

Payload model `SyncPayload` (private, config-model conventions): `source: str | None = None`; `entities: list[str] | None = None` (requires `source`); `mode: Literal["incremental", "backfill"] = "incremental"`; `start: datetime.date | None = None`; `end: datetime.date | None = None` (both only with `backfill`).

| Item | Content |
|------|---------|
| Kind | function (job handler; spec 08 §5.1; one argument `ctx`, R-42) |
| Purpose | Runs `sync` jobs from the scheduler and the CLI. A source whose breaker is open does not fail the job: the job ends `done` with outcome `skipped_open_circuit` and the next scheduled run retries (R-39). |
| Preconditions | Handler registered (U01-53). |
| Postconditions | Every requested entity was attempted once unless the job yielded. |
| Invariants | One failing entity does not stop the others (design 01 §6); an open breaker skips the rest of that source's entities. |
| Algorithm | 1. `p = SyncPayload.model_validate(ctx.job.payload)` (R-42); failure → `ConfigError("invalid sync payload")`. 2. `cfg = X:10/herness.core.config.get_config()`. 3. `names = [p.source]` or, when `None`, the names from `cfg.sources.enabled_sources()`. A named source that is disabled → `ConfigError`. 4. For each name: `conn = build_connector(name, cfg)`; `runner = SyncRunner(conn, settings, connector_factory=lambda: build_connector(name, cfg), progress=ctx.heartbeat, should_stop=ctx.should_yield)`; `entities = p.entities or conn.entities` (unknown → `ConfigError`). 5. For each entity: if `ctx.should_yield()` → return `JobOutcome(status="yield", result=<partial result>)`. Call `run_incremental(entity)`, or `run_backfill(entity, start, end)` with `start` = `p.start` at 00:00 UTC or `backfill_for(entity).resolve_start(now)` and `end` = `p.end` at 00:00 UTC or `now`. Append `result.to_dict()`; add `runner.skipped_open` to the skipped list; if `runner.stopped` → return `yield`. `CircuitOpen` → add `exc.key`, log `connectors.sync.skipped_open_circuit`, skip the rest of this source. Other `HernessError` → record `(name, entity, exc)`, log `connectors.sync.failed`, continue. 6. `result = {"results": [...], "partial": bool(failed or skipped), "skipped_open_circuit": sorted(skipped), "failed": [{"source", "entity", "error_class"}]}`. 7. If any failure was recorded: raise the first `FatalError`, else the first failure (spec 08 then fails or reschedules the job; the per-entity outcome is in the logs). 8. Else return `JobOutcome(status="done", result=result)`. |
| Side effects | Everything the runner does. |
| Errors | `ConfigError` (payload, config); the first recorded entity error. |
| Concurrency | Runs in the job thread; backfill spawns slice threads. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT01-54, UT01-55, UT01-56, IT01-08, FT01-06 |

#### U01-52 herness.connectors.jobs.handle_reconcile

Same signature as U01-51. Payload `ReconcilePayload`: `source: str` (required); `entities: list[str] | None = None`.

| Item | Content |
|------|---------|
| Kind | function (job handler) |
| Purpose | Runs `reconcile` jobs (design 01 §5.4; spec 08 §5.1). |
| Preconditions | Source enabled and reconcilable (not `monitoring`). |
| Postconditions | As U01-51 for mode `reconcile`. |
| Invariants | As U01-51. |
| Algorithm | Steps 1–8 of U01-51 with `run_reconcile(entity)` in place of the sync call; files entities in `delta` mode are skipped with log `connectors.reconcile.skipped` (INFO, reason `delta_mode`). |
| Side effects | As U01-44. |
| Errors | As U01-51; the safety-valve `SchemaViolation` fails the job. |
| Concurrency | Job thread. |
| Complexity and limits | — |
| Security notes | TH01-13. |
| Tests | UT01-55, IT01-08 |

#### U01-53 herness.connectors.jobs.register_job_handlers

No parameters; returns `None`.

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | Registers `handle_sync` for kind `sync` and `handle_reconcile` for kind `reconcile` with X:08/herness.core.jobs.register_handler. Called by the composition root (X:09/herness.cli worker and `run_inline` paths). |
| Preconditions | — |
| Postconditions | Both handlers registered. |
| Invariants | A second call is a no-op (module-level flag reset by the registry test fixture, ENG §2.3). |
| Algorithm | Check the flag; call `register_handler` twice; set the flag. |
| Side effects | Mutates the X:08 handler table. |
| Errors | None. |
| Concurrency | Called once at start-up in the main thread. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | IT01-08 |

#### U01-54 herness.connectors.jobs.build_sync_payload

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `source` | `str \| None` | — | positional | configured source or `None` = all |
| `entities` | `Sequence[str]` | `()` | keyword-only | only with `source` |
| `full` | `bool` | `False` | keyword-only | — |
| `backfill` | `bool` | `False` | keyword-only | — |
| `from_` | `datetime.date \| None` | `None` | keyword-only | — |
| `to` | `datetime.date \| None` | `None` | keyword-only | — |
| `reconcile` | `bool` | `False` | keyword-only | — |
| `today` | `datetime.date` | — | keyword-only | UTC date |

Returns `tuple[str, dict[str, object], str]` = (job kind, payload, `idem_key`).

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | Maps `herness sync` options (spec 09 §5.6 command row) to a job (design 01 §3.2 "CLI"). |
| Preconditions | — |
| Postconditions | The payload validates as `SyncPayload` or `ReconcilePayload`. |
| Invariants | — |
| Algorithm | 1. `entities` without `source` → error. 2. `reconcile` with `full` or `backfill` → error; `reconcile` without `source` → error; else return `("reconcile", {"source", "entities"}, f"reconcile:{source}")`. 3. `full` and `backfill` together → error. 4. `backfill`: `from_` required; `to` defaults to `today`; require `from_ < to ≤ today`; return `("sync", {"source", "entities", "mode": "backfill", "start": from_, "end": to}, f"sync:{source or 'all'}:backfill:{from_}:{to}")`. 5. `full`: same with `start = None`, `end = None` (the handler uses `backfill.start` and now; §13 D-6). 6. `from_` or `to` without `backfill` → error. 7. Else return `("sync", {"source", "entities", "mode": "incremental"}, f"sync:{source or 'all'}")` (matches the spec 08 `sync:<source>` dedupe key). Dates are serialised as ISO strings. |
| Side effects | None. |
| Errors | Each "error" above → `ConfigError` naming the conflicting options. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | TH01-07 (operator input validated, TB10). |
| Tests | UT01-57 |

`--check-mapping` and `--discover-fields` do not enqueue jobs: X:09 calls `check_mapping` (U01-56) and `JiraConnector.discover_fields` (U01-75) inline and prints the result.

#### U01-55 herness.connectors.factory.build_connector

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `name` | `str` | — | positional | source name |
| `cfg` | X:10/herness.core.config.HernessConfig | — | positional | loaded config |
| `clock` | `Callable[[], datetime]` | `now_utc` | keyword-only | — |

Returns `Connector`.

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | Constructs the configured connector through the registry (ENG §2.2). |
| Preconditions | `cfg.sources.source(name).enabled`, else `ConfigError(f"source {name} is disabled")`. |
| Postconditions | No network call has been made. |
| Invariants | — |
| Algorithm | 1. `settings = cfg.sources.source(name)`. 2. `cls = X:10/herness.core.registry.get("connector", name)`. 3. Keyword arguments by name: `files` → `inbox_root` = `settings.inbox` if absolute, else `cfg.paths.data.parent / settings.inbox`, resolved; `jira` → `custom_field_ids` = the non-empty values of X:02 `cfg.mappings.custom_fields.jira` (`story_points`, `team`, `estimate_cost_usd`, `epic_link`) in that order; `monitoring` → `adapters` = for each enabled adapter, `get("monitoring_adapter", tool)(adapter_settings, clock=clock)`. 4. Return `cls(settings, clock=clock, **kwargs)`. |
| Side effects | Lazy imports of the connector module. |
| Errors | `ConfigError` (disabled, unknown name from the registry). |
| Concurrency | Pure construction. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT01-94 |

#### U01-56 herness.connectors.mapping_check.check_mapping, MappingIssue, STAGING_FILES

`STAGING_FILES`: `servicenow → 110_stg_servicenow.sql`, `jira → 120_stg_jira.sql`, `monitoring → 130_stg_monitoring.sql`, `files → 140_stg_files.sql`, `mongodb → 150_stg_mongodb.sql`, `snowflake → 160_stg_snowflake.sql`, `dataverse → 170_stg_dataverse.sql` (spec 02 §4.1). `MappingIssue` (frozen dataclass): `entity: str`, `column: str`, `file: str`.

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `source` | `str` | — | positional | configured source |
| `cfg` | `HernessConfig` | — | positional | — |

Returns `list[MappingIssue]` sorted by `(entity, column)`; empty = pass.

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | `herness sync --check-mapping`: fail when staging SQL references a raw column the config does not fetch (design 01 §4.2). |
| Preconditions | — |
| Postconditions | Writes nothing. |
| Invariants | — |
| Algorithm | 1. Render the staging file with X:02/herness.model.build.render_sql (Jinja, config values only). 2. `sqlglot.parse(text, read="duckdb")`. 3. In each statement, find table expressions that call `read_parquet` with a string literal first argument matching `raw/<source>/(?P<entity>[a-z0-9_]+)/`; record the table alias, or the enclosing CTE name when the call is the only source of a CTE. 4. For each `Column` node: if its qualifier is a recorded alias or CTE name, add `column.name` (lower-cased) to `required[entity]`; an unqualified column counts when its `SELECT` has exactly one source and that source is recorded. 5. Drop names starting with `_`. 6. `fetched[entity]` by source: servicenow → `fields` ∪ `{f + "_display"}` ∪ `{sys_id, sys_updated_on, sys_class_name}` (all `to_snake`); jira → `JIRA_ISSUE_COLUMNS` (U01-71) ∪ custom field ids; monitoring → `EVENT_COLUMNS` or `METRIC_COLUMNS` (U01-80); mongodb → `to_snake` of `fields`, `key_field`, `updated_field`; snowflake → `to_snake` of `columns`; dataverse → `select` ∪ `key_field` ∪ `updated_field` ∪ `{c + "_display"}`; files → skipped (headers are only known from the files). 7. Each `required − fetched` element becomes a `MappingIssue`. |
| Side effects | Reads the SQL resource. |
| Errors | Render or parse failure → `ConfigError(f"cannot parse {file}")` from the original. |
| Concurrency | Pure over files. |
| Complexity and limits | O(size of SQL). |
| Security notes | — |
| Tests | UT01-58, IT01-07 |

#### U01-57 herness.connectors.health.source_health_report, SourceHealth

`SourceHealth` (frozen dataclass): `key: str` (stream key), `status: Literal["ok", "degraded", "down"]`, `reason: str`.

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `cfg` | `SourcesConfig` | — | positional | — |
| `now` | `datetime` | — | keyword-only | aware |

Returns `list[SourceHealth]`, one per stream key of each enabled source.

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | `health()` for `herness doctor` and the dashboard (ENG §4). Makes no source calls; `doctor --sources` calls `Connector.check()` separately (spec 09). |
| Preconditions | — |
| Postconditions | — |
| Invariants | — |
| Algorithm | Load `list_watermarks()` (U01-92) once. For each stream key: 1. X:08/herness.core.resilience.breaker(key).state(): `open` → `down`, reason `breaker open`; `half_open` → `degraded`, reason `breaker probing`. 2. Else, for sources other than `files`: any entity without a watermark → `degraded`, `no watermark for <entity>`; any watermark older than `STALE_AFTER` (24 h; monitoring 48 h) → `degraded`, `watermark stale for <entity>: <hours> h`. 3. Else `ok`, reason `""`. |
| Side effects | Reads `source_health` (via X:08) and `watermark` (via U01-92). |
| Errors | `StoreBusy`. |
| Concurrency | Any thread. |
| Complexity and limits | O(streams × entities). |
| Security notes | — |
| Tests | UT01-59 |

### 3.12 HTTP layer (`herness/connectors/http.py`)

Module constants: `MAX_RESPONSE_BYTES = 67_108_864` (64 MiB per page); `MAX_LINE_BYTES = 1_048_576` (streamed line); `MAX_PAGES_PER_STREAM = 1_000_000`.

#### U01-58 herness.connectors.http.http_client (re-exported as `herness.connectors.base.http_client`)

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `settings` | `SourceSettings \| MonitoringAdapterSettings` | — | positional | has `base_url` |
| `source` | `str` | — | keyword-only | stream key, for messages |
| `max_concurrency` | `int` | — | keyword-only | connection pool size |

Returns `httpx.Client` built by `herness.core.egress` (R-06).

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | The one place connectors obtain an HTTP client. It builds nothing itself: under R-06 and ENG §2.1 only `herness.core.egress` constructs `httpx` clients and transports, so this function selects the egress factory and passes the source's settings. Only the host of the source's `base_url` is reachable. |
| Preconditions | `base_url` set. |
| Postconditions | The client reaches only the `base_url` host; TLS verification on for every non-loopback host (`settings.httpx_verify()`); redirects not followed; environment proxies and `.netrc` ignored; timeouts set. |
| Invariants | Every request passes the egress-built host check. |
| Algorithm | 1. `host = urlsplit(base_url).hostname.lower()`. 2. `host` in {`127.0.0.1`, `localhost`, `::1`} → return X:10/herness.core.egress.loopback_http_client(base_url, timeout_s=settings.timeout_s) (R-06 signature). 3. Otherwise return X:10/herness.core.egress.source_http_client(base_url, verify=settings.httpx_verify(), timeout_s=settings.timeout_s, connect_timeout_s=X:08/herness.core.resilience.policy("source_http_page").connect_timeout_s, max_connections=2 × max_concurrency). The contract this spec requires of that factory (§13 O-9): the transport refuses any host other than the `base_url` host and any scheme other than `https` with `EgressBlocked`; `follow_redirects=False`; `trust_env=False`; proxy from X:10 `security.network.http_proxy`; TLS 1.2 or later with verification per `verify`; `retries=0`; keep-alive pool of `max_concurrency` connections. Default headers (`User-Agent`, `Accept`) are added per request by U01-59, so both factories behave alike. |
| Side effects | None until used. |
| Errors | `EgressBlocked` (X:10) at request time for a foreign host or scheme. |
| Concurrency | `httpx.Client` is thread-safe; each connector instance owns one. |
| Complexity and limits | — |
| Security notes | TH01-01, TH01-02, TH01-15. |
| Tests | UT01-63, ST01-01, ST01-02, ST01-14 |

#### U01-59 herness.connectors.http.SourceHttp, JsonPage, CursorGuard

Constructor `SourceHttp(client: httpx.Client, *, breaker_key: str, auth: httpx.Auth | None, clock=now_utc)`.

`JsonPage` (frozen dataclass): `status: int`, `body: object` (decoded JSON; validated by the caller), `headers: httpx.Headers`, `links: Mapping[str, str]` (rel → absolute URL, from the `Link` header).

| Method | Returns | Behavior |
|--------|---------|----------|
| `get_json(url, *, params=None, headers=None, allow_status=frozenset())` | `JsonPage` | One GET page with retry |
| `post_json(url, *, json_body, params=None, headers=None, allow_status=frozenset())` | `JsonPage` | One POST page with retry |
| `post_form_lines(url, *, data)` | `Iterator[dict[str, object]]` | Streamed POST of form data; yields one decoded JSON object per non-empty line (Splunk export) |
| `check_next_url(url: str)` | `str` | Returns `url` when it is absolute, `https`, and its host is allowed; else `ForeignHostError` |

`CursorGuard.step(cursor: str) -> None` raises `SchemaViolation("pagination cursor repeated")` when a cursor value repeats within a stream, or `SchemaViolation("page limit exceeded")` after `MAX_PAGES_PER_STREAM` steps.

| Item | Content |
|------|---------|
| Kind | class ×3 |
| Purpose | Page-level fetch with the spec 08 retry policy, size limits and HTTP error mapping (design 01 §6). |
| Preconditions | — |
| Postconditions | A returned page has status 2xx or a status in `allow_status`. |
| Invariants | Retries wrap exactly one page; cursors stay in the caller's generator, so a retry resumes at the failed page. |
| Algorithm | `get_json`/`post_json`: 1. Return `X:08/herness.core.resilience.retry_page(lambda: self._once(...), source=breaker_key)`. 2. `_once`: `fault_point("http.page", source=breaker_key)`; request headers = `{"User-Agent": "herness/<package version>", "Accept": "application/json"}` updated with `headers`; `with client.stream(method, url, params=..., headers=..., json=..., auth=auth) as resp`: if `resp.status_code` not in `allow_status`, `err = map_http_error(resp, now=clock())` and raise it when not `None`; read `resp.iter_bytes()` accumulating ≤ `MAX_RESPONSE_BYTES` (exceeded → `SchemaViolation("response too large")`); decode with `json.loads` (failure → `SchemaViolation("malformed JSON")`). 3. `httpx.HTTPError` → raise `classify(exc, family="source")` (X:08). 4. Log `connectors.http.page_fetched` (DEBUG: `source`, `status`, `bytes`, `elapsed_ms`); emit `herness_connectors_pages_total`. `post_form_lines`: the request is issued inside `retry_page` until the first line is read; later stream errors raise `SourceUnavailable` without retry (the export restarts on the next job attempt); each line ≤ `MAX_LINE_BYTES` (else `SchemaViolation`); invalid JSON line → `SchemaViolation`. |
| Side effects | Network; logs; metrics; retries emit spec 08 `retry` events. |
| Errors | `map_http_error` results; `SchemaViolation` (size, JSON, cursor); `ForeignHostError`; `classify` results. |
| Concurrency | Thread-safe when the underlying client and auth are (they are). |
| Complexity and limits | Memory ≤ 64 MiB per page. |
| Security notes | TH01-02, TH01-04, TH01-05, TH01-06. |
| Tests | UT01-62, ST01-04, ST01-05, FT01-04 |

#### U01-60 herness.connectors.http.map_http_error

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `response` | `httpx.Response` | — | positional | headers read |
| `now` | `datetime` | — | keyword-only | aware |

Returns `HernessError | None`.

| Status | Result |
|--------|--------|
| 2xx | `None` |
| 3xx | `SchemaViolation("unexpected redirect")` |
| 400 | `ConfigError("source rejected request")` |
| 401, 403 | `AuthError` |
| 404 | `SourceNotFound` |
| 408 | `SourceUnavailable` |
| 429 | `RateLimited(retry_after=parse_retry_after(response.headers, now=now))` |
| 500–599 (incl. 529) | `SourceUnavailable` |
| other 4xx | `SchemaViolation("unexpected status")` |

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | HTTP status → taxonomy error (design 01 §6). |
| Preconditions | — |
| Postconditions | Messages carry the status, method and URL path without the query string. |
| Invariants | — |
| Algorithm | Table lookup. |
| Side effects | None. |
| Errors | Returns, never raises. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | TH01-03 (no query strings or bodies in messages). |
| Tests | UT01-60, FT01-05 |

#### U01-61 herness.connectors.http.parse_retry_after

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `headers` | `Mapping[str, str]` | — | positional | case-insensitive |
| `now` | `datetime` | — | keyword-only | aware |

Returns `float | None` (seconds, ≥ 0).

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | Delay from `Retry-After` (seconds or HTTP date), else `X-RateLimit-Reset`, else `None` (design 01 §6). |
| Preconditions | — |
| Postconditions | Never negative. |
| Invariants | — |
| Algorithm | 1. `Retry-After` all digits → `float(value)`; else `email.utils.parsedate_to_datetime(value)` → `max(0, (dt − now).total_seconds())`; unparseable → step 2. 2. `X-RateLimit-Reset` numeric `v`: `v ≥ 10^12` → epoch ms, `max(0, v / 1000 − now.timestamp())`; `v ≥ 10^9` → epoch s, `max(0, v − now.timestamp())`; else `max(0, v)` seconds. 3. `None`. The spec 08 policy caps the value (`retry_after_cap_s`). |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | TH01-06. |
| Tests | UT01-61, PT01-02, ST01-06 |

#### U01-62 herness.connectors.http.ForeignHostError, SourceNotFound

| Class | Base | Attributes | Raised when |
|-------|------|------------|-------------|
| `ForeignHostError` | `SchemaViolation` | `host: str` | A response-supplied next URL (`Link`, `@odata.nextLink`) targets a host outside the source's `base_url` (`check_next_url`). A request to a foreign host is refused earlier by the egress-built transport with `EgressBlocked` (U01-58) |
| `SourceNotFound` | `SchemaViolation` | `path: str` | HTTP 404 (callers that expect it, such as the Jira bulk changelog, catch it) |

| Item | Content |
|------|---------|
| Kind | class ×2 (taxonomy subclasses declared by this spec, ENG §3.4) |
| Purpose | Distinguish two `SchemaViolation` cases that callers handle. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Fatal (subclass of `FatalError` through `SchemaViolation`). |
| Algorithm | — |
| Side effects | — |
| Errors | — |
| Concurrency | — |
| Complexity and limits | — |
| Security notes | TH01-02. |
| Tests | UT01-63, UT01-74 |

### 3.13 Auth (`herness/connectors/auth.py`)

Credential secret shapes (resolved with X:10/herness.core.secrets.resolve or `resolve_json`):

| Method | Secret shape | HTTP effect |
|--------|--------------|-------------|
| `basic` | JSON `{"username", "password"}` | `httpx.BasicAuth` |
| `api_token` (jira) | JSON `{"email", "token"}` | `httpx.BasicAuth(email, token)` |
| `api_token` (dynatrace) | plain token | `Authorization: Api-Token <token>` |
| `pat`, `bearer` | plain token | `Authorization: Bearer <token>` |
| `api_and_app_key` | JSON `{"api_key", "app_key"}` | `DD-API-KEY`, `DD-APPLICATION-KEY` headers |
| `oauth_client_credentials` | JSON `{"client_id", "client_secret"}` | `OAuthTokenAuth`, grant `client_credentials` |
| `oauth_password` | JSON `{"client_id", "client_secret", "username", "password"}` | `OAuthTokenAuth`, grant `password` |
| `msal_client_credentials` | JSON `{"client_id", "client_secret"}` or `{"client_id", "certificate_pem", "thumbprint"}` | `MsalTokenProvider` |
| `key_pair` | JSON `{"user", "private_key_pem", "passphrase"?}` | Snowflake connector (U01-88) |
| `connection_string` | plain MongoDB URI | MongoDB connector (U01-86) |

#### U01-63 herness.connectors.auth.build_auth, StaticHeaderAuth

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `auth` | `AuthSettings` | — | positional | — |
| `source` | `str` | — | keyword-only | `servicenow`, `jira`, `prometheus`, `datadog`, `splunk`, `dynatrace`, `dataverse` |
| `base_url` | `str` | — | keyword-only | source base URL |
| `token_client` | `httpx.Client` | — | keyword-only | the source's host-guarded client (token endpoints on the same host) |
| `clock` | `Callable[[], datetime]` | `now_utc` | keyword-only | — |

Returns `httpx.Auth | None` (`None` for method `none`).

| Item | Content |
|------|---------|
| Kind | function; class `StaticHeaderAuth(httpx.Auth)` |
| Purpose | Build the per-request auth for HTTP sources from secret references. |
| Preconditions | `auth.method` allowed for `source` (validated by settings). |
| Postconditions | Secret values live only inside the returned object as `SecretStr`, unwrapped when a header is set. |
| Invariants | `repr()` and `str()` of every auth object show `***` instead of values. |
| Algorithm | Dispatch on `(source, method)` per the shapes table. ServiceNow OAuth: `OAuthTokenAuth(token_url=f"{base_url}/oauth_token.do", ...)`. Dataverse: `MsalTokenProvider(..., scope=f"{base_url}/.default")`. A secret JSON missing a required member → `ConfigError(f"secret {name} lacks {member}")`. `key_pair` or `connection_string` → `ConfigError("not an HTTP auth method")`. |
| Side effects | Secret backend reads (X:10). |
| Errors | `ConfigError` (missing secret, bad shape). |
| Concurrency | Returned objects are thread-safe. |
| Complexity and limits | O(1). |
| Security notes | TH01-03, TH01-16. |
| Tests | UT01-64, UT01-66, ST01-03 |

#### U01-64 herness.connectors.auth.OAuthTokenAuth

Constructor `OAuthTokenAuth(token_url: str, form: Mapping[str, SecretStr | str], *, client: httpx.Client, clock=now_utc, refresh_margin_s: int = 300)`.

| Item | Content |
|------|---------|
| Kind | class (`httpx.Auth`) |
| Purpose | OAuth 2.0 token for ServiceNow (`client_credentials` or `password` grant), cached until 5 minutes before expiry (design 01 §5.7; ASVS V9, V10). |
| Preconditions | — |
| Postconditions | Every request carries `Authorization: Bearer <token>` with a token whose expiry is > now + margin. |
| Invariants | At most one token fetch in flight (`threading.Lock`). |
| Algorithm | `auth_flow(request)`: 1. Under the lock, if no token or `expires_at − refresh_margin_s ≤ clock()`, fetch: `POST token_url` with the form (values unwrapped) through `client` (not through `retry_page`); non-2xx → `map_http_error` result (400/401 become `AuthError`); body validated with private model `TokenResponse(access_token: str 1–8192 chars, expires_in: int 1–86400, token_type: str)`; failure → `SchemaViolation("bad token response")`; `expires_at = clock() + expires_in`. 2. Set the header; `yield request`. 3. If the response is 401 and this request has not been retried, drop the token, fetch a new one, set the header and `yield` the request once more. |
| Side effects | Token requests; log `connectors.auth.token_refreshed` (DEBUG: `source`) without the token. |
| Errors | `AuthError`, `SourceUnavailable`, `SchemaViolation`. |
| Concurrency | Thread-safe (lock). |
| Complexity and limits | One token request per ≈ `expires_in − 300 s`. |
| Security notes | TH01-16, TH01-03. |
| Tests | UT01-65, ST01-15 |

#### U01-65 herness.connectors.auth.MsalTokenProvider

Constructor `MsalTokenProvider(*, tenant_id: str, client_id: str, credential: SecretStr | Mapping[str, SecretStr], scope: str, clock=now_utc, refresh_margin_s: int = 300)`.

| Item | Content |
|------|---------|
| Kind | class (`httpx.Auth`) |
| Purpose | Dataverse token via `msal.ConfidentialClientApplication(...).acquire_token_for_client(scopes=[scope])`, cached until 5 minutes before expiry (design 01 §5.9). |
| Preconditions | `login.microsoftonline.com` is listed in `sources.dataverse.hosts` (U01-12), so the socket guard allows MSAL's own HTTP session (R-06). |
| Postconditions | As U01-64. |
| Invariants | As U01-64. |
| Algorithm | 1. Build the MSAL app once with `authority=f"https://login.microsoftonline.com/{tenant_id}"` and `client_credential` = the secret string, or `{"private_key": pem, "thumbprint": thumbprint}`. 2. `auth_flow`: under the lock, refresh when missing or within the margin; the MSAL result without `access_token` → `AuthError(f"msal error {result.get('error')}")` (the error code only; never `error_description`); `expires_at = clock() + result["expires_in"]`. 3. Header `Authorization: Bearer <token>`; retry once on 401 as U01-64. MSAL transport errors (`requests.RequestException`) → `SourceUnavailable`. |
| Side effects | Token requests by MSAL (its own HTTP session). |
| Errors | `AuthError`, `SourceUnavailable`. |
| Concurrency | Thread-safe (lock). |
| Complexity and limits | — |
| Security notes | TH01-16, TH01-03, TH01-18. |
| Tests | UT01-90, ST01-15 |

### 3.14 ServiceNow (`herness/connectors/servicenow.py`)

#### U01-66 herness.connectors.servicenow.ServiceNowConnector

Constructor `ServiceNowConnector(settings: ServiceNowSettings, *, http: SourceHttp | None = None, clock=now_utc)`; decorated `register("connector", "servicenow")`. When `http` is `None` it builds `SourceHttp(http_client(settings, source="servicenow", max_concurrency=...), breaker_key="servicenow", auth=build_auth(...))`.

| Member | Behavior |
|--------|----------|
| `name` | `"servicenow"` |
| `entities` | configured entity names |
| `watermark_field(entity)` | `"sys_updated_on"` |
| `check()` | `GET /api/now/table/<first entity>` with `sysparm_limit=1`, `sysparm_fields=sys_id`; errors propagate |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | ServiceNow Table API connector (design 01 §4.2, §5.7). |
| Preconditions | Integration user timezone is UTC (design 01 §5.7; verified manually, §13 V-1). |
| Postconditions | — |
| Invariants | Table name = entity name. |
| Algorithm | See U01-67–U01-70. |
| Side effects | Network reads. |
| Errors | §6. |
| Concurrency | One instance per thread. |
| Complexity and limits | — |
| Security notes | TH01-01–TH01-06. |
| Tests | UT01-67, UT01-94 |

#### U01-67 herness.connectors.servicenow.build_sn_query

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `since` | `datetime \| None` | — | keyword-only | aware |
| `until` | `datetime \| None` | — | keyword-only | aware; `None` only for key listing |
| `classes` | `Sequence[str] \| None` | `None` | keyword-only | `cmdb_ci` only |
| `filter` | `str \| None` | `None` | keyword-only | validated config |
| `order` | `Literal["time", "key"]` | `"time"` | keyword-only | — |

Returns `str` (`sysparm_query` value).

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | Encoded query of design 01 §5.7. |
| Preconditions | — |
| Postconditions | Clauses joined with `^` in this order: `sys_updated_on>=<since>`, `sys_updated_on<<until>`, `sys_class_nameIN<c1,c2>`, `<filter>`, then `ORDERBYsys_updated_on^ORDERBYsys_id` (`order="time"`) or `ORDERBYsys_id` (`order="key"`). Absent parts are omitted. |
| Invariants | — |
| Algorithm | Timestamps formatted `%Y-%m-%d %H:%M:%S` in UTC after truncating to whole seconds (`since` floored keeps it inclusive; `until` floored excludes the last partial second, which the next run's overlap re-reads). |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | TH01-07 (filter validated in U01-07). |
| Tests | UT01-67 |

#### U01-68 herness.connectors.servicenow.ServiceNowConnector.sync

Signature per U01-16.

| Item | Content |
|------|---------|
| Kind | method |
| Purpose | Ascending, windowed, paged fetch with optional delete tombstones (design 01 §5.4, §5.7). |
| Preconditions | `entity` configured. |
| Postconditions | Batches ascending by `(_source_updated_at, _source_key)`, including merged tombstones. |
| Invariants | `sysparm_display_value=all`: each field is `{value, display_value}`, flattened into `<f>` and `<f>_display`. |
| Algorithm | 1. `until = until or clock()`; `since = since or settings.backfill_for(entity).resolve_start(until)`. 2. `windows = split_range(since, until, timedelta(hours=entity.window_hours))`. 3. `fetch_fields` = `sys_id`, `sys_updated_on`, `sys_class_name` (for `cmdb_ci`), then the configured fields, deduplicated. `batcher = RowBatcher("servicenow", entity, batch_rows=..., columns=[f, f + "_display" for each fetch field])`. 4. Per window: `deletes = _audit_deletes(entity, w0, w1)` (below); `records = _pages(entity, build_sn_query(since=w0, until=w1, ...))` mapped to `(ts, sys_id, rec)`; for each item of `merge_by_time(records, deletes)`: a live item → `batcher.add(sys_id, ts, json.dumps(rec, ensure_ascii=False, separators=(",", ":")), flatten_record(rec, fields=fetch_fields, display_pairs=True))`; a delete → `batcher.add_tombstone(key, ts)`; yield each full batch. 5. Yield `batcher.flush()` when not `None`. `_pages(entity, query)`: `url = f"/api/now/table/{table}"`, `offset = 0`, params `sysparm_query`, `sysparm_fields=",".join(fetch_fields)`, `sysparm_limit=page_size`, `sysparm_offset`, `sysparm_display_value=all`, `sysparm_exclude_reference_link=true`, `sysparm_no_count=true`; loop: `page = http.get_json(url, params=params)`; body must be an object with a list `result` (else `SchemaViolation`); yield its records; stop when `len(result) < page_size`; if `page.links` has `next` → `url = http.check_next_url(next)`, `params = None`; else `offset += len(result)`; `CursorGuard.step(f"{url}\|{offset}")`. Each record: `sys_id.value` non-empty string (else `SchemaViolation("missing sys_id")`); `ts = parse_source_timestamp(rec["sys_updated_on"]["value"], field="sys_updated_on")`. `_audit_deletes(entity, w0, w1)`: on first call per instance, probe `GET /api/now/table/sys_audit_delete` with `sysparm_limit=1`, `sysparm_fields=sys_id`, `allow_status={403}`; 403 → mark unreadable, log `connectors.servicenow.audit_delete_unreadable` (INFO) once, return `[]` from then on. Readable: page through `sys_audit_delete` with query `tablename=<table>^sys_created_on>=<w0>^sys_created_on<<w1>^ORDERBYsys_created_on^ORDERBYdocumentkey`, fields `documentkey,sys_created_on`, `sysparm_display_value=false`; return a list of `(parse(sys_created_on), documentkey)`; more than 100,000 in one window → `SchemaViolation("too many deletes in window; lower window_hours")`. |
| Side effects | Network reads. |
| Errors | §6; `SchemaViolation` for shape and key errors. |
| Concurrency | One instance per thread. |
| Complexity and limits | Memory: one page + one batch + the window's deletes. Offset drift (design 01 §5.7): a record updated during paging moves to `sys_updated_on ≥ until`, so a skipped record is fetched by the next run; windows of `window_hours` keep offsets shallow. |
| Security notes | TH01-02 (next links checked), TH01-04, TH01-05. |
| Tests | UT01-68, UT01-69, UT01-70, IT01-02, IT01-10, BT01-01 |

#### U01-69 herness.connectors.servicenow.merge_by_time

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `records` | `Iterator[tuple[datetime, str, T]]` | — | positional | ascending by `(ts, key)` |
| `deletes` | `Sequence[tuple[datetime, str]]` | — | positional | ascending |

Returns `Iterator[tuple[datetime, str, T | None]]` (`None` marks a delete).

| Item | Content |
|------|---------|
| Kind | function (pure, generic) |
| Purpose | Keeps the output ascending when tombstones join the record stream (runner invariant, design 01 §5.3). |
| Preconditions | Both inputs ascending. |
| Postconditions | Output ascending by `(ts, key)`; on equal `(ts, key)` the record comes first. |
| Invariants | — |
| Algorithm | Two-way merge. |
| Side effects | None. |
| Errors | An input out of order → `SchemaViolation("source order violated")`. |
| Concurrency | Pure. |
| Complexity and limits | O(n + m). |
| Security notes | — |
| Tests | UT01-70 |

#### U01-70 herness.connectors.servicenow.ServiceNowConnector.list_keys

Signature per U01-17.

| Item | Content |
|------|---------|
| Kind | method |
| Purpose | Key-only listing: `sysparm_fields=sys_id` (design 01 §5.4). |
| Preconditions | — |
| Postconditions | All keys matching `classes` and `filter`. |
| Invariants | — |
| Algorithm | `_pages` with `build_sn_query(since=None, until=None, classes, filter, order="key")`, `sysparm_fields=sys_id`, `sysparm_display_value=false`; each `sys_id` string goes into `KEY_SCHEMA` batches of `batch_rows`. |
| Side effects | Network reads. |
| Errors | §6. |
| Concurrency | One instance per thread. |
| Complexity and limits | Memory one page + one batch. |
| Security notes | TH01-13. |
| Tests | UT01-71 |

### 3.15 Jira (`herness/connectors/jira.py`, `herness/connectors/jira_changelog.py`)

#### U01-71 herness.connectors.jira.JiraConnector, JIRA_FIELDS, JIRA_ISSUE_COLUMNS

Constructor `JiraConnector(settings: JiraSettings, *, custom_field_ids: Sequence[str] = (), http: SourceHttp | None = None, clock=now_utc)`; decorated `register("connector", "jira")`.

| Member | Behavior |
|--------|----------|
| `name`, `entities` | `"jira"`, `("issue",)` |
| `watermark_field(entity)` | `"updated"` |
| `check()` | `GET /rest/api/{v}/myself` (`v` = `3` Cloud, `2` DC) |
| `JIRA_FIELDS` | `("issuetype", "parent", "project", "components", "labels", "status", "created", "resolutiondate", "summary", "description", "updated", "issuelinks")` |
| `JIRA_ISSUE_COLUMNS` | `("id", "key")` + `JIRA_FIELDS` + `("changelog", "remotelinks")` |

| Item | Content |
|------|---------|
| Kind | class, constants |
| Purpose | Jira Cloud and Data Center connector (design 01 §4.2, §5.8). |
| Preconditions | Service account timezone is UTC (§13 V-2). Custom field ids match `^customfield_\d{1,10}$`, else `ConfigError`. |
| Postconditions | — |
| Invariants | Never requests `*all` fields. |
| Algorithm | See U01-72–U01-77. |
| Side effects | Network reads. |
| Errors | §6. |
| Concurrency | One instance per thread. |
| Complexity and limits | — |
| Security notes | TH01-01–TH01-06. |
| Tests | UT01-72, UT01-94 |

#### U01-72 herness.connectors.jira.build_jql

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `since` | `datetime \| None` | — | keyword-only | aware |
| `until` | `datetime \| None` | — | keyword-only | aware |
| `scope` | `str \| None` | — | keyword-only | validated `jql_scope` |
| `order` | `Literal["time", "key"]` | `"time"` | keyword-only | — |

Returns `str`.

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | JQL of design 01 §5.8. |
| Preconditions | — |
| Postconditions | `order="time"`: `updated >= "<since>" AND updated < "<until>" AND (<scope>) ORDER BY updated ASC, id ASC` with absent clauses omitted; `order="key"`: `(<scope>) ORDER BY id ASC`, or `ORDER BY id ASC` without scope. |
| Invariants | — |
| Algorithm | Timestamps in UTC, floored to the minute, formatted `%Y/%m/%d %H:%M` (JQL minute precision; the 60-minute overlap covers truncation and Cloud search's eventual consistency). |
| Side effects | None. |
| Errors | None. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | TH01-07. |
| Tests | UT01-72 |

#### U01-73 herness.connectors.jira.JiraConnector.sync

Signature per U01-16.

| Item | Content |
|------|---------|
| Kind | method |
| Purpose | Search pages plus complete changelog and remote links per issue (design 01 §4.2 Jira, §5.8). |
| Preconditions | `entity == "issue"`. |
| Postconditions | Every row carries complete `changelog` and `remotelinks` (NULL when `fetch_remote_links` is off). Ascending by `(updated, id)`. |
| Invariants | — |
| Algorithm | 1. `jql = build_jql(since=since, until=until, scope=settings.jql_scope)`; `fields = list(JIRA_FIELDS) + custom_field_ids`. 2. Cloud: loop `POST /rest/api/3/search/jql` with `{"jql", "fields", "maxResults": page_size}` plus `"nextPageToken"` after the first page; body must hold a list `issues`; `last = isLast is True`, or `isLast` absent and no `nextPageToken`; `not last` without a token → `SchemaViolation("missing nextPageToken")`; a page smaller than `maxResults` with `isLast: false` continues; `CursorGuard.step(token)`. 3. DC: loop `POST /rest/api/2/search` with `{"jql", "fields", "startAt", "maxResults": page_size, "expand": ["changelog"]}`; `startAt += len(issues)`; stop when `issues` is empty or `startAt ≥ total`. 4. Per page: `changelogs = fetch_changelogs(http, flavor=..., issues=issues, state=self._cl_state)`; `links = fetch_remote_links(http, version=v, issue_ids=ids)` when enabled. 5. Per issue: `cols = flatten_issue(issue, changelog=changelogs[id], remotelinks=links[id] if enabled else None, custom_field_ids=custom_field_ids)` (U01-93: the raw column contract of §4.5, R-59; it validates `id`); `ts = parse_source_timestamp(issue["fields"]["updated"], field="updated")`; `payload = json.dumps(issue, ensure_ascii=False, separators=(",", ":"))`; `batcher.add(id, ts, payload, cols)` with `batcher` built with `columns = JIRA_ISSUE_COLUMNS + custom_field_ids`. 6. Yield full batches and the final flush. |
| Side effects | Network reads: 1 search call per page, ≥ 1 changelog call per page (Cloud), 1 remote-link call per issue when enabled. |
| Errors | §6; `SchemaViolation` for shapes. |
| Concurrency | One instance per thread. |
| Complexity and limits | Memory one page (≤ 100 issues Cloud, ≤ 1,000 DC) with changelogs. |
| Security notes | TH01-04, TH01-05. |
| Tests | UT01-72, UT01-73, UT01-75, UT01-76 |

#### U01-74 herness.connectors.jira.JiraConnector.list_keys

| Item | Content |
|------|---------|
| Kind | method |
| Purpose | Key-only listing with `fields=id` (design 01 §5.4). |
| Preconditions | `entity == "issue"`. |
| Postconditions | All issue ids in scope. |
| Invariants | — |
| Algorithm | Same paging as U01-73 steps 2–3 with `jql = build_jql(since=None, until=None, scope=..., order="key")`, `fields = ["id"]`, no `expand`; ids go into `KEY_SCHEMA` batches. |
| Side effects | Network reads. |
| Errors | §6. |
| Concurrency | One instance per thread. |
| Complexity and limits | One page in memory. |
| Security notes | TH01-13. |
| Tests | UT01-78 |

#### U01-75 herness.connectors.jira.JiraConnector.discover_fields, FieldCandidate

`FieldCandidate` (frozen dataclass): `id: str`, `name: str`, `schema_type: str | None`, `suggested_key: Literal["jira.story_points", "jira.team", "jira.epic_link", "jira.estimate_cost_usd"]`.

Returns `list[FieldCandidate]` sorted by `(suggested_key, name)`.

| Item | Content |
|------|---------|
| Kind | method, dataclass |
| Purpose | `herness sync jira --discover-fields` (design 01 §5.8). Writes nothing. |
| Preconditions | — |
| Postconditions | — |
| Invariants | — |
| Algorithm | `GET /rest/api/{v}/field` → list of `{id, name, custom, schema}` (not a list → `SchemaViolation`). Candidates by `name.casefold()`: `story points` or `story point estimate` → `jira.story_points`; `team` → `jira.team`; `epic link` → `jira.epic_link`; `custom` true, `schema.type == "number"` and name matching `(?i)cost\|usd\|budget` → `jira.estimate_cost_usd`. |
| Side effects | One network read. |
| Errors | §6. |
| Concurrency | — |
| Complexity and limits | O(fields). |
| Security notes | — |
| Tests | UT01-77 |

#### U01-76 herness.connectors.jira_changelog.fetch_changelogs, ChangelogState

`ChangelogState` (mutable dataclass, one per connector instance): `bulk_available: bool = True`.

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `http` | `SourceHttp` | — | positional | — |
| `flavor` | `Literal["cloud", "datacenter"]` | — | keyword-only | — |
| `issues` | `Sequence[Mapping[str, object]]` | — | keyword-only | one search page (≤ 1,000 issues) |
| `state` | `ChangelogState` | — | keyword-only | — |

Returns `dict[str, list[dict[str, object]]]` (issue id → histories ascending by `(created, id)`).

| Item | Content |
|------|---------|
| Kind | function, dataclass |
| Purpose | Complete changelog per issue (design 01 §5.8). |
| Preconditions | — |
| Postconditions | Every issue id has an entry (possibly empty). Each history is projected by `project_history` (U01-94); `author` is dropped (data minimisation). |
| Invariants | — |
| Algorithm | Cloud: 1. If `state.bulk_available`: loop `POST /rest/api/3/changelog/bulkfetch` with `{"issueIdsOrKeys": ids, "maxResults": 1000}` plus `"nextPageToken"` after the first call; append each `issueChangeLogs[].changeHistories` to its `issueId`; stop when `nextPageToken` is absent. `SourceNotFound` → `state.bulk_available = False`, log `connectors.jira.bulk_changelog_unavailable` (WARNING) once, go to step 2 for this page. 2. Otherwise per issue: loop `GET /rest/api/3/issue/{id}/changelog?startAt=<n>&maxResults=100` until `isLast` is true, or `startAt + len(values) ≥ total`, or `values` is empty. DC: histories from the search `changelog.histories`; when `changelog.total > len(histories)`, `GET /rest/api/2/issue/{id}?expand=changelog&fields=id` and use its `changelog.histories`. |
| Side effects | Network reads. |
| Errors | §6; shape errors → `SchemaViolation`. |
| Concurrency | One call per page, single thread. |
| Complexity and limits | Calls per page: Cloud bulk ≥ 1; fallback 1 per issue per 100 histories. |
| Security notes | TH01-05; personal-data minimisation. |
| Tests | UT01-74, UT01-75 |

#### U01-77 herness.connectors.jira_changelog.fetch_remote_links

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `http` | `SourceHttp` | — | positional | — |
| `version` | `Literal["2", "3"]` | — | keyword-only | — |
| `issue_ids` | `Sequence[str]` | — | keyword-only | — |

Returns `dict[str, list[dict[str, object]]]`.

| Item | Content |
|------|---------|
| Kind | function |
| Purpose | Remote links per issue, one call each (design 01 §4.2, §5.8). |
| Preconditions | `fetch_remote_links` is true. |
| Postconditions | Each link projected by `project_remote_link` (U01-94). |
| Invariants | — |
| Algorithm | For each id: `GET /rest/api/{version}/issue/{id}/remotelink`; the body must be a list (else `SchemaViolation`); project. |
| Side effects | Network reads. |
| Errors | §6. |
| Concurrency | Single thread. |
| Complexity and limits | 1 call per issue (≈ 20 issues/s, design 01 §8). |
| Security notes | URLs are stored as data and never fetched. |
| Tests | UT01-76 |

#### U01-93 herness.connectors.jira.flatten_issue

| Parameter | Type | Default | Kind | Constraint |
|-----------|------|---------|------|------------|
| `issue` | `Mapping[str, object]` | — | positional | One issue object as the search API returns it (`id`, `key`, `fields`) |
| `changelog` | `Sequence[Mapping[str, object]]` | — | keyword-only | Complete histories of the issue (raw or already projected) |
| `remotelinks` | `Sequence[Mapping[str, object]] \| None` | — | keyword-only | `None` when `fetch_remote_links` is off |
| `custom_field_ids` | `Sequence[str]` | `()` | keyword-only | Each `^customfield_\d{1,10}$` |

Returns `dict[str, str | None]` whose keys are exactly `JIRA_ISSUE_COLUMNS` followed by `custom_field_ids`, in that order.

| Item | Content |
|------|---------|
| Kind | function (pure) |
| Purpose | The single implementation of the Jira raw column-name contract (R-59, §4.5): the connector writes its output to the lake, impl 02 staging (`120_stg_jira.sql`) reads it, and the impl 11 generator calls this function so synthetic rows match real ones. |
| Preconditions | As in the table. |
| Postconditions | `id` and `key` are strings; every other value is `str` or `None`; `changelog` is a compact JSON array ascending by `(created, id)` with no `author` members; `remotelinks` is a compact JSON array or `None`. |
| Invariants | Column names never depend on the data: a field absent from `fields` gives a `None` value, not a missing key. |
| Algorithm | 1. `id` must be a non-empty string of digits, else `SchemaViolation("bad issue id")`; `key` must match `^[A-Z][A-Z0-9_]{0,31}-\d{1,10}$`, else `SchemaViolation("bad issue key", id)`. 2. `fields` must be a mapping, else `SchemaViolation("issue fields missing", id)`. 3. A custom field id not matching its pattern → `ConfigError(f"bad custom field id {cid}")`. 4. `out = {"id": id, "key": key}`, then `out.update(flatten_record(fields, fields=list(JIRA_FIELDS) + list(custom_field_ids)))` (U01-23: objects and arrays become compact JSON text, numbers become text, timestamps stay the source strings). 5. `out["changelog"]` = compact JSON (`ensure_ascii=False`, `separators=(",", ":")`) of `[project_history(h) for h in changelog]` sorted by `(created, id)`. 6. `out["remotelinks"]` = `None` when `remotelinks` is `None`, else compact JSON of `[project_remote_link(r) for r in remotelinks]` in input order. 7. Return `out`. |
| Side effects | None. |
| Errors | `SchemaViolation` (id, key, fields); `ConfigError` (custom field id); `flatten_record` collision `SchemaViolation`. |
| Concurrency | Pure. |
| Complexity and limits | O(size of issue + changelog). |
| Security notes | TH01-05; personal-data minimisation (changelog `author` dropped). |
| Tests | UT01-96, IT01-11 |

#### U01-94 herness.connectors.jira_changelog.project_history, project_remote_link

`project_history(history: Mapping[str, object]) -> dict[str, object]`; `project_remote_link(link: Mapping[str, object]) -> dict[str, object]`.

| Item | Content |
|------|---------|
| Kind | function ×2 (pure) |
| Purpose | The projections stored in the `changelog` and `remotelinks` columns (design 01 §4.2, §5.8), shared by U01-76, U01-77 and U01-93. |
| Preconditions | `history` holds `id` (string) and `created` (string); `link` holds `id` (number or string) and an `object` mapping. |
| Postconditions | `project_history` returns `{"id", "created", "items": [{"field", "from", "fromString", "to", "toString"}]}` (missing members become `None`; `items` absent → `[]`); `project_remote_link` returns `{"id", "object": {"url", "title"}}` with `id` as a string. Both are idempotent: projecting a projected value returns an equal value. |
| Invariants | No other member (such as `author`, `application`, `relationship`) survives. |
| Algorithm | Build the output dictionaries member by member from the input; `items` elements that are not mappings are skipped. |
| Side effects | None. |
| Errors | Missing `id` or `created` in a history, or missing `id` or `object` in a link → `SchemaViolation("bad changelog history")` or `SchemaViolation("bad remote link")`. |
| Concurrency | Pure. |
| Complexity and limits | O(size of input). |
| Security notes | TH01-05; personal-data minimisation. |
| Tests | UT01-74, UT01-96 |

### 3.16 Monitoring (`herness/connectors/monitoring/`)

Adapter constructor convention (every adapter): `<Adapter>(settings: MonitoringAdapterSettings, *, clock=now_utc, http: SourceHttp | None = None, batch_rows: int = DEFAULT_BATCH_ROWS)`; when `http` is `None` it is built with `breaker_key=f"monitoring:{tool}"`. Adapters are decorated `register("monitoring_adapter", <tool>)`.

#### U01-78 herness.connectors.monitoring.base.MonitoringAdapter

| Member | Signature | Contract |
|--------|-----------|----------|
| `tool` | `str` attribute | `prometheus` \| `datadog` \| `splunk` \| `dynatrace` |
| `check()` | `-> None` | One cheap authenticated read |
| `events(since, until)` | `(datetime, datetime) -> Iterator[pa.RecordBatch]` | Event rows built with `event_batch`, `ts` in `[since, until)` as the tool filters them; any order |
| `daily_metrics(since, until)` | `(datetime, datetime) -> Iterator[pa.RecordBatch]` | Metric rows built with `metric_batch` for complete UTC days in `complete_days(since, until)` only; never raw series |

| Item | Content |
|------|---------|
| Kind | protocol (design 01 §3.3) |
| Purpose | One monitoring tool behind the `monitoring` connector. |
| Preconditions | Aware datetimes, `since < until`. |
| Postconditions | Rows carry `source_tool = tool`. |
| Invariants | Events and daily aggregates only (design 01 §2). |
| Algorithm | — |
| Side effects | Network reads. |
| Errors | §6. |
| Concurrency | One instance per thread. |
| Complexity and limits | — |
| Security notes | TH01-04. |
| Tests | UT01-79 |

#### U01-79 herness.connectors.monitoring.base.MonitoringConnector

Constructor `MonitoringConnector(settings: MonitoringSettings, *, adapters: Sequence[MonitoringAdapter], clock=now_utc)`; decorated `register("connector", "monitoring")`.

| Member | Behavior |
|--------|----------|
| `name`, `entities` | `"monitoring"`, configured subset of `("event", "metric_daily")` |
| `watermark_field(entity)` | `"ts"` for `event`, `"date"` for `metric_daily` |
| `check()` | Calls `check()` on every adapter in order; the first error propagates |
| `tools()`, `stream_key(tool)` | Per U01-18 |
| `sync_tool(tool, entity, since, until)` | `since=None` → `settings.backfill_for(entity).resolve_start(until)`; `event` → `adapter.events(since, until)`; `metric_daily` → `adapter.daily_metrics(since, until)` |
| `sync(entity, since, until=None)` | Chains `sync_tool` over all tools (`until=None` → `clock()`); used by protocol checks, not by the runner |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | Fan-out connector over the enabled adapters (design 01 §3.3), implementing `Connector` and `SupportsToolStreams`. Not `SupportsKeyListing` (monitoring is not reconciled). |
| Preconditions | At least one adapter. |
| Postconditions | — |
| Invariants | Streams are keyed `monitoring:<tool>` for breakers, watermarks and slices; `_source` is `monitoring`. |
| Algorithm | As in the member table. |
| Side effects | Network reads through adapters. |
| Errors | `ConfigError` for unknown tool or entity; adapter errors. |
| Concurrency | One instance per thread. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT01-79, UT01-92, UT01-94 |

#### U01-80 herness.connectors.monitoring.base.EventRow, MetricRow, event_batch, metric_batch, EVENT_COLUMNS, METRIC_COLUMNS

`EventRow` (`TypedDict`): `event_key: str`, `ts: datetime`, `service: str | None`, `host: str | None`, `severity_raw: str | None`, `title: str | None`, `status: str | None`, `dedup_key: str | None`, `end_ts: datetime | None`, `incident_ref: str | None`, `payload: str`.
`MetricRow` (`TypedDict`): `date: datetime.date`, `service: str`, `metric_name: str`, `value: float | None`, `unit: str`, `payload: str`.
`EVENT_COLUMNS = ("source_tool", "event_key", "ts", "service", "host", "severity_raw", "title", "status", "dedup_key", "end_ts", "incident_ref")`; `METRIC_COLUMNS = ("source_tool", "date", "service", "metric_name", "value", "unit")`.

`event_batch(tool: str, rows: Sequence[EventRow], *, fetched_at: datetime) -> pa.RecordBatch`; `metric_batch(tool: str, rows: Sequence[MetricRow], *, fetched_at: datetime) -> pa.RecordBatch`.

| Item | Content |
|------|---------|
| Kind | TypedDict ×2, function ×2, constant ×2 (pure) |
| Purpose | Lake rows for `monitoring/event` and `monitoring/metric_daily` (design 01 §4.2 Monitoring). |
| Preconditions | 1 ≤ rows ≤ `batch_rows`; aware datetimes; non-empty `event_key` and `service` for metrics. |
| Postconditions | Metadata columns, then the column tuple, all field columns `string`. Event: `_source_key = f"{tool}:{event_key}"`, `_source_updated_at = ts`. Metric: `_source_key = f"{tool}\|{metric_name}\|{service}\|{date.isoformat()}"`, `_source_updated_at` = `date` + 1 day at 00:00 UTC (end of the day bucket). `ts`/`end_ts` as ISO-8601 with `Z`; `date` as ISO date; `value` as `json.dumps(float)`, `None` for NaN or infinite. |
| Invariants | — |
| Algorithm | Build columns with `record_id` and `pa.array`. |
| Side effects | None. |
| Errors | Violated precondition → `SchemaViolation("monitoring row", tool)`. |
| Concurrency | Pure. |
| Complexity and limits | O(rows). |
| Security notes | TH01-05. |
| Tests | UT01-79 |

#### U01-81 herness.connectors.monitoring.base.floor_day, complete_days

`floor_day(ts: datetime) -> datetime` returns 00:00:00 UTC of `ts`'s UTC date. `complete_days(since: datetime, until: datetime) -> tuple[datetime, datetime]` returns `(floor_day(since), floor_day(until))`, the half-open range of whole UTC days that have ended before `until`; empty when the first ≥ the second.

| Item | Content |
|------|---------|
| Kind | function ×2 (pure) |
| Purpose | Daily-bucket alignment for metric adapters. |
| Preconditions | Aware datetimes, else `ConfigError`. |
| Postconditions | Results at midnight UTC. |
| Invariants | — |
| Algorithm | As described. |
| Side effects | None. |
| Errors | Naive → `ConfigError`. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT01-79 |

#### U01-82 herness.connectors.monitoring.prometheus.PrometheusAdapter

| Member | Behavior |
|--------|----------|
| `tool` | `"prometheus"` (Prometheus and Mimir) |
| `check()` | `GET /api/v1/query?query=vector(1)` |
| `events(since, until)` | Yields nothing: there is no alert history API; alert load arrives as the metric `alert_firing_minutes` (design 01 §5.9) |
| `daily_metrics(since, until)` | Below |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | Daily aggregates from `query_range` with `step=1d` (design 01 §5.9). |
| Preconditions | Every query aggregates to one daily value per `service_label` (config responsibility). |
| Postconditions | One row per (query, service, complete day). |
| Invariants | Never requests a step other than 86,400 s. |
| Algorithm | 1. `(a, b) = complete_days(since, until)`; the point at timestamp `T` covers the day ending at `T`, so request `start = a + 1 day`, `end = b`; if `start > end` → return. 2. Split `[start, end]` into chunks of ≤ 10,000 days (below the 11,000-points-per-series limit). 3. Per query and chunk: `GET /api/v1/query_range` with `query`, `start` and `end` (epoch seconds), `step=86400`; header `X-Scope-OrgID: <tenant>` when `tenant` is set (Mimir; the `/prometheus` prefix is part of `base_url`). 4. Body must have `status == "success"` and `data.resultType == "matrix"`, else `SchemaViolation`. 5. Per series: `service = metric[service_label]` (absent → `SchemaViolation("series without service label")`); per `[t, v]`: `date = (UTC(t) − 1 day).date()`; `value = float(v)`; `payload = json.dumps({"query": name, "metric": metric, "t": t, "v": v})`. 6. Emit `metric_batch` chunks of `batch_rows`. |
| Side effects | Network reads: one request per query per chunk. |
| Errors | §6; shape → `SchemaViolation`. |
| Concurrency | One instance per thread. |
| Complexity and limits | 3 years × 50 metrics < 10 min (design 01 §8). |
| Security notes | TH01-04. |
| Tests | UT01-80, UT01-09 |

#### U01-83 herness.connectors.monitoring.datadog.DatadogAdapter

| Member | Behavior |
|--------|----------|
| `tool` | `"datadog"` |
| `check()` | `GET /api/v1/validate` |
| `events(since, until)` | Below; nothing when `event_query` is `None` |
| `daily_metrics(since, until)` | Below |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | Datadog events search and daily rollups (design 01 §5.9). |
| Preconditions | Auth headers `DD-API-KEY` and `DD-APPLICATION-KEY` (U01-63). |
| Postconditions | — |
| Invariants | Cursor pagination only. |
| Algorithm | Events: 1. `POST /api/v2/events/search` with `{"filter": {"query": event_query, "from": <since ISO>, "to": <until ISO>}, "sort": "timestamp", "page": {"limit": page_size}}`; later pages add `"page": {"limit", "cursor": <meta.page.after>}`. 2. Stop when `data` is empty or `meta.page.after` is absent; `CursorGuard.step(after)`. 3. Per item: `event_key = id` (non-empty string, else `SchemaViolation`); `ts = parse_source_timestamp(attributes.timestamp)`; `inner = attributes.attributes` (object or `{}`); `service = inner.service`, else the value of the first tag `service:<v>` in `attributes.tags`; `host = inner.host`; `severity_raw = inner.priority`; `status = inner.status`; `title = inner.title`; `dedup_key = inner.aggregation_key`; `end_ts`, `incident_ref` = `None`; `payload = json.dumps(item)`. Metrics: 1. `(a, b) = complete_days(...)`; empty → return. 2. Per query: `GET /api/v1/query` with `from = a` and `to = b − 1 s` (epoch seconds) and `query = f"{query}.rollup({agg}, 86400)"`. 3. `status == "error"` → `SchemaViolation`. 4. Per series: `service` = the `tag_set` entry prefixed `f"{service_label}:"` (absent → `SchemaViolation`); per `[ms, v]` in `pointlist`: `date = UTC(ms / 1000).date()`, kept only when `a ≤ date < b`. 429 → `RateLimited` using `X-RateLimit-Reset` (U01-61). |
| Side effects | Network reads. |
| Errors | §6. |
| Concurrency | One instance per thread. |
| Complexity and limits | 500 events/s target (design 01 §8). |
| Security notes | TH01-04; field mapping verified in Phase 6 (§13 V-4). |
| Tests | UT01-81 |

#### U01-84 herness.connectors.monitoring.splunk.SplunkAdapter

| Member | Behavior |
|--------|----------|
| `tool` | `"splunk"` |
| `check()` | `GET /services/server/info?output_mode=json` |
| `events(since, until)` | Below; nothing when `event_query` is `None` |
| `daily_metrics(since, until)` | Below |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | Splunk export search, streamed one JSON result per line (design 01 §5.9). |
| Preconditions | SPL validated by `validate_spl` (U01-09). |
| Postconditions | — |
| Invariants | Only aggregate or table-shaped searches run. |
| Algorithm | 1. `search = spl` if it starts with `\|`, else `"search " + spl`. 2. `http.post_form_lines("/services/search/v2/jobs/export", data={"search": search, "earliest_time": <epoch s>, "latest_time": <epoch s>, "output_mode": "json"})`. 3. Per line object: a `messages` entry of type `ERROR` or `FATAL` → `SchemaViolation("splunk search error")`; skip lines with `preview == true` or without `result`. Events: `result.event_key` required; `ts` from `result._time` (numeric → epoch s; else ISO-8601); optional `service`, `host`, `severity_raw`, `title`, `status`, `dedup_key`, `end_ts` (same parsing), `incident_ref`; `payload = json.dumps(result)`. Metrics: per query with `earliest = a`, `latest = b` from `complete_days`; `date = UTC(_time).date()` kept when `a ≤ date < b`; `service = result[service_label]` (absent → `SchemaViolation`); `value = float(result[value_field])`. |
| Side effects | One streamed request per query. |
| Errors | §6; `SchemaViolation` for lines. |
| Concurrency | One instance per thread. |
| Complexity and limits | Line ≤ 1 MiB; 2,000 rows/s target. |
| Security notes | TH01-07, TH01-04. |
| Tests | UT01-82, UT01-08 |

#### U01-85 herness.connectors.monitoring.dynatrace.DynatraceAdapter

| Member | Behavior |
|--------|----------|
| `tool` | `"dynatrace"` |
| `check()` | `GET /api/v2/problems?pageSize=1&from=now-5m` |
| `events(since, until)` | Below |
| `daily_metrics(since, until)` | Below |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | Dynatrace problems and daily metrics (design 01 §5.9). |
| Preconditions | Header `Authorization: Api-Token <token>` (U01-63). |
| Postconditions | Problems re-fetched across the overlap land as new versions (OPEN → CLOSED). |
| Invariants | Later problem pages send `nextPageKey` alone. |
| Algorithm | Events: 1. First call `GET /api/v2/problems` with `from`, `to` (epoch ms), `pageSize=page_size`, and `problemSelector=event_query` when set. 2. Later calls send only `nextPageKey`; stop when it is absent or `null`; `CursorGuard.step(key)`. 3. Per problem: `event_key = problemId`; `ts = startTime` (ms); `end_ts = None` when `endTime` is `-1` or absent, else `endTime`; `title`; `status`; `severity_raw = severityLevel`; `service = rootCauseEntity.name`, else the first `affectedEntities[].name`, else `None`; `host = None`; `dedup_key = displayId`; `incident_ref = None`. Metrics: per query: `GET /api/v2/metrics/query` with `metricSelector=query`, `resolution=1d`, `from = a`, `to = b` (epoch ms); a non-null `nextPageKey` → `SchemaViolation("unexpected pagination")`; per `result[].data[]`: `service = dimensionMap[service_dimension]` (absent → `SchemaViolation`); per `(timestamp, value)`: bucket date = `UTC(timestamp).date() − 1 day` (the timestamp marks the end of the slot; §13 V-5), kept when `a ≤ date < b`; `null` values → `None`. |
| Side effects | Network reads. |
| Errors | §6. |
| Concurrency | One instance per thread. |
| Complexity and limits | 500 events/s; metrics one request per query. |
| Security notes | TH01-04. |
| Tests | UT01-83 |

### 3.17 MongoDB (`herness/connectors/mongodb.py`)

#### U01-86 herness.connectors.mongodb.MongoConnector

Constructor `MongoConnector(settings: MongoSettings, *, clock=now_utc, client_factory: Callable[[str], pymongo.MongoClient] | None = None)`; decorated `register("connector", "mongodb")`. `client_factory` defaults to building `pymongo.MongoClient(uri, tz_aware=True, tzinfo=UTC, readPreference="secondaryPreferred", connectTimeoutMS=10000, serverSelectionTimeoutMS=timeout_s × 1000, socketTimeoutMS=timeout_s × 1000, appname="herness", retryReads=False)`; tests pass a `mongomock` factory.

| Member | Behavior |
|--------|----------|
| `name`, `entities` | `"mongodb"`, configured names |
| `watermark_field(entity)` | entity `updated_field` |
| `check()` | `db.command("ping")`; per entity, if no index in `list_indexes()` has `updated_field` as its first key → log `connectors.mongodb.index_missing` (WARNING: `entity`, `field`) |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | MongoDB connector (design 01 §5.9). |
| Preconditions | The URI secret starts with `mongodb://` or `mongodb+srv://`; for non-`+srv` URIs whose hosts are not all loopback it sets `tls=true` (or `ssl=true`); it never sets `tlsInsecure`, `tlsAllowInvalidCertificates` or `tlsAllowInvalidHostnames` to true. Violations → `ConfigError("MongoDB URI must enable verified TLS")` (the URI is never in the message). Every host the URI names (for `+srv`, the SRV name) is in `settings.hosts` (R-06), checked before `client_factory` is called; a missing host → `ConfigError("MongoDB URI names a host not listed in sources.mongodb.hosts")` (the host is not in the message, because it comes from a secret). SRV targets are enforced by the socket guard, which allows only the listed hosts. |
| Postconditions | — |
| Invariants | `key_field` values are unique in the collection (config responsibility). |
| Algorithm | Lazy client on first use; `_map_mongo_error(exc)`: `AutoReconnect`, `NetworkTimeout`, `ServerSelectionTimeoutError`, `ConnectionFailure`, `ExecutionTimeout` → `SourceUnavailable`; `OperationFailure` with code 13 or 18 → `AuthError`; other `OperationFailure` → `SchemaViolation("mongodb operation failed", code)`; `ConfigurationError`, `InvalidURI` → `ConfigError`; other `PyMongoError` → `SourceUnavailable`. |
| Side effects | Network reads. |
| Errors | As mapped. |
| Concurrency | `MongoClient` is thread-safe; one connector per thread. |
| Complexity and limits | — |
| Security notes | TH01-01, TH01-03 (URI resolved at use, never logged), TH01-18 (URI hosts must be listed in `hosts`). |
| Tests | UT01-85, UT01-94, ST01-17 |

#### U01-87 herness.connectors.mongodb.MongoConnector.sync, list_keys

| Item | Content |
|------|---------|
| Kind | method ×2 |
| Purpose | Bounded, ordered, key-set paged reads; key-only listing. |
| Preconditions | — |
| Postconditions | Ascending by `(updated_field, key_field)`; `_source_key = str(doc[key_field])`. |
| Invariants | The last `(updated, key)` pair stays in the generator, so a retried page resumes after it (design 01 §6). |
| Algorithm | `sync`: 1. `base = {**filter}` plus `{updated_field: {"$gte": since, "$lt": until}}` (omit an absent bound). 2. `last = None`; loop: `q = base` when `last is None`, else `{"$and": [base, {"$or": [{updated: {"$gt": last_ts}}, {updated: last_ts, key: {"$gt": last_key}}]}]}`; `projection = {f: 1 for f in fields + [updated, key]}`; `docs = retry_page(lambda: list(coll.find(q, projection).sort([(updated, 1), (key, 1)]).limit(page_size).max_time_ms(max_time_ms)), source="mongodb")` with `fault_point("http.page", source="mongodb")` and `_map_mongo_error` inside the lambda. 3. Per doc: key present (else `SchemaViolation("missing key field")`); `ts` = the aware `datetime` (else `parse_source_timestamp`); `payload = bson.json_util.dumps(doc, json_options=RELAXED_JSON_OPTIONS)`; fields = `flatten_record(json.loads(payload), fields=fields)` (so `_id` becomes column `f_id`). 4. Stop when `len(docs) < page_size`. `list_keys`: key-set pages over `{**filter, key: {"$gt": last_key}}` with projection `{key: 1}`, sort `key` ascending. |
| Side effects | Network reads. |
| Errors | As U01-86. |
| Concurrency | One instance per thread. |
| Complexity and limits | One page in memory; 20,000 docs/s target. |
| Security notes | TH01-05, TH01-07. |
| Tests | UT01-84 |

### 3.18 Snowflake (`herness/connectors/snowflake.py`)

#### U01-88 herness.connectors.snowflake.SnowflakeConnector

Constructor `SnowflakeConnector(settings: SnowflakeSettings, *, clock=now_utc, connect: Callable[..., SnowflakeConnection] | None = None)` (default `snowflake.connector.connect`); decorated `register("connector", "snowflake")`.

| Member | Behavior |
|--------|----------|
| `name`, `entities` | `"snowflake"`, configured names |
| `watermark_field(entity)` | entity `updated_field` |
| `check()` | `SHOW WAREHOUSES LIKE %(wh)s`; the `resource_monitor` column empty, `null` or absent → `ConfigError("warehouse has no resource monitor")` |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | Snowflake connector with cost guard (design 01 §5.9). |
| Preconditions | — |
| Postconditions | — |
| Invariants | Identifiers are upper-cased and double-quoted (ENG §3.5); values are bound parameters. |
| Algorithm | 1. Lazy connection: `cred = resolve_json(auth.credentials)`; private key PEM loaded with `cryptography.hazmat.primitives.serialization.load_pem_private_key` (passphrase when present) and passed as DER PKCS#8 bytes; `connect(account=..., user=cred.user, private_key=der, warehouse=..., role=..., session_parameters={"STATEMENT_TIMEOUT_IN_SECONDS": statement_timeout_s, "TIMEZONE": "UTC"}, login_timeout=timeout_s, network_timeout=timeout_s, application="herness", ocsp_fail_open=True)`. 2. `_scan_guard(sql, params)`: `EXPLAIN USING JSON <sql>` with the same parameters; parse the JSON; `GlobalStats.bytesAssigned` absent → `SchemaViolation`; `> max_scan_gb × 2^30` → `ConfigError(f"scan guard: {gb:.1f} GB exceeds max_scan_gb")`. 3. `_map_sf_error(exc)`: `DatabaseError` with `sqlstate == "28000"` → `AuthError`; `OperationalError` or `InterfaceError` (network) → `SourceUnavailable`; `ProgrammingError` with `sqlstate == "57014"` (statement canceled, including queue timeout) → `RateLimited(retry_after=None)`; other `ProgrammingError` → `ConfigError("snowflake query rejected")` (§13 V-6). |
| Side effects | Network; warehouse credits. |
| Errors | As mapped. |
| Concurrency | One connection per connector instance; one instance per thread. |
| Complexity and limits | — |
| Security notes | TH01-17, TH01-03, TH01-07, TH01-18 (the account host must be listed in `hosts`, U01-11; the connector opens its own network session, R-06). |
| Tests | UT01-86, UT01-87, UT01-88, ST01-16, UT01-94 |

#### U01-89 herness.connectors.snowflake.SnowflakeConnector.sync, list_keys

| Item | Content |
|------|---------|
| Kind | method ×2 |
| Purpose | Bounded ordered SELECT read as Arrow batches; key-only listing. |
| Preconditions | — |
| Postconditions | Arrow types kept for field columns (design 01 §4.1); names `to_snake`. |
| Invariants | `QUERY_TAG = 'herness:<entity>'` for every statement of the entity. |
| Algorithm | `sync`: 1. `cur.execute("ALTER SESSION SET QUERY_TAG = %(tag)s", {"tag": f"herness:{entity}"})`. 2. `sql = SELECT <cols> FROM <db>.<schema>.<table> WHERE <upd> >= %(since)s AND <upd> < %(until)s [AND (<filter>)] ORDER BY <upd>, <key>` (lower bound omitted when `since` is `None`); `since`/`until` bound as naive UTC datetimes (session `TIMEZONE = 'UTC'`). 3. `_scan_guard(sql, params)`. 4. `retry_page(lambda: cur.execute(sql, params), source="snowflake")`. 5. For each table from `cur.fetch_arrow_batches()`, for each record batch of ≤ `batch_rows` (`to_batches(max_chunksize=batch_rows)`): rename with `to_snake`; `_source_key` = key column cast to string (null → `SchemaViolation`); `_source_updated_at = parse_arrow_timestamps(updated column)`; `_payload` = per-row `json.dumps(row, default=str)` (Decimal as string); add the other metadata columns. Errors during iteration → `_map_sf_error` (the query restarts on the job retry; Arrow result pages cannot resume). `list_keys`: `SELECT <key> FROM <table> [WHERE (<filter>)] ORDER BY <key>`, cast to string, `KEY_SCHEMA` batches. |
| Side effects | Network; credits. |
| Errors | As U01-88. |
| Concurrency | One instance per thread. |
| Complexity and limits | 100,000 rows/s target; memory one Arrow result chunk. |
| Security notes | TH01-17. |
| Tests | UT01-86, UT01-87 |

### 3.19 Dataverse (`herness/connectors/dataverse.py`)

#### U01-90 herness.connectors.dataverse.DataverseConnector

Constructor `DataverseConnector(settings: DataverseSettings, *, http: SourceHttp | None = None, clock=now_utc)`; decorated `register("connector", "dataverse")`; default `http` uses `MsalTokenProvider(scope=f"{base_url}/.default")`.

| Member | Behavior |
|--------|----------|
| `name`, `entities` | `"dataverse"`, configured names |
| `watermark_field(entity)` | entity `updated_field` |
| `check()` | `GET /api/data/v9.2/WhoAmI` |

| Item | Content |
|------|---------|
| Kind | class |
| Purpose | Dataverse Web API connector (design 01 §5.9). |
| Preconditions | — |
| Postconditions | — |
| Invariants | Never uses `$skip` or `$top`; `@odata.nextLink` is followed unmodified. Change tracking (Q4) is not used; deletes come from weekly reconciliation. |
| Algorithm | See U01-91. |
| Side effects | Network reads. |
| Errors | §6. |
| Concurrency | One instance per thread. |
| Complexity and limits | — |
| Security notes | TH01-02, TH01-16. |
| Tests | UT01-90, UT01-94 |

#### U01-91 herness.connectors.dataverse.DataverseConnector.sync, list_keys

| Item | Content |
|------|---------|
| Kind | method ×2 |
| Purpose | OData paged read with formatted values; key-only listing. |
| Preconditions | — |
| Postconditions | Ascending by `(updated_field, key_field)`; each selected field `f` gives columns `f` and `f_display`. |
| Invariants | Header `Prefer` is sent on every page. |
| Algorithm | `sync`: 1. `url = f"/api/data/v9.2/{entityset}"`; `select` = key, updated, then the configured fields (deduplicated). 2. Params: `$select`; `$filter` = `"<upd> ge <since>"` and/or `"<upd> lt <until>"` joined with `" and "` (timestamps `%Y-%m-%dT%H:%M:%SZ`, floored to seconds); `$orderby = f"{upd} asc,{key} asc"`. 3. Headers: `Prefer: odata.maxpagesize=<page_size>,odata.include-annotations="OData.Community.Display.V1.FormattedValue"`, `OData-MaxVersion: 4.0`, `OData-Version: 4.0`. 4. Loop: `page = http.get_json(url, params=params, headers=headers)`; body must be an object with a list `value`; per row: key string non-empty (else `SchemaViolation`); `ts = parse_source_timestamp(row[upd])`; `payload = json.dumps(row)`; fields: for each selected `f`: `text(row.get(f))` and `f_display = row.get(f + "@OData.Community.Display.V1.FormattedValue")`; `next = body.get("@odata.nextLink")`: present → `url = http.check_next_url(next)`, `params = None`, `CursorGuard.step(next)`; absent → stop. `list_keys`: same paging with `$select=<key>` only, no `$filter`/`$orderby`. 429 carries `Retry-After` seconds (U01-60, U01-61). |
| Side effects | Network reads. |
| Errors | §6. |
| Concurrency | One instance per thread. |
| Complexity and limits | ≤ 5,000 rows per page; 1,500 rows/s target; service protection limits of design 01 §5.9 are enforced by the source and surface as `RateLimited`. |
| Security notes | TH01-02. |
| Tests | UT01-89, UT01-91 |

---

## 4. State and data

### 4.1 Ops-store tables owned by this component (spec 02 §5.1)

The tables are created by impl 02 migration 001 (X:02/herness/store/migrations/001_ingestion_health.sql), which R-11 makes the creator of every table named in the design specs. This section states the column types and constraints this component relies on; migration 001 has all of them, so this spec adds no migration and its range 010–019 (R-11) stays unused (§13 O-1). Writes go only through the ingest area `herness/store/ops/ingest.py` (R-08).

**`watermark`**

| Column | Type | Null | Constraint | Meaning |
|--------|------|------|------------|---------|
| `source` | TEXT | no | PK part | Connector name, or `monitoring:<tool>` |
| `entity` | TEXT | no | PK part | Entity |
| `field` | TEXT | no | — | Source watermark field (`sys_updated_on`, `updated`, `ts`, `date`, configured field) |
| `value` | TEXT | no | fixed-width UTC (spec 00 §8) | Watermark value |
| `updated_at` | TEXT | no | fixed-width UTC | Last change |

PK (`source`, `entity`). Write: `set_watermark` upsert, monotonic, idempotency key (`source`, `entity`); one statement per transaction.

**`sync_slice`**

| Column | Type | Null | Constraint | Meaning |
|--------|------|------|------------|---------|
| `source` | TEXT | no | PK part | Stream key |
| `entity` | TEXT | no | PK part | Entity |
| `slice_start` | TEXT | no | PK part, fixed-width | Inclusive start |
| `slice_end` | TEXT | no | fixed-width, > `slice_start` | Exclusive end |
| `status` | TEXT | no | CHECK in (`pending`, `running`, `done`, `failed`) | State |
| `rows` | INTEGER | no | ≥ 0, default 0 | Rows committed by the last successful attempt |
| `files` | TEXT (JSON array) | no | default `'[]'` | Committed lake files, POSIX relative to `paths.data` |
| `attempts` | INTEGER | no | ≥ 0, default 0 | Starts of this slice |
| `last_error` | TEXT | yes | ≤ 500 chars | `ErrorClass: message` |
| `updated_at` | TEXT | no | fixed-width | Last change |

PK (`source`, `entity`, `slice_start`). Writes: `ensure_slices` (one transaction for the plan), `mark_slice_*` (one statement each). Idempotency key: PK.

**`file_ingest`**

| Column | Type | Null | Constraint | Meaning |
|--------|------|------|------------|---------|
| `fingerprint` | TEXT | no | PK; 64 lower-case hex | SHA-256 of the file bytes |
| `source` | TEXT | no | `'files'` | Connector |
| `entity` | TEXT | no | — | Entity |
| `path` | TEXT | no | POSIX relative to inbox root | Path at ingest time |
| `size_bytes` | INTEGER | no | ≥ 0 | Size |
| `mtime` | TEXT | no | fixed-width | File mtime |
| `rows` | INTEGER | no | ≥ 0 | Rows written |
| `files` | TEXT (JSON array) | no | — | Lake files written |
| `ingested_at` | TEXT | no | fixed-width | Record time |

Write: `record_file_ingest` (`INSERT OR IGNORE`), idempotency key `fingerprint`, written only after the lake commit.

**Read only:** `deletion_request` (owner 10: `record_id`, `status`; read by U01-29, §13 O-10), `source_health` (owner 08, through X:08 `breaker`).

Retention: rows are kept indefinitely (small: one row per stream, slice and inbox file). Spec 10 purge does not touch them.

### 4.2 Files

| Path | Written by | Lifetime | Notes |
|------|-----------|----------|-------|
| `data/raw/<source>/<entity>/dt=YYYY-MM-DD/part-<ulid>.parquet` | X:02 `LakeWriter` on behalf of the runner | Retention per spec 10 (`raw_lake_months`) | Append-only; one schema per file |
| `data/raw/<source>/.../.<name>.parquet.tmp-<ulid>` | `LakeWriter` | Until commit/abort; orphans deleted after 1 h (U01-34) | Never read by the build; not committed lake files, so their removal does not breach the append-only rule of R-57 |
| `data/tmp/reconcile/<source>-<entity>-<ulid>.parquet` | U01-44 | Deleted in `finally` of the run | Key list for the anti-join; DuckDB spill files in the same folder |
| `data/inbox/<entity>/*` | people and systems | Never moved or deleted by this component | Read only |

### 4.3 In-memory state

| State | Owner | Model | Bound |
|-------|-------|-------|-------|
| Deletion set (`pa.StringArray`) | `DeletionFilter`, per stream | Reference swapped under a lock | ≈ 4 MB per 100k ids |
| OAuth / MSAL token | `OAuthTokenAuth`, `MsalTokenProvider` | Lock-protected | One token |
| `ChangelogState.bulk_available` | per `JiraConnector` | Single thread | — |
| ServiceNow audit-delete readability | per `ServiceNowConnector` | Single thread | — |
| Handler registration flag | `jobs` module | Main thread at start-up; reset by the registry fixture | — |

### 4.4 Transaction boundaries

The lake commit and the ops write are separate stores and are never atomic together. Ordering makes a crash safe: lake `commit()` → `fault_point("connector.before_watermark")` → `set_watermark` / `mark_slice_done` / `record_file_ingest`. A crash between them re-fetches data whose duplicates collapse in staging dedupe (spec 02 §4.2).

### 4.5 Jira raw column-name contract (R-59)

This spec owns the column names and encodings of lake entity `jira/issue`. The connector writes them (U01-73 through U01-93), impl 02 staging `120_stg_jira.sql` reads them, and the impl 11 generator writes them by calling `flatten_issue` (U01-93). A change to this table is a contract change: it needs the same change in X:02 staging and a new fixture from X:11 before merge.

| Column | Arrow type | Null | Source | Encoding |
|--------|-----------|------|--------|----------|
| 8 metadata columns | `METADATA_SCHEMA` (U01-19) | per U01-19 | — | `_source = "jira"`, `_entity = "issue"`, `_source_key` = `id`, `_record_id = "jira:issue:<id>"`, `_source_updated_at` = parsed `fields.updated` (U01-24), `_payload` = the whole issue as compact JSON |
| `id` | string | no | `issue.id` | digits as text |
| `key` | string | no | `issue.key` | as returned (`PROJ-123`) |
| `issuetype`, `parent`, `project`, `status` | string | yes | `fields.<name>` | compact JSON text of the object |
| `components`, `labels`, `issuelinks` | string | yes | `fields.<name>` | compact JSON text of the array |
| `created`, `resolutiondate`, `updated` | string | yes (`updated` no) | `fields.<name>` | the source timestamp string unchanged (for example `2024-05-01T10:00:00.000+0000`); staging parses it |
| `summary` | string | yes | `fields.summary` | text |
| `description` | string | yes | `fields.description` | Cloud: compact JSON text of the Atlassian document; Data Center: wiki text |
| `customfield_<n>` (one per configured id, `mappings.custom_fields.jira.*`) | string | yes | `fields.customfield_<n>` | U01-23 text rules: number as text, object or array as compact JSON |
| `changelog` | string | no | complete histories (U01-76) | compact JSON array of `project_history` values (U01-94), ascending by `(created, id)`; `[]` when none |
| `remotelinks` | string | yes | remote links (U01-77) | compact JSON array of `project_remote_link` values (U01-94); NULL when `fetch_remote_links` is false |

Column order in each batch: metadata columns, then `JIRA_ISSUE_COLUMNS` (U01-71: `id`, `key`, the twelve `JIRA_FIELDS` in their tuple order, `changelog`, `remotelinks`), then custom fields in `mappings.custom_fields.jira` order (`story_points`, `team`, `estimate_cost_usd`, `epic_link`). Readers select columns by name; the order is fixed only so that files are comparable. Tombstone rows carry the metadata columns with every contract column NULL.

---

## 5. Control flows

### F01-01 Incremental sync (`herness sync SOURCE` / scheduled `sync` job)

| # | Step | Unit | State changed | On failure |
|---|------|------|---------------|------------|
| 1 | Validate payload, build connector and runner | U01-51, U01-55, U01-37 | — | `ConfigError` → job `failed` |
| 2 | Delete orphan temp files > 1 h | U01-34 | lake temp files | per-file OS error logged, flow continues |
| 3 | `guard(key)` | X:08 `guard` | — | `CircuitOpen` → source skipped, job `done` with outcome `skipped_open_circuit`; the next scheduled run retries (R-39) |
| 4 | Read watermark; none → F01-02 with `start = backfill.start`, `end = now` | U01-27 | — | `StoreBusy` → job retry |
| 5 | `since = wm − overlap`, `until = now − settle`; `since ≥ until` → zero result | U01-39 | — | — |
| 6 | Load deletion set | U01-33 | memory | `StoreBusy` → job retry |
| 7 | Fetch pages (page retry by `retry_page`) | connector `sync`, U01-59 | — | Retryable exhausted → writer aborted, watermark unchanged, job rescheduled; `AuthError` → breaker forced open, job `failed`; `SchemaViolation` → entity failed, others continue |
| 8 | Filter deleted ids; detect drift (drift → checkpoint, new writer) | U01-33, U01-35, U01-40 | — | — |
| 9 | `LakeWriter.write` | X:02 | temp files | writer aborted, error propagates |
| 10 | Checkpoint at `checkpoint_rows` or 600 s: `commit()`, fault point, `set_watermark` (ordered streams), reload deletion set, new writer | U01-40, U01-28 | lake files, `watermark` | crash after commit, before watermark → rerun re-fetches (superset, FT01-01) |
| 11 | End: final commit; unordered streams set watermark once | U01-40 | lake files, `watermark` | as step 10 |
| 12 | Log `connectors.sync.completed`, metrics, `SyncResult` | U01-39 | logs, `metric_sample` | — |

Monitoring: steps 3–12 run once per tool with key `monitoring:<tool>`; a tool failure does not stop the other tools (U01-38 step 3).

### F01-02 Backfill (`herness sync SOURCE --backfill --from D --to D`, `--full` = from `backfill.start` to now (R-63), or first sync)

| # | Step | Unit | State changed | On failure |
|---|------|------|---------------|------------|
| 1 | Split `[start, end)` into slices of `slice_days` | U01-21 | — | `ConfigError` (range) |
| 2 | `ensure_slices` | U01-30 | `sync_slice` | `StoreBusy` → job retry |
| 3 | For each non-`done` slice, in ≤ `max_concurrency` threads: stop check → `mark_slice_running` → `guard` → deletion set → `_write_stream(advance_watermark=False)` → `mark_slice_done` | U01-43, U01-31, U01-40 | `sync_slice`, lake | slice → `failed` with `last_error`; other slices continue; first error re-raised after all finish |
| 4 | Stop requested → remaining slices stay; job yields | U01-43 | — | — |
| 5 | All slices `done` → `set_watermark(min(end, max committed))` or `end` | U01-28 | `watermark` | — |
| 6 | Restart: `done` skipped; `running`/`failed` rerun from their start | U01-30 | — | re-fetched rows harmless |

### F01-03 Reconciliation (weekly `reconcile` job, Sunday 03:00)

| # | Step | Unit | State changed | On failure |
|---|------|------|---------------|------------|
| 1 | `guard(source)`; load deletion set | U01-44 | — | `CircuitOpen` → skipped |
| 2 | Stream `list_keys` to `data/tmp/reconcile/*.parquet` | U01-17, U01-44 | scratch file | error → scratch deleted, nothing written |
| 3 | DuckDB anti-join of latest live lake keys (minus deletion set) against source keys | U01-45 | — | `SchemaViolation("reconcile query failed")` |
| 4 | Safety valve: missing > `max_delete_pct` % of live → log `connectors.reconcile.aborted`, raise | U01-44 | — | job `failed`; nothing written |
| 5 | Write tombstones (detection time), commit | U01-26, X:02 | lake | writer aborted |
| 6 | Delete scratch file; log completion | U01-44 | scratch removed | — |

### F01-04 Files ingest

| # | Step | Unit | State changed | On failure |
|---|------|------|---------------|------------|
| 1 | List candidates (symlink, containment, size, settle checks) | U01-47 | — | rejected files logged and skipped |
| 2 | SHA-256 fingerprint; changed during read → skip | U01-48 | — | skip this run |
| 3 | Known fingerprint → skip | U01-32 | — | — |
| 4 | Read via DuckDB, filter deleted, write, commit | U01-49, U01-40 | lake | `SchemaViolation` → file not recorded, retried next run |
| 5 | Fault point, `record_file_ingest` | U01-32 | `file_ingest` | crash before record → file re-ingested next run; staging dedupe collapses rows (FT01-02) |
| 6 | Snapshot mode → F01-03 steps 2–6 with the file's keys and `deleted_at = file mtime` | U01-44 | lake | valve error → job `failed`; file already recorded |

### F01-05 Deletion request during a sync

| # | Step | Unit | State changed | On failure |
|---|------|------|---------------|------------|
| 1 | Spec 10 sets a request to `running` | X:10 | `deletion_request` | — |
| 2 | Current run reloads the set at its next checkpoint; later batches drop the id | U01-33, U01-40 | — | — |
| 3 | Rows committed before the reload are removed by spec 10's second lake pass | X:10 deletion procedure step 7 | lake | — |

### F01-06 Mapping check and field discovery (inline CLI)

| # | Step | Unit | State changed | On failure |
|---|------|------|---------------|------------|
| 1 | `herness sync SOURCE --check-mapping` → `check_mapping`; issues printed; X:09 exits 3 when any issue is found (R-46: validation found problems) | U01-56 | — | `ConfigError` on render or parse → exit 1 (R-46) |
| 2 | `herness sync jira --discover-fields` → `discover_fields`; candidates printed | U01-75 | — | §6 errors |

---

## 6. Error handling

| Failure condition | Class raised | Caught where | Retry / fallback | User-visible effect | Log event |
|-------------------|--------------|--------------|------------------|---------------------|-----------|
| Connect error, timeout, HTTP 408/5xx/529, Mongo network errors, Snowflake network error | `SourceUnavailable` | `retry_page` (X:08) per page; then job layer | Page retried per `source_http_page`; job rescheduled with backoff; breaker counts failures | Job shows retry / `failed` after `max_attempts` | `connectors.sync.failed` + X:08 `retry` |
| HTTP 429, Snowflake queue timeout (`57014`) | `RateLimited(retry_after)` | `retry_page`; job layer | Wait ≥ `retry_after` (capped 300 s); above cap → job rescheduled at `now + retry_after`; breaker not charged | Delayed sync | X:08 `retry` |
| Breaker open | `CircuitOpen` | `handle_sync` / U01-38 | No retry; stream skipped; the next scheduled run retries (R-39) | Job `done` with outcome `skipped_open_circuit` | `connectors.sync.skipped_open_circuit` |
| Request to a host other than `base_url`, or a non-`https` scheme to a non-loopback host; an SDK connecting to a host not in `sources.<name>.hosts` | `EgressBlocked` (raised by X:10 egress-built transport or socket guard) | job layer | None; watermark unchanged | Entity failed | `connectors.sync.failed` |
| MongoDB URI names a host missing from `sources.mongodb.hosts` | `ConfigError` | job layer | None | Entity failed; operator adds the host | `connectors.sync.failed` |
| HTTP 401/403, MSAL error, bad key pair, Mongo code 13/18, Snowflake `28000` | `AuthError` | job layer (X:08 forces the breaker open) | None | Job `failed`; doctor shows breaker open | `connectors.sync.failed` |
| Missing key or watermark field, unparseable timestamp, bad response shape, repeated cursor, response > 64 MiB, redirect, foreign next-URL host | `SchemaViolation` (`ForeignHostError`) | job layer | None; watermark unchanged | Entity failed; other entities continue | `connectors.sync.failed` |
| Reconcile safety valve | `SchemaViolation` | job layer | None | Reconcile job `failed`; nothing written | `connectors.reconcile.aborted` |
| HTTP 404 | `SourceNotFound` | Jira bulk changelog (fallback); elsewhere job layer | Jira: per-issue fallback | None / entity failed | `connectors.jira.bulk_changelog_unavailable` |
| HTTP 400, bad config, forbidden SPL, scan guard, unresolved `secret:`, invalid payload | `ConfigError` | loader or job layer | None | `herness config validate` or job `failed` | `connectors.sync.failed` |
| Inbox file changed during read | `InboxFileChanged` | `ingest_files` | Skipped; retried next run | File ingested later | `connectors.files.rejected` |
| Unreadable inbox file | `SchemaViolation` | job layer via `ingest_files` | File not recorded; retried next run | Entity failed | `connectors.files.rejected` |
| `LakeWriter.abort()` fails | original error | `_write_stream` | Orphan cleanup removes leftovers after 1 h | — | `connectors.lake.abort_failed` |
| Ops store busy | `StoreBusy` | X:02 `sqlite_write` policy, then job layer | Retry | Delayed sync | X:08 `retry` |

Rules: no `except Exception` in this component except the per-entity loop in `handle_sync`/`handle_reconcile`, which catches `HernessError` only. Foreign exceptions (`httpx`, `pymongo`, `snowflake`, `duckdb`, `msal`) are converted at the call site listed in the unit specs.

---

## 7. Security

### 7.1 (a) Trust boundaries

TB1 (source systems → connectors), TB2 (inbox drops → files connector), TB10 (operator → `sources.yaml`, CLI arguments, secrets). TB6 is not crossed: source connectors are not off-network model egress (spec 10 §3.5). Their HTTP clients are still built by `herness.core.egress` (R-06), and every host they or a vendor SDK reach must be in the socket guard allowlist (U01-15: `base_url` hosts plus `sources.<name>.hosts`).

### 7.2 (b) STRIDE threat table

| ID | Boundary | STRIDE | Threat | L | I | Control | Reference | Test |
|----|----------|--------|--------|---|---|---------|-----------|------|
| TH01-01 | TB1 | S | Man-in-the-middle impersonates a source endpoint | M | H | TLS verification always on; `verify` accepts only a CA bundle path; Mongo URI must enable verified TLS | ASVS v5.0.0-V12.3 | ST01-01 |
| TH01-02 | TB1 | T/I | Response-supplied next link (`Link`, `@odata.nextLink`) points to another host, leaking the bearer token (SSRF) | M | H | Host-guard transport in `http_client`; `check_next_url`; redirects not followed; `trust_env=False` | ASVS v5.0.0-V12.3, ASVS v5.0.0-V15 | ST01-02 |
| TH01-03 | TB1, TB10 | I | Secrets appear in config, logs, exceptions or lake files | M | H | `secret:` references only; `SecretStr` held in auth objects; masked `repr`; messages carry no URLs with queries, headers or URIs; spec 10 scrubber as last line | ASVS v5.0.0-V13.3, ASVS v5.0.0-V16 | ST01-03 |
| TH01-04 | TB1 | D | Oversized page, endless pagination or streamed line exhausts memory or time | M | M | 64 MiB page cap, 1 MiB line cap, `CursorGuard` (repeat and page count), timeouts per policy, daily aggregates only | ASVS v5.0.0-V4 | ST01-04 |
| TH01-05 | TB1 | T | Malformed or unexpected response writes wrong keys or timestamps and moves the watermark | M | H | Shape checks, strict timestamp parsing, `record_id` validation; any violation raises before commit; watermark unchanged | ASVS v5.0.0-V2.2 | ST01-05 |
| TH01-06 | TB1 | D | Huge `Retry-After` parks the job | L | M | X:08 `retry_after_cap_s` (300 s); above it the job is rescheduled | ASVS v5.0.0-V4 | ST01-06 |
| TH01-07 | TB10 | E/T | Config filters widen scope or run write/side-effect commands (ServiceNow `^NQ`, Mongo `$where`, SPL `\| delete`, Snowflake `;`, JQL `ORDER BY`) | L | H | Validators U01-07–U01-11; identifier allowlist and quoting; bound parameters | ASVS v5.0.0-V2.2, ASVS v5.0.0-V1 | ST01-07 |
| TH01-08 | TB2 | T/E | Inbox symlink, junction or glob escapes the entity folder | M | H | Pattern validation; symlink/junction skip; resolved-path containment | ASVS v5.0.0-V5.2 | ST01-08 |
| TH01-09 | TB2 | D | Very large or decompression-bomb file (XLSX, Parquet) exhausts memory | M | M | 1 GiB size check before reading; DuckDB `memory_limit` 1 GB; failure → `SchemaViolation`, file skipped | ASVS v5.0.0-V5.2 | ST01-09 |
| TH01-10 | TB2 | T | File replaced while fingerprinted or read, so the recorded fingerprint does not match the ingested rows | L | M | Size and `mtime_ns` re-checked after hashing and after reading; changed → skipped | ASVS v5.0.0-V5 | ST01-10 |
| TH01-11 | TB1 | I | Records under a deletion request are re-ingested | M | H | `DeletionFilter` before every write, reloaded at checkpoints; reconcile excludes them; spec 10 second pass | ASVS v5.0.0-V14 | ST01-11 |
| TH01-12 | TB2, TB1 | R | No record of what was ingested from where | L | M | `file_ingest` rows (fingerprint, path, rows, files); `SyncResult` in job results; INFO logs with counts | ASVS v5.0.0-V16 | ST01-12 |
| TH01-13 | TB1 | T/D | Partial or empty key listing (outage, permission change) tombstones most of an entity | M | H | Safety valve `max_delete_pct`; key listing raises instead of ending early | ASVS v5.0.0-V2.3 | ST01-13 |
| TH01-14 | TB1 | E | Over-privileged source identity would let a bug write to a source | L | H | Read-only grants (design 01 §9); connectors issue only GET, search POST and export POST; no write endpoints are coded | ASVS v5.0.0-V8 | ST01-14 |
| TH01-15 | TB1 | I | Connector code opens clients outside the egress-built constructor | L | M | Lint test: no `httpx.Client(`, `httpx.AsyncClient(`, `httpx.HTTPTransport(` or `httpx.AsyncHTTPTransport(` under `herness/connectors/` (R-06, ENG §2.1); `verify=False` nowhere; socket guard allowlist from U01-15 | ASVS v5.0.0-V15 | ST01-14 |
| TH01-18 | TB1, TB10 | I/T | A vendor SDK (`pymongo`, `snowflake.connector`, `msal`) connects to a host an attacker placed in the secret URI or that DNS resolves unexpectedly, sending credentials or reading from a rogue server | L | H | `sources.<name>.hosts` allowlist required for SDK sources (R-06); URI hosts checked against it before connecting (U01-86); Snowflake and MSAL hosts must be listed (U01-11, U01-12); nothing derived from `base_url`, `account` or the URI; socket guard enforces the list | ASVS v5.0.0-V13 | ST01-17 |
| TH01-16 | TB1 | S | Expired or leaked OAuth token reused; token logged | L | M | Expiry tracked; refresh 300 s early; one retry on 401; token never logged | ASVS v5.0.0-V9, ASVS v5.0.0-V10 | ST01-15 |
| TH01-17 | TB1, TB10 | D | Snowflake query scans far more than expected and burns credits | M | M | `EXPLAIN USING JSON` scan guard; statement timeout; resource monitor required by `check()` | ASVS v5.0.0-V2.3 | ST01-16 |

### 7.3 (c) ASVS mapping

| ASVS reference | Requirement area | Control in this spec | Test |
|----------------|------------------|----------------------|------|
| ASVS v5.0.0-V1 | Encoding and sanitization (injection) | Bound parameters (Snowflake, DuckDB paths); quoted allowlisted identifiers | ST01-07 |
| ASVS v5.0.0-V2.2 | Input validation | Pydantic config models; response shape checks; timestamp parsing | UT01-01–UT01-13, ST01-05 |
| ASVS v5.0.0-V2.3 | Business logic limits | Reconcile valve; scan guard; page and line caps | ST01-13, ST01-16 |
| ASVS v5.0.0-V4 | API and web service (outbound clients) | Timeouts, size limits, status mapping | ST01-04, ST01-06 |
| ASVS v5.0.0-V5.2 | File handling (content and size) | Size before read; suffix-selected readers; no execution | ST01-08, ST01-09 |
| ASVS v5.0.0-V8 | Authorization (least privilege) | Read-only identities; no write calls | ST01-14 |
| ASVS v5.0.0-V9 | Self-contained tokens | Expiry validated; never logged | ST01-15 |
| ASVS v5.0.0-V10 | OAuth client | Client-credentials flows; token endpoint on the source host | UT01-65 |
| ASVS v5.0.0-V12.3 | Service-to-service TLS | Verification always on | ST01-01 |
| ASVS v5.0.0-V13 | Configuration | Explicit `hosts` allowlist for SDK sources; secure defaults | ST01-17 |
| ASVS v5.0.0-V13.3 | Secret management | `secret:` refs; resolved at use | ST01-03 |
| ASVS v5.0.0-V14 | Data protection | Deletion filter; changelog `author` dropped | ST01-11 |
| ASVS v5.0.0-V15 | Secure coding and architecture | Single client constructor; layering; no unsafe deserialisation | ST01-14 |
| ASVS v5.0.0-V16 | Logging and error handling | No values in logs; ingest records; taxonomy errors | ST01-03, ST01-12 |

### 7.4 (d) LLM Top 10 and AI RMF

Not applicable: this component calls no model and builds no prompts. It stores untrusted source text (TB3 input for spec 03) unchanged in `_payload` and flattened columns; LLM01/LLM02 controls apply downstream (specs 03, 05, 10). Jira changelog `author` is dropped to reduce personal data reaching later stages.

### 7.5 (e) Secrets

| Secret (spec 10 §3.3 names) | Used by | Resolved | Held |
|-----------------------------|---------|----------|------|
| `servicenow_oauth` (or basic JSON) | ServiceNow auth | X:10 `resolve_json` at connector construction | `SecretStr` in `OAuthTokenAuth` |
| `jira_api_token` / DC PAT | Jira auth | `resolve_json` / `resolve` | `BasicAuth` / `StaticHeaderAuth` |
| `datadog_keys`, `mimir_read`, Splunk and Dynatrace tokens | Monitoring adapters | at adapter construction | header auth objects |
| MongoDB URI secret | `MongoConnector` | at first use | passed to `MongoClient`, not stored elsewhere |
| `snowflake_svc` | `SnowflakeConnector` | at first connect | DER key bytes in the connection only |
| `dataverse_app` | `MsalTokenProvider` | at construction | MSAL app |

A new job re-resolves secrets (worker builds connectors per job), so rotation takes effect on the next job (spec 10 §5.2).

### 7.6 (f) Data classification

| Field / artifact | Class |
|------------------|-------|
| `_payload`, flattened text fields (descriptions, summaries, names, `*_display`) | personal |
| `_record_id`, `_source_key`, `_source_updated_at`, `_fetched_at`, `_deleted` | internal |
| Monitoring metric values, event titles | confidential |
| `watermark.*`, `sync_slice.*` | internal |
| `file_ingest.path` (inbox file names) | internal |
| Scratch key files | internal |
| Log events and metrics (counts, keys like `monitoring:datadog`) | internal |
| Secret values | never stored or emitted |

### 7.7 (g) Accepted residual risks

| Risk | Reason | Owner |
|------|--------|-------|
| Source grants cannot be verified programmatically (TH01-14) | Sources expose no portable "my permissions" API; grants are checked at install by the admin (design 01 §9) | Deployment admin (spec 10 §9.3) |
| A record restored in the source with an older update time stays tombstoned (Q5) | Needs a spec 02 decision; default: no bump | Product owner, Phase 6 (V-7) |
| MongoDB `+srv` targets are resolved by DNS and must be listed in `sources.mongodb.hosts` by hand (R-06); a target added by the provider fails with `EgressBlocked` until the list is updated | URI lives in a secret; nothing is derived from it | Deployment admin |
| Snowflake OCSP responder hosts not listed in `sources.snowflake.hosts` make OCSP checks fail open (`ocsp_fail_open=True`) | Listing every CA responder is brittle; TLS verification still applies | Deployment admin |
| Rows committed between a request moving to `running` and the next checkpoint reach the lake | Removed by spec 10's second pass (Q11) | Spec 10 |

---

## 8. Observability

### 8.1 Log events (component `connectors`)

| Event | Level | Fields | Emitted when |
|-------|-------|--------|--------------|
| `connectors.sync.started` | INFO | `source`, `entity`, `stream`, `mode`, `since`, `until`, `job_id` | Start of a stream |
| `connectors.sync.checkpoint_committed` | INFO | `source`, `entity`, `stream`, `rows`, `files`, `watermark` | Each checkpoint |
| `connectors.sync.completed` | INFO | `source`, `entity`, `mode`, `rows`, `tombstones`, `skipped_deleted`, `files`, `watermark_before`, `watermark_after`, `duration_s` | End of a stream or backfill |
| `connectors.sync.failed` | ERROR | `source`, `entity`, `stream`, `mode`, `error_class` | Entity or tool failed |
| `connectors.sync.skipped_open_circuit` | WARNING | `source`, `stream`, `retry_at` | `CircuitOpen` |
| `connectors.backfill.slice_completed` | INFO | `source`, `entity`, `stream`, `slice_start`, `slice_end`, `rows` | Slice done |
| `connectors.backfill.slice_failed` | WARNING | same + `error_class`, `attempts` | Slice failed |
| `connectors.schema_drift.detected` | WARNING | `source`, `entity`, `added`, `removed`, `changed` | Drift (design name `schema_drift`, §13 D-4) |
| `connectors.reconcile.completed` | INFO | `source`, `entity`, `live_keys`, `source_keys`, `tombstones` | Reconcile end |
| `connectors.reconcile.aborted` | ERROR | `source`, `entity`, `live_keys`, `missing_keys`, `max_delete_pct` | Valve (design name `reconcile_aborted`) |
| `connectors.reconcile.skipped` | INFO | `source`, `entity`, `reason` | Delta-mode files entity |
| `connectors.lake.orphans_removed` | INFO | `source`, `count` | Cleanup removed files |
| `connectors.lake.orphan_remove_failed` | WARNING | `source`, `file`, `error_class` | Unlink failed |
| `connectors.lake.abort_failed` | ERROR | `source`, `entity`, `error_class` | `abort()` raised |
| `connectors.files.rejected` | WARNING | `entity`, `file`, `reason` (`symlink`, `not_regular`, `outside_root`, `too_large`, `changed`, `unreadable`) | File skipped |
| `connectors.files.skipped_known` | INFO | `entity`, `fingerprint` (12 hex) | Known fingerprint |
| `connectors.files.ingested` | INFO | `entity`, `fingerprint` (12 hex), `rows`, `files` | File recorded |
| `connectors.files.entity_folder_missing` | WARNING | `entity` | `check()` |
| `connectors.http.page_fetched` | DEBUG | `source`, `status`, `bytes`, `elapsed_ms` | Each page |
| `connectors.auth.token_refreshed` | DEBUG | `source` | Token fetched |
| `connectors.servicenow.audit_delete_unreadable` | INFO | `entity` | 403 on `sys_audit_delete` |
| `connectors.jira.bulk_changelog_unavailable` | WARNING | — | Bulk endpoint 404 |
| `connectors.mongodb.index_missing` | WARNING | `entity`, `field` | `check()` |

No event carries record text, record ids of deleted records, secret values, URLs with query strings, or file contents.

### 8.2 Metrics (`metric_sample`, written with X:08/herness.store.ops.record_metric_samples, R-12)

| Name | Kind | Labels | Meaning |
|------|------|--------|---------|
| `herness_connectors_records_total` | counter | `source`, `entity`, `mode` | Rows written |
| `herness_connectors_tombstones_total` | counter | `source`, `entity`, `mode` | Tombstones written |
| `herness_connectors_skipped_deleted_total` | counter | `source`, `entity` | Rows dropped by the deletion filter |
| `herness_connectors_pages_total` | counter | `source`, `status_class` (`2xx`, `4xx`, `5xx`) | HTTP pages |
| `herness_connectors_sync_duration_seconds` | histogram | `source`, `entity`, `mode` | Stream duration |
| `herness_connectors_watermark_lag_seconds` | histogram | `source`, `entity` | `now − watermark` after a run |
| `herness_connectors_schema_drift_total` | counter | `source`, `entity` | Drift events |
| `herness_connectors_reconcile_aborted_total` | counter | `source`, `entity` | Valve trips |
| `herness_connectors_files_ingested_total` | counter | `entity` | Inbox files recorded |

`source` label values are stream keys (`servicenow`, `monitoring:datadog`); all label sets are closed by config.

### 8.3 Trace events

None. Connectors run outside agent runs; spec 08 routes their retries to logs and `resilience_event` only (spec 08 §4.4).

### 8.4 Health

`source_health_report` (U01-57) returns `ok | degraded | down` with a reason per stream key; `herness doctor` shows it, and `doctor --sources` additionally calls `Connector.check()` (X:09).

---

## 9. Configuration

All keys are in `config/sources.yaml` under `sources` (read at process start; a change takes effect for the next job because the worker loads config per process and builds connectors per job — "restart" below means worker restart). Sensitivity: `internal` unless noted.

| Key path | Type | Default | Validation | Restart | Sensitivity |
|----------|------|---------|------------|---------|-------------|
| `sources.<s>.enabled` | bool | `false` | — | yes | internal |
| `sources.<s>.base_url` | str | — | U01-05 URL rules | yes | internal |
| `sources.<s>.auth.method` | str | — | `AUTH_METHODS` | yes | internal |
| `sources.<s>.auth.credentials` | str | — | `secret:<name>` | yes | reference only (value secret) |
| `sources.<s>.auth.tenant_id` | str | `null` | GUID; Dataverse only | yes | internal |
| `sources.<s>.page_size` | int | per source (SN 1000, Jira 100, Mongo 1000, Dataverse 5000) | per-source bounds | yes | internal |
| `sources.<s>.batch_rows` | int | 10000 | 1,000–100,000 | yes | internal |
| `sources.<s>.checkpoint_rows` | int | 500000 | ≥ `batch_rows`, ≤ 5,000,000 | yes | internal |
| `sources.<s>.overlap_minutes` | int | 30 (SN, Jira 60) | 0–1,440 | yes | internal |
| `sources.<s>.settle_seconds` | int | 60 | 0–3,600 | yes | internal |
| `sources.<s>.max_concurrency` | int | `CONCURRENCY_DEFAULTS` | ≤ `CONCURRENCY_CAPS` | yes | internal |
| `sources.<s>.schedule` | cron | `null` | five fields | scheduler reload (X:08) | internal |
| `sources.<s>.reconcile.schedule` | cron | `0 3 * * SUN` | five fields | scheduler reload | internal |
| `sources.<s>.reconcile.max_delete_pct` | float | 2.0 | 0 < x ≤ 100 | yes | internal |
| `sources.<s>.backfill.start` | date | now − 1,096 days | ≥ 1970-01-01, < now | yes | internal |
| `sources.<s>.backfill.slice_days` | int | 30 (SN `incident` 7) | 1–366 | yes | internal |
| `sources.<s>.timeout_s` | float | 60 | 1–600 | yes | internal |
| `sources.<s>.verify` | path | `null` | existing file; bool rejected | yes | internal |
| `sources.<s>.hosts` | list of host names | `[]` | U01-05 host rule; required for `mongodb` (URI hosts), `snowflake` (must include `<account>.snowflakecomputing.com`), `dataverse` (must include `login.microsoftonline.com`); optional for other sources (R-06) | yes (socket guard is installed at process start) | internal |
| `sources.<s>.entities.<e>.*` | per source | — | U01-04, U01-07–U01-13 | yes | internal |
| `sources.servicenow.entities.<e>.window_hours` | int | 24 | 1–168 | yes | internal |
| `sources.servicenow.entities.<e>.classes` | list | — | `cmdb_ci` only | yes | internal |
| `sources.servicenow.entities.<e>.filter` | str | `null` | no `^NQ`, `^EQ`, `ORDERBY` | yes | internal |
| `sources.jira.flavor` | `cloud`\|`datacenter` | — | required | yes | internal |
| `sources.jira.jql_scope` | str | `null` | no `order by`, no newlines | yes | internal |
| `sources.jira.fetch_remote_links` | bool | `false` | — | yes | internal |
| `sources.monitoring.adapters.<tool>.*` | adapter model | — | U01-09 | yes | internal |
| `sources.monitoring.adapters.<tool>.metric_queries[].name` | str | — | ∈ `DAILY_METRIC_NAMES` | yes | internal |
| `sources.mongodb.database`, `.max_time_ms` | str, int | —, 60000 | U01-10 | yes | internal |
| `sources.snowflake.account`, `.warehouse`, `.role`, `.statement_timeout_s`, `.max_scan_gb` | — | —, —, —, 900, 50 | U01-11 | yes | internal |
| `sources.files.inbox` | path | `data/inbox` | not a symlink | yes | internal |
| `sources.files.entities.<e>.pattern`, `.sheet`, `.key_field`, `.updated_field`, `.mode` | — | mode `delta` | U01-13 | yes | internal |

Also read: `mappings.custom_fields.jira.*` (X:02) by `build_connector`; `security.network.http_proxy` (X:10) by the X:10 egress client factory that `http_client` calls; `paths.data` (X:10); `resilience.retry.source_http_page` (X:08) through `retry_page`.

---

## 10. Performance and capacity

| ID | Design target (01 §8) | Dataset and hardware | Pass threshold | Marker |
|----|-----------------------|----------------------|----------------|--------|
| BT01-01 | ServiceNow replay throughput | 1M-row `api-pages` output (X:11 `synth_data.py api-pages`) through respx; reference PC (spec 02 §9) | ≥ 5,000 committed rows/s | `slow` |
| BT01-02 | Deletion filter overhead | 1M rows, 100k deleted ids | `_write_stream` time with filter ≤ 1.05 × without | `slow` |
| BT01-03 | Connector RSS | BT01-01 run plus a reconcile of 5M keys | peak RSS < 1.5 GB (`psutil`) | `slow` |
| BT01-04 | Files readers | Synthetic CSV 2M rows, XLSX 200k rows, Parquet 10M rows | ≥ 200k / 20k / 1M rows/s | `slow` |
| BT01-05 | Incremental all sources | Fixture replay of 300k new records across all sources | < 5 min end to end | `slow` |
| BT01-06 | Live source rates (SN 300–800 rows/s, Jira 150 issues/s, Mongo 20k docs/s, Snowflake 100k rows/s, Dataverse 1.5k rows/s, events 500/s) | Vendor sandboxes, Phase 6 | ≥ the lower bound of each design range | manual, Phase 6 gate |

Limits enforced by code: `batch_rows` (10,000), `checkpoint_rows` (500,000), page cap 64 MiB, line cap 1 MiB, pages per stream 1,000,000, DuckDB memory 1 GB (files and reconcile), inbox file 1 GiB, snapshot keys 5M, ServiceNow deletes per window 100,000, slice threads ≤ `max_concurrency`.

---

## 11. Test specification

Locations: `tests/unit/connectors/`, `tests/integration/connectors/`, `tests/fault/connectors/`, `tests/bench/connectors/`. Fixtures: respx cassettes in `tests/fixtures/connectors/<source>/` (X:11, scrubbed), `tests/fixtures/lake_small/` (X:11), `mongomock`, a fake Snowflake cursor returning Arrow batches (`tests/support/fake_snowflake.py`, written in T01-23), a fake `LakeWriter` recording writes/commits/aborts (`tests/support/fake_lake.py`, written in T01-06), a temp ops store migrated with X:02 `migrate()` and bound per test through the X:11 `ops_store` fixture (X:02 `reset_connections`), `freezegun` for time. Each test name or docstring carries its ID (ENG §6).

### 11.1 Unit tests

| ID | Unit / flow | Setup | Action | Expected | Marker |
|----|-------------|-------|--------|----------|--------|
| UT01-01 | U01-01, U01-06 | YAML with `credentials: "hunter2"` and with `secret:` + 70-char name | validate | `ValidationError`; message lacks `hunter2` | unit |
| UT01-02 | U01-05, U01-08, U01-12, U01-14 | Unknown key at source, entity and root levels | validate | error per key path | unit |
| UT01-03 | U01-05 | `verify: false`, `verify: true`, missing path, existing CA file | validate | first three fail with the TLS message; file accepted; `httpx_verify()` returns the path | unit |
| UT01-04 | U01-05, U01-06 | `max_concurrency` 9 for servicenow, 53 for dataverse, 2 for files | validate | all fail; cap values accepted | unit |
| UT01-05 | U01-02–U01-05 | Source and entity overrides of overlap, page size, backfill | call `overlap_for`, `backfill_for`, `resolve_start` | entity wins field by field; `start=None` → now − 1,096 d at midnight; future start → `ConfigError` | unit |
| UT01-06 | U01-07 | filters with `^NQ`, `ORDERBYDESCx`, newline; `classes` on `incident`; `incident` without backfill | validate | rejections; `incident` gets `slice_days=7` | unit |
| UT01-07 | U01-10 | filter with nested `$where`, `$function`, top-level `updated_field`, depth 9 | validate | each rejected | unit |
| UT01-08 | U01-09 (`validate_spl`), U01-84 | SPL without stats; with `\| delete`; with backtick; valid `\| tstats` | validate | three rejected, one accepted | unit |
| UT01-09 | U01-09, U01-82 | Prometheus `step` `12h`, `2d`, `1d`, `86400s` | validate | first two rejected | unit |
| UT01-10 | U01-11 | `table` with two parts; column `a;b`; filter with `drop` and `--` | validate | rejected | unit |
| UT01-11 | U01-09, U01-06 | `metric_queries[].name: foo` | validate | rejected | unit |
| UT01-12 | U01-13 | pattern `../*.csv`, `a/b.csv`, `*.exe`; `sheet` on csv | validate | rejected | unit |
| UT01-13 | U01-15, U01-12 | Config with SN, monitoring (2 adapters), Snowflake and Dataverse with `hosts` lists | `allowed_hosts` | exact set: SN and adapter `base_url` hosts, Dataverse `base_url` host, and the `hosts` entries; no host derived from `account` or `tenant_id` | unit |
| UT01-14 | U01-20 | valid and invalid inputs | `record_id` | format; `SchemaViolation` without the key value | unit |
| UT01-15 | U01-22 | table of names (`sys_id`, `_id`, `HTTPStatus`, `1x`, `CC_ID`, `a.b-c`, `___`) | `to_snake` | expected outputs; `___` → error | unit |
| UT01-16 | U01-23 | record with scalars, bool, float, dict, list, `{value, display_value}`, colliding keys | `flatten_record` | expected strings; collision → error | unit |
| UT01-17 | U01-24 | SN format, ISO `Z`, `+0000`, 9-digit fraction, date-only, naive ISO, epoch without unit, 1969; arrays with NTZ, tz, date32, null | parse | values or `SchemaViolation` | unit |
| UT01-18 | U01-25, U01-19 | batch_rows 3, 7 rows with a new column on row 5 | add/flush | batches 3/3/1; schema equals `METADATA_SCHEMA` + columns; new column null-filled later; one batch accepted by a real X:02 `LakeWriter` | unit |
| UT01-19 | U01-26 | 3 keys | `tombstone_batch` | `_deleted` true, `_payload` null, ids correct | unit |
| UT01-20 | U01-27, U01-28 | temp ops store | set, set lower, set higher, get | lower ignored (`False`); fixed-width round trip | unit |
| UT01-21 | U01-29 | requests in all four statuses, ids with `%` and `_`, another entity | `deleted_record_ids` | only `running`/`done` of this entity | unit |
| UT01-22 | U01-30 | plan A, rerun A, plan with longer last slice, done slice with larger end | `ensure_slices` | idempotent; reset rules of U01-30 | unit |
| UT01-23 | U01-31 | slice row | running, failed (600-char error), running, done | attempts 2; error truncated to 500; done clears error; missing row → error | unit |
| UT01-24 | U01-32 | — | record twice, get | second insert returns `False`; row equal | unit |
| UT01-25 | U01-33 | set of 2 ids; batch of 5 | `apply` before and after `reload` | before → error; after → 3 rows, dropped 2; empty set passthrough | unit |
| UT01-26 | U01-34 | temp tree with old/new temp files, non-matching dotfile, symlink to outside | cleanup at frozen now | only old matching files removed; symlink target untouched | unit |
| UT01-27 | U01-35 | schemas with added, removed, retyped column | `observe` | drift fields; identical → `None` | unit |
| UT01-28 | U01-21 | ranges: exact multiple, remainder, empty, naive | `split_range` | windows; error for naive | unit |
| UT01-29 | U01-38, U01-39, U01-36 | fake connector records `(since, until)`; watermark T; frozen now | `run_incremental` | `since = T − overlap`, `until = now − settle`; `SyncResult.to_dict` JSON-safe | unit |
| UT01-30 | U01-39 | fake connector yields nothing | run | watermark unchanged; result rows 0 | unit |
| UT01-31 | U01-40 | `checkpoint_rows` 20, 50 ordered rows, deletion request added after the first checkpoint | run | 3 commits; watermark set after each commit (call order asserted); second request id dropped after reload | unit |
| UT01-32 | U01-39 | no watermark | run | backfill path called with `backfill.start` and now; mode `backfill` | unit |
| UT01-33 | U01-40 | connector raises mid-stream | run | `abort()` called once; error re-raised; watermark unchanged | unit |
| UT01-34 | U01-40, U01-35 | batch 2 adds a column | run | commit before batch 2; two writers; `connectors.schema_drift.detected` logged (structlog capture) | unit |
| UT01-35 | U01-37, U01-39 | fake `guard` raising `CircuitOpen`; mismatched connector name | run; construct | no connector call; construct → `ConfigError` | unit |
| UT01-36 | U01-40 | unordered connector (`monitoring` name), rows past `until` | run | watermark set once at end, capped at `until` | unit |
| UT01-37 | U01-43 | 10 slices, factory, `max_concurrency` 3, fake connectors | backfill | ≤ 3 concurrent (counter); all `done`; watermark `min(end, max)` | unit |
| UT01-38 | U01-43 | slice 4 raises `SourceUnavailable` | backfill | slice 4 `failed`, others `done`; error raised; watermark unset | unit |
| UT01-39 | U01-43 | no rows anywhere; `should_stop` true after 2 slices | backfill | watermark = end; stop → `stopped` true, watermark unchanged | unit |
| UT01-40 | U01-44, U01-45 | lake with keys a,b,c; source keys a,b | reconcile | one tombstone for c; watermark unchanged; scratch removed | unit |
| UT01-41 | U01-44 | lake 100 live keys, source 90 keys, valve 2 % | reconcile | nothing written; `SchemaViolation`; `connectors.reconcile.aborted` | unit |
| UT01-42 | U01-44 | c under a `running` request | reconcile | no tombstone for c | unit |
| UT01-43 | U01-42 | monitoring connector; connector without `list_keys` | `run_reconcile` | `ConfigError` | unit |
| UT01-44 | U01-44, U01-45 | c already tombstoned; empty lake | reconcile | 0 tombstones both | unit |
| UT01-45 | U01-46, U01-47 | inbox with dotfile, `~$x.xlsx`, symlink, junction (Windows only), file 2 s old, wrong suffix | `candidates` | only eligible files; rejects logged with reasons | unit |
| UT01-46 | U01-48 | same bytes under two names; file appended during hashing (patched read) | fingerprint | equal digests; change → `InboxFileChanged` | unit |
| UT01-47 | U01-49 | CSV with composite key and a null key row | read | `_source_key` `a\|b`; null → error with row number | unit |
| UT01-48 | U01-49 | CSV with `last_modified`; CSV without | read | parsed values; else mtime | unit |
| UT01-49 | U01-49 | XLSX with sheet `Teams`; Parquet with int/decimal | read | strings for XLSX; Arrow types kept for Parquet | unit |
| UT01-50 | U01-47, U01-49 | 1 GiB + 1 file (sparse); corrupt XLSX | candidates / read | rejected `too_large`; corrupt → `SchemaViolation` | unit |
| UT01-51 | U01-50 | two files, one renamed copy, then a modified file | two runs | copy skipped; modified ingested; `file_ingest` after commit (order asserted) | unit |
| UT01-52 | U01-50 | snapshot entity: file1 keys a,b,c,d…(100), file2 missing 1 key | two runs | one tombstone with `_source_updated_at = file2 mtime` | unit |
| UT01-53 | U01-50 | file changes during `read_file` | run | writer aborted; no `file_ingest` row; next run ingests | unit |
| UT01-54 | U01-51 | two sources; first `CircuitOpen` | `handle_sync` | `done`; `skipped_open_circuit` = [first]; second ran | unit |
| UT01-55 | U01-51, U01-52 | entity A `SourceUnavailable`, B `SchemaViolation`, C ok | handler | C ran; `SchemaViolation` raised (fatal first) | unit |
| UT01-56 | U01-51 | `should_yield` true before entity 2 | handler | `JobOutcome(status="yield")` | unit |
| UT01-57 | U01-54 | option matrix incl. `--full` + `--backfill`, `--reconcile` without source, `--from` ≥ `--to` | build | kinds, payloads, idem keys; conflicts → `ConfigError` | unit |
| UT01-58 | U01-56 | staging SQL fixture referencing `x.close_code` and unqualified columns; config missing `close_code` | check | one issue `(incident, close_code)`; complete config → `[]` | unit |
| UT01-59 | U01-57 | breaker open for jira, stale SN watermark, fresh files | report | `down`, `degraded`, `ok` | unit |
| UT01-60 | U01-60 | responses 200, 302, 400, 401, 403, 404, 408, 429, 500, 529, 418 | map | table of U01-60 | unit |
| UT01-61 | U01-61 | `Retry-After: 7`, HTTP date +30 s, invalid + `X-RateLimit-Reset` epoch s / epoch ms / delta, nothing | parse | 7, 30, correct deltas, `None` | unit |
| UT01-62 | U01-59 | respx page of 65 MiB; invalid JSON; 503 twice then 200 | fetch | size error; JSON error; third attempt returns (only that page retried) | unit |
| UT01-63 | U01-58, U01-62 | fake X:10 factories recording their arguments; base URLs `https://sn.example`, `http://127.0.0.1:9090`; request to other host; 302 to other host; `check_next_url` to other host | client | `source_http_client` called with the `base_url`, `verify`, timeouts and pool size; loopback URL → `loopback_http_client`; other host → `EgressBlocked`; redirect not followed; next URL → `ForeignHostError` | unit |
| UT01-64 | U01-63 | each method with fake secrets backend | `build_auth` | headers per shapes table; missing member → `ConfigError` | unit |
| UT01-65 | U01-64 | respx token endpoint `expires_in` 600; frozen time | requests at t, t+299, t+301; a 401 | 1 fetch until t+300; refresh after; 401 → one refetch and retry | unit |
| UT01-66 | U01-08, U01-63 | `auth.method: oauth_3lo` | validate | `ConfigError` "not supported in v1" | unit |
| UT01-67 | U01-67, U01-68 | since/until with sub-second parts; 60 h range, window 24 | build query; sync | exact query strings; 3 windows | unit |
| UT01-68 | U01-68 | cassettes: empty result, exactly one full page then empty, short last page, `Link rel=next` | sync | row counts equal fixture; no duplicate or missed page | unit |
| UT01-69 | U01-68 | record with reference field `{value, display_value}` | sync | `<f>` and `<f>_display` columns | unit |
| UT01-70 | U01-68, U01-69 | audit deletes interleaved; 403 probe | sync | output ascending with tombstones; 403 → no tombstones, INFO once | unit |
| UT01-71 | U01-70 | 2 pages of ids | `list_keys` | `KEY_SCHEMA` batches; `sysparm_fields=sys_id` | unit |
| UT01-72 | U01-72, U01-73 | Cloud cassette: page of 37 with `isLast: false`, then `isLast: true` | sync | JQL text exact (minute, UTC); loop continues; all rows | unit |
| UT01-73 | U01-73 | `isLast: false` without token | sync | `SchemaViolation` | unit |
| UT01-74 | U01-76, U01-62 | bulk changelog 404; issue with 57 histories over 2 pages | sync | per-issue fallback; `changelog` has 57 entries without `author` | unit |
| UT01-75 | U01-73, U01-76 | DC cassette `startAt` paging; `changelog.total` 60 > 40 | sync | refetch called; complete changelog | unit |
| UT01-76 | U01-77, U01-73 | `fetch_remote_links` true and false | sync | one call per issue, projected; false → column NULL, no calls | unit |
| UT01-77 | U01-75 | field list cassette | discover | expected candidates; no writes (ops store untouched) | unit |
| UT01-78 | U01-74 | key listing cassettes Cloud and DC | `list_keys` | all ids | unit |
| UT01-79 | U01-78–U01-81 | fake adapters | `sync_tool` both entities; `event_batch`, `metric_batch`; `complete_days` | source keys `<tool>:<id>` and `<tool>\|m\|svc\|date`; `_source_updated_at` = day end | unit |
| UT01-80 | U01-82 | query_range cassette; series without `service`; tenant set | daily_metrics | params `step=86400`, start = a+1 d; date = t − 1 d; missing label → error; `X-Scope-OrgID` sent | unit |
| UT01-81 | U01-83 | events 3 pages ending without `meta.page.after`; metrics series; 429 with `X-RateLimit-Reset: 12` | fetch | all events; rollup appended; `RateLimited.retry_after == 12` | unit |
| UT01-82 | U01-84 | export stream with preview line, error message line, 2 MiB line | fetch | preview skipped; error → `SchemaViolation`; long line → `SchemaViolation`; `search ` prefix added | unit |
| UT01-83 | U01-85 | problems 2 pages (second request has only `nextPageKey`); `endTime -1`; metrics | fetch | request params asserted; `end_ts` NULL; bucket date rule | unit |
| UT01-84 | U01-87 | mongomock 2,500 docs, page 1,000; `AutoReconnect` injected on page 2 once | sync | 2,500 rows ascending, no duplicates; `_source_key = str(_id)`; relaxed JSON payload | unit |
| UT01-85 | U01-86 | collection without index on `updated_field`; URI without TLS; `OperationFailure(code=18)` | check / sync | WARNING logged; `ConfigError`; `AuthError` | unit |
| UT01-86 | U01-88, U01-89 | fake cursor with 3 Arrow tables | sync | SQL text with bound params and quoted identifiers; `QUERY_TAG` set; batches ≤ `batch_rows`; types kept | unit |
| UT01-87 | U01-88 | EXPLAIN reports 60 GB with `max_scan_gb` 50; `SHOW WAREHOUSES` without monitor | sync / check | `ConfigError` before the SELECT; check `ConfigError` | unit |
| UT01-88 | U01-88 | fake errors: sqlstate 28000, 57014, network | sync | `AuthError`, `RateLimited(None)`, `SourceUnavailable` | unit |
| UT01-89 | U01-91 | 3 pages with `@odata.nextLink`, annotations | sync | `Prefer` on every page; nextLink used unmodified; `_display` columns | unit |
| UT01-90 | U01-65 | fake MSAL app: token, then error result | token calls | cached until exp − 300 s; error → `AuthError` with code only | unit |
| UT01-91 | U01-91 | key listing pages | `list_keys` | all keys; `$select=<key>` only | unit |
| UT01-92 | U01-38, U01-79 | two tools; datadog raises `SourceUnavailable` | `run_incremental` | prometheus rows committed and `monitoring:prometheus` advanced; datadog watermark unchanged; error re-raised | unit |
| UT01-93 | U01-41, U01-43 | monitoring backfill | run | slices keyed `monitoring:<tool>` | unit |
| UT01-94 | U01-16, U01-17, U01-55 | every registered connector built from a synth config | `isinstance` checks, `build_connector` | satisfies `Connector`; key listing where specified; no network during construction (respx asserts no calls) | unit |
| UT01-95 | U01-92 | temp ops store with watermarks for `servicenow/incident`, `monitoring:datadog/event`, `monitoring:prometheus/metric_daily`; one corrupt value in a second store | `list_watermarks` | three `Watermark` rows ordered by `(source, entity)`, aware UTC values, per-tool monitoring keys; corrupt value → `SchemaViolation` | unit |
| UT01-96 | U01-93, U01-94 | Cloud issue with every `JIRA_FIELDS` member, two custom fields, `description` as an ADF object, histories with `author` given out of order, remote links with extra members; the same issue without `resolutiondate`; `id` `"12a"`; custom id `cf_1` | `flatten_issue` | keys exactly `JIRA_ISSUE_COLUMNS` + custom ids in order; absent field → `None`; objects compact JSON; `changelog` sorted and without `author`; projections idempotent; `remotelinks=None` → `None`; bad id → `SchemaViolation`; bad custom id → `ConfigError` | unit |
| UT01-97 | U01-05, U01-07–U01-15 | `hosts` with an IP literal, a port, a duplicate, upper case; `hosts: [sso.example.com]` on servicenow; mongodb without `hosts`; snowflake `hosts` without the account host; dataverse `hosts` without `login.microsoftonline.com` | validate; `allowed_hosts` | IP, port and duplicate rejected; upper case lower-cased; servicenow accepted and `sso.example.com` is in `allowed_hosts` while `http_client` still targets only the `base_url` host; the three SDK cases rejected with their messages; a valid config's `hosts` entries appear in `allowed_hosts` | unit |

### 11.2 Property tests

| ID | Unit | Property | Marker |
|----|------|----------|--------|
| PT01-01 | U01-22 | For any text, output matches `^[a-z][a-z0-9_]{0,127}$` or raises; idempotent | unit |
| PT01-02 | U01-61 | Never negative; integer `Retry-After` returns itself | unit |
| PT01-03 | U01-21 | Windows cover `[start, end)` exactly, no overlap, each ≤ step | unit |
| PT01-04 | U01-23 | Values are `str` or `None`; dict/list values round-trip through `json.loads` | unit |
| PT01-05 | U01-20 | Split on first two `:` returns the inputs | unit |
| PT01-06 | U01-33 | Output ∩ deleted = ∅ and `rows_out + dropped = rows_in` | unit |

### 11.3 Integration tests

| ID | Flow | Setup | Action | Expected | Marker |
|----|------|-------|--------|----------|--------|
| IT01-01 | F01-04 | `lake_small` inbox (X:11), real `LakeWriter`, temp ops store | `handle_sync` files twice | lake files present; `file_ingest` rows; second run adds 0 rows | integration |
| IT01-02 | F01-01 | ServiceNow cassette; record updated inside the overlap after run 1 | two runs + X:02 staging build | record in lake; staging keeps one row | integration |
| IT01-03 | F01-01 | Same cassette unchanged | two runs + build | `core.*` row counts identical after dedupe | integration |
| IT01-04 | F01-01 | Cassettes with a new field, a removed field, a type change | runs + build | new writers; build succeeds; drift logged | integration |
| IT01-05 | F01-03 | Real lake from IT01-02 plus key cassette missing one key | reconcile + build | tombstone written; record gone in staging | integration |
| IT01-06 | F01-02 → F01-01 | Backfill with `end` = now, then an incremental | run | first incremental `since` = backfill watermark − overlap; no gap | integration |
| IT01-07 | F01-06 | synth profile config, X:02 staging SQL | `check_mapping` for every source | `[]` for the synth config | integration |
| IT01-08 | F01-01 | X:08 `run_inline` with registered handlers | enqueue `sync` for files | job `done`; result contains `SyncResult` dict | integration |
| IT01-09 | F01-05 | Ops store with requests `running`, `done`, `pending` for three fetched ids | sync | only the `pending` one is written; `skipped_deleted` = 2 | integration |
| IT01-10 | F01-01 | ServiceNow cassette where a record changes `sys_updated_on` during paging | run 1, run 2 | record missing after run 1, present after run 2 | integration |
| IT01-11 | §4.5 (R-59) | Jira Cloud cassette (X:11) synced through `JiraConnector` with a real `LakeWriter`; `mappings.custom_fields.jira` set for the synth profile | sync, then X:02 staging `120_stg_jira.sql` | every §4.5 column present with its encoding; `stg.jira_issue`, `stg.jira_transition` and `stg.jira_link` have the expected rows; X:02 `stg.cast_stats` reports no failed casts for the contract columns | integration |

### 11.4 Fault tests (`HERNESS_ENV=test`, `HERNESS_FAULTS` plans, spec 08 §5.13)

Fault points are those of the X:08 registry only; this spec uses `connector.before_watermark` and `http.page` (R-40). Plans are JSON files and are honoured only when `HERNESS_ENV=test` (R-40). The Plan column abbreviates the JSON fields `point`, `action` and the action's parameters.

| ID | Plan | Assertions | Marker |
|----|------|------------|--------|
| FT01-01 | `connector.before_watermark kill nth=2` on ServiceNow replay (subprocess) | Rerun gives a superset of rows; after build, `core.*` equals a no-crash run | fault |
| FT01-02 | `connector.before_watermark kill nth=1` on files (spec 11 X5) | Rerun re-ingests the file once; `file_ingest` has one row per fingerprint; `core.*` has no duplicates | fault |
| FT01-03 | Kill the job process after 3 of 10 slices are `done` | Restart runs 7 slices; final watermark = `min(end, max)` | fault |
| FT01-04 | `http.page http_429 retry_after=7 count=3 source=jira` | Three `retry` events with `retry_after_s=7`; only that page repeated; watermark advanced once | fault |
| FT01-05 | respx 401 on first page | `AuthError`; no retry; breaker `open` (X:08 `force_open`) | fault |
| FT01-06 | 10 consecutive 503 on one source | Breaker open; job `done` with `skipped_open_circuit`; next run makes no HTTP call while open | fault |
| FT01-07 | Leave `.x.parquet.tmp-<ulid>` files aged 2 h and 10 min | Runner deletes the 2 h file only; build ignores both | fault |

### 11.5 Security tests

| ID | Threat | Attack | Expected | Marker |
|----|--------|--------|----------|--------|
| ST01-01 | TH01-01 | `verify: false` in config; respx server with a self-signed cert via a real local TLS server | Config rejected; connection fails with `SourceUnavailable` (TLS error) | unit |
| ST01-02 | TH01-02 | `Link: <https://evil.example/...>; rel="next"`; `@odata.nextLink` to another host | `ForeignHostError`; no request to the other host; bearer never sent | unit |
| ST01-03 | TH01-03 | Sentinel secret values in the fake backend; run a sync that fails with 401 and 500 | Sentinels absent from captured logs, exception strings, `SyncResult`, lake files and `sync_slice.last_error` | unit |
| ST01-04 | TH01-04 | Endless pagination returning the same cursor; 65 MiB page | `SchemaViolation` in both | unit |
| ST01-05 | TH01-05 | Response with `result` as an object; record without `sys_updated_on`; watermark before | `SchemaViolation`; watermark unchanged; nothing committed | unit |
| ST01-06 | TH01-06 | 429 with `Retry-After: 1000000` | `RateLimited` re-raised past the cap (X:08); job rescheduled, not blocked | unit |
| ST01-07 | TH01-07 | Injection strings for each filter type and `jql_scope` | Config rejected | unit |
| ST01-08 | TH01-08 | Symlink in inbox to `C:\Windows\win.ini` / `/etc/passwd`; pattern `..\*.csv` | Symlink skipped; pattern rejected | unit |
| ST01-09 | TH01-09 | XLSX zip bomb (small file, huge sheet); 1.1 GiB sparse CSV | `SchemaViolation` within the memory cap; size reject without reading | unit |
| ST01-10 | TH01-10 | Swap file content between fingerprint and read | File skipped; no `file_ingest` row | unit |
| ST01-11 | TH01-11 | Deleted `record_id` in a batch | The id's bytes do not appear in any written lake file | unit |
| ST01-12 | TH01-12 | Ingest two files | `file_ingest` rows with fingerprint, path, rows, files; INFO logs carry counts | unit |
| ST01-13 | TH01-13 | Source key listing returns zero keys | Valve trips; nothing written | unit |
| ST01-14 | TH01-14, TH01-15 | Static scan of `herness/connectors/` | No `httpx.Client(`, `httpx.AsyncClient(`, `httpx.HTTPTransport(` or `httpx.AsyncHTTPTransport(` anywhere (R-06); no `verify=False`; no HTTP methods other than GET/POST; no `requests.` import | unit |
| ST01-15 | TH01-16 | Token expired by the frozen clock; capture DEBUG logs | New token fetched before use; token string absent from logs and `repr` | unit |
| ST01-16 | TH01-17 | EXPLAIN reporting 10× `max_scan_gb` | Main SELECT never executed (fake cursor call log) | unit |
| ST01-17 | TH01-18 | MongoDB URI secret naming `rogue.example` while `sources.mongodb.hosts` lists only `db1.example`; `hosts` omitted for snowflake; dataverse `hosts` without `login.microsoftonline.com` | URI case: `ConfigError` before `client_factory` is called (factory call log empty), message lacks the host; config cases rejected at validation | unit |

### 11.6 Benchmarks

BT01-01–BT01-06 as defined in §10, in `tests/bench/connectors/` using `pytest-benchmark` (BT01-06 is a manual Phase 6 measurement recorded in `data/bench/`).

---

## 12. Task cards

Ordered by phase, then dependency. "Lines" are production lines. Every card's acceptance includes: `ruff check`, `ruff format --check`, `mypy --strict herness/` and `lint-imports` report 0 errors, and coverage of `herness/connectors` does not drop (spec 11 §4.3: ≥ 85 % line, ≥ 75 % branch at phase gate).

### Phase 1

#### T01-01 Common settings models

| Field | Content |
|-------|---------|
| Goal | `settings_base.py` validates every common key of `sources.yaml` with secure defaults. |
| Depends on | X:10/herness.core.config.load_config (ValidationError → ConfigError conversion), X:00/herness.core.errors |
| Units | U01-01, U01-02, U01-03, U01-04, U01-05, U01-06 |
| Files | `herness/connectors/__init__.py`, `herness/connectors/settings_base.py` |
| Tests | UT01-01, UT01-03, UT01-04, UT01-05, ST01-01 (config part) |
| Threats | TH01-01, TH01-03 |
| Acceptance checks | `pytest -m unit -k "UT01-01 or UT01-03 or UT01-04 or UT01-05 or ST01-01"` passes; importing `herness.connectors.settings_base` imports no module outside the standard library, pydantic, `herness.core.types` and `herness.core.errors` (R-03; asserted by a test with `sys.modules`) |
| Blocked by | none |
| Size | M |

#### T01-02 Per-source settings and `SourcesConfig`

| Field | Content |
|-------|---------|
| Goal | `settings.py` models every source section (including the `hosts` rules of R-06), `validate_spl` and `allowed_hosts`, and X:10 assembles the `config/sources.yaml` model from `SourcesConfig` plus the X:02 `dq` and `build` sections (R-03). |
| Depends on | T01-01; X:10/herness.core.config.load_config (assembly of the file model, consumer) |
| Units | U01-07–U01-15 |
| Files | `herness/connectors/settings.py`, `pyproject.toml` (settings import contract lists `herness.connectors.settings` and `herness.connectors.settings_base`; not production code) |
| Tests | UT01-02, UT01-06–UT01-13, UT01-66, UT01-97, ST01-07 |
| Threats | TH01-07, TH01-08, TH01-15, TH01-18 |
| Acceptance checks | `pytest -k "UT01-02 or UT01-06 or UT01-07 or UT01-08 or UT01-09 or UT01-10 or UT01-11 or UT01-12 or UT01-13 or UT01-66 or UT01-97 or ST01-07"` passes; importing `herness.connectors.settings` loads no module outside the standard library, pydantic, `herness.core.types`, `herness.core.errors` and `herness.connectors.settings_base` (asserted with `sys.modules`); `lint-imports` passes with no `herness.connectors.settings -> herness.model.settings` exception; the design 01 §7 example YAML (with the `[...]` lists filled) validates; `herness config validate --profile synth --offline` accepts the synth `files` section |
| Blocked by | none |
| Size | M |

#### T01-03 Protocols, metadata constants and row helpers

| Field | Content |
|-------|---------|
| Goal | `base.py` and `rows.py` provide the protocols, `METADATA_SCHEMA`, `record_id`, `split_range` and the batching helpers. |
| Depends on | T01-01; X:02/herness.store.lake.LakeWriter (for UT01-18 compatibility check) |
| Units | U01-16–U01-26 |
| Files | `herness/connectors/base.py`, `herness/connectors/rows.py` |
| Tests | UT01-14–UT01-19, UT01-28, PT01-01, PT01-03, PT01-04, PT01-05 |
| Threats | TH01-05 |
| Acceptance checks | `pytest -k "UT01-14 or UT01-15 or UT01-16 or UT01-17 or UT01-18 or UT01-19 or UT01-28 or PT01-01 or PT01-03 or PT01-04 or PT01-05"` passes; a batch from `RowBatcher` is accepted by a real `LakeWriter.write` |
| Blocked by | none |
| Size | M |

#### T01-04 Ops-store ingestion functions

| Field | Content |
|-------|---------|
| Goal | The ingest area `herness/store/ops/ingest.py` (R-08) exposes the watermark, slice, file-ingest, watermark-listing and deletion-set functions through `herness.store.ops`. |
| Depends on | X:02/herness.store.ops.core.connection, run_write, read_one, read_all, dump_json, load_json (R-10); X:02/herness/store/migrations/001_ingestion_health.sql; X:02/herness.store.ops package `__init__.py`; X:00/herness.core.time.format_fixed |
| Units | U01-27–U01-32, U01-92 |
| Files | `herness/store/ops/ingest.py`, `herness/store/ops/__init__.py` (spec 01 `__all__` block only) |
| Tests | UT01-20–UT01-24, UT01-95 |
| Threats | TH01-11, TH01-12 |
| Acceptance checks | `pytest -k "UT01-20 or UT01-21 or UT01-22 or UT01-23 or UT01-24 or UT01-95"` passes on a migrated temp store; X:02 UT02-68 (no duplicate names across `__all__` blocks) passes; `lint-imports` shows `herness.store` imports nothing from `herness.connectors`; no file `herness/store/ops_ingest.py` exists |
| Blocked by | none |
| Size | M |

#### T01-05 Deletion filter and lake-file helpers

| Field | Content |
|-------|---------|
| Goal | `DeletionFilter`, orphan temp cleanup and `SchemaTracker` exist. |
| Depends on | T01-03, T01-04 |
| Units | U01-33, U01-34, U01-35 |
| Files | `herness/connectors/deletion.py`, `herness/connectors/lakefiles.py` |
| Tests | UT01-25, UT01-26, UT01-27, PT01-06 |
| Threats | TH01-08, TH01-11 |
| Acceptance checks | `pytest -k "UT01-25 or UT01-26 or UT01-27 or PT01-06"` passes; UT01-26 runs on Windows and Linux CI |
| Blocked by | none |
| Size | S |

#### T01-06 Sync runner: incremental flow and write loop

| Field | Content |
|-------|---------|
| Goal | `SyncRunner.run_incremental` implements F01-01 for ordered, unordered and tool-stream connectors. |
| Depends on | T01-05; X:08/herness.core.resilience.guard, fault_point; X:02 LakeWriter; X:08/herness.store.ops.record_metric_samples |
| Units | U01-36, U01-37, U01-38, U01-39, U01-40 |
| Files | `herness/connectors/runner.py`, `tests/support/fake_lake.py` (test support) |
| Tests | UT01-29, UT01-30, UT01-31, UT01-32, UT01-33, UT01-34, UT01-35, UT01-36, UT01-92 (with fake tool-stream connector) |
| Threats | TH01-05, TH01-11 |
| Acceptance checks | `pytest -k "UT01-29 or UT01-30 or UT01-31 or UT01-32 or UT01-33 or UT01-34 or UT01-35 or UT01-36 or UT01-92"` passes; UT01-31 asserts commit → watermark call order; log events of §8.1 for sync asserted |
| Blocked by | none |
| Size | M |

#### T01-07 Backfill slices

| Field | Content |
|-------|---------|
| Goal | Resumable parallel backfill (F01-02) wired into `SyncRunner.run_backfill`. |
| Depends on | T01-06 |
| Units | U01-41, U01-43 |
| Files | `herness/connectors/backfill.py`, `herness/connectors/runner.py` |
| Tests | UT01-37, UT01-38, UT01-39, UT01-93 |
| Threats | TH01-11 |
| Acceptance checks | `pytest -k "UT01-37 or UT01-38 or UT01-39 or UT01-93"` passes; no test sleeps (fake clock and synchronisation events) |
| Blocked by | none |
| Size | M |

#### T01-08 Reconciliation

| Field | Content |
|-------|---------|
| Goal | `run_reconcile` implements F01-03 with the safety valve. |
| Depends on | T01-06 |
| Units | U01-42, U01-44, U01-45 |
| Files | `herness/connectors/reconcile.py`, `herness/connectors/runner.py` |
| Tests | UT01-40–UT01-44, ST01-13 |
| Threats | TH01-13 |
| Acceptance checks | `pytest -k "UT01-40 or UT01-41 or UT01-42 or UT01-43 or UT01-44 or ST01-13"` passes; scratch folder empty after each test |
| Blocked by | none |
| Size | M |

#### T01-09 Files connector

| Field | Content |
|-------|---------|
| Goal | `FilesConnector` lists, fingerprints and reads CSV, XLSX and Parquet drops safely. |
| Depends on | T01-03 |
| Units | U01-46, U01-47, U01-48, U01-49 |
| Files | `herness/connectors/files.py` |
| Tests | UT01-45–UT01-50, ST01-08, ST01-09, BT01-04 |
| Threats | TH01-08, TH01-09, TH01-10 |
| Acceptance checks | `pytest -k "UT01-45 or UT01-46 or UT01-47 or UT01-48 or UT01-49 or UT01-50 or ST01-08 or ST01-09"` passes; DuckDB `excel` extension loads offline in CI |
| Blocked by | O-3 (excel extension pre-installed) |
| Size | M |

#### T01-10 Files ingest path

| Field | Content |
|-------|---------|
| Goal | F01-04: fingerprint dedupe, `file_ingest` records, snapshot reconcile. |
| Depends on | T01-08, T01-09 |
| Units | U01-50 |
| Files | `herness/connectors/files_ingest.py`, `herness/connectors/runner.py` (dispatch line) |
| Tests | UT01-51, UT01-52, UT01-53, ST01-10, ST01-12 |
| Threats | TH01-10, TH01-12 |
| Acceptance checks | `pytest -k "UT01-51 or UT01-52 or UT01-53 or ST01-10 or ST01-12"` passes |
| Blocked by | none |
| Size | S |

#### T01-11 Job handlers, factory and CLI payloads

| Field | Content |
|-------|---------|
| Goal | `sync` and `reconcile` jobs run through X:08; `herness sync` options map to jobs. |
| Depends on | T01-07, T01-08, T01-10; X:08/herness.core.jobs.register_handler, JobContext; X:08/herness.core.types.JobOutcome; X:10/herness.core.registry.get; X:09/herness.cli sync command (consumer) |
| Units | U01-51, U01-52, U01-53, U01-54, U01-55 |
| Files | `herness/connectors/jobs.py`, `herness/connectors/factory.py` |
| Tests | UT01-54–UT01-57, UT01-94, IT01-01, IT01-08, IT01-09 |
| Threats | TH01-07 |
| Acceptance checks | `pytest -k "UT01-54 or UT01-55 or UT01-56 or UT01-57 or UT01-94"` and `pytest -m integration -k "IT01-01 or IT01-08 or IT01-09"` pass; `herness sync files` on the synth profile ingests the generated inbox (X:11) |
| Blocked by | none |
| Size | M |

#### T01-12 Mapping check and health

| Field | Content |
|-------|---------|
| Goal | `--check-mapping` and the connectors health report exist. |
| Depends on | T01-11; X:02/herness.model.build.render_sql; X:08/herness.core.resilience.breaker |
| Units | U01-56, U01-57 |
| Files | `herness/connectors/mapping_check.py`, `herness/connectors/health.py` |
| Tests | UT01-58, UT01-59, IT01-07 |
| Threats | — |
| Acceptance checks | `pytest -k "UT01-58 or UT01-59"` and `pytest -m integration -k IT01-07` pass |
| Blocked by | none |
| Size | M |

#### T01-13 Phase 1 fault and orphan tests

| Field | Content |
|-------|---------|
| Goal | Crash-safety of the files path and orphan cleanup proven end to end. |
| Depends on | T01-11; X:08 fault plan loader (`HERNESS_FAULTS`) |
| Units | — (tests only) |
| Files | none (tests in `tests/fault/connectors/`) |
| Tests | FT01-02, FT01-07, ST01-11, ST01-14 |
| Threats | TH01-11, TH01-14, TH01-15 |
| Acceptance checks | `pytest -m fault -k "FT01-02 or FT01-07"` and `pytest -k "ST01-11 or ST01-14"` pass |
| Blocked by | none |
| Size | S |

### Phase 6

#### T01-14 HTTP layer

| Field | Content |
|-------|---------|
| Goal | Source HTTP client obtained from `herness.core.egress` (R-06), page fetch with retry, error mapping, `Retry-After` parsing. |
| Depends on | T01-13; X:08/herness.core.resilience.retry_page, classify, policy, fault_point; X:10/herness.core.egress.source_http_client, loopback_http_client |
| Units | U01-58, U01-59, U01-60, U01-61, U01-62 |
| Files | `herness/connectors/http.py`, `herness/connectors/base.py` (re-export of `http_client`) |
| Tests | UT01-60–UT01-63, PT01-02, ST01-01, ST01-02, ST01-04, ST01-05, ST01-06 |
| Threats | TH01-01, TH01-02, TH01-04, TH01-05, TH01-06, TH01-15 |
| Acceptance checks | `pytest -k "UT01-60 or UT01-61 or UT01-62 or UT01-63 or PT01-02 or ST01-01 or ST01-02 or ST01-04 or ST01-05 or ST01-06"` passes; spec 10 egress lint passes with no exception for `herness/connectors/` |
| Blocked by | O-9 (`source_http_client` in impl 10) |
| Size | M |

#### T01-15 Auth

| Field | Content |
|-------|---------|
| Goal | Auth objects for every HTTP method, with token caches. |
| Depends on | T01-14; X:10/herness.core.secrets.resolve, resolve_json |
| Units | U01-63, U01-64, U01-65 |
| Files | `herness/connectors/auth.py` |
| Tests | UT01-64, UT01-65, UT01-90, ST01-03, ST01-15 |
| Threats | TH01-03, TH01-16 |
| Acceptance checks | `pytest -k "UT01-64 or UT01-65 or UT01-90 or ST01-03 or ST01-15"` passes |
| Blocked by | none |
| Size | M |

#### T01-16 ServiceNow connector

| Field | Content |
|-------|---------|
| Goal | ServiceNow sync, delete tombstones and key listing. |
| Depends on | T01-15; X:11 ServiceNow cassettes and `api-pages` output |
| Units | U01-66, U01-67, U01-68, U01-69, U01-70 |
| Files | `herness/connectors/servicenow.py` |
| Tests | UT01-67–UT01-71, IT01-02, IT01-03, IT01-04, IT01-05, IT01-06, IT01-10 |
| Threats | TH01-02, TH01-05 |
| Acceptance checks | `pytest -k "UT01-67 or UT01-68 or UT01-69 or UT01-70 or UT01-71"` and `pytest -m integration -k "IT01-02 or IT01-03 or IT01-04 or IT01-05 or IT01-06 or IT01-10"` pass |
| Blocked by | V-1 (sets production `max_concurrency` and auth method; code proceeds on defaults) |
| Size | M |

#### T01-17 Jira search and keys

| Field | Content |
|-------|---------|
| Goal | Jira Cloud and DC search, key listing, field discovery and the raw column contract (R-59). |
| Depends on | T01-15; X:11 Jira cassettes |
| Units | U01-71, U01-72, U01-73, U01-74, U01-75, U01-93, U01-94 |
| Files | `herness/connectors/jira.py`, `herness/connectors/jira_changelog.py` (`project_history`, `project_remote_link` only) |
| Tests | UT01-72, UT01-73, UT01-77, UT01-78, UT01-96 |
| Threats | TH01-04, TH01-05 |
| Acceptance checks | `pytest -k "UT01-72 or UT01-73 or UT01-77 or UT01-78 or UT01-96"` passes (changelog calls stubbed until T01-18); X:11 generator imports `herness.connectors.jira.flatten_issue` without importing `httpx` clients (import test) |
| Blocked by | V-2 (custom field ids per instance; config only) |
| Size | M |

#### T01-18 Jira changelog and remote links

| Field | Content |
|-------|---------|
| Goal | Complete changelog (bulk with fallback, DC refetch) and remote links per issue. |
| Depends on | T01-17 |
| Units | U01-76, U01-77 |
| Files | `herness/connectors/jira_changelog.py`, `herness/connectors/jira.py` |
| Tests | UT01-74, UT01-75, UT01-76, IT01-11 |
| Threats | TH01-05 |
| Acceptance checks | `pytest -k "UT01-74 or UT01-75 or UT01-76"` and `pytest -m integration -k IT01-11` pass (§4.5 contract read by X:02 `120_stg_jira.sql`) |
| Blocked by | V-2 (bulk changelog availability; fallback path covers absence) |
| Size | M |

#### T01-19 Monitoring connector base

| Field | Content |
|-------|---------|
| Goal | `MonitoringConnector`, adapter protocol, row builders and day helpers; per-tool streams through the runner. |
| Depends on | T01-14 |
| Units | U01-78, U01-79, U01-80, U01-81 |
| Files | `herness/connectors/monitoring/__init__.py`, `herness/connectors/monitoring/base.py` |
| Tests | UT01-79 |
| Threats | TH01-04 |
| Acceptance checks | `pytest -k "UT01-79 or UT01-92 or UT01-93"` passes with the real `MonitoringConnector` and fake adapters |
| Blocked by | none |
| Size | M |

#### T01-20 Prometheus/Mimir and Dynatrace adapters

| Field | Content |
|-------|---------|
| Goal | Two adapters with daily metrics and Dynatrace problems. |
| Depends on | T01-19, T01-15 |
| Units | U01-82, U01-85 |
| Files | `herness/connectors/monitoring/prometheus.py`, `herness/connectors/monitoring/dynatrace.py` |
| Tests | UT01-80, UT01-83 |
| Threats | TH01-04 |
| Acceptance checks | `pytest -k "UT01-80 or UT01-83"` passes |
| Blocked by | V-5 (Dynatrace timestamp semantics; default implemented) |
| Size | M |

#### T01-21 Datadog and Splunk adapters

| Field | Content |
|-------|---------|
| Goal | Two adapters with events and daily metrics. |
| Depends on | T01-19, T01-15 |
| Units | U01-83, U01-84 |
| Files | `herness/connectors/monitoring/datadog.py`, `herness/connectors/monitoring/splunk.py` |
| Tests | UT01-81, UT01-82 |
| Threats | TH01-04, TH01-07 |
| Acceptance checks | `pytest -k "UT01-81 or UT01-82"` passes |
| Blocked by | V-4 (Datadog event field mapping; default implemented) |
| Size | M |

#### T01-22 MongoDB connector

| Field | Content |
|-------|---------|
| Goal | MongoDB sync and key listing with key-set paging. |
| Depends on | T01-13; X:08 retry_page; X:10 secrets |
| Units | U01-86, U01-87 |
| Files | `herness/connectors/mongodb.py` |
| Tests | UT01-84, UT01-85, ST01-17 |
| Threats | TH01-01, TH01-03, TH01-07, TH01-18 |
| Acceptance checks | `pytest -k "UT01-84 or UT01-85 or ST01-17"` passes with `mongomock` |
| Blocked by | none |
| Size | M |

#### T01-23 Snowflake connector

| Field | Content |
|-------|---------|
| Goal | Snowflake sync, key listing, scan guard and resource-monitor check. |
| Depends on | T01-13; X:10 secrets |
| Units | U01-88, U01-89 |
| Files | `herness/connectors/snowflake.py`, `tests/support/fake_snowflake.py` (test support) |
| Tests | UT01-86, UT01-87, UT01-88, ST01-16 |
| Threats | TH01-17, TH01-07 |
| Acceptance checks | `pytest -k "UT01-86 or UT01-87 or UT01-88 or ST01-16"` passes |
| Blocked by | V-6 (error codes; default mapping implemented) |
| Size | M |

#### T01-24 Dataverse connector

| Field | Content |
|-------|---------|
| Goal | Dataverse sync and key listing with MSAL auth. |
| Depends on | T01-15 |
| Units | U01-90, U01-91 |
| Files | `herness/connectors/dataverse.py` |
| Tests | UT01-89, UT01-91 |
| Threats | TH01-02, TH01-16 |
| Acceptance checks | `pytest -k "UT01-89 or UT01-91"` passes |
| Blocked by | V-3 (change tracking; not used in v1) |
| Size | S |

#### T01-25 Phase 6 fault suite and benchmarks

| Field | Content |
|-------|---------|
| Goal | Live-connector crash, rate-limit, auth and breaker behavior and throughput proven. |
| Depends on | T01-16, T01-17, T01-18, T01-20, T01-21, T01-22, T01-23, T01-24 |
| Units | — (tests only) |
| Files | none (tests in `tests/fault/connectors/`, `tests/bench/connectors/`) |
| Tests | FT01-01, FT01-03, FT01-04, FT01-05, FT01-06, BT01-01, BT01-02, BT01-03, BT01-05, BT01-06 |
| Threats | TH01-06, TH01-16 |
| Acceptance checks | `pytest -m fault -k "FT01-01 or FT01-03 or FT01-04 or FT01-05 or FT01-06"` passes; `pytest -m slow -k "BT01-01 or BT01-02 or BT01-03 or BT01-05"` meets §10 thresholds; BT01-06 recorded in `data/bench/` at the Phase 6 gate |
| Blocked by | V-1, V-2 for BT01-06 only |
| Size | S |

---

## 13. Design deltas and open items

Cross-spec rulings of the consistency pass are recorded in [`DECISIONS.md`](DECISIONS.md). Each earlier delta and open item below carries its status: "Resolved by R-nn" (the ruling settles it and this spec now follows the ruling), "Accepted (R-nn)" (the ruling adopts this spec's position or makes this spec's change the contract; the design-spec edit is pending per DECISIONS §9), or "Still open" (no ruling covers it; the default stated here applies).

### 13.1 Design deltas (changes needed in design specs; none changes a contract signature except D-15)

| # | Design spec | Current text | Needed change | This spec implements | Status |
|---|-------------|--------------|---------------|----------------------|--------|
| D-1 | 01 §5.3 | "Monitoring has one watermark per entity for all adapters … advances only when every enabled adapter finished" | Replace with per-tool watermarks `monitoring:<tool>` (01 §6, 02 §5.1, Q10 resolved) | Per-tool (U01-18, U01-38) | Resolved by R-62 |
| D-2 | 01 §5.1 step 1, §10 "Open breaker" | Open breaker → job rescheduled at `retry_at` | `sync`/`reconcile` job ends `done` with outcome `skipped_open_circuit`; the next scheduled run retries | U01-51, F01-01 step 3 | Resolved by R-39 |
| D-3 | 10 §5.4 socket guard; ENG §2.1 | Guard allows only `base_url` hosts; connectors built their own host-guarded `httpx` client under a spec 10 §3.5 carve-out | No carve-out: HTTP sources get their client from `herness.core.egress`; SDK sources list their hosts in `sources.<name>.hosts`; the guard allowlist is those lists, the `base_url` hosts, the egress allowlist and loopback | U01-05, U01-10–U01-12, U01-15, U01-58, U01-86 | Resolved by R-06 (see O-9 for the missing factory) |
| D-4 | 01 §5.2, §5.4 | Log names `schema_drift`, `reconcile_aborted` | Use ENG §3.6 names `connectors.schema_drift.detected`, `connectors.reconcile.aborted` | §8.1 | Still open |
| D-5 | 01 §3.2 | `SyncRunner(connector, cfg, ops)` | Add keyword-only `clock`, `writer_factory`, `connector_factory`, `data_root`, `progress`, `should_stop`, and attributes `skipped_open`, `stopped` (additive) | U01-37 | Still open (the removal of `ops` is D-15) |
| D-6 | 09 §5.6 `sync` row vs 01 §3.2 | 09 lists `--full`; 01 does not define it | `--full` is a backfill from `backfill.start` to now | U01-54 | Resolved by R-63 |
| D-7 | 11 §5.5 X5 | Fault point `sync.before_watermark` | Use the impl 08 registry name `connector.before_watermark` | U01-40, U01-50, FT01-02 | Resolved by R-40 |
| D-8 | 01 §5.5 | Final watermark `min(end, max committed)` | State that a backfill committing no rows sets the watermark to `end` | U01-43 | Still open |
| D-9 | 01 §4.1, §5.10 | Tombstone time = deletion time, else detection time | For files snapshots, the snapshot file's mtime is the deletion time | U01-50 | Still open |
| D-10 | 01 §5.8 | Jira Cloud auth may be OAuth 2.0 (3LO) | v1 supports `api_token` (Cloud) and `pat` (DC) only; 3LO needs a refresh-token flow and `api.atlassian.com` host | U01-05, U01-08 | Still open |
| D-11 | 02 §5.1 / 02 §5 | Ops functions live in `herness.store.ops` | This spec's functions live in `herness/store/ops/ingest.py` (not `herness/store/ops_ingest.py`), re-exported by `herness.store.ops`; `deleted_record_ids` and `list_watermarks` are specified here | U01-27–U01-32, U01-92 | Resolved by R-08, R-09 |
| D-12 | 01 §7 | `metric_queries[].name` "must be a configured `core.metric_daily.metric_name`" | Spec 04 publishes the list of daily metric names; until then `DAILY_METRIC_NAMES` (O-5) | U01-06 | Still open |
| D-13 | 01 §5.9 Snowflake | Identifiers unquoted in the example SQL | Identifiers are upper-cased and double-quoted; case-sensitive lower-case Snowflake identifiers are not supported | U01-88 | Still open |
| D-14 | 01 §7, 10 §3.5 | No `hosts` key; Snowflake, MSAL and MongoDB hosts derived or listed in `security.network.extra_allowed_hosts` | New per-source key `sources.<name>.hosts` (list of host names), required for SDK sources; nothing derived from `base_url`, `account` or the URI | U01-05, U01-10–U01-12, U01-15 | Accepted (R-06) |
| D-15 | 01 §3.2 | `SyncRunner(connector, cfg, ops)`; `DeletionFilter` and health take a store object | No store parameter anywhere: ops access uses the impl 02 core API (`connection()`, `run_write()`, `read_one()`, `read_all()`) | U01-27–U01-33, U01-37, U01-57, U01-92 | Accepted (R-10) |
| D-16 | 01 §7, 00 §3 | `sources.yaml` root model embeds `dq` and `build` in the connectors settings module | `herness.connectors.settings` imports no `herness.model` module; `SourcesConfig` holds `version` and `sources`, and X:10/herness.core.config assembles the file model by adding `dq` and `build` | U01-14 | Accepted (R-03) |
| D-17 | 01 §4.2 Jira, 02 §4.1.2, 11 generator | The Jira raw column contract is described in three specs | Impl 01 owns the contract (§4.5) and its single implementation `flatten_issue`; impl 02 staging reads it; the impl 11 generator calls `flatten_issue` | §4.5, U01-93, U01-94 | Accepted (R-59) |

### 13.2 Open items (with current defaults)

| # | Item | Default in this spec | Affects | Status |
|---|------|----------------------|---------|--------|
| O-1 | Impl 02 ops migration contains `watermark`, `sync_slice`, `file_ingest` with the §4.1 types | Migration 001 of impl 02 has every column; this spec adds no migration in 010–019 | T01-04 | Resolved by R-11 |
| O-2 | Ops store safe to call from several threads | X:02 `connection()` is one SQLite connection per thread; no store object or `open_ops_store()` accessor | T01-04, T01-07, T01-11 | Resolved by R-10 |
| O-3 | DuckDB `excel` extension is installed at deploy (autoinstall is off) | X:10 deploy installs it; T01-09 test skips XLSX with a clear message if absent in dev | T01-09 | Still open |
| O-4 | MongoDB hosts for the socket guard | Operator lists them in `sources.mongodb.hosts`; the connector checks the URI against the list (U01-86) | T01-02, T01-22 | Resolved by R-06 |
| O-5 | Daily metric names | `availability_pct`, `error_rate`, `request_count`, `alert_firing_minutes` | T01-02 | Still open |
| O-6 | `metric_sample` table and writer | Table from impl 02 migration 006; writer X:08/herness.store.ops.record_metric_samples | T01-06 | Resolved by R-12 |
| O-7 | Q5: restored record with older update time stays tombstoned | No bump of `_source_updated_at` | — | Still open |
| O-8 | Jira OAuth 3LO | Rejected (D-10) | T01-02 | Still open |
| O-9 | R-06 makes `herness.core.egress` the only builder of `httpx` clients, but impl 10 defines only `EgressGuard.http_client` (model egress, purpose and payload class) and `loopback_http_client`. Connectors need a factory for non-loopback source hosts: `X:10/herness.core.egress.source_http_client(base_url, *, verify, timeout_s, connect_timeout_s, max_connections)` with the contract of U01-58 step 3 | U01-58 calls that name; T01-14 is blocked until impl 10 adds it | T01-14, and through it T01-15–T01-24 | Still open (new; owner impl 10) |
| O-10 | `deleted_record_ids` reads `deletion_request`, whose area under R-08 is `privacy.py` (impl 10) | Kept in the ingest area as a read-only function because impl 02 references it as `X:01/herness.store.ops.deleted_record_ids` and the runner is its main caller; impl 10 writes the table | T01-04 | Still open (new; confirm placement) |
| O-11 | Impl 02 (`SourcesConfig` "declares `dq` and `build`") and impl 10 (file model assembly) must follow D-16 | This spec's `SourcesConfig` has no `dq` or `build` | T01-02 | Still open (new; impl 02 and impl 10 text) |
| O-12 | Impl 11 references `X:01/herness.connectors.servicenow.flatten_record`, which does not exist | The ServiceNow flattening is `herness.connectors.rows.flatten_record` (U01-23) with `display_pairs=True` and the entity's fetch fields; the Jira one is `herness.connectors.jira.flatten_issue` (U01-93) | T01-03, T01-17 | Still open (new; impl 11 symbol name) |
| O-13 | Impl 08 U08-17 defines its own `parse_retry_after` with a different `X-RateLimit-Reset` rule (no epoch-millisecond branch) | This spec keeps U01-61 for HTTP pages; the two must converge on one implementation | T01-14 | Still open (new) |
| O-14 | Impl 09 calls the payload builder `sync_payload(...)` and uses idem key `sync:{source or 'all'}` for every sync, while U01-54 is `build_sync_payload` and gives backfill and reconcile their own keys | U01-54 as specified; impl 09 should call `build_sync_payload` and use its `idem_key` | T01-11 | Still open (new) |
| O-15 | R-03 lists what a settings module may import but not a settings module split over two files | `herness.connectors.settings_base` follows the same import rule and is listed with `herness.connectors.settings` in the settings import contract | T01-01, T01-02 | Accepted (R-03) |

Rulings checked that need no change here: R-01, R-02 (`JobContext` in `herness.core.jobs`, `JobOutcome` in `herness.core.types`; only the symbol names in X: references changed), R-04, R-05, R-07, R-13–R-38, R-41, R-43–R-45, R-47–R-58, R-60 (`busines_criticality` is a configured field of ServiceNow entity `cmdb_ci_service`, spelling kept), R-61, R-64–R-66. R-42 (handler reads `ctx.job.payload`), R-46 (exit codes in F01-06) and R-57 (orphan temp files are not committed lake files) are applied in U01-51, §5 and §4.2.

Inherited design open questions (01 §11):

| Q | Default followed |
|---|------------------|
| Q1 ServiceNow OAuth client credentials and rate-limit rule | `max_concurrency` 4; auth method from config (V-1) |
| Q2 Jira bulk changelog availability | Bulk with automatic per-issue fallback (V-2) |
| Q3 Monitoring tools and `availability_pct` definition | Adapters for all four; metric defined per tool in `sources.yaml` (D14) |
| Q4 Dataverse change tracking | Not used; weekly reconcile (V-3) |
| Q5 Tombstone bump on restore | No bump (O-7, V-7) |
| Q6 `customer_impact_minutes` source | Custom field from `mappings.yaml` (D13); config only |
| Q7–Q11 | Resolved in the design; implemented as resolved (Q10 → D-1, Q11 → F01-05) |

### 13.3 Verification items (`docs/specs/open-questions.md` (b), plus items raised here)

| # | Item | Source | Blocks |
|---|------|--------|--------|
| V-1 | ServiceNow OAuth client credentials allowed; inbound rate-limit rule; integration user timezone UTC | OQ (b) 22 | T01-16 production settings, BT01-06 |
| V-2 | Jira bulk changelog on the tenant; custom field ids; service account timezone UTC | OQ (b) 23 | T01-17/T01-18 production settings, BT01-06 |
| V-3 | Dataverse change tracking on needed tables | OQ (b) 24 | T01-24 (non-blocking) |
| V-4 | Datadog event attribute paths for `priority`, `status`, `title`, `aggregation_key` | raised here | T01-21 fixture freeze |
| V-5 | Dynatrace metric timestamps mark the slot end at `resolution=1d` | raised here | T01-20 fixture freeze |
| V-6 | Snowflake `sqlstate` values for auth failure and queue timeout | raised here | T01-23 fixture freeze |
| V-7 | Q5 decision with spec 02 | OQ (b) 26 | none (default) |
| V-8 | ServiceNow field survey for `acknowledged_at` and customer impact | OQ (b) 25 | config only |

### 13.4 Lint suppressions

None. A suppression needed later is added to this table with its rule and reason before merge.

---

## 14. Dependencies

### 14.1 Third-party packages (all already in spec 00 §9 unless marked new)

| Package | Min version | Licence | Use |
|---------|-------------|---------|-----|
| `httpx` | 0.27 | BSD-3-Clause | HTTP client, transports |
| `pyarrow` | 17 | Apache-2.0 | Record batches, Parquet key files |
| `duckdb` | 1.3 (with `excel` extension) | MIT | Inbox readers, reconcile anti-join |
| `pydantic` | 2.9 | MIT | Config and payload models |
| `pymongo` | 4.8 | Apache-2.0 | MongoDB connector (`bson.json_util`) |
| `snowflake-connector-python[pandas]` | 3.12 | Apache-2.0 | Snowflake connector, Arrow batches |
| `cryptography` | 42 (new direct use; already transitive via snowflake and msal) | Apache-2.0 / BSD | PEM → DER private key |
| `msal` | 1.31 | MIT | Dataverse tokens |
| `sqlglot` | as pinned by spec 05 | MIT | Staging SQL parsing for `--check-mapping` |
| `structlog` | 24 | MIT / Apache-2.0 | Logging (through X:00) |
| Tests: `respx`, `freezegun`, `mongomock`, `hypothesis`, `pytest-benchmark`, `psutil` | spec 00 §9 | BSD / Apache-2.0 / ISC / MPL-2.0 / BSD / BSD | Fixtures, time, property and bench tests |

`tenacity` is used only through X:08.

### 14.2 Internal implementation specs and units used

| Spec | Units / artifacts (`X:` references) |
|------|--------------------------------------|
| 00 | `X:00/herness.core.errors` (taxonomy), `X:00/herness.core.ids.new_ulid`, `X:00/herness.core.time.now_utc`, `format_fixed`, `parse_fixed`, `X:00/herness.core.logging.get_logger` |
| 02 | `X:02/herness.store.lake.LakeWriter`, `LakeFileSet`; `X:02/herness.store.ops.core.connection`, `run_write`, `read_one`, `read_all`, `dump_json`, `load_json`, `reset_connections` (R-10), `X:02/herness.store.ops` package `__init__.py`, migration 001 (tables of §4.1); consumer of this spec's `list_watermarks` and `deleted_record_ids` (build job) and of the §4.5 Jira contract (`120_stg_jira.sql`); `X:02/herness.model.build.render_sql`; staging SQL files `110`–`170`; `X:02` mappings model `custom_fields.jira` |
| 08 | `X:08/herness.core.resilience.guard`, `retry_page`, `classify`, `policy`, `breaker`, `fault_point`; `X:08/herness.core.jobs.register_handler`, `JobContext`, `run_inline`; `X:08/herness.core.types.JobOutcome`; `X:08/herness.store.ops.record_metric_samples` (R-12); fault points `connector.before_watermark`, `http.page` (R-40) |
| 09 | `X:09/herness.cli` `sync` and `doctor --sources` commands (consumers of U01-54, U01-56, U01-57, U01-75) |
| 10 | `X:10/herness.core.config.load_config` (assembles the `sources.yaml` model from `SourcesConfig`, D-16), `get_config`, `HernessConfig`; `X:10/herness.core.registry.register`, `get`; `X:10/herness.core.secrets.resolve`, `resolve_json`; `X:10/herness.core.egress.source_http_client` (O-9), `loopback_http_client` (R-06); `X:10/herness.core.egress.install_socket_guard` (consumer of U01-15); deploy step installing the DuckDB `excel` extension |
| 11 | `X:11/tests/fixtures/connectors/<source>/` cassettes; `X:11/tests/fixtures/lake_small`; `X:11/tools/synth_data.py api-pages`; synth profile `files` entry; `ops_store` test fixture; consumer of `herness.connectors.rows.flatten_record` and `herness.connectors.jira.flatten_issue` (R-59) |
