# 02 — Data Model: Raw Lake, Warehouse, Ops Store

Status: Draft v2 · 2026-09-24 · Depends on: 00. Phase 1.

v2 folds in the table and column requests from specs 01, 03, 04, 05, 06, 07, 08, 09, 10 and 11.

## 1. Purpose and scope

This spec defines every persistent table in Herness and the build that turns raw source records into the canonical model. It is the single source of truth for table and column names. Other specs own the logic that fills a table (the "Owner" column) but must not add or rename columns without changing this file.

In scope: raw lake contract and writer, warehouse schemas and build, ops store schema, vector tables, data quality checks, retention.
Out of scope: how connectors fetch data (01), how labels are produced (03), metric formulas (04).

## 2. Responsibilities

- Define the raw lake record contract and provide the lake writer every connector uses.
- Build a new warehouse file from the lake with deterministic, versioned SQL (`herness build`).
- Deduplicate, type and normalize source records into canonical tables.
- Resolve entity links (service ↔ team ↔ Jira project ↔ org) into `core.service_map`.
- Run data quality checks and refuse to promote a build that fails them.
- Create and migrate the ops store (SQLite) and vector tables.

## 3. Raw lake

### 3.1 Contract

Path: `data/raw/<source>/<entity>/dt=YYYY-MM-DD/part-<ulid>.parquet`, zstd, 64–256 MB target, Hive partitioning on `dt` (fetch date, UTC).

Required metadata columns:

| Column | Type | Meaning |
|--------|------|---------|
| `_record_id` | VARCHAR | `<source>:<entity>:<source_key>` (spec 00 §5; Jira uses issue `id`) |
| `_source` | VARCHAR | connector name |
| `_entity` | VARCHAR | source entity name |
| `_source_key` | VARCHAR | immutable key in the source |
| `_source_updated_at` | TIMESTAMPTZ | last-modified time; for tombstones the deletion or detection time |
| `_fetched_at` | TIMESTAMPTZ | when fetched |
| `_deleted` | BOOLEAN | tombstone |
| `_payload` | VARCHAR (JSON) | full source record, unmodified; NULL for tombstones |

Plus the entity's fields flattened one level: nested objects as JSON strings, reference fields split into `<field>` and `<field>_display` when both exist, names in `snake_case`, values not re-typed. New fields may appear at any time (`union_by_name = true`). The lake is append-only.

Raw entity names (confirmed with spec 11's generator): `servicenow/{incident,change_request,problem,cmdb_ci,cmdb_ci_service,cmdb_rel_ci,sys_user_group,cmn_department,task_sla}`, `jira/issue` (with `changelog`, `issuelinks`, `remotelinks` as JSON columns), `monitoring/{event,metric_daily}`, `files/<entity>`, and config-defined entities for `mongodb`, `snowflake`, `dataverse`.

### 3.2 Lake writer (`herness/store/lake.py`)

```python
class LakeWriter:
    def __init__(self, source: str, entity: str, *, target_bytes: int = 128 * 2**20,
                 max_open_s: int = 600) -> None: ...
    def write(self, batch: pa.RecordBatch) -> None: ...        # validates metadata columns, buffers
    def commit(self) -> LakeFileSet: ...                       # flushes, renames, returns committed files
    def abort(self) -> None: ...                               # deletes temp files

@dataclass(frozen=True)
class LakeFileSet:
    files: tuple[Path, ...]; rows: int; max_source_updated_at: datetime | None
```

Files are written as `.<name>.parquet.tmp-<ulid>` in the target partition and renamed on commit, so readers never see partial files. A file rotates at `target_bytes`, after `max_open_s`, or when `dt` changes. Build readers ignore dot-prefixed files. Spec 01's sync runner advances the watermark only after `commit()` returns.

## 4. Warehouse (DuckDB)

One file per build (spec 00 §4). Schemas: `stg`, `core`, `enrich`, `metrics`, `score`, `meta`.

### 4.1 Build pipeline

`herness build` runs SQL files from `herness/model/sql/` in lexical order inside one DuckDB connection:

| Range | Stage | Files |
|-------|-------|-------|
| `000–099` | setup, macros | `000_settings.sql`, `010_macros.sql` |
| `100–199` | staging per source | `110_stg_servicenow.sql`, `120_stg_jira.sql`, `130_stg_monitoring.sql`, `140_stg_files.sql`, `150_stg_mongodb.sql`, `160_stg_snowflake.sql`, `170_stg_dataverse.sql` |
| `200–299` | canonical core | `200_org_team_service.sql`, `220_service_map.sql`, `230_incident.sql`, `240_change.sql`, `250_problem.sql`, `260_event.sql`, `270_metric_daily.sql`, `280_work_item.sql` |
| `300–399` | enrichment attach | `300_attach_decisions.sql` (also fills `core.incident.content_hash`), `310_attach_clusters.sql` |
| `400–499` | metric facts (spec 04) | `400_facts.sql`, executed through the Python hook `herness.metrics.facts.materialize_facts(con, build_id)` so each statement is recorded in `meta.evidence` (producer `facts`) |
| `900–999` | data quality | `900_dq_checks.sql` |

Files are Jinja-templated for config values only. No business logic in Python except enrichment (03) and scoring (04), which run between stages.

Nightly `build_pipeline` job (spec 08), in one process: 000–299 → enrichment (03) → 300–399 → 400–499 → scoring (04) → 900–999 → promote. Any failure leaves `CURRENT` unchanged.

### 4.2 Staging rules (`stg`)

- Read with `read_parquet('data/raw/<source>/<entity>/**/[!.]*.parquet', hive_partitioning = true, union_by_name = true)`.
- Deduplicate: `QUALIFY row_number() OVER (PARTITION BY _record_id ORDER BY _source_updated_at DESC, _fetched_at DESC) = 1`.
- Drop rows whose latest version has `_deleted = true`, and rows whose `record_id` has a `deletion_request` in status `running` or `done` (ops store attached read-only).
- Cast with `TRY_CAST`; failures counted per column into `meta.dq_result`.
- Map source enums with macros configured in `config/mappings.yaml: enums`.

### 4.3 Canonical tables (`core`, owner: this spec)

**`core.org`** (from `cmn_department`, falling back to the group hierarchy): `org_id PK`, `name`, `parent_org_id`, `cost_center`, `source`.

**`core.team`**: `team_id PK`, `name`, `org_id`, `source`, `active BOOLEAN`.

**`core.service`** (from `cmdb_ci_service`, criticality from its `busines_criticality` field): `service_id PK`, `name`, `ci_class`, `criticality SMALLINT` (1 highest … 4), `business_owner_team_id`, `org_id`, `source`.

**`core.service_map`**: `service_id`, `team_id`, `jira_project`, `jira_component`, `org_id`, `role` (`owner` \| `support` \| `delivery`), `link_source` (`cmdb` \| `override` \| `suggested_approved`), `confidence DOUBLE`. Resolution order: `mappings.yaml` overrides → CMDB relationships → approved `mapping_suggestion` review items (spec 03).

**`core.incident`**

| Column | Type | Notes |
|--------|------|-------|
| `record_id` | VARCHAR PK | |
| `number` | VARCHAR | |
| `opened_at`, `acknowledged_at`, `resolved_at`, `closed_at` | TIMESTAMPTZ | `acknowledged_at` nullable (MTTA) |
| `priority` | SMALLINT | 1–5 |
| `state` | VARCHAR | `open`, `in_progress`, `on_hold`, `resolved`, `closed`, `canceled` |
| `service_id`, `ci_id`, `team_id` | VARCHAR | `team_id` = assignment group at resolution |
| `reassignment_count`, `reopen_count` | INTEGER | |
| `short_description`, `description`, `close_notes` | VARCHAR | raw text; never exposed to agents or UI (spec 05, 09) |
| `close_code`, `problem_id`, `caused_by_change_id` | VARCHAR | |
| `sla_breached` | BOOLEAN | from `task_sla.has_breached` when present, else incident `made_sla` |
| `business_duration_s` | BIGINT | |
| `customer_impact_minutes` | DOUBLE | from `mappings.yaml: custom_fields.servicenow.customer_impact_minutes`; never estimated by a model |
| `content_hash` | VARCHAR | filled in `300_attach_decisions.sql` from `enrich.text_redacted` |
| `source_updated_at` | TIMESTAMPTZ | |

**`core.change`**: `record_id PK`, `number`, `type` (`standard` \| `normal` \| `emergency`), `state`, `risk`, `opened_at` (nullable, lead time), `planned_start`, `planned_end`, `actual_start`, `actual_end`, `service_id`, `ci_id`, `team_id`, `outcome` (`successful` \| `successful_with_issues` \| `unsuccessful` \| `backed_out` \| `canceled` \| NULL), `short_description`, `description`, `source_updated_at`.

**`core.problem`**: `record_id PK`, `number`, `opened_at`, `resolved_at`, `state`, `service_id`, `team_id`, `known_error BOOLEAN`, `root_cause_text`, `source_updated_at`.

**`core.event`**: `event_id PK`, `source_tool`, `ts`, `service_id`, `host`, `severity` (`critical` \| `major` \| `minor` \| `warning` \| `info`), `alert_name`, `status`, `dedup_key`, `duration_s`, `incident_id`.

**`core.metric_daily`**: `date`, `service_id`, `metric_name`, `value DOUBLE`, `unit`, `source_tool`; PK (`date`, `service_id`, `metric_name`, `source_tool`).

**`core.work_item`**: `record_id PK` (Jira issue id based), `key`, `type` (`initiative` \| `epic` \| `feature` \| `story` \| `bug` \| `task` \| `subtask`), `parent_key`, `project`, `component`, `components VARCHAR[]`, `labels VARCHAR[]`, `status`, `status_category` (`todo` \| `in_progress` \| `done`), `created_at`, `resolved_at`, `story_points DOUBLE`, `estimate_cost_usd DECIMAL(18,2)`, `team_id`, `service_id`, `summary`, `description`, `source_updated_at`. Parent links on Data Center come from `custom_fields.jira.epic_link`.

**`core.work_item_transition`**: `record_id`, `from_status`, `to_status`, `from_category`, `to_category`, `at`.

**`core.work_item_link`**: `from_key`, `to_key`, `link_type` (Jira link types plus `mentions_incident`).

### 4.4 Enrichment tables (`enrich`, owner: spec 03)

| Table | Columns |
|-------|---------|
| `enrich.text_redacted` | `record_id`, `entity`, `text`, `content_hash` |
| `enrich.decision` | `record_id`, `question`, `answer`, `probability DOUBLE`, `agreement DOUBLE` (nullable, ensemble), `decider`, `decider_version`, `question_set_version`, `content_hash`, `decided_at`, `escalated BOOLEAN`, `review_status` (`none` \| `pending` \| `confirmed` \| `corrected`) |
| `enrich.decision_wide` | view: one row per `record_id`, column per question plus `<question>_p` |
| `enrich.cluster` | `cluster_id` (`cl_<ulid>`), `label`, `root_cause_category`, `size`, `first_seen`, `last_seen`, `top_terms VARCHAR[]`, `service_ids VARCHAR[]`, `algorithm_version` |
| `enrich.cluster_member` | `record_id`, `cluster_id`, `membership_prob DOUBLE` |
| `enrich.incident_change_link` | `incident_id`, `change_id`, `method` (`source_field` \| `time_ci_window` \| `decider`), `score DOUBLE` |

### 4.5 Metrics (`metrics`, owner: spec 04)

| Table | Columns |
|-------|---------|
| `metrics.incident_fact` | `record_id`, `number`, `opened_at`, `resolved_at`, `priority`, `service_id`, `team_id`, `org_id`, `criticality`, `cluster_id`, `membership_prob`, `excluded`, `resolve_h`, `resolve_bh`, `impact_h`, `impact_estimated`, `toil_h`, `downtime_usd`, `toil_usd`, `total_usd`, `is_repeat`, `is_reopened`, `is_reassigned`, `sla_breached`, `change_caused`, `query_id` |
| `metrics.change_fact` | `record_id`, `type`, `service_id`, `team_id`, `org_id`, `actual_end`, `deployed`, `failed`, `linked_incident_count`, `lead_time_h`, `query_id` |
| `metrics.work_item_fact` | `record_id`, `key`, `type`, `parent_key`, `status_category`, `service_id`, `team_id`, `org_id`, `story_points`, `created_at`, `first_in_progress_at`, `done_at`, `cycle_days`, `is_unplanned`, `query_id` |
| `metrics.org_closure` | `org_id`, `ancestor_org_id`, `depth` |
| `metrics.work_item_closure` | `record_id`, `ancestor_record_id`, `candidate_record_id`, `depth` |
| `metrics.metric_value` | `metric`, `entity_type` (`service` \| `team` \| `org` \| `work_item` \| `cluster`), `entity_id`, `period` (`week` \| `month` \| `quarter` \| `t12w` \| `t12m`), `period_start DATE`, `value DOUBLE`, `numerator DOUBLE`, `denominator DOUBLE`, `sample_size BIGINT`, `unit`, `flags VARCHAR[]`, `query_id` |

Money columns are `DECIMAL(18,2)`; column types for facts are in spec 04 §4.2.

### 4.6 Scores (`score`, owner: spec 04)

| Table | Columns |
|-------|---------|
| `score.funding` | `candidate_id`, `candidate_type` (`epic` \| `feature` \| `initiative` \| `cluster_fix`), `title`, `annual_pain_usd`, `addressable_pain_usd`, `expected_reduction`, `n_incidents`, `confidence`, `strategic_weight`, `effort_cost_usd`, `priority`, `wsjf`, `rank`, `unconfirmed BOOLEAN`, `flags VARCHAR[]`, `query_ids VARCHAR[]` |
| `score.funding_attribution` | `candidate_id`, `record_id`, `record_kind`, `tier`, `weight`, `share`, `pain_usd`, `quality`, `query_id` |
| `score.org` | `entity_type`, `entity_id`, `metric`, `value`, `peer_group`, `peer_median`, `z_score`, `trend_slope`, `sample_size`, `composite`, `rank`, `unconfirmed`, `flags VARCHAR[]`, `query_ids VARCHAR[]` |
| `score.action_lever` | `entity_type`, `entity_id`, `metric`, `target_kind` (`peer_median` \| `top_quartile`), `current_value`, `target_value`, `delta_usd`, `rationale_template`, `template_params JSON`, `unconfirmed`, `query_ids VARCHAR[]` |
| `score.portfolio` | `scenario`, `budget_usd`, `candidate_id`, `selected BOOLEAN`, `order_rank`, `expected_impact_usd`, `solver_status`, `flags VARCHAR[]`, `query_ids VARCHAR[]` |

### 4.7 Build metadata (`meta`)

- `meta.build`: `build_id PK`, `started_at`, `finished_at`, `git_sha`, `config_hash`, `dataset_kind` (`synthetic` \| `real`; set to `synthetic` when the active profile is `synth` or any lake file comes from `data/synth/`), `source_watermarks JSON`, `row_counts JSON`, `status` (`building` \| `failed` \| `promoted` \| `retired`).
- `meta.evidence`: `query_id PK`, `sql`, `params JSON`, `result_hash` (spec 00 §5.1), `row_count`, `result_sample JSON` (≤ 50 rows), `executed_at`, `producer` (`score` \| `metrics` \| `facts`).
- `meta.dq_result`: `check_name`, `severity` (`error` \| `warn`), `value`, `threshold`, `passed`, `details JSON`.

### 4.8 Data quality checks

Thresholds in `config/sources.yaml: dq`. `error` blocks promotion; `warn` shows in reports and dashboard.

| Check | Default | Severity |
|-------|---------|----------|
| Row count drop vs previous build per canonical table | > 5 % | error |
| Incidents with `service_id` NULL | > 30 % (error > 60 %) | warn |
| Work items with `service_id` NULL | > 40 % | warn |
| Type-cast failures per column | > 0.5 % | warn |
| Future timestamps | any | warn |
| `resolved_at < opened_at` | > 0.1 % | warn |
| Duplicate `record_id` after dedupe | any | error |
| Decision coverage on incidents | < 95 % | warn |

## 5. Ops store (SQLite, `data/ops.sqlite`)

Migrations in `herness/store/migrations/NNN_*.sql`, applied by `herness.store.ops.migrate()`. WAL, `foreign_keys=ON`, timestamps in the fixed-width format of spec 00 §8, JSON as TEXT. All access through `herness.store.ops` functions (each owning spec lists its functions).

### 5.1 Ingestion and health

| Table | Columns | Owner |
|-------|---------|-------|
| `watermark` | `source`, `entity`, `field`, `value`, `updated_at`; PK (`source`, `entity`). For monitoring, `source` = `monitoring:<tool>` so each tool advances independently | 01 |
| `sync_slice` | `source`, `entity`, `slice_start`, `slice_end`, `status` (`pending` \| `running` \| `done` \| `failed`), `rows`, `files JSON`, `attempts`, `last_error`, `updated_at`; PK (`source`, `entity`, `slice_start`) | 01 |
| `file_ingest` | `fingerprint PK`, `source`, `entity`, `path`, `size_bytes`, `mtime`, `rows`, `files JSON`, `ingested_at` | 01 |
| `source_health` | `source PK` (key convention `<connector>` \| `monitoring:<tool>` \| `model:<profile>` \| `decider:<name>`), `state` (`closed` \| `open` \| `half_open`), `failures`, `trips`, `opened_at`, `last_error`, `updated_at` | 08 |

### 5.2 Jobs and workers (owner 08)

| Table | Columns |
|-------|---------|
| `job` | `job_id PK`, `kind` (`sync` \| `reconcile` \| `build_pipeline` \| `distill` \| `review` \| `chat` \| `outcome_measure` \| `memory_maintenance` \| `maintenance` \| `eval`), `gpu_class` (`none` \| `reasoning` \| `decider` \| `large`), `status` (`queued` \| `running` \| `done` \| `failed` \| `canceled`), `priority`, `payload JSON`, `idem_key`, `result JSON`, `attempts`, `max_attempts`, `last_error`, `lease_owner`, `lease_expires_at`, `scheduled_for`, `created_at`, `started_at`, `finished_at` |
| `worker` | `worker_id PK` (`<host>:<pid>`), `host`, `pid`, `gpu_slot`, `cpu_slots`, `gpu_class_loaded` (`none` \| `reasoning` \| `decider` \| `large` \| `swapping`), `requested_class`, `status` (`starting` \| `running` \| `draining` \| `stopped`), `current_jobs JSON`, `started_at`, `heartbeat_at`, `version`, `faults_enabled` |
| `resilience_event` | `event_id PK`, `ts`, `kind`, `component`, `target`, `run_id`, `job_id`, `task_id`, `detail JSON`; retention 90 days |

Indexes: `UNIQUE job(idem_key) WHERE status IN ('queued','running')`; `job(status, gpu_class, scheduled_for, priority)`; `resilience_event(kind, ts)`.

### 5.3 Runs, tasks, findings, evidence

| Table | Columns | Owner |
|-------|---------|-------|
| `run` | `run_id PK`, `kind` (`funding_review` \| `org_review` \| `chat` \| `eval`), `depth` (`fast` \| `standard` \| `deep`), `profile`, `build_id`, `status` (`created` \| `planning` \| `running` \| `challenging` \| `verifying` \| `writing` \| `recording` \| `done` \| `partial` \| `failed` \| `canceled`), `started_at`, `finished_at`, `token_usage JSON`, `cost_usd`, `config_hash`, `meta JSON` | 06 |
| `task` | `task_id PK`, `run_id FK`, `parent_task_id`, `role` (`planner` \| `judge` \| `analyst` \| `skeptic` \| `verifier` \| `writer` \| `chat`), `spec JSON`, `status` (`pending` \| `running` \| `done` \| `failed` \| `dead`), `attempts`, `last_error`, `checkpoint JSON` (key `scratchpad` reserved for spec 07), `result JSON`, `created_at`, `updated_at` | 06 (mechanics 08) |
| `finding` | `finding_id PK`, `run_id`, `task_id`, `author_role`, `claim`, `entity_type`, `entity_id`, `numbers JSON` (list of `NumberRef`, spec 00 §12.1), `query_ids JSON`, `confidence`, `status` (`proposed` \| `challenged` \| `verified` \| `rejected` \| `revised` \| `merged`), `challenge JSON`, `verification JSON`, `supersedes`, `merged_into`, `created_at` | 06 |
| `evidence` | `query_id PK`, `run_id` (first run; NULL for ad-hoc calls outside a run, e.g. dashboard budget scenarios), `build_id`, `sql`, `params JSON`, `result_hash`, `row_count`, `result_sample JSON` (≤ 50 rows), `executed_at`, `duration_ms` | 05 |
| `evidence_use` | `query_id`, `run_id`, `task_id`, `used_at`; PK (`query_id`, `run_id`, `task_id`) | 05 |

Indexes: `UNIQUE task(run_id, json_extract(spec, '$.dedup_key'))`; `task(run_id, status)`; `finding(run_id, status)`.

### 5.4 Memory and closed loop (owner 07)

| Table | Columns |
|-------|---------|
| `memory_item` | `memory_id PK`, `layer` (`episodic` \| `semantic` \| `procedural`), `kind`, `content`, `data JSON`, `provenance JSON`, `confidence`, `status` (`candidate` \| `pending_approval` \| `active` \| `expired` \| `rejected`), `created_at`, `expires_at`, `last_used_at`, `use_count` |
| `memory_fts` | FTS5 virtual table (`content`, `kind`), external content `memory_item`, kept in sync by triggers |
| `recommendation` | `rec_id PK`, `run_id`, `kind` (`fund` \| `org_action`), `target_type`, `target_id`, `summary` (text with `[[nX]]` markers), `numbers JSON` (list of `NumberRef`), `expected_metric`, `expected_delta`, `expected_usd`, `confidence`, `confidence_basis JSON`, `finding_ids JSON`, `created_at` |
| `decision_log` | `rec_id FK`, `decision` (`accepted` \| `rejected` \| `deferred`), `reason`, `decided_by`, `decided_at`, `effective_at` |
| `outcome` | `outcome_id PK`, `rec_id FK`, `measurement` (1, 2, … per rec), `measured_at`, `metric`, `baseline`, `actual`, `delta`, `query_id`, `verdict` (`paid_off` \| `no_effect` \| `worse` \| `inconclusive`), `details JSON` |

### 5.5 Review, chat, privacy

| Table | Columns | Owner |
|-------|---------|-------|
| `review_item` | `item_id PK`, `kind` (`mapping_suggestion` \| `label_check` \| `memory_write` \| `weight_change`), `payload JSON` (schemas per kind in specs 03, 04, 07), `status` (`pending` \| `approved` \| `rejected`), `created_at`, `decided_by`, `decided_at`, `note` | 02 (shared) |
| `chat_session` | `session_id PK`, `user_ref` (HMAC hash), `title`, `created_at`, `last_active_at`, `summary` | 09 |
| `chat_message` | `message_id PK`, `session_id FK`, `role`, `content`, `status` (`queued` \| `streaming` \| `done` \| `failed`), `verified` (`verified` \| `partial` \| `unverified` \| NULL), `feedback` (`up` \| `down` \| NULL), `feedback_note`, `run_id`, `query_ids JSON`, `meta JSON`, `created_at` | 09 |
| `deletion_request` | `request_id PK`, `record_id`, `requested_by`, `reason_ref`, `status` (`pending` \| `running` \| `done` \| `failed`), `steps JSON`, `created_at`, `completed_at` | 10 |

## 6. Vector tables (LanceDB, `data/vectors/`)

| Table | Columns | Owner |
|-------|---------|-------|
| `ticket_embedding` | `record_id`, `entity`, `service_id`, `opened_at`, `content_hash`, `model`, `vector` (float32[1024], bge-m3) | 03 |
| `memory_embedding` | `memory_id`, `layer`, `kind`, `status`, `content_hash`, `model`, `vector` | 07 |

Vectors are keyed by `content_hash`, so unchanged text is never re-embedded.

## 7. Errors and resilience

- Build runs in one leased job (spec 08). An orphan `wh-<build_id>.duckdb` with `meta.build.status = 'building'` is deleted on the next run.
- A SQL file failure raises `SchemaViolation` with file name and DuckDB error; `CURRENT` untouched.
- Deleting retired warehouse files tolerates Windows `PermissionError` (open reader) and retries at the next pipeline run.
- Ops writes retry on `SQLITE_BUSY` via `StoreBusy` (spec 08).

## 8. Configuration

- `config/sources.yaml`: `dq.*` thresholds; `build.keep_last` (3); `build.memory_limit` (`75%`); `build.threads`.
- `config/mappings.yaml`: `enums` (including `jira.status_category`: status name → `todo` \| `in_progress` \| `done`, used for `core.work_item_transition` categories); `service_overrides`; `custom_fields` including `servicenow.customer_impact_minutes`, `servicenow.acknowledged_at` (optional; when absent `acknowledged_at` stays NULL and MTTA is disabled), `jira.story_points`, `jira.team`, `jira.estimate_cost_usd`, `jira.epic_link` (Data Center).

## 9. Performance targets (16 cores, 64 GB RAM, NVMe)

| Operation | Volume | Target |
|-----------|--------|--------|
| Build 000–299 | 5M incidents, 0.5M changes, 2M events, 0.2M work items, 3 years `metric_daily` | < 10 min |
| DQ checks | same | < 1 min |
| Warehouse size | same | < 15 GB |
| Typical agent query | same | p95 < 2 s |

## 10. Security

- Raw ticket text stays in `core.*`; agents and UI see only `enrich.text_redacted` (specs 05, 09).
- The warehouse is opened read-only everywhere except the build pipeline job. Folder ACLs per spec 10.

## 11. Tests and acceptance criteria

- Staging macros with table-driven fixtures.
- Golden build: `tests/fixtures/lake_small/` builds to expected row counts and a `core.*` snapshot.
- Dedupe and tombstones; deletion requests remove records from staging.
- Schema drift: unknown column does not fail the build; dot-prefixed temp files are ignored.
- Blue/green: kill mid-build → `CURRENT` unchanged; next build cleans the orphan.
- DQ: forced 10 % row drop blocks promotion.
- Migrations: fresh and incremental migrations produce the same schema; FTS triggers keep `memory_fts` in sync.
- Scale: synthetic full build meets §9 (spec 11).

## 12. Open questions

- D1 (spec 00): if the CMDB is not authoritative for ownership, `service_map` resolution order changes.
- Jira custom field IDs per instance, confirmed in Phase 6.

## 13. Dependencies

`duckdb` (with `sqlite` extension), `pyarrow`, `jinja2`, `lancedb`, standard library `sqlite3` (FTS5 enabled).
