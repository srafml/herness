# 01 — Connectors

Status: Draft v2 · 2026-09-24 · Depends on: 00, 02. Phase 1 (files), Phase 6 (live sources).

v2 aligns with shared contracts 00 v2 and 02 v2. The v1 contract requests were accepted (§13).

## 1. Purpose and scope

Connectors copy source records into the raw Parquet lake (spec 02 §3) incrementally, idempotently and resumably. They do not type, join, map enums or resolve services. That work belongs to staging SQL (spec 02 §4.2).

In scope: the `Connector` implementations and the sync runner, watermarks, tombstones and key reconciliation, backfill, honoring deletion requests, and one connector per source: ServiceNow, Jira Cloud/Data Center, monitoring (adapters for Prometheus/Mimir, Datadog, Splunk, Dynatrace), MongoDB, Snowflake, Dataverse, Files.
Out of scope: the lake writer (spec 02 §3.2), retry and circuit-breaker mechanics (spec 08), secret storage and the deletion procedure (spec 10), canonical SQL (spec 02), scheduling of `sync`/`reconcile` jobs (spec 08).

## 2. Responsibilities

- Implement `Connector` (spec 00 §6) for every source in `herness/connectors/`, registered with `registry.register("connector", name)`.
- Fetch only records changed since the watermark minus an overlap window, and hand them to `LakeWriter` with the spec 02 §3.1 metadata columns.
- Advance `watermark` only after `LakeWriter.commit()` returns.
- Emit tombstones (`_deleted = true`) when the source reports deletes. Run weekly key reconciliation where it does not.
- Run initial backfills as parallel date slices that resume after a crash (`sync_slice`).
- Never write records whose `record_id` has a `deletion_request` in status `running` or `done` (spec 02 §5.5, spec 10).
- Raise only spec 00 §7 errors. Honor `Retry-After`.
- Fetch monitoring **events and daily aggregates only**, never raw full-resolution series.

## 3. Interfaces

### 3.1 Protocol (`herness/connectors/base.py`)

`Connector` and `SupportsKeyListing` are defined in spec 00 §6 (`sync(entity, since, until=None)`, `list_keys(entity)`). Contract for `sync()`:
- Yields `pa.RecordBatch` objects of at most `batch_rows` rows (default 10,000). Every batch already contains the eight spec 02 §3.1 metadata columns with their declared types.
- Records come in ascending `(watermark_field, source_key)` order when the source supports that order (every source here except Files). The runner relies on this order for checkpointed watermarks (§5.3).
- `since` is inclusive and `until` is exclusive. `since=None` means the source's beginning.
- It performs no file I/O, no ops-store writes and no deletion filtering. The runner does all three, so connectors can be tested with respx fixtures alone.

### 3.2 Sync runner (`herness/connectors/runner.py`)

```python
class SyncRunner:
    def __init__(self, connector: Connector, cfg: SourceSettings, ops: OpsStore): ...
    def run_incremental(self, entity: str) -> SyncResult: ...     # job kind "sync"
    def run_backfill(self, entity: str, start: datetime, end: datetime) -> SyncResult: ...   # job kind "sync"
    def run_reconcile(self, entity: str) -> SyncResult: ...       # job kind "reconcile"

@dataclass(frozen=True)
class SyncResult:
    source: str; entity: str; mode: Literal["incremental", "backfill", "reconcile"]
    rows: int; tombstones: int; skipped_deleted: int; files: tuple[Path, ...]
    watermark_before: str | None; watermark_after: str | None
```

The runner uses the lake writer from spec 02 §3.2 (`LakeWriter(source, entity, target_bytes=, max_open_s=)`, `write`, `commit() -> LakeFileSet`, `abort()`). It opens one `LakeWriter` per checkpoint. It writes batches until `checkpoint_rows` (default 500,000) or `max_open_s` is reached, calls `commit()`, advances the watermark from `LakeFileSet.max_source_updated_at`, and opens the next writer. File rotation within a writer (`target_bytes`, `max_open_s`, `dt` change) and temp naming `.<name>.parquet.tmp-<ulid>` belong to spec 02. On any exception the runner calls `abort()` and re-raises.

`SourceSettings` is the pydantic section model in `herness/connectors/settings.py` (spec 00 §3, spec 10).

CLI (spec 09 owns the command, this spec owns its behavior): `herness sync [SOURCE] [--entity E] [--backfill --from D --to D] [--reconcile] [--check-mapping]`, and `herness sync jira --discover-fields`.

### 3.3 Monitoring (`herness/connectors/monitoring/`)

One connector named `monitoring` with the entities `event` and `metric_daily`. This matches the raw entity names `monitoring/{event,metric_daily}` in spec 02 §3.1. It fans out to the enabled adapters, each implementing:

```python
class MonitoringAdapter(Protocol):          # registered as registry.register("monitoring_adapter", name)
    tool: str                               # "prometheus" | "datadog" | "splunk" | "dynatrace"
    def check(self) -> None: ...
    def events(self, since: datetime, until: datetime) -> Iterator[pa.RecordBatch]: ...
    def daily_metrics(self, since: datetime, until: datetime) -> Iterator[pa.RecordBatch]: ...
```

Modules: `base.py`, `prometheus.py` (Prometheus and Mimir), `datadog.py`, `splunk.py`, `dynatrace.py`. Every row carries a `source_tool` column, which becomes `core.event.source_tool` and `core.metric_daily.source_tool`.

## 4. Data contracts

### 4.1 Lake rows

Follow spec 02 §3.1. Connector rules:
- `_source_key` is the immutable key listed per source below. `_record_id = f"{_source}:{_entity}:{_source_key}"`.
- `_source_updated_at` is parsed from `watermark_field` into UTC. This is the only value a connector parses. A missing or unparseable value raises `SchemaViolation`.
- `_payload` is the record as received (JSON text; BSON in `bson.json_util` relaxed mode).
- Flattened fields: REST/JSON sources write every field as `string` (JSON scalar text; objects and arrays as JSON text). Arrow-native sources (Snowflake, Parquet files) keep their Arrow types. Reference fields become `<field>` and `<field>_display`.
- Tombstones: `_deleted = true`, `_payload = NULL`, flattened fields NULL, `_source_updated_at` = the source's deletion time, else the detection time.

### 4.2 Field mapping per source

What each connector must fetch for spec 02 §4.3. Staging owns typing and enums. "cf" = custom field from `mappings.yaml: custom_fields`.

**ServiceNow** (`_source` `servicenow`, `_source_key` = `sys_id`, watermark `sys_updated_on`)

| Raw entity | Source field → canonical column |
|---|---|
| `incident` → `core.incident` | `number` · `opened_at` · `resolved_at` · `closed_at` · `priority` · `state` (or `incident_state`) · `business_service`→service_id · `cmdb_ci`→ci_id · `assignment_group`→team_id · `reassignment_count` · `reopen_count` · `short_description` · `description` · `close_notes` · `close_code` · `problem_id` · `caused_by`→caused_by_change_id · `made_sla` (inverted)→sla_breached · `business_duration`→business_duration_s · cf `servicenow.customer_impact_minutes` · `sys_updated_on`→source_updated_at. `acknowledged_at`: no standard field (Q8) |
| `change_request` → `core.change` | `number` · `type` · `state` · `risk` · `opened_at` · `start_date`/`end_date`→planned_start/planned_end · `work_start`/`work_end`→actual_start/actual_end · `business_service` · `cmdb_ci` · `assignment_group` · `close_code`→outcome · `short_description` · `description` · `sys_updated_on` |
| `problem` → `core.problem` | `number` · `opened_at` · `resolved_at` · `state` (or `problem_state`) · `business_service` · `assignment_group` · `known_error` · `cause_notes`→root_cause_text · `sys_updated_on` |
| `cmdb_ci` → `core.service` | filtered by `sys_class_name` IN config list: `sys_id`→service_id · `name` · `sys_class_name`→ci_class · `owned_by`/`support_group`→business_owner_team_id · `cost_center` · `company`. `busines_criticality` (ServiceNow's spelling) exists only on `cmdb_ci_service` and is not returned by a `cmdb_ci` query (Q7) |
| `cmdb_rel_ci` → `core.service_map` | `parent` · `child` · `type` (+ `type_display`) |
| `sys_user_group` → `core.team`, `core.org` | `sys_id`→team_id · `name` · `parent`→org hierarchy · `cost_center` · `active` · `type` |

**Jira** (`_source` `jira`, raw entity `issue`, `_source_key` = numeric issue `id`, watermark `updated`)

| Column in the `issue` row | Source → canonical column |
|---|---|
| flattened fields | `id` · `key` · `fields.issuetype.name`→type · `fields.parent.key` (Cloud) / cf `jira.epic_link` (DC)→parent_key · `fields.project.key`→project · `fields.components[].name`→components/component · `fields.labels` · `fields.status.name`→status · `fields.status.statusCategory.key`→status_category · `fields.created` · `fields.resolutiondate`→resolved_at · cf `jira.story_points` · cf `jira.estimate_cost_usd` · cf `jira.team` · `fields.summary` · `fields.description` (ADF JSON on Cloud v3, wiki text on DC; staging extracts text) · `fields.updated` |
| `changelog` (JSON) → `core.work_item_transition` | the **complete** list of histories, each `{id, created, items[{field, from, fromString, to, toString}]}`. Staging keeps `field = 'status'`: `created`→at, `fromString`→from_status, `toString`→to_status. Categories come from `mappings.yaml: enums` status-name maps (Q9) |
| `issuelinks` (JSON) → `core.work_item_link` | `fields.issuelinks[]`: `type.name`→link_type, `inwardIssue.key`/`outwardIssue.key`→from_key/to_key |
| `remotelinks` (JSON) → `core.work_item_link` | list of `{id, object.url, object.title}`. Staging regexes INC/CHG/PRB numbers into `mentions_incident`. NULL when `fetch_remote_links` is off |

Every `issue` row carries complete `changelog` and `remotelinks` values, because staging keeps only the latest row per `record_id`.

**Monitoring** (`_source` `monitoring`)

| Raw entity | Lake columns → canonical column |
|---|---|
| `event` → `core.event` | `source_tool` · `event_key` (tool's event/problem id); `_source_key` = `<source_tool>:<event_key>` · `ts` · `service` (tag/label/entity name)→service_id via staging · `host` · `severity_raw`→severity · `title`→alert_name · `status` · `dedup_key` · `end_ts`→duration_s · `incident_ref`→incident_id |
| `metric_daily` → `core.metric_daily` | `source_tool` · `date` · `service` · `metric_name` · `value` · `unit`; `_source_key` = `<source_tool>\|<metric_name>\|<service>\|<date>`; `_source_updated_at` = end of the day bucket |

**MongoDB, Snowflake, Dataverse, Files**: config-defined entities (spec 02 §3.1). Each entity names `key_field`, `updated_field` and the fields to fetch. Staging files `150_stg_mongodb.sql`, `160_stg_snowflake.sql`, `170_stg_dataverse.sql` and `140_stg_files.sql` (spec 02 §4.1) decide which canonical table they feed. For example, a Snowflake cost-center table feeds `core.org.cost_center`, and a Dataverse project table feeds `core.work_item`. `herness sync --check-mapping` parses the staging SQL for referenced raw columns and fails if the config does not fetch one of them.

## 5. Behavior

### 5.1 Incremental sync (`run_incremental`)

1. `resilience.guard(source)` (spec 08). An open breaker raises `CircuitOpen`, and the job is rescheduled at `retry_at`.
2. Read `watermark(source, entity)`. `since = value - overlap_minutes` (default 30; ServiceNow and Jira 60). No watermark → run a backfill instead (§5.5).
3. `until = now_utc() - settle_seconds` (default 60).
4. Load the deletion set (§5.6). Iterate `connector.sync(entity, since, until)`, drop deleted `record_id`s, and write to the current `LakeWriter`. At each checkpoint: `commit()`, then set the watermark (§5.3).
5. At the end: final `commit()` and watermark update. Zero rows leaves the watermark unchanged.

The overlap re-fetches some records on every run. Duplicates collapse in staging dedupe (spec 02 §4.2), and late-committed updates are not lost.

### 5.2 Lake files

The runner does not write files directly. Paths, rotation, zstd, temp naming (`.<name>.parquet.tmp-<ulid>`), atomic rename and the staging glob `[!.]*.parquet` are defined in spec 02 §3. The runner adds three things:
- On start it deletes dot-prefixed `.tmp-*` files older than 1 hour under `data/raw/<source>/`. These are left over from crashes that bypassed `abort()`.
- Schema drift: when a batch has columns the writer has not seen, the runner commits the current writer and opens a new one, so each file has one schema. A REST field whose type changes is written as `string`. For Arrow-native sources, `union_by_name` in staging resolves the difference.
- Each drift event is logged as `schema_drift` with the added and removed column names.

### 5.3 Watermarks

- Stored in ops table `watermark` (`source`, `entity`, `field`, `value`, `updated_at`; spec 02 §5.1). `value` is the fixed-width UTC text of spec 00 §8. It is written only through `herness.store.ops.set_watermark()`.
- Invariant: `value` ≤ the `_source_updated_at` of every record not yet committed. Ascending order guarantees this once `commit()` returns. Equal timestamps that span a checkpoint are covered by the overlap.
- Monitoring has one watermark per entity for all adapters. It advances only when every enabled adapter finished the window. A failed adapter leaves it unchanged, and the others re-fetch the window harmlessly (Q10).
- Files do not use `watermark`. They use `file_ingest` (§5.9).
- `meta.build.source_watermarks` (spec 02 §4.7) is copied from this table by the build.

### 5.4 Tombstones and key reconciliation

- Sources that report deletes produce tombstones in the normal flow: ServiceNow `sys_audit_delete` when readable, and Dataverse change tracking when enabled (Q4).
- Every other entity has a weekly `reconcile` job (default Sunday 03:00 business timezone, before the D3 deep run):
  1. `list_keys(entity)` streams all current source keys through key-only queries: ServiceNow `sysparm_fields=sys_id`, Jira `fields=id`, Mongo projection `{_id: 1}`, Snowflake `SELECT <key>`, Dataverse `$select=<pk>`, monitoring not reconciled.
  2. The keys go to a temporary Parquet file in the scratch dir. DuckDB anti-joins the lake's latest non-deleted `_source_key` set (the staging dedupe rule) against it.
  3. Keys in the lake but missing from the source become tombstones, written through `LakeWriter`.
  4. Safety valve: if tombstones exceed `reconcile.max_delete_pct` (default 2 %) of live keys, nothing is written. The runner logs `reconcile_aborted` and raises `SchemaViolation`.

### 5.5 Initial backfill

- `run_backfill(entity, start, end)` splits `[start, end)` into slices of `backfill.slice_days` (default 30; 7 for ServiceNow `incident`) and runs up to `max_concurrency` slices in parallel. Each slice has its own `LakeWriter`.
- Slice state lives in ops table `sync_slice` (spec 02 §5.1): `pending → running → done | failed`, with `rows`, `files`, `attempts` and `last_error`. A restart skips `done` slices and reruns `running` and `failed` slices from their start. Re-fetched rows are harmless.
- When all slices are `done`, the watermark is set to `min(end, max committed _source_updated_at)`. `end` defaults to backfill start time, so updates during the backfill fall inside the first incremental overlap.
- `start` defaults to `backfill.start` (3 years ago, matching spec 02 §9).

### 5.6 Deletion requests

- Before each `sync`, `backfill` slice or `reconcile` run, the runner loads `herness.store.ops.deleted_record_ids(source, entity)`: the `record_id`s with a `deletion_request` in status `running` or `done`. The set is held in memory as a `pa.Array`.
- Every batch is filtered with `pc.invert(pc.is_in(batch["_record_id"], deleted))` before `LakeWriter.write`. Dropped rows count into `SyncResult.skipped_deleted` and are never logged by value.
- Reconciliation never writes tombstones or rows for those `record_id`s. Spec 10's procedure owns removing them from existing lake files.
- A request that moves to `running` during a sync is picked up at the next checkpoint, when the runner reloads the set. Spec 10 re-runs its lake purge after it marks the request `done`, which catches rows committed in that gap (Q11).

### 5.7 ServiceNow (`servicenow.py`)

- Endpoint: `GET {base_url}/api/now/table/{table}` with `sysparm_query`, `sysparm_fields`, `sysparm_limit`, `sysparm_offset`, `sysparm_display_value=all`, `sysparm_exclude_reference_link=true` and `sysparm_no_count=true`. With `display_value=all` each field is `{value, display_value}`, flattened to `<f>` and `<f>_display`. Date `value`s use the internal UTC format.
- Query: `sys_updated_on>={since}^sys_updated_on<{until}^ORDERBYsys_updated_on^ORDERBYsys_id` plus the optional config `filter`. Page with `sysparm_limit` (default 1,000; 500–2,000 recommended) and `sysparm_offset`. Follow `Link rel="next"` when present, and stop on an empty or short page ([Table API iteration guidance](https://www.servicenow.com/community/developer-forum/best-practice-for-iterating-through-a-table-with-the-table-api/m-p/3387123)).
- Offset drift: a record updated during paging moves beyond `until` and may shift offsets. Its new `sys_updated_on` ≥ `until`, so the next run gets it. Windows are cut to `window_hours` (default 24) to keep offsets shallow.
- Rate limits: a 429 carries `Retry-After`, `X-RateLimit-Limit`, `X-RateLimit-Reset` and `X-RateLimit-Rule` ([ServiceNow rate limits](https://www.servicenow.com/community/developer-articles/understanding-servicenow-rest-api-rate-limits-key-concepts-amp/ta-p/3407367)).
- Auth methods: `oauth_client_credentials` (preferred when the instance release supports it), `oauth_password`, `basic`. The integration user's timezone must be UTC, because encoded-query date literals use the user's timezone.

### 5.8 Jira (`jira.py`, `flavor: cloud | datacenter`)

- **Cloud search**: `POST {base_url}/rest/api/3/search/jql` with body `{jql, fields, maxResults, nextPageToken}`. The response has `issues`, `nextPageToken` and `isLast`. The legacy `/rest/api/3/search` (`startAt`) has been removed ([migration guide](https://community.atlassian.com/forums/Jira-articles/Avoiding-Pitfalls-A-Guide-to-Smooth-Migration-to-Enhanced-JQL/ba-p/2985433); [API reference](https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-search/)). Request `maxResults=100`, accept smaller pages, and loop until `isLast` is true.
- **Data Center search**: `POST {base_url}/rest/api/2/search` with `startAt`, `maxResults` and `total`. The page size is capped server-side (`jira.search.views.default.max`). Use `startAt += len(issues)`.
- JQL: `updated >= "{since:yyyy/MM/dd HH:mm}" AND updated < "{until}" [AND {jql_scope}] ORDER BY updated ASC, id ASC`. JQL has minute precision and uses the service account's timezone, which must be UTC. The 60-minute overlap covers truncation and Cloud search's eventual consistency.
- Fields: the explicit list from §4.2 plus configured custom field IDs. Never `*all`.
- **Changelog, Cloud**: `expand=changelog` is capped at about 40 histories per issue. For every search page the connector calls `POST /rest/api/3/changelog/bulkfetch` with `{issueIdsOrKeys (≤ 1000), maxResults, nextPageToken}` and stores the complete result in the `changelog` column ([bulk changelog](https://community.developer.atlassian.com/t/bulk-fetch-changelogs-experimental-api/87240)). On 404 (the endpoint is experimental) it falls back to `GET /rest/api/3/issue/{id}/changelog` paged with `startAt`.
- **Changelog, DC**: `expand=changelog` on search. If `changelog.total > changelog.maxResults`, it refetches via `GET /rest/api/2/issue/{id}?expand=changelog`.
- Remote links: when `fetch_remote_links: true`, the connector calls `GET /rest/api/{3|2}/issue/{id}/remotelink` for every issue it writes, including during backfill. That is one call per issue (§8).
- **Custom field discovery** (`--discover-fields`): `GET /rest/api/{3|2}/field`. It prints candidate IDs by name and schema (`Story Points`, `Story point estimate`, `Team`, `Epic Link`, number-typed cost fields) for the admin to copy into `mappings.yaml: custom_fields`. It writes nothing.
- Rate limits: Cloud returns 429 with `Retry-After` and `X-RateLimit-*` under a points model ([Jira Cloud rate limiting](https://developer.atlassian.com/cloud/jira/platform/rate-limiting/)). DC admin rate limiting also returns 429 with `Retry-After`.
- Auth methods: Cloud `api_token` (email + token, basic) or OAuth 2.0 (3LO). DC personal access token (Bearer).

### 5.9 Other sources

**Monitoring: Prometheus / Mimir** (`prometheus.py`): `GET {base_url}/api/v1/query_range?query=...&start=...&end=...&step=1d`, one request per `metric_queries[]` item. Each expression must aggregate to a daily value with a `service` label, for example `sum by (service)(increase(http_requests_total[1d]))`. At `step=1d`, 3 years is about 1,100 points, far below the 11,000-points-per-series limit ([Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)). Mimir adds the `X-Scope-OrgID` header and the `/prometheus` path prefix. `events()` yields nothing because there is no alert history API. Alert load arrives as a daily metric `alert_firing_minutes` from `ALERTS{alertstate="firing"}`.

**Monitoring: Datadog** (`datadog.py`): events via `POST /api/v2/events/search` with `filter.from`, `filter.to`, `sort=timestamp`, `page.limit` and cursor `page.cursor` = previous `meta.page.after` ([Datadog search events](https://docs.datadoghq.com/api/latest/events/search-events/)). Metrics via `GET /api/v1/query?from&to&query=<agg>:<metric>{*} by {service}.rollup(<agg>, 86400)`. Headers `DD-API-KEY` and `DD-APPLICATION-KEY`. On 429, `X-RateLimit-Reset` gives the delay.

**Monitoring: Splunk** (`splunk.py`): `POST {base_url}/services/search/v2/jobs/export` with `search`, `earliest_time`, `latest_time` and `output_mode=json`, streamed one JSON result per line ([Splunk export](https://help.splunk.com/en/splunk-enterprise/search/search-manual/10.4/export-search-results/export-data-using-the-splunk-rest-api)). SPL must aggregate: metric queries end with `| bin _time span=1d | stats ... by _time, service`, and event queries return alert or notable rows. Config validation rejects SPL without `stats`, `tstats` or `table` with `ConfigError`.

**Monitoring: Dynatrace** (`dynatrace.py`): events via `GET /api/v2/problems?from&to&pageSize=500`, then `nextPageKey` alone on later calls. Metrics via `GET /api/v2/metrics/query?metricSelector=...&resolution=1d&from&to`, which is not paginated ([Dynatrace community](https://community.dynatrace.com/t5/Dynatrace-API/Pagination-not-working-and-nextPageKey-is-null-even-after/m-p/189337)). Header `Authorization: Api-Token <token>`. Problems are re-fetched across the overlap, so OPEN→CLOSED changes land as new versions.

**MongoDB** (`mongodb.py`): `find({updated_field: {"$gte": since, "$lt": until}, **filter}, projection).sort([(updated_field, 1), ("_id", 1)]).batch_size(page_size)`, with read preference `secondaryPreferred` and `maxTimeMS`. `check()` warns when no index starts with `updated_field`. `_source_key = str(_id)`. Change streams are out of scope for v1.

**Snowflake** (`snowflake.py`): `SELECT <columns> FROM <table> WHERE <updated_col> >= %(since)s AND <updated_col> < %(until)s [AND filter] ORDER BY <updated_col>, <key>`, read with `cursor.fetch_arrow_batches()`. The session sets `QUERY_TAG='herness:<entity>'` and `STATEMENT_TIMEOUT_IN_SECONDS`. Use a dedicated XS warehouse with `AUTO_SUSPEND=60`. Cost guard: run `EXPLAIN USING JSON` first and abort with `ConfigError` if `GlobalStats.bytesAssigned > max_scan_gb`. `check()` requires a resource monitor on the warehouse.

**Dataverse** (`dataverse.py`): `GET {base_url}/api/data/v9.2/{entityset}?$select=...&$filter=modifiedon ge {since} and modifiedon lt {until}&$orderby=modifiedon asc,{pk} asc` with header `Prefer: odata.maxpagesize=5000` (the maximum, repeated on every page). Follow `@odata.nextLink` unmodified until it is absent, and never use `$skip` or `$top` ([Dataverse paging](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/webapi/query/page-results)). Add `odata.include-annotations="OData.Community.Display.V1.FormattedValue"` for `<f>_display`. Auth: `msal.ConfidentialClientApplication(...).acquire_token_for_client(scopes=[f"{base_url}/.default"])`, cached until 5 minutes before expiry. Service protection limits are 6,000 requests, 20 min of execution time and 52 concurrent requests per user per 5 minutes, and 429 responses carry `Retry-After` in seconds ([Dataverse API limits](https://learn.microsoft.com/en-us/power-apps/developer/data-platform/api-limits)).

### 5.10 Files (`files.py`)

- Drop folder `data/inbox/<entity>/` (spec 00 §4). Readers: DuckDB `read_csv(path, all_varchar=true, header=true)`, `read_xlsx(path, all_varchar=true, sheet=...)` and `read_parquet(path)`, returned as Arrow.
- Fingerprint = SHA-256 of the file bytes, stored in `file_ingest` (spec 02 §5.1) with `path`, `size_bytes`, `mtime`, `rows` and `files`. A known fingerprint is skipped even if the file was renamed. A changed file is ingested as new rows.
- `_source_key` = configured `key_field` (a list is joined with `|`). `_source_updated_at` = `updated_field` if configured, else the file mtime.
- `mode: delta` appends rows. `mode: snapshot` treats the file as the full entity and reconciles its keys (§5.4) right after ingest.
- Inbox files are never moved or deleted by the connector.

## 6. Errors and resilience

| Condition | Raised | Notes |
|---|---|---|
| Connect error, timeout, 5xx, Mongo `AutoReconnect`, Snowflake network error | `SourceUnavailable` | |
| 429 or Snowflake queueing timeout | `RateLimited(retry_after)` | From `Retry-After` (seconds or HTTP date), else `X-RateLimit-Reset`, else `None` |
| Breaker open for the source | `CircuitOpen` | Raised by `resilience.guard`. The job is rescheduled, not failed |
| 401, 403, MSAL error, bad key-pair | `AuthError` | Fatal. Spec 08 opens the breaker |
| Missing key or watermark field, unparseable timestamp, reconcile safety valve, unexpected response shape | `SchemaViolation` | Fatal for this entity's run. Watermark unchanged |
| Bad config, forbidden SPL, Snowflake scan guard, unresolved `secret:` reference | `ConfigError` | |

- Retries run at page level through spec 08's retry policy (`config/resilience.yaml`), with the wait at least `retry_after` when present. Page cursors (offset, `nextPageToken`, `nextLink`, `nextPageKey`, Mongo last key) stay inside the generator, so a retry resumes at the failed page.
- The breaker key is the connector name (`source_health.source`, spec 02 §5.1 convention). Monitoring adapters use one key per tool, `monitoring:<tool>`, for breakers and watermarks (spec 02 §5.1), so one failing tool does not stall or trip the others.
- A crash leaves at most uncommitted temp files and a watermark that is behind. A rerun is always safe.
- One failing entity does not stop the source's other entities.

## 7. Configuration (`config/sources.yaml`, key `sources`)

`config/sources.yaml` is owned by 01 (key `sources`) and 02 (keys `dq`, `build`) (spec 00 §11). Secrets are `secret:<name>` references, resolved by spec 10 (`herness/core/secrets.py`). Plain-text credentials fail validation.

```yaml
sources:
  servicenow:
    enabled: true
    base_url: https://acme.service-now.com
    auth: {method: oauth_client_credentials, credentials: "secret:servicenow_oauth"}
    page_size: 1000
    overlap_minutes: 60
    max_concurrency: 4
    schedule: "*/30 * * * *"                  # cron, business timezone; spec 08 enqueues job kind=sync
    reconcile: {schedule: "0 3 * * SUN", max_delete_pct: 2.0}   # job kind=reconcile
    backfill: {start: "2023-09-01", slice_days: 30}
    entities:
      incident:
        fields: [number, opened_at, resolved_at, closed_at, priority, state, business_service, cmdb_ci,
                 assignment_group, reassignment_count, reopen_count, short_description, description,
                 close_notes, close_code, problem_id, caused_by, made_sla, business_duration, u_impact_minutes]
        window_hours: 24
        backfill: {slice_days: 7}
      change_request: {fields: [...]}
      cmdb_ci: {fields: [...], classes: [cmdb_ci_appl, cmdb_ci_service, cmdb_ci_service_auto]}
  jira:
    enabled: true
    flavor: cloud                             # cloud | datacenter
    base_url: https://acme.atlassian.net
    auth: {method: api_token, credentials: "secret:jira_api_token"}
    page_size: 100
    overlap_minutes: 60
    max_concurrency: 2
    schedule: "15 * * * *"
    jql_scope: "project in (PAY, OPS, PLAT)"
    fetch_remote_links: true
    entities: {issue: {}}
  monitoring:
    enabled: true
    overlap_minutes: 120
    schedule: "0 2 * * *"
    adapters:
      datadog:
        enabled: true
        base_url: https://api.datadoghq.eu
        auth: {method: api_and_app_key, credentials: "secret:datadog_keys"}
        page_size: 1000
        event_query: "source:monitor status:(error OR warning)"
        metric_queries:
          - {name: error_rate, query: "avg:trace.http.request.errors{*} by {service}", agg: avg, unit: ratio}
      prometheus:
        enabled: false
        base_url: https://mimir.acme.local/prometheus
        tenant: ops
        auth: {method: bearer, credentials: "secret:mimir_read"}
        metric_queries:
          - {name: request_count, query: "sum by (service)(increase(http_requests_total[1d]))", unit: count}
  snowflake:
    enabled: false
    account: acme-xy12345
    auth: {method: key_pair, credentials: "secret:snowflake_svc"}
    warehouse: HERNESS_XS
    role: HERNESS_READER
    statement_timeout_s: 900
    max_scan_gb: 50
    entities:
      cost_center: {table: FINANCE.PUBLIC.COST_CENTER, key_field: CC_ID, updated_field: UPDATED_AT,
                    columns: [CC_ID, NAME, ORG_UNIT, UPDATED_AT]}
  dataverse:
    enabled: false
    base_url: https://acme.crm.dynamics.com
    auth: {method: msal_client_credentials, tenant_id: "<guid>", credentials: "secret:dataverse_app"}
    page_size: 5000
    entities:
      msdyn_project: {entityset: msdyn_projects, key_field: msdyn_projectid, updated_field: modifiedon,
                      select: [msdyn_subject, msdyn_projectmanager, msdyn_scheduledstart, modifiedon]}
  files:
    enabled: true
    inbox: data/inbox
    schedule: "*/10 * * * *"
    entities:
      team_roster: {pattern: "*.xlsx", sheet: Teams, key_field: [team_code], mode: snapshot}
      legacy_incidents: {pattern: "*.csv", key_field: [ticket_id], updated_field: last_modified, mode: delta}
```

Common keys, validated by `SourceSettings`: `enabled`, `base_url`, `auth.method`, `auth.credentials` (`secret:<name>`), `page_size`, `batch_rows` (10,000), `checkpoint_rows` (500,000), `overlap_minutes`, `settle_seconds` (60), `max_concurrency`, `schedule`, `reconcile.*`, `backfill.*`, `timeout_s` (60), `verify` (CA bundle path), `entities`. Unknown keys raise `ConfigError`. Monitoring `metric_queries[].name` must be a configured `core.metric_daily.metric_name`. Lake rotation settings belong to spec 02's `LakeWriter` defaults.

## 8. Performance targets

Reference PC as in spec 02 §9. Rates are sustained committed lake rows per second. Remote rates depend on the source's limits.

| Source | Per stream | Max concurrency (default / cap) | 3-year backfill reference |
|---|---|---|---|
| ServiceNow | 300–800 rows/s | 4 / 8 (agree with instance owner) | 5M incidents in < 3 h |
| Jira Cloud | 150 issues/s search; bulk changelog +50/s; remote links ~20 issues/s | 2 / 4 | 200k issues in < 1 h, plus < 3 h with remote links |
| Jira DC | 300 issues/s | 2 / 4 | |
| Prometheus/Mimir, Dynatrace metrics | 1 query per metric per year slice | 4 | 3 years × 50 metrics in < 10 min |
| Datadog / Dynatrace events | 500 events/s | 2 | 2M events in < 1.5 h |
| Splunk export | 2,000 rows/s | 2 | |
| MongoDB | 20,000 docs/s | 4 | |
| Snowflake | 100,000 rows/s | 2 | |
| Dataverse | 1,500 rows/s | 4 (limit 52) | |
| Files CSV / XLSX / Parquet | 200k / 20k / 1M rows/s | 1 per entity | |

- An incremental run over all enabled sources at about 300k new records per month finishes in < 5 min.
- Connector process RSS stays < 1.5 GB. Deletion-set filtering adds < 5 % at 100k deleted IDs.

## 9. Security

- Each source uses a dedicated, read-only, non-interactive identity. Secrets are referenced as `secret:<name>` and never appear in logs, traces, lake files or exception text. The spec 10 scrubber also removes known secret values.
- Minimum grants:

| Source | Identity and scope |
|---|---|
| ServiceNow | Integration user marked "Web service access only". `snc_read_only` plus `itil` (task tables) and `cmdb_read`. OAuth client limited to the Table API. Optional read on `sys_audit_delete`. Timezone UTC |
| Jira Cloud | Service account with Browse Projects on in-scope projects. API token, or OAuth scopes `read:jira-work` and `read:jira-user`. Timezone UTC |
| Jira DC | PAT of a user with Browse Projects in scope. Timezone UTC |
| Datadog | Application key scoped to `events_read` and `timeseries_query` |
| Splunk | Token for a role with `search`, `srchIndexesAllowed` limited to configured indexes, and a low `srchJobsQuota` |
| Dynatrace | API token with `problems.read` and `metrics.read` |
| Prometheus/Mimir | Read path only, with the read tenant |
| MongoDB | `read` role on the configured database |
| Snowflake | Key-pair user. Role with `USAGE` on warehouse, database and schema and `SELECT` on configured tables only |
| Dataverse | Entra app registration plus a Dataverse application user with a custom role granting organization-level Read on configured tables only |

- TLS verification is always on. Only `verify: <ca_bundle_path>` is allowed.
- `data/raw/` and `data/inbox/` hold raw ticket text and personal data, and get the warehouse's folder ACLs (spec 10). Deletion requests are honored at ingest (§5.6).

## 10. Tests and acceptance criteria

Live-API tests use `respx` fixtures recorded from sandboxes and scrubbed of personal data (`tests/fixtures/connectors/<source>/`). Mongo tests use `mongomock`. Snowflake tests use a fake cursor that returns Arrow batches.

| Test | Acceptance |
|---|---|
| Pagination per source: empty result, exactly one full page, short last page, end of `Link`/`nextPageToken`/`nextLink`/`nextPageKey`/`meta.page.after` | Row count equals the fixture. No duplicate or missed page |
| Jira Cloud page smaller than `maxResults` with `isLast: false` | Loop continues until `isLast: true` |
| Bulk changelog 404; issue with more than 40 histories | Per-issue fallback. The `changelog` column is complete |
| Watermark overlap: a record updated inside the overlap after the last run | Appears in the lake. Staging keeps one row |
| Crash between `commit()` and watermark write (fault injection) | Rerun gives a superset. After dedupe, `core.*` equals the no-crash result |
| Orphan temp files older than 1 h | Deleted by the runner and never read by the build |
| Tombstones: reported delete, reconcile delete, reconcile above `max_delete_pct` | Written with `_deleted=true` and removed in staging. The safety valve writes nothing and raises `SchemaViolation` |
| Deletion request `running`/`done` for a fetched `record_id` | Row not written. `skipped_deleted` = 1. Reconcile writes no tombstone for it. `pending` requests do not filter |
| 429 with `Retry-After: 7` and with an HTTP date | `RateLimited.retry_after` is correct. Only the page is retried |
| 401 | `AuthError`, no retry. Breaker opened via spec 08 |
| Open breaker | `CircuitOpen`. Job rescheduled. No HTTP calls made |
| Schema drift: new field, removed field, type change | New writer started, build succeeds, `schema_drift` logged |
| Resumable backfill: kill after 3 of 10 slices | Restart runs 7 slices. Final watermark is correct |
| ServiceNow offset drift mid-paging | Record picked up on the next run |
| Monitoring: one adapter fails | Other adapters' rows committed. Watermark unchanged |
| Files: renamed re-drop, modified file, snapshot missing a key | Skipped / ingested / tombstoned |
| Guards: Prometheus `step < 1d`, SPL without aggregation, plain-text credential | `ConfigError` at config load |
| Throughput | 1M-row ServiceNow fixture replay reaches ≥ 5,000 rows/s locally |

## 11. Open questions

- Q1: Does the ServiceNow instance allow OAuth client credentials, and which inbound rate-limit rule applies? This sets `max_concurrency`.
- Q2: Is the Jira bulk changelog endpoint available on the tenant? Confirm in Phase 6.
- Q3: Which monitoring tools are in use, and which metrics define `availability_pct` per tool?
- Q4: Is Dataverse change tracking enabled on the needed tables? If so, use delta links for true deletes.
- Q5: A tombstoned record restored in the source with an older `sys_updated_on` stays deleted after dedupe. Should reconciliation bump `_source_updated_at` on re-fetch? This needs spec 02 agreement.
- Q6: Is `customer_impact_minutes` a ServiceNow custom field, or derived from `cmdb_ci_outage`?
- Q7: Resolved (02 v2 §3.1): `cmdb_ci_service`, `cmn_department` and `task_sla` are confirmed raw entities, so `core.service.criticality`, `core.org` and `sla_breached` have sources.
- Q8: Resolved (02 v2 §8): `acknowledged_at` comes from the optional `mappings.yaml: custom_fields.servicenow.acknowledged_at`; when absent it stays NULL and MTTA is disabled. Which field the instance uses is part of Q6's field survey (Phase 6).
- Q9: Resolved (02 v2 §8): status-name → category maps live in `mappings.yaml: enums.jira.status_category`.
- Q10: Resolved (02 v2 §5.1): monitoring watermarks and breakers are keyed per tool (`monitoring:<tool>`).
- Q11: Resolved (10 v2 §5.5): the deletion procedure runs a second lake pass after marking `done` (step 7).

## 12. Dependencies

- Specs: 00 (protocols, IDs, errors, logging, config ownership), 02 (lake contract and `LakeWriter`, `watermark`, `sync_slice`, `file_ingest`, `deletion_request`, staging), 08 (retry policy, `guard`, `source_health`, `sync`/`reconcile` jobs), 10 (secret resolution, deletion procedure, folder ACLs), 09 (CLI wiring), 11 (fixtures, synthetic lakes).
- Packages (spec 00 §9): `httpx`, `tenacity`, `pyarrow`, `duckdb` (excel extension), `pymongo`, `snowflake-connector-python[pandas]`, `msal`; tests `respx`, `freezegun`, `mongomock`.

## 13. Contract changes (resolved)

- C1 `sync(..., until)` and `SupportsKeyListing.list_keys`: now in spec 00 §6.
- C2 `sync_slice` and `file_ingest` ops tables: now in spec 02 §5.1 (owner 01).
- C3 inbox folder: now in spec 00 §4 (`data/inbox/<entity>/`).
- C4 lake writer API and temp naming: now in spec 02 §3.2 (`LakeWriter`, `LakeFileSet`, `.<name>.parquet.tmp-<ulid>`). This spec only uses it (§3.2).
- C5 Jira `source_key` = issue `id`: now in spec 00 §5 and spec 02 §3.1, §4.3.
- C6 staging slots `150_stg_mongodb.sql`, `160_stg_snowflake.sql`, `170_stg_dataverse.sql`: now in spec 02 §4.1.
- C7 tombstone `_payload` NULL and `_source_updated_at` = deletion or detection time: now in spec 02 §3.1.
- C8 `mongomock` and `snowflake-connector-python[pandas]`: now in spec 00 §9.
- C9 `custom_fields.servicenow.customer_impact_minutes` and `custom_fields.jira.epic_link`: now in spec 02 §8.
