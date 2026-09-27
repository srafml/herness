# 02 — Data Model: Implementation Spec

Status: Draft v1 · 2026-09-24 · Design spec: [`docs/specs/02-data-model.md`](../specs/02-data-model.md) (Draft v2) · Phase: 1 · Standards: [`ENG-STANDARDS.md`](ENG-STANDARDS.md)

Implementation specs this one depends on (interfaces only): impl 00 (errors, ids, time, logging), impl 01 (`watermark` reads, Jira raw column contract, `SourcesConfig`), impl 03 (`run_enrichment`, `LlmFactory`), impl 04 (`materialize_facts`, `run_scoring`, content of `400_facts.sql`), impl 05 (`harness.sql.blocked_columns` default), impl 06 (`run` statuses), impl 08 (`JobContext`, retry policies, fault points, `metric_sample` writer), impl 10 (config loader, `config_hash`, audit, deletion procedure, `deleted_record_ids` in area `privacy`, R-68), impl 11 (fixtures `lake_small`, synthetic generator).

Consistency rulings: this spec applies the binding rulings of [`DECISIONS.md`](DECISIONS.md) and cites each one where it applies as `R-nn`. Spec 02 owns the ops store core, the area table (§2.3, R-08, R-09), the core API names (R-10), the migration runner and its owner ranges (R-11) and migration 006 `metric_sample` (R-12).

Cross-spec references are written `T<NN>-<nn> (<qualified symbol or artifact>)`, naming the task card of impl NN whose Units list defines the symbol (DECISIONS §8).

## 1. Scope and traceability

This spec builds everything the design spec assigns to `herness/store/` and `herness/model/`: the raw lake writer and lake purge primitives, the blue/green warehouse file management (`CURRENT` pointer, read-only and build connections, retirement), the SQLite ops store core (connections, write transactions, JSON helpers, the forward-only migration runner that applies every owner's migrations in numeric order, and migrations 001–006 for every ops table named in the design specs including the ENG §4 `metric_sample` table), the ops store area table and re-export rules, the ops functions spec 02 owns (every `review_item` function, build-retention reads), the LanceDB vector store wrapper, the config section models for `mappings.yaml`, `sources.yaml: dq` and `sources.yaml: build`, and the nightly `build_pipeline` job handler with its numbered Jinja SQL files and data quality gate. It does not build connector fetch logic (impl 01), enrichment (impl 03), metric and score SQL (impl 04; `400_facts.sql` content is theirs), or the ops functions for tables owned by other specs (each owner's impl spec, under the rule in §2.3).

### 1.1 Traceability matrix

| Design § | Requirement (short) | Impl § | Units | Tasks | Tests |
|----------|---------------------|--------|-------|-------|-------|
| 1 | Single source of truth for table and column names | 4, 3.5, 3.10 | U02-49…U02-54, U02-106…U02-127 | T02-05, T02-06, T02-12…T02-20 | UT02-32, IT02-21 |
| 2 | Lake contract and writer for every connector | 3.2 | U02-08…U02-19 | T02-02 | UT02-01…UT02-13, PT02-01 |
| 2 | Deterministic versioned build `herness build` | 3.9, 3.10, 5 | U02-82…U02-105, U02-134 | T02-11, T02-18…T02-21 | IT02-21, IT02-22, IT02-27, UT02-78 |
| 2 | Deduplicate, type, normalize | 3.10 | U02-106…U02-115 | T02-12…T02-14 | UT02-60…UT02-64, IT02-02…IT02-08 |
| 2 | Resolve entity links into `core.service_map` | 3.10 | U02-117 | T02-15 | IT02-11 |
| 2 | DQ checks block promotion | 3.9, 3.10 | U02-93, U02-94, U02-127 | T02-20 | IT02-28…IT02-30, ST02-17 |
| 2 | Create and migrate ops store and vector tables | 3.5, 3.6, 3.7 | U02-36…U02-70, U02-129 | T02-04…T02-08 | UT02-25…UT02-52, UT02-70, UT02-71 |
| 3.1 | Lake path, partitioning, metadata columns, flattening, append-only | 3.2, 4.1 | U02-08, U02-10, U02-11, U02-19 | T02-02 | UT02-01…UT02-05, UT02-08 |
| 3.1 | Raw entity names | 3.9 | U02-78 | T02-11 | UT02-55 |
| 3.2 | `LakeWriter` / `LakeFileSet` API, temp naming, rotation, commit/abort | 3.2 | U02-12…U02-18 | T02-02 | UT02-01, UT02-06…UT02-12, ST02-02, FT02-06 |
| 4 | One warehouse file per build; schemas | 3.4, 3.10 | U02-24…U02-35, U02-107, U02-133 | T02-09, T02-10, T02-12 | UT02-17…UT02-24, UT02-77 |
| 4.1 | Stage order, lexical SQL execution, Jinja for config values only, Python hooks, nightly pipeline, failure leaves `CURRENT` | 3.9, 5 | U02-82…U02-86, U02-95…U02-105 | T02-11, T02-18, T02-19, T02-21 | UT02-56…UT02-58, IT02-22, IT02-25…IT02-27, FT02-03, FT02-04 |
| 4.2 | Staging glob, dedupe, tombstones, deletion requests, `TRY_CAST` with counts, enum macros | 3.10 | U02-106…U02-115, U02-87 | T02-12…T02-14 | IT02-02…IT02-08, ST02-16 |
| 4.3 | Canonical `core.*` tables | 3.10 | U02-116…U02-123 | T02-15…T02-17 | IT02-09…IT02-20, IT02-21 |
| 4.4 | `enrich.*` tables (owner 03); enrichment hook with `stages` (R-48); impl 03 takes the `decider` GPU scope itself (R-43) | 3.9, 3.10, 4.2 | U02-100, U02-107, U02-124, U02-125 | T02-12, T02-19 | IT02-23…IT02-25 |
| 4.5 | `metrics.*` tables (owner 04) | 3.10 | U02-126 | T02-19 | IT02-26 |
| 4.6 | `score.*` tables (owner 04) | 3.9 | U02-101 | T02-19 | IT02-26 |
| 4.7 | `meta.build`, `meta.evidence`, `meta.dq_result`, `dataset_kind` | 3.9, 3.10 | U02-88…U02-92, U02-107 | T02-12, T02-18 | UT02-69, UT02-67, IT02-21 |
| 4.8 | DQ checks and thresholds | 3.9, 3.10, 9 | U02-74, U02-93, U02-94, U02-127 | T02-01, T02-20 | IT02-28…IT02-31 |
| 5 | Ops store: migrations (owner ranges, R-11), WAL, `foreign_keys`, timestamps, JSON as TEXT, access only through `herness.store.ops` areas (R-08) | 2.3, 3.5, 3.6, 4.3 | U02-36…U02-48, U02-62, U02-129 | T02-04, T02-05 | UT02-25…UT02-31, UT02-40…UT02-42, UT02-68, UT02-70, UT02-71 |
| 5.1 | `watermark`, `sync_slice`, `file_ingest`, `source_health` | 4.3 | U02-49 | T02-05 | UT02-32, UT02-37 |
| 5.2 | `job`, `worker`, `resilience_event`, indexes | 4.3 | U02-50 | T02-05 | UT02-32, UT02-37 |
| 5.3 | `run`, `task`, `finding`, `evidence`, `evidence_use`, indexes | 4.3 | U02-51 | T02-06 | UT02-38, UT02-39 |
| 5.4 | Memory tables, `memory_fts` triggers | 4.3 | U02-52 | T02-06 | UT02-36 |
| 5.5 | `review_item` (shared, owner 02; every `review_item` function, R-08, R-33), chat, `deletion_request` | 3.7, 4.3 | U02-53, U02-55…U02-60, U02-130…U02-132 | T02-06, T02-07, T02-24 | UT02-43…UT02-47, UT02-72…UT02-76, UT02-79, ST02-06, ST02-07 |
| 6 | LanceDB `ticket_embedding`, `memory_embedding` keyed by `content_hash` | 3.8 | U02-63…U02-70 | T02-08 | UT02-49…UT02-52, ST02-08, ST02-09 |
| 7 | One leased job; orphan deletion; `SchemaViolation` on SQL failure; tolerant retired-file deletion; `StoreBusy` retries | 3.4, 3.5, 3.9, 6 | U02-32, U02-38, U02-97, U02-104, U02-105 | T02-04, T02-09, T02-21 | UT02-22, UT02-28, IT02-22, FT02-02…FT02-05 |
| 8 | Config keys in `sources.yaml` and `mappings.yaml` | 3.11, 9 | U02-71…U02-75 | T02-01 | UT02-53, UT02-54 |
| 9 | Performance targets | 10 | U02-97, U02-127 | T02-22, T02-23 | BT02-01…BT02-07 |
| 10 | Raw text stays in `core.*`; warehouse read-only except the build job; ACLs | 7 | U02-29, U02-34, U02-109…U02-123 | T02-09, T02-10 | ST02-04, ST02-05, ST02-13 |
| 11 | Tests and acceptance criteria | 11 | all | T02-22 | IT02-01, IT02-21, IT02-29, FT02-03, BT02-01 |
| 12 | Open questions (D1, Jira custom field IDs) | 13 | U02-117, U02-72 | T02-15 | IT02-11 |
| 13 | Dependencies | 14 | — | — | — |

## 2. Module map

### 2.1 Files

| Path | Purpose | Public symbols | Layer | Extra imports | Line budget |
|------|---------|----------------|-------|---------------|-------------|
| `herness/store/__init__.py` | Package marker | none | L1 | — | 5 |
| `herness/store/errors.py` | Store error subclasses | `NotFoundError`, `ReviewItemConflict`, `LakeContractError`, `LakeStateError`, `MigrationError` | L1 | — | 70 |
| `herness/store/layout.py` | Resolve data paths from config | `DataLayout`, `data_layout` | L1 | — | 70 |
| `herness/store/lake.py` | Lake contract validation and `LakeWriter` | `META_COLUMNS`, `validate_name`, `partition_dir`, `lake_glob`, `LakeFileSet`, `LakeWriter` | L1 | `pyarrow`, `pyarrow.parquet`, `pyarrow.compute` | 380 |
| `herness/store/lake_purge.py` | Record deletion rewrite and partition retention for the lake | `LakePurgeResult`, `purge_record_ids`, `LakeRetentionResult`, `purge_partitions_before` | L1 | `pyarrow` | 200 |
| `herness/store/warehouse.py` | Build IDs, `CURRENT` reading, read-only connections, build listing and file deletion | `BUILD_ID_RE`, `new_build_id`, `build_path`, `build_exists`, `read_current`, `CurrentPointer`, `open_readonly`, `BuildInfo`, `list_builds`, `delete_build_files`, `warehouse_health` | L1 | `duckdb` | 370 |
| `herness/store/_warehouse_rw.py` | Writable build connection and `CURRENT` writer (import restricted to `herness.model`) | `open_for_build`, `write_current` | L1 | `duckdb` | 120 |
| `herness/store/ops/__init__.py` | Public `herness.store.ops` namespace: re-exports of every area's public functions (§2.3, R-08); one import block and one `__all__` block per area (17 areas), so the budget is the ENG §2.4 cap | re-exports only (§2.3) | L1 | — | 400 |
| `herness/store/ops/core.py` | Per-thread SQLite connections, write transactions with retry, JSON helpers (core API names of R-10) | `OPS_JSON_MAX_BYTES`, `connection`, `run_write`, `read_one`, `read_all`, `dump_json`, `load_json`, `reset_connections` | L1 | `sqlite3` | 280 |
| `herness/store/ops/_shims.py` | Module attributes `core` calls for the two impl 08 hooks: `retry_call` is a re-export of `T08-07 (herness.core.resilience.retry_call)` and `fault_point` of `T08-08 (herness.core.resilience.fault_point)`; kept (not deleted) because `core` never changes and tests monkeypatch the hooks here; not an area (§2.3) | `retry_call`, `fault_point` (private module) | L1 | — | 80 |
| `herness/store/ops/_review_common.py` | Private helper module of `shared` (T02-24): pure `review_item` SQL text and match-key/field validation with no state, no connection and no import of `shared`; not an area (§2.3). Keeps `shared.py` within its module-map budget | `keys_ok`, `is_count`, `is_json_object`, `check_decision`, SQL text constants (private module) | L1 | — | 100 |
| `herness/store/ops/migrate.py` | Forward-only migration runner over all owner ranges (R-11) and ops health | `MIGRATION_RANGES`, `MigrationReport`, `migrate`, `pending_migrations`, `schema_version`, `ops_health` | L1 | `importlib.resources` | 260 |
| `herness/store/ops/shared.py` | Ops functions owned by spec 02: every `review_item` function (R-08) | `ReviewItem`, `create_review_item`, `create_review_item_if_absent`, `get_review_item`, `list_review_items`, `count_review_items`, `decide_review_item`, `update_review_payload`, `approved_mapping_suggestions` | L1 | — | 390 |
| `herness/store/migrations/001_ingestion_health.sql` | `watermark`, `sync_slice`, `file_ingest`, `source_health` | SQL | L1 | — | 90 |
| `herness/store/migrations/002_jobs.sql` | `job`, `worker`, `resilience_event` | SQL | L1 | — | 110 |
| `herness/store/migrations/003_runs_evidence.sql` | `run`, `task`, `finding`, `evidence`, `evidence_use` | SQL | L1 | — | 140 |
| `herness/store/migrations/004_memory.sql` | `memory_item`, `memory_fts` + triggers, `recommendation`, `decision_log`, `outcome` | SQL | L1 | — | 140 |
| `herness/store/migrations/005_review_chat_privacy.sql` | `review_item`, `chat_session`, `chat_message`, `deletion_request` | SQL | L1 | — | 110 |
| `herness/store/migrations/006_metric_sample.sql` | `metric_sample` (ENG delta E5, R-12) | SQL | L1 | — | 40 |
| `herness/store/migrations/0NN_<slug>.sql` outside 001–009 | Owner-range migrations for tables or columns that exist only in an implementation spec (R-11). Each is a unit of its owner spec; this spec only applies them (U02-45) | SQL | L1 | — | owner's budget |
| `herness/store/vectors.py` | LanceDB wrapper and table schemas | `EMBEDDING_DIM`, `TICKET_EMBEDDING_SCHEMA`, `MEMORY_EMBEDDING_SCHEMA`, `VectorStore` | L1 | `lancedb`, `pyarrow` | 260 |
| `herness/model/__init__.py` | Package marker | none | L2 | — | 5 |
| `herness/model/settings.py` | Config section models for `mappings.yaml` and the top-level `sources.yaml` sections `dq` and `build` (siblings of impl 01's connector sections; composed into the root by impl 10, R-03) | `ServiceOverride`, `CustomFieldsConfig`, `MappingsConfig`, `DqSettings`, `BuildSettings` | L2 (pydantic only) | `pydantic` | 240 |
| `herness/model/errors.py` | Build error subclasses | `BuildSqlError`, `DqGateFailed` | L2 | — | 40 |
| `herness/model/lakeinfo.py` | Inventory of lake entities and their columns | `EXPECTED_ENTITIES`, `EntityInventory`, `LakeInventory`, `scan_lake` | L2 | `pyarrow.parquet` | 200 |
| `herness/model/sqlfiles.py` | SQL file discovery, sandboxed Jinja rendering, filters | `SqlFile`, `discover_sql_files`, `render_sql` | L2 | `jinja2`, `jinja2.sandbox` | 260 |
| `herness/model/render_context.py` | Build the Jinja context from config and lake inventory | `RenderContext`, `build_render_context` | L2 | — | 200 |
| `herness/model/refdata.py` | Register config and ops reference rows as DuckDB tables | `register_reference_tables` | L2 | `duckdb`, `pyarrow` | 220 |
| `herness/model/meta.py` | `meta.build` row, row counts, `dataset_kind`, `git_sha` | `dataset_kind`, `git_sha`, `insert_build_row`, `update_build_row`, `collect_row_counts` | L2 | `duckdb`, `subprocess` | 240 |
| `herness/model/dq.py` | DQ gate evaluation | `DqOutcome`, `evaluate_gate` | L2 | `duckdb` | 120 |
| `herness/model/build.py` | `build_pipeline` job handler and stage orchestration | `STAGE_ORDER`, `BuildPipelinePayload`, `run_sql_range`, `run_build_pipeline`, `make_build_pipeline_handler` | L2 | `duckdb` | 400 |
| `herness/model/_build_support.py` | Private sibling of `build` (T02-18 spec note), split off for the 400-line budget of `build.py`: `STAGE_ORDER` (re-exported by `build`), build resolution of U02-98 step 3, the orphan deletion that stands in for `cleanup_builds(mode="pre")` until T02-21, the §8.2 metric samples, and the DuckDB form of `build.memory_limit` (T02-18 spec note: DuckDB rejects `%`, so a percentage becomes MiB of total physical RAM, at least 256 MiB; a size below 256 MiB is `ConfigError`) passed to `open_for_build` as a converted view; imported only by `build`; opens no writable connection | none (private) | L2 | `psutil` | 200 |
| `herness/model/_build_stages.py` | Private sibling of `build` (T02-19 spec note), split off for the 400-line budget of `build.py`: the stage units `_stage_enrich` (U02-100) and `_stage_score` (U02-101), named `stage_enrich` / `stage_score` in `build`'s stage table, and `make_build_pipeline_handler` (U02-134, re-exported by `build`); holds the lazy upward imports of the §2.2 exception (`herness.enrich.gpu.YieldRequested` until T03-04 re-exports it from `herness.enrich.pipeline`; `herness.metrics.facts.materialize_facts`) and the private loader seams of `run_enrichment` (T03-28) and `run_scoring` (T04-13), which turn a hook module that is not installed into `ConfigError("build stage <name> is not available")`; imported only by `build`, reaches `build` only inside functions (no module-level cycle) | none (private) | L2 | `duckdb` | 200 |
| `herness/model/promote.py` | Promotion, retirement and orphan cleanup | `promote_build`, `cleanup_builds` | L2 | `duckdb` | 260 |
| `herness/model/sql/_macros.jinja` | Jinja macros imported by SQL files (never executed) | Jinja macros | L2 | — | 150 |
| `herness/model/sql/000_settings.sql` … `900_dq_checks.sql` | Build SQL (§3.10) | SQL | L2 | — | per file ≤ 400 |

### 2.2 Layering and import contracts

| Contract (in `pyproject.toml`, `import-linter`) | Rule |
|---|---|
| `store-rw-restricted` | `herness.store._warehouse_rw` may be imported only by `herness.model.build` and `herness.model.promote` (forbidden contract with those two modules as the allowed importers; also checked by ST02-05). |
| `model-settings-light` | `herness.model.settings` imports only the standard library, `pydantic`, `herness.core.types` and `herness.core.errors` (R-03, ENG §2.1 settings exception; loading config never imports duckdb). It imports no other package's `settings.py`. |
| `store-no-upward` | `herness.store` imports nothing from `herness.connectors`, `herness.model`, `herness.enrich`, `herness.metrics`, `herness.harness`, `herness.reports`, `herness.eval`, `herness.cli`, `herness.admin`. |
| `ops-areas-acyclic` | Each area module `herness.store.ops.<area>` imports from the ops package only `herness.store.ops.core` (and `herness.store.errors`, `herness.core`). No area imports another area or the package `herness.store.ops` itself; only `herness/store/ops/__init__.py` imports the areas (§2.3 rule 5). |
| Exception | `herness.model.build` imports `herness.enrich.pipeline.run_enrichment` (L3) and `herness.metrics.facts.materialize_facts`, `herness.metrics.scoring.run_scoring` (L3), and the `LlmFactory` type of impl 03 for annotations under `typing.TYPE_CHECKING`. These are upward imports required by design 02 §4.1 (the build job calls the stage hooks in one process). They are done lazily inside `_stage_enrich` and `_stage_score` (T02-19 spec note: both live in the private sibling `herness.model._build_stages`, so the `ignore_imports` lines name that module) and listed as an `ignore_imports` exception on the layers contract with this reason. Model clients are never imported: the composition root passes `llm_factory` in (R-05, U02-134). |

### 2.3 Ops store areas and ownership (R-08, R-09)

`herness.store.ops` is a package, not the single file `ops.py` named in design 00 §3 (DD02-01, accepted by R-08 and ENG §14 E6). Top-level files named `herness/store/ops_<area>.py` in other specs are renamed to `herness/store/ops/<area>.py` (R-08). This table is the canonical area table: an area not listed here does not exist, and adding one is a change to this spec.

| Area submodule | Owning impl spec | Tables written (primary) | Port bound by the composition root (R-04) |
|---|---|---|---|
| `herness/store/ops/core.py` | 02 (this spec) | none (transactions for every area) | — |
| `herness/store/ops/migrate.py` | 02 (this spec) | `schema_migration`; DDL of every migration file | — |
| `herness/store/ops/shared.py` | 02 (this spec) | `review_item` (every `review_item` function lives here). It reads no `run` or `task` rows: those reads belong to impl 06 (R-68) | — |
| `herness/store/ops/ingest.py` | 01 | `watermark` (monitoring watermarks kept per tool, R-62), `sync_slice`, `file_ingest` | — |
| `herness/store/ops/evidence.py` | 05 | `evidence`, `evidence_use` (writer `record_evidence`, R-13) | — |
| `herness/store/ops/runs.py` | 06 | `run`, `task` (insert, `spec`) | — |
| `herness/store/ops/findings.py` | 06 | `finding` | — |
| `herness/store/ops/memory.py` | 07 | `memory_item` (and `memory_fts` through its triggers) | — |
| `herness/store/ops/closed_loop.py` | 07 | `recommendation`, `decision_log`, `outcome` | — |
| `herness/store/ops/jobs.py` | 08 | `job` | jobs port |
| `herness/store/ops/tasks.py` | 08 | `task` status, attempts, `checkpoint`, `result` fields (checkpoint envelope, R-21) | tasks port |
| `herness/store/ops/worker.py` | 08 | `worker` | jobs port |
| `herness/store/ops/resilience.py` | 08 | `source_health`, `resilience_event` | breaker state port |
| `herness/store/ops/metrics.py` | 08 | `metric_sample` (writer `record_metric_samples`, R-12) | metric recording port |
| `herness/store/ops/chat.py` | 09 | `chat_session`, `chat_message` | — |
| `herness/store/ops/ui_reads.py` | 09 | none (read-only queries for the dashboard and CLI) | — |
| `herness/store/ops/privacy.py` | 10 | `deletion_request` (including the read `deleted_record_ids`, R-68); JSON rewrites in `evidence.result_sample` and `finding.numbers` | — |

The owner spec decides which of its functions go in which of its own areas, within the tables listed for each area. The "Port" column names the `herness.core` Protocol an area implements under R-04; `herness.core` never imports `herness.store`.

Rules for every area submodule:

1. It obtains connections only through `herness.store.ops.core.connection()` and performs every write through `herness.store.ops.core.run_write()`, or on the connection that a caller's `run_write` callback received (the `conn` parameter pattern of U02-56). The names `write_tx`, `write_transaction`, `transaction`, `connect`, `read_connection`, `open_ops_store`, `OpsStore` and `migration_status` do not exist (R-10); §13.4 maps each one to its replacement.
2. It never issues DDL at run time. A table or column that exists only in an implementation spec is created by a migration file in the owner's range (U02-129), listed as a unit in the owner spec (R-11). Tables named in the design specs are created only by migrations 001–006 of this spec (R-11); an owner migration never re-creates them.
3. A function in an area is specified only in the owner's implementation spec (R-09). Another spec that needs it references the owner's unit. This spec adds a unit for every function in `core`, `migrate` and `shared` that another spec references (§13.4 lists the references and the aliases).
4. Re-exports: each owner adds `from .<area> import <names>` lines and the same names to `__all__` of `herness/store/ops/__init__.py`, in one block per area headed by a comment with the owning spec number and area name. Every public function and type of every area is re-exported. Callers import `herness.store.ops.<name>`; specs name the canonical area path `herness.store.ops.<area>.<name>` (for example `herness.store.ops.metrics.record_metric_samples`, R-12). Both paths are the same object (the package attribute `migrate` is the function, so the `migrate` area is imported by its dotted path; U02-62). A name defined in two areas fails UT02-68; in particular no area other than `shared` defines a `review_item` function (R-08). The namespace is flat (R-68): reads of `run` and `task` belong to impl 06 (`get_run`, `select_runs`, `select_tasks`), UI-only projections in impl 09's `ui_reads` carry a `ui_` prefix, and `deleted_record_ids` belongs to impl 10's `privacy` area.
5. Import order: `__init__.py` imports `core` first, then `migrate`, `shared`, then the other areas in the row order of the table. An area imports `herness.store.ops.core`, never the package `herness.store.ops` or another area (contract `ops-areas-acyclic`, §2.2). Importing the package opens no connection.
6. Each area module stays within the 400-line limit (ENG §2.4).

## 3. Unit specs

Conventions for every unit below:

- "Now" is never read inside calculation code. Units that need time take `now: datetime` (timezone-aware UTC) or read the clock at the I/O edge named in the unit.
- Fixed-width timestamp text (`YYYY-MM-DDTHH:MM:SS.ffffffZ`, 27 characters, spec 00 §8) is produced and parsed only by `T00-04 (herness.core.time.format_utc)` and `T00-04 (herness.core.time.parse_utc)`. This spec calls them "ts-text" and "parse-ts".
- Logging uses `T00-07 (herness.core.logging)` with `component` = `store` or `model`. Events are listed in §8.
- Error messages name only identifiers (`build_id`, file names, table names, `item_id`, counts), never row values, payloads or ticket text.
- Units whose field would be empty show "—".

### 3.1 Errors and layout

#### U02-01 `herness.store.errors.NotFoundError`

| Field | Content |
|---|---|
| Kind | class (subclass of `T00-03 (herness.core.errors.NotFound)`, which is a `RecoverableError`; R-19) |
| Purpose | A requested ops row, build or pointer does not exist. CLI exit code 7 (not found, R-46). |
| Signature | Constructor `(message: str, *, kind: str, key: str)`; attributes `kind` (`review_item`, `build`, `current`, `vector_table`) and `key` (the identifier). |
| Preconditions | `kind` non-empty. |
| Postconditions | `str(err)` = message; the message contains `kind` and `key`; the inherited `details` (R-19) equals `{"kind": kind, "key": key}`; `hint` is `None`. |
| Invariants | Immutable after construction. |
| Algorithm | Stores fields; calls the base constructor with the message and `details={"kind": kind, "key": key}`. Callers that catch `NotFound` also catch this class. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | `key` is an identifier, never free text (TH02-14). |
| Tests | UT02-45 |

#### U02-02 `herness.store.errors.ReviewItemConflict`

| Field | Content |
|---|---|
| Kind | class (subclass of `RecoverableError`) |
| Purpose | A review decision was attempted on an item that is not `pending`. |
| Signature | Constructor `(item_id: str, current_status: str)`; attributes of the same names. |
| Preconditions | — |
| Postconditions | Message = `review item <item_id> is already <current_status>`. |
| Invariants | Immutable. |
| Algorithm | Stores fields, builds the message. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-45 |

#### U02-03 `herness.store.errors.LakeContractError`

| Field | Content |
|---|---|
| Kind | class (subclass of `T00-03 (herness.core.errors.SchemaViolation)`) |
| Purpose | A batch handed to `LakeWriter.write` violates the design 02 §3.1 contract. |
| Signature | Constructor `(source: str, entity: str, rule: str, column: str, bad_rows: int)`. |
| Preconditions | `bad_rows ≥ 0`. |
| Postconditions | Message = `lake contract violated for <source>/<entity>: <rule> on <column> (<bad_rows> rows)`. |
| Invariants | Immutable. |
| Algorithm | Stores fields, builds the message. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | Never includes values (TH02-14). |
| Tests | UT02-02…UT02-05 |

#### U02-04 `herness.store.errors.LakeStateError`

| Field | Content |
|---|---|
| Kind | class (subclass of `T00-03 (herness.core.errors.FatalError)`) |
| Purpose | A `LakeWriter` method was called after `commit()` or `abort()`. |
| Signature | Constructor `(source: str, entity: str, state: Literal["committed", "aborted"], method: str)`. |
| Preconditions | — |
| Postconditions | Message = `LakeWriter for <source>/<entity> is <state>; <method>() not allowed`. |
| Invariants | Immutable. |
| Algorithm | Stores fields. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-10 |

#### U02-05 `herness.store.errors.MigrationError`

| Field | Content |
|---|---|
| Kind | class (subclass of `SchemaViolation`) |
| Purpose | Migration discovery, checksum verification or application failed. |
| Signature | Constructor `(version: int, name: str, reason: Literal["checksum_mismatch", "apply_failed", "out_of_range", "duplicate_version", "unknown_applied", "sqlite_too_old"], detail: str)`. `out_of_range` and `duplicate_version` replace the earlier `gap` reason: migration numbers are no longer contiguous (R-11, U02-129). |
| Preconditions | `detail` holds no SQL values (only the SQLite error class name and message). |
| Postconditions | Message = `ops migration <version:03d>_<name>: <reason> (<detail>)`. |
| Invariants | Immutable. |
| Algorithm | Stores fields. |
| Side effects | None. |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | TH02-15. |
| Tests | UT02-34, UT02-35, UT02-70 |

#### U02-06 `herness.store.layout.DataLayout`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) |
| Purpose | Absolute paths of every store under the data root. |
| Signature | Fields: `root: Path` (resolved data root), `raw: Path` (= `root/raw`), `warehouse: Path` (= `root/warehouse`), `ops_db: Path` (= `root/ops.sqlite`), `vectors: Path` (= `root/vectors`), `synth_marker: bool`. Classmethod `from_root(root: Path) -> DataLayout`. |
| Preconditions | — |
| Postconditions | All paths are children of `root`. |
| Invariants | Frozen; paths are never re-resolved after construction. |
| Algorithm | `from_root`: (1) resolve `root` with `Path.resolve(strict=False)`; (2) derive the children; (3) `synth_marker` = true when `root.parts` contains a consecutive pair (`data`, `synth`), compared case-insensitively. |
| Side effects | None (does not create directories). |
| Errors | None. |
| Concurrency | Immutable. |
| Complexity and limits | O(path depth). |
| Security notes | Resolving once prevents later symlink swaps from changing the target (TH02-01). |
| Tests | UT02-66 |

#### U02-07 `herness.store.layout.data_layout`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `cfg` | `HernessConfig \| None` | `None` | keyword-only | `None` = `T10-03 (herness.core.config.get_config)()` |
| `root` | `Path \| None` | `None` | keyword-only | overrides `cfg.paths.data` (spec 11 generator, tests) |

Returns `DataLayout`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | The single place that turns `paths.data` into a `DataLayout`. |
| Preconditions | Config loadable when `root` is `None`. |
| Postconditions | Returns `DataLayout.from_root(root or cfg.paths.data)`; a relative `paths.data` resolves against the current working directory. |
| Invariants | — |
| Algorithm | (1) Pick `root` if given, else `cfg.paths.data`. (2) Return `DataLayout.from_root(Path(root))`. |
| Side effects | None. |
| Errors | `ConfigError` from the loader propagates. |
| Concurrency | Pure given config. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-66 |

### 3.2 Lake writer (`herness/store/lake.py`)

Module constants: `BUFFER_ROWS = 131_072`, `BUFFER_BYTES = 64 * 2**20`, `ZSTD_LEVEL = 3`, `PARQUET_VERSION = "2.6"`, `LAKE_FILE_PATTERN = "[!.]*.parquet"` (see OI-06), `NAME_RE = ^[a-z][a-z0-9_]{0,63}$`, `COLUMN_RE = ^[a-z_][a-z0-9_]{0,127}$`.

#### U02-08 `herness.store.lake.META_COLUMNS`

| Field | Content |
|---|---|
| Kind | constant |
| Purpose | Required metadata columns and their Arrow types (design 02 §3.1). |
| Signature | `META_COLUMNS: Final[tuple[tuple[str, pa.DataType, bool], ...]]` = (`_record_id`, `string`, not nullable), (`_source`, `string`, no), (`_entity`, `string`, no), (`_source_key`, `string`, no), (`_source_updated_at`, `timestamp("us", tz="UTC")`, no), (`_fetched_at`, `timestamp("us", tz="UTC")`, no), (`_deleted`, `bool`, no), (`_payload`, `string`, nullable). |
| Preconditions | — |
| Postconditions | — |
| Invariants | Column order of every written file: metadata columns first, then entity columns in the order of the batch. |
| Algorithm | — |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-01 |

#### U02-09 `herness.store.lake.validate_name`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `kind` | `Literal["source", "entity"]` | — | positional | |
| `value` | `str` | — | positional | |

Returns `str` (the value unchanged).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Allowlist check for source and entity names used in lake paths. |
| Preconditions | — |
| Postconditions | Value full-matches `NAME_RE`. |
| Invariants | — |
| Algorithm | (1) Full-match `NAME_RE`. (2) On mismatch raise. (3) Return the value. |
| Side effects | None. |
| Errors | `T00-03 (herness.core.errors.ConfigError)`: `invalid lake <kind> name (length <n>)`; the value is not echoed. |
| Concurrency | Pure. |
| Complexity and limits | O(len). |
| Security notes | Blocks `..`, separators, drive letters, uppercase and Unicode look-alikes (TH02-01). |
| Tests | UT02-13, ST02-01 |

#### U02-10 `herness.store.lake.partition_dir`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `raw_root` | `Path` | — | positional | resolved |
| `source` | `str` | — | positional | valid name |
| `entity` | `str` | — | positional | valid name |
| `dt` | `date` | — | positional | |

Returns `Path` = `raw_root/<source>/<entity>/dt=YYYY-MM-DD`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Canonical partition directory. |
| Preconditions | — |
| Postconditions | Result `is_relative_to(raw_root)`. |
| Invariants | — |
| Algorithm | (1) `validate_name` both names. (2) Join. (3) Check containment; on failure raise. |
| Side effects | None. |
| Errors | `ConfigError`. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | TH02-01. |
| Tests | UT02-13 |

#### U02-11 `herness.store.lake.lake_glob`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `raw_root` | `Path` | — | positional | |
| `source` | `str` | — | positional | valid name |
| `entity` | `str` | — | positional | valid name |

Returns `str`: `<raw_root as POSIX>/<source>/<entity>/**/<LAKE_FILE_PATTERN>`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | The one glob staging uses (design 02 §4.2); excludes dot-prefixed temp files. |
| Preconditions | — |
| Postconditions | The glob never matches `.part-*.parquet.tmp-*` names. |
| Invariants | — |
| Algorithm | (1) Validate names. (2) Build the string with `Path.as_posix()` and `LAKE_FILE_PATTERN`. |
| Side effects | None. |
| Errors | `ConfigError`. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | TH02-02. |
| Tests | ST02-02, IT02-06 |

#### U02-12 `herness.store.lake.LakeFileSet`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`, design 02 §3.2) |
| Purpose | Result of one `commit()`. |
| Signature | `files: tuple[Path, ...]` (final paths sorted by POSIX string), `rows: int`, `max_source_updated_at: datetime \| None` (UTC). |
| Preconditions | — |
| Postconditions | — |
| Invariants | `rows == 0` ⇔ `files == ()` ⇔ `max_source_updated_at is None`. |
| Algorithm | — |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-01, UT02-11 |

#### U02-13 `herness.store.lake.LakeWriter`

| Field | Content |
|---|---|
| Kind | class |
| Purpose | Buffer, validate and write one source entity's batches as zstd Parquet files, committed atomically by rename (design 02 §3.2). |
| Signature | Methods U02-14…U02-18. |
| Preconditions | — |
| Postconditions | — |
| Invariants | (a) State ∈ {`open`, `committed`, `aborted`}; every method except `abort` requires `open`. (b) At most one Parquet file is open for writing. (c) A written file exists only under its temp name `.<name>.parquet.tmp-<ulid>` until `commit()`. (d) All rows of a file share one `dt` (UTC date of `_fetched_at`) and one Arrow schema. (e) The buffer holds at most `BUFFER_ROWS` rows or `BUFFER_BYTES` bytes (Arrow `nbytes`) before it is flushed as one row group. |
| Algorithm | See methods. |
| Side effects | See methods. |
| Errors | See methods. |
| Concurrency | Not thread-safe; one writer per thread. Any number of writers may target the same entity from one or many processes, because file names are unique ULIDs. |
| Complexity and limits | Memory ≤ `BUFFER_BYTES` + one input batch + Parquet page buffers. |
| Security notes | TH02-01, TH02-02. |
| Tests | UT02-01…UT02-12, PT02-01, FT02-06 |

#### U02-14 `herness.store.lake.LakeWriter.__init__`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `source` | `str` | — | positional | valid name |
| `entity` | `str` | — | positional | valid name |
| `target_bytes` | `int` | `128 * 2**20` | keyword-only | 1 MiB ≤ value ≤ 1 GiB |
| `max_open_s` | `int` | `600` | keyword-only | 1 ≤ value ≤ 86,400 |
| `root` | `Path \| None` | `None` | keyword-only | raw lake root; `None` = `data_layout().raw` (DD02-03) |
| `clock` | `Callable[[], float]` | `time.monotonic` | keyword-only | monotonic seconds; tests inject a fake (DD02-03) |

Returns `None`.

| Field | Content |
|---|---|
| Kind | method |
| Purpose | Create an open writer; no file is created before the first flush. |
| Preconditions | Names valid; limits in range. |
| Postconditions | State `open`; empty buffer; no open file; `rows = 0`. |
| Invariants | — |
| Algorithm | (1) Validate names and ranges. (2) Resolve `root`. (3) Initialise `rows = 0`, `max_source_updated_at = None`, `temp_files: list[tuple[Path, Path]]` (temp, final), `opened_at = None`. |
| Side effects | None. |
| Errors | `ConfigError`. |
| Concurrency | See U02-13. |
| Complexity and limits | O(1). |
| Security notes | TH02-01. |
| Tests | UT02-13, ST02-01 |

#### U02-15 `herness.store.lake.LakeWriter.write`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `batch` | `pa.RecordBatch` | — | positional | contains all `META_COLUMNS` |

Returns `None`.

| Field | Content |
|---|---|
| Kind | method |
| Purpose | Validate a batch, split it by `dt`, buffer it, and flush and rotate files as limits are reached (design 02 §3.2). |
| Preconditions | State `open`, else `LakeStateError`. |
| Postconditions | Every row is buffered or written to a temp file; `rows` and `max_source_updated_at` include the batch. |
| Invariants | U02-13 (a)–(e). |
| Algorithm | (1) If `batch.num_rows == 0`, return. (2) If a file is open and `clock() − opened_at ≥ max_open_s`, flush the buffer (step 7) and close the file (reason `age`). (3) Normalise with `_validate_batch` (U02-19). (4) Compute `dt` per row = UTC date of `_fetched_at`. (5) Split rows into groups by `dt`, keeping the order of first appearance. (6) For each group: if the buffer is non-empty and the group's `dt` or schema differs from the buffer's, flush and close the open file (reason `dt_change` or `schema_change`); append the group to the buffer; if buffer rows ≥ `BUFFER_ROWS` or bytes ≥ `BUFFER_BYTES`, flush. (7) Flush: when no file is open, create the partition directory (`parents=True, exist_ok=True`) and open a `pyarrow.parquet.ParquetWriter` at `partition_dir(...)/.part-<ulidA>.parquet.tmp-<ulidB>` with compression `zstd`, level `ZSTD_LEVEL`, `version=PARQUET_VERSION`, `use_dictionary=True`, `write_statistics=True`; record (temp, final `part-<ulidA>.parquet`) and `opened_at = clock()`. Write the buffer as one table with `row_group_size` = its row count; clear the buffer. After the write, close the file if its on-disk size ≥ `target_bytes` (reason `size`). (8) Add the batch's row count to `rows`; update `max_source_updated_at` with the batch maximum. ULIDs come from `T00-05 (herness.core.ids.new_ulid)()`. |
| Side effects | Creates directories and temp files under `root`; `store.lake.file_rotated` (DEBUG) on each close with `source`, `entity`, `dt`, `rows`, `bytes`, `reason`. |
| Errors | `LakeContractError` (U02-19); `LakeStateError`; `StoreBusy` for `OSError` with errno `EACCES` or `EBUSY` or Windows `winerror` 32 or 33; any other `OSError` → `SchemaViolation("lake write failed for <source>/<entity>: <errno name>")`. |
| Concurrency | Not thread-safe. |
| Complexity and limits | O(rows). Memory bound per U02-13. |
| Security notes | Values are never logged. TH02-02. |
| Tests | UT02-01, UT02-06…UT02-09, PT02-01 |

#### U02-16 `herness.store.lake.LakeWriter.commit`

Returns `LakeFileSet`.

| Field | Content |
|---|---|
| Kind | method |
| Purpose | Make every written row visible by renaming temp files (design 02 §3.2). |
| Preconditions | State `open`, else `LakeStateError`. |
| Postconditions | State `committed`; every temp file renamed to its final name in the same directory. |
| Invariants | — |
| Algorithm | (1) Flush a non-empty buffer; close the open file (reason `commit`). (2) For each (temp, final) in creation order: if temp does not exist and final exists, skip (idempotent retry); open temp, `os.fsync`, close; `os.replace(temp, final)`. (3) On POSIX, `fsync` each distinct partition directory once; skip on Windows. (4) State `committed`. (5) Log `store.lake.committed` (INFO) with `source`, `entity`, `files`, `rows`, `bytes`, `duration_ms`. (6) Return `LakeFileSet`. |
| Side effects | Renames files. |
| Errors | `StoreBusy` on sharing violations (state stays `open`, the caller retries `commit()` or calls `abort()`); other `OSError` → `SchemaViolation` with state `open`. |
| Concurrency | Not thread-safe. |
| Complexity and limits | O(files). |
| Security notes | A crash between renames leaves some files final and some temp. Readers ignore temps, spec 01 does not advance the watermark, and staging dedupe collapses the rerun's duplicates (FT02-06). |
| Tests | UT02-01, UT02-11, FT02-06 |

#### U02-17 `herness.store.lake.LakeWriter.abort`

Returns `None`.

| Field | Content |
|---|---|
| Kind | method |
| Purpose | Discard everything not yet committed. |
| Preconditions | None; idempotent; no-op when `committed`. |
| Postconditions | State `aborted` (unless `committed`); no temp file of this writer exists except those deferred by locks. |
| Invariants | — |
| Algorithm | (1) If `committed`, return. (2) Close the open Parquet writer, suppressing `OSError` and `pyarrow.ArrowException` only. (3) Unlink each temp file with `missing_ok=True`; on `PermissionError` count it. (4) If the count > 0, log `store.lake.abort_leftover` (WARNING) with the count (spec 01's runner removes temps older than 1 h). (5) Clear the buffer; state `aborted`. |
| Side effects | Deletes temp files. |
| Errors | None raised. |
| Concurrency | Not thread-safe. |
| Complexity and limits | O(files). |
| Security notes | — |
| Tests | UT02-10 |

#### U02-18 `herness.store.lake.LakeWriter.__enter__` / `__exit__`

| Field | Content |
|---|---|
| Kind | method pair |
| Purpose | Context manager: abort on exception; abort with a warning on a normal exit without `commit()`. |
| Signature | `__enter__() -> LakeWriter`; `__exit__(exc_type, exc, tb) -> Literal[False]`. |
| Preconditions | — |
| Postconditions | On exception exit: `aborted` unless already `committed`; the exception propagates. On normal exit while `open`: `aborted` and `store.lake.uncommitted_exit` (WARNING). |
| Invariants | — |
| Algorithm | As stated. |
| Side effects | May delete temp files. |
| Errors | None raised by the manager. |
| Concurrency | Not thread-safe. |
| Complexity and limits | O(files). |
| Security notes | — |
| Tests | UT02-12 |

#### U02-19 `herness.store.lake._validate_batch` (private, logic)

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `batch` | `pa.RecordBatch` | — | positional | |
| `source` | `str` | — | positional | |
| `entity` | `str` | — | positional | |

Returns `pa.RecordBatch` (normalised).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Enforce the design 02 §3.1 contract with vectorised checks. |
| Preconditions | — |
| Postconditions | `META_COLUMNS` first with exact types, then the other columns in incoming order; names unchanged. |
| Invariants | — |
| Algorithm | Rules in this order; the first failure raises `LakeContractError(source, entity, rule, column, bad_rows)`: (1) `missing_column`: every `META_COLUMNS` name present. (2) `type`: the five string metadata columns are `string` or `large_string`; both time columns are `timestamp` with tz `UTC` (any unit; naive fails); `_deleted` is `bool`. (3) `null`: no nulls in non-nullable metadata columns. (4) `source_mismatch`, `entity_mismatch`: every `_source` equals `source`, every `_entity` equals `entity`. (5) `record_id_format`: `_record_id` equals `_source + ":" + _entity + ":" + _source_key` element-wise (`pyarrow.compute.binary_join_element_wise`). (6) `payload_null`: rows with `_deleted = false` have a non-null `_payload`. (7) `column_name`: every other column name full-matches `COLUMN_RE`. (8) `duplicate_column`: names unique. Then cast time columns to `timestamp("us", tz="UTC")` and `large_string` to `string`; reorder. |
| Side effects | None. |
| Errors | `LakeContractError`. |
| Concurrency | Pure. |
| Complexity and limits | O(rows × 8). |
| Security notes | Values never appear in errors. |
| Tests | UT02-02…UT02-05, PT02-01 |

### 3.3 Lake purge (`herness/store/lake_purge.py`)

These primitives serve spec 10's deletion procedure (steps 2 and 7) and retention purge. They are the only code that rewrites or deletes committed lake files. R-57: the lake is append-only except for privacy deletion, the retention purge (both driven by spec 10 through this module) and compaction; this spec has no lake compaction, so no third exception exists (DD02-08, accepted).

#### U02-20 `herness.store.lake_purge.LakePurgeResult`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) |
| Purpose | Counts from `purge_record_ids`. |
| Signature | `files_scanned: int`, `files_rewritten: int`, `files_deleted: int`, `rows_removed: int`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | All ≥ 0; `files_rewritten + files_deleted ≤ files_scanned`. |
| Algorithm | — |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-14 |

#### U02-21 `herness.store.lake_purge.purge_record_ids`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `record_ids` | `Collection[str]` | — | positional | 1–10,000 ids of form `<source>:<entity>:<key>` |
| `root` | `Path \| None` | `None` | keyword-only | raw root; `None` = `data_layout().raw` |

Returns `LakePurgeResult`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Remove every row whose `_record_id` is in `record_ids` from committed lake files. |
| Preconditions | Each id's source and entity pass `validate_name`; count in range. |
| Postconditions | No committed file under the affected entity directories contains any of the ids. Files without matches are untouched (same bytes and mtime). |
| Invariants | — |
| Algorithm | (1) Group ids by (`source`, `entity`). (2) For each group, list committed files `part-*.parquet` under `root/<source>/<entity>/dt=*/`. (3) For each file, read only `_record_id`; if no id matches (`pc.is_in`), continue. (4) Read the full file and filter out matches. (5) If nothing remains, unlink the file. Otherwise write the rest to `.<final name>.tmp-<ulid>` in the same directory with the U02-15 Parquet settings, `fsync`, and `os.replace` over the original. (6) Accumulate counts. (7) Log `store.lake.purged` (INFO) with the counts and the number of ids, never the ids. |
| Side effects | Rewrites or deletes lake files. |
| Errors | `StoreBusy` for sharing violations on replace or unlink (the temp is deleted first; the original stays intact); `ConfigError` for invalid ids or count; other `OSError` → `SchemaViolation`. |
| Concurrency | Runs only inside the exclusive `maintenance` job (spec 08 `exclusive_kinds`). Safe against concurrent `LakeWriter`s, which never touch final files. |
| Complexity and limits | One `_record_id` column scan of the entity; full rewrite only of matching files. |
| Security notes | TH02-16. |
| Tests | UT02-14, UT02-15, FT02-01, ST02-16 |

#### U02-22 `herness.store.lake_purge.LakeRetentionResult`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) |
| Purpose | Counts from `purge_partitions_before`. |
| Signature | `partitions_deleted: int`, `bytes_freed: int`, `skipped_unparsable: int`, `deferred_locked: int`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | All ≥ 0. |
| Algorithm | — |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-16 |

#### U02-23 `herness.store.lake_purge.purge_partitions_before`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `cutoff` | `date` | — | positional | partitions with `dt < cutoff` are deleted |
| `today` | `date` | — | keyword-only | caller's current UTC date |
| `root` | `Path \| None` | `None` | keyword-only | |

Returns `LakeRetentionResult`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Retention: delete whole `dt=` partitions older than the cutoff (spec 10 `retention.raw_lake_months`; the caller computes `cutoff`). |
| Preconditions | `cutoff ≤ today − 30 days`, else `ConfigError` (guards against a mis-computed cutoff). |
| Postconditions | No partition with parsed date < `cutoff` remains, except those deferred by locks. |
| Invariants | — |
| Algorithm | (1) Walk `root/<source>/<entity>/` for directories named `dt=YYYY-MM-DD`. (2) Unparsable names increment `skipped_unparsable` and log `store.lake.partition_unparsable` (WARNING, path relative to `root`). (3) For each partition with date < `cutoff`: check `is_relative_to(root)`, sum file sizes, `shutil.rmtree`; on `PermissionError` increment `deferred_locked`. (4) Log `store.lake.retention_applied` (INFO) with the counts. |
| Side effects | Deletes directories. |
| Errors | `ConfigError`. |
| Concurrency | Only inside the exclusive `maintenance` job. |
| Complexity and limits | O(partitions). |
| Security notes | Containment check before each deletion (TH02-01). |
| Tests | UT02-16 |

### 3.4 Warehouse files and `CURRENT` (`herness/store/warehouse.py`, `herness/store/_warehouse_rw.py`)

Files: `data/warehouse/wh-<build_id>.duckdb` (+ DuckDB's `wh-<build_id>.duckdb.wal` while open), pointer `data/warehouse/CURRENT` (one line: the build ID and `\n`), spill directory `data/warehouse/tmp/<build_id>/` (build only).

Health results follow the ENG §4 convention used by impl 03 and impl 09 (`herness.reports.render.health`): a plain tuple `tuple[Literal["ok", "degraded", "down"], str]` of status and reason. This spec calls that tuple a "health result"; no shared `HealthStatus` type exists in `herness.core.types` (OI-15 closed).

#### U02-24 `herness.store.warehouse.BUILD_ID_RE`

| Field | Content |
|---|---|
| Kind | constant |
| Purpose | Format check for `build_id` (spec 00 §5: `YYYYMMDD-HHMMSS-<ulid6>`). |
| Signature | `BUILD_ID_RE: Final = re.compile(r"^\d{8}-\d{6}-[0-9A-HJKMNP-TV-Z]{6}$")` (Crockford base32, upper case). |
| Preconditions | — |
| Postconditions | — |
| Invariants | Lexical order of valid IDs equals creation-time order to the second. |
| Algorithm | — |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | Every path built from a build ID is gated by this pattern (TH02-03). |
| Tests | UT02-17, ST02-03 |

#### U02-25 `herness.store.warehouse.new_build_id`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `now` | `datetime` | — | positional | timezone-aware UTC |

Returns `str`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Create a build ID. |
| Preconditions | `now.tzinfo` is UTC, else `ConfigError`. |
| Postconditions | Result matches `BUILD_ID_RE`. |
| Invariants | — |
| Algorithm | `now.strftime("%Y%m%d-%H%M%S")` + `-` + the last 6 characters of `T00-05 (herness.core.ids.new_ulid)()` (the random part, so two IDs in the same second differ; OI-10). |
| Side effects | None. |
| Errors | `ConfigError`. |
| Concurrency | Thread-safe. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-17 |

#### U02-26 `herness.store.warehouse.build_path`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `build_id` | `str` | — | positional | matches `BUILD_ID_RE` |
| `layout` | `DataLayout \| None` | `None` | keyword-only | `None` = `data_layout()` |

Returns `Path` = `<warehouse>/wh-<build_id>.duckdb`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Canonical warehouse file path. |
| Preconditions | Valid ID, else `SchemaViolation("invalid build_id")`. |
| Postconditions | Path is a direct child of `layout.warehouse`. |
| Invariants | — |
| Algorithm | Validate, join. |
| Side effects | None. |
| Errors | `SchemaViolation`. |
| Concurrency | Pure. |
| Complexity and limits | O(1). |
| Security notes | TH02-03. |
| Tests | UT02-17, ST02-03 |

#### U02-133 `herness.store.warehouse.build_exists`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `build_id` | `str` | — | positional | any string |
| `layout` | `DataLayout \| None` | `None` | keyword-only | `None` = `data_layout()` |

Returns `bool`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Tell a caller (impl 06 run start and resume) whether a build file exists, without opening it. |
| Preconditions | — |
| Postconditions | `True` exactly when `build_id` full-matches `BUILD_ID_RE` and `build_path(build_id)` is an existing regular file. |
| Invariants | — |
| Algorithm | (1) If `BUILD_ID_RE` does not full-match, return `False` (no path is built from an invalid ID). (2) Return `build_path(build_id, layout=layout).is_file()`. |
| Side effects | One `stat` call. |
| Errors | None raised; an `OSError` from `stat` returns `False`. |
| Concurrency | Thread-safe. The answer can change right after the call (retention may delete a file); callers pin builds through non-terminal runs (U02-105 step 1). |
| Complexity and limits | O(1). |
| Security notes | TH02-03 (ID gate before any path). |
| Tests | UT02-77 |

#### U02-27 `herness.store.warehouse.read_current`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `layout` | `DataLayout \| None` | `None` | keyword-only | |

Returns `str | None` (the promoted build ID; `None` when `CURRENT` does not exist).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Read the blue/green pointer (spec 00 §4). |
| Preconditions | — |
| Postconditions | A returned ID matches `BUILD_ID_RE` and its file exists. |
| Invariants | — |
| Algorithm | (1) If `CURRENT` is absent, return `None`. (2) Read at most 64 bytes as ASCII; strip whitespace. (3) If not a full match of `BUILD_ID_RE`, raise `SchemaViolation("CURRENT content invalid")`. (4) If `build_path(id)` does not exist, raise `NotFoundError(kind="build", key=id)`. (5) Return the ID. |
| Side effects | File read. |
| Errors | `SchemaViolation`, `NotFoundError`; `StoreBusy` on a sharing violation while the file is being replaced. |
| Concurrency | Thread-safe; atomic replace means a reader sees the old or the new content, never a mix. |
| Complexity and limits | O(1). |
| Security notes | Rejects traversal or arbitrary paths written into `CURRENT` (TH02-03). |
| Tests | UT02-18, ST02-03 |

#### U02-28 `herness.store.warehouse.CurrentPointer`

| Param (`__init__`) | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `layout` | `DataLayout \| None` | `None` | keyword-only | |
| `recheck_s` | `float` | `60.0` | keyword-only | 1–3,600 (spec 00 §4: re-check every 60 s) |
| `clock` | `Callable[[], float]` | `time.monotonic` | keyword-only | |

Method `get() -> str | None`; property `changed_at: float | None` (clock value of the last observed change).

| Field | Content |
|---|---|
| Kind | class |
| Purpose | Cached `read_current()` that re-reads at most once per `recheck_s` for long-running readers (CLI jobs, spec 05 tools; spec 09 keeps its own cache in `app/common/wh.py`). |
| Preconditions | — |
| Postconditions | `get()` returns the value read within the last `recheck_s` seconds. |
| Invariants | `_value` and `_read_at` updated together under `_lock`. |
| Algorithm | `get()`: (1) Under `threading.Lock`, if `_read_at is None` or `clock() − _read_at ≥ recheck_s`, call `read_current`, compare with the cached value, set `changed_at` and log `store.warehouse.current_changed` (INFO, old and new ID) on a change, update `_read_at`. (2) Return the cached value. Errors from `read_current` propagate and leave the cache unchanged. |
| Side effects | File read at most once per interval. |
| Errors | As U02-27. |
| Concurrency | Lock-protected (`_lock`). |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-19 |

#### U02-29 `herness.store.warehouse.open_readonly`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `build_id` | `str \| None` | `None` | positional | `None` = `read_current()` |
| `threads` | `int \| None` | `None` | keyword-only | 1–256; `None` = DuckDB default |
| `memory_limit` | `str \| None` | `None` | keyword-only | pattern of `BuildSettings.memory_limit`; `None` = DuckDB default |
| `layout` | `DataLayout \| None` | `None` | keyword-only | |

Returns `duckdb.DuckDBPyConnection`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | The hardened read-only connection every reader uses (design 02 §10; same settings as spec 05 §5.4.1). |
| Preconditions | A build ID resolves (else `NotFoundError(kind="current", key="CURRENT")`); the file exists. |
| Postconditions | Connection is read-only; external access disabled; configuration locked; extension auto-install and auto-load off. |
| Invariants | — |
| Algorithm | (1) Resolve `build_id`. (2) `duckdb.connect(str(build_path(id)), read_only=True, config={"autoinstall_known_extensions": False, "autoload_known_extensions": False, "threads": …, "memory_limit": …})` (keys omitted when `None`). (3) Execute `SET TimeZone = 'UTC'`, then `SET enable_external_access = false`, then `SET lock_configuration = true` (setting names are open-questions item 4, verified at Phase 3; T02-09 adds the check against the pinned DuckDB). (4) Return. |
| Side effects | Opens a file handle (shared lock). |
| Errors | `NotFoundError`; `StoreBusy` for a DuckDB `IOException` whose message contains "lock"; other `duckdb.Error` → `SchemaViolation("cannot open warehouse <build_id>")`. |
| Concurrency | One connection per process per build; callers create cursors per thread (`con.cursor()`). |
| Complexity and limits | O(1). |
| Security notes | TH02-04. Agents cannot read files, attach databases or change settings back. |
| Tests | UT02-20, ST02-04 |

#### U02-30 `herness.store.warehouse.BuildInfo`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) |
| Purpose | One warehouse file as seen by `list_builds`. |
| Signature | `build_id: str`, `path: Path`, `size_bytes: int` (file plus `.wal`), `status: Literal["building", "failed", "promoted", "retired", "unreadable", "locked"]`, `started_at: datetime \| None`, `finished_at: datetime \| None`, `is_current: bool`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `unreadable`/`locked` ⇒ `started_at is None`. |
| Algorithm | — |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-21 |

#### U02-31 `herness.store.warehouse.list_builds`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `layout` | `DataLayout \| None` | `None` | keyword-only | |

Returns `list[BuildInfo]`, newest `build_id` first.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Inventory for promotion, cleanup, `herness status` and the dashboard. |
| Preconditions | — |
| Postconditions | One entry per `wh-*.duckdb` whose stem matches `BUILD_ID_RE`; other names are ignored and logged `store.warehouse.foreign_file` (WARNING, file name). |
| Invariants | — |
| Algorithm | (1) Read `CURRENT` with `read_current`, treating `SchemaViolation` and `NotFoundError` as "no current". (2) For each file: open read-only (plain `duckdb.connect(path, read_only=True)` with extension auto-install and auto-load off), read `meta.build` row (`status`, `started_at`, `finished_at`); close. (3) Lock errors → `locked`; any other `duckdb.Error` or a missing `meta.build` row → `unreadable`. (4) Sort by `build_id` descending. |
| Side effects | Opens and closes each file. |
| Errors | None raised for per-file problems. |
| Concurrency | Safe with concurrent readers. |
| Complexity and limits | O(files); at most `keep_last + 3` files expected. |
| Security notes | — |
| Tests | UT02-21 |

#### U02-32 `herness.store.warehouse.delete_build_files`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `build_id` | `str` | — | positional | valid ID |
| `layout` | `DataLayout \| None` | `None` | keyword-only | |

Returns `Literal["deleted", "deferred", "absent"]`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Delete a warehouse file, its WAL and its spill directory, tolerating Windows file locks (design 02 §7). |
| Preconditions | `build_id` is not the current build, else `ConfigError("refusing to delete CURRENT build <id>")`. |
| Postconditions | `deleted`: none of the three paths exists. `deferred`: at least one could not be removed because it is open; the rest may be gone. `absent`: nothing existed. |
| Invariants | — |
| Algorithm | (1) Check not current. (2) Unlink `.duckdb.wal` then `.duckdb` (`missing_ok=True`); `shutil.rmtree(tmp/<id>, ignore_errors=False)` if present. (3) `PermissionError` (Windows sharing violation) on any step → log `store.warehouse.delete_deferred` (WARNING, `build_id`) and return `deferred`. (4) Log `store.warehouse.deleted` (INFO, `build_id`, `bytes`). |
| Side effects | Deletes files. |
| Errors | `ConfigError`; other `OSError` → `SchemaViolation`. |
| Concurrency | Called only from the exclusive build job (U02-105) and spec 10's deletion step 5 (same exclusivity). |
| Complexity and limits | O(1). |
| Security notes | Path built through `build_path` (TH02-03). |
| Tests | UT02-22, FT02-05 |

#### U02-33 `herness.store.warehouse.warehouse_health`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `now` | `datetime` | — | keyword-only | UTC |
| `stale_after_h` | `float` | `48.0` | keyword-only | > 0 |
| `layout` | `DataLayout \| None` | `None` | keyword-only | |

Returns a health result `tuple[Literal["ok", "degraded", "down"], str]` (status, reason; §3.4).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | `herness doctor` / `status` check (ENG §4 health). |
| Preconditions | — |
| Postconditions | `down`: no `CURRENT`, invalid pointer, or the build cannot be opened. `degraded`: `meta.build.status ≠ 'promoted'`, or `now − finished_at > stale_after_h`. `ok` otherwise. `reason` names the build ID and age in hours. |
| Invariants | — |
| Algorithm | (1) `read_current`. (2) `open_readonly`, read `meta.build`, close. (3) Classify. |
| Side effects | Opens and closes the file. |
| Errors | None raised (all mapped to `down`). |
| Concurrency | Thread-safe. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-24 |

#### U02-34 `herness.store._warehouse_rw.open_for_build`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `build_id` | `str` | — | positional | valid ID |
| `create` | `bool` | — | keyword-only | `True`: file must not exist; `False`: must exist |
| `cfg` | `BuildSettings` | — | keyword-only | U02-75 |
| `layout` | `DataLayout \| None` | `None` | keyword-only | |

Returns `duckdb.DuckDBPyConnection` (writable).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | The only writable warehouse connection (design 02 §10: only the build pipeline writes). |
| Preconditions | As `create` states, else `ConfigError`. Import restricted by contract `store-rw-restricted`. |
| Postconditions | Writable connection with extension auto-install and auto-load off, UTC time zone, spill directory `tmp/<build_id>/` created. |
| Invariants | — |
| Algorithm | (1) Check existence rule. (2) Create `layout.warehouse` and `tmp/<build_id>/`. (3) `duckdb.connect(path, read_only=False, config={"autoinstall_known_extensions": False, "autoload_known_extensions": False, "threads": cfg.threads or os.cpu_count(), "memory_limit": cfg.memory_limit, "temp_directory": <spill dir>, "preserve_insertion_order": False})`. (4) `SET TimeZone = 'UTC'`. External access stays enabled because staging reads the lake with `read_parquet`. (5) Log `store.warehouse.opened_for_build` (INFO, `build_id`, `create`). |
| Side effects | Creates the file (when `create`) and the spill directory. |
| Errors | `ConfigError`; `StoreBusy` for lock errors; other `duckdb.Error` → `SchemaViolation`. |
| Concurrency | Single writer process per file (spec 00 §4); the job layer's `exclusive_kinds` guarantees one build job at a time. |
| Complexity and limits | Memory bounded by `cfg.memory_limit`. |
| Security notes | TH02-05, TH02-12. |
| Tests | UT02-20, ST02-05, ST02-12 |

#### U02-35 `herness.store._warehouse_rw.write_current`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `build_id` | `str` | — | positional | valid ID; file exists |
| `layout` | `DataLayout \| None` | `None` | keyword-only | |

Returns `str | None` (the previous pointer value).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Atomically switch `CURRENT` (spec 00 §4; ENG §3.5 atomic write pattern). |
| Preconditions | File for `build_id` exists, else `NotFoundError`. |
| Postconditions | `CURRENT` contains `build_id + "\n"`. |
| Invariants | — |
| Algorithm | (1) Validate. (2) Read the previous value with `read_current` (errors → previous `None`). (3) Write `.CURRENT.tmp-<ulid>` in `layout.warehouse` (ASCII), `flush`, `os.fsync`. (4) `os.replace(tmp, CURRENT)`. (5) POSIX: `fsync` the directory. (6) Log `store.warehouse.current_switched` (INFO, `previous`, `build_id`). |
| Side effects | Replaces `CURRENT`. |
| Errors | `NotFoundError`; `StoreBusy` on `PermissionError` (a reader has `CURRENT` open during replace; temp removed before raising); other `OSError` → `SchemaViolation`. |
| Concurrency | Called only by `promote_build` inside the exclusive build job. |
| Complexity and limits | O(1). |
| Security notes | TH02-03, TH02-17. |
| Tests | UT02-23, FT02-04 |

### 3.5 Ops store core (`herness/store/ops/core.py`)

The names in this section are the core API of R-10: `connection()`, `run_write()`, `read_one()`, `read_all()`, `dump_json()`, `load_json()`, together with `migrate()`, `pending_migrations()` and `schema_version()` of §3.6. Every other spec uses these names; the aliases other specs used earlier are mapped in §13.4.

Module state (ENG §2.3 exception, listed in §13): `_path_override: Path | None`, a `threading.local()` holding (`pid`, `path`, `connection`), and a lock-protected registry of open connections for `reset_connections`. The test fixture `ops_store` (spec 11 `tests/support/`) calls `reset_connections(path=tmp_path / "ops.sqlite")` before and `reset_connections()` after each test.

#### U02-36 `herness.store.ops.core.OPS_JSON_MAX_BYTES`

| Field | Content |
|---|---|
| Kind | constant |
| Purpose | Default size cap for JSON TEXT values written to the ops store. |
| Signature | `OPS_JSON_MAX_BYTES: Final[int] = 65_536` (UTF-8 bytes). Callers with a larger contract pass `max_bytes` explicitly (spec 08 `task.checkpoint`: 4 MiB). |
| Preconditions | — |
| Postconditions | — |
| Invariants | — |
| Algorithm | — |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH02-07. |
| Tests | ST02-07 |

#### U02-37 `herness.store.ops.core.connection`

Returns `sqlite3.Connection` for the calling thread.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | One SQLite connection per thread per process, configured per spec 00 §4 and design 02 §5. |
| Preconditions | — |
| Postconditions | Connection has `journal_mode = wal`, `foreign_keys = ON`, `busy_timeout = 10000`, `synchronous = NORMAL`, `trusted_schema = OFF`, `temp_store = MEMORY`, `cache_size = -65536`; `row_factory = sqlite3.Row`; autocommit mode (`isolation_level=None`). |
| Invariants | A cached connection is reused only when its `pid` equals `os.getpid()` and its path equals the effective path (fork and path-change safety). |
| Algorithm | (1) Effective path = `_path_override` or `data_layout().ops_db`. (2) If the thread-local entry matches pid and path, return its connection. (3) Check `sqlite3.sqlite_version_info ≥ (3, 38, 0)` (STRICT tables, built-in JSON) and that `PRAGMA compile_options` lists `ENABLE_FTS5`; otherwise raise `ConfigError("SQLite 3.38+ with FTS5 required")`. (4) Create the parent directory. (5) `sqlite3.connect(path, timeout=10.0, isolation_level=None, check_same_thread=True)`. (6) Apply the PRAGMAs; if `journal_mode` does not return `wal`, raise `ConfigError`. (7) Register and cache; log `store.ops.connected` (DEBUG, thread name). |
| Side effects | Opens a file handle; may create `ops.sqlite`, `-wal` and `-shm`. |
| Errors | `ConfigError`; `StoreBusy` when the PRAGMAs hit `database is locked`; other `sqlite3.Error` → `SchemaViolation("cannot open ops store")`. |
| Concurrency | Per-thread. |
| Complexity and limits | O(1) after the first call per thread. |
| Security notes | `trusted_schema = OFF` stops schema-defined functions in views or triggers from running with elevated trust. |
| Tests | UT02-25, UT02-26 |

#### U02-38 `herness.store.ops.core.run_write`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `fn` | `Callable[[sqlite3.Connection], T]` | — | positional | performs only SQL on the given connection; no network, sleep, file or other-store I/O |
| `op` | `str` | — | keyword-only | low-cardinality operation name, `^[a-z_]{1,64}$` |

Returns `T`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Run a write callback in one `BEGIN IMMEDIATE` transaction with the spec 08 `sqlite_write` retry policy (design 02 §7). |
| Preconditions | The thread's connection is not already in a transaction (nested calls raise `ConfigError("nested run_write in <op>")`). To combine writes of several areas in one transaction (for example R-33: a memory approval and its `review_item` decision; R-21: a checkpoint key), `fn` calls the area functions that accept a `conn` keyword with the connection it received, instead of nesting `run_write`. |
| Postconditions | On return, the transaction committed. On exception, it rolled back. |
| Invariants | — |
| Algorithm | (1) Define `attempt()`: call `T08-08 (herness.core.resilience.fault_point)("sqlite.write", kind=op)` (point named in impl 08's registry, R-40); `conn = connection()`; execute `BEGIN IMMEDIATE`; call `fn(conn)`; execute `COMMIT`; return the result. On any exception: if `conn.in_transaction`, execute `ROLLBACK`; then map the exception: `sqlite3.OperationalError` whose lower-cased message contains `locked` or `busy` → `StoreBusy(op)`; `sqlite3.IntegrityError` → `SchemaViolation("ops constraint failed in <op>: <sqlite message>")` (SQLite constraint messages name table and column, not values); other `sqlite3.Error` → `SchemaViolation("ops write failed in <op>: <class name>")`; `HernessError` passes unchanged. (2) Return `T08-07 (herness.core.resilience.retry_call)("sqlite_write", attempt)`. (3) Measure the transaction time; above 1 s log `store.ops.slow_write` (WARNING, `op`, `duration_ms`). |
| Side effects | Writes via `fn`. |
| Errors | `StoreBusy` after the policy's 6 attempts; `SchemaViolation`; `ConfigError`; any `HernessError` raised by `fn`. |
| Concurrency | Per-thread connection; SQLite serialises writers across threads and processes. |
| Complexity and limits | Policy `sqlite_write`: 6 attempts, max elapsed 30 s (spec 08 §5.2). |
| Security notes | TH02-18. |
| Tests | UT02-27, UT02-28, FT02-02, ST02-18 |

#### U02-39 `herness.store.ops.core.read_one`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `sql` | `str` | — | positional | a module constant of the calling ops submodule; parameterised |
| `params` | `Sequence[object] \| Mapping[str, object]` | `()` | positional | |

Returns `sqlite3.Row | None`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Single-row read for ops submodules. |
| Preconditions | — |
| Postconditions | Returns the first row or `None`. |
| Invariants | — |
| Algorithm | `connection().execute(sql, params).fetchone()`. |
| Side effects | None. |
| Errors | Busy or locked → `StoreBusy` (no retry here; the caller's policy decides); other `sqlite3.Error` → `SchemaViolation`. |
| Concurrency | Per-thread connection; WAL readers do not block writers. |
| Complexity and limits | Query-dependent. |
| Security notes | ENG §3.5 parameterisation. |
| Tests | UT02-27 |

#### U02-40 `herness.store.ops.core.read_all`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `sql` | `str` | — | positional | as U02-39 |
| `params` | as U02-39 | `()` | positional | |
| `max_rows` | `int` | `100_000` | keyword-only | 1–1,000,000 |

Returns `list[sqlite3.Row]`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Bounded multi-row read. |
| Preconditions | — |
| Postconditions | At most `max_rows` rows. |
| Invariants | — |
| Algorithm | `fetchmany(max_rows + 1)`; more than `max_rows` → `SchemaViolation("read exceeded <max_rows> rows")`. |
| Side effects | None. |
| Errors | As U02-39, plus the row cap. |
| Concurrency | As U02-39. |
| Complexity and limits | Memory bounded by `max_rows`. |
| Security notes | TH02-07 (unbounded reads). |
| Tests | UT02-27 |

#### U02-41 `herness.store.ops.core.dump_json`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `value` | `object` | — | positional | JSON-shaped |
| `field` | `str` | — | keyword-only | column name for messages |
| `max_bytes` | `int` | `OPS_JSON_MAX_BYTES` | keyword-only | 1 KiB–16 MiB |

Returns `str`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Canonical JSON TEXT for ops columns (design 02 §5: JSON as TEXT). |
| Preconditions | — |
| Postconditions | Output is compact (`separators=(",", ":")`), keys sorted, `ensure_ascii=False`, UTF-8 length ≤ `max_bytes`. |
| Invariants | — |
| Algorithm | `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False, default=_encode)` where `_encode` maps `Decimal` → string (spec 00 §8), aware `datetime` → ts-text, `date` → ISO date, `Path` → POSIX string, `enum.Enum` → `.value`, pydantic `BaseModel` → `model_dump(mode="json")`; anything else raises. Then check the byte length. |
| Side effects | None. |
| Errors | `SchemaViolation("JSON for <field> not serialisable: <type name>")`, `SchemaViolation("JSON for <field> exceeds <max_bytes> bytes")`, NaN/Infinity → `SchemaViolation`. |
| Concurrency | Pure. |
| Complexity and limits | O(size). |
| Security notes | TH02-07. |
| Tests | UT02-29, ST02-07 |

#### U02-42 `herness.store.ops.core.load_json`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `text` | `str \| None` | — | positional | |
| `field` | `str` | — | keyword-only | |

Returns `object` (`None` for `None`).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Parse JSON TEXT columns. |
| Preconditions | — |
| Postconditions | Decimal strings stay strings (callers convert). |
| Invariants | — |
| Algorithm | `json.loads(text)`; `json.JSONDecodeError` → raise. |
| Side effects | None. |
| Errors | `SchemaViolation("invalid JSON in <field>")`. |
| Concurrency | Pure. |
| Complexity and limits | O(size). |
| Security notes | Standard library JSON only (ENG §3.5). |
| Tests | UT02-30 |

#### U02-43 `herness.store.ops.core.reset_connections`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `path` | `Path \| None` | `None` | keyword-only | new override; `None` clears it |

Returns `None`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Close every registered connection and set the path override (test fixture, spec 11 generator, `herness init --data-root`). |
| Preconditions | No thread is inside `run_write` (callers ensure). |
| Postconditions | Registry empty; override set; each thread opens a fresh connection on next use. |
| Invariants | — |
| Algorithm | Under the registry lock: close each connection (suppress `sqlite3.ProgrammingError` for connections owned by other threads, which are then invalidated by path or pid mismatch), clear, set `_path_override`. |
| Side effects | Closes handles. |
| Errors | None. |
| Concurrency | Registry lock. |
| Complexity and limits | O(connections). |
| Security notes | — |
| Tests | UT02-31 |

### 3.6 Ops migrations (`herness/store/ops/migrate.py`, `herness/store/migrations/`)

Migration file rules (ENG §3.5):

| Rule | Detail |
|---|---|
| Name | `NNN_<slug>.sql`, `NNN` three digits, slug `^[a-z0-9_]+$`. Files starting with another character are ignored. Numbers are unique but not contiguous: each number lies in the range of its owning spec (`MIGRATION_RANGES`, U02-129, R-11). |
| Ownership | Migrations 001–006 (this spec) create every ops table named in the design specs. A table or column that exists only in an implementation spec gets a migration in its owner's range, listed as a unit in the owner spec (R-11). The owner's task card adds the file; this spec's runner applies it. |
| Dependencies | A migration may depend only on migrations 001–009 and on lower-numbered migrations of its own range. It never depends on another owner's range, so pending migrations can be applied in numeric order whatever the highest applied number is. |
| Location | Every migration file of every owner lives in `herness/store/migrations/` (package resource `herness.store.migrations`). |
| Content | Only `CREATE TABLE`, `CREATE INDEX`, `CREATE UNIQUE INDEX`, `CREATE TRIGGER`, `CREATE VIRTUAL TABLE`, `ALTER TABLE`, `DROP …`, and data `INSERT`/`UPDATE` for backfills. No `PRAGMA`, `BEGIN`, `COMMIT`, `ATTACH`, `VACUUM`. |
| Tables | Ordinary tables are `STRICT`. Timestamp columns carry `CHECK (col IS NULL OR (length(col) = 27 AND col GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'))`. JSON columns carry `CHECK (col IS NULL OR json_valid(col))`. Closed enumerations from design 02 §5 carry `CHECK (col IN (…))`. |
| Immutability | An applied file never changes (checksum). A fix is a new migration. Adding an enum value to a `CHECK` needs a table-rebuild migration (create new table, copy, drop, rename, recreate indexes and triggers) in one transaction. |
| Checksum | SHA-256 hex of the file bytes after replacing CRLF with LF. |

#### U02-44 `herness.store.ops.migrate.MigrationReport`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) |
| Purpose | Result of `migrate()`. |
| Signature | `applied: tuple[str, ...]` (names `NNN_slug` applied now), `version: int` (highest applied), `duration_ms: int`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `version ≥ 0`. |
| Algorithm | — |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-32 |

#### U02-45 `herness.store.ops.migrate.migrate`

Returns `MigrationReport`. No parameters (the path comes from `connection()`).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Create and upgrade the ops store (design 02 §5; spec 09 `herness init`, worker start, `herness doctor --fix-hints`). |
| Preconditions | — |
| Postconditions | Every migration file of every owner range is applied exactly once; `schema_migration` has one row per file with matching checksums. |
| Invariants | — |
| Algorithm | (1) Ensure `schema_migration` exists (`CREATE TABLE IF NOT EXISTS schema_migration (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL) STRICT`) through `run_write(op="migrate_bootstrap")`. (2) Discover files in package resource `herness.store.migrations` (`importlib.resources.files`); parse names. A version that lies in no range of `MIGRATION_RANGES` → `MigrationError(out_of_range)`; two files with the same version → `MigrationError(duplicate_version)`. Gaps between versions are allowed (R-11). (3) Read applied rows. An applied version without a file → `MigrationError(unknown_applied)` (database is newer than the code). A checksum mismatch → `MigrationError(checksum_mismatch)`. (4) Pending = discovered versions without a `schema_migration` row, sorted ascending (numeric order, R-11). A pending version lower than the highest applied version is applied normally (an owner added a migration to its range after a higher range was applied) and logged `store.ops.migration_out_of_order` (INFO: `version`, `name`, `highest_applied`). (5) For each pending version in order, `run_write(op="migrate")` with a callback that: re-reads `schema_migration` for that version and returns if present (another process applied it); splits the file into statements by accumulating lines until `sqlite3.complete_statement` is true (trigger bodies stay whole); executes each; inserts the `schema_migration` row with ts-text now. A `SchemaViolation` from `run_write` becomes `MigrationError(apply_failed)` with the statement index. (6) Log `store.ops.migrated` (INFO: `version`, `name`, `owner`, `duration_ms`) per file, where `owner` is the spec number from `MIGRATION_RANGES`. (7) Return the report. |
| Side effects | DDL on `ops.sqlite`. |
| Errors | `MigrationError`; `StoreBusy` after retries; `ConfigError` from `connection()`. |
| Concurrency | Safe across processes: `BEGIN IMMEDIATE` serialises and step 4 re-checks inside the transaction. |
| Complexity and limits | Fresh database < 2 s (BT02-07). |
| Security notes | TH02-15. |
| Tests | UT02-32…UT02-35, UT02-70, UT02-71, IT02-01, ST02-15 |

#### U02-46 `herness.store.ops.migrate.pending_migrations`

Returns `list[str]` (names not yet applied, in order).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | For `herness doctor` ("ops migrations current", spec 09) and `ops_health`. |
| Preconditions | — |
| Postconditions | Empty when current. |
| Invariants | — |
| Algorithm | Steps (2)–(4) of U02-45 without applying; a missing `schema_migration` table means all files are pending. The list includes pending versions below the highest applied version (U02-45 step 4). |
| Side effects | None. |
| Errors | As U02-45 steps 2–3. |
| Concurrency | Read-only. |
| Complexity and limits | O(files). |
| Security notes | — |
| Tests | UT02-40, UT02-71 |

#### U02-47 `herness.store.ops.migrate.schema_version`

Returns `int` (highest applied version; 0 when none).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Report the schema version. Because owner ranges are independent (R-11), this number alone does not prove the store is current; `pending_migrations()` does. |
| Preconditions | — |
| Postconditions | — |
| Invariants | — |
| Algorithm | `SELECT coalesce(max(version), 0) FROM schema_migration`; missing table → 0. |
| Side effects | None. |
| Errors | `StoreBusy`, `SchemaViolation` per U02-39. |
| Concurrency | Read-only. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-40 |

#### U02-48 `herness.store.ops.migrate.ops_health`

Returns a health result `tuple[Literal["ok", "degraded", "down"], str]` (status, reason; §3.4).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Health check for `herness doctor` (ENG §4). |
| Preconditions | — |
| Postconditions | `down`: `connection()` fails or `SELECT 1` fails. `degraded`: pending migrations, or `PRAGMA journal_mode` ≠ `wal`. `ok` otherwise. |
| Invariants | — |
| Algorithm | Run the checks; map every exception to `down` with the error class name as reason. |
| Side effects | None. |
| Errors | None raised. |
| Concurrency | Per-thread connection. |
| Complexity and limits | O(files). |
| Security notes | — |
| Tests | UT02-41 |

#### U02-129 `herness.store.ops.migrate.MIGRATION_RANGES`

| Field | Content |
|---|---|
| Kind | constant |
| Purpose | The migration number range of each owning spec (R-11). |
| Signature | `MIGRATION_RANGES: Final[tuple[tuple[int, int, str], ...]]` = (first, last, owner spec): (1, 9, `02`), (10, 19, `01`), (20, 29, `03`), (30, 39, `05`), (40, 49, `06`), (50, 59, `08`), (70, 79, `07`), (80, 89, `10`), (90, 99, `09`). Numbers 060–069 and 100 and above belong to no owner. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Ranges do not overlap and are sorted by `first`. This spec uses 001–006; 007–009 stay free for later spec 02 migrations. |
| Algorithm | Lookup helper `_owner_of(version: int) -> str \| None` returns the owner of the range that contains `version`, else `None` (used by U02-45 step 2 and step 6). |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | O(ranges). |
| Security notes | TH02-15 (a stray file outside every range stops startup instead of being applied). |
| Tests | UT02-70 |

#### U02-49 `herness/store/migrations/001_ingestion_health.sql`

| Field | Content |
|---|---|
| Kind | SQL file |
| Purpose | Tables of design 02 §5.1. |
| Signature | Creates `watermark`, `sync_slice`, `file_ingest`, `source_health` with the columns, types and constraints of §4.3.1; index `sync_slice_status` on (`status`, `updated_at`). |
| Preconditions | Fresh database or version 0. |
| Postconditions | Four tables exist. |
| Invariants | — |
| Algorithm | Plain DDL, one statement per object. |
| Side effects | DDL. |
| Errors | Via U02-45. |
| Concurrency | Via U02-45. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-32, UT02-37, UT02-42 |

#### U02-50 `herness/store/migrations/002_jobs.sql`

| Field | Content |
|---|---|
| Kind | SQL file |
| Purpose | Tables of design 02 §5.2. |
| Signature | Creates `job`, `worker`, `resilience_event` (§4.3.2); indexes `job_idem_active` = `UNIQUE job(idem_key) WHERE status IN ('queued','running')`, `job_claim` on `job(status, gpu_class, scheduled_for, priority)`, `resilience_event_kind_ts` on `resilience_event(kind, ts)`, plus `resilience_event_ts` on `resilience_event(ts)` (retention purge). |
| Preconditions | Version 1. |
| Postconditions | Three tables, four indexes. |
| Invariants | — |
| Algorithm | Plain DDL. |
| Side effects | DDL. |
| Errors | Via U02-45. |
| Concurrency | Via U02-45. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-32, UT02-37 |

#### U02-51 `herness/store/migrations/003_runs_evidence.sql`

| Field | Content |
|---|---|
| Kind | SQL file |
| Purpose | Tables of design 02 §5.3. |
| Signature | Creates `run`, `task`, `finding`, `evidence`, `evidence_use` (§4.3.3); indexes `task_dedup` = `UNIQUE task(run_id, json_extract(spec, '$.dedup_key'))`, `task_run_status` on `task(run_id, status)`, `finding_run_status` on `finding(run_id, status)`, plus `evidence_run` on `evidence(run_id)` and `run_status` on `run(status)` (active-build lookup, U02-105). |
| Preconditions | Version 2. |
| Postconditions | Five tables, five indexes. |
| Invariants | — |
| Algorithm | Plain DDL. The expression index requires `json_extract` to be deterministic (it is in SQLite ≥ 3.38). |
| Side effects | DDL. |
| Errors | Via U02-45. |
| Concurrency | Via U02-45. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-32, UT02-38, UT02-39 |

#### U02-52 `herness/store/migrations/004_memory.sql`

| Field | Content |
|---|---|
| Kind | SQL file |
| Purpose | Tables of design 02 §5.4 and the FTS5 index. |
| Signature | Creates `memory_item`, `recommendation`, `decision_log`, `outcome` (§4.3.4); virtual table `memory_fts` = FTS5 over (`content`, `kind`) with `content='memory_item'`, `content_rowid='rowid'`, `tokenize='unicode61 remove_diacritics 2'`; triggers `memory_item_ai` (AFTER INSERT: insert new rowid, content, kind into `memory_fts`), `memory_item_ad` (AFTER DELETE: insert the FTS5 `'delete'` command row with old rowid, content, kind), `memory_item_au` (AFTER UPDATE OF `content`, `kind`: the `'delete'` command for old values, then insert new values); indexes `memory_item_layer_status` on (`layer`, `status`), `memory_item_kind_status` on (`kind`, `status`), `recommendation_run` on (`run_id`), `decision_log_rec` on (`rec_id`, `decided_at`), `outcome_rec_measurement` = `UNIQUE outcome(rec_id, measurement)`. |
| Preconditions | Version 3. |
| Postconditions | FTS stays in sync with `memory_item` for insert, update and delete. |
| Invariants | — |
| Algorithm | Plain DDL. `memory_item` is a rowid table (not `WITHOUT ROWID`) so `content_rowid` works. |
| Side effects | DDL. |
| Errors | Via U02-45. |
| Concurrency | Via U02-45. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-32, UT02-36 |

#### U02-53 `herness/store/migrations/005_review_chat_privacy.sql`

| Field | Content |
|---|---|
| Kind | SQL file |
| Purpose | Tables of design 02 §5.5. |
| Signature | Creates `review_item`, `chat_session`, `chat_message`, `deletion_request` (§4.3.5); indexes `review_item_status_kind` on (`status`, `kind`, `created_at`), `chat_session_user` on (`user_ref`, `last_active_at`), `chat_session_active` on (`last_active_at`), `chat_message_session` on (`session_id`, `created_at`), `deletion_request_status` on (`status`), `deletion_request_record` on (`record_id`). |
| Preconditions | Version 4. |
| Postconditions | Four tables, six indexes. |
| Invariants | — |
| Algorithm | Plain DDL. |
| Side effects | DDL. |
| Errors | Via U02-45. |
| Concurrency | Via U02-45. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-32, UT02-39 |

#### U02-54 `herness/store/migrations/006_metric_sample.sql`

| Field | Content |
|---|---|
| Kind | SQL file |
| Purpose | Component metrics table required by ENG §4 (ENG delta E5; R-12: table here, writer `herness.store.ops.metrics.record_metric_samples` and retention in impl 08; DD02-04). |
| Signature | Creates `metric_sample` (§4.3.6); indexes `metric_sample_name_ts` on (`name`, `ts`), `metric_sample_ts` on (`ts`). |
| Preconditions | Version 5. |
| Postconditions | Table exists. |
| Invariants | — |
| Algorithm | Plain DDL. |
| Side effects | DDL. |
| Errors | Via U02-45. |
| Concurrency | Via U02-45. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-32 |

### 3.7 Ops functions owned by spec 02 (`herness/store/ops/shared.py`, `herness/store/ops/__init__.py`)

Every `review_item` function lives in this module (R-08). Other specs reference these units instead of defining their own (R-09): impl 03 (`create_review_item_if_absent`, `list_review_items` with `decided_after` and `payload_match` (impl 03 RQ-01, RQ-02), `count_review_items`), impl 07 (`create_review_item`, `decide_review_item`, `update_review_payload`, `ReviewItem`), impl 09 (`get_review_item`, `list_review_items`, `decide_review_item`). Earlier names used by those specs are mapped in §13.4. `ReviewKind` = `Literal["mapping_suggestion", "label_check", "memory_write", "weight_change"]`; `ReviewStatus` = `Literal["pending", "approved", "rejected"]`; both are module-level type aliases of this module.

#### U02-55 `herness.store.ops.shared.ReviewItem`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) |
| Purpose | Typed `review_item` row. |
| Signature | `item_id: str`, `kind: Literal["mapping_suggestion", "label_check", "memory_write", "weight_change"]`, `payload: Mapping[str, object]` (read-only `MappingProxyType`), `status: Literal["pending", "approved", "rejected"]`, `created_at: datetime`, `decided_by: str \| None`, `decided_at: datetime \| None`, `note: str \| None`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `status == "pending"` ⇔ `decided_at is None` ⇔ `decided_by is None`. |
| Algorithm | Classmethod `from_row(row: sqlite3.Row)` parses JSON and timestamps. |
| Side effects | — |
| Errors | `SchemaViolation` from `load_json` or parse-ts. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-43 |

#### U02-56 `herness.store.ops.shared.create_review_item`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `kind` | `ReviewKind` literal | — | positional | one of the four kinds |
| `payload` | `Mapping[str, object]` | — | positional | JSON object; ≤ 64 KiB; no ticket text (payload schemas owned by 03, 04, 07) |
| `now` | `datetime` | — | keyword-only | UTC |
| `conn` | `sqlite3.Connection \| None` | `None` | keyword-only | when given, the caller is inside its own `run_write` callback |

Returns `str` (`item_id` = `rev_<ulid>`).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Insert a pending review item (design 02 §5.5, shared table owned here). |
| Preconditions | `payload` is a mapping, else `SchemaViolation`. |
| Postconditions | Row with `status = 'pending'`, decided fields NULL. |
| Invariants | — |
| Algorithm | (1) `item_id = "rev_" + new_ulid()`. (2) `payload_text = dump_json(payload, field="payload")`. (3) SQL `INSERT INTO review_item (item_id, kind, payload, status, created_at) VALUES (?, ?, ?, 'pending', ?)` executed on `conn` if given, else inside `run_write(op="review_item_create")`. (4) Log `store.ops.review_item_created` (INFO, `item_id`, `kind`). |
| Side effects | One row. |
| Errors | `SchemaViolation` (payload), `StoreBusy`. |
| Concurrency | Per `run_write`. Idempotency is the caller's: impl 03 uses `create_review_item_if_absent` (U02-130); impl 07 keys by `(task_id, content_hash)` and calls this function with `conn` inside its own transaction. |
| Complexity and limits | O(payload size). |
| Security notes | TH02-07. |
| Tests | UT02-43, ST02-07 |

#### U02-57 `herness.store.ops.shared.get_review_item`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `item_id` | `str` | — | positional | `^rev_[0-9A-HJKMNP-TV-Z]{26}$` |

Returns `ReviewItem`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Fetch one item. |
| Preconditions | Valid format, else `NotFoundError`. |
| Postconditions | — |
| Invariants | — |
| Algorithm | `read_one("SELECT … FROM review_item WHERE item_id = ?")`; `None` → `NotFoundError(kind="review_item", key=item_id)`. |
| Side effects | None. |
| Errors | `NotFoundError`, `StoreBusy`. |
| Concurrency | Read. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-45 |

#### U02-58 `herness.store.ops.shared.list_review_items`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `kind` | `ReviewKind \| None` | `None` | keyword-only | |
| `status` | `ReviewStatus \| None` | `None` | keyword-only | exclusive with `statuses` |
| `statuses` | `Collection[ReviewStatus] \| None` | `None` | keyword-only | 1–3 distinct values; exclusive with `status` |
| `decided_after` | `datetime \| tuple[datetime, str] \| None` | `None` | keyword-only | UTC. A `datetime` returns rows with `decided_at` strictly after it; a tuple is a keyset cursor (`decided_at`, `item_id`). Either form returns decided rows only (impl 03 RQ-01) |
| `payload_match` | `Mapping[str, str] \| None` | `None` | keyword-only | 1–8 top-level payload keys, each `^[a-z_][a-z0-9_]{0,63}$`; values ≤ 1,024 characters; equality on each key (impl 03 RQ-02) |
| `limit` | `int` | `100` | keyword-only | 1–5,000 |
| `offset` | `int` | `0` | keyword-only | ≥ 0; must be 0 when `decided_after` is given |

Returns `list[ReviewItem]`. Order: by `created_at`, then `item_id`, ascending; when `decided_after` is given, by `decided_at`, then `item_id`, ascending.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Review queue listing (impl 09 review page and `herness review-queue list`; impl 03 label sync paging by decision time). |
| Preconditions | Ranges and exclusivity rules, else `ConfigError`. |
| Postconditions | With `decided_after = t`, every returned row has `decided_at > t`. With `decided_after = (t, i)`, every returned row has `(decided_at, item_id) > (t, i)` in lexical order of ts-text and `item_id`. With `payload_match`, every returned row's payload holds each given key with a value equal to the given string. |
| Invariants | — |
| Algorithm | (1) Validate. (2) Status set = `[status]`, `statuses`, or all three. (3) Choose one of two fixed SQL constants (no string building): the created-order query `SELECT … FROM review_item WHERE (? IS NULL OR kind = ?) AND status IN (SELECT value FROM json_each(?)) ORDER BY created_at, item_id LIMIT ? OFFSET ?`, or the decided-order query `… WHERE (? IS NULL OR kind = ?) AND status IN (SELECT value FROM json_each(?)) AND decided_at IS NOT NULL AND (decided_at > ? OR (decided_at = ? AND item_id > ?)) ORDER BY decided_at, item_id LIMIT ?`. Both constants also carry the payload predicate `AND (? IS NULL OR NOT EXISTS (SELECT 1 FROM json_each(?) AS m WHERE json_extract(review_item.payload, '$.' \|\| m.key) IS NOT m.value))`, bound to the `payload_match` object serialised with `dump_json` (or NULL), so keys and values reach SQL only as data. The status set is bound as one JSON array text; the cursor time as ts-text. A plain `datetime` `t` is bound as the cursor (`t`, `rev_` followed by 26 `Z`), which no valid `item_id` exceeds, so only rows with `decided_at > t` match. (4) Parse rows with `ReviewItem.from_row`. |
| Side effects | None. |
| Errors | `ConfigError`, `StoreBusy`. |
| Concurrency | Read. |
| Complexity and limits | Uses index `review_item_status_kind`; at most 5,000 rows per call. |
| Security notes | TH02-07 (bounded page). |
| Tests | UT02-46, UT02-75, UT02-79 |

#### U02-59 `herness.store.ops.shared.decide_review_item`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `item_id` | `str` | — | positional | |
| `status` | `Literal["approved", "rejected"]` | — | positional | |
| `decided_by` | `str` | — | keyword-only | `^[0-9a-f]{32}$` (spec 09 `user_ref`) or `system` |
| `note` | `str \| None` | `None` | keyword-only | ≤ 2,000 characters; for `label_check` a JSON object string per spec 03 §4.6 |
| `now` | `datetime` | — | keyword-only | UTC |
| `conn` | `sqlite3.Connection \| None` | `None` | keyword-only | when given, the caller is inside its own `run_write` callback (impl 07 `MemoryStore.approve` / `MemoryStore.reject`, R-33; unblocks T07-09) |

Returns `ReviewItem` (after the update).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Record a human decision on a review item and audit it (spec 10 §4.6 `review_decision`). This is the only function that decides a `review_item` (R-33). Role checks are the caller's (impl 09). |
| Preconditions | Arguments valid, else `ConfigError`. An item of kind `memory_write` is decided with `conn` given, from inside the transaction of impl 07's `MemoryStore.approve` or `MemoryStore.reject`, so the memory item and its review item change together (R-33). The one exception is the privacy purge (R-54): impl 07 `MemoryStore.purge` rejects a pending `memory_write` item with `decided_by="system"` and `status="rejected"` without `conn`, because the memory item is being deleted anyway (C15). Any other `memory_write` decision without `conn` raises `ConfigError("memory_write items are decided through MemoryStore.approve or MemoryStore.reject")` before any write. |
| Postconditions | Row has the new status, `decided_by`, `decided_at = now`, `note`; one audit line exists. With `conn`, both become durable when the caller's transaction commits. |
| Invariants | A decided item is never decided again. |
| Algorithm | Run the steps on `conn` if given, else inside `run_write(op="review_item_decide")`: (1) `SELECT kind, status FROM review_item WHERE item_id = ?`; missing → `NotFoundError`. (2) `kind = 'memory_write'`, `conn is None`, and not (`status == "rejected"` and `decided_by == "system"`) → `ConfigError` (precondition). (3) `status ≠ 'pending'` → `ReviewItemConflict`. (4) `UPDATE review_item SET status = ?, decided_by = ?, decided_at = ?, note = ? WHERE item_id = ? AND status = 'pending'`. (5) Call `T10-05 (herness.core.audit.audit)("review_decision", decided_by, item_id=…, kind=…, status=…, decided_by=…, note_len=len(note or ""))` before the transaction commits; if it raises, the transaction rolls back (spec 10 §6: an action that cannot be audited does not happen; with `conn`, the error propagates and the caller's `run_write` rolls back). (6) Re-read and return. Log `store.ops.review_item_decided` (INFO, `item_id`, `kind`, `status`). |
| Side effects | One update; one audit line. |
| Errors | `NotFoundError`, `ReviewItemConflict`, `ConfigError`, `StoreBusy`, errors from `audit`. Callers that need a status value instead of an exception (impl 09 `decided` / `not_pending` / `not_found`) map `ReviewItemConflict` and `NotFoundError` themselves (§13.4). |
| Concurrency | Two concurrent deciders: the second sees `status ≠ 'pending'` after the first commits and gets `ReviewItemConflict`. |
| Complexity and limits | O(1). |
| Security notes | TH02-06. If the commit fails after the audit line was written (not reachable after `BEGIN IMMEDIATE` holds the lock, except for disk errors), `store.ops.audit_orphan` (ERROR, `item_id`) is logged. With `conn`, the audit line is written before the caller commits; if the caller's transaction rolls back later, the line records a decision attempt that did not persist (accepted residual risk, §7.7). |
| Tests | UT02-44, UT02-45, UT02-76, ST02-06 |

#### U02-60 `herness.store.ops.shared.approved_mapping_suggestions`

Returns `list[ReviewItem]` of kind `mapping_suggestion` with status `approved`, ordered by `decided_at`, `item_id`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Input to `core.service_map` (design 02 §4.3, third precedence). |
| Preconditions | — |
| Postconditions | Every returned payload is a JSON object. |
| Invariants | — |
| Algorithm | One SELECT; parse rows. |
| Side effects | None. |
| Errors | `StoreBusy`, `SchemaViolation`. |
| Concurrency | Read. |
| Complexity and limits | `read_all` cap 100,000. |
| Security notes | LLM04: only human-approved suggestions reach the model. |
| Tests | UT02-47, IT02-11 |

#### U02-61 `herness.store.ops.shared.builds_in_use`

Removed (R-68): reads of `run` belong to impl 06. Retention reads pinned builds through `T06-05 (herness.store.ops.runs.select_runs)` inside `cleanup_builds` (U02-105 step 1).

#### U02-130 `herness.store.ops.shared.create_review_item_if_absent`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `kind` | `ReviewKind` | — | positional | |
| `payload` | `Mapping[str, object]` | — | positional | as U02-56 |
| `match_keys` | `Sequence[str]` | — | keyword-only | 1–8 distinct top-level payload keys, each `^[a-z_][a-z0-9_]{0,63}$` and present in `payload` |
| `blocking_statuses` | `Collection[ReviewStatus]` | `("pending",)` | keyword-only | 1–3 distinct values |
| `now` | `datetime` | — | keyword-only | UTC |

Returns `tuple[str, bool]`: (`item_id`, `created`). `created` is `False` when an existing item blocked the insert; `item_id` is then that item's ID.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Idempotent insert for impl 03 spot checks, ensemble disagreements and mapping suggestions: create a pending item unless an item of the same kind with equal match-key values already has a blocking status. |
| Preconditions | Arguments valid, else `ConfigError`; payload rules of U02-56. |
| Postconditions | At most one item of `kind` with a blocking status exists per combination of match-key values created through this function. |
| Invariants | — |
| Algorithm | Inside one `run_write(op="review_item_create_if_absent")` (`BEGIN IMMEDIATE`): (1) Build the match object `{key: payload[key]}` for each match key and serialise it with `dump_json`. (2) Look up `SELECT item_id FROM review_item WHERE kind = ? AND status IN (SELECT value FROM json_each(?)) AND NOT EXISTS (SELECT 1 FROM json_each(?) AS m WHERE json_extract(review_item.payload, '$.' \|\| m.key) IS NOT m.value) ORDER BY created_at, item_id LIMIT 1`, binding `kind`, the blocking statuses as a JSON array and the match object (keys reach SQL only as JSON data, never as SQL text). (3) Found → return (`item_id`, `False`) and log `store.ops.review_item_exists` (DEBUG, `item_id`, `kind`). (4) Else call `create_review_item(kind, payload, now=now, conn=<the transaction connection>)` and return (new ID, `True`). |
| Side effects | Zero or one row. |
| Errors | `ConfigError`, `SchemaViolation` (payload), `StoreBusy`. |
| Concurrency | The lookup and insert share one `BEGIN IMMEDIATE` transaction, so two concurrent callers with the same match values create one item. |
| Complexity and limits | One scan of the `kind` and status slice of `review_item_status_kind`; payload values compared with `json_extract`. Expected pending items per kind < 100,000. |
| Security notes | TH02-07. Match keys are pattern-checked and reach SQL only as bound JSON data (ENG §3.5). |
| Tests | UT02-72 |

#### U02-131 `herness.store.ops.shared.count_review_items`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `kind` | `ReviewKind` | — | keyword-only | |
| `status` | `ReviewStatus` | — | keyword-only | |
| `group_by_payload` | `str \| None` | `None` | keyword-only | a top-level payload key, `^[a-z_][a-z0-9_]{0,63}$` |

Returns `dict[str, int]`, keys sorted. Without `group_by_payload`: `{"": total}`. With it: payload value (as text; a missing or JSON-null value maps to `""`) → count.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Open-item counts for impl 03 spot-check selection (for example pending `label_check` items per `question`). |
| Preconditions | Arguments valid, else `ConfigError`. |
| Postconditions | The counts sum to the number of rows with `kind` and `status`. |
| Invariants | — |
| Algorithm | One of two fixed SQL constants: `SELECT count(*) FROM review_item WHERE kind = ? AND status = ?`, or `SELECT coalesce(CAST(json_extract(payload, ?) AS TEXT), '') AS k, count(*) FROM review_item WHERE kind = ? AND status = ? GROUP BY k`, binding the JSON path `'$.' + group_by_payload` as a parameter. At most 100,000 groups (`read_all` cap). |
| Side effects | None. |
| Errors | `ConfigError`, `StoreBusy`, `SchemaViolation` (row cap). |
| Concurrency | Read. |
| Complexity and limits | One indexed scan of the slice. |
| Security notes | The key is pattern-checked and bound as a JSON path parameter. |
| Tests | UT02-73 |

#### U02-132 `herness.store.ops.shared.update_review_payload`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `item_id` | `str` | — | positional | `^rev_[0-9A-HJKMNP-TV-Z]{26}$` |
| `fields` | `Mapping[str, object]` | — | positional | 1–32 top-level keys, each `^[a-z_][a-z0-9_]{0,63}$`; JSON-shaped values |
| `conn` | `sqlite3.Connection \| None` | `None` | keyword-only | when given, the caller is inside its own `run_write` callback |

Returns `None`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Replace named top-level payload fields of one review item; used by impl 07 erasure to blank `content` of `memory_write` items (design 07 §9; privacy deletion step of R-54). |
| Preconditions | Arguments valid, else `ConfigError`. The item exists, else `NotFoundError(kind="review_item", key=item_id)`. |
| Postconditions | The payload holds the given values for the given keys; other keys, `status`, `decided_by`, `decided_at` and `note` are unchanged. Any status is accepted, so erasure also reaches decided items. |
| Invariants | The payload stays a JSON object within the U02-56 size cap. |
| Algorithm | On `conn` if given, else inside `run_write(op="review_item_payload")`: (1) `SELECT payload FROM review_item WHERE item_id = ?`; missing → `NotFoundError`. (2) `load_json`, replace the keys, `dump_json(field="payload")`. (3) `UPDATE review_item SET payload = ? WHERE item_id = ?`. (4) Log `store.ops.review_payload_updated` (INFO, `item_id`, `keys` = the key names). Values are never logged. |
| Side effects | One update. |
| Errors | `ConfigError`, `NotFoundError`, `SchemaViolation` (JSON), `StoreBusy`. |
| Concurrency | Per `run_write` or the caller's transaction. |
| Complexity and limits | O(payload size). |
| Security notes | TH02-07. Supports erasure (ASVS v5.0.0-V14). |
| Tests | UT02-74 |

#### U02-62 `herness.store.ops` package (`__init__.py`)

| Field | Content |
|---|---|
| Kind | module |
| Purpose | Public namespace for all ops functions of every area (§2.3, R-08). |
| Signature | Spec 02 blocks of `__all__`, in this order. Block "02 core": `connection`, `run_write`, `read_one`, `read_all`, `dump_json`, `load_json`, `reset_connections`, `OPS_JSON_MAX_BYTES`. Block "02 migrate": `MIGRATION_RANGES`, `migrate`, `pending_migrations`, `schema_version`, `ops_health`, `MigrationReport`. Block "02 shared": `ReviewItem`, `ReviewKind`, `ReviewStatus`, `create_review_item`, `create_review_item_if_absent`, `get_review_item`, `list_review_items`, `count_review_items`, `decide_review_item`, `update_review_payload`, `approved_mapping_suggestions`. Other owners append one block per area in the row order of the §2.3 table (01 `ingest`; 05 `evidence`; 06 `runs`, `findings`; 07 `memory`, `closed_loop`; 08 `jobs`, `tasks`, `worker`, `resilience`, `metrics`; 09 `chat`, `ui_reads`; 10 `privacy`). |
| Preconditions | — |
| Postconditions | Importing the package does not open a connection. `herness.store.ops.<name>` and `herness.store.ops.<area>.<name>` are the same object. |
| Invariants | No duplicate names across blocks; every name in `__all__` resolves to an attribute of the area named in its block header. The re-exported function `migrate` (R-10) shadows the package attribute of the area module `migrate` (R-08); the area stays importable by its dotted path (`from herness.store.ops.migrate import …`). No other re-exported name may equal an area name. |
| Algorithm | Explicit `from .<area> import …` lines per block, in the import order of §2.3 rule 5. |
| Side effects | None. |
| Errors | — |
| Concurrency | — |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-68 |

### 3.8 Vector store (`herness/store/vectors.py`)

#### U02-63 `herness.store.vectors.EMBEDDING_DIM`, `TICKET_EMBEDDING_SCHEMA`, `MEMORY_EMBEDDING_SCHEMA`

| Field | Content |
|---|---|
| Kind | constants |
| Purpose | Table schemas of design 02 §6. |
| Signature | `EMBEDDING_DIM = 1024`. `TICKET_EMBEDDING_SCHEMA` = `record_id` string not null, `entity` string not null, `service_id` string nullable, `opened_at` `timestamp("us", tz="UTC")` nullable, `content_hash` string not null, `model` string not null, `vector` `fixed_size_list(float32, 1024)` not null. `MEMORY_EMBEDDING_SCHEMA` = `memory_id` string not null, `layer` string not null, `kind` string not null, `status` string not null, `content_hash` string not null, `model` string not null, `vector` `fixed_size_list(float32, 1024)` not null. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Dimension matches bge-m3 (spec 03 §5.2). |
| Algorithm | — |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | — |
| Tests | UT02-49 |

#### U02-64 `herness.store.vectors.VectorStore`

| Param (`__init__`) | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `path` | `Path \| None` | `None` | positional | `None` = `data_layout().vectors` |

| Field | Content |
|---|---|
| Kind | class |
| Purpose | Thin wrapper over one LanceDB database directory; owners (spec 03 tickets, spec 07 memory) do upserts and searches on the table objects it returns. |
| Signature | Methods U02-65…U02-70. `TableName = Literal["ticket_embedding", "memory_embedding"]`. |
| Preconditions | — |
| Postconditions | `__init__` calls `lancedb.connect(str(path))` and stores the handle; no table is created. |
| Invariants | Only the two table names are ever opened or created. |
| Algorithm | See methods. |
| Side effects | Creates the directory on connect. |
| Errors | `lancedb`/`OSError` on connect → `SchemaViolation("cannot open vector store")`. |
| Concurrency | One instance per process; LanceDB handles concurrent readers and serialises commits (retry on commit conflict is mapped to `StoreBusy`). |
| Complexity and limits | — |
| Security notes | TH02-08, TH02-09. |
| Tests | UT02-49…UT02-52 |

#### U02-65 `herness.store.vectors.VectorStore.ensure_tables`

Returns `None`.

| Field | Content |
|---|---|
| Kind | method |
| Purpose | Create missing tables with the declared schemas; verify existing ones. |
| Preconditions | — |
| Postconditions | Both tables exist with exactly the declared field names and types. |
| Invariants | — |
| Algorithm | For each name: if absent from `table_names()`, `create_table(name, schema=…)`; else compare `table.schema` field names and types with the constant, raising on mismatch. Log `store.vectors.table_created` (INFO, `table`) on creation. |
| Side effects | May create tables. |
| Errors | `SchemaViolation("vector table <name> schema mismatch: <field>")`. |
| Concurrency | Idempotent; a concurrent create by another process is tolerated by re-listing on the "already exists" error. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-49 |

#### U02-66 `herness.store.vectors.VectorStore.table`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `name` | `TableName` | — | positional | |

Returns `lancedb.table.Table`.

| Field | Content |
|---|---|
| Kind | method |
| Purpose | Open a table for the owner's reads and writes. |
| Preconditions | Name in the allowlist, else `ConfigError`. |
| Postconditions | — |
| Invariants | — |
| Algorithm | `open_table(name)`; missing → `NotFoundError(kind="vector_table", key=name)`. |
| Side effects | None. |
| Errors | `ConfigError`, `NotFoundError`. |
| Concurrency | Per instance. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-49 |

#### U02-67 `herness.store.vectors.VectorStore.delete_ids`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `name` | `TableName` | — | positional | |
| `column` | `str` | — | positional | `ticket_embedding`: `record_id` or `content_hash`; `memory_embedding`: `memory_id` or `content_hash` |
| `ids` | `Collection[str]` | — | positional | each `^[A-Za-z0-9_:.\|-]{1,256}$`; 1–100,000 ids |

Returns `int` (rows deleted).

| Field | Content |
|---|---|
| Kind | method |
| Purpose | Safe deletion by key for spec 03 `purge_record` and spec 07 maintenance. |
| Preconditions | Column allowlisted; every id matches the pattern, else `ConfigError` before any deletion. |
| Postconditions | No row with those keys remains in the latest version. |
| Invariants | — |
| Algorithm | (1) Validate all ids. (2) Count matching rows (`count_rows(filter)`). (3) In chunks of 500 ids, `table.delete(f"{column} IN ('id1','id2',…)")`; ids cannot contain quotes because of the pattern. (4) Return the count from step 2. Log `store.vectors.deleted` (INFO, `table`, `rows`). |
| Side effects | New table version. |
| Errors | `ConfigError`; commit conflict → `StoreBusy`. |
| Concurrency | LanceDB commit serialisation. |
| Complexity and limits | O(ids / 500) delete calls. |
| Security notes | TH02-08 (filter injection). |
| Tests | UT02-50, ST02-08 |

#### U02-68 `herness.store.vectors.VectorStore.purge_history`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `name` | `TableName` | — | positional | |

Returns `None`.

| Field | Content |
|---|---|
| Kind | method |
| Purpose | Make deletions irreversible by compacting and removing old table versions (LLM08: deletion purges vectors). |
| Preconditions | — |
| Postconditions | Only the latest version remains; rows deleted earlier cannot be read by checking out an older version. |
| Invariants | — |
| Algorithm | (1) `table.compact_files()`. (2) `table.cleanup_old_versions(older_than=timedelta(0), delete_unverified=True)`. API names are verified against the pinned `lancedb` (OI-12); if the pinned version exposes `table.optimize(cleanup_older_than=timedelta(0), delete_unverified=True)` instead, that call replaces both steps. (3) Log `store.vectors.history_purged` (INFO, `table`). |
| Side effects | Deletes old version files. |
| Errors | Commit conflict → `StoreBusy`; other errors → `SchemaViolation`. |
| Concurrency | Called from exclusive jobs (`maintenance`, `build_pipeline`, `memory_maintenance`). |
| Complexity and limits | Rewrites fragments; proportional to table size. |
| Security notes | TH02-09. |
| Tests | UT02-51, ST02-09 |

#### U02-69 `herness.store.vectors.VectorStore.count`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `name` | `TableName` | — | positional | |

Returns `int`.

| Field | Content |
|---|---|
| Kind | method |
| Purpose | Row count for status, doctor and tests. |
| Preconditions | Table exists, else `NotFoundError`. |
| Postconditions | — |
| Invariants | — |
| Algorithm | `table.count_rows()`. |
| Side effects | None. |
| Errors | `NotFoundError`. |
| Concurrency | Read. |
| Complexity and limits | O(fragments). |
| Security notes | — |
| Tests | UT02-49 |

#### U02-70 `herness.store.vectors.VectorStore.health`

Returns a health result `tuple[Literal["ok", "degraded", "down"], str]` (status, reason; §3.4).

| Field | Content |
|---|---|
| Kind | method |
| Purpose | Health for `herness doctor`. |
| Preconditions | — |
| Postconditions | `down`: `table_names()` fails. `degraded`: a table is missing. `ok` otherwise. |
| Invariants | — |
| Algorithm | Check and classify; exceptions → `down` with the class name. |
| Side effects | None. |
| Errors | None raised. |
| Concurrency | Read. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-52 |

### 3.9 Build pipeline (`herness/model/*.py`)

#### U02-71 `herness.model.settings.ServiceOverride`

| Field | Content |
|---|---|
| Kind | class (pydantic v2, `extra="forbid"`, `strict=True`, `frozen=True`) |
| Purpose | One entry of `mappings.yaml: service_overrides` (first precedence in `core.service_map`, design 02 §4.3; D1). |
| Signature | `service_id: str` (pattern `^[a-z][a-z0-9_]*:[a-z0-9_]+:[^\s]{1,200}$`), `team_id: str \| None = None` (same pattern), `jira_project: str \| None = None` (pattern `^[A-Z][A-Z0-9_]{0,31}$`), `jira_component: str \| None = None` (1–255 characters, no control characters), `org_id: str \| None = None` (record-ID pattern), `role: Literal["owner", "support", "delivery"] = "owner"`, `aliases: list[str] = []` (≤ 20 items, each 1–200 characters, no control characters). |
| Preconditions | — |
| Postconditions | — |
| Invariants | At least one of `team_id`, `jira_project`, `org_id`, `aliases` is set. `jira_component` requires `jira_project`. `role == "delivery"` requires `jira_project`. |
| Algorithm | Field validators plus one `model_validator(mode="after")` for the invariants. |
| Side effects | — |
| Errors | pydantic `ValidationError`, converted to `ConfigError` by the spec 10 loader. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | Values reach SQL only as Arrow table data (U02-87), never as SQL text (TH02-10). |
| Tests | UT02-53 |

#### U02-72 `herness.model.settings.CustomFieldsConfig`

| Field | Content |
|---|---|
| Kind | class (pydantic, forbid, strict, frozen) with nested `ServiceNowCustomFields` and `JiraCustomFields` |
| Purpose | Raw lake column names of source custom fields (design 02 §8). |
| Signature | `servicenow: ServiceNowCustomFields` with `customer_impact_minutes: str \| None = None`, `acknowledged_at: str \| None = None`; `jira: JiraCustomFields` with `story_points: str \| None = None`, `team: str \| None = None`, `estimate_cost_usd: str \| None = None`, `epic_link: str \| None = None`. Every non-null value full-matches `^[a-z_][a-z0-9_]{0,127}$` (the flattened snake_case column name, for example `u_customer_impact_minutes`, `customfield_10016`). |
| Preconditions | — |
| Postconditions | — |
| Invariants | — |
| Algorithm | Field validators. |
| Side effects | — |
| Errors | `ValidationError`. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | Values are rendered into SQL only through the `ident`/`raw` filters (U02-86) after this pattern check (TH02-10). |
| Tests | UT02-53, ST02-10 |

#### U02-73 `herness.model.settings.MappingsConfig`

| Field | Content |
|---|---|
| Kind | class (pydantic, forbid, strict, frozen); constant `ENUM_DOMAINS: Final[Mapping[str, frozenset[str]]]` |
| Purpose | The `mappings.yaml` section (spec 10 §5.1 table: owner 02). |
| Signature | `enums: dict[str, dict[str, str]] = {}` (domain → source value → canonical value), `service_overrides: list[ServiceOverride] = []` (≤ 10,000), `custom_fields: CustomFieldsConfig = CustomFieldsConfig()`. `ENUM_DOMAINS` = `servicenow.incident_state` → {`open`, `in_progress`, `on_hold`, `resolved`, `closed`, `canceled`}; `servicenow.change_type` → {`standard`, `normal`, `emergency`}; `servicenow.change_close_code` → {`successful`, `successful_with_issues`, `unsuccessful`, `backed_out`, `canceled`}; `monitoring.severity` → {`critical`, `major`, `minor`, `warning`, `info`}; `jira.issue_type` → {`initiative`, `epic`, `feature`, `story`, `bug`, `task`, `subtask`}; `jira.status_category` (status name → category) → {`todo`, `in_progress`, `done`}; `jira.status_category_key` (Jira category key → category) → {`todo`, `in_progress`, `done`}. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Every domain key is in `ENUM_DOMAINS`; every canonical value is in the domain's set; source values are 1–200 characters; two source values that are equal after `str.lower()` map to the same canonical value; no two overrides share an alias (compared lower-cased) with different `service_id`s. |
| Algorithm | Validators for each invariant. |
| Side effects | — |
| Errors | `ValidationError` naming the domain or alias position (not the value). |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH02-10. |
| Tests | UT02-53 |

#### U02-74 `herness.model.settings.DqSettings`

| Field | Content |
|---|---|
| Kind | class (pydantic, forbid, strict, frozen) |
| Purpose | `sources.yaml: dq` thresholds (design 02 §4.8). |
| Signature | All fields `float` in [0, 1] except `future_timestamp_max` and `duplicate_key_max` (`int` ≥ 0): `row_count_drop_max = 0.05`, `incident_service_null_warn = 0.30`, `incident_service_null_error = 0.60`, `work_item_service_null_warn = 0.40`, `cast_fail_warn = 0.005`, `future_timestamp_max = 0`, `resolved_before_opened_warn = 0.001`, `duplicate_key_max = 0`, `decision_coverage_min = 0.95`, `metric_daily_unmapped_warn = 0.05`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `incident_service_null_warn ≤ incident_service_null_error`. |
| Algorithm | Validators. |
| Side effects | — |
| Errors | `ValidationError`. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | Rendered only through the `num` filter. |
| Tests | UT02-54 |

#### U02-75 `herness.model.settings.BuildSettings`

| Field | Content |
|---|---|
| Kind | class (pydantic, forbid, strict, frozen) |
| Purpose | `sources.yaml: build` (design 02 §8). |
| Signature | `keep_last: int = 3` (1–20), `memory_limit: str = "75%"` (pattern `^([1-9][0-9]?%\|100%\|[0-9]+(\.[0-9]+)?\s?(GB\|MB\|GiB\|MiB))$`), `threads: int \| None = None` (1–256; `None` = `os.cpu_count()`), `service_ci_classes: list[str] = ["cmdb_ci_service", "cmdb_ci_service_business", "cmdb_ci_service_technical"]` (1–50 items, each `^[a-z][a-z0-9_]{0,79}$`; DD02-06). |
| Preconditions | — |
| Postconditions | — |
| Invariants | — |
| Algorithm | Validators. |
| Side effects | — |
| Errors | `ValidationError`. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | `service_ci_classes` reach SQL as Arrow data only. |
| Tests | UT02-54 |

Wiring (R-03, R-69, agreed with impl 01): `sources.yaml` has sibling top-level sections. `T01-02 (herness.connectors.settings)` owns the connector sections; `herness.model.settings` owns the `dq` and `build` sections (`DqSettings`, `BuildSettings`). Neither settings module imports the other. The root config of `T10-03 (herness.core.config)`, which may import every package's `settings.py`, composes the `sources.yaml` model from both (fields `dq: DqSettings = DqSettings()` and `build: BuildSettings = BuildSettings()` next to the connector sections, read as `cfg.sources.dq` and `cfg.sources.build`), and types the field `T10-03 (herness.core.config.HernessConfig.mappings)` as `MappingsConfig`.

#### U02-76 `herness.model.errors.BuildSqlError`

| Field | Content |
|---|---|
| Kind | class (subclass of `SchemaViolation`) |
| Purpose | A build SQL file failed (design 02 §7: `SchemaViolation` with file name and DuckDB error). |
| Signature | Constructor `(build_id: str, file: str, statement_index: int, db_error: str)`. |
| Preconditions | — |
| Postconditions | Message = `build <build_id>: <file> statement <n> failed: <db_error>`. `db_error` = DuckDB exception class name + `: ` + the first line of its message truncated to 300 characters, with every single-quoted literal replaced by `'?'` (regex `'(?:[^']\|'')*'`), so data values never reach logs. |
| Invariants | Immutable. |
| Algorithm | Build the sanitised message. |
| Side effects | — |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH02-14. |
| Tests | IT02-22, ST02-14 |

#### U02-77 `herness.model.errors.DqGateFailed`

| Field | Content |
|---|---|
| Kind | class (subclass of `SchemaViolation`; CLI exit code 5, build blocked, R-46) |
| Purpose | Promotion blocked by `error`-severity DQ failures. |
| Signature | Constructor `(build_id: str, failed_checks: tuple[str, ...])`. |
| Postconditions | Message = `build <build_id> blocked by DQ: <comma-joined check names, first 20>`. |
| Other fields | Immutable, no side effects. |
| Tests | IT02-29 |

#### U02-78 `herness.model.lakeinfo.EXPECTED_ENTITIES`

| Field | Content |
|---|---|
| Kind | constant |
| Purpose | Fixed raw entities the staging files read (design 02 §3.1). |
| Signature | `EXPECTED_ENTITIES: Final[tuple[tuple[str, str], ...]]` = `servicenow`/{`incident`, `change_request`, `problem`, `cmdb_ci`, `cmdb_ci_service`, `cmdb_rel_ci`, `sys_user_group`, `cmn_department`, `task_sla`}, `jira`/`issue`, `monitoring`/{`event`, `metric_daily`}. Config-defined entities of `files`, `mongodb`, `snowflake`, `dataverse` come from `T01-02 (herness.connectors.settings.SourcesConfig)` entity names at build time. |
| Other fields | Immutable. |
| Tests | UT02-55 |

#### U02-79 `herness.model.lakeinfo.EntityInventory`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) |
| Purpose | What the lake holds for one entity. |
| Signature | `source: str`, `entity: str`, `glob: str` (from `lake_glob`), `present: bool`, `files: int`, `bytes: int`, `columns: frozenset[str]` (union across files, metadata columns included), `from_synth: bool`. |
| Invariants | `present` ⇔ `files > 0`. |
| Other fields | Immutable. |
| Tests | UT02-55 |

#### U02-80 `herness.model.lakeinfo.LakeInventory`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) |
| Purpose | Inventory for the render context and `dataset_kind`. |
| Signature | `root: Path`, `entities: Mapping[tuple[str, str], EntityInventory]` (read-only), `from_synth: bool` (any entity `from_synth`, or the layout's `synth_marker`). Method `get(source: str, entity: str) -> EntityInventory` (absent key → an `EntityInventory` with `present=False`, `columns=frozenset()`). |
| Other fields | Immutable. |
| Tests | UT02-55 |

#### U02-81 `herness.model.lakeinfo.scan_lake`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `layout` | `DataLayout` | — | positional | |
| `extra_entities` | `Sequence[tuple[str, str]]` | `()` | keyword-only | config-defined entities; names valid |

Returns `LakeInventory`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Find committed files and the union of column names per entity so staging tolerates missing entities and missing columns (schema drift, design 02 §11). |
| Preconditions | — |
| Postconditions | One `EntityInventory` per expected or extra entity. |
| Invariants | — |
| Algorithm | (1) For each (source, entity): list `layout.raw/<source>/<entity>/` recursively with `Path.glob("**/" + LAKE_FILE_PATTERN)` (same exclusion as the staging glob). (2) More than 100,000 files → `SchemaViolation("too many lake files for <source>/<entity>")`. (3) For each file, `pyarrow.parquet.read_schema`; add names to the union; sum sizes. A file that fails to parse → `SchemaViolation("unreadable lake file <path relative to raw>")`. (4) `from_synth` = the file's resolved path has a consecutive (`data`, `synth`) pair, or `layout.synth_marker`. (5) Log `model.build.lake_scanned` (INFO: entities present, files, bytes). |
| Side effects | Reads Parquet footers. |
| Errors | `SchemaViolation`. |
| Concurrency | Read-only; safe against concurrent writers (temp files are excluded). |
| Complexity and limits | O(files) footer reads. |
| Security notes | — |
| Tests | UT02-55 |

#### U02-82 `herness.model.sqlfiles.SqlFile`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) |
| Purpose | One numbered build SQL file. |
| Signature | `number: int` (0–999), `name: str` (file name), `path: Path`, `stage: Literal["setup", "staging", "core", "attach", "facts", "dq"]` (000–099, 100–199, 200–299, 300–399, 400–499, 900–999). |
| Other fields | Immutable. |
| Tests | UT02-56 |

#### U02-83 `herness.model.sqlfiles.discover_sql_files`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `sql_dir` | `Path \| None` | `None` | keyword-only | `None` = package directory `herness/model/sql` |

Returns `list[SqlFile]` sorted by name (lexical = numeric).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | The ordered file list of design 02 §4.1. |
| Preconditions | — |
| Postconditions | Unique numbers; every number in a stage range. |
| Invariants | — |
| Algorithm | (1) List `*.sql` and ignore names starting with `_`. (2) Each name must full-match `^(\d{3})_([a-z0-9_]+)\.sql$`. (3) Map the number to its stage; 500–899 → error. (4) Duplicate numbers → error. (5) Sort. |
| Side effects | Directory listing. |
| Errors | `ConfigError("unexpected build SQL file <name>")`, `ConfigError("duplicate build SQL number <n>")`. |
| Concurrency | Pure given the directory. |
| Complexity and limits | O(files). |
| Security notes | — |
| Tests | UT02-56 |

#### U02-84 `herness.model.render_context.RenderContext` and `build_render_context`

| Param (`build_render_context`) | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `cfg` | `HernessConfig` | — | positional | |
| `inventory` | `LakeInventory` | — | positional | |
| `build_id` | `str` | — | positional | valid ID |

Returns `RenderContext`.

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) + function |
| Purpose | The only values SQL templates may see (design 02 §4.1: Jinja for config values only). |
| Signature | `RenderContext` fields: `build_id: str`, `dq: DqSettings`, `custom_fields: CustomFieldsConfig`, `lake: LakeInventory`, `extra_entities: Mapping[str, tuple[str, ...]]` (source → entity names for `files`, `mongodb`, `snowflake`, `dataverse`). Method `template_vars() -> dict[str, object]` returns exactly these keys plus `raw_root` (POSIX string). |
| Preconditions | — |
| Postconditions | No free-text config value is present (enum maps, overrides and class lists go through U02-87 instead). |
| Invariants | — |
| Algorithm | Copy the fields from `cfg.sources.dq`, `cfg.mappings.custom_fields`, the inventory and the configured entity names. |
| Side effects | None. |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH02-10. |
| Tests | UT02-57 |

#### U02-85 `herness.model.sqlfiles.render_sql`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `file` | `SqlFile` | — | positional | |
| `context` | `RenderContext` | — | positional | |

Returns `str` (rendered SQL).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Render one file in a sandboxed Jinja environment. |
| Preconditions | — |
| Postconditions | Output contains no Jinja syntax. |
| Invariants | — |
| Algorithm | (1) Environment: `jinja2.sandbox.SandboxedEnvironment(loader=FileSystemLoader(sql dir), undefined=StrictUndefined, autoescape=False, trim_blocks=True, lstrip_blocks=True, keep_trailing_newline=True)`, cached per process. (2) Register filters `ident`, `sqlstr`, `num`, `sqldate` and global `raw` (U02-86) bound to `context.lake`. (3) Render with `context.template_vars()`. (4) Map `UndefinedError`, `TemplateSyntaxError`, `SecurityError`, and filter errors to `ConfigError("render failed for <file>: <exception class> at line <n>")`. |
| Side effects | Reads the template file and `_macros.jinja`. |
| Errors | `ConfigError`. |
| Concurrency | Environment is thread-safe after creation. |
| Complexity and limits | O(template size). |
| Security notes | TH02-10, TH02-11. |
| Tests | UT02-57, ST02-10, ST02-11 |

#### U02-86 `herness.model.sqlfiles` filters `ident`, `sqlstr`, `num`, `sqldate`, `raw` (private, logic)

| Filter | Input | Output | Rule | Error |
|---|---|---|---|---|
| `ident` | `str` | `"name"` | full-match `^[a-z_][a-z0-9_]{0,127}$` | `ConfigError("invalid identifier")` |
| `sqlstr` | `str` | `'text'` with `'` doubled | length ≤ 1,024; no character below U+0020 | `ConfigError("invalid SQL string literal")` |
| `num` | `int`, `float`, `Decimal` (not `bool`) | `str(int)`, `repr(float)`, `str(Decimal)` | finite | `ConfigError("invalid number")` |
| `sqldate` | `date` | `DATE 'YYYY-MM-DD'` | — | `ConfigError` for other types |
| `raw(source, entity, column, sqltype)` | names, column, type in {`VARCHAR`, `BOOLEAN`, `DOUBLE`, `BIGINT`, `TIMESTAMPTZ`} | `"column"` when `column ∈ inventory.get(source, entity).columns`, else `CAST(NULL AS <sqltype>)` | `column` passes `ident`; `sqltype` allowlisted | `ConfigError` |

| Field | Content |
|---|---|
| Kind | functions |
| Purpose | The only way a template turns a value into SQL text (ENG §3.5: identifiers allowlisted and quoted; no value formatting). |
| Concurrency | Pure. |
| Security notes | TH02-10. |
| Tests | UT02-58, PT02-02, ST02-10 |

#### U02-87 `herness.model.refdata.register_reference_tables`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | — | positional | the build connection |
| `mappings` | `MappingsConfig` | — | keyword-only | |
| `build_cfg` | `BuildSettings` | — | keyword-only | |
| `deleted_ids` | `Collection[str]` | — | keyword-only | record IDs with a `deletion_request` in `running` or `done` |
| `approved` | `Sequence[ReviewItem]` | — | keyword-only | from `approved_mapping_suggestions()` |

Returns `dict[str, int]` (row count per table created).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Put config and ops reference data into DuckDB as tables, so SQL never embeds these values as text and the build needs no DuckDB `sqlite` extension (DD02-02). |
| Preconditions | Schema `stg` exists (created by `000_settings.sql`). |
| Postconditions | Tables exist with these exact columns: `stg.enum_map(domain VARCHAR, source_value_lc VARCHAR, canonical VARCHAR)`; `stg.service_override(service_id, team_id, jira_project, jira_component, org_id, role VARCHAR)`; `stg.service_alias(alias_lc VARCHAR, service_id VARCHAR)`; `stg.service_ci_class(ci_class VARCHAR)`; `stg.deleted_record(record_id VARCHAR)`; `stg.approved_mapping(item_id, subject_type, jira_project, jira_component, team_id, service_id VARCHAR, score DOUBLE)`. |
| Invariants | — |
| Algorithm | (1) Build one `pyarrow.Table` per target with the declared schema: `enum_map` from `mappings.enums` (source value lower-cased with `str.lower()`); overrides as given; aliases lower-cased; classes from `build_cfg.service_ci_classes`; deleted IDs de-duplicated; approved payload fields `subject_type`, `jira_project`, `jira_component`, `team_id`, `service_id`, `score` (items with a missing `service_id` or `subject_type` outside {`jira_component`, `team`} are skipped and counted; a count > 0 logs `model.build.mapping_skipped` WARNING with the count). (2) For each: `con.register("_ref_<name>", table)`, `CREATE OR REPLACE TABLE stg.<name> AS SELECT * FROM _ref_<name>`, `con.unregister("_ref_<name>")`. (3) Log `model.build.refdata_registered` (INFO, counts). |
| Side effects | Creates six `stg` tables. |
| Errors | `duckdb.Error` → `SchemaViolation("refdata registration failed: <table>")`. |
| Concurrency | Build connection only. |
| Complexity and limits | O(rows); deletion set expected < 100,000. |
| Security notes | TH02-10, TH02-16. |
| Tests | UT02-59 |

#### U02-128 `herness.model.refdata.register_prev_row_counts`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | — | positional | |
| `counts` | `Mapping[str, int] \| None` | — | positional | `meta.build.row_counts` of the current promoted build; `None` when there is none |

Returns `None`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Input to the row-count-drop check (design 02 §4.8). |
| Preconditions | — |
| Postconditions | `stg.prev_row_counts(table_name VARCHAR, row_count BIGINT)` exists; empty when `counts` is `None`. |
| Invariants | — |
| Algorithm | Arrow table from the mapping (keys that are not `schema.table` identifiers are skipped); register, CTAS, unregister as U02-87. |
| Side effects | One table. |
| Errors | As U02-87. |
| Concurrency | Build connection. |
| Complexity and limits | O(tables). |
| Security notes | — |
| Tests | IT02-29 |

#### U02-88 `herness.model.meta.dataset_kind`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `profile` | `str` | — | positional | active profile name |
| `inventory` | `LakeInventory` | — | positional | |

Returns `Literal["synthetic", "real"]`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Design 02 §4.7 rule for `meta.build.dataset_kind`. |
| Algorithm | `synthetic` when `profile == "synth"` or `inventory.from_synth`; else `real`. |
| Side effects | None. |
| Errors | — |
| Concurrency | Pure. |
| Tests | UT02-69 |

#### U02-89 `herness.model.meta.git_sha`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `env` | `Mapping[str, str]` | — | keyword-only | process environment |
| `repo_dir` | `Path` | — | keyword-only | directory containing `pyproject.toml` |

Returns `str` (12 lower-case hex characters, or `unknown`).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | `meta.build.git_sha`. |
| Algorithm | (1) If `env["HERNESS_GIT_SHA"]` full-matches `^[0-9a-f]{7,40}$`, return its first 12 characters. (2) Else run `subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, timeout=5, check=False)`; if the exit code is 0 and stdout matches `^[0-9a-f]{40}\s*$`, return the first 12 characters. (3) Else `unknown` and log `model.build.git_sha_unknown` (WARNING). |
| Side effects | One subprocess with an argument list and a 5 s timeout (ENG §3.5). |
| Errors | `subprocess.TimeoutExpired` and `OSError` map to `unknown`. |
| Concurrency | Thread-safe. |
| Tests | UT02-67 |

#### U02-90 `herness.model.meta.insert_build_row`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | — | positional | |
| `build_id` | `str` | — | keyword-only | |
| `started_at` | `datetime` | — | keyword-only | UTC |
| `git_sha` | `str` | — | keyword-only | |
| `config_hash` | `str` | — | keyword-only | `^cfg_[0-9a-f]{16}$` |
| `dataset_kind` | `Literal["synthetic", "real"]` | — | keyword-only | |
| `source_watermarks` | `Mapping[str, str]` | — | keyword-only | `"<source>/<entity>"` → ts-text |

Returns `None`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Write the single `meta.build` row with `status = 'building'`. |
| Preconditions | `meta.build` is empty, else `SchemaViolation("meta.build already has a row")`. |
| Postconditions | One row: `finished_at` NULL, `row_counts` `{}`. |
| Algorithm | Parameterised `INSERT` (`?` placeholders); JSON values serialised with `json.dumps(sort_keys=True)`. |
| Side effects | One row. |
| Errors | `SchemaViolation`. |
| Concurrency | Build connection. |
| Tests | IT02-21 |

#### U02-91 `herness.model.meta.update_build_row`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | — | positional | |
| `status` | `Literal["building", "failed", "promoted", "retired"] \| None` | `None` | keyword-only | `None` = unchanged |
| `finished_at` | `datetime \| None` | `None` | keyword-only | set when given |
| `clear_finished` | `bool` | `False` | keyword-only | sets `finished_at` to NULL (a resumed stage run); exclusive with `finished_at` |
| `row_counts` | `Mapping[str, int] \| None` | `None` | keyword-only | replaces the JSON when given |

Returns `None`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Update the `meta.build` row. |
| Preconditions | Exactly one row exists; at least one change requested. |
| Postconditions | Requested columns updated. |
| Algorithm | One parameterised `UPDATE meta.build SET …` whose SET list is chosen from fixed column names (no dynamic identifiers). |
| Side effects | One row. |
| Errors | `SchemaViolation` (row missing, `duckdb.Error`); `ConfigError` for conflicting arguments. |
| Concurrency | Build connection. |
| Tests | IT02-21, IT02-29 |

#### U02-92 `herness.model.meta.collect_row_counts`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | — | positional | |
| `schemas` | `Sequence[Literal["core", "enrich", "metrics", "score"]]` | — | positional | |

Returns `dict[str, int]` (`"schema.table"` → rows, sorted by key).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | `meta.build.row_counts` and the drop check. |
| Algorithm | (1) `SELECT schema_name, table_name FROM duckdb_tables() WHERE schema_name IN (…)` with parameters. (2) For each, check both names against `^[a-z_][a-z0-9_]{0,127}$`, then `SELECT count(*) FROM "<schema>"."<table>"`. Views are not counted. |
| Side effects | None. |
| Errors | `SchemaViolation` on `duckdb.Error`. |
| Concurrency | Build connection. |
| Complexity and limits | One count per table (DuckDB answers from metadata for base tables). |
| Tests | IT02-21 |

#### U02-93 `herness.model.dq.DqOutcome`

| Field | Content |
|---|---|
| Kind | class (frozen `dataclass`) |
| Purpose | Result of the DQ gate. |
| Signature | `passed: bool`, `failed_errors: tuple[str, ...]`, `failed_warnings: tuple[str, ...]`, `checks: int`. |
| Invariants | `passed` ⇔ `failed_errors == ()` and `checks > 0`. |
| Tests | IT02-28 |

#### U02-94 `herness.model.dq.evaluate_gate`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | — | positional | build connection |

Returns `DqOutcome`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Decide promotion from `meta.dq_result` (design 02 §4.8: `error` blocks promotion; `warn` shows in reports). The gate includes spec 04 invariant rows. |
| Preconditions | — |
| Postconditions | — |
| Algorithm | (1) `SELECT check_name, severity, passed FROM meta.dq_result ORDER BY check_name`. (2) Zero rows → `SchemaViolation("no DQ results for build")`. (3) Failed errors = `severity = 'error' AND NOT passed`; failed warnings likewise for `warn`. (4) Log `model.dq.evaluated` (INFO: `checks`, `failed_errors`, `failed_warnings` counts) and one `model.dq.check_failed` (WARNING or ERROR by severity, `check_name`) per failure. |
| Side effects | Logs. |
| Errors | `SchemaViolation`. |
| Concurrency | Build connection. |
| Security notes | TH02-17. |
| Tests | IT02-28…IT02-30, ST02-17 |

#### U02-95 `herness.model.build.STAGE_ORDER`

| Field | Content |
|---|---|
| Kind | constant |
| Purpose | Stage names of the `build_pipeline` job payload (spec 08 §7 `stages: [build, enrich, score, dq, promote]`; spec 09 `--from-stage`). |
| Signature | `STAGE_ORDER: Final = ("build", "enrich", "score", "dq", "promote")`; `Stage = Literal["build", "enrich", "score", "dq", "promote"]`. |
| Tests | UT02-65 |

#### U02-96 `herness.model.build.BuildPipelinePayload`

| Field | Content |
|---|---|
| Kind | class (pydantic, `extra="forbid"`, `strict=False` for JSON-decoded job payloads, `frozen=True`) |
| Purpose | Validated `job.payload` of kind `build_pipeline`. |
| Signature | `stages: list[Stage]` (1–5 items), `build_id: str \| None = None`, `depth: Literal["fast", "standard", "deep"] = "standard"`, `score_steps: list[str] \| None = None` (spec 04 step names, 1–10 items, each `^[a-z_]{1,32}$`), `enrich_stage: str \| None = None` (one impl 03 `StageName`, pattern `^[a-z][a-z_-]{0,31}$`; `herness enrich --stage`, R-48), `rekey_night: bool = False` (spec 08 §5.11; recorded in the job result only), `schedule: str \| None = None`, `fire_at: str \| None = None` (added by the spec 08 scheduler). |
| Invariants | (a) `stages` are strictly consecutive in `STAGE_ORDER` (for example `[enrich, score]`, never `[build, score]`). (b) `"build" in stages` ⇔ `build_id is None`. (c) `build_id`, when set, matches `BUILD_ID_RE`. (d) `score_steps` only with `"score"`. (e) `enrich_stage` only with `"enrich"`; it is passed as `stages=[enrich_stage]` to `run_enrichment`, which rejects an unknown stage name with `ConfigError` (R-48, U02-100). |
| Algorithm | `model_validator(mode="after")` for (a)–(e). |
| Errors | `ValidationError` → `ConfigError` in U02-98. |
| Tests | UT02-65 |

#### U02-97 `herness.model.build.run_sql_range`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | — | positional | build connection |
| `files` | `Sequence[SqlFile]` | — | positional | from `discover_sql_files` |
| `lo` | `int` | — | positional | inclusive |
| `hi` | `int` | — | positional | inclusive |
| `context` | `RenderContext` | — | keyword-only | |
| `should_yield` | `Callable[[], bool]` | — | keyword-only | `JobContext.should_yield` |
| `heartbeat` | `Callable[[str], None]` | — | keyword-only | `JobContext.heartbeat` |

Returns `SqlRangeResult` (frozen `dataclass`: `status: Literal["done", "yield"]`, `files_run: tuple[str, ...]`, `durations_ms: Mapping[str, int]`).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Execute the files whose number is in `[lo, hi]` in lexical order (design 02 §4.1). |
| Preconditions | `0 ≤ lo ≤ hi ≤ 999`; the range excludes 400–499 (facts run through the spec 04 hook), else `ConfigError`. |
| Postconditions | `done`: every file in range ran successfully. `yield`: files before the yield point ran. |
| Invariants | — |
| Algorithm | For each file in range: (1) If `should_yield()`, return `yield`. (2) `render_sql`. (3) `statements = con.extract_statements(sql)`. (4) Execute each statement's `query` text with `con.execute`; a `duckdb.Error` → `BuildSqlError(build_id, file.name, index, sanitised error)`. (5) Record duration; log `model.build.sql_file_done` (INFO: `build_id`, `file`, `statements`, `duration_ms`); `heartbeat(f"sql {file.name}")`. (6) `T08-08 (herness.core.resilience.fault_point)("build.mid_sql")` (impl 08 registry point, R-40). |
| Side effects | Warehouse DDL/DML. |
| Errors | `BuildSqlError`, `ConfigError`. |
| Concurrency | Build connection, single thread (DuckDB parallelises inside statements). |
| Complexity and limits | Stage targets §10. |
| Security notes | Only repo SQL runs; rendered values pass U02-86. |
| Tests | IT02-21, IT02-22, IT02-27, FT02-03 |

#### U02-98 `herness.model.build.run_build_pipeline`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `ctx` | `JobContext` | — | positional | `T08-03 (herness.core.jobs.JobContext)` (R-02); kind `build_pipeline` |
| `llm_factory` | `LlmFactory \| None` | `None` | keyword-only | impl 03 `LlmFactory`, passed to `run_enrichment` (R-05); bound by U02-134 |

Returns `JobOutcome` (`T08-01 (herness.core.types.JobOutcome)`).

| Field | Content |
|---|---|
| Kind | function (job body; the registered one-argument handler is built by U02-134, R-42) |
| Purpose | Run the requested stages on a new or existing unpromoted build (design 02 §4.1, §7). |
| Preconditions | Runs in the leased, exclusive `build_pipeline` job (spec 08 §5.1). The job starts with no GPU class; only impl 03's enrichment stages take the `decider` class, through their own `ctx.gpu_scope("decider")` (R-43); U02-100 enters no GPU scope. |
| Postconditions | `done`: every requested stage finished; when `promote` ran, `CURRENT` names this build. On error: `meta.build.status = 'failed'` when the build file is writable, `CURRENT` untouched, the error propagates to the job layer. |
| Invariants | `CURRENT` changes only inside `promote_build`. |
| Algorithm | Flow F02-02 (§5) step by step: (1) Validate the payload read from `ctx.job.payload` (R-42) with `BuildPipelinePayload`; `ValidationError` → `ConfigError`. (2) Load config, layout, `now`. (3) Resolve the build: payload `build_id` → must exist with `list_builds` status `building` (else `NotFoundError` or `ConfigError("build <id> is <status>")`); else if `ctx.load_state()` holds `build_id` with `"build"` in `stages_done` and that build is `building`, resume it with the payload stages not yet done; else create a new ID with `new_build_id(now)`. (4) `cleanup_builds(mode="pre", …)` protecting this build and `CURRENT`. (5) For each remaining stage: if `ctx.should_yield()`, save state and return `JobOutcome(status="yield")`; call the stage unit (U02-99…U02-103); append to `stages_done`; `ctx.save_state({"build_id": …, "stages_done": […]})`; `ctx.heartbeat(f"stage {name} done")`; log `model.build.stage_done` (INFO: `build_id`, `stage`, `duration_ms`). (6) If `promote` was not requested, set `finished_at = now` (status stays `building`: a completed, unpromoted build). (7) Write metrics (§8.2) through `T08-05 (herness.store.ops.metrics.record_metric_samples)` (R-12). (8) Return `JobOutcome(status="done", result={"build_id", "stages", "promoted", "dq": {checks, failed_errors, failed_warnings}, "durations_ms", "row_counts", "enrich": <EnrichReport JSON or null>, "scoring": <ScoringReport JSON or null>, "rekey_night"})`. On a `HernessError` in step 5: open the build writable if closed, `update_build_row(status="failed", finished_at=now)` (errors here are logged `model.build.mark_failed_error` and suppressed so the original error propagates), log `model.build.failed` (ERROR: `build_id`, `stage`, error class), write metrics, re-raise. |
| Side effects | Warehouse file, `CURRENT` (via promote), ops `job` state (via `ctx`), `metric_sample` rows, logs. |
| Errors | `ConfigError`, `NotFoundError`, `BuildSqlError`, `DqGateFailed`, `StoreBusy`, and errors from spec 03/04 hooks. |
| Concurrency | One build job at a time (`exclusive_kinds`). |
| Complexity and limits | Stage targets in §10. |
| Security notes | TH02-05, TH02-17. |
| Tests | IT02-21, IT02-25…IT02-27, IT02-29, FT02-03, FT02-04 |

#### U02-134 `herness.model.build.make_build_pipeline_handler`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `llm_factory` | `LlmFactory \| None` | — | keyword-only | built by the composition root from spec 05 clients; `None` runs enrichment without the reasoning phase (impl 03 degraded mode `reasoning_unavailable`) |

Returns `Callable[[JobContext], JobOutcome]`.

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Build the one-argument `build_pipeline` job handler (R-42) that carries the model client factory, so `herness.model` and `herness.enrich` never import `herness.harness` (R-05). Same pattern as impl 03 `make_distill_handler`. |
| Preconditions | — |
| Postconditions | The returned handler calls `run_build_pipeline(ctx, llm_factory=llm_factory)` and returns its result. The composition root (`herness.cli`) registers it with `T08-12 (herness.core.jobs.register_handler)("build_pipeline", handler)`. |
| Invariants | The handler holds no other state. |
| Algorithm | Return a closure over `llm_factory` (or `functools.partial(run_build_pipeline, llm_factory=llm_factory)`, which has the same one-argument call shape). |
| Side effects | None. |
| Errors | None raised by the factory; the handler propagates U02-98 errors. |
| Concurrency | Immutable. |
| Complexity and limits | O(1). |
| Security notes | — |
| Tests | UT02-78 |

#### U02-99 `herness.model.build._stage_build` (private)

| Field | Content |
|---|---|
| Kind | function `(run: _BuildRun) -> None`; `_BuildRun` is a private mutable holder of `ctx`, `payload`, `cfg`, `layout`, `build_id`, `con`, `now`, `files`, `context`, `result`, `llm_factory` |
| Purpose | Stage `build`: create the file and run 000–299. |
| Preconditions | The build file does not exist. |
| Postconditions | `core.*` tables and `meta.build` exist; `row_counts` holds `core` counts. |
| Algorithm | (1) `open_for_build(build_id, create=True)`. (2) `inventory = scan_lake(layout, extra_entities=<configured entities>)`; `context = build_render_context(cfg, inventory, build_id)`. (3) `run_sql_range(0, 99)`. (4) `insert_build_row(started_at=now, git_sha=git_sha(...), config_hash=T10-03 (herness.core.config.config_hash)(cfg), dataset_kind=dataset_kind(cfg.profile, inventory), source_watermarks={f"{r.source}/{r.entity}": r.value for r in T01-04 (herness.store.ops.ingest.list_watermarks)()})`. (5) Deleted IDs = union of `T10-32 (herness.store.ops.privacy.deleted_record_ids)(source, entity)` over present entities; `register_reference_tables(...)` with `approved_mapping_suggestions()`. (6) `run_sql_range(100, 299)`. (7) `update_build_row(row_counts=collect_row_counts(con, ["core"]))`. (8) `CHECKPOINT`. A `yield` from `run_sql_range` returns `yield` up to U02-98, which saves state without `"build"` in `stages_done` (the partial file becomes an orphan). |
| Side effects | Creates the warehouse file. |
| Errors | As U02-98. |
| Tests | IT02-21, IT02-22 |

#### U02-100 `herness.model.build._stage_enrich` (private)

| Field | Content |
|---|---|
| Kind | function `(run: _BuildRun) -> None` |
| Purpose | Stage `enrich`: spec 03 enrichment, then 300–399. |
| Preconditions | `build` stage done for this build. |
| Postconditions | `enrich.*` filled by spec 03; `core.incident.content_hash` set; enrich rows of deleted records removed. |
| Algorithm | (1) Ensure the connection (`open_for_build(create=False)` if closed; `update_build_row(clear_finished=True)` when resuming a completed unpromoted build). (2) `prev = build_path(current)` when `read_current()` returns an ID ≠ this build, else `None`. (3) Import `herness.enrich.pipeline.run_enrichment` lazily. (4) Without entering any GPU scope (R-43: the build job starts with no GPU class and impl 03's enrichment stages take `decider` themselves through `ctx.gpu_scope("decider")`, so no class is held during this unit's CPU-only SQL), call `report = T03-28 (herness.enrich.pipeline.run_enrichment)(con, build_id, depth=payload.depth, ctx=ctx, prev_warehouse=prev, stages=[payload.enrich_stage] if payload.enrich_stage else None, llm_factory=run.llm_factory)` (R-48, R-05). (5) Yield: `T03-04 (herness.enrich.pipeline.YieldRequested)` (impl 03's public yield signal, U03-152, raised when `ctx.should_yield()` is true inside enrichment after its checkpoint is flushed) is caught here, through the same lazy import, and turned into a `yield` result; any other exception propagates. On yield, `enrich` is not added to `stages_done`, so the next run resumes at `enrich` and impl 03 resumes from its own checkpoint key. (6) Store `report.model_dump(mode="json")` in the result. (7) `run_sql_range(300, 399)`. (8) `CHECKPOINT`. |
| Errors | Errors from spec 03 propagate; `ConfigError` from `run_enrichment` for an unknown `enrich_stage`. |
| Tests | IT02-23, IT02-25 |

#### U02-101 `herness.model.build._stage_score` (private)

| Field | Content |
|---|---|
| Kind | function `(run: _BuildRun) -> None` |
| Purpose | Stage `score`: facts (400–499 via the spec 04 hook), then scoring. |
| Preconditions | `build` done. |
| Postconditions | `metrics.*`, `score.*`, `meta.evidence` written by spec 04. |
| Algorithm | (1) Ensure the connection. (2) `query_ids = T04-06 (herness.metrics.facts.materialize_facts)(con, build_id)`; the runner never renders 400–499 files itself. (3) `CHECKPOINT`. (4) `report = T04-13 (herness.metrics.scoring.run_scoring)(build_id, steps=payload.score_steps, con=con, ctx=ctx)`: the build connection is passed in (impl 04 DD04-02), so the file is never opened twice and no close and reopen happens (OI-07 closed). `run_scoring` does not close `con`. (5) Store `len(query_ids)` and `report` JSON in the result. (6) `CHECKPOINT`. |
| Errors | Spec 04 errors propagate (`SchemaViolation` blocks the build). |
| Tests | IT02-26 |

#### U02-102 `herness.model.build._stage_dq` (private)

| Field | Content |
|---|---|
| Kind | function `(run: _BuildRun) -> DqOutcome` |
| Purpose | Stage `dq`: run 900–999 and evaluate the gate. |
| Postconditions | `meta.dq_result` holds this run's `dq900` rows plus spec 04 rows; `row_counts` covers `core`, `enrich`, `metrics`, `score`. |
| Algorithm | (1) Ensure the connection. (2) Previous counts: when `CURRENT` names another build, open it with `open_readonly`, read `meta.build.row_counts`, close; `register_prev_row_counts(con, counts or None)`. (3) `run_sql_range(900, 999)`. (4) `update_build_row(row_counts=collect_row_counts(con, ["core", "enrich", "metrics", "score"]))`. (5) `outcome = evaluate_gate(con)`. (6) Not passed → `update_build_row(status="failed", finished_at=now)`, raise `DqGateFailed`. (7) Return the outcome. |
| Errors | `DqGateFailed`, `BuildSqlError`. |
| Tests | IT02-28…IT02-30 |

#### U02-103 `herness.model.build._stage_promote` (private)

| Field | Content |
|---|---|
| Kind | function `(run: _BuildRun) -> None` |
| Purpose | Stage `promote`. |
| Preconditions | — |
| Postconditions | `CURRENT` names this build. |
| Algorithm | (1) If `dq` did not run in this job, run `_stage_dq` first (a promotion is never based on stale DQ results; covers `--from-stage promote`). (2) `promote_build(con, build_id, …)` (U02-104). |
| Errors | As U02-102 and U02-104. |
| Security notes | TH02-17. |
| Tests | IT02-32, ST02-17, FT02-04 |

#### U02-104 `herness.model.promote.promote_build`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `con` | `duckdb.DuckDBPyConnection` | — | positional | build connection (closed by this function) |
| `build_id` | `str` | — | positional | |
| `build_cfg` | `BuildSettings` | — | keyword-only | |
| `layout` | `DataLayout` | — | keyword-only | |
| `now` | `datetime` | — | keyword-only | UTC |

Returns `str | None` (previous `CURRENT`).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Blue/green switch (spec 00 §4). |
| Preconditions | DQ gate passed in this job. |
| Postconditions | `meta.build.status = 'promoted'`, `finished_at = now`; `CURRENT` = `build_id`; retention applied. |
| Algorithm | (1) `fault_point("pipeline.before_promote")` (impl 08 registry point, R-40). (2) `update_build_row(status="promoted", finished_at=now)`; `CHECKPOINT`; close `con`. (3) `previous = write_current(build_id)`. (4) If `previous` and `previous ≠ build_id`: try `open_for_build(previous, create=False)`, `update_build_row(status="retired")`, close; `StoreBusy` (a reader holds it) → log `model.build.retire_deferred` (INFO, `previous`); the retry happens in the next `cleanup_builds`. (5) `cleanup_builds(mode="post", keep_last=build_cfg.keep_last, protect=frozenset({build_id}), layout, now)`. (6) Log `model.build.promoted` (INFO: `build_id`, `previous`). |
| Side effects | `CURRENT`; old files. |
| Errors | `StoreBusy` from `write_current` (the job retries and resumes at `promote`); `SchemaViolation`. |
| Concurrency | Exclusive build job. |
| Security notes | TH02-03, TH02-17. |
| Tests | IT02-32, FT02-04 |

#### U02-105 `herness.model.promote.cleanup_builds`

| Param | Type | Default | Kind | Constraints |
|---|---|---|---|---|
| `mode` | `Literal["pre", "post"]` | — | keyword-only | |
| `keep_last` | `int` | — | keyword-only | 1–20 (includes `CURRENT`) |
| `protect` | `frozenset[str]` | — | keyword-only | IDs never deleted |
| `layout` | `DataLayout` | — | keyword-only | |
| `now` | `datetime` | — | keyword-only | UTC |

Returns `CleanupReport` (frozen `dataclass`: `deleted: tuple[str, ...]`, `deferred: tuple[str, ...]`, `retired: tuple[str, ...]`).

| Field | Content |
|---|---|
| Kind | function |
| Purpose | Orphan removal before a build and retention after promotion (design 02 §7; spec 00 §4 "last 3 builds are kept"). Also used by spec 10 deletion step 5 (`T10-29 (herness.admin.privacy.run_privacy_delete)`, `mode="post"`, `keep_last=1`). |
| Preconditions | Exclusive job. |
| Postconditions | Rules below applied; `CURRENT`, `protect` and every build pinned by a non-terminal run are never deleted. |
| Algorithm | (1) `builds = list_builds()`; `pinned` = the non-empty `build_id` values of `T06-05 (herness.store.ops.runs.select_runs)(statuses=NON_TERMINAL_RUN_STATUSES)`, where the module constant `NON_TERMINAL_RUN_STATUSES = ("created", "planning", "running", "challenging", "verifying", "writing", "recording")` is the `run.status` CHECK list of §4.3 minus the terminal set `done`, `partial`, `failed`, `canceled` (spec 06 §5.1; reads of `run` belong to impl 06, R-68); `keep = protect ∪ {CURRENT} ∪ pinned`. (2) Both modes delete, unless in `keep`: status `building` with `finished_at` NULL (crashed or killed build: the orphan); status `unreadable` whose file mtime is older than 1 hour (`ORPHAN_UNREADABLE_AGE_S = 3600`); status `failed` except the newest failed build. `locked` files are never deleted. (3) `post` also deletes, unless in `keep`: `promoted` or `retired` builds that are not current beyond the newest `keep_last − 1` of them (by `build_id`); `building` builds with `finished_at` set whose `build_id` < the current build's. (4) `post`: for each remaining non-current `promoted` build, try the retire update of U02-104 step 4 and record successes in `retired`. (5) Each deletion goes through `delete_build_files`; `deferred` results are collected. (6) Log `model.build.cleanup` (INFO: counts). |
| Side effects | Deletes files; may update `meta.build.status` in old files. |
| Errors | `StoreBusy` from `select_runs` propagates; per-file errors are handled by `delete_build_files`. |
| Concurrency | Exclusive job. |
| Security notes | — |
| Tests | UT02-48, IT02-32, FT02-03, FT02-05 |

### 3.10 Build SQL files (`herness/model/sql/`)

Rules for every file:

| Rule | Detail |
|---|---|
| Idempotence | Every table statement is `CREATE OR REPLACE TABLE … AS SELECT …` or `CREATE TABLE IF NOT EXISTS`; re-running a file on the same build gives the same result. |
| Determinism | Every window or `LIMIT` has a total order (ties broken by `record_id`, then by `filename` for lake reads). No `random()`, `now()` or `current_timestamp`; "build start" is `(SELECT started_at FROM meta.build)`. |
| Keys | Logical primary keys are not declared as constraints on `core.*` (bulk-load speed); uniqueness is checked by `duplicate_key:*` DQ checks. `meta.evidence.query_id` is a declared `PRIMARY KEY`. |
| Text | Raw free-text columns (`short_description`, `description`, `close_notes`, `root_cause_text`, `summary`) are copied as-is into `core.*` and never into `stg` flag columns, logs or `meta.*`. `stg` never holds `_payload`. |
| IDs | References become record IDs of the referenced entity: ServiceNow service and CI IDs are always `servicenow:cmdb_ci:<sys_id>` (a `cmdb_ci_service` row is the same CI), teams `servicenow:sys_user_group:<sys_id>`, departments `servicenow:cmn_department:<sys_id>`, problems `servicenow:problem:<sys_id>`, changes `servicenow:change_request:<sys_id>`. Empty strings are NULL. |
| Casting | Every typed staging column uses macro `typed` (U02-106), which also stores the flags `_nn_<col>` (raw non-null) and `_cf_<col>` (raw non-null and typed NULL). Each staging file ends with `cast_stats` for its typed columns. Unmapped enum values count as cast failures of their column. |
| Raw reads | Only through macro `latest` (U02-106), which applies the design 02 §4.2 glob, dedupe, tombstone and deletion rules. |

#### U02-106 `herness/model/sql/_macros.jinja`

| Field | Content |
|---|---|
| Kind | Jinja macro file (imported, never executed) |
| Purpose | Shared SQL fragments so staging rules are written once. |
| Signature | Macros listed below; files use `{% import "_macros.jinja" as m %}`. |
| Algorithm | See table. |
| Security notes | Macros emit values only through `ident`, `sqlstr`, `num`, `raw` (U02-86). |
| Tests | IT02-02…IT02-08, UT02-60 |

| Macro | Arguments | Emits |
|---|---|---|
| `latest(source, entity, columns)` | names; `columns` = list of (`raw_column`, `sqltype`) | When `lake.get(source, entity).present`: a subquery selecting `_record_id`, `_source_key`, `_source_updated_at` and `raw(source, entity, c, t) AS "c"` for each column, from `read_parquet(<glob as sqlstr>, hive_partitioning = true, union_by_name = true, filename = true)` with `QUALIFY row_number() OVER (PARTITION BY _record_id ORDER BY _source_updated_at DESC, _fetched_at DESC, filename DESC) = 1`, wrapped by an outer `WHERE NOT _deleted AND _record_id NOT IN (SELECT record_id FROM stg.deleted_record)`. When absent: a zero-row `SELECT` of typed NULLs with the same column names and types. |
| `typed(raw_expr, expr, alias)` | SQL expressions (repo-authored), alias passing `ident` | `<expr> AS "<alias>", (<raw_expr> IS NOT NULL) AS "_nn_<alias>", (<raw_expr> IS NOT NULL AND <expr> IS NULL) AS "_cf_<alias>"` |
| `cast_stats(table, aliases)` | stg table name, list of aliases | `INSERT INTO stg.cast_stats` one row per alias: (`'stg.<table>'`, `'<alias>'`, `count_if("_nn_<alias>")`, `count_if("_cf_<alias>")`) |
| `enum(alias, domain, raw_expr)` | join alias, domain literal, raw expression | `LEFT JOIN stg.enum_map AS <alias> ON <alias>.domain = <sqlstr domain> AND <alias>.source_value_lc = lower(<raw_expr>)` (exact match after lower-casing; no trimming, so `Emergency ` stays unmapped as spec 11 §5.1.6 expects) |
| `rid(prefix, raw_expr)` | `prefix` like `servicenow:sys_user_group` | `CASE WHEN nullif(trim(<raw_expr>), '') IS NULL THEN NULL ELSE <sqlstr prefix> || ':' || trim(<raw_expr>) END` |

#### U02-107 `herness/model/sql/000_settings.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage setup) |
| Purpose | Schemas and fixed tables that later stages and other specs rely on (design 02 §4, §4.4, §4.7). |
| Inputs | None. |
| Outputs | Schemas `stg`, `core`, `enrich`, `metrics`, `score`, `meta`; tables below (all `CREATE TABLE IF NOT EXISTS`). |
| Algorithm | DDL only. Enrich tables are created empty so that `300_attach_decisions.sql`, facts and DQ work when enrichment is skipped; spec 03 may `CREATE OR REPLACE` them with the same columns. |
| Tests | IT02-21 |

| Table | Columns (type) |
|---|---|
| `meta.build` | `build_id VARCHAR`, `started_at TIMESTAMPTZ`, `finished_at TIMESTAMPTZ`, `git_sha VARCHAR`, `config_hash VARCHAR`, `dataset_kind VARCHAR`, `source_watermarks JSON`, `row_counts JSON`, `status VARCHAR` |
| `meta.evidence` | `query_id VARCHAR PRIMARY KEY`, `sql VARCHAR`, `params JSON`, `result_hash VARCHAR`, `row_count BIGINT`, `result_sample JSON`, `executed_at TIMESTAMPTZ`, `producer VARCHAR` |
| `meta.dq_result` | `check_name VARCHAR`, `severity VARCHAR`, `value DOUBLE`, `threshold DOUBLE`, `passed BOOLEAN`, `details JSON` |
| `enrich.text_redacted` | `record_id VARCHAR`, `entity VARCHAR`, `text VARCHAR`, `content_hash VARCHAR` |
| `enrich.decision` | `record_id VARCHAR`, `question VARCHAR`, `answer VARCHAR`, `probability DOUBLE`, `agreement DOUBLE`, `decider VARCHAR`, `decider_version VARCHAR`, `question_set_version VARCHAR`, `content_hash VARCHAR`, `decided_at TIMESTAMPTZ`, `escalated BOOLEAN`, `review_status VARCHAR` |
| `enrich.cluster` | `cluster_id VARCHAR`, `label VARCHAR`, `root_cause_category VARCHAR`, `size BIGINT`, `first_seen TIMESTAMPTZ`, `last_seen TIMESTAMPTZ`, `top_terms VARCHAR[]`, `service_ids VARCHAR[]`, `algorithm_version VARCHAR` |
| `enrich.cluster_member` | `record_id VARCHAR`, `cluster_id VARCHAR`, `membership_prob DOUBLE` |
| `enrich.incident_change_link` | `incident_id VARCHAR`, `change_id VARCHAR`, `method VARCHAR`, `score DOUBLE` |
| `stg.cast_stats` | `table_name VARCHAR`, `column_name VARCHAR`, `non_null BIGINT`, `failed BIGINT` |
| `stg.build_counts` | `name VARCHAR`, `value BIGINT` |

#### U02-108 `herness/model/sql/010_macros.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage setup) |
| Purpose | DuckDB scalar macros used by staging (design 02 §4.2 "map source enums with macros"; typing rules). |
| Inputs | None. |
| Outputs | `CREATE OR REPLACE MACRO` definitions below (temporary to the connection is not used: macros are stored in schema `main` of the build file so a resumed stage sees them). |
| Tests | UT02-60…UT02-64 |

| Macro | Result type | Rule |
|---|---|---|
| `ts_utc(x)` | `TIMESTAMPTZ` | NULL if `x` is NULL. Otherwise the first successful parse of `x` among exactly these formats: naive (read as UTC) `%Y-%m-%d %H:%M:%S`, `%Y-%m-%d %H:%M:%S.%f`, `%Y-%m-%dT%H:%M:%S`, `%Y-%m-%dT%H:%M:%S.%f`; zoned `%Y-%m-%dT%H:%M:%S%z`, `%Y-%m-%dT%H:%M:%S.%f%z` (Jira `+0000`), `%Y-%m-%dT%H:%M:%SZ`, `%Y-%m-%dT%H:%M:%S.%fZ`. Anything else (for example `31/02/2024`, epoch-millisecond strings, empty string) is NULL. Uses `try_strptime`. |
| `to_date(x)` | `DATE` | `try_strptime(x, '%Y-%m-%d')` as `DATE`; else NULL. |
| `to_bool(x)` | `BOOLEAN` | `lower(x)` in (`true`, `1`, `yes`, `y`, `t`) → true; in (`false`, `0`, `no`, `n`, `f`) → false; else NULL. |
| `lead_int(x, lo, hi)` | `INTEGER` | `TRY_CAST(regexp_extract(x, '^([0-9]+)(\s*-.*)?$', 1) AS INTEGER)`, NULL when outside [`lo`, `hi`]. `1 - Critical` → 1; `2` → 2; `P2-ish` → NULL. |
| `to_double(x)` | `DOUBLE` | `TRY_CAST(x AS DOUBLE)`; non-finite → NULL. |
| `to_int(x)` | `INTEGER` | `TRY_CAST(x AS INTEGER)`. |
| `sn_duration_s(x)` | `BIGINT` | digits only → that many seconds; else `epoch(ts_utc(x))` (ServiceNow glide duration `1970-01-01 HH:MM:SS` style); else NULL. |
| `jstr(x, path)` | `VARCHAR` | `CASE WHEN json_valid(x) THEN json_extract_string(x, path) END`. |
| `json_names(x)` | `VARCHAR[]` | `CASE WHEN json_valid(x) THEN json_extract_string(x, '$[*].name') END`. |
| `json_str_list(x)` | `VARCHAR[]` | `CASE WHEN json_valid(x) THEN json_extract_string(x, '$[*]') END`. |
| `jira_text(x)` | `VARCHAR` | If `json_valid(x)` and `json_extract_string(x, '$.type') = 'doc'` (Atlassian document format): the values of every `"text"` key, found with `regexp_extract_all(x, '"text"\s*:\s*"((?:[^"\\]\|\\.)*)"', 1)`, each JSON-unescaped (`json_extract_string('"' \|\| s \|\| '"', '$')`) and joined with a single space. Otherwise `x` unchanged (Data Center wiki text). |
| `team_value(x)` | `VARCHAR` | If `json_valid(x)` and `x` is an object: `coalesce($.name, $.title, $.value)`; if a JSON string: its value; else `x`. |

#### U02-109 `herness/model/sql/110_stg_servicenow.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage staging) |
| Purpose | Deduplicated, typed ServiceNow staging tables (design 02 §4.2). |
| Inputs | Raw `servicenow/{incident, change_request, problem, cmdb_ci, cmdb_ci_service, cmdb_rel_ci, sys_user_group, cmn_department, task_sla}`; `stg.enum_map`, `stg.deleted_record`; custom field names from the context. |
| Outputs | Tables below; rows in `stg.cast_stats`. Every table also has `record_id` (`_record_id`), `source_key`, `source_updated_at`. |
| Postconditions | One row per live `record_id`; absent entities give empty tables with the same columns. Any of `cmdb_ci_service`, `cmn_department` and `task_sla` may be missing from the lake, and the build still succeeds (R-60). |
| Errors | `BuildSqlError` through U02-97. |
| Tests | IT02-02…IT02-08, IT02-10, IT02-12…IT02-15 |

| Table · column | Type | Rule (raw column → value) |
|---|---|---|
| `stg.sn_incident` · `number` | VARCHAR | `number` |
| · `opened_at`, `resolved_at`, `closed_at` | TIMESTAMPTZ | `ts_utc` of the same-named raw column (typed) |
| · `acknowledged_at` | TIMESTAMPTZ | `ts_utc(raw(custom_fields.servicenow.acknowledged_at))` when the custom field is configured, else NULL without flags |
| · `priority` | SMALLINT | `lead_int(priority, 1, 5)` (typed) |
| · `state` | VARCHAR | enum `servicenow.incident_state` on `coalesce(state, incident_state)` (typed) |
| · `business_service`, `cmdb_ci`, `assignment_group`, `problem_id`, `caused_by` | VARCHAR | raw sys_id values |
| · `reassignment_count`, `reopen_count` | INTEGER | `to_int` (typed) |
| · `short_description`, `description`, `close_notes`, `close_code` | VARCHAR | raw |
| · `made_sla` | BOOLEAN | `to_bool(made_sla)` (typed) |
| · `business_duration_s` | BIGINT | `sn_duration_s(business_duration)` (typed) |
| · `customer_impact_minutes` | DOUBLE | `to_double(raw(custom_fields.servicenow.customer_impact_minutes))` when configured (typed), else NULL |
| `stg.sn_change_request` · `number`, `business_service`, `cmdb_ci`, `assignment_group`, `short_description`, `description` | VARCHAR | raw |
| · `type` | VARCHAR | enum `servicenow.change_type` on `type` (typed) |
| · `state`, `risk` | VARCHAR | `coalesce(<f>_display, <f>)` |
| · `opened_at`, `planned_start`, `planned_end`, `actual_start`, `actual_end` | TIMESTAMPTZ | `ts_utc` of `opened_at`, `start_date`, `end_date`, `work_start`, `work_end` (typed) |
| · `outcome` | VARCHAR | enum `servicenow.change_close_code` on `close_code` (typed) |
| `stg.sn_problem` · `number`, `business_service`, `assignment_group` | VARCHAR | raw |
| · `opened_at`, `resolved_at` | TIMESTAMPTZ | typed `ts_utc` |
| · `state` | VARCHAR | `coalesce(problem_state_display, state_display, problem_state, state)` |
| · `known_error` | BOOLEAN | typed `to_bool` |
| · `root_cause_text` | VARCHAR | `cause_notes` |
| `stg.sn_ci` · `sys_id` | VARCHAR | `_source_key`; one row per `sys_id` over both `cmdb_ci` and `cmdb_ci_service` (the `cmdb_ci_service` row wins for `name`, `criticality`; ties by later `source_updated_at`) |
| · `name`, `owned_by`, `support_group`, `cost_center`, `company` | VARCHAR | raw |
| · `ci_class` | VARCHAR | `sys_class_name`; `cmdb_ci_service` for rows of that entity without the column |
| · `criticality` | SMALLINT | `lead_int(busines_criticality, 1, 4)` read from `cmdb_ci_service` rows only (the source's spelling, R-60); a `busines_criticality` column on `cmdb_ci` rows is ignored; NULL when `cmdb_ci_service` is absent (typed) |
| `stg.sn_rel_ci` · `parent`, `child` | VARCHAR | raw sys_ids |
| · `type` | VARCHAR | `coalesce(type_display, type)` |
| `stg.sn_group` · `sys_id`, `name`, `parent`, `manager`, `cost_center`, `type` | VARCHAR | raw |
| · `active` | BOOLEAN | typed `to_bool` |
| `stg.sn_department` · `sys_id`, `name`, `parent`, `cost_center` | VARCHAR | raw |
| `stg.sn_task_sla` · `task` | VARCHAR | raw sys_id of the incident |
| · `has_breached` | BOOLEAN | typed `to_bool` |

#### U02-110 `herness/model/sql/120_stg_jira.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage staging) |
| Purpose | Jira issue staging plus unnested transitions and links. |
| Inputs | Raw `jira/issue`. Raw column names follow impl 01's Jira raw column contract (`T01-17 (herness.connectors.jira.JIRA_ISSUE_COLUMNS)` plus the configured custom field columns), which impl 01 owns (R-59); §4.1.2 lists the columns this file reads. |
| Outputs | `stg.jira_issue`, `stg.jira_transition`, `stg.jira_link`; `stg.cast_stats` rows. |
| Tests | IT02-18…IT02-20 |

| Table · column | Type | Rule |
|---|---|---|
| `stg.jira_issue` · `key` | VARCHAR | `key` |
| · `type` | VARCHAR | enum `jira.issue_type` on `jstr(issuetype, '$.name')` (typed) |
| · `parent_key` | VARCHAR | `coalesce(jstr(parent, '$.key'), raw(custom_fields.jira.epic_link))` |
| · `project` | VARCHAR | `jstr(project, '$.key')` |
| · `components` | VARCHAR[] | `json_names(components)` |
| · `labels` | VARCHAR[] | `json_str_list(labels)` |
| · `status` | VARCHAR | `jstr(status, '$.name')` |
| · `status_category` | VARCHAR | enum `jira.status_category_key` on `jstr(status, '$.statusCategory.key')`, else enum `jira.status_category` on the status name (typed; failure only when both miss) |
| · `created_at`, `resolved_at` | TIMESTAMPTZ | typed `ts_utc(created)`, `ts_utc(resolutiondate)` |
| · `story_points` | DOUBLE | typed `to_double(raw(custom_fields.jira.story_points))` when configured |
| · `estimate_cost_usd` | DECIMAL(18,2) | typed `TRY_CAST(raw(custom_fields.jira.estimate_cost_usd) AS DECIMAL(18,2))` when configured |
| · `team_value` | VARCHAR | `team_value(raw(custom_fields.jira.team))` when configured |
| · `summary` | VARCHAR | `summary` |
| · `description` | VARCHAR | `jira_text(description)` |
| · `changelog`, `issuelinks`, `remotelinks` | VARCHAR | raw JSON text |
| `stg.jira_transition` · `record_id` | VARCHAR | issue `record_id` |
| · `at` | TIMESTAMPTZ | `ts_utc(history.created)` for each history in `changelog` (a JSON array of histories, or an object whose `histories` key holds it) and each item with `field = 'status'` |
| · `from_status`, `to_status` | VARCHAR | item `fromString`, `toString` |
| · `from_category`, `to_category` | VARCHAR | enum `jira.status_category` on each status name |
| `stg.jira_link` · `from_key`, `to_key`, `link_type` | VARCHAR | From `issuelinks` elements: `outwardIssue` present → (issue key, `outwardIssue.key`, `type.name`); `inwardIssue` present → (`inwardIssue.key`, issue key, `type.name`). From `remotelinks` elements: every match of `(INC\|CHG\|PRB)[0-9]{4,}` in `object.url` or `object.title` → (issue key, match, `mentions_incident`). `DISTINCT`. |

#### U02-111 `herness/model/sql/130_stg_monitoring.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage staging) |
| Purpose | Monitoring events and daily metrics staging. |
| Inputs | Raw `monitoring/event`, `monitoring/metric_daily`. |
| Outputs | `stg.mon_event`, `stg.mon_metric_daily`; `stg.cast_stats`. |
| Tests | IT02-16, IT02-17 |

| Table · column | Type | Rule |
|---|---|---|
| `stg.mon_event` · `source_tool`, `event_key`, `host`, `status`, `dedup_key`, `incident_ref` | VARCHAR | raw |
| · `service_name` | VARCHAR | `service` |
| · `ts`, `end_ts` | TIMESTAMPTZ | typed `ts_utc` |
| · `severity` | VARCHAR | enum `monitoring.severity` on `severity_raw` (typed) |
| · `alert_name` | VARCHAR | `title` |
| `stg.mon_metric_daily` · `source_tool`, `metric_name`, `unit` | VARCHAR | raw |
| · `service_name` | VARCHAR | `service` |
| · `date` | DATE | typed `to_date(date)` |
| · `value` | DOUBLE | typed `to_double(value)` |

#### U02-112 `herness/model/sql/140_stg_files.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage staging) |
| Purpose | Generic staging of every configured `files` entity (spec 01 §5.10). |
| Inputs | Raw `files/<entity>` for each entity in `extra_entities["files"]`. |
| Outputs | `stg.files_<entity>`: `record_id`, `source_key`, `source_updated_at` and every other present lake column except the metadata columns and `_payload`, with the lake types (no casting). |
| Algorithm | Jinja loop over configured entities; each table from `latest(...)` with all inventory columns. Canonical feeds from these tables are open item OI-05; no `core` table reads them in this version. |
| Tests | IT02-08 |

#### U02-113 `herness/model/sql/150_stg_mongodb.sql`, U02-114 `160_stg_snowflake.sql`, U02-115 `170_stg_dataverse.sql`

| Field | Content |
|---|---|
| Kind | SQL files (stage staging), same shape as U02-112 |
| Purpose | Generic staging of configured `mongodb`, `snowflake`, `dataverse` entities (design 02 §4.1). |
| Outputs | `stg.mongodb_<entity>`, `stg.snowflake_<entity>`, `stg.dataverse_<entity>`. |
| Algorithm | As U02-112. |
| Tests | IT02-08 |

#### U02-116 `herness/model/sql/200_org_team_service.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage core) |
| Purpose | `core.org`, `core.team`, `core.service` (design 02 §4.3). |
| Inputs | `stg.sn_department`, `stg.sn_group`, `stg.sn_ci`, `stg.service_ci_class`. |
| Outputs | Tables below. |
| Algorithm | Org mode: `department` when `stg.sn_department` has at least one row, else `group_hierarchy` (design 02: "falling back to the group hierarchy"). Group-hierarchy mode: a group with at least one child group (a group whose `parent` is its `sys_id`) is an org; a group with no child group is a team. |
| Tests | IT02-09, IT02-10 |

| Table · column | Type | Rule |
|---|---|---|
| `core.org` · `org_id` | VARCHAR | department mode: `rid('servicenow:cmn_department', sys_id)`; hierarchy mode: `rid('servicenow:sys_user_group', sys_id)` of org groups |
| · `name`, `cost_center` | VARCHAR | from the department or group |
| · `parent_org_id` | VARCHAR | the parent's ID in the same mode; NULL when the parent is absent or not an org |
| · `source` | VARCHAR | `'servicenow'` |
| `core.team` · `team_id` | VARCHAR | `rid('servicenow:sys_user_group', sys_id)`; department mode: every group; hierarchy mode: leaf groups |
| · `name` | VARCHAR | group name |
| · `org_id` | VARCHAR | department mode: the department with the same non-empty `cost_center` (lowest `sys_id` when several; OI-04), else NULL; hierarchy mode: the parent group's `org_id` when the parent is an org |
| · `source` | VARCHAR | `'servicenow'` |
| · `active` | BOOLEAN | `coalesce(active, true)` |
| `core.service` · `service_id` | VARCHAR | `rid('servicenow:cmdb_ci', sys_id)` for `stg.sn_ci` rows whose `ci_class` is in `stg.service_ci_class` |
| · `name`, `ci_class` | VARCHAR | from `stg.sn_ci` |
| · `criticality` | SMALLINT | `stg.sn_ci.criticality` (1 highest … 4) |
| · `business_owner_team_id` | VARCHAR | the first of `rid(group, owned_by)`, `rid(group, support_group)` that exists in `core.team` (spec 01 §4.2 order), else NULL |
| · `org_id` | VARCHAR | owner team's `org_id` |
| · `source` | VARCHAR | `'servicenow'` |

#### U02-117 `herness/model/sql/220_service_map.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage core) |
| Purpose | `core.service_map` with resolution order overrides → CMDB → approved suggestions (design 02 §4.3; D1), plus the service name lookup used by monitoring. |
| Inputs | `stg.service_override`, `stg.service_alias`, `core.service`, `core.team`, `stg.sn_ci`, `stg.approved_mapping`. |
| Outputs | `core.service_map`; `stg.service_name_lookup(name_lc VARCHAR, service_id VARCHAR)`. |
| Algorithm | (1) Candidates: (a) each override → (`service_id`, `team_id`, `jira_project`, `jira_component`, `org_id` = `coalesce(override.org_id, team.org_id, service.org_id)`, `role`, `link_source 'override'`, `confidence 1.0`, rank 1); (b) CMDB owner: (`service_id`, `business_owner_team_id`, NULL, NULL, team `org_id`, `'owner'`, `'cmdb'`, 1.0, rank 2) where the owner is not NULL; CMDB support: (`service_id`, `rid(group, support_group)`, …, `'support'`, `'cmdb'`, 1.0, rank 2) where that team exists and differs from the owner; (c) approved suggestions: `subject_type = 'jira_component'` → (`service_id`, payload `team_id` (nullable), `jira_project`, `jira_component`, org of team or service, `'delivery'`, `'suggested_approved'`, `score`, rank 3); `subject_type = 'team'` → (`service_id`, `team_id`, NULL, NULL, team org, `'support'`, `'suggested_approved'`, `score`, rank 3). (2) Identity key: rows with `jira_project` → (`'jira'`, `jira_project`, `coalesce(jira_component, '')`); other rows → (`'team'`, `service_id`, `role`, `team_id`). (3) Keep, per key, the row with the lowest rank, then lowest `service_id`, then lowest `team_id`. (4) Lookup table: every override alias (lower-cased) → its service; plus every `lower(core.service.name)` that belongs to exactly one service and is not already an alias. |
| Tests | IT02-11 |

| Column | Type | Meaning |
|---|---|---|
| `service_id`, `team_id`, `jira_project`, `jira_component`, `org_id` | VARCHAR | as resolved |
| `role` | VARCHAR | `owner` \| `support` \| `delivery` |
| `link_source` | VARCHAR | `cmdb` \| `override` \| `suggested_approved` |
| `confidence` | DOUBLE | 1.0 for override and CMDB; suggestion `score` |

#### U02-118 `herness/model/sql/230_incident.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage core) |
| Purpose | `core.incident` (design 02 §4.3). |
| Inputs | `stg.sn_incident`, `stg.sn_task_sla`, `stg.sn_rel_ci`, `core.service`. |
| Outputs | `core.incident`. |
| Tests | IT02-12, IT02-13 |

| Column | Type | Rule |
|---|---|---|
| `record_id` | VARCHAR | staging `record_id` |
| `number` | VARCHAR | |
| `opened_at`, `acknowledged_at`, `resolved_at`, `closed_at` | TIMESTAMPTZ | staging values (`acknowledged_at` NULL when the custom field is not configured: MTTA disabled) |
| `priority` | SMALLINT | 1–5 |
| `state` | VARCHAR | canonical state |
| `service_id` | VARCHAR | `rid('servicenow:cmdb_ci', business_service)` when non-empty; else the single `core.service` whose sys_id is the `parent` of a `stg.sn_rel_ci` row with `child` = the incident's `cmdb_ci` (NULL when zero or several) |
| `ci_id` | VARCHAR | `rid('servicenow:cmdb_ci', cmdb_ci)` |
| `team_id` | VARCHAR | `rid('servicenow:sys_user_group', assignment_group)` of the latest version (the assignment group at resolution for resolved incidents; OI-11) |
| `reassignment_count`, `reopen_count` | INTEGER | |
| `short_description`, `description`, `close_notes` | VARCHAR | raw text |
| `close_code` | VARCHAR | raw |
| `problem_id` | VARCHAR | `rid('servicenow:problem', problem_id)` |
| `caused_by_change_id` | VARCHAR | `rid('servicenow:change_request', caused_by)` |
| `sla_breached` | BOOLEAN | `bool_or(has_breached)` over `stg.sn_task_sla` rows with `task` = the incident sys_id when any exist; else `NOT made_sla` |
| `business_duration_s` | BIGINT | |
| `customer_impact_minutes` | DOUBLE | custom field value; never estimated |
| `content_hash` | VARCHAR | NULL here; filled by `300_attach_decisions.sql` |
| `source_updated_at` | TIMESTAMPTZ | |

#### U02-119 `herness/model/sql/240_change.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage core) |
| Purpose | `core.change`. |
| Inputs | `stg.sn_change_request`, `stg.sn_rel_ci`, `core.service`. |
| Outputs | `core.change`. |
| Tests | IT02-14 |

| Column | Type | Rule |
|---|---|---|
| `record_id`, `number`, `state`, `risk` | VARCHAR | staging |
| `type` | VARCHAR | `standard` \| `normal` \| `emergency` \| NULL |
| `opened_at`, `planned_start`, `planned_end`, `actual_start`, `actual_end` | TIMESTAMPTZ | staging |
| `service_id` | VARCHAR | same rule as `core.incident.service_id` |
| `ci_id`, `team_id` | VARCHAR | `rid` of `cmdb_ci`, `assignment_group` |
| `outcome` | VARCHAR | staging `outcome`; when NULL and `lower(state)` in (`canceled`, `cancelled`) → `canceled` |
| `short_description`, `description` | VARCHAR | raw |
| `source_updated_at` | TIMESTAMPTZ | |

#### U02-120 `herness/model/sql/250_problem.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage core) |
| Purpose | `core.problem`. |
| Inputs | `stg.sn_problem`. |
| Outputs | `core.problem`: `record_id`, `number` VARCHAR; `opened_at`, `resolved_at` TIMESTAMPTZ; `state` VARCHAR; `service_id` = `rid('servicenow:cmdb_ci', business_service)`; `team_id` = `rid('servicenow:sys_user_group', assignment_group)`; `known_error` BOOLEAN; `root_cause_text` VARCHAR; `source_updated_at` TIMESTAMPTZ. |
| Tests | IT02-15 |

#### U02-121 `herness/model/sql/260_event.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage core) |
| Purpose | `core.event`. |
| Inputs | `stg.mon_event`, `stg.service_name_lookup`, `core.incident`. |
| Outputs | `core.event`. |
| Tests | IT02-16 |

| Column | Type | Rule |
|---|---|---|
| `event_id` | VARCHAR | staging `record_id` (`monitoring:event:<source_tool>:<event_key>`) |
| `source_tool`, `host`, `alert_name`, `status`, `dedup_key` | VARCHAR | staging |
| `ts` | TIMESTAMPTZ | staging |
| `service_id` | VARCHAR | `stg.service_name_lookup` on `lower(service_name)`; NULL when not found |
| `severity` | VARCHAR | canonical severity |
| `duration_s` | BIGINT | `epoch(end_ts) − epoch(ts)` when both are set and `end_ts ≥ ts`, else NULL |
| `incident_id` | VARCHAR | `core.incident.record_id` whose `number` = `incident_ref` when exactly one matches, else NULL |

#### U02-122 `herness/model/sql/270_metric_daily.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage core) |
| Purpose | `core.metric_daily` with its primary key (`date`, `service_id`, `metric_name`, `source_tool`). |
| Inputs | `stg.mon_metric_daily`, `stg.service_name_lookup`. |
| Outputs | `core.metric_daily` (`date DATE`, `service_id VARCHAR`, `metric_name VARCHAR`, `value DOUBLE`, `unit VARCHAR`, `source_tool VARCHAR`); `stg.build_counts` rows `metric_daily_raw` (staging rows) and `metric_daily_unmapped` (rows dropped because `service_id` or `date` is NULL). |
| Algorithm | Resolve `service_id` by name lookup; drop rows with NULL `service_id`, `date` or `metric_name`; per key keep the row with the latest `source_updated_at`, then highest `record_id`. |
| Tests | IT02-17 |

#### U02-123 `herness/model/sql/280_work_item.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage core) |
| Purpose | `core.work_item`, `core.work_item_transition`, `core.work_item_link`. |
| Inputs | `stg.jira_issue`, `stg.jira_transition`, `stg.jira_link`, `core.service_map`, `core.team`. |
| Outputs | Tables below. |
| Tests | IT02-18…IT02-20 |

| Table · column | Type | Rule |
|---|---|---|
| `core.work_item` · `record_id`, `key`, `type`, `parent_key`, `project`, `status`, `status_category`, `summary`, `description` | VARCHAR | staging |
| · `component` | VARCHAR | `components[1]` (first in source order) |
| · `components`, `labels` | VARCHAR[] | staging |
| · `created_at`, `resolved_at`, `source_updated_at` | TIMESTAMPTZ | staging |
| · `story_points` | DOUBLE | staging |
| · `estimate_cost_usd` | DECIMAL(18,2) | staging |
| · `service_id` | VARCHAR | the single `service_id` of `core.service_map` rows with `role = 'delivery'` matching (`project`, `component`); else matching (`project`, NULL component); NULL when zero or several |
| · `team_id` | VARCHAR | `team_id` of the matched delivery row when not NULL; else the single `core.team` whose `lower(name)` = `lower(team_value)`; else NULL |
| `core.work_item_transition` · `record_id`, `from_status`, `to_status`, `from_category`, `to_category` | VARCHAR | staging, only for issues present in `core.work_item` |
| · `at` | TIMESTAMPTZ | staging |
| `core.work_item_link` · `from_key`, `to_key`, `link_type` | VARCHAR | staging, `DISTINCT` |

#### U02-124 `herness/model/sql/300_attach_decisions.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage attach) |
| Purpose | Attach enrichment to the canonical model and drop enrichment rows of records that no longer exist (deleted or tombstoned since the previous build). |
| Inputs | `enrich.*`, `core.incident`, `core.change`, `core.problem`, `core.work_item`. |
| Outputs | Updated `core.incident.content_hash`; pruned `enrich.text_redacted`, `enrich.decision`, `enrich.cluster_member`, `enrich.incident_change_link`; view `enrich.decision_wide` when spec 03 did not create it; `stg.build_counts` row `enrich_pruned`. |
| Algorithm | (1) Live IDs = `record_id` union of the four core tables. (2) Count, then delete rows of `enrich.text_redacted`, `enrich.decision`, `enrich.cluster_member` whose `record_id` is not live, and rows of `enrich.incident_change_link` whose `incident_id` or `change_id` is not live. (3) `UPDATE core.incident SET content_hash = t.content_hash FROM enrich.text_redacted t WHERE t.record_id = core.incident.record_id`. (4) `CREATE VIEW IF NOT EXISTS enrich.decision_wide AS SELECT DISTINCT record_id FROM enrich.decision`. |
| Security notes | TH02-16. |
| Tests | IT02-23, ST02-16 |

#### U02-125 `herness/model/sql/310_attach_clusters.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage attach) |
| Purpose | Keep cluster membership consistent with `enrich.cluster`. |
| Inputs | `enrich.cluster`, `enrich.cluster_member`. |
| Outputs | `stg.build_counts` row `cluster_member_orphans`; orphan member rows deleted. |
| Algorithm | Count and delete `enrich.cluster_member` rows whose `cluster_id` is not in `enrich.cluster`. |
| Tests | IT02-24 |

#### U02-126 `herness/model/sql/400_facts.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage facts), content owned by spec 04 (`T04-06 (herness/model/sql/400_facts.sql)`, with the change and work-item facts added by T04-07) |
| Purpose | `metrics.incident_fact`, `metrics.change_fact`, `metrics.work_item_fact`, `metrics.org_closure`, `metrics.work_item_closure` (design 02 §4.5; types in spec 04 §4.2). |
| Algorithm | Never rendered by `run_sql_range`; executed by `materialize_facts(con, build_id)` (U02-101). This spec only reserves the file number range 400–499 and checks the tables exist before DQ (`collect_row_counts`). |
| Tests | IT02-26 |

#### U02-127 `herness/model/sql/900_dq_checks.sql`

| Field | Content |
|---|---|
| Kind | SQL file (stage dq) |
| Purpose | Write design 02 §4.8 checks into `meta.dq_result`. |
| Inputs | `core.*`, `enrich.text_redacted`, `enrich.decision`, `stg.cast_stats`, `stg.build_counts`, `stg.prev_row_counts`, `meta.build.started_at`; thresholds from `dq` via `num`. |
| Outputs | `meta.dq_result` rows whose `details` JSON has `"producer": "dq900"`. |
| Algorithm | (1) `DELETE FROM meta.dq_result WHERE json_extract_string(details, '$.producer') = 'dq900'` (spec 04 rows are kept). (2) Insert the checks in the table below. |
| Tests | IT02-28…IT02-31 |

| `check_name` | `severity` | `value` | `threshold` | `passed` | `details` (besides `producer`) |
|---|---|---|---|---|---|
| `row_count_drop:core.<t>` for `org`, `team`, `service`, `service_map`, `incident`, `change`, `problem`, `event`, `metric_daily`, `work_item`, `work_item_transition`, `work_item_link` | error | `(prev − cur) / prev` when `prev > 0`, else 0 | `row_count_drop_max` | `value ≤ threshold` | `prev`, `cur`; `no_previous_build: true` when `stg.prev_row_counts` is empty |
| `incident_service_null` | `error` when `value > incident_service_null_error`, else `warn` | share of `core.incident` rows with `service_id` NULL (0 when empty) | the threshold of the chosen severity | `value ≤ incident_service_null_warn` for `warn`; false for `error` | `null_rows`, `rows` |
| `work_item_service_null` | warn | share with NULL `service_id` | `work_item_service_null_warn` | `≤` | `null_rows`, `rows` |
| `cast_fail:<table>.<column>` per `stg.cast_stats` row with `non_null > 0` | warn | `failed / non_null` | `cast_fail_warn` | `≤` | `failed`, `non_null` |
| `future_timestamp:core.<t>.<c>` for `incident.{opened_at, resolved_at, closed_at}`, `change.{opened_at, actual_start, actual_end}`, `problem.{opened_at, resolved_at}`, `event.ts`, `work_item.{created_at, resolved_at}` | warn | count of values `> meta.build.started_at` | `future_timestamp_max` | `≤` | — |
| `resolved_before_opened` | warn | share of incidents with `resolved_at < opened_at` among those with both set | `resolved_before_opened_warn` | `≤` | `rows` (count) |
| `duplicate_key:core.<t>` for `org(org_id)`, `team(team_id)`, `service(service_id)`, `incident`, `change`, `problem`, `work_item` (`record_id`), `event(event_id)`, `metric_daily(date, service_id, metric_name, source_tool)` | error | `count(*) − count(DISTINCT key)` | `duplicate_key_max` | `≤` | — |
| `decision_coverage_incident` | warn | incidents with a `enrich.text_redacted` row that have ≥ 1 `enrich.decision` row, divided by incidents with a text row; 1.0 when `core.incident` is empty; 0.0 when incidents exist but no text rows | `decision_coverage_min` | `value ≥ threshold` | `covered`, `with_text` |
| `metric_daily_unmapped_service` (DD02-07) | warn | `metric_daily_unmapped / metric_daily_raw` (0 when raw is 0) | `metric_daily_unmapped_warn` | `≤` | `unmapped`, `raw` |

### 3.11 Configuration models

The config models are units U02-71…U02-75 in §3.9. Their keys, defaults and validation rules are listed in §9.

## 4. State and data

### 4.1 Raw lake

#### 4.1.1 Files

| Item | Rule |
|---|---|
| Path | `data/raw/<source>/<entity>/dt=YYYY-MM-DD/part-<ulid>.parquet` (design 02 §3.1); `dt` = UTC date of `_fetched_at` |
| Temp name | `.part-<ulidA>.parquet.tmp-<ulidB>` in the target partition; purge rewrites use `.part-<ulid>.parquet.tmp-<ulid2>` |
| Format | Parquet 2.6, zstd level 3, dictionary on, statistics on; row groups of ≤ 131,072 rows; file target 128 MiB (`target_bytes`) |
| Schema | `META_COLUMNS` first, then entity columns; one schema per file |
| Writers | `LakeWriter` (connectors, spec 11 generator); `purge_record_ids`, `purge_partitions_before` (spec 10 jobs). Append-only except privacy deletion and retention purge; there is no lake compaction (R-57). |
| Idempotency | Appends are not idempotent by themselves; repeated rows collapse in staging dedupe (`_record_id`, latest `_source_updated_at`, `_fetched_at`, `filename`). Purge is idempotent. |
| Transactions | A `LakeWriter` commit renames files one by one; the file set is not atomic as a whole (FT02-06); each file is. |
| Retention | Partitions older than `retention.raw_lake_months` (36) deleted by spec 10 through U02-23. |

#### 4.1.2 Raw columns read by staging (contracts owned by impl 01, R-59)

Impl 01 owns the raw column-name contract that connectors write to the lake (R-59); impl 11's generator writes the same contract. This table only lists what staging reads. Where it differs from impl 01, impl 01 wins and staging changes.

| Source | Columns the staging SQL reads |
|---|---|
| ServiceNow (every entity) | Each field `f` of the Table API response as `f` (value: sys_id for references, internal UTC text for dates) and `f_display` (display value) when present (spec 01 §5.7). `busines_criticality` is read from `cmdb_ci_service` only; `cmdb_ci_service`, `cmn_department` and `task_sla` may be absent (R-60). |
| Jira `issue` | `T01-17 (herness.connectors.jira.JIRA_ISSUE_COLUMNS)` = `id`, `key`, the `fields` keys `issuetype`, `parent`, `project`, `components`, `labels`, `status`, `created`, `resolutiondate`, `summary`, `description`, `updated`, `issuelinks` (objects and arrays as JSON text), plus `changelog` and `remotelinks` as JSON text; plus each configured custom field id (`customfield_NNNNN`) as its own column. `changelog` is either a JSON array of histories or an object whose `histories` key holds it; staging accepts both. |
| Monitoring | Columns listed in spec 01 §4.2 (`source_tool`, `event_key`, `ts`, `service`, `host`, `severity_raw`, `title`, `status`, `dedup_key`, `end_ts`, `incident_ref`; `date`, `metric_name`, `value`, `unit`). Watermarks are kept per tool (`watermark.source = monitoring:<tool>`, R-62). |
| Config-defined | Whatever the connector writes; staged generically |

### 4.2 Warehouse (DuckDB)

| Item | Rule |
|---|---|
| Files | `data/warehouse/wh-<build_id>.duckdb`, `.wal`, spill dir `tmp/<build_id>/`, pointer `CURRENT` |
| Schemas | `stg` (build-internal; denied to agents by spec 05), `core`, `enrich`, `metrics`, `score`, `meta`, plus macros in `main` |
| Owner of writes | Only the `build_pipeline` job, through `open_for_build` |
| Column schemas | `core.*` per U02-116…U02-123; `stg.*` per U02-109…U02-115 and U02-87; `enrich.*`, `meta.*` per U02-107; `metrics.*`, `score.*` per spec 04 §4.2–4.3 |
| Idempotency | Each SQL statement is `CREATE OR REPLACE` or guarded by `IF NOT EXISTS`; `meta.build` has exactly one row per file; `meta.dq_result` rows of this spec are replaced by producer tag `dq900`; `meta.evidence` keyed by `query_id` (spec 04 inserts with conflict-ignore) |
| Transactions | DuckDB auto-commit per statement; `CHECKPOINT` at the end of each stage; a stage is the resume unit |
| Lifecycle | `meta.build.status`: `building` (created; `finished_at` NULL while a stage runs, set when a requested stage set ends without promote) → `failed` (SQL error or DQ block) or `promoted` (CURRENT switched) → `retired` (superseded; written best-effort, DD02-09) → deleted by retention |
| Retention | CURRENT + newest `build.keep_last − 1` promoted/retired builds; the newest failed build; builds pinned by non-terminal runs; orphans deleted at the next pipeline start |

### 4.3 Ops store (SQLite `data/ops.sqlite`)

Global rules: every ordinary table is `STRICT`; timestamps are 27-character fixed-width text with the GLOB check of §3.6; JSON columns have `json_valid` checks; money is TEXT decimal strings (spec 00 §8); booleans are `INTEGER` with `CHECK (col IN (0, 1))`. "N" = NOT NULL. Owners write through their `herness.store.ops` area submodule (§2.3, R-08). `schema_migration` (`version INTEGER PK`, `name TEXT N`, `checksum TEXT N`, `applied_at TEXT N`) is created by U02-45 itself. Migrations 001–006 below create every table named in the design specs (R-11). Tables and columns that exist only in an implementation spec are created by that owner's migrations in its range of `MIGRATION_RANGES` (U02-129) and are specified in the owner spec; U02-45 applies them in numeric order.

#### 4.3.1 Migration 001 (design 02 §5.1)

| Table | Column | Type | N | Constraint / default | Meaning |
|---|---|---|---|---|---|
| `watermark` | `source` | TEXT | N | PK part | connector, or `monitoring:<tool>` (one watermark per monitoring tool, R-62) |
| | `entity` | TEXT | N | PK part | |
| | `field` | TEXT | N | | watermark field name |
| | `value` | TEXT | N | ts check | last committed `_source_updated_at` |
| | `updated_at` | TEXT | N | ts check | |
| `sync_slice` | `source`, `entity`, `slice_start` | TEXT | N | PK (`source`, `entity`, `slice_start`); `slice_start` ts check | |
| | `slice_end` | TEXT | N | ts check | |
| | `status` | TEXT | N | `IN ('pending','running','done','failed')`, default `'pending'` | |
| | `rows` | INTEGER | N | default 0, `≥ 0` | |
| | `files` | TEXT | N | JSON, default `'[]'` | |
| | `attempts` | INTEGER | N | default 0 | |
| | `last_error` | TEXT | | | redacted, ≤ 2 KB (owner 01) |
| | `updated_at` | TEXT | N | ts check | |
| `file_ingest` | `fingerprint` | TEXT | N | PK; `length = 64` | SHA-256 of file bytes |
| | `source`, `entity`, `path` | TEXT | N | | |
| | `size_bytes`, `rows` | INTEGER | N | `≥ 0` | |
| | `mtime`, `ingested_at` | TEXT | N | ts check | |
| | `files` | TEXT | N | JSON | |
| `source_health` | `source` | TEXT | N | PK | key convention of design 02 §5.1 |
| | `state` | TEXT | N | `IN ('closed','open','half_open')`, default `'closed'` | |
| | `failures`, `trips` | INTEGER | N | default 0 | |
| | `opened_at` | TEXT | | ts check | |
| | `last_error` | TEXT | | | |
| | `updated_at` | TEXT | N | ts check | |

#### 4.3.2 Migration 002 (design 02 §5.2)

| Table | Column | Type | N | Constraint / default |
|---|---|---|---|---|
| `job` | `job_id` | TEXT | N | PK |
| | `kind` | TEXT | N | `IN ('sync','reconcile','build_pipeline','distill','review','chat','outcome_measure','memory_maintenance','maintenance','eval')` |
| | `gpu_class` | TEXT | N | `IN ('none','reasoning','decider','large')` |
| | `status` | TEXT | N | `IN ('queued','running','done','failed','canceled')`, default `'queued'` |
| | `priority` | INTEGER | N | `BETWEEN 0 AND 100` |
| | `payload` | TEXT | N | JSON; `length(payload) ≤ 65536` |
| | `idem_key` | TEXT | | |
| | `result`, `last_error` | TEXT | | JSON |
| | `attempts` | INTEGER | N | default 0 |
| | `max_attempts` | INTEGER | N | `≥ 1` |
| | `lease_owner` | TEXT | | |
| | `lease_expires_at`, `started_at`, `finished_at` | TEXT | | ts check |
| | `scheduled_for`, `created_at` | TEXT | N | ts check |
| `worker` | `worker_id` | TEXT | N | PK |
| | `host` | TEXT | N | |
| | `pid`, `gpu_slot`, `cpu_slots` | INTEGER | N | `≥ 0` |
| | `gpu_class_loaded` | TEXT | N | `IN ('none','reasoning','decider','large','swapping')`, default `'none'` |
| | `requested_class` | TEXT | | `IN ('none','reasoning','decider','large')` or NULL |
| | `status` | TEXT | N | `IN ('starting','running','draining','stopped')` |
| | `current_jobs` | TEXT | N | JSON, default `'[]'` |
| | `started_at`, `heartbeat_at` | TEXT | N | ts check |
| | `version` | TEXT | N | |
| | `faults_enabled` | INTEGER | N | boolean, default 0 |
| `resilience_event` | `event_id` | TEXT | N | PK |
| | `ts` | TEXT | N | ts check |
| | `kind`, `component` | TEXT | N | (values owned by spec 08 §4.2; no CHECK) |
| | `target`, `run_id`, `job_id`, `task_id` | TEXT | | |
| | `detail` | TEXT | N | JSON, default `'{}'` |

Retention of `resilience_event`: 90 days, purged by spec 08.

#### 4.3.3 Migration 003 (design 02 §5.3)

| Table | Column | Type | N | Constraint / default |
|---|---|---|---|---|
| `run` | `run_id` | TEXT | N | PK |
| | `kind` | TEXT | N | `IN ('funding_review','org_review','chat','eval')` |
| | `depth` | TEXT | N | `IN ('fast','standard','deep')` |
| | `profile`, `config_hash` | TEXT | N | |
| | `build_id` | TEXT | | |
| | `status` | TEXT | N | `IN ('created','planning','running','challenging','verifying','writing','recording','done','partial','failed','canceled')`, default `'created'` |
| | `started_at` | TEXT | N | ts check |
| | `finished_at` | TEXT | | ts check |
| | `token_usage`, `meta` | TEXT | N | JSON, default `'{}'` |
| | `cost_usd` | TEXT | | decimal string |
| `task` | `task_id` | TEXT | N | PK |
| | `run_id` | TEXT | N | `REFERENCES run(run_id)` |
| | `parent_task_id` | TEXT | | |
| | `role` | TEXT | N | `IN ('planner','judge','analyst','skeptic','verifier','writer','chat')` |
| | `spec` | TEXT | N | JSON |
| | `status` | TEXT | N | `IN ('pending','running','done','failed','dead')`, default `'pending'` |
| | `attempts` | INTEGER | N | default 0 |
| | `last_error` | TEXT | | |
| | `checkpoint`, `result` | TEXT | | JSON (checkpoint ≤ 4 MiB enforced by spec 08) |
| | `created_at`, `updated_at` | TEXT | N | ts check |
| `finding` | `finding_id` | TEXT | N | PK |
| | `run_id`, `task_id`, `author_role`, `claim` | TEXT | N | |
| | `entity_type`, `entity_id`, `supersedes`, `merged_into` | TEXT | | |
| | `numbers`, `query_ids` | TEXT | N | JSON, default `'[]'` |
| | `confidence` | REAL | | `BETWEEN 0 AND 1` |
| | `status` | TEXT | N | `IN ('proposed','challenged','verified','rejected','revised','merged')`, default `'proposed'` |
| | `challenge`, `verification` | TEXT | | JSON |
| | `created_at` | TEXT | N | ts check |
| `evidence` | `query_id` | TEXT | N | PK; `GLOB 'q_[0-9a-f]*' AND length = 18` |
| | `run_id` | TEXT | | NULL for ad-hoc calls |
| | `build_id`, `sql`, `result_hash` | TEXT | N | |
| | `params`, `result_sample` | TEXT | N | JSON (`result_sample` ≤ 50 rows, default `'[]'`) |
| | `row_count`, `duration_ms` | INTEGER | N | `≥ 0` |
| | `executed_at` | TEXT | N | ts check |
| `evidence_use` | `query_id`, `run_id`, `task_id` | TEXT | N | PK (`query_id`, `run_id`, `task_id`) |
| | `used_at` | TEXT | N | ts check |

#### 4.3.4 Migration 004 (design 02 §5.4)

| Table | Column | Type | N | Constraint / default |
|---|---|---|---|---|
| `memory_item` | `memory_id` | TEXT | N | PK (rowid table) |
| | `layer` | TEXT | N | `IN ('episodic','semantic','procedural')` |
| | `kind`, `content` | TEXT | N | |
| | `data`, `provenance` | TEXT | N | JSON, default `'{}'` |
| | `confidence` | REAL | | |
| | `status` | TEXT | N | `IN ('candidate','pending_approval','active','expired','rejected')` |
| | `created_at` | TEXT | N | ts check |
| | `expires_at`, `last_used_at` | TEXT | | ts check |
| | `use_count` | INTEGER | N | default 0 |
| `memory_fts` | `content`, `kind` | FTS5 | | external content `memory_item`, triggers per U02-52 |
| `recommendation` | `rec_id` | TEXT | N | PK |
| | `run_id`, `target_type`, `target_id`, `summary` | TEXT | N | |
| | `kind` | TEXT | N | `IN ('fund','org_action')` |
| | `numbers` | TEXT | N | JSON |
| | `expected_metric` | TEXT | | |
| | `expected_delta`, `confidence` | REAL | | |
| | `expected_usd` | TEXT | | decimal string |
| | `confidence_basis` | TEXT | N | JSON, default `'{}'` |
| | `finding_ids` | TEXT | N | JSON, default `'[]'` |
| | `created_at` | TEXT | N | ts check |
| `decision_log` | `rec_id` | TEXT | N | `REFERENCES recommendation(rec_id)` |
| | `decision` | TEXT | N | `IN ('accepted','rejected','deferred')` |
| | `reason` | TEXT | | |
| | `decided_by` | TEXT | N | |
| | `decided_at`, `effective_at` | TEXT | N | ts check |
| `outcome` | `outcome_id` | TEXT | N | PK |
| | `rec_id` | TEXT | N | `REFERENCES recommendation(rec_id)`; UNIQUE with `measurement` |
| | `measurement` | INTEGER | N | `≥ 1` |
| | `measured_at` | TEXT | N | ts check |
| | `metric` | TEXT | N | |
| | `baseline`, `actual`, `delta` | REAL | | |
| | `query_id` | TEXT | | |
| | `verdict` | TEXT | N | `IN ('paid_off','no_effect','worse','inconclusive')` |
| | `details` | TEXT | N | JSON, default `'{}'` |

#### 4.3.5 Migration 005 (design 02 §5.5)

| Table | Column | Type | N | Constraint / default |
|---|---|---|---|---|
| `review_item` | `item_id` | TEXT | N | PK |
| | `kind` | TEXT | N | `IN ('mapping_suggestion','label_check','memory_write','weight_change')` |
| | `payload` | TEXT | N | JSON object (`json_type(payload) = 'object'`), `length ≤ 65536` |
| | `status` | TEXT | N | `IN ('pending','approved','rejected')`, default `'pending'` |
| | `created_at` | TEXT | N | ts check |
| | `decided_by`, `note` | TEXT | | `note` length ≤ 2000 |
| | `decided_at` | TEXT | | ts check |
| | table check | | | `(status = 'pending') = (decided_at IS NULL)` and `(decided_at IS NULL) = (decided_by IS NULL)` |
| `chat_session` | `session_id` | TEXT | N | PK |
| | `user_ref` | TEXT | N | `length = 32` |
| | `title`, `summary` | TEXT | | |
| | `created_at`, `last_active_at` | TEXT | N | ts check |
| `chat_message` | `message_id` | TEXT | N | PK |
| | `session_id` | TEXT | N | `REFERENCES chat_session(session_id) ON DELETE CASCADE` |
| | `role` | TEXT | N | `IN ('user','assistant','system')` (spec 09 §4) |
| | `content` | TEXT | N | default `''` |
| | `status` | TEXT | N | `IN ('queued','streaming','done','failed')` |
| | `verified` | TEXT | | `IN ('verified','partial','unverified')` or NULL |
| | `feedback` | TEXT | | `IN ('up','down')` or NULL |
| | `feedback_note`, `run_id` | TEXT | | |
| | `query_ids` | TEXT | N | JSON, default `'[]'` |
| | `meta` | TEXT | N | JSON, default `'{}'` |
| | `created_at` | TEXT | N | ts check |
| `deletion_request` | `request_id` | TEXT | N | PK |
| | `record_id`, `requested_by` | TEXT | N | |
| | `reason_ref` | TEXT | | |
| | `status` | TEXT | N | `IN ('pending','running','done','failed')` |
| | `steps` | TEXT | N | JSON, default `'{}'` |
| | `created_at` | TEXT | N | ts check |
| | `completed_at` | TEXT | | ts check |

#### 4.3.6 Migration 006 (ENG E5; DD02-04)

| Table | Column | Type | N | Constraint / default | Meaning |
|---|---|---|---|---|---|
| `metric_sample` | `ts` | TEXT | N | ts check | sample time |
| | `name` | TEXT | N | `GLOB 'herness_*'` | metric name per ENG §4 |
| | `kind` | TEXT | N | `IN ('counter','gauge','histogram')` | |
| | `value` | REAL | N | | counter increment, gauge value or one histogram observation |
| | `labels` | TEXT | N | JSON object, default `'{}'`, `length ≤ 1024` | low-cardinality labels |
| | `component` | TEXT | N | | |

Rowid primary key. Writer `herness.store.ops.metrics.record_metric_samples` (impl 08, R-12). Retention 90 days (spec 08 purge).

#### 4.3.7 Idempotency keys of the writes this spec owns

| Write | Key | Transaction |
|---|---|---|
| `schema_migration` row | `version` (re-checked inside the migration transaction) | one `BEGIN IMMEDIATE` per migration file, DDL included |
| `review_item` insert (`create_review_item`) | none by itself; callers pass `conn` and check duplicates in the same transaction | caller's `run_write` |
| `review_item` insert (`create_review_item_if_absent`) | (`kind`, match-key values, blocking statuses) | one `run_write` holding lookup and insert |
| `review_item` decision | `status = 'pending'` guard | one `run_write` including the audit line, or the caller's transaction when `conn` is given (R-33) |
| `review_item` payload update | `item_id`; replacing the same keys with the same values is a no-op in effect | one `run_write` or the caller's transaction |

### 4.4 Vectors (LanceDB `data/vectors/`)

| Table | Schema | Writers | Key | Retention |
|---|---|---|---|---|
| `ticket_embedding` | U02-63 | spec 03 (`merge_insert` on `record_id`) | `record_id`; vectors reused by `content_hash` | rows for records gone from `core.*` deleted by spec 03; old versions removed by `purge_history` after deletions |
| `memory_embedding` | U02-63 | spec 07 | `memory_id` | spec 07 maintenance |

### 4.5 In-memory state

| State | Owner | Model |
|---|---|---|
| Per-thread SQLite connections, path override, registry | `herness.store.ops.core` | per-thread; registry lock |
| `CurrentPointer` cache | instance | `threading.Lock` |
| Jinja environment | `herness.model.sqlfiles` | created once, immutable after |
| `LakeWriter` buffer | instance | single thread |

## 5. Control flows

### F02-01 Connector sync writes the lake (caller: spec 01 runner)

| Step | Unit | State change | On failure |
|---|---|---|---|
| 1 | `LakeWriter.__init__` | none | `ConfigError` → job fails (bad config) |
| 2 | `LakeWriter.write` per batch | temp files | `LakeContractError` → runner calls `abort()`, job fails `SchemaViolation`; `StoreBusy` → runner aborts, job retried |
| 3 | `LakeWriter.commit` at each checkpoint | temps renamed | `StoreBusy` → runner retries commit via policy, else `abort()`; renamed files stay; watermark not advanced |
| 4 | runner sets watermark (spec 01) | `watermark` | ops `StoreBusy` retried by `run_write` |

### F02-02 Nightly `build_pipeline` job

| Step | Unit | State change | On failure |
|---|---|---|---|
| 1 | `run_build_pipeline`: validate payload | — | `ConfigError` → job `failed` |
| 2 | resolve build (new, payload, or resumed from `ctx.load_state`) | — | `NotFoundError`/`ConfigError` → job `failed` |
| 3 | `cleanup_builds(mode="pre")` | orphan files deleted | locked files skipped; `StoreBusy` from ops → job retried |
| 4 | `_stage_build`: `open_for_build(create)`, `scan_lake`, `run_sql_range(0,99)`, `insert_build_row`, `register_reference_tables`, `run_sql_range(100,299)`, row counts, `CHECKPOINT` | new file, `meta.build` `building` | `BuildSqlError` → status `failed`, job `failed` (no retry, `SchemaViolation`); kill → file stays `building`/`finished_at` NULL → orphan at next run (FT02-03) |
| 5 | `ctx.save_state` | job `result.state` | ops busy retried |
| 6 | `_stage_enrich`: `run_enrichment(..., stages, llm_factory)` (spec 03, R-48, R-05), which enters `ctx.gpu_scope("decider")` itself for its GPU stages (R-43); then `run_sql_range(300,399)` with no GPU class held | `enrich.*`, `core.incident.content_hash`; GPU class back to its entry value (none) after enrichment | spec 03 errors per spec 08 class rules; the retry resumes at `enrich` (state has `build`); impl 03 `YieldRequested` → handler returns `yield` |
| 7 | `_stage_score`: `materialize_facts`, `run_scoring(build_id, steps, con=con, ctx=ctx)` on the same connection | `metrics.*`, `score.*`, `meta.evidence` | `SchemaViolation` → `failed`; retryable errors → resume at `score` (impl 04 resumes from its checkpoint) |
| 8 | `_stage_dq`: prev row counts, `run_sql_range(900,999)`, counts, `evaluate_gate` | `meta.dq_result`, `row_counts` | `DqGateFailed` → status `failed`, `CURRENT` untouched, CLI exit code 5 (build blocked, R-46) |
| 9 | `_stage_promote` → `promote_build`: fault point, status `promoted`, close, `write_current`, retire previous, `cleanup_builds(post)` | `CURRENT`, old files | kill before step `write_current` → `CURRENT` unchanged, rerun resumes at `promote` and re-runs DQ (FT02-04); `StoreBusy` on `CURRENT` → job retried |
| 10 | metrics and `JobOutcome` | `metric_sample`, `job.result` | metric write failure logged `model.build.metrics_write_failed` (WARNING), outcome still returned |

Yield (`ctx.should_yield()`) is checked before each SQL file and each stage; the handler saves state and returns `yield`; a yield inside `build` leaves an orphan (step 4 rule).

### F02-03 Reader opens the current build

| Step | Unit | On failure |
|---|---|---|
| 1 | `CurrentPointer.get` / `read_current` | no `CURRENT` → caller shows "No promoted build yet" (spec 09); invalid → `SchemaViolation` |
| 2 | `open_readonly(build_id)` | lock or missing file → `StoreBusy` / `NotFoundError`; retried by caller policy `warehouse_read` |
| 3 | every 60 s re-check | a change switches new queries to the new build; old connection closed by the caller |

### F02-04 Ops write

| Step | Unit | On failure |
|---|---|---|
| 1 | owner function builds SQL and params | validation errors raised before any I/O |
| 2 | `run_write(fn, op)`: fault point, `BEGIN IMMEDIATE`, `fn`, `COMMIT` | busy → rollback, `StoreBusy`, retried (6 attempts, 30 s); integrity → `SchemaViolation`, no retry |

### F02-05 Migration at startup (`herness init`, worker start)

| Step | Unit | On failure |
|---|---|---|
| 1 | `migrate()` bootstrap `schema_migration` | busy retried |
| 2 | discover every owner's files, check each number against `MIGRATION_RANGES` (R-11), verify checksums | `MigrationError(out_of_range, duplicate_version, checksum_mismatch, unknown_applied)` → process exits (CLI code 5, doctor FAIL, R-46) |
| 3 | apply each pending file in ascending numeric order, each in its own transaction, including pending files numbered below the highest applied one | `MigrationError(apply_failed)`, earlier migrations stay applied, the failed one rolled back |

### F02-06 Review decision (dashboard, spec 09)

| Step | Unit | On failure |
|---|---|---|
| 1 | role check (spec 09) | `PermissionDenied` |
| 2 | kind `memory_write`: impl 07 `MemoryStore.approve` or `MemoryStore.reject` runs its memory update and calls `decide_review_item(..., conn=conn)` in one transaction (R-33). Other kinds: `decide_review_item` in its own transaction with audit | `NotFoundError` (CLI exit 7), `ReviewItemConflict` (UI shows "already decided"), audit failure → nothing committed; a `memory_write` item decided without `conn` (other than the system rejection of a purge, R-54) → `ConfigError` |
| 3 | owner acts on approved items at its next step (for `mapping_suggestion`: next build via U02-60) | — |

### F02-07 Privacy deletion (driven by spec 10; the parts this spec provides)

| Step | Unit | On failure |
|---|---|---|
| 1 | spec 10 marks request `running` | — |
| 2 | `purge_record_ids` (lake pass 1) | `StoreBusy` → spec 10 job retries the step |
| 3 | spec 03 `purge_record` → `VectorStore.delete_ids` + `purge_history` | `StoreBusy` → retry |
| 3a | impl 07 `MemoryStore.purge(record_id)` (R-54): memory items, memory vectors, FTS rows; blanks `memory_write` payload content through `update_review_payload` (U02-132) | `StoreBusy` → retry |
| 4 | next build: `stg.deleted_record` excludes the record, `300_attach_decisions.sql` prunes `enrich` rows | build failure per F02-02 |
| 5 | `cleanup_builds(mode="post", keep_last=1)` after promotion | deferred files retried at the next run |
| 6 | `purge_record_ids` (lake pass 2) | as step 2 |

## 6. Error handling

CLI exit codes follow R-46 (corrected), which is design 09 §5.8; the CLI (impl 09) maps the class that reaches it. For this spec's failures: `ConfigError` → 3; `SchemaViolation` and its subclasses (`LakeContractError`, `MigrationError`, `BuildSqlError`, `DqGateFailed`) → 5 (build blocked); `NotFound` and its subclass `NotFoundError` → 7; `StoreBusy` left after retries → 8; `PermissionDenied` → 11; any other `HernessError` (for example `LakeStateError`, `ReviewItemConflict`) → 1; `herness doctor` FAIL → 1; usage errors → 2 (Typer only). A failed job is reported by the CLI's job follower with the code of its recorded error class. The "Exit" column below gives the code.

| Failure condition | Class raised | Caught where | Retry / fallback | User-visible effect | Log event |
|---|---|---|---|---|---|
| Invalid source/entity name | `ConfigError` | connector job top level | none | job failed, exit 3 | `jobs.job_failed` (spec 08) |
| Batch violates lake contract | `LakeContractError` | spec 01 runner (aborts writer) | none | sync job failed, exit 5 | `store.lake.contract_violation` (ERROR) |
| Lake file locked on create/rename | `StoreBusy` | spec 01 runner / spec 08 | job retry policy | none unless exhausted (exit 8) | `store.lake.busy` (WARNING) |
| Writer used after commit/abort | `LakeStateError` | none (programming error) | none | job failed | `jobs.job_failed` |
| Purge hits locked file | `StoreBusy` | spec 10 deletion job | job retry | deletion stays `running` | `store.lake.busy` |
| `CURRENT` invalid content | `SchemaViolation` | reader's page wrapper / CLI | none | "No promoted build" message with fix `herness pipeline` | `store.warehouse.current_invalid` (ERROR) |
| `CURRENT` points to missing file | `NotFoundError` | same | none | exit 7 | `store.warehouse.current_missing_file` (ERROR) |
| Warehouse lock on open | `StoreBusy` | caller policy `warehouse_read` | 3 attempts | exit 8 when exhausted | `store.warehouse.busy` (WARNING) |
| Retired file still open on delete | none (returns `deferred`) | `cleanup_builds` | next pipeline run | none | `store.warehouse.delete_deferred` (WARNING) |
| SQLite busy/locked | `StoreBusy` | `run_write` via `retry_call("sqlite_write")` | 6 attempts, 30 s | exit 8 when exhausted | `resilience.retry` (spec 08), `store.ops.busy_exhausted` (ERROR) |
| SQLite constraint failure | `SchemaViolation` | owner / job top level | none | operation fails | `store.ops.constraint_failed` (ERROR, `op`) |
| Nested `run_write` | `ConfigError` | none (programming error) | none | — | — |
| JSON too large / not serialisable | `SchemaViolation` | owner | none | operation fails | `store.ops.json_rejected` (WARNING, `field`) |
| Migration checksum mismatch / out of range / duplicate / unknown | `MigrationError` | CLI entry, worker start | none | exit 5, doctor FAIL with fix "restore the migration file or upgrade the code" | `store.ops.migration_failed` (CRITICAL) |
| Migration statement fails | `MigrationError(apply_failed)` | same | none (rolled back) | exit 5 | `store.ops.migration_failed` |
| SQLite too old / no FTS5 | `ConfigError` | CLI entry | none | exit 3; `herness doctor` FAIL (exit 1) | `store.ops.sqlite_unsupported` (CRITICAL) |
| Review item missing | `NotFoundError` | spec 09 | none | "not found", exit 7 | — |
| Review item already decided | `ReviewItemConflict` | spec 09 | none | "already decided", exit 1 | `store.ops.review_conflict` (INFO) |
| `memory_write` item decided outside `MemoryStore.approve` / `reject` (R-33), other than the purge's system rejection (R-54) | `ConfigError` | none (programming error) | none | decision not saved | — |
| Invalid review-item arguments (match key, group key, payload field name, limit, cursor) | `ConfigError` | caller (impl 03, 07, 09) | none | operation fails | — |
| Migration file number outside every owner range, or duplicate | `MigrationError(out_of_range / duplicate_version)` | CLI entry, worker start | none | exit 5, doctor FAIL with fix "rename the migration into its owner's range" | `store.ops.migration_failed` (CRITICAL) |
| Impl 03 yield inside enrichment | `YieldRequested` (impl 03 U03-152), caught in U02-100 | `_stage_enrich` | resume at `enrich` next run | none | `model.build.stage_done` not emitted for `enrich` |
| Audit write fails | error from `audit` (spec 10: `FatalError` after 3 `StoreBusy`) | spec 09 | none | decision not saved | spec 10 events |
| Vector filter id invalid | `ConfigError` | spec 03/07 | none | step fails | `store.vectors.invalid_id` (ERROR, count) |
| LanceDB commit conflict | `StoreBusy` | spec 03/07 policy | per caller | — | `store.vectors.busy` (WARNING) |
| Vector schema mismatch | `SchemaViolation` | job top level | none | job failed | `store.vectors.schema_mismatch` (ERROR) |
| Invalid payload | `ConfigError` | job worker | none | exit 3 | `model.build.payload_invalid` (ERROR) |
| Build SQL file fails | `BuildSqlError` | job worker | none; status `failed` | exit 5 | `model.build.failed` (ERROR) |
| Template render failure | `ConfigError` | job worker | none | exit 3 | `model.build.render_failed` (ERROR) |
| Unreadable lake file | `SchemaViolation` | job worker | none | exit 5 | `model.build.failed` |
| DQ error check fails | `DqGateFailed` | job worker | none | exit 5; dashboard shows failed checks | `model.dq.check_failed` (ERROR), `model.build.failed` |
| Kill mid-stage | none | next run | resume or orphan cleanup | `CURRENT` unchanged | `model.build.orphan_deleted` (INFO) at next run |
| Spec 03/04 hook error | per their taxonomy | job worker | spec 08 class rules; resume at that stage | per class | `model.build.failed` |

## 7. Security

### 7.1 Trust boundaries touched

| Boundary | How this component touches it |
|---|---|
| TB1 | Lake stores source record content as delivered; staging parses it with `TRY_*` functions only |
| TB2 | Files connector output lands through `LakeWriter` (same contract) |
| TB3 | Raw ticket text is stored in `core.*` text columns; never copied to `stg` flags, `meta`, logs or errors |
| TB4 | The ops store and vectors hold model output written by other specs; this spec provides size caps and parameterised access |
| TB7 | Review decisions from the dashboard are recorded and audited here |
| TB9 | DuckDB extensions: auto-install and auto-load disabled on every connection; no extension is used |
| TB10 | `mappings.yaml`, `sources.yaml` values reach SQL; `CURRENT` and data files are local operator-controlled state |

### 7.2 STRIDE threat table

| ID | Boundary | STRIDE | Threat | Likelihood | Impact | Control | Reference | Test |
|---|---|---|---|---|---|---|---|---|
| TH02-01 | TB10, TB1 | T, E | Source or entity name containing `..`, separators or a drive letter writes or deletes outside the lake | Low | High | `validate_name` allowlist; containment checks in `partition_dir` and purge | ASVS v5.0.0-V5 (file handling); ASVS v5.0.0-V2.2 | ST02-01 |
| TH02-02 | TB1, TB2 | T | Build reads a half-written lake file | Medium | Medium | temp names with leading dot; glob `[!.]*.parquet`; rename on commit | ASVS v5.0.0-V5 | ST02-02 |
| TH02-03 | TB10 | T, E | `CURRENT` edited to point at an arbitrary path or a crafted DuckDB file | Low | High | `BUILD_ID_RE` gate; path derived only from the ID; folder ACLs (spec 10 §5.5) | ASVS v5.0.0-V5; ASVS v5.0.0-V13 | ST02-03 |
| TH02-04 | TB4 | I, E | A reader connection used for agent SQL reads files, attaches databases or re-enables settings | Medium | High | `open_readonly`: `read_only`, `enable_external_access = false`, `lock_configuration = true`, extensions off | ASVS v5.0.0-V1.2; LLM05 | ST02-04 |
| TH02-05 | TB4, TB7 | T, E | A non-build process opens a warehouse writable | Low | High | `_warehouse_rw` import contract; only the exclusive job calls it | ASVS v5.0.0-V15 | ST02-05 |
| TH02-06 | TB7 | R | A review decision is recorded without an audit line | Low | Medium | audit inside the decision transaction; failure rolls back | ASVS v5.0.0-V16 | ST02-06 |
| TH02-07 | TB4, TB7 | D | Oversized JSON or payload bloats the ops store | Medium | Medium | `dump_json` byte cap; `CHECK length(...)` on `payload`, `note`, `labels`; `read_all` row cap | ASVS v5.0.0-V2.2; LLM10 | ST02-07 |
| TH02-08 | TB4 | T | A crafted ID injects into a LanceDB filter string | Low | High | ID pattern excludes quotes; column allowlist | ASVS v5.0.0-V1.2; LLM08 | ST02-08 |
| TH02-09 | TB3 | I | Deleted records' vectors stay readable in old LanceDB versions | Medium | High | `purge_history` after deletions | ASVS v5.0.0-V14; LLM08 | ST02-09 |
| TH02-10 | TB10 | T, E | A config value (custom field, enum, alias) injects SQL into build files | Low | High | identifier allowlist and quoting; values only as Arrow tables; `num` for numbers | ASVS v5.0.0-V1.2 | ST02-10 |
| TH02-11 | TB10 | E | Template code escapes the Jinja sandbox | Low | High | `SandboxedEnvironment`, `StrictUndefined`, templates only from the package directory | ASVS v5.0.0-V15 | ST02-11 |
| TH02-12 | TB9 | T, E | DuckDB downloads and loads an extension at build or query time | Low | High | auto-install and auto-load off on every connection; no `INSTALL`/`LOAD` in SQL; ops rows registered from Arrow instead of the `sqlite` extension | ASVS v5.0.0-V15; LLM03 | ST02-12 |
| TH02-13 | TB3 | I | Raw ticket text reaches agents or UI via `stg` or unlisted `core` text columns | Medium | High | `stg` holds no `_payload`; every raw-text `core` column is in the spec 05 blocked list; spec 05 guard denies `stg` | ASVS v5.0.0-V14; LLM02 | ST02-13 |
| TH02-14 | TB3 | I | Row values or ticket text appear in logs or error messages | Medium | Medium | messages carry IDs and counts only; DuckDB errors sanitised (`'?'` for literals) | ASVS v5.0.0-V16; LLM02 | ST02-14 |
| TH02-15 | TB10 | T | An applied migration file is edited, silently diverging schemas | Low | Medium | SHA-256 checksum per applied migration; mismatch stops startup | ASVS v5.0.0-V15 | ST02-15 |
| TH02-16 | TB1, TB3 | I | A record under a deletion request reappears in a later build | Medium | High | `stg.deleted_record` filter in `latest`; `300` prunes `enrich`; lake purge passes | ASVS v5.0.0-V14 | ST02-16 |
| TH02-17 | TB1 | T | A build with failing `error` checks is promoted (for example via `--from-stage promote`) | Low | High | DQ always re-run before promotion; gate requires ≥ 1 check and no failed errors | ASVS v5.0.0-V2 (business logic) | ST02-17 |
| TH02-18 | TB4, TB7 | D | A long transaction or lock holder stalls every writer | Medium | Medium | WAL, `busy_timeout = 10000`, `BEGIN IMMEDIATE`, retry policy, slow-write warning, callbacks forbidden from I/O | ASVS v5.0.0-V2 | ST02-18 |

### 7.3 ASVS mapping

| ASVS 5.0.0 section | Control here | Units |
|---|---|---|
| V1.2 (injection prevention) | parameterised SQLite and DuckDB statements; allowlisted, quoted identifiers; Arrow-registered values; LanceDB ID pattern | U02-38…U02-40, U02-58, U02-86, U02-87, U02-67, U02-130, U02-131 |
| V2.2 (input validation) | pydantic models with `forbid`/`strict`; lake contract checks; size caps | U02-19, U02-71…U02-75, U02-96, U02-41 |
| V5 (file handling) | path containment, temp-then-rename, no execution of lake content | U02-09, U02-10, U02-15, U02-16, U02-21, U02-23 |
| V13 (configuration) | secure defaults (read-only connections, extensions off) | U02-29, U02-34 |
| V14 (data protection) | raw text segregation, deletion propagation, vector history purge, review payload erasure | U02-68, U02-124, U02-106, U02-132 |
| V15 (secure coding and architecture) | layering contracts, sandboxed templates, migration integrity, safe deserialisation (JSON only, no pickle) | U02-45, U02-85 |
| V16 (security logging) | review decisions audited, also inside a caller's transaction (R-33); no sensitive data in logs | U02-59, U02-76 |

### 7.4 LLM Top 10 and AI RMF

| Item | Control here |
|---|---|
| LLM02 | Raw text only in `core.*` blocked columns; `stg` without `_payload`; logs without text (TH02-13, TH02-14) |
| LLM03 | No runtime extension download (TH02-12) |
| LLM04 | `core.service_map` uses only human-approved `mapping_suggestion` items (U02-60, U02-117) |
| LLM05 | Hardened read-only warehouse connection for agent SQL (TH02-04) |
| LLM08 | Vector deletes by validated ID; history purge (TH02-08, TH02-09) |
| LLM10 | Size caps on JSON and reads (TH02-07) |
| AI RMF | Not required for spec 02 (ENG §5.4 lists 03, 05, 06, 07, 11). The DQ gate supports the Manage function by blocking promotion. |

### 7.5 Secrets

None. This component reads no `secret:` reference. The audit call (spec 10) handles its own key material.

### 7.6 Data classification

| Data | Classification |
|---|---|
| Lake `_payload` and flattened fields; `core.*` text columns (`short_description`, `description`, `close_notes`, `root_cause_text`, `summary`) | personal (may contain names and contact data) |
| `core.*` identifiers, timestamps, enums, counts; `meta.build`; `meta.dq_result` | internal |
| `enrich.text_redacted.text` | confidential (redacted, pseudonymised) |
| Money columns (`estimate_cost_usd`) | confidential |
| `review_item.payload` | internal (no text per spec 03 §4.6; spec 07 `memory_write` holds redacted content → confidential) |
| `review_item.decided_by`, `chat_session.user_ref` | personal (pseudonymous HMAC) |
| `chat_message.content`, `memory_item.content` | confidential (redacted) |
| `deletion_request.record_id` | internal |
| Vectors | confidential (derived from redacted text) |
| `metric_sample`, `schema_migration` | internal |
| Logs and metrics from this component | internal |

### 7.7 Accepted residual risks

| Risk | Reason | Owner |
|---|---|---|
| Raw personal text persists in the lake and `core.*` for the retention period | Needed for re-redaction after key rotation and rebuilds; protected by ACLs and BitLocker (spec 10 §5.5); deletion requests remove it | spec 10 owner |
| A crash between lake renames leaves a partially visible file set | Staging dedupe makes the duplicate re-fetch harmless | spec 01/02 owners |
| `retired` status may be missing in an old build that a reader held open | Readers treat "promoted and not current" the same as retired | spec 02 owner |
| SQLite `CHECK` lists require table rebuilds to extend enums | Integrity is worth the migration cost | spec 02 owner |
| An audit line for a `review_item` decision made inside a caller's transaction (R-33) can outlive a rollback of that transaction | The audit log must be written before commit (an unaudited action never happens); an extra "attempted" line is the safer failure | spec 02 and spec 07 owners |

## 8. Observability

### 8.1 Log events

| Event | Level | Fields | When |
|---|---|---|---|
| `store.lake.file_rotated` | DEBUG | `source`, `entity`, `dt`, `rows`, `bytes`, `reason` | a lake file closes |
| `store.lake.committed` | INFO | `source`, `entity`, `files`, `rows`, `bytes`, `duration_ms` | commit returns |
| `store.lake.contract_violation` | ERROR | `source`, `entity`, `rule`, `column`, `bad_rows` | `LakeContractError` raised |
| `store.lake.busy` | WARNING | `source`, `entity`, `op` | sharing violation mapped to `StoreBusy` |
| `store.lake.abort_leftover` | WARNING | `source`, `entity`, `count` | abort could not delete temps |
| `store.lake.uncommitted_exit` | WARNING | `source`, `entity` | context exit without commit |
| `store.lake.purged` | INFO | `ids`, `files_scanned`, `files_rewritten`, `files_deleted`, `rows_removed` | purge done |
| `store.lake.partition_unparsable` | WARNING | `path` (relative) | retention scan |
| `store.lake.retention_applied` | INFO | four counts | retention done |
| `store.warehouse.current_changed` | INFO | `old`, `new` | pointer change seen by a reader |
| `store.warehouse.current_switched` | INFO | `previous`, `build_id` | promotion wrote `CURRENT` |
| `store.warehouse.current_invalid` | ERROR | — | invalid pointer content |
| `store.warehouse.current_missing_file` | ERROR | `build_id` | pointer to a missing file |
| `store.warehouse.busy` | WARNING | `build_id` | lock on open |
| `store.warehouse.opened_for_build` | INFO | `build_id`, `create` | writable open |
| `store.warehouse.deleted` | INFO | `build_id`, `bytes` | file deleted |
| `store.warehouse.delete_deferred` | WARNING | `build_id` | file locked |
| `store.warehouse.foreign_file` | WARNING | `name` | unexpected file in the warehouse dir |
| `store.ops.connected` | DEBUG | `thread` | new connection |
| `store.ops.slow_write` | WARNING | `op`, `duration_ms` | transaction > 1 s |
| `store.ops.busy_exhausted` | ERROR | `op` | retries exhausted |
| `store.ops.constraint_failed` | ERROR | `op`, `constraint` | integrity error |
| `store.ops.json_rejected` | WARNING | `field`, `reason` | JSON cap or type |
| `store.ops.migrated` | INFO | `version`, `name`, `owner`, `duration_ms` | migration applied |
| `store.ops.migration_out_of_order` | INFO | `version`, `name`, `highest_applied` | a pending owner migration numbered below the highest applied one is applied (R-11) |
| `store.ops.migration_failed` | CRITICAL | `version`, `name`, `reason` | `MigrationError` |
| `store.ops.sqlite_unsupported` | CRITICAL | `sqlite_version` | version or FTS5 check fails |
| `store.ops.review_item_created` | INFO | `item_id`, `kind` | insert |
| `store.ops.review_item_exists` | DEBUG | `item_id`, `kind` | `create_review_item_if_absent` found a blocking item |
| `store.ops.review_payload_updated` | INFO | `item_id`, `keys` | payload fields replaced |
| `store.ops.review_item_decided` | INFO | `item_id`, `kind`, `status` | decision |
| `store.ops.review_conflict` | INFO | `item_id`, `status` | conflict |
| `store.ops.audit_orphan` | ERROR | `item_id` | commit failed after audit |
| `store.vectors.table_created` | INFO | `table` | creation |
| `store.vectors.deleted` | INFO | `table`, `rows` | delete by IDs |
| `store.vectors.history_purged` | INFO | `table` | purge |
| `store.vectors.invalid_id` | ERROR | `count` | bad IDs rejected |
| `store.vectors.busy` | WARNING | `table` | commit conflict |
| `store.vectors.schema_mismatch` | ERROR | `table`, `field` | schema check |
| `model.build.payload_invalid` | ERROR | `job_id` | bad payload |
| `model.build.lake_scanned` | INFO | `build_id`, `entities`, `files`, `bytes` | scan done |
| `model.build.refdata_registered` | INFO | `build_id`, table counts | reference tables |
| `model.build.mapping_skipped` | WARNING | `count` | invalid approved suggestions |
| `model.build.sql_file_done` | INFO | `build_id`, `file`, `statements`, `duration_ms` | each file |
| `model.build.render_failed` | ERROR | `build_id`, `file`, `line` | template error |
| `model.build.stage_done` | INFO | `build_id`, `stage`, `duration_ms` | each stage |
| `model.build.failed` | ERROR | `build_id`, `stage`, `error_class` | stage error |
| `model.build.mark_failed_error` | ERROR | `build_id`, `error_class` | could not mark failed |
| `model.build.git_sha_unknown` | WARNING | — | no SHA |
| `model.build.promoted` | INFO | `build_id`, `previous` | promotion |
| `model.build.retire_deferred` | INFO | `build_id` | previous build locked |
| `model.build.orphan_deleted` | INFO | `build_id`, `status` | pre-cleanup |
| `model.build.cleanup` | INFO | `deleted`, `deferred`, `retired` | cleanup done |
| `model.build.metrics_write_failed` | WARNING | `error_class` | metric rows not written |
| `model.dq.evaluated` | INFO | `build_id`, `checks`, `failed_errors`, `failed_warnings` | gate |
| `model.dq.check_failed` | WARNING (warn) / ERROR (error) | `build_id`, `check_name`, `severity` | each failed check |

All events carry `component` (`store` or `model`) and `job_id`/`build_id` when in scope.

### 8.2 Metrics (`metric_sample`, written once per build job through `T08-05 (herness.store.ops.metrics.record_metric_samples)`, R-12)

| Name | Kind | Labels | Meaning |
|---|---|---|---|
| `herness_model_build_total` | counter | `status` (`promoted`, `failed`, `unpromoted`, `yield`) | build job outcomes |
| `herness_model_build_stage_seconds` | histogram | `stage` | stage duration |
| `herness_model_build_sql_file_seconds` | histogram | `file` (≤ 30 values) | SQL file duration |
| `herness_model_build_rows_total` | gauge | `table` (`core.*` names) | rows after build |
| `herness_model_dq_failed_checks_total` | counter | `severity` | failed checks per build |
| `herness_model_warehouse_bytes` | gauge | — | size of the new build file |
| `herness_store_warehouse_delete_deferred_total` | counter | — | deferred deletions in cleanup |

The lake writer and ops core write no metric rows: the lake writer also runs in processes without an ops store (spec 11 generator), and ops writes would recurse. Their counts are emitted by callers (spec 01 records rows and files per sync; spec 08 records `sqlite_write` retries).

### 8.3 Trace events

Not applicable: no unit in this spec runs inside an agent run, and spec 05's `Tracer` is not passed to store or build code.

### 8.4 Health checks

| Check | Unit | ok / degraded / down |
|---|---|---|
| Ops store | `ops_health` (U02-48) | per U02-48 |
| Warehouse | `warehouse_health` (U02-33) | per U02-33; stale after 48 h |
| Vectors | `VectorStore.health` (U02-70) | per U02-70 |

`herness doctor` is `T09-22 (herness._cli.doctor.run_doctor)`, which appends impl 10's host checks from `T10-27 (herness.admin.doctor_host.doctor_checks)`. Its own `ops_migrations` and `current_build` rows read `pending_migrations` (U02-47) and `CURRENT` directly; it does not yet call these three health functions (C14, §13.3).

## 9. Configuration

| Key path | Type | Default | Validation | Restart needed | Sensitivity |
|---|---|---|---|---|---|
| `herness.yaml: paths.data` (owner 10) | path | `data` | resolved once per process by `data_layout` | yes | internal |
| `profile` (owner 10) | literal | `local` | — | yes | internal |
| `sources.yaml: build.keep_last` (section `build` = `BuildSettings`; section `dq` = `DqSettings`; both top-level sections owned by `herness.model.settings`, composed by impl 10, R-03) | int | 3 | 1–20 | no (read at job start) | internal |
| `sources.yaml: build.memory_limit` | str | `75%` | U02-75 pattern | no | internal |
| `sources.yaml: build.threads` | int \| null | null (= CPU count) | 1–256 | no | internal |
| `sources.yaml: build.service_ci_classes` (DD02-06) | list[str] | `cmdb_ci_service`, `cmdb_ci_service_business`, `cmdb_ci_service_technical` | 1–50 names, pattern U02-75 | no | internal |
| `sources.yaml: dq.row_count_drop_max` | float | 0.05 | 0–1 | no | internal |
| `sources.yaml: dq.incident_service_null_warn` | float | 0.30 | 0–1, ≤ error | no | internal |
| `sources.yaml: dq.incident_service_null_error` | float | 0.60 | 0–1 | no | internal |
| `sources.yaml: dq.work_item_service_null_warn` | float | 0.40 | 0–1 | no | internal |
| `sources.yaml: dq.cast_fail_warn` | float | 0.005 | 0–1 | no | internal |
| `sources.yaml: dq.future_timestamp_max` | int | 0 | ≥ 0 | no | internal |
| `sources.yaml: dq.resolved_before_opened_warn` | float | 0.001 | 0–1 | no | internal |
| `sources.yaml: dq.duplicate_key_max` | int | 0 | ≥ 0 | no | internal |
| `sources.yaml: dq.decision_coverage_min` | float | 0.95 | 0–1 | no | internal |
| `sources.yaml: dq.metric_daily_unmapped_warn` (DD02-07) | float | 0.05 | 0–1 | no | internal |
| `mappings.yaml: enums.<domain>` | map str→str | shipped template below | domain in `ENUM_DOMAINS`; canonical in the domain set | no | internal |
| `mappings.yaml: service_overrides[]` | list[`ServiceOverride`] | `[]` | U02-71 | no | internal |
| `mappings.yaml: custom_fields.servicenow.customer_impact_minutes` | str \| null | null (synth profile: `u_customer_impact_minutes`) | column pattern | no | internal |
| `mappings.yaml: custom_fields.servicenow.acknowledged_at` | str \| null | null (MTTA disabled) | column pattern | no | internal |
| `mappings.yaml: custom_fields.jira.story_points` / `team` / `estimate_cost_usd` / `epic_link` | str \| null | null | column pattern | no | internal |

Shipped enum template in `config/mappings.yaml` (matching is lower-cased, exact):

| Domain | Source value → canonical |
|---|---|
| `servicenow.incident_state` | `1`, `new` → `open`; `2`, `in progress` → `in_progress`; `3`, `on hold` → `on_hold`; `6`, `resolved` → `resolved`; `7`, `closed` → `closed`; `8`, `canceled`, `cancelled` → `canceled` |
| `servicenow.change_type` | `standard`, `normal`, `emergency` → same |
| `servicenow.change_close_code` | `successful` → `successful`; `successful with issues` → `successful_with_issues`; `unsuccessful` → `unsuccessful`; `backed out`, `backed_out` → `backed_out`; `canceled`, `cancelled` → `canceled` |
| `monitoring.severity` | `critical`, `major`, `minor`, `warning`, `info` → same; `error` → `major`; `warn` → `warning`; `informational` → `info` |
| `jira.issue_type` | `initiative`, `epic`, `feature`, `story`, `bug`, `task` → same; `sub-task`, `subtask` → `subtask` |
| `jira.status_category_key` | `new` → `todo`; `indeterminate` → `in_progress`; `done` → `done` |
| `jira.status_category` | `to do`, `open`, `backlog` → `todo`; `in progress`, `in review` → `in_progress`; `done`, `closed`, `resolved` → `done` |

## 10. Performance and capacity

### 10.1 Benchmarks

Hardware for BT02-01…BT02-04: reference PC of design 02 §9 (16 cores, 64 GB RAM, NVMe). Dataset: spec 11 `synth_data.py --seed 42 --scale full` (5M incidents, 0.5M changes, 2M events, 0.2M work items, 3 years `metric_daily`).

| ID | Target (design 02 §9 unless marked) | Measurement | Pass threshold | Marker |
|---|---|---|---|---|
| BT02-01 | Build 000–299 | `build` stage duration from `job.result.durations_ms` | < 600 s | `bench` |
| BT02-02 | DQ checks | `run_sql_range(900, 999)` duration | < 60 s | `bench` |
| BT02-03 | Warehouse size | file size after full pipeline | < 15 GB | `bench` |
| BT02-04 | Typical agent query | 200 `tests/fixtures/sql_ok/` queries on `open_readonly` of the full build | p95 < 2 s | `bench` |
| BT02-05 | Lake write throughput (impl target) | `LakeWriter` 1,000,000 incident-shaped rows in 10,000-row batches, laptop-class CI runner | < 60 s | `bench` |
| BT02-06 | Ops single-row write (impl target, supports spec 08 enqueue p95 < 10 ms) | 1,000 `run_write` inserts into `review_item` | p95 < 10 ms | `bench` |
| BT02-07 | Fresh migration (impl target) | `migrate()` on an empty file | < 2 s | `bench` |

### 10.2 Enforced limits

| Limit | Value | Where |
|---|---|---|
| Lake buffer | 131,072 rows or 64 MiB | U02-13 |
| Lake file size | `target_bytes` 128 MiB (1 MiB–1 GiB) | U02-14 |
| Lake file age | `max_open_s` 600 s | U02-14 |
| Files per entity scanned | 100,000 | U02-81 |
| Purge batch | 10,000 IDs | U02-21 |
| DuckDB build memory / threads | `build.memory_limit`, `build.threads`; spill to `tmp/<build_id>/` | U02-34 |
| Ops JSON value | 64 KiB default | U02-41 |
| Ops read rows | 100,000 default | U02-40 |
| SQLite busy wait | 10 s per attempt, 6 attempts, 30 s total | U02-37, U02-38 |
| Review list page | 5,000 | U02-58 |
| Review match keys / grouped counts | 8 keys; 100,000 groups | U02-130, U02-131 |
| Vector delete chunk | 500 IDs | U02-67 |
| `git rev-parse` timeout | 5 s | U02-89 |

## 11. Test specification

Fixtures (spec 11 layout): `ops_store` (temp `ops.sqlite`, `reset_connections`), `lake_root` (temp raw root), `lake_small` (`T11-16 (tests/fixtures/lake_small)`), `build_harness` (`tests/support/build_harness.py`, added in T02-12: renders and runs a range of build files on a temp DuckDB file with a given inventory and reference data), `fake_job_context` (a test fake of the `T08-03 (herness.core.jobs.JobContext)` protocol with in-memory state, defined in `tests/support/build_harness.py` of T02-12), `frozen_time` (freezegun), `fault_plan` (writes a JSON `HERNESS_FAULTS` plan, honoured only with `HERNESS_ENV=test`, R-40). Record IDs, ULIDs and clocks are fixed.

### 11.1 Unit tests (`unit`)

| ID | Under test | Setup | Action | Expected |
|---|---|---|---|---|
| UT02-01 | U02-15, U02-16 | `lake_root`; batch of 10 rows, `_fetched_at` one day | write, commit | one `part-*.parquet` under `dt=`; `LakeFileSet.rows == 10`; max `_source_updated_at` correct; metadata columns first |
| UT02-02 | U02-19 | batch without `_deleted` | write | `LakeContractError(rule="missing_column")` |
| UT02-03 | U02-19 | `_record_id` not equal to the concatenation | write | `rule="record_id_format"`, `bad_rows` = count |
| UT02-04 | U02-19 | `_source` differs from writer source | write | `rule="source_mismatch"` |
| UT02-05 | U02-19 | non-tombstone row with NULL `_payload`; naive timestamp column | write each | `rule="payload_null"`; `rule="type"` |
| UT02-06 | U02-15 | `target_bytes = 1 MiB`, 5 MiB of rows | write, commit | ≥ 4 files, each ≥ 1 MiB except the last |
| UT02-07 | U02-15 | fake clock; `max_open_s = 10` | write, advance 11 s, write | two files, reason `age` logged |
| UT02-08 | U02-15 | one batch spanning two `_fetched_at` dates | write, commit | two partitions, row counts split correctly |
| UT02-09 | U02-15 | second batch with an extra column | write both, commit | two files, each with one schema |
| UT02-10 | U02-17 | writer with temp files | abort; then write | no temp files remain; `LakeStateError` |
| UT02-11 | U02-16 | no writes | commit | empty `LakeFileSet`, no files, no directories |
| UT02-12 | U02-18 | `with LakeWriter(...)` raising inside | exit | temps deleted; exception propagates |
| UT02-13 | U02-09, U02-10 | names `Incident`, `a/b`, `..`, `c:`, `ok_name` | validate | first four `ConfigError`; last accepted |
| UT02-14 | U02-21 | three files, one containing the ID | purge | one file rewritten, others byte-identical with same mtime; `rows_removed == 1` |
| UT02-15 | U02-21 | file whose only row is the ID | purge | file deleted, `files_deleted == 1` |
| UT02-16 | U02-23 | partitions `dt=2020-01-01`, `dt=2026-01-01`, `dt=bad` | cutoff 2023-01-01 | first deleted, others kept; `skipped_unparsable == 1`; cutoff within 30 days of today → `ConfigError` |
| UT02-17 | U02-24…U02-26 | frozen time | two `new_build_id` in the same second | both match `BUILD_ID_RE`, differ; `build_path` rejects `../x` |
| UT02-18 | U02-27 | no `CURRENT`; valid; file missing | read | `None`; ID; `NotFoundError` |
| UT02-19 | U02-28 | fake clock | `get`, change file, `get` at 30 s and 61 s | old value at 30 s, new at 61 s; `current_changed` logged once |
| UT02-20 | U02-29, U02-34 | tiny build file | open read-only; open for build | read-only: `current_setting('enable_external_access')` false, `lock_configuration` true, extension settings false; build: temp dir set, time zone UTC |
| UT02-21 | U02-31 | building, promoted, corrupt file, file held by a writer in a subprocess | list | statuses `building`, `promoted`, `unreadable`, `locked`; newest first |
| UT02-22 | U02-32 | file open by another handle (Windows) or unlink patched to raise `PermissionError` | delete | returns `deferred`; current build → `ConfigError` |
| UT02-23 | U02-35 | existing build | write_current | content `<id>\n`; no temp left; previous returned |
| UT02-24 | U02-33 | no CURRENT; stale; fresh | health | `down`, `degraded`, `ok` |
| UT02-25 | U02-37 | `ops_store` | connection | PRAGMAs as specified |
| UT02-26 | U02-37 | two threads | connection in each | different objects; same object on repeat in one thread |
| UT02-27 | U02-38…U02-40 | callback inserting then raising | run_write | nothing committed; `read_all` cap raises beyond `max_rows` |
| UT02-28 | U02-38 | second connection holds `BEGIN IMMEDIATE` for 0.5 s with `busy_timeout` 100 ms (test override) | run_write | succeeds after retry; nested run_write → `ConfigError` |
| UT02-29 | U02-41 | Decimal, aware datetime, Path, enum, model, set, NaN, 70 KiB string | dump | first five encoded as specified; set, NaN, oversize → `SchemaViolation` |
| UT02-30 | U02-42 | `"{bad"` | load | `SchemaViolation` naming the field |
| UT02-31 | U02-43 | open connections, new path | reset | old closed; next connection uses new path |
| UT02-32 | U02-45, U02-49…U02-54 | empty file | migrate | table set equals §4.3 list plus `schema_migration`; six rows recorded |
| UT02-33 | U02-45 | migrated DB | migrate again | `applied == ()` |
| UT02-34 | U02-45 | applied DB, migration file edited (temp copy of the package dir) | migrate | `MigrationError(checksum_mismatch)` |
| UT02-35 | U02-45 | extra migration with a failing second statement | migrate | `MigrationError(apply_failed)`; its first statement rolled back; version unchanged |
| UT02-36 | U02-52 | migrated DB | insert, update content, delete `memory_item` | `memory_fts MATCH` follows each change |
| UT02-37 | U02-49…U02-53 | migrated DB | insert bad enum, bad JSON, bad timestamp, duplicate active `idem_key` | each rejected; duplicate allowed after status `done` |
| UT02-38 | U02-51 | two tasks same run, same `dedup_key` | insert | second fails `UNIQUE` |
| UT02-39 | U02-51, U02-53 | task with unknown `run_id`; session with messages | insert; delete session | FK error; messages cascade-deleted |
| UT02-40 | U02-46, U02-47 | versions 0, 3, 6 | query | pending lists and versions correct |
| UT02-41 | U02-48 | ok DB; pending migration; unreadable path | health | `ok`, `degraded`, `down` |
| UT02-42 | U02-49 | STRICT table | insert text into `rows` | rejected |
| UT02-43 | U02-56, U02-55 | `ops_store` | create | `rev_` ID; pending; payload round-trips |
| UT02-44 | U02-59 | pending item, audit capture fixture | approve | row updated; one `review_decision` audit call with `note_len` |
| UT02-45 | U02-57, U02-59 | decided item; unknown ID | decide; get | `ReviewItemConflict`; `NotFoundError` |
| UT02-46 | U02-58 | 5 items mixed | list with filters and paging | order and filters correct; `limit=5001` → `ConfigError`; `status` and `statuses` together → `ConfigError` |
| UT02-47 | U02-60 | approved and pending suggestions | call | only approved, ordered |
| UT02-48 | U02-105 | ops store with runs in `running`, `done`, `partial`, each on its own build; four unprotected promoted builds | `cleanup_builds(mode="post", keep_last=1)` | only the running run's build survives besides `CURRENT` (it is read through impl 06 `select_runs`, R-68) |
| UT02-49 | U02-63…U02-66, U02-69 | temp vectors dir | ensure twice; table; count | tables exist with schemas; idempotent; unknown name → `ConfigError` |
| UT02-50 | U02-67 | 3 rows | delete 2 IDs | returns 2; 1 row left |
| UT02-51 | U02-68 | delete then purge | checkout previous version | deleted rows not retrievable |
| UT02-52 | U02-70 | missing table | health | `degraded` |
| UT02-53 | U02-71…U02-73 | YAML samples | validate | bad domain, bad canonical, conflicting alias, bad custom field name, `delivery` without project → errors |
| UT02-54 | U02-74, U02-75 | samples | validate | warn > error rejected; `memory_limit` `75%` and `48GB` accepted, `abc` rejected |
| UT02-55 | U02-78…U02-81 | lake with incident files, a temp file, a synth path | scan | columns union; temp ignored; absent entity `present=False`; `from_synth` true for synth root |
| UT02-56 | U02-82, U02-83 | temp SQL dir | discover | order; `_macros.jinja` ignored; `510_x.sql` and duplicate numbers → `ConfigError` |
| UT02-57 | U02-84, U02-85 | template using an undefined var | render | `ConfigError` naming the file and line |
| UT02-58 | U02-86 | table of inputs | each filter | outputs per table; invalid inputs → `ConfigError` |
| UT02-59 | U02-87, U02-128 | config with enums, overrides, aliases; deleted IDs; approved items incl. one invalid | register | six tables with expected rows; invalid item skipped and counted |
| UT02-60 | `ts_utc` (U02-108) | table of 20 strings | select | parsed values per table; `31/02/2024`, epoch-ms, empty → NULL |
| UT02-61 | `lead_int` | `1 - Critical`, `2`, `P2-ish`, `9` | select | 1, 2, NULL, NULL (range 1–5) |
| UT02-62 | `to_bool`, `sn_duration_s` | tables | select | per table |
| UT02-63 | `jira_text` | ADF doc, wiki text, NULL | select | joined text; unchanged; NULL |
| UT02-64 | `json_names`, `json_str_list`, `jstr`, `team_value` | tables | select | per table; invalid JSON → NULL |
| UT02-65 | U02-95, U02-96 | payloads | validate | `[build, score]`, `build` with `build_id`, `enrich_stage` without `enrich`, `enrich_stage` `"Bad Stage"` → errors; `[enrich, score, dq, promote]` with ID ok; `[enrich]` with `enrich_stage="link"` ok (R-48) |
| UT02-66 | U02-06, U02-07 | roots `D:/x/data/synth/7-tiny`, `D:/x/data` | layout | child paths; `synth_marker` true, false |
| UT02-67 | U02-89 | env SHA; git present; git missing (PATH empty) | call | 12 chars; 12 chars; `unknown` |
| UT02-68 | U02-62 | package import | inspect `__all__` and the block headers | no duplicates; every name resolves to the area named in its block; no `review_item` function outside `shared` (R-08); `herness.store.ops.run_write is herness.store.ops.core.run_write`; the package attribute `migrate` is the function and `from herness.store.ops.migrate import pending_migrations` still works; no other re-exported name equals an area name; import opens no connection |
| UT02-69 | U02-88 | profile `synth`/`local`, inventory flags | call | `synthetic` when either condition holds, else `real` |
| UT02-70 | U02-45, U02-129, U02-05 | temp migrations dir with 001–006 plus `065_x.sql`; then plus two files numbered `012` | migrate | `MigrationError(out_of_range)` for 065; `MigrationError(duplicate_version)` for 012; nothing applied in either case |
| UT02-71 | U02-45, U02-46, U02-47 | temp migrations dir 001–006 and `070_a.sql`; migrate; then add `012_b.sql` and `071_c.sql` | `pending_migrations`, migrate | pending lists `012_b`, `071_c` in that order; both applied in numeric order; `store.ops.migration_out_of_order` logged for 012; `schema_version() == 71` |
| UT02-72 | U02-130 | `ops_store`; mapping suggestion payload | create twice with the same match values; decide the first `rejected`; create again with blocking (`pending`, `rejected`) and again with blocking (`pending`) | second call returns (first ID, `False`); third returns (first ID, `False`); fourth creates a new item; a match key `a.b` or one missing from the payload → `ConfigError`; two threads racing create one item |
| UT02-73 | U02-131 | 3 pending `label_check` items for question `q1`, 1 for `q2`, 1 approved | count ungrouped; grouped by `question`; group key `x'` | `{"": 4}`; `{"q1": 3, "q2": 1}`; `ConfigError` |
| UT02-74 | U02-132 | pending and approved `memory_write` items | replace `content` with `""` on both; unknown ID; key `bad-key` | payload `content` blank, other keys and decision fields unchanged; `NotFoundError`; `ConfigError`; log has key names only |
| UT02-75 | U02-58 | 6 decided items with equal and different `decided_at`, 2 pending | page with `statuses=("approved","rejected")`, `decided_after` cursor, `limit=2` until empty | every decided item exactly once in (`decided_at`, `item_id`) order; pending never returned; `offset` with a cursor → `ConfigError`; a plain `datetime` equal to one item's `decided_at` returns only later items (RQ-01) |
| UT02-76 | U02-59 | pending `memory_write` and `label_check` items; audit capture fixture | decide `memory_write` as `approved` by a user without `conn`; decide it with `conn` inside a `run_write` callback that then raises; decide it with `conn` and commit; on a second pending `memory_write` item, decide `rejected` by `system` without `conn` | `ConfigError`, nothing written; after the raising callback the status is still `pending`; after the committing callback the status is `approved` and exactly one audit call was made (R-33); the system rejection succeeds with one audit call (R-54, C15) |
| UT02-77 | U02-133 | existing build file; valid ID without file; `../x` | call | `True`; `False`; `False` without any path built |
| UT02-78 | U02-134, U02-98 | `fake_job_context` whose `job.payload` holds `{"stages": ["build"]}`; recording `run_build_pipeline` stub | build the handler with a fake `llm_factory`, call it with `ctx` | the handler takes one argument; payload read from `ctx.job.payload` (R-42); `llm_factory` reaches `run_build_pipeline` |
| UT02-79 | U02-58 | `label_check` items with payload `question` `q1`/`q2` and `purpose` `spot_check`/`gold` | list with `payload_match={"question": "q1", "purpose": "gold"}`; with a value containing `'` and `%`; with key `a.b`; with 9 keys | only items matching both keys; the quoted value matches literally; `ConfigError` for the bad key and for 9 keys (RQ-02) |

### 11.2 Property tests (`unit`, hypothesis)

| ID | Under test | Property |
|---|---|---|
| PT02-01 | U02-15, U02-16, U02-19 | For any sequence of valid batches (random sizes, dates, rotation limits), the multiset of `_record_id` in committed files equals the input, and every file has one `dt` and one schema |
| PT02-02 | U02-86 | For any string, `sqlstr` output parsed by DuckDB as a literal equals the input (or the input is rejected); `ident` never emits a quote inside the name |

### 11.3 Integration tests (`integration`)

| ID | Under test | Setup | Action | Expected |
|---|---|---|---|---|
| IT02-01 | U02-45 | for each k in 1…5 | apply 1…k, reconnect, apply the rest; compare with fresh | identical normalised `sqlite_schema` dumps |
| IT02-02 | `latest` macro, 110 | incident with three versions and one identical re-emit | build 000–199 | latest version wins; one row |
| IT02-03 | `latest` | latest version tombstone; older tombstone then newer live | build | first dropped; second kept |
| IT02-04 | `latest`, U02-87 | deletion requests `running`, `done`, `pending` | build | first two removed; `pending` kept |
| IT02-05 | `raw`, `latest` | file with unknown column; custom field configured but absent | build | build succeeds; column NULL |
| IT02-06 | U02-11 | dot-prefixed temp file in a partition | build | temp rows not staged |
| IT02-07 | `typed`, `cast_stats` | 3 bad timestamps of 100 | build | `stg.cast_stats` failed 3, non_null 100 |
| IT02-08 | 110 | no `cmn_department`, `task_sla`, `cmdb_ci_service` files | build | empty staging tables; build succeeds |
| IT02-09 | 200 | groups with and without children; then with departments | build | hierarchy mode orgs/teams; department mode by cost center |
| IT02-10 | 110, 200 | CIs of several classes; `busines_criticality` on `cmdb_ci_service` rows and a different value on `cmdb_ci` rows; then no `cmdb_ci_service` files | build | only configured classes; criticality from `cmdb_ci_service` only; without it criticality NULL and the build succeeds (R-60) |
| IT02-11 | 220 | override, CMDB owner, approved and pending suggestions for overlapping keys | build | precedence override > cmdb > suggestion; pending ignored; lookup excludes ambiguous names |
| IT02-12 | 230 | incidents with and without task_sla, custom fields present/absent | build | `sla_breached`, `customer_impact_minutes`, `acknowledged_at` per rules |
| IT02-13 | 230 | incident without service, CI related to one service; CI related to two | build | first gets the service; second NULL |
| IT02-14 | 240 | close codes incl. unknown; canceled state | build | outcome mapped; unknown NULL and counted; canceled |
| IT02-15 | 250 | problem rows | build | columns per U02-120 |
| IT02-16 | 260 | events with alias, unique name, ambiguous name, `incident_ref` | build | service and incident resolution per U02-121; duration rule |
| IT02-17 | 270 | duplicate keys, unmapped service | build | one row per key (latest); unmapped counted in `stg.build_counts` |
| IT02-18 | 120, 280 | Cloud-style parent, DC epic link, components, custom fields | build | `core.work_item` per rules |
| IT02-19 | 120, 280 | changelog array and object forms | build | transitions with categories |
| IT02-20 | 120, 280 | issuelinks inward/outward; remotelinks with INC numbers | build | link rows incl. `mentions_incident` |
| IT02-21 | F02-02, U02-99 | `lake_small`; fake enrichment/scoring | pipeline `[build]` | row counts equal `tests/fixtures/golden/lake_small_counts.json`; `core.*` snapshot equals `tests/fixtures/golden/lake_small_core.json` (sorted rows, text columns hashed); `meta.build` fields set |
| IT02-22 | U02-97, U02-76 | SQL dir copy with a broken file 230 | pipeline | `BuildSqlError` naming `230_incident.sql`; status `failed`; `CURRENT` unchanged; message has no literal values |
| IT02-23 | 300 | enrich rows for a live and a deleted record | stage enrich (fake `run_enrichment` writing rows) | `content_hash` set; deleted record's rows gone |
| IT02-24 | 310 | member of a missing cluster | stage enrich | member deleted; count recorded |
| IT02-25 | U02-100 | CURRENT exists; `fake_job_context` recording GPU requests; payload `enrich_stage="link"`; a fake `llm_factory` | stage enrich with a recording fake `run_enrichment`; then a fake that raises `YieldRequested` | called with (con, build_id, depth, ctx, prev path, `stages=["link"]`, the same `llm_factory`); U02-100 itself makes no GPU request (the recording context sees none from the handler, R-43) and the class during `run_sql_range(300, 399)` is `none`; on the yield signal the handler returns `yield` and `enrich` is not in `stages_done` |
| IT02-26 | U02-101 | fakes for 04 hooks | stage score | `materialize_facts` then `run_scoring(build_id, steps=..., con=<the build connection>, ctx=ctx)`; runner did not render 400 files; the build connection stays open and is never reopened |
| IT02-27 | U02-97, U02-98 | `fake_job_context` requesting yield after 3 SQL files; then no yield | run twice | first `yield`, no `build` in state → second run creates a new build and deletes the orphan |
| IT02-28 | U02-127, U02-94 | crafted core tables per check | stage dq | each row's value, threshold, severity, passed as in U02-127 |
| IT02-29 | F02-02 | previous promoted build; new lake with 10 % fewer incidents | pipeline to promote | `DqGateFailed` with `row_count_drop:core.incident`; status `failed`; `CURRENT` unchanged |
| IT02-30 | U02-94 | warn failures only | pipeline | promoted; warnings logged |
| IT02-31 | `T11-19 (tests/integration/test_build_dirty.py)` | synthetic `small`, dirty `default` | pipeline | DQ counts within ±1 of truth; promoted |
| IT02-32 | U02-104, U02-105 | 5 promoted builds, one pinned by a running run | promote a new build with `keep_last=3` | CURRENT new; newest 2 others kept plus the pinned one; rest deleted; previous marked `retired` |
| IT02-33 | 140 | configured `files/service_costs` entity | build | `stg.files_service_costs` with latest rows and no `_payload` |

### 11.4 Fault tests (`fault`)

| ID | Under test | Setup | Action | Expected |
|---|---|---|---|---|
| FT02-01 | U02-21 | `os.replace` patched to raise `PermissionError` | purge | `StoreBusy`; original file intact; no temp left |
| FT02-02 | U02-38 | plan `sqlite.write error:StoreBusy count=4`; then `count=7` | write | first succeeds; second raises `StoreBusy` after 6 attempts |
| FT02-03 | F02-02 | plan `build.mid_sql action: kill nth: 5` in a subprocess job | run, then run again | `CURRENT` unchanged after kill; next run deletes the orphan and promotes |
| FT02-04 | F02-02 | plan `pipeline.before_promote kill` | run, retry | `CURRENT` unchanged after kill; retry resumes at `promote`, re-runs DQ, promotes (spec 08 F8) |
| FT02-05 | U02-105 | old build held open by a reader process | promote | deletion `deferred`; next run deletes it after the reader closes |
| FT02-06 | U02-16 | kill a writer subprocess between renames | rerun sync into the same entity; build | temps ignored; duplicates collapse; row counts equal a clean run |

### 11.5 Security tests (`unit` or `integration` as marked)

| ID | Threat | Attack | Expected | Marker |
|---|---|---|---|---|
| ST02-01 | TH02-01 | `LakeWriter("..", "x")`, `("a", "b/../../c")`, `("C:", "x")` | `ConfigError`; nothing created outside `lake_root` | unit |
| ST02-02 | TH02-02 | open writer with flushed rows; DuckDB `read_parquet(lake_glob(...))` | zero files matched before commit | integration |
| ST02-03 | TH02-03 | `CURRENT` = `..\..\evil`, a valid ID with trailing path, 10 KB junk | `SchemaViolation`; no file opened | unit |
| ST02-04 | TH02-04 | on `open_readonly`: `COPY`, `ATTACH`, `read_csv('ops.sqlite')`, `SET enable_external_access=true`, `INSTALL httpfs` | every statement fails | integration |
| ST02-05 | TH02-05 | AST scan of `herness/` and `app/` imports; `lint-imports` | only `herness.model.build` and `herness.model.promote` import `_warehouse_rw` | unit |
| ST02-06 | TH02-06 | audit function patched to raise | decide | status still `pending` |
| ST02-07 | TH02-07 | 70 KiB payload; 3,000-char note | create / decide | `SchemaViolation` / `ConfigError`; SQLite CHECK also rejects a direct oversize insert |
| ST02-08 | TH02-08 | `delete_ids` with `x' OR '1'='1` | `ConfigError`; row count unchanged | unit |
| ST02-09 | TH02-09 | delete, purge, then open every remaining version | deleted vector absent in all | integration |
| ST02-10 | TH02-10 | custom field `x" ; DROP TABLE core.incident; --`; enum value with quotes | config rejects the field; enum value stored as data and matched literally | integration |
| ST02-11 | TH02-11 | template with `{{ ''.__class__.__mro__ }}` | `ConfigError` (SecurityError) | unit |
| ST02-12 | TH02-12 | run the `lake_small` build with the spec 10 socket guard in strict mode and a recording audit hook | no network attempt; `autoinstall_known_extensions`/`autoload_known_extensions` false on every connection | integration |
| ST02-13 | TH02-13 | inspect built warehouse | no `stg` table has `_payload`; every `core` VARCHAR column classified as raw text in §7.6 is in `T05-04 (config/models.yaml harness.sql.blocked_columns)` default | integration |
| ST02-14 | TH02-14 | build `lake_small` with sentinel strings in descriptions and a failing cast literal; capture logs and error text | no sentinel or literal in logs, `meta.*` or `BuildSqlError` | integration |
| ST02-15 | TH02-15 | edit an applied migration | startup refuses with `MigrationError` | unit |
| ST02-16 | TH02-16 | deletion request `done` for a record with lake rows, enrich rows (from previous build) and a vector | next build | record absent from `stg`, `core`, `enrich` | integration |
| ST02-17 | TH02-17 | build with a failing `duplicate_key` check; run `--from-stage promote` | DQ re-runs and blocks; `CURRENT` unchanged; empty `meta.dq_result` also blocks | integration |
| ST02-18 | TH02-18 | a thread holds a read transaction for 5 s; another holds `BEGIN IMMEDIATE` for 12 s | writes during the read succeed (WAL); the writer blocked by the long writer gets `StoreBusy`, never hangs past policy 30 s. Note (controller ruling, T02-04): the test asserts `StoreBusy` raised inside `run_write` on the first attempt and completion within the 30 s policy; a caller-visible `StoreBusy` is unreachable because the retry after `busy_timeout` 10 s outlasts the 12 s holder | integration |

### 11.6 Benchmarks

BT02-01…BT02-07 as in §10.1 (marker `bench`; BT02-01…BT02-04 run in the phase-gate selection on the dev box, spec 11 §4.2).

## 12. Task cards

All cards are Phase 1.

#### T02-01 Store and model foundations

| Field | Content |
|---|---|
| Goal | Error classes, data layout and config section models exist and validate. |
| Depends on | `T00-03 (herness.core.errors)`, `T10-03 (herness.core.config.HernessConfig)` |
| Units | U02-01…U02-07, U02-71…U02-77 |
| Files | `herness/store/errors.py`, `herness/store/layout.py`, `herness/model/settings.py`, `herness/model/errors.py` |
| Tests | UT02-53, UT02-54, UT02-66 |
| Threats | TH02-10 (validation part) |
| Acceptance checks | `pytest -k "UT02-53 or UT02-54 or UT02-66"` passes; `mypy --strict herness/store herness/model` 0 errors; `lint-imports` passes with `model-settings-light` |
| Blocked by | none |
| Size | M |

#### T02-02 Lake writer

| Field | Content |
|---|---|
| Goal | `LakeWriter` writes, rotates, commits and aborts per design 02 §3.2. |
| Depends on | T02-01, `T00-05 (herness.core.ids.new_ulid)` |
| Units | U02-08…U02-19 |
| Files | `herness/store/lake.py` |
| Tests | UT02-01…UT02-13, PT02-01, ST02-01, ST02-02 |
| Threats | TH02-01, TH02-02 |
| Acceptance checks | listed tests pass; module ≤ 380 lines; spec 11 generator smoke (`T11-14 (tools/synth_data.py) --scale tiny`) writes a readable lake |
| Blocked by | OI-06 (glob check inside ST02-02 decides `LAKE_FILE_PATTERN`) |
| Size | M |

#### T02-03 Lake purge and retention

| Field | Content |
|---|---|
| Goal | Record purge and partition retention primitives for spec 10. |
| Depends on | T02-02 |
| Units | U02-20…U02-23 |
| Files | `herness/store/lake_purge.py` |
| Tests | UT02-14…UT02-16, FT02-01 |
| Threats | TH02-01, TH02-16 (lake part) |
| Acceptance checks | listed tests pass; mypy clean |
| Blocked by | none |
| Size | S |

#### T02-04 Ops store core

| Field | Content |
|---|---|
| Goal | Per-thread connections, `run_write` with retry and fault point, JSON helpers, package namespace. |
| Depends on | T02-01, `T08-07 (herness.core.resilience.retry_call)`, `T08-08 (herness.core.resilience.fault_point)` |
| Units | U02-36…U02-43, U02-62 (spec 02 blocks and the block layout of §2.3 rule 4) |
| Files | `herness/store/ops/__init__.py`, `herness/store/ops/core.py`, `herness/store/ops/_shims.py` (re-exports of `retry_call` (T08-07) and `fault_point` (T08-08) that `core` calls; kept, see §2) |
| Tests | UT02-25…UT02-31, UT02-68, FT02-02, ST02-18, BT02-06 |
| Threats | TH02-07, TH02-18 |
| Acceptance checks | listed tests pass; `BT02-06` p95 < 10 ms on CI |
| Blocked by | none |
| Size | M |

#### T02-05 Migration runner and migrations 001–002

| Field | Content |
|---|---|
| Goal | `migrate()` applies checksummed forward-only migrations of every owner range in numeric order (R-11); ingestion and job tables exist. |
| Depends on | T02-04 |
| Units | U02-05 (reason set), U02-44…U02-50, U02-129 |
| Files | `herness/store/ops/migrate.py`, `herness/store/migrations/001_ingestion_health.sql`, `herness/store/migrations/002_jobs.sql`, `herness/store/errors.py` |
| Tests | UT02-33…UT02-35, UT02-40…UT02-42, UT02-70, UT02-71, ST02-15, BT02-07 |
| Threats | TH02-15 |
| Acceptance checks | listed tests pass; `herness init` (`T09-22 (herness._cli.cmd_system)`) creates `ops.sqlite` with 2 migrations recorded |
| Blocked by | none |
| Size | M |

#### T02-06 Migrations 003–006

| Field | Content |
|---|---|
| Goal | All remaining design ops tables, FTS triggers and `metric_sample` exist (R-11, R-12). |
| Depends on | T02-05 |
| Units | U02-51…U02-54 |
| Files | `herness/store/migrations/003_runs_evidence.sql`, `004_memory.sql`, `005_review_chat_privacy.sql`, `006_metric_sample.sql` |
| Tests | UT02-32, UT02-36…UT02-39, IT02-01 |
| Threats | TH02-07 (CHECK caps) |
| Acceptance checks | listed tests pass; schema dump reviewed against §4.3 |
| Blocked by | none (DD02-04 resolved by R-12) |
| Size | M |

#### T02-07 Review items

| Field | Content |
|---|---|
| Goal | `review_item` functions with audit (including decisions inside a caller's transaction, R-33), keyset listing, `approved_mapping_suggestions`. `builds_in_use` is removed (R-68; U02-61). |
| Depends on | T02-06, `T10-05 (herness.core.audit.audit)` |
| Units | U02-55…U02-60 |
| Files | `herness/store/ops/shared.py`, `herness/store/ops/__init__.py` |
| Tests | UT02-43…UT02-47, UT02-75, UT02-76, UT02-79, ST02-06, ST02-07 |
| Threats | TH02-06, TH02-07 |
| Acceptance checks | listed tests pass |
| Blocked by | none |
| Size | M |

#### T02-24 Review-item helpers for impl 03 and impl 07

| Field | Content |
|---|---|
| Goal | `create_review_item_if_absent`, `count_review_items` and `update_review_payload` exist in `shared` and are re-exported (R-09). |
| Depends on | T02-07 |
| Units | U02-130, U02-131, U02-132 |
| Files | `herness/store/ops/shared.py`, `herness/store/ops/__init__.py` |
| Tests | UT02-72, UT02-73, UT02-74 |
| Threats | TH02-07 |
| Acceptance checks | listed tests pass; UT02-68 still passes; `herness/store/ops/shared.py` ≤ 390 lines |
| Blocked by | none |
| Size | S |

#### T02-08 Vector store

| Field | Content |
|---|---|
| Goal | LanceDB tables with schemas, safe deletes and history purge. |
| Depends on | T02-01 |
| Units | U02-63…U02-70 |
| Files | `herness/store/vectors.py` |
| Tests | UT02-49…UT02-52, ST02-08, ST02-09 |
| Threats | TH02-08, TH02-09 |
| Acceptance checks | listed tests pass on the pinned `lancedb` |
| Blocked by | OI-12 (API names; checked inside UT02-51) |
| Size | M |

#### T02-09 Warehouse read side

| Field | Content |
|---|---|
| Goal | Build IDs, `CURRENT` reading, hardened read-only connections, listing, existence check, deletion, health. |
| Depends on | T02-01 |
| Units | U02-24…U02-33, U02-133 |
| Files | `herness/store/warehouse.py` |
| Tests | UT02-17…UT02-22, UT02-24, UT02-77, ST02-03, ST02-04 |
| Threats | TH02-03, TH02-04 |
| Acceptance checks | listed tests pass; UT02-20 asserts the setting names on the pinned DuckDB |
| Blocked by | open-questions (b) item 4 (DuckDB setting names; verified by UT02-20 in this card) |
| Size | M |

#### T02-10 Warehouse write side and import contract

| Field | Content |
|---|---|
| Goal | Writable build connection and atomic `CURRENT` writer, import-restricted. |
| Depends on | T02-09 |
| Units | U02-34, U02-35 |
| Files | `herness/store/_warehouse_rw.py`, `pyproject.toml` (import-linter contracts of §2.2, including `ops-areas-acyclic`) |
| Tests | UT02-20 (build part), UT02-23, ST02-05 |
| Threats | TH02-05 |
| Acceptance checks | listed tests pass; `lint-imports` passes |
| Blocked by | none |
| Size | S |

#### T02-11 Lake inventory and SQL rendering

| Field | Content |
|---|---|
| Goal | Inventory, SQL discovery and sandboxed rendering with safe filters. |
| Depends on | T02-02, T02-01 |
| Units | U02-78…U02-86 |
| Files | `herness/model/lakeinfo.py`, `herness/model/sqlfiles.py`, `herness/model/render_context.py` |
| Tests | UT02-55…UT02-58, PT02-02, ST02-10, ST02-11 |
| Threats | TH02-10, TH02-11 |
| Acceptance checks | listed tests pass |
| Blocked by | none |
| Size | M |

#### T02-12 Reference data, setup SQL and macros

| Field | Content |
|---|---|
| Goal | Reference tables, schemas, fixed tables, DuckDB and Jinja macros; test harness for build ranges. |
| Depends on | T02-11, T02-07 |
| Units | U02-87, U02-128, U02-106, U02-107, U02-108 |
| Files | `herness/model/refdata.py`, `herness/model/sql/_macros.jinja`, `herness/model/sql/000_settings.sql`, `herness/model/sql/010_macros.sql` (plus test support `tests/support/build_harness.py`) |
| Tests | UT02-59…UT02-64 |
| Threats | TH02-10, TH02-12 |
| Acceptance checks | listed tests pass |
| Blocked by | none |
| Size | M |

#### T02-13 ServiceNow and Jira staging

| Field | Content |
|---|---|
| Goal | `110` and `120` staging with dedupe, tombstones, deletions and cast stats. |
| Depends on | T02-12 |
| Units | U02-109, U02-110 |
| Files | `herness/model/sql/110_stg_servicenow.sql`, `herness/model/sql/120_stg_jira.sql` |
| Tests | IT02-02…IT02-08 |
| Threats | TH02-16 (staging part) |
| Acceptance checks | listed tests pass |
| Blocked by | none (R-59: impl 01 owns the Jira raw column contract; §4.1.2 lists the columns staging reads) |
| Size | M |

#### T02-14 Monitoring and generic staging

| Field | Content |
|---|---|
| Goal | `130`–`160` staging. |
| Depends on | T02-12 |
| Units | U02-111…U02-114 |
| Files | `herness/model/sql/130_stg_monitoring.sql`, `140_stg_files.sql`, `150_stg_mongodb.sql`, `160_stg_snowflake.sql` |
| Tests | IT02-33 |
| Threats | — |
| Acceptance checks | IT02-33 passes; build with `extra_entities` for each source renders |
| Blocked by | none |
| Size | S |

#### T02-15 Dataverse staging, org/team/service, service map

| Field | Content |
|---|---|
| Goal | `170`, `200`, `220`. |
| Depends on | T02-13, T02-14 |
| Units | U02-115, U02-116, U02-117 |
| Files | `herness/model/sql/170_stg_dataverse.sql`, `200_org_team_service.sql`, `220_service_map.sql` |
| Tests | IT02-09…IT02-11 |
| Threats | LLM04 control (approved suggestions only) |
| Acceptance checks | listed tests pass |
| Blocked by | D1 (default: CMDB plus overrides) |
| Size | M |

#### T02-16 Incident, change, problem

| Field | Content |
|---|---|
| Goal | `230`, `240`, `250`. |
| Depends on | T02-15 |
| Units | U02-118…U02-120 |
| Files | `herness/model/sql/230_incident.sql`, `240_change.sql`, `250_problem.sql` |
| Tests | IT02-12…IT02-15 |
| Threats | — |
| Acceptance checks | listed tests pass |
| Blocked by | none |
| Size | M |

#### T02-17 Events, daily metrics, work items

| Field | Content |
|---|---|
| Goal | `260`, `270`, `280`. |
| Depends on | T02-15 |
| Units | U02-121…U02-123 |
| Files | `herness/model/sql/260_event.sql`, `270_metric_daily.sql`, `280_work_item.sql` |
| Tests | IT02-16…IT02-20 |
| Threats | — |
| Acceptance checks | listed tests pass |
| Blocked by | none |
| Size | M |

#### T02-18 Build handler and build stage

| Field | Content |
|---|---|
| Goal | `run_build_pipeline` runs stage `build` end to end on `lake_small`. |
| Depends on | T02-16, T02-17, T02-10, `T08-03 (herness.core.jobs.JobContext)`, `T08-12 (herness.core.jobs.register_handler)`, `T10-32 (herness.store.ops.privacy.deleted_record_ids)`, `T01-04 (herness.store.ops.ingest.list_watermarks)`, `T10-03 (herness.core.config.config_hash)`, `T08-05 (herness.store.ops.metrics.record_metric_samples)` |
| Units | U02-88…U02-92, U02-95…U02-99 |
| Files | `herness/model/meta.py`, `herness/model/build.py` |
| Tests | UT02-65, UT02-67, UT02-69, IT02-21, IT02-22, IT02-27, ST02-12, ST02-14 |
| Threats | TH02-12, TH02-14 |
| Acceptance checks | listed tests pass; `herness build` (`T09-23 (herness._cli.cmd_data)`) on `lake_small` produces a `building` file with `finished_at` set |
| Blocked by | `T11-16 (tests/fixtures/lake_small)` exists |
| Size | M |

#### T02-19 Enrich and score stages, attach SQL

| Field | Content |
|---|---|
| Goal | Stages `enrich` (with `stages` and `llm_factory`; impl 03 takes the `decider` GPU scope itself, R-43; `YieldRequested` → `yield`) and `score` (on the build connection) call spec 03/04 hooks; `300`, `310` attach and prune; the one-argument handler factory exists. |
| Depends on | T02-18, `T03-28 (herness.enrich.pipeline.run_enrichment)`, `T04-06 (herness.metrics.facts.materialize_facts)`, `T04-13 (herness.metrics.scoring.run_scoring)` (fakes suffice for tests), `T03-04 (herness.enrich.pipeline.YieldRequested)` |
| Units | U02-100, U02-101, U02-124, U02-125, U02-126, U02-134 |
| Files | `herness/model/build.py`, `herness/model/sql/300_attach_decisions.sql`, `herness/model/sql/310_attach_clusters.sql` |
| Tests | IT02-23…IT02-26, UT02-78, ST02-16 |
| Threats | TH02-16 |
| Acceptance checks | listed tests pass; layers contract exception documented in `pyproject.toml` |
| Blocked by | none (OI-07 and OI-08 are closed: impl 04 `con`, R-48) |
| Size | M |

#### T02-20 DQ checks and gate

| Field | Content |
|---|---|
| Goal | `900_dq_checks.sql`, `evaluate_gate`, stage `dq`. |
| Depends on | T02-19 |
| Units | U02-93, U02-94, U02-102, U02-127 |
| Files | `herness/model/dq.py`, `herness/model/sql/900_dq_checks.sql`, `herness/model/build.py` |
| Tests | IT02-28…IT02-30 |
| Threats | TH02-17 (gate part) |
| Acceptance checks | listed tests pass |
| Blocked by | none |
| Size | M |

#### T02-21 Promotion and cleanup

| Field | Content |
|---|---|
| Goal | Stage `promote`, retention and orphan cleanup. |
| Depends on | T02-20, `T06-05 (herness.store.ops.runs.select_runs)` (a fake suffices for tests) |
| Units | U02-103…U02-105 |
| Files | `herness/model/promote.py`, `herness/model/build.py` |
| Tests | UT02-48, IT02-32, FT02-03, FT02-04, FT02-05, ST02-17 |
| Threats | TH02-03, TH02-17 |
| Acceptance checks | listed tests pass; `herness pipeline` (`T09-23 (herness._cli.cmd_data)`) on `lake_small` promotes and `herness status` shows the build |
| Blocked by | none |
| Size | M |

#### T02-22 End-to-end and golden verification

| Field | Content |
|---|---|
| Goal | Golden snapshot files, dirty-data and crash-consistency tests, raw-text exposure check, CI benchmarks. |
| Depends on | T02-21 |
| Units | — (tests only) |
| Files | none (tests and fixtures: `tests/fixtures/golden/lake_small_counts.json`, `lake_small_core.json`) |
| Tests | IT02-31, FT02-06, ST02-13, BT02-05 |
| Threats | TH02-13 |
| Acceptance checks | listed tests pass; coverage of `herness/store` and `herness/model` at or above spec 11 §4.3 targets |
| Blocked by | `T11-14 (tools/synth_data.py)` `small` scale with `--dirty default`; `T05-04 (config/models.yaml harness.sql.blocked_columns)` default published |
| Size | S |

#### T02-23 Scale benchmarks

| Field | Content |
|---|---|
| Goal | Design 02 §9 targets measured on the reference PC. |
| Depends on | T02-22 |
| Units | — |
| Files | none (bench tests under `tests/bench/`) |
| Tests | BT02-01…BT02-04 |
| Threats | — |
| Acceptance checks | results written to `data/bench/`; all four pass their thresholds |
| Blocked by | `T11-14 (tools/synth_data.py)` `full` scale; reference hardware |
| Size | S |

## 13. Design deltas and open items

Rulings: the consistency pass rulings in [`DECISIONS.md`](DECISIONS.md) (R-01…R-76, with R-46 as corrected) bind this spec. Each item below is marked "Resolved by R-nn" (a ruling settled it and this spec follows the ruling), "Accepted (R-nn)" (a ruling accepted this spec's proposal; the design spec edit is pending per `DECISIONS.md` §9), or "Still open" (no ruling; the default stated here is implemented).

### 13.1 Design deltas

| ID | Design spec | Change needed | Status |
|---|---|---|---|
| DD02-01 | 00 §3, 02 §5 | `herness/store/ops.py` becomes package `herness/store/ops/` with one submodule per area (§2.3); public path `herness.store.ops.<fn>` unchanged. Reason: 400-line module limit (ENG §2.4) with functions from eight specs. | Accepted (R-08; ENG §14 E6). The canonical area table is §2.3. |
| DD02-02 | 02 §4.2, §13; 00 §4 | Staging reads `deletion_request` and approved `review_item` rows through `herness.store.ops` and registers them in DuckDB as Arrow tables instead of `ATTACH`ing the ops store; drop "`duckdb` (with `sqlite` extension)" from §13. Reason: no extension download on an offline host (TB9), and ops access stays behind `herness.store.ops`. | Still open |
| DD02-03 | 02 §3.2 | `LakeWriter.__init__` gains keyword-only `root: Path \| None = None` (spec 11 generator writes to its own root) and `clock`; `LakeWriter` is a context manager. Additive. | Still open |
| DD02-04 | 02 §5.2, 08 | Add table `metric_sample` (§4.3.6) in migration 006 of this spec; writer and 90-day retention in impl 08 (ENG E5). | Resolved by R-12 (table here; writer `herness.store.ops.metrics.record_metric_samples`, impl 08) |
| DD02-05 | 02 §5 | Name the `schema_migration` table (`version`, `name`, `checksum`, `applied_at`) that ENG §3.5 requires. | Still open |
| DD02-06 | 02 §8 | New key `sources.yaml: build.service_ci_classes`; `dq.*` key names of U02-74; `service_overrides[]` shape of U02-71 incl. `aliases`; enum domain names of U02-73. `dq` and `build` are top-level sibling sections of `sources.yaml`, owned by `herness.model.settings` and composed into the root by impl 10 (R-03). | Still open (the R-03 part is resolved) |
| DD02-07 | 02 §4.8 | Additional warn check `metric_daily_unmapped_service` (threshold `dq.metric_daily_unmapped_warn` 0.05), because `core.metric_daily` rows need a non-NULL `service_id` for their key. | Still open |
| DD02-08 | 02 §3.1 | "The lake is append-only" gains "except privacy deletion and the retention purge by spec 10 through `herness.store.lake_purge`". This spec has no lake compaction. | Accepted (R-57) |
| DD02-09 | 02 §4.7, §7 | `finished_at IS NULL` with `status = 'building'` defines an orphan; a completed unpromoted build keeps `status = 'building'` with `finished_at` set; `retired` is written best-effort when the old file is not held open. | Still open |
| DD02-10 | 02 §3.1, 01 §4.2 | Jira flattening contract: each key of `fields` becomes its own column (JSON text for objects), plus `changelog`, `issuelinks`, `remotelinks`. | Resolved by R-59: impl 01 owns the contract (`JIRA_ISSUE_COLUMNS`); §4.1.2 now only lists what staging reads. |
| DD02-11 | 02 §5, 00 §3 | Migration numbering: migrations 001–006 create every design table; implementation-only tables and columns use owner ranges (`MIGRATION_RANGES`, U02-129) and the runner applies all files in numeric order, allowing gaps and late lower-numbered files. | Accepted (R-11) |
| DD02-12 | 02 §5.5 | `review_item` API: `decide_review_item` takes an optional caller connection and refuses `memory_write` items outside `MemoryStore.approve` / `MemoryStore.reject` (except the system rejection of a purge, R-54); new `create_review_item_if_absent`, `count_review_items`, `update_review_payload`; `list_review_items` gains `statuses`, `decided_after` and `payload_match`. | Accepted (R-08, R-09, R-33; impl 03 RQ-01, RQ-02) |

### 13.2 Open items (with the default this spec implements)

| ID | Item | Default | Blocks | Status |
|---|---|---|---|---|
| OI-01 | D1: is the CMDB authoritative for ownership? | CMDB plus `mappings.yaml` overrides; suggestions third | T02-15 (proceeds on default) | Still open |
| OI-02 | Jira custom field IDs per instance (design 02 §12, open-questions (b) 23) | configured in `mappings.yaml` at Phase 6; NULL columns until then | none in Phase 1 | Still open |
| OI-03 | open-questions (b) 25: which ServiceNow field holds `acknowledged_at` and customer impact (D13) | custom fields unset → NULL, MTTA disabled | none in Phase 1 | Still open |
| OI-04 | Department mode: how groups map to departments when `cmn_department` exists | same non-empty `cost_center`, lowest department `sys_id` | T02-15 (default) | Still open |
| OI-05 | Canonical feeds from config-defined sources (`files`, `mongodb`, `snowflake`, `dataverse`) | staged generically only; a deployment adds a reviewed file in 281–299 (`29N_feed_<source>_<entity>.sql`) through a spec 02 change | none | Still open |
| OI-06 | Does DuckDB's glob support `[!.]`? | verified in ST02-02; fallback `LAKE_FILE_PATTERN = "part-*.parquet"` | T02-02 | Still open |
| OI-07 | `run_scoring(build_id)` took no connection while the build held one | impl 04 added keyword-only `con` and `ctx` (DD04-02); U02-101 passes the build connection; no close and reopen | none | Closed by impl 04 DD04-02 (no ruling needed) |
| OI-08 | `herness enrich --stage S` (spec 09) had no parameter in `run_enrichment` (spec 03) | payload `enrich_stage` is passed as `stages=[enrich_stage]` | none | Resolved by R-48 |
| OI-09 | `DuckDBPyConnection.extract_statements` on the pinned DuckDB | present from 1.0; T02-18 asserts it | T02-18 | Still open |
| OI-10 | Which 6 ULID characters form `<ulid6>` in `build_id` | last 6 (random part) | T02-09 | Still open |
| OI-11 | `core.incident.team_id` "assignment group at resolution" | latest version's `assignment_group` (no history table) | T02-16 | Still open |
| OI-12 | LanceDB version-cleanup API names on the pinned version | `compact_files` + `cleanup_old_versions`, else `optimize(cleanup_older_than=…)` | T02-08 | Still open |
| OI-13 | open-questions (b) 26: a restored tombstone with an older `sys_updated_on` stays deleted | dedupe as designed (latest `_source_updated_at` wins); spec 01 reconciliation decides whether to bump the timestamp | none | Still open |
| OI-14 | ENG §2.3 module-state exception: per-thread ops connections and path override | allowed and reset by the `ops_store` fixture | T02-04 | Still open |
| OI-15 | `HealthStatus` type location: no spec defines it (R-01 lists no generic submodule of `herness.core.types`) | no shared type: U02-33, U02-48 and U02-70 return the ENG §4 tuple `(status, reason)` that impl 03 and impl 09 health functions also return (§3.4) | none | Closed (no ruling needed) |
| OI-16 | Impl 03's yield signal was private but the `build_pipeline` handler (U02-100) must catch it | impl 03 published `T03-04 (herness.enrich.pipeline.YieldRequested)` (U03-152); U02-100 catches it by that name | none | Resolved (impl 03 U03-152) |
| OI-17 | `T01-04 (herness.store.ops.ingest.list_watermarks)` (used by U02-99 for `meta.build.source_watermarks`) was not defined by impl 01 | impl 01 added it as U01-92 on T01-04 (R-09) | none | Closed |

### 13.3 Contradictions noticed between specs (for the consistency pass)

| # | Specs | Observation | Handling here | Status |
|---|---|---|---|---|
| C1 | 01 §4.2 (Q7) vs 11 §5.1.2 vs 02 §3.1 | 01 says `busines_criticality` only comes from `cmdb_ci_service`; spec 11's generator writes it on `cmdb_ci` rows and writes no `cmdb_ci_service`, `cmn_department` or `task_sla` entities | staging reads `busines_criticality` from `cmdb_ci_service` only and tolerates each of the three entities being absent (U02-109, IT02-08, IT02-10) | Resolved by R-60 |
| C2 | 01, 02, 11 | Jira raw column names are not defined consistently | impl 01 owns the contract; staging reads it (§4.1.2) | Resolved by R-59 |
| C3 | 10 §3.1 vs ENG §2.1 | `herness.core.config` (L0) imports section models from owner packages (L2+) | settings exception; `herness.model.settings` imports only the standard library, pydantic, `herness.core.types`, `herness.core.errors` | Resolved by R-03 |
| C4 | 09 §5 vs 03 §3.1 | `enrich --stage` has no matching parameter | `stages` parameter; OI-08 | Resolved by R-48 |
| C5 | 04 §3.1 vs 02 §4.1 | `run_scoring` opens its own connection while the build job holds one | U02-101 passes `con` (impl 04 DD04-02); the design 04 §3.1 signature edit is still pending | Still open (design edit only; implemented here) |
| C6 | 05 §5.3 vs 08 §3.1 | spec 05 names a retry policy `sql_tool` that spec 08's `PolicyName` does not list | not used here | Resolved by R-24 |
| C7 | ENG §4 vs 08 | `metric_sample` not defined in spec 08 | migration 006 here, writer in impl 08 | Resolved by R-12 |
| C8 | impl 07 §3.4, impl 09 §4.1 vs R-11 | impl 07 `070_memory.sql` and impl 09 `090_chat.sql` create `memory_item`, `memory_fts`, `recommendation`, `decision_log`, `outcome`, `chat_session`, `chat_message`, which migrations 004 and 005 already create | under R-11 only 001–006 create design tables; 070 and 090 may only add implementation-only tables or columns. Until impl 07 and 09 change, UT02-32 fails on a duplicate `CREATE TABLE` | Still open (owners 07, 09 to apply R-11) |
| C9 | impl 09 U09-51, U09-52 vs R-08 | impl 09 defines `herness.store.ops.review.decide_review_item` and `ui_reads.list_review_items`, `ui_reads.get_review_item` with different return shapes | U02-57, U02-58, U02-59 are the only definitions; §13.4 gives the mapping; a duplicate name fails UT02-68 | Resolved by R-08, R-09 (impl 09 to reference) |
| C10 | impl 01 §2 vs R-03 | `herness.connectors.settings` imported `herness.model.settings` to nest `dq` and `build` inside `SourcesConfig`, and this spec's earlier wiring asked for that | `dq` and `build` are top-level sibling sections of `sources.yaml`; `herness.connectors.settings` owns the connector sections, `herness.model.settings` owns `DqSettings` and `BuildSettings`; impl 10's root config composes both; neither settings module imports the other (§3.9 wiring, §9) | Resolved by R-03 (agreed with impl 01; impl 10 composes) |
| C11 | impl 09 `pipeline_payload` vs U02-96 | impl 09 puts scoring steps under payload key `steps`; `BuildPipelinePayload` (`extra="forbid"`) names it `score_steps` | `score_steps` stays; a payload with `steps` fails validation with `ConfigError` | Still open (impl 09 to use `score_steps`) |
| C12 | impl 04 U04-56 vs §2.2 `store-rw-restricted` | `run_scoring` falls back to a writable `herness.store.warehouse.open_build(build_id, read_only=False)` when `con` is `None`; no such public writable opener exists (the only writable opener is the restricted `herness.store._warehouse_rw.open_for_build`, U02-34, on T02-10), and `_warehouse_rw` may be imported only by `herness.model.build` and `herness.model.promote` | the pipeline always passes `con`; impl 04 must make `con` required for writes (or raise `ConfigError` without it) | Still open (impl 04) |
| C13 | impl 03 U03-144 vs R-43 | impl 03's precondition says the job already holds `decider` | impl 03 enters `ctx.gpu_scope("decider")` itself for its GPU stages; U02-100 enters no GPU scope, so no class is held during CPU-only stages | Resolved by R-43 |
| C14 | impl 09 U09-94 vs ENG §4 | `herness doctor` (`T09-22 (herness._cli.doctor.run_doctor)`) does not call `ops_health`, `warehouse_health` or `VectorStore.health`; its `ops_migrations` and `current_build` rows read the stores directly. The earlier reference to an impl 10 doctor card was wrong: impl 10's doctor checks (`T10-27`, `T10-28`) are host and GPU checks only | the three health functions stay (ENG §4) and return the `(status, reason)` tuple | Still open (impl 09 to call them or state why not) |
| C15 | impl 07 U07-100 (purge) vs U02-59 | `MemoryStore.purge` rejects pending `memory_write` review items with `decide_review_item(..., decided_by="system")` without `conn`, which the earlier U02-59 refused | U02-59 accepts a `memory_write` decision without `conn` only as a `system` rejection (R-54); every other `memory_write` decision needs `conn` from `MemoryStore.approve` / `reject` (R-33) | Resolved here (UT02-76) |
| C16 | impl 01 U01-29 vs R-68 | impl 01 still defines `herness.store.ops.deleted_record_ids` in area `ingest`; R-68 places it in impl 10's `privacy` area (U10-111) | this spec calls `T10-32 (herness.store.ops.privacy.deleted_record_ids)`; §2.3 no longer lists the read under `ingest` | Resolved by R-68 (impl 01 to remove U01-29) |
| C17 | impl 09 U09-87 vs R-46 (corrected) | impl 09 still maps every failure to exit 1 and lists codes 0–4 | §6 uses the corrected design 09 §5.8 codes (3, 5, 7, 8, 11) | Resolved by R-46 (impl 09 to apply) |

### 13.4 Names other specs use for spec 02 units (R-09, R-10)

Every function of the `core`, `migrate` and `shared` areas that another implementation spec references has a unit here. Earlier names map as follows; the other specs replace them (R-10), and this table is the lookup other specs use to resolve a reference to a spec 02 unit into `T02-nn (<canonical name>)`.

| Name used elsewhere (spec) | Canonical unit | Note |
|---|---|---|
| `write_tx` (07, 08, 09), `write_transaction` (06), `transaction` (01) | `herness.store.ops.core.run_write` (U02-38) | Callback form `run_write(fn, op=...)`. Code that wrote inside a `with write_tx() as conn:` block moves that body into `fn(conn)`. |
| `connect` (08), `read_connection` (06), `connection` (07, 09) | `herness.store.ops.core.connection` (U02-37) | One connection per thread. |
| `open_ops_store` (01), `OpsStore` (01, 06, 09, 11) | module functions of `herness.store.ops` (U02-37, U02-38, U02-45) | There is no store object. Parameters typed `OpsStore` are removed; the path comes from config or `reset_connections(path=...)` (U02-43). |
| `migration_status` (09) | `herness.store.ops.migrate.pending_migrations` (U02-46) and `schema_version` (U02-47) | `herness doctor` FAIL when `pending_migrations()` is non-empty. |
| `migrate` (07, 08, 09) | `herness.store.ops.migrate.migrate` (U02-45) | Same name. |
| `insert_review_item` (07) | `herness.store.ops.shared.create_review_item` (U02-56) | Pass `conn` inside the caller's transaction. |
| `create_review_item_if_absent` (03) | U02-130 | Same name; takes `now`. |
| `count_review_items` (03) | U02-131 | Same name. |
| `list_review_items` (03, 09) | U02-58 | 03's `statuses`, `decided_after`, `payload_match` and `limit` up to 5,000 are supported; 09's `status` default `"pending"` is the caller's argument (the unit's default is all statuses). |
| `get_review_item` (09, `ui_reads`) | U02-57 | Raises `NotFoundError` instead of returning `None`. |
| `decide_review_item` (07, 09 `herness.store.ops.review`) | U02-59 | Lives in `shared`, not `review`. Returns the updated `ReviewItem`; 09 maps `ReviewItemConflict` → `not_pending` and `NotFoundError` → `not_found`. 07 passes `conn` (R-33). |
| `update_review_payload` (07) | U02-132 | Same name. |
| `ReviewItemRow` (07, 09) | `herness.store.ops.shared.ReviewItem` (U02-55) | Frozen dataclass, not a `TypedDict`. |
| `herness.store.warehouse.open_current`, `open_current_readonly`, `connect_current_readonly` (03, 04, 07) | `herness.store.warehouse.open_readonly()` (U02-29) with `build_id=None` | The returned DuckDB connection is also a context manager. |
| `herness.store.warehouse.connect_build_readonly(build_id)` (07) | `open_readonly(build_id)` (U02-29) | |
| `herness.store.warehouse.current_build_id` (06, 07) | `read_current()` (U02-27), or `CurrentPointer.get()` (U02-28) for long-running readers | |
| `herness.store.warehouse.build_exists` (06) | U02-133 | Same name. |
| `herness.store.warehouse.open_build(build_id, read_only=False)` (04) | none public; the build job's own opener is `herness.store._warehouse_rw.open_for_build` (U02-34, T02-10), importable only by `herness.model.build` and `herness.model.promote` | See C12: writable connections come only from the build job. |
| `herness.store.vectors.connect` (07) | `VectorStore(path)` (U02-64) | |
| `herness.store.vectors.open_table` (03) | `VectorStore.table` (U02-66) | |
| `herness.model.build.render_sql` (01) | `herness.model.sqlfiles.render_sql` (U02-85) | Takes a `SqlFile` and a `RenderContext`. |
| `DqConfig`, `BuildConfig` (earlier drafts of this spec) | `herness.model.settings.DqSettings` (U02-74), `BuildSettings` (U02-75) | Renamed to the names impl 01 uses; composed into the root by impl 10 (R-03). |
| `write_metric_samples`, `record_metric_sample` (any) | `herness.store.ops.metrics.record_metric_samples` (impl 08) | R-12; not a spec 02 unit. |
| `insert_evidence` (any) | `herness.store.ops.evidence.record_evidence` (impl 05) | R-13; not a spec 02 unit. |
| `builds_in_use` (earlier drafts of this spec) | none: removed (R-68, U02-61) | `cleanup_builds` reads pinned builds through impl 06 `select_runs`. |
| `herness.store.ops.ingest.deleted_record_ids` (01, earlier drafts of this spec) | `T10-32 (herness.store.ops.privacy.deleted_record_ids)` (impl 10) | R-68; not a spec 02 unit. |

## 14. Dependencies

### 14.1 Third-party packages

| Package | Min version | Licence | Use |
|---|---|---|---|
| `duckdb` | 1.3 | MIT | warehouse build and reads (no extensions) |
| `pyarrow` | 17 | Apache-2.0 | lake files, reference tables, vector schemas |
| `jinja2` | 3.1 | BSD-3-Clause | sandboxed SQL templates |
| `lancedb` | 0.13 | Apache-2.0 | vector tables |
| `pydantic` | 2.9 | MIT | config and payload models |
| `sqlite3` (standard library) | SQLite 3.38 with FTS5 | public domain | ops store |
| `hypothesis`, `freezegun`, `pytest-benchmark` (dev) | per spec 00 §9 | MPL-2.0, Apache-2.0, BSD-2-Clause | PT, UT, BT tests |

No new dependency beyond spec 00 §9; the DuckDB `sqlite` extension is removed (DD02-02).

### 14.2 Internal dependencies

| Spec | Units used |
|---|---|
| 00 | `T00-03 (herness.core.errors)` (taxonomy: `ConfigError`, `NotFound`, `SchemaViolation`, `FatalError`, `StoreBusy`), `T00-05 (herness.core.ids.new_ulid)`, `T00-04 (herness.core.time.format_utc)` and `T00-04 (herness.core.time.parse_utc)` (fixed-width UTC text), `T00-07 (herness.core.logging)` |
| 01 | `T01-04 (herness.store.ops.ingest.list_watermarks)` (OI-17), `T01-17 (herness.connectors.jira.JIRA_ISSUE_COLUMNS)` (Jira raw column contract, R-59), connector sections of `sources.yaml` (siblings of `dq` and `build`, R-03); consumer of `LakeWriter` |
| 03 | `T03-28 (herness.enrich.pipeline.run_enrichment)` (with `stages`, `llm_factory`; R-48, R-05), `LlmFactory`, `T03-04 (herness.enrich.pipeline.YieldRequested)` (OI-16); consumer of `VectorStore`, enrich placeholder tables, `create_review_item_if_absent`, `list_review_items`, `count_review_items` |
| 04 | `T04-06 (herness.metrics.facts.materialize_facts)`, `T04-13 (herness.metrics.scoring.run_scoring)` (keyword-only `con`, `ctx`), `T04-06 (herness/model/sql/400_facts.sql)` |
| 05 | `T05-04 (config/models.yaml harness.sql.blocked_columns)` default (ST02-13); consumer of `open_readonly`, `CurrentPointer` |
| 06 | `T06-05 (herness.store.ops.runs.select_runs)` and the `run.status` terminal set (read by `cleanup_builds`, R-68); consumer of `build_exists`, `read_current`, `run_write` |
| 07 | `MemoryStore.approve`, `MemoryStore.reject` (T07-09) and `MemoryStore.purge` (T07-26) call this spec's review functions (R-33, R-54); consumer of `VectorStore`, `memory_*` tables, `create_review_item`, `decide_review_item`, `update_review_payload` |
| 08 | `T08-07 (herness.core.resilience.retry_call)`, `T08-08 (herness.core.resilience.fault_point)`, `T08-03 (herness.core.jobs.JobContext)` (`should_yield`, `save_state`; `gpu_scope` is used by impl 03, not here, R-43), `T08-01 (herness.core.types.JobOutcome)`, `T08-12 (herness.core.jobs.register_handler)`, `T08-05 (herness.store.ops.metrics.record_metric_samples)` (R-12); owner areas `jobs`, `tasks`, `worker`, `resilience`, `metrics` (§2.3) |
| 09 | CLI wiring of `herness init` and `status` (`T09-22 (herness._cli.cmd_system)`), `build`, `pipeline`, `enrich --stage` (`T09-23 (herness._cli.cmd_data)`), review queue (`list_review_items`, `get_review_item`, `decide_review_item`); `herness doctor` (`T09-22 (herness._cli.doctor.run_doctor)`, C14); exit codes of R-46 (corrected) |
| 10 | `T10-03 (herness.core.config.get_config)` (composes `sources.yaml` from impl 01 and this spec's sections, R-03), `T10-03 (herness.core.config.config_hash)`, `T10-05 (herness.core.audit.audit)`, `T10-32 (herness.store.ops.privacy.deleted_record_ids)` (R-68); `T10-29 (herness.admin.privacy.run_privacy_delete)` is a consumer of `purge_record_ids`, `purge_partitions_before`, `cleanup_builds` |
| 11 | `T11-16 (tests/fixtures/lake_small)`, `T11-14 (tools/synth_data.py)`, `T11-19 (tests/integration/test_build_dirty.py)` |
