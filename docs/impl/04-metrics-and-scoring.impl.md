# 04 — Metrics and Scoring: Implementation Specification

Status: Draft v1 · 2026-09-24 · Design spec: [`docs/specs/04-metrics-and-scoring.md`](../specs/04-metrics-and-scoring.md) (Draft v2) · Phase: 2 · Standards: [`ENG-STANDARDS.md`](ENG-STANDARDS.md)

Depends on implementation specs: impl 00 (errors, logging, time, and the single implementations of `canonical_json`, `sha256_hex`, `normalize_sql` and `query_id` in `herness.core.ids`, R-14), impl 02 (read-only warehouse open, build runner and its writable connection, `core.*` and `meta.*` DDL, empty `enrich.*` tables, review-item reads), impl 03 (`enrich.*` content), impl 05 (ops evidence writer `herness.store.ops.evidence.record_evidence`, R-13; `Evidence` type in `herness.core.types.harness`, re-exported from `herness.core.types`, R-01), impl 08 (`herness.core.jobs.JobContext`, R-02; `herness.core.jobs.run_inline`, R-45; metric writer `herness.store.ops.metrics.record_metric_samples`, R-12), impl 09 (CLI command table, R-47), impl 10 (config loader, `ConfigIssue`, `config_hash`, start-up validation hook, R-71), impl 11 (synthetic data generator). Cross-spec task dependencies are written `T<NN>-<nn> (<qualified name>)`, naming the owner task card.

Consistency pass: this spec applies the binding rulings of [`DECISIONS.md`](DECISIONS.md). The rulings that change it are R-01, R-02, R-03, R-08, R-10, R-11, R-12, R-13, R-14, R-15, R-40, R-44, R-45, R-47, R-61 and R-64. Each change cites its ruling where it applies, and §13 records the status of every earlier delta.

## 1. Scope and traceability

This spec builds the package `herness/metrics/`, the stage file `herness/model/sql/400_facts.sql`, the SQL templates under `herness/metrics/sql/`, and the config files `config/metrics.yaml` and `config/weights.yaml`. The package loads and validates the metric catalog and weights, materializes per-record fact tables in stage 400, computes metrics for any grain and window, computes the funding score, the org improvement score, action levers and the portfolio selection, and records every producing query in `meta.evidence` with the shared `result_hash` of design 00 §5.1. No model is called anywhere in the package. Every stored number comes from a recorded DuckDB `SELECT`; the only Python-side calculations are pure functions (hashing, encoding, window arithmetic, template rendering, the CP-SAT model and the ordering of selected candidates), per ENG §2.3.

### 1.1 Traceability matrix

| Design § | Requirement (short) | Impl § | Units | Tasks | Tests |
|----------|---------------------|--------|-------|-------|-------|
| 04 §1 | Package scope, no model calls, every number from a recorded `SELECT` | 1, 2, 7 | all | T04-01…T04-22 | ST04-02, IT04-02 |
| 04 §2 | Responsibilities list | 1, 3 | U04-01…U04-83 | T04-01…T04-21 | IT04-01 |
| 04 §3.1 catalog API | `Period`, `EntityType`, `Unit`, `MetricDef`, `load_catalog`, `MetricCatalog` | 3.3 | U04-14, U04-15, U04-23…U04-25 | T04-02, T04-03 | UT04-13…UT04-18 |
| 04 §3.1 compute API | `MetricRow`, `MetricResult`, `compute_metric`, `metric_series` | 3.6 | U04-49…U04-53 | T04-08 | UT04-64…UT04-70, IT04-03 |
| 04 §3.1 `peer_group` | Peer group for team, org, service, work item | 3.8 | U04-61…U04-63 | T04-17 | UT04-71…UT04-74, IT04-04 |
| 04 §3.1 `materialize_facts` | Stage 400 hook | 3.5 | U04-41…U04-47 | T04-06, T04-07 | UT04-30…UT04-35 |
| 04 §3.1 `run_scoring` | Steps, report | 3.7 | U04-54…U04-60 | T04-12, T04-13, T04-21 | UT04-110…UT04-118 |
| 04 §3.1 portfolio API | `Scenario`, `optimize_portfolio`, `PortfolioResult` | 3.10 | U04-72…U04-81 | T04-19, T04-20 | UT04-101…UT04-109, PT04-09, PT04-10 |
| 04 §3.1 evidence API | `result_hash`, `result_sample`, `run_recorded`, `RecordedQuery` | 3.1, 3.2 | U04-01…U04-13 | T04-01, T04-05 | UT04-01…UT04-12, PT04-01, PT04-02 |
| 04 §3.1 bullets | Read-only callers, caps, default window, `as_of`, persist modes | 3.4, 3.6, 3.10 | U04-29…U04-32, U04-51, U04-52, U04-80 | T04-04, T04-08, T04-20 | UT04-27…UT04-29, UT04-107, UT04-108, IT04-09 |
| 04 §3.2 | CLI hook sequence (`herness score` enqueues by default, `--inline` runs in-process, R-45; `metrics list`; nightly) | 5 (F04-02, F04-09, F04-14) | U04-23, U04-24, U04-56, U04-80 | T04-03, T04-13, T04-20 | UT04-18, UT04-110, UT04-111, UT04-112 |
| 04 §3.3 | Scoring steps, order, reads/writes, checkpoint | 3.7, 5 (F04-02, F04-13) | U04-55…U04-59 | T04-13 | UT04-110…UT04-112, FT04-01 |
| 04 §4.1 catalog file | YAML shape, template contract, macros, bind names, allowed filters, forbidden columns | 3.3, 3.4, 9 | U04-26, U04-28, U04-33…U04-40 | T04-03, T04-04 | UT04-14, UT04-16, UT04-23…UT04-26, ST04-02, ST04-04 |
| 04 §4.1 grain table | `entity_col` / `entity_join` per source and grain | 3.4 | U04-35 | T04-04 | UT04-23 |
| 04 §4.1 D6 | Resolving team for org scores; service owner for funding | 3.4, 3.5, 3.9 | U04-35, U04-43, U04-64 | T04-04, T04-06, T04-14 | UT04-23, UT04-30, UT04-76 |
| 04 §4.2 | Fact tables and closures, column types, `query_id` per row | 3.5, 4.1 | U04-41…U04-47 | T04-06, T04-07 | UT04-30…UT04-35 |
| 04 §4.3 | Output tables, `metric_value` coverage, `SCORE_UNITS` | 3.3, 3.7, 4.1 | U04-27, U04-58 | T04-03, T04-13 | UT04-117, UT04-118 |
| 04 §4.4 | `run_recorded`, `params` shape, stored-table hashing, `meta.evidence` row, `result_sample` | 3.1, 3.2 | U04-06, U04-09…U04-12 | T04-01, T04-05 | UT04-05…UT04-10, IT04-06 |
| 04 §4.4 tolerance | Verifier cell tolerance; the Verifier (05) calls `rows_equivalent` (R-15) | 3.1 | U04-07 | T04-01 | UT04-11, PT04-11 |
| 04 §5.1 | Fact derivations (exclusion, durations, repeat, change_caused, cluster, change, work item, `cat_at`) | 3.4, 3.5 | U04-35, U04-43…U04-45 | T04-04, T04-06, T04-07 | UT04-30…UT04-33 |
| 04 §5.2 | 28 metric definitions | 3.6 | U04-48, U04-40 | T04-08…T04-11 | UT04-36…UT04-63, PT04-03, PT04-04 |
| 04 §5.2.1 | Epic predictability | 3.6 | U04-48 (#26) | T04-11 | UT04-61 |
| 04 §5.3 | Dollar conversion, `unconfirmed_weights` | 3.3, 3.5 | U04-19, U04-20, U04-43 | T04-02, T04-06 | UT04-30, UT04-68 |
| 04 §5.4 | Funding candidates, closure, cluster-fix IDs, leaf-most allocation | 3.5, 3.9 | U04-42, U04-64 | T04-06, T04-14 | UT04-34, UT04-78, UT04-87 |
| 04 §5.5 | Attribution tiers, allocation, two passes, annualization, confidence, strategic weight, effort, priority | 3.9 | U04-64…U04-66 | T04-14, T04-15 | UT04-75…UT04-87, PT04-05, PT04-06 |
| 04 §5.6 | WSJF | 3.9 | U04-65 | T04-15 | UT04-85 |
| 04 §5.7 | Ranking and ties | 3.9 | U04-65 | T04-15 | UT04-84 |
| 04 §5.8 | Org improvement score | 3.9 | U04-67, U04-68 | T04-16 | UT04-88…UT04-91, PT04-07 |
| 04 §5.8.1 | Peer groups for services and work items | 3.8 | U04-61…U04-63 | T04-17 | UT04-72…UT04-74 |
| 04 §5.9 | Action levers, templates, placeholders | 3.3, 3.9 | U04-26, U04-69…U04-71 | T04-03, T04-18 | UT04-21, UT04-92…UT04-100, ST04-10 |
| 04 §5.10 | Portfolio optimizer, constraints, determinism, order_rank, persist modes | 3.10 | U04-72…U04-81 | T04-19, T04-20 | UT04-101…UT04-109, PT04-08…PT04-10, IT04-05, IT04-07 |
| 04 §6 | Errors and resilience, resumable scoring | 6 | U04-12, U04-51, U04-56, U04-80 | T04-05, T04-08, T04-13, T04-20 | UT04-65, UT04-66, UT04-113, UT04-115, FT04-01…FT04-05 |
| 04 §7.1 | `metrics.yaml` scoring section | 9 | U04-16…U04-18 | T04-02 | UT04-19, UT04-22 |
| 04 §7.2 | `weights.yaml`, unconfirmed gate via `weight_change` review item | 9, 5 (F04-12) | U04-19…U04-22, U04-82, U04-83 | T04-02, T04-03 | UT04-19, UT04-20, UT04-120, ST04-05 |
| 04 §8 | Performance targets | 10 | U04-04, U04-12, U04-47, U04-56, U04-80 | T04-22 | BT04-01…BT04-09 |
| 04 §9 | Security (read-only, sandbox, no text columns, rationale, weight gate) | 7 | U04-11, U04-26, U04-33, U04-51, U04-80 | T04-03, T04-04, T04-05, T04-08, T04-20 | ST04-01…ST04-14 |
| 04 §10.1 | Hand-computed fixtures | 11 | U04-43, U04-48 | T04-06…T04-11 | UT04-30…UT04-63 |
| 04 §10.2 | Property tests and invariants in `check` | 3.7, 11 | U04-59, U04-60 | T04-13, T04-21 | PT04-01…PT04-12, UT04-115 |
| 04 §10.3 | Synthetic planted truths | 11 | U04-56 | T04-22 | IT04-08 |
| 04 §10.4 | Optimizer tests | 11 | U04-73, U04-74, U04-80 | T04-19, T04-20 | PT04-09, PT04-10, IT04-05, IT04-07, UT04-105, UT04-107, UT04-108 |
| 04 §10.5 | API contracts for 07 and 09 | 11 | U04-53, U04-62, U04-06, U04-26 | T04-08, T04-17 | IT04-03, IT04-04, IT04-06, UT04-15 |
| 04 §11 | Open questions | 13 | — | — | — |
| 04 §12 | Dependencies | 14 | — | — | — |
| 04 §13 | Resolved contract changes | 4, 13 | U04-41…U04-45, U04-58 | T04-06, T04-07, T04-13 | UT04-30…UT04-34 |
| 00 §5 | `query_id` over normalized SQL, params, build_id, computed only by `herness.core.ids` (R-14) | 3.2 | U04-09, U04-12 | T04-01, T04-05 | UT04-10, IT04-11 |
| 00 §5.1 | Shared `result_hash`, owned here with its pinned canonical rules (R-15) | 3.1 | U04-01…U04-05 | T04-01 | UT04-01…UT04-04, PT04-01, IT04-10 |
| 00 §12.1 | Units vocabulary | 3.3 | U04-14, U04-27 | T04-02, T04-03 | UT04-15, UT04-117 |

## 2. Module map

All paths are repo-relative. Layer L3 per ENG §2.1. `herness.metrics` MUST NOT import `herness.enrich`, `herness.harness`, `herness.reports`, `herness.eval`, `herness.cli` or `app`.

| Path | Purpose | Public symbols | Layer | Extra imports | Line budget |
|------|---------|----------------|-------|---------------|-------------|
| `herness/metrics/__init__.py` | Package marker; docstring only; no re-exports | — | L3 | — | 10 |
| `herness/metrics/settings.py` | Pydantic section models for `metrics.yaml` and `weights.yaml`; literals; weight-confirmation gate; `weight_change` payload | `Period`, `EntityType`, `Unit`, `Better`, `Aggregation`, `UsdModel`, `MetricDef`, `MetricsDefaults`, `ScoringConfig`, `MetricsCatalogConfig`, `WeightsConfig`, `WeightChangePayload`, `WeightIssue`, `check_weight_confirmations`, `unconfirmed_blocks`, `WEIGHT_USES` | L3 (imported by L0 `herness.core.config` under the settings exception, R-03) | only the standard library, pydantic, `herness.core.types` and `herness.core.errors` (R-03); no duckdb, jinja2, sqlglot, and no `herness.core.config` | 390 |
| `herness/metrics/catalog.py` | Catalog object, loader, validator, units and flag vocabularies | `MetricCatalog`, `load_catalog`, `catalog_from_config`, `validate_catalog`, `metrics_owner_validator`, `SCORE_UNITS`, `METRIC_FLAGS`, `SOURCE_FILTERS`; re-exports `Period`, `EntityType`, `Unit`, `MetricDef` | L3 | `sqlglot`, `yaml` | 380 |
| `herness/metrics/_encode.py` | Type-driven cell encoding, row digests, streaming and batch hashing (worker-safe top-level functions) | `encode_cell`, `row_digest`, `HashAccumulator`, `hash_arrow_batch` | L3 | `pyarrow` | 280 |
| `herness/metrics/evidence.py` | Shared `result_hash`, samples, tolerance compare, recorded execution into `meta.evidence` | `result_hash`, `result_sample`, `rows_equivalent`, `iter_batch_rows`, `canonical_params`, `RecordedQuery`, `IntoSpec`, `run_recorded`, `RESULT_SAMPLE_LIMIT`, `MAX_RESULT_ROWS`, `WRITABLE_TABLES` | L3 | `duckdb`, `pyarrow` | 390 |
| `herness/metrics/windows.py` | Pure window and `as_of` arithmetic | `Window`, `resolve_as_of`, `default_window`, `custom_window`, `DEFAULT_PERIOD_COUNTS` | L3 | `zoneinfo` | 200 |
| `herness/metrics/render.py` | Sandboxed Jinja environment, metric and score template rendering, bind parameter construction | `RenderedQuery`, `make_environment`, `render_metric_query`, `render_named`, `default_binds`, `weight_binds`, `BIND_TYPES` | L3 | `jinja2` | 300 |
| `herness/metrics/facts.py` | Stage 400 hook | `materialize_facts`, `split_statements`, `FACT_TABLES` | L3 | `duckdb` | 150 |
| `herness/metrics/compute.py` | Interactive metric API | `MetricRow`, `MetricResult`, `compute_metric`, `metric_series`, `validate_metric_request`; re-exports `peer_group`, `PeerGroupInfo` | L3 | `duckdb` | 340 |
| `herness/metrics/peers.py` | Peer group resolution | `PeerGroupInfo`, `peer_group` | L3 | `duckdb` | 180 |
| `herness/metrics/context.py` | Step inputs and outputs shared by step modules (avoids import cycles) | `StepContext`, `StepResult`, `ScoringReport` | L3 | none | 80 |
| `herness/metrics/scoring.py` | Step runner, validate/metrics/check steps, checkpointing | `ScoringReport`, `run_scoring`, `STEPS`, `run_metrics_step`, `run_check_step`, `missing_required_columns` | L3 | `duckdb` | 390 |
| `herness/metrics/funding.py` | Funding step (attribution and score) | `run_funding_step` | L3 | `duckdb` | 150 |
| `herness/metrics/org.py` | Org step | `run_org_step` | L3 | `duckdb` | 110 |
| `herness/metrics/levers.py` | Levers step | `run_levers_step`, `USD_MODELS`, `LEVER_PLACEHOLDERS` | L3 | `duckdb` | 130 |
| `herness/metrics/_solver.py` | Pure CP-SAT model build/solve and ordering | `PortfolioCandidate`, `SolveOutcome`, `solve_portfolio`, `order_selected`, `binding_constraints` | L3 | `ortools` | 300 |
| `herness/metrics/portfolio.py` | Portfolio API, scenario resolution, persistence | `Scenario`, `PortfolioRow`, `PortfolioResult`, `resolve_scenario`, `optimize_portfolio`, `run_portfolio_step` | L3 | `duckdb` | 360 |
| `herness/metrics/sql/_macros.sql.j2` | Jinja macros: `p`, `entity_col`, `entity_join`, `period_start`, `period_start_date`, `period_end_date`, `period_spine`, `filter_clause`, `entity_filter`, `owner_team`, `cat_at_join`, `cat_at`, `lkp`, `team_bucket`, `observed_days` | — | SQL | — | 260 |
| `herness/metrics/sql/metric_wrapper.sql.j2` | Wraps a catalog SELECT: min-sample rule, flags, identity columns, order | — | SQL | — | 60 |
| `herness/metrics/sql/peer_group.sql.j2` | One recorded peer query | — | SQL | — | 130 |
| `herness/metrics/sql/funding_attribution.sql.j2` | Both allocation passes, stored pass 2 | — | SQL | — | 320 |
| `herness/metrics/sql/funding_score.sql.j2` | Annualization, confidence, weights, effort, priority, WSJF, rank | — | SQL | — | 300 |
| `herness/metrics/sql/org_score.sql.j2` | Robust z, trend, composite, rank | — | SQL | — | 220 |
| `herness/metrics/sql/levers.sql.j2` | Lever targets and `delta_usd` | — | SQL | — | 200 |
| `herness/metrics/sql/portfolio_input.sql.j2` | Optimizer input rows | — | SQL | — | 110 |
| `herness/metrics/sql/checks.sql.j2` | Invariant SELECTs, one per check, split by `-- @check` markers | — | SQL | — | 200 |
| `herness/model/sql/400_facts.sql` | Five fact SELECTs split by `-- @statement` markers | — | SQL (stage 400) | — | 400 |
| `config/metrics.yaml` | Catalog (28 metrics), defaults, scoring section | — | config | — | 760 |
| `config/weights.yaml` | Weights and portfolio section | — | config | — | 45 |

Test-side files (not production, no line budget): `tests/unit/metrics/test_*.py`, `tests/integration/metrics/test_*.py`, `tests/fault/metrics/test_*.py`, `tests/bench/test_metrics_bench.py`, `tests/support/metrics_tiny.py` (builds a DuckDB warehouse from the fixture rows), `tests/support/metrics_oracle.py` (pure-Python reference formulas for property tests), `tests/fixtures/metrics_tiny/*.csv`, `tests/fixtures/result_hash_vectors.json`.

Import-linter additions for `pyproject.toml`: contract "metrics does not import enrich" (forbidden `herness.metrics` → `herness.enrich`, ENG §2.1); contract "settings is light" (forbidden `herness.metrics.settings` → `duckdb`, `jinja2`, `sqlglot`, `ortools`, `pyarrow`, `yaml`, `herness.core.config`, and any other `herness.metrics` module). `herness.core.config` importing `herness.metrics.settings` is the settings exception of R-03 and ENG §2.1; impl 00 owns and encodes that named exception, so this spec adds no exception of its own. Because of R-03, `herness.core.config` cannot import `herness.metrics.catalog`: the catalog SQL cross-check (U04-26) is run by the composition root, not by the config loader (DD04-20).

`herness.metrics` imports these lower-layer modules only: `herness.core.errors`, `herness.core.ids`, `herness.core.time`, `herness.core.logging`, `herness.core.config`, `herness.core.types`, `herness.core.jobs` (type of `JobContext` only), `herness.store.warehouse` (read-only connections only), `herness.store.ops.evidence` (R-13), `herness.store.ops.metrics` (R-12) and `herness.store.ops.shared` (read of approved `weight_change` items, U04-83). It never imports `herness.store._warehouse_rw`: impl 02's `store-rw-restricted` contract allows only `herness.model.build` and `herness.model.promote`, so every write path takes the build pipeline's connection as a required argument (C12). It defines no `herness.store.ops` function (R-09) and owns no ops table, so it has no migration in any R-11 range.

## 3. Unit specs

Conventions for this section:

- "Raises `X`" means the taxonomy class of design 00 §7 imported from `herness.core.errors`.
- "Recorded" means executed through `run_recorded` (U04-10).
- A SQL-file unit describes its computation in prose and tables; the implementer writes the SQL. Every bind parameter is written in SQL as `{{ p('name') }}`, which the macro renders as `CAST($name AS <type>)` with the type from `BIND_TYPES` (U04-36). Values never render into SQL text.
- Every recorded SQL text is single-line-comment free after rendering: templates use Jinja comments `{# #}` only, because `normalize_sql` collapses whitespace (design 00 §5).
- Duration and ratio values are DOUBLE; money columns are `DECIMAL(18,2)` produced by `CAST(<double expression> AS DECIMAL(18,2))` at the last projection (DuckDB rounds half away from zero); sums of `DECIMAL(18,2)` stay exact.

### 3.1 Encoding and result hash (`_encode.py`, `evidence.py`, pure)

#### U04-01 herness.metrics._encode.encode_cell

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Encode one result cell into its canonical JSON token text for hashing (design 00 §5.1), driven by the column's DuckDB type string. |
| Signature | `value: object` (positional; any Python value produced by DuckDB `fetchall` or by `pyarrow` `to_pylist`); `duckdb_type: str` (positional; type string as in the U04-05 header, e.g. `DECIMAL(18,2)`). Returns `str` (one JSON value token). |
| Preconditions | `duckdb_type` is a non-empty string. |
| Postconditions | The returned text is valid JSON. The same logical value yields the same text whether it came from `fetchall` or from Arrow. |
| Invariants | — |
| Algorithm | 1. If `value is None` return `null`. 2. Normalize `duckdb_type`: upper case, remove spaces directly inside parentheses. 3. List types (`T[]`, `T[n]`): element type = text before the last `[`; return `[` + `,`.join(`encode_cell(e, T)` for each element) + `]`. 4. `BOOLEAN`: `true`/`false` from `bool(value)`. 5. Integer family (`TINYINT`, `SMALLINT`, `INTEGER`, `BIGINT`, `HUGEINT`, `UTINYINT`, `USMALLINT`, `UINTEGER`, `UBIGINT`, `UHUGEINT`): `str(int(value))`. 6. `FLOAT`, `REAL`, `DOUBLE`: `x = float(value)`; NaN → the JSON string `"nan"`; ±infinity → `"inf"` / `"-inf"` (quoted); if `x == 0.0` set `x = 0.0` (drops the sign of `-0.0`); return `format(x, ".9g")` unquoted. 7. `DECIMAL(p,s)`: `d = value` if `Decimal` else `Decimal(str(value))`; quantize to `s` places with `ROUND_HALF_EVEN`; a zero becomes positive zero of that scale; return the JSON string of `format(d, "f")`. 8. `DATE`: JSON string of `value.isoformat()`. 9. `TIMESTAMP WITH TIME ZONE` (alias `TIMESTAMPTZ`): `astimezone(timezone.utc)`; naive `TIMESTAMP`, `TIMESTAMP_S`, `TIMESTAMP_MS`, `TIMESTAMP_NS`: treated as UTC; return the JSON string `YYYY-MM-DDTHH:MM:SS.ffffffZ` (always six fractional digits, design 00 §8). 10. `TIME`: JSON string `HH:MM:SS.ffffff`. 11. `INTERVAL`: `timedelta.total_seconds()` encoded as step 6. 12. `VARCHAR`, `UUID`, `JSON`, `ENUM(...)`: JSON string of `str(value)` (`ensure_ascii=False`). 13. `BLOB`: JSON string of standard base64. 14. `STRUCT(...)`, `MAP(...)`, `UNION(...)` and any other type: encode by Python type — `dict` → `{` + `,`.join(JSON key + `:` + encoded value) in insertion order + `}`; `list`/`tuple` → array; `bool`, `int`, `float`, `Decimal`, `str`, `date`, aware or naive `datetime`, `bytes` → as steps 4–13 (`Decimal` keeps its own exponent via `format(d, "f")`); anything else raises. |
| Side effects | none |
| Errors | Unsupported Python type in step 14 → `SchemaViolation("unsupported result value type <type name> for column type <duckdb_type>")`. Conversion failure in steps 5, 7 → `SchemaViolation` naming the column type, raised `from` the original error. |
| Concurrency | Pure; thread- and process-safe. |
| Complexity and limits | O(size of value). |
| Security notes | TH04-12: the header carries types and strings are quoted, so `"1"` and `1` never collide. |
| Tests | UT04-01, UT04-03, PT04-02 |

#### U04-02 herness.metrics._encode.row_digest

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Canonical JSON text and SHA-256 digest of one row. |
| Signature | `names: Sequence[str]`; `types: Sequence[str]`; `row: Sequence[object]` (all positional, equal length). Returns `tuple[bytes, str]` = (32-byte digest, row JSON text). |
| Preconditions | Equal lengths, else `SchemaViolation("row width <n> != column count <m>")`. |
| Postconditions | Row JSON = `{` + `,`.join(`json.dumps(name, ensure_ascii=False)` + `:` + `encode_cell(v, t)`) in column order + `}`. Duplicate column names stay in order (the text is built directly, never through a `dict`). Digest = SHA-256 of the UTF-8 bytes. |
| Invariants | — |
| Algorithm | As postconditions. |
| Side effects | none |
| Errors | Precondition; U04-01 errors. |
| Concurrency | Pure. |
| Complexity and limits | O(row size). |
| Security notes | TH04-12. |
| Tests | UT04-02, PT04-02 |

#### U04-03 herness.metrics._encode.HashAccumulator

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Accumulate row digests and sample candidates of one result so hash and sample can be computed from streamed or parallel batches. |
| Signature | Constructor: `columns: Sequence[tuple[str, str]]` (positional); `sample_limit: int = 50` (keyword-only, 0–1000). Methods: `add_rows(rows: Iterable[Sequence[object]]) -> None`; `add_digests(digests: Iterable[bytes], sample: Iterable[tuple[bytes, str]]) -> None`; `finish() -> tuple[str, int, list[dict[str, object]]]` = (hash hex, row count, sample). |
| Preconditions | Any call after `finish` → `SchemaViolation("accumulator finished")`. |
| Postconditions | `finish` returns exactly `result_hash(columns, rows)` and `result_sample(columns, rows, sample_limit)` for all rows added. |
| Invariants | One digest per row added; the sample heap holds at most `sample_limit` (digest, row JSON) pairs with the smallest digests seen (bounded max-heap). |
| Algorithm | 1. `add_rows`: U04-02 per row; append digest; offer (digest, row JSON) to the heap. 2. `add_digests`: extend digests; offer each sample pair. 3. `finish`: `header = json.dumps([[n, t] for n, t in columns], separators=(",", ":"), ensure_ascii=False)`; sort the digests (byte order equals lowercase-hex order); `body = "\n".join(d.hex() for d in digests)`; `hash = sha256((header + "\n" + body).encode("utf-8")).hexdigest()`; sample = heap pairs sorted by digest, each row JSON parsed with `json.loads` into a `dict` (for duplicate names the last value wins in the sample only). The header and row texts follow the pinned `result_hash` rules owned here (R-15); they deliberately do not use `herness.core.ids.canonical_json`, which sorts keys and would lose column order (U00-29 security note). |
| Side effects | none |
| Errors | Precondition. |
| Concurrency | Not thread-safe; one instance per result. |
| Complexity and limits | About 65 bytes per row (5M rows ≈ 330 MB); O(n log n) sort. |
| Security notes | none |
| Tests | UT04-02, UT04-05, PT04-01 |

#### U04-04 herness.metrics._encode.hash_arrow_batch

| Field | Content |
|-------|---------|
| Kind | function (top-level, runs in a `ProcessPoolExecutor` worker) |
| Purpose | Hash one Arrow IPC record batch in a worker process. |
| Signature | `columns: tuple[tuple[str, str], ...]`; `ipc_bytes: bytes` (one batch in Arrow IPC stream format); `sample_limit: int` (all positional). Returns `tuple[list[bytes], list[tuple[bytes, str]]]` = (digests, up to `sample_limit` smallest (digest, row JSON) pairs). |
| Preconditions | Batch width equals `len(columns)`, else `SchemaViolation`. |
| Postconditions | Output equals what `HashAccumulator.add_rows` accumulates for the same rows. |
| Invariants | — |
| Algorithm | 1. Read the batch with `pyarrow.ipc.open_stream`. 2. Rows via U04-08. 3. U04-02 per row; keep all digests and the `sample_limit` smallest pairs. |
| Side effects | none |
| Errors | U04-01/U04-02 errors. |
| Concurrency | Pure; separate process. |
| Complexity and limits | One batch of 10,000 rows per call. |
| Security notes | Data crosses the process boundary as Arrow IPC bytes only; the executor pickles only `bytes`, `str`, `int` and tuples of them (ENG §3.5). |
| Tests | UT04-12, BT04-09 |

#### U04-05 herness.metrics.evidence.result_hash

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | The single implementation of design 00 §5.1 `result_hash` with the pinned canonical rules of U04-01…U04-03 (R-15); the harness imports it (design 05 §4.6). |
| Signature | `columns: Sequence[tuple[str, str]]` (positional; (name, DuckDB type string) in result order); `rows: Iterable[Sequence[Any]]` (positional; any order). Returns `str` (64 lowercase hex). |
| Preconditions | Type strings are `str()` of the DuckDB relation column types (for example `VARCHAR`, `BIGINT`, `DOUBLE`, `DECIMAL(18,2)`, `TIMESTAMP WITH TIME ZONE`, `VARCHAR[]`); VI04-02. |
| Postconditions | Independent of row order; `-0.0` equals `0.0`; an empty result hashes to `sha256(header + "\n")`. |
| Invariants | — |
| Algorithm | `acc = HashAccumulator(columns, sample_limit=0)`; `acc.add_rows(rows)`; return the hash of `acc.finish()`. |
| Side effects | none |
| Errors | `SchemaViolation` from U04-01/U04-02. |
| Concurrency | Pure. |
| Complexity and limits | See U04-03. |
| Security notes | TH04-06, TH04-12. |
| Tests | UT04-02, UT04-03, UT04-04, PT04-01, ST04-12 |

#### U04-06 herness.metrics.evidence.result_sample

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | The first `limit` rows of a result in design 00 §5.1 sort order (ascending row digest) as JSON-ready dicts with the hash's value encoding (design 04 §4.4 item 4). |
| Signature | `columns: Sequence[tuple[str, str]]`; `rows: Iterable[Sequence[Any]]` (positional); `limit: int = 50` (keyword, 0–1000). Returns `list[dict[str, object]]`. |
| Preconditions | `0 <= limit <= 1000`, else `SchemaViolation`. |
| Postconditions | Length `min(limit, row count)`; element `i` has the `i`-th smallest digest; floats are the value of their `.9g` token (non-finite as strings `nan`, `inf`, `-inf`), DECIMAL and timestamps are strings, lists are lists. |
| Invariants | — |
| Algorithm | `acc = HashAccumulator(columns, sample_limit=limit)`; `acc.add_rows(rows)`; return the sample of `acc.finish()`. |
| Side effects | none |
| Errors | As U04-05. |
| Concurrency | Pure. |
| Complexity and limits | Heap of `limit` entries. |
| Security notes | TH04-04 (templates cannot select text columns, so samples carry none). |
| Tests | UT04-05, UT04-06, IT04-06 |

#### U04-07 herness.metrics.evidence.rows_equivalent

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Design 04 §4.4 Verifier tolerance: decide whether two results of the same query match when their hashes differ. The Verifier (05) calls it for the cell tolerance (R-15, DD04-14). |
| Signature | `columns: Sequence[tuple[str, str]]`; `rows_a: Sequence[Sequence[Any]]`; `rows_b: Sequence[Sequence[Any]]` (all positional). Returns `bool`. |
| Preconditions | none. |
| Postconditions | `True` iff counts are equal and, after sorting both sides by the same key, every cell pair passes: numeric columns (integer family, `FLOAT`, `REAL`, `DOUBLE`, `DECIMAL`) pass when both NULL or `abs(a - b) <= 1e-9 + 1e-6 * max(abs(a), abs(b))` in `float`; every other column passes when the `encode_cell` texts are equal. |
| Invariants | — |
| Algorithm | 1. Different counts → `False`. 2. Sort key per row: tuple over columns of `encode_cell` text for non-numeric columns and `format(float(v), ".6g")` (or `"null"`) for numeric columns. 3. Sort both sides. 4. Compare pairwise; first failing cell → `False`. 5. `True`. |
| Side effects | none |
| Errors | `SchemaViolation` from encoding. |
| Concurrency | Pure. |
| Complexity and limits | O(n log n); callers pass ≤ `MAX_RESULT_ROWS` rows. |
| Security notes | TH04-06. |
| Tests | UT04-11, PT04-11 |

#### U04-08 herness.metrics.evidence.iter_batch_rows

| Field | Content |
|-------|---------|
| Kind | function (generator) |
| Purpose | Turn Arrow record batches into row tuples so the harness feeds `run_sql` batches to `result_hash` (design 05 §5.4.4). Owned here (R-15); impl 05 imports it and does not re-implement it. |
| Signature | `batches: Iterable[pyarrow.RecordBatch]` (positional). Returns `Iterator[tuple[object, ...]]`. |
| Preconditions | Batches share one schema. |
| Postconditions | Rows in batch order, values as `to_pylist()` gives them. |
| Invariants | — |
| Algorithm | Per batch: `cols = [c.to_pylist() for c in batch.columns]`; yield `tuple(col[i] for col in cols)` for each row index. |
| Side effects | none |
| Errors | none |
| Concurrency | Pure. |
| Complexity and limits | One batch materialized at a time. |
| Security notes | none |
| Tests | UT04-12 |

#### U04-09 herness.metrics.evidence.canonical_params

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | JSON-round-tripped `params` used for `query_id`, storage and binding, so the Verifier's re-bind of `params.bind` reproduces the build execution exactly (DD04-09). |
| Signature | `bind: Mapping[str, object]`; `template: Mapping[str, object]` (positional). Returns `dict[str, object]` = `{"bind": {...}, "template": {...}}`. |
| Preconditions | Bind keys match `^[a-z_][a-z0-9_]{0,62}$`, else `ConfigError("bad bind name <k>")`. |
| Postconditions | Values are JSON types only: `Decimal` → string `format(d, "f")`; `date` → ISO string; aware `datetime` → UTC string `YYYY-MM-DDTHH:MM:SS.ffffffZ`; tuples → lists; mapping keys sorted. |
| Invariants | — |
| Algorithm | 1. Convert recursively to JSON types as stated in the postconditions (this pre-conversion fixes the `Decimal` and timestamp text forms used for binding; it is not a second canonical encoder). 2. `text = canonical_json({"bind": b, "template": t})` (T00-05 (herness.core.ids.canonical_json); R-14 forbids a local re-implementation). 3. Return `json.loads(text)`. |
| Side effects | none |
| Errors | Naive datetime, non-finite float or unsupported type → `ConfigError("unsupported bind value for <key>")`, raised in step 1 before `canonical_json` is called; a `SchemaViolation` from `canonical_json` (depth above 64) is re-raised as `ConfigError("params not canonicalisable")` `from` the original. |
| Concurrency | Pure. |
| Complexity and limits | Callers keep the text ≤ 64 KiB (checked in U04-12). |
| Security notes | TH04-01. |
| Tests | UT04-10, IT04-11 |

### 3.2 Recorded execution (`evidence.py`, adapter)

#### U04-10 herness.metrics.evidence.RecordedQuery

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) |
| Purpose | Everything a caller needs to persist evidence and use a result (design 04 §3.1 `RecordedQuery`). |
| Signature | Fields: `query_id: str` (`^q_[0-9a-f]{16}$`); `sql: str` (normalized, as executed); `params: dict[str, object]` (U04-09 output); `build_id: str`; `result_hash: str`; `row_count: int`; `result_sample: list[dict[str, object]]` (≤ 50); `columns: tuple[tuple[str, str], ...]`; `rows: list[tuple[object, ...]] | None` (None when materialized); `executed_at: datetime` (UTC); `duration_ms: int`. Method `to_evidence(run_id: str | None) -> herness.core.types.Evidence` (field mapping 1:1, design 05 §4.6; the type is defined in `herness.core.types.harness`, owner 05, and imported through the `herness.core.types` re-export, R-01). |
| Preconditions | — |
| Postconditions | — |
| Invariants | `rows is None` or `len(rows) == row_count`. |
| Algorithm | `to_evidence` copies fields and sets `run_id`. |
| Side effects | none |
| Errors | none |
| Concurrency | Immutable. |
| Complexity and limits | `rows` ≤ `MAX_RESULT_ROWS`. |
| Security notes | none |
| Tests | UT04-08, UT04-108 |

#### U04-11 herness.metrics.evidence.IntoSpec

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) |
| Purpose | How a recorded SELECT is materialized into a stored table (DD04-08). |
| Signature | Fields: `table: str`; `mode: Literal["replace", "append"]`; `id_column: Literal["query_id", "query_ids"]`; `upstream_query_ids: tuple[str, ...] = ()`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `table ∈ WRITABLE_TABLES`; `upstream_query_ids == ()` when `id_column == "query_id"`; each upstream ID matches `^q_[0-9a-f]{16}$`. Checked in `__post_init__`. |
| Algorithm | Validation only. |
| Side effects | none |
| Errors | `ConfigError("table <t> is not writable by metrics")`; `ConfigError("bad upstream query id")`. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-09: allowlist keeps writes inside `metrics.*` and `score.*`. |
| Tests | UT04-09, ST04-09 |

#### U04-12 herness.metrics.evidence.run_recorded

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Execute one rendered SELECT with bound parameters; compute `query_id`, `result_hash`, `row_count`, `result_sample`; optionally materialize into a table; optionally write `meta.evidence` (design 04 §4.4). |
| Signature | `con: duckdb.DuckDBPyConnection`; `sql: str` (one SELECT or WITH…SELECT); `params: Mapping[str, object]` (keys exactly `bind`, `template`); `producer: Literal["facts", "metrics", "score"] | None` (all positional); `build_id: str | None = None` (keyword-only; None → read `meta.build`); `into: IntoSpec | None = None` (keyword-only); `timeout_s: float | None = None` (keyword-only; None = no timer); `max_rows: int = MAX_RESULT_ROWS` (keyword-only; ignored with `into`). Returns `RecordedQuery`. Signature extension of design 04 §3.1: DD04-01. |
| Preconditions | `into` requires `producer is not None` (stored results are always recorded), else `ConfigError`. Canonical params text ≤ 65,536 bytes, else `ConfigError("params too large")`. |
| Postconditions | `result_hash` equals the hash of re-running `sql` with `params["bind"]` on the same build (for `into`: of the stored rows without the `id_column`). With `producer`, exactly one `meta.evidence` row has this `query_id`. |
| Invariants | — |
| Algorithm | 1. `p = canonical_params(params["bind"], params["template"])`; the 64 KiB check measures `canonical_json(p)` (T00-05 (herness.core.ids.canonical_json)). 2. `norm = normalize_sql(sql)` (T00-05 (herness.core.ids.normalize_sql), R-14); if `norm` contains `--`, `/*` or `;` → `ConfigError("recorded SQL must not contain comments or semicolons")`. 3. `build_id` = argument, else `SELECT build_id FROM meta.build` (exactly one row, else `SchemaViolation("meta.build must hold one row")`). 4. `qid = query_id(norm, p, build_id)` (T00-05 (herness.core.ids.query_id), R-14; this package never computes a `query_id` itself). 5. When `timeout_s` is set, start `threading.Timer(timeout_s, con.interrupt)`; cancel it in `finally`. 6a. No `into`: execute `norm` with named parameters `p["bind"]`; take column names and `str()` of each column type from the result relation; stream Arrow batches of 10,000 rows; per batch extend `rows` (U04-08) and feed `HashAccumulator.add_rows`; more than `max_rows` rows → `QueryError("result too large: more than <max_rows> rows")` with `query_id`. 6b. With `into`: bind `p["bind"]` plus `__query_id` (and `__upstream` for `query_ids`); `replace` executes `CREATE OR REPLACE TABLE <table> AS SELECT s.*, <idexpr> FROM (<norm>) s`; `append` executes `INSERT INTO <table> SELECT s.*, <idexpr> FROM (<norm>) s`; `<idexpr>` = `CAST($__query_id AS VARCHAR) AS query_id` or `list_concat([CAST($__query_id AS VARCHAR)], CAST($__upstream AS VARCHAR[])) AS query_ids` (VI04-01 gives the fallback if DuckDB rejects parameters in DDL). Read back `SELECT * EXCLUDE (<id_column>) FROM <table>`, filtered by `query_id = $__query_id` or `query_ids[1] = $__query_id` for `append`, unfiltered for `replace`. When the read-back count (`SELECT count(*)` with the same filter) is ≥ `HASH_PARALLEL_MIN_ROWS`, serialize each Arrow batch to IPC bytes, hash in a `ProcessPoolExecutor(max_workers=HASH_WORKERS)` via U04-04 in batch order, and merge with `add_digests`; otherwise hash in process. `rows = None`. 7. `(h, n, sample) = acc.finish()`. 8. With `producer`: `INSERT INTO meta.evidence (query_id, sql, params, result_hash, row_count, result_sample, executed_at, producer) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (query_id) DO NOTHING` (params and sample as JSON text); read back the stored `result_hash`; if it differs from `h` → `SchemaViolation("nondeterministic result for <qid>")`. 9. Log `metrics.query.recorded` and record metric samples (§8). 10. Return `RecordedQuery` (`executed_at` from T00-04 (herness.core.time.now), called as `clock.now()` per impl 00's import convention; `duration_ms` from `time.perf_counter`). |
| Side effects | Writes `meta.evidence` (with `producer`) and the `into` table; log; metric samples through T08-05 (herness.store.ops.metrics.record_metric_samples) (R-12). |
| Errors | Timer interrupt → `QueryError("timeout after <timeout_s>s")` with `query_id`. `duckdb.Error` → `QueryError(<DuckDB message>)` with `query_id`; build-path callers re-raise as `SchemaViolation` naming the template. Steps 2, 3, 8 and preconditions as stated. |
| Concurrency | One call per connection at a time; callers pass a per-thread cursor. Worker processes are pure. |
| Complexity and limits | `MAX_RESULT_ROWS`, `HASH_WORKERS`, batch 10,000 rows (U04-13). |
| Security notes | TH04-01, TH04-06, TH04-09. |
| Tests | UT04-07, UT04-08, UT04-09, UT04-10, UT04-66, ST04-06, IT04-01, IT04-11 |

#### U04-13 herness.metrics.evidence constants

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | Limits and allowlists of recorded execution. |
| Signature | `RESULT_SAMPLE_LIMIT: Final[int] = 50`; `MAX_RESULT_ROWS: Final[int] = 1_000_000`; `HASH_PARALLEL_MIN_ROWS: Final[int] = 200_000`; `HASH_WORKERS: Final[int] = min(8, os.cpu_count() or 1)`; `WRITABLE_TABLES: Final[frozenset[str]]` = {`metrics.incident_fact`, `metrics.change_fact`, `metrics.work_item_fact`, `metrics.org_closure`, `metrics.work_item_closure`, `metrics.metric_value`, `score.funding`, `score.funding_attribution`, `score.org`, `score.action_lever`, `score.portfolio`}. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Values are fixed at import. |
| Algorithm | — |
| Side effects | none |
| Errors | none |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-07, TH04-09. |
| Tests | UT04-09 |

### 3.3 Settings and catalog (`settings.py`, `catalog.py`)

`settings.py` imports only the standard library, pydantic, `herness.core.types` and `herness.core.errors` (R-03, ENG §2.1 settings exception). It never imports `herness.core.config`, so nothing in it returns `ConfigIssue` (see U04-82). The literals and `MetricDef` live there so the spec 10 loader can import them without DuckDB; `catalog.py` re-exports them under the design 04 §3.1 names (DD04-17).

#### U04-14 herness.metrics.settings literals

| Field | Content |
|-------|---------|
| Kind | constant (type aliases) |
| Purpose | Closed vocabularies used across the package. |
| Signature | `Period = Literal["week", "month", "quarter", "t12w", "t12m"]`; `EntityType = Literal["service", "team", "org", "work_item", "cluster"]`; `Unit = Literal["count", "usd", "pct", "ratio", "hours", "minutes", "seconds", "days", "score", "rank", "other"]`; `Better = Literal["higher", "lower"]`; `Aggregation = Literal["count", "sum", "mean", "median", "ratio", "snapshot"]`; `Domain = Literal["ops", "change", "delivery", "monitoring", "cost"]`; `UsdModel = Literal["mttr", "repeat", "reopen", "reassign", "sla", "cfr", "noise"]`; `FilterKey = Literal["priority", "service_id", "team_id", "org_id", "cluster_id", "work_item_type", "severity", "change_type"]`; `Source = Literal["incident", "change", "event", "work_item", "metric_daily"]`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `Unit` equals the design 00 §12.1 vocabulary. |
| Algorithm | — |
| Side effects | none |
| Errors | none |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-15 |

#### U04-15 herness.metrics.settings.MetricDef

| Field | Content |
|-------|---------|
| Kind | class (pydantic v2; `frozen=True`, `extra="forbid"`, `strict=True`) |
| Purpose | One catalog entry (design 04 §3.1, §4.1). |
| Signature | Fields exactly as design 04 §3.1: `name: str` (`^[a-z][a-z0-9_]{2,63}$`); `description: str` (1–300 chars); `domain: Domain`; `grains: list[EntityType]` (1–5, unique); `unit: Unit`; `better: Better`; `aggregation: Aggregation`; `min_sample_size: int` (1–100,000); `owner: str` (`^[a-z0-9_-]{1,64}$`); `estimate: bool`; `uses_weights: list[str]` (block names of U04-19); `usd_model: UsdModel | None`; `filters: list[FilterKey]` (unique); `enabled: bool`; `requires_columns: list[str]` (each `^core\.[a-z_]+\.[a-z_]+$`); `sql: str` (1–20,000 chars). |
| Preconditions | — |
| Postconditions | — |
| Invariants | Field constraints above; pydantic `ValidationError` is converted to `ConfigError` by the loaders. |
| Algorithm | — |
| Side effects | none |
| Errors | `ValidationError` (converted). |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-02 (size cap on `sql`). |
| Tests | UT04-13, UT04-15 |

#### U04-16 herness.metrics.settings.MetricsDefaults

| Field | Content |
|-------|---------|
| Kind | class (pydantic; frozen, forbid, strict) |
| Purpose | `metrics.yaml: defaults` (design 04 §4.1). |
| Signature | `min_sample_size: int = 10` (1–100,000); `windows: dict[Literal["week","month","quarter"], int] = {week: 26, month: 24, quarter: 8}` (all three keys, each 1–520); `exclude_incident_states: list[str] = ["canceled"]`; `exclude_close_codes: list[str] = ["Duplicate", "Cancelled", "Not an incident"]`; `max_resolve_days: int = 365` (1–3650); `cluster_min_membership: float = 0.5` (0–1); `change_link_min_score: float = 0.7` (0–1); `repeat_window_days: int = 30` (1–365); `noise_severities: list[Literal["critical","major","minor","warning","info"]] = [critical, major, minor, warning]` (non-empty); `failure_outcomes: list[Literal["unsuccessful","backed_out","successful_with_issues"]] = [unsuccessful, backed_out]` (non-empty); `compute_timeout_s: float = 30.0` (1–300; DD04-06). |
| Preconditions | — |
| Postconditions | — |
| Invariants | As field constraints. |
| Algorithm | — |
| Side effects | none |
| Errors | `ValidationError`. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-19 |

#### U04-17 herness.metrics.settings.ScoringConfig

| Field | Content |
|-------|---------|
| Kind | class (pydantic; frozen, forbid, strict) with nested models `FundingScoring`, `TierWeights`, `OrgScoring`, `PeerGroupScoring`, `LeverScoring` |
| Purpose | `metrics.yaml: scoring` (design 04 §7.1). |
| Signature | `as_of: date | None = None`; `funding: FundingScoring` (`tier_weights: TierWeights` = `cluster_weight: 0.8`, `root_cause_weight: 0.6`, `service_weight: 0.3`, each in (0, 1]; `min_direct_links: int = 1` (1–100); `window_days: int = 365` (30–1095)); `org: OrgScoring` (`min_peer_group: int = 5` (2–100); `trend_weight: float = 0.5` (0–5); `min_weight_coverage: float = 0.5` (0–1); `metrics: dict[str, float]` (1–40 entries, each > 0)); `peer_group: PeerGroupScoring` (`min_service_peers: int = 3` (1–100)); `levers: LeverScoring` (`top_entities: int = 50` (1–10,000); `templates: dict[UsdModel, str]` (all seven keys, each 1–500 chars)). |
| Preconditions | — |
| Postconditions | — |
| Invariants | Checks that need the catalog (scorecard metrics exist and are not count-like; template placeholders) run in U04-26. |
| Algorithm | — |
| Side effects | none |
| Errors | `ValidationError`. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-10 (template length cap). |
| Tests | UT04-19, UT04-22 |

#### U04-18 herness.metrics.settings.MetricsCatalogConfig

| Field | Content |
|-------|---------|
| Kind | class (pydantic; frozen, forbid, strict) |
| Purpose | Root model of `config/metrics.yaml`; spec 10 root field `metrics` (design 10 §3.1). |
| Signature | `version: Literal[1]`; `defaults: MetricsDefaults`; `metrics: list[MetricDef]` (1–200; names unique); `scoring: ScoringConfig`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Unique metric names (validator error "duplicate metric <name>"). |
| Algorithm | — |
| Side effects | none |
| Errors | `ValidationError`. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-13 |

#### U04-19 herness.metrics.settings.WeightsConfig

| Field | Content |
|-------|---------|
| Kind | class (pydantic; frozen, forbid, strict) with block models |
| Purpose | Root model of `config/weights.yaml` (design 04 §7.2); spec 10 root field `weights`. |
| Signature | `version: Literal[1]`; `business_timezone: str` (must construct `zoneinfo.ZoneInfo`). Blocks, each with `unconfirmed: bool`: `cost_per_downtime_hour` (`by_criticality: dict[int, Decimal]`, keys ⊆ {1,2,3,4}, values ≥ 0; `default: Decimal` ≥ 0); `cost_per_engineer_hour` (`value: Decimal` > 0); `hours_per_story_point` (`value: Decimal` > 0); `priority_impact_multiplier` (`values: dict[int, float]`, keys exactly {1..5}, each 0–10); `impact_fallback` (`enabled: bool`; `max_priority: int` 1–5; `outage_fraction: dict[int, float]`, keys ⊇ {1..max_priority}, each 0–1; `cap_hours: float` > 0); `toil` (`effort_factor: dict[int, float]`, keys exactly {1..5}, each 0–10; `business_share: float` 0–1; `max_hours_per_incident: float` > 0; `triage_minutes_per_alert: float` ≥ 0; `reassignment_hours: float` ≥ 0; `reopen_rework_factor: float` ≥ 0); `change` (`backout_effort_hours: float` ≥ 0); `sla_penalty_usd` (`value: Decimal` ≥ 0); `strategic_weights` (`default: float` > 0; `clip: tuple[float, float]`, 0 < min ≤ max; `portfolio: dict[str, float]`, values > 0; `org: dict[str, float]`, values > 0); `expected_reduction` (`epic`, `feature`, `initiative`, `cluster_fix`: float 0–1; `overrides: dict[str, float]`, values 0–1); `cluster_fix` (`effort_hours: float` > 0; `min_incidents_12m: int` ≥ 1; `min_annual_pain_usd: Decimal` ≥ 0; `max_linked_share: float` 0–1); `team_capacity_points_per_quarter` (`default: float` > 0; `teams: dict[str, float]`, values > 0). Non-block section `portfolio: PortfolioConfig` = `horizon_quarters: int` (1–8); `scenarios: list[ScenarioConfig]` (0–20; names unique, never `unconstrained`); `mandatory: list[str]`; `excluded: list[str]`; `enforce_team_capacity: bool`; `solver: SolverConfig` (`num_workers: Literal[1]`; `random_seed: int` ≥ 0; `max_deterministic_time: float` 0.1–600; `max_time_in_seconds: float` 1–600). `ScenarioConfig`: `name` (`^[a-z0-9_]{1,64}$`), `budget_usd: Decimal` (> 0, ≤ 1e12). Method `blocks() -> dict[str, BaseModel]` returns the 14 blocks by name. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `mandatory ∩ excluded = ∅`; clip ordered. Decimal fields accept YAML integers, floats with ≤ 2 decimals and decimal strings (strict mode relaxed on these fields only, because YAML has no decimal type). |
| Algorithm | — |
| Side effects | none |
| Errors | `ValidationError`. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-05 (the `unconfirmed` flags are what U04-22 gates). |
| Tests | UT04-19 |

#### U04-20 herness.metrics.settings.WEIGHT_USES and unconfirmed_blocks

| Field | Content |
|-------|---------|
| Kind | constant and function |
| Purpose | Name the weight blocks each consumer reads (design 04 §5.3 "a value uses a weight when its formula reads it") and compute which are unconfirmed. |
| Signature | `WEIGHT_USES: Final[Mapping[str, tuple[str, ...]]]`; `unconfirmed_blocks(weights: WeightsConfig, blocks: Iterable[str]) -> list[str]` (sorted, unique). |
| Preconditions | Every name passed exists in `WeightsConfig.blocks()`, else `ConfigError("unknown weight block <b>")`. |
| Postconditions | Returns the named blocks with `unconfirmed = true`. |
| Invariants | — |
| Algorithm | `WEIGHT_USES` entries (tuples in this order): `incident_cost` = `cost_per_downtime_hour`, `priority_impact_multiplier`, `impact_fallback`, `toil`, `cost_per_engineer_hour`; `toil` = `toil`, `cost_per_engineer_hour`; `impact` = `impact_fallback`; `noise_cost` = `toil`, `cost_per_engineer_hour`; `backout_cost` = `change`, `cost_per_engineer_hour`; `funding` = union of `incident_cost`, `noise_cost`, `backout_cost` plus `hours_per_story_point`, `strategic_weights`, `expected_reduction`, `cluster_fix`; `lever_mttr` and `lever_repeat` = `incident_cost`; `lever_reopen` = `incident_cost`; `lever_reassign` = `toil`, `cost_per_engineer_hour`; `lever_sla` = `sla_penalty_usd`; `lever_cfr` = union of `incident_cost`, `backout_cost`; `lever_noise` = `noise_cost`; `portfolio` = `funding` plus `team_capacity_points_per_quarter`. Unions keep first-seen order. |
| Side effects | none |
| Errors | `ConfigError` as above. |
| Concurrency | Pure. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-68, UT04-99 |

#### U04-21 herness.metrics.settings.WeightChangePayload

| Field | Content |
|-------|---------|
| Kind | class (pydantic; frozen, forbid, strict) |
| Purpose | Schema of `review_item.payload` for kind `weight_change` (design 02 §5.5 assigns payload schemas to the owning spec). |
| Signature | `blocks: list[str]` (1–20, each a block name); `proposed_config_hash: str | None` (`^cfg_[0-9a-f]{16}$`); `changes: list[WeightChange]` (0–100; `WeightChange` = `path: str` matching `^weights\.[a-z_]+(\.[A-Za-z0-9_:\-]+){0,3}$`, `old: str | None`, `new: str | None`, each ≤ 32 chars matching `^(true|false|-?[0-9]+(\.[0-9]+)?)$`); `origin: Literal["operator", "memory"]`; `memory_id: str | None` (`^mem_[0-9A-HJKMNP-TV-Z]{26}$`, required iff `origin == "memory"`). |
| Preconditions | — |
| Postconditions | — |
| Invariants | No free-text field exists (TH04-05, design 03 §9 rule "payloads carry no text"). |
| Algorithm | — |
| Side effects | none |
| Errors | `ValidationError`. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-05. |
| Tests | UT04-20 |

#### U04-22 herness.metrics.settings.check_weight_confirmations

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Design 04 §7.2 gate: a block may change `unconfirmed` from true to false only with an approved `weight_change` item referencing the new `config_hash`. Called by the impl 04 owner validator U04-83, which impl 10's start-up validation hook runs after `load_config` (R-71); U04-83 converts each returned `WeightIssue` into a `ConfigIssue`. The L0 loader does not call it, because the approved payloads come from the ops store (DD04-20). |
| Signature | `previous: WeightsConfig | None` (last effective weights; None on first load); `current: WeightsConfig`; `new_config_hash: str`; `approved: Sequence[WeightChangePayload]` (payloads of `review_item` rows with `kind='weight_change'`, `status='approved'`; U04-83 reads them, this function does no I/O) — all positional. Returns `list[WeightIssue]` (U04-82). |
| Preconditions | `new_config_hash` matches `^cfg_[0-9a-f]{16}$`, else `ConfigError`. |
| Postconditions | One `WeightIssue` with `severity = "error"` for each block with `current.unconfirmed == false` whose previous value was `true` (or which had no previous config) and which is not listed in `blocks` of any approved payload with `proposed_config_hash == new_config_hash`. Issue `path` = `weights.<block>.unconfirmed`; message "confirming <block> needs an approved weight_change review item for <new_config_hash>". A block already `false` in `previous` needs nothing. |
| Invariants | — |
| Algorithm | Iterate blocks in `blocks()` order; build issues as stated. |
| Side effects | none |
| Errors | Precondition only. |
| Concurrency | Pure. |
| Complexity and limits | — |
| Security notes | TH04-05. |
| Tests | UT04-20, UT04-119, ST04-05 |

#### U04-82 herness.metrics.settings.WeightIssue

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) |
| Purpose | Issue type returned by `check_weight_confirmations`, so `settings.py` needs no import of `herness.core.config` (R-03). U04-83 maps it 1:1 onto `ConfigIssue`. |
| Signature | Fields: `severity: Literal["error", "warn"]`; `path: str` (1–200 chars, `^weights\.[a-z_]+\.unconfirmed$` for the issues U04-22 emits); `message: str` (1–300 chars, built only from block names and the `config_hash`). |
| Preconditions | — |
| Postconditions | — |
| Invariants | Field constraints checked in `__post_init__`; a violation raises `ConfigError("bad weight issue")`. The message holds no weight values (TH04-05, TH04-11). |
| Algorithm | Validation only. |
| Side effects | none |
| Errors | `ConfigError` as stated. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-05. |
| Tests | UT04-20, UT04-119 |

#### U04-23 herness.metrics.catalog.MetricCatalog

| Field | Content |
|-------|---------|
| Kind | class |
| Purpose | Read-only catalog for the harness, CLI and scoring (design 04 §3.1). |
| Signature | Constructor `MetricCatalog(config: MetricsCatalogConfig)`. Attributes `version: str` (12 hex), `defaults: MetricsDefaults`, `scoring: ScoringConfig`. Methods: `get(name: str) -> MetricDef`; `describe() -> list[dict[str, object]]`; `names(*, enabled_only: bool = True) -> list[str]` (sorted). |
| Preconditions | `config` passed U04-26 (the constructor does not re-validate SQL). |
| Postconditions | — |
| Invariants | Immutable; `version = sha256_hex(canonical_json(config.model_dump(mode="json")))[:12]` (T00-05 (herness.core.ids.sha256_hex) and T00-05 (herness.core.ids.canonical_json), R-14). |
| Algorithm | `get`: dict lookup; unknown → `ToolInputError("unknown metric <name>; known: <comma-separated enabled names>")`. `describe`: one dict per metric sorted by name, keys `name`, `description`, `grains`, `unit`, `better`, `min_sample_size`, `filters`, `estimate`, `enabled`. |
| Side effects | none |
| Errors | `ToolInputError`. |
| Concurrency | Immutable; thread-safe. |
| Complexity and limits | O(1) lookup. |
| Security notes | none |
| Tests | UT04-17, UT04-18 |

#### U04-24 herness.metrics.catalog.load_catalog

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Read and validate `config/metrics.yaml` (design 04 §3.1; used by `herness metrics list` and `herness score`). |
| Signature | `path: Path = Path("config/metrics.yaml")` (positional). Returns `MetricCatalog`. |
| Preconditions | Regular file, ≤ 1 MiB, else `ConfigError("catalog file <path> missing or too large")`. |
| Postconditions | The catalog passed U04-26 with no `error` issue. |
| Invariants | — |
| Algorithm | 1. Read UTF-8 text. 2. `yaml.safe_load`. 3. `MetricsCatalogConfig.model_validate` (errors → `ConfigError` listing field paths). 4. `issues = validate_catalog(cfg, weights=get_config().weights)` (T10-03 (herness.core.config.get_config)). 5. Any `error` issue → `ConfigError` joining all messages; log `metrics.catalog.rejected`. 6. Log `metrics.catalog.loaded`. 7. Return `MetricCatalog(cfg)`. |
| Side effects | File read; log. |
| Errors | `ConfigError`. |
| Concurrency | Thread-safe. |
| Complexity and limits | < 2 s. |
| Security notes | TH04-02, TH04-04. |
| Tests | UT04-13, UT04-14 |

#### U04-25 herness.metrics.catalog.catalog_from_config

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | Build the catalog from the already-validated process config (used by `compute_metric`, `peer_group`, `run_scoring`, `optimize_portfolio`). |
| Signature | `cfg: HernessConfig | None = None` (positional; None → `get_config()`). Returns `MetricCatalog`. |
| Preconditions | The process config passed pydantic validation (spec 10 loader). The SQL cross-check U04-26 ran in this process at start-up: the composition root (`herness.cli` for commands and jobs, `app/common` for the dashboard) registers U04-83 on impl 10's start-up validation hook (T10-12 (herness.core.config_validate.register_owner_validator)), runs the hook after `load_config` (T09-20 (herness.cli.run_startup_validation)) and refuses to start on any `error` issue (R-71, DD04-20). `herness.core.config` cannot run it, because R-03 limits its imports to `settings.py` modules. `run_scoring` repeats the check in its validate step (U04-57). |
| Postconditions | — |
| Invariants | — |
| Algorithm | `MetricCatalog(cfg.metrics)`. No caching (ENG §2.3). |
| Side effects | none |
| Errors | none |
| Concurrency | Thread-safe. |
| Complexity and limits | < 5 ms. |
| Security notes | none |
| Tests | UT04-18 |

#### U04-26 herness.metrics.catalog.validate_catalog

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | All catalog rules of design 04 §4.1, §5.8 (scorecard), §5.9 (templates) and §10.5; called by `load_catalog`, by the scoring validate step (U04-56 step 6) and by the owner validator U04-83, which impl 10's start-up validation hook runs at start-up and in `herness config validate` and `herness doctor` (R-71). It is never called from `herness.core.config` (R-03; DD04-20). |
| Signature | `cfg: MetricsCatalogConfig` (positional); `weights: WeightsConfig` (keyword-only). Returns `list[ConfigIssue]`. |
| Preconditions | — |
| Postconditions | Empty list iff valid. Issue `path` = `metrics.<name>.<field>` or `scoring.<...>`. |
| Invariants | — |
| Algorithm | Per metric `m`: 1. `uses_weights` names exist in `weights.blocks()`. 2. Raw `m.sql` has none of these identifiers as whole words (case-insensitive): `description`, `short_description`, `close_notes`, `summary`, `root_cause_text`, `text_redacted`, `text`, `label`, `top_terms`, `alert_name`, `host`, `answer` (DD04-16 adds the last six free-text or model-text columns). 3. Raw `m.sql` contains no `$`, `--`, `/*` or `;` (parameters only through `p()`). 4. For each grain in `m.grains`, render with U04-38 (period `week`, every key of `m.filters` present, `entity_ids` present); parse with `sqlglot.parse(text, read="duckdb")`: exactly one statement; root `Select` or set operation of selects, optionally under `With`; no node of type `Insert`, `Update`, `Delete`, `Merge`, `Create`, `Drop`, `Alter`, `Command`, `Pragma`, `Set`, `Use`, `Attach`, `Detach`, `Copy`, `Export`, `Transaction`, `Load`, `Install`; every table is a CTE name or `schema.table` with schema ∈ {`core`, `enrich`, `metrics`}; no function named `read_*`, `sqlite_*`, `postgres_*`, `pragma_*`, `glob`, `query`, `query_table`, `getenv`, `current_setting`, `load_extension`. A render error in any grain → issue "grain <g> not supported: <macro error>". 5. The catalog SELECT (inner query of the wrapper) outputs exactly `entity_id, period_start, value, numerator, denominator, sample_size`, optionally followed by any ordered subset of `coverage DOUBLE`, `estimated_count BIGINT`, `unweighted BOOLEAN` (DD04-04). 6. Every `m.filters` key is in `SOURCE_FILTERS` of every source the template passed to `filter_clause` (the render context records the sources). 7. `usd_model` set implies `better == "lower"`. Scoring: 8. Each `scoring.org.metrics` key is an enabled metric with `aggregation ∈ {mean, median, ratio}` and grains ⊇ {team, org}; else "scorecard metric <m> is count-like or lacks team/org grain". 9. Each lever template: fields from `string.Formatter().parse`; every field name ∈ `LEVER_PLACEHOLDERS` (U04-71) and matches `^[a-z_]+$` (no attribute or index access); no conversion and no format spec; unbalanced braces → issue. 10. A metric with `usd_model` that is not a scorecard metric → `warn` issue "usd_model unused". |
| Side effects | none |
| Errors | Returns issues; raises nothing on invalid input. |
| Concurrency | Pure. |
| Complexity and limits | ≤ 200 metrics × 5 grains; < 2 s. |
| Security notes | TH04-02, TH04-03, TH04-04, TH04-10. |
| Tests | UT04-14, UT04-15, UT04-16, UT04-21, UT04-22, ST04-02, ST04-04, ST04-10 |

#### U04-83 herness.metrics.catalog.metrics_owner_validator

| Field | Content |
|-------|---------|
| Kind | function (owner validator, R-71) |
| Purpose | The impl 04 owner validator for impl 10's start-up validation hook T10-12 (herness.core.config_validate.register_owner_validator, `run_owner_validators`). It runs the catalog cross-check U04-26 and the weight-confirmation gate U04-22 after `load_config`, outside the L0 loader (R-03, R-71, DD04-20). The composition roots (`herness.cli` and `app/common`, whose start-up call is T09-20 (herness.cli.run_startup_validation)) register it under the name `metrics` before `init_config`. `herness config validate` and `herness doctor` run it through T10-12 (herness.core.config.validate). |
| Signature | `cfg: HernessConfig` (positional); `offline: bool` (keyword-only; not used, the checks do no network I/O). Returns `list[ConfigIssue]` (T10-03 (herness.core.config.ConfigIssue)). Satisfies the `OwnerValidator` protocol of U10-109. |
| Preconditions | `cfg` passed pydantic validation. The ops store ports are bound (R-04). |
| Postconditions | Returns the U04-26 issues unchanged, followed by one `ConfigIssue` per `WeightIssue` (same `severity`, `path` and `message`; `file` = `weights.yaml`). |
| Invariants | Reads only; writes nothing. No issue message holds a weight value or SQL text (TH04-05). |
| Algorithm | 1. `issues = validate_catalog(cfg.metrics, weights=cfg.weights)`. 2. Previous weights: read `<paths.data>/config_snapshots/LAST` and the snapshot `<hash>.yaml` it names (files written by T10-05 (herness.core.audit.record_config_change), which the composition root calls after validation passes, so `LAST` still names the previous effective config); `yaml.safe_load`, then `WeightsConfig.model_validate` of its `weights` section. `LAST` or the snapshot absent (new install) → `previous = None`. Unreadable or invalid snapshot → one `warn` issue at path `weights` "previous config snapshot unreadable; every confirmed block is checked as new", and `previous = None`. 3. `approved` = `WeightChangePayload.model_validate(item.payload)` for each item of T02-07 (herness.store.ops.shared.list_review_items)(kind="weight_change", status="approved", limit=5000); a payload that fails validation is skipped and logged as `metrics.weights.payload_invalid` (WARNING, `item_id`). 4. `weight_issues = check_weight_confirmations(previous, cfg.weights, config_hash(cfg), approved)` (T10-03 (herness.core.config.config_hash)); convert each to `ConfigIssue` as stated; when any has severity `error`, log `metrics.weights.confirmation_rejected` (ERROR: `blocks`, `config_hash`). 5. Return `issues`. |
| Side effects | Reads the config snapshot files and the ops `review_item` table; logs. |
| Errors | Raises nothing for config problems. An ops store error (`StoreBusy`) propagates; U10-109 turns it into one `error` issue `validator metrics failed: StoreBusy`. |
| Concurrency | Called on the process main thread by the hook. |
| Complexity and limits | U04-26 bound (< 2 s) plus one ops read of ≤ 5,000 rows. |
| Security notes | TH04-05, TH04-11. |
| Tests | UT04-120 |

#### U04-27 herness.metrics.catalog.SCORE_UNITS and unit_for

| Field | Content |
|-------|---------|
| Kind | constant and function |
| Purpose | Units of NumberRef-facing columns (design 04 §4.3). |
| Signature | `SCORE_UNITS: Final[Mapping[str, Unit | Literal["metric"]]]` keyed `<schema>.<table>.<column>`; `unit_for(column: str, metric: str | None, catalog: MetricCatalog) -> Unit`. |
| Preconditions | `column` is a key; `"metric"` entries need `metric`. Else `ToolInputError("no unit for <column>")`. |
| Postconditions | — |
| Invariants | Entries: `metrics.metric_value.value`, `score.org.value`, `score.org.peer_median`, `score.action_lever.current_value`, `score.action_lever.target_value` → `metric` (the row's catalog unit); `score.funding.annual_pain_usd`, `score.funding.addressable_pain_usd`, `score.funding.effort_cost_usd`, `score.action_lever.delta_usd`, `score.portfolio.expected_impact_usd`, `score.portfolio.budget_usd`, `score.funding_attribution.pain_usd` → `usd`; `score.funding.confidence`, `score.funding.strategic_weight`, `score.funding.expected_reduction`, `score.funding_attribution.share`, `score.funding.priority` → `ratio`; `score.funding.wsjf`, `score.org.z_score`, `score.org.composite`, `score.org.trend_slope` → `score`; `score.funding.rank`, `score.org.rank`, `score.portfolio.order_rank` → `rank`; `score.funding.n_incidents`, `metrics.metric_value.sample_size`, `score.org.sample_size` → `count`. |
| Algorithm | Lookup; `metric` resolves to `catalog.get(metric).unit`. |
| Side effects | none |
| Errors | `ToolInputError`. |
| Concurrency | Pure. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-117 |

#### U04-28 herness.metrics.catalog.METRIC_FLAGS, SOURCE_FILTERS, LOW_COVERAGE_THRESHOLD

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | Flag vocabulary for `metric_value.flags` and `MetricRow.flags`; filter support matrix. |
| Signature | `METRIC_FLAGS: Final[frozenset[str]]` = {`insufficient_sample`, `estimate`, `unconfirmed_weights`, `low_coverage`, `partial_period`, `unweighted`}; `SOURCE_FILTERS: Final[Mapping[Source, frozenset[FilterKey]]]` per the table below; `LOW_COVERAGE_THRESHOLD: Final[float] = 0.8`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Fixed. |
| Algorithm | — |
| Side effects | none |
| Errors | none |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-01 (filter allowlist). |
| Tests | UT04-16, UT04-68 |

Filter support and the column each filter binds to. `$f_<key>` is always a list; ID filters accept a string or a list (normalized to a list, 1–500 items, each 1–256 printable characters without control characters); `priority` items are integers 1–5; enum filters accept only their enum values (`severity`: critical, major, minor, warning, info; `change_type`: standard, normal, emergency; `work_item_type`: initiative, epic, feature, story, bug, task, subtask).

| Filter key | `incident` (`metrics.incident_fact f`) | `change` (`metrics.change_fact c`) | `event` (`core.event e`) | `work_item` (`metrics.work_item_fact w`) | `metric_daily` (`core.metric_daily m`) |
|------------|------|------|------|------|------|
| `priority` | `f.priority` | — | — | — | — |
| `service_id` | `f.service_id` | `c.service_id` | `e.service_id` | `w.service_id` | `m.service_id` |
| `team_id` | `f.team_id` | `c.team_id` | owner team of `e.service_id` | `w.team_id` | owner team of `m.service_id` |
| `org_id` | `f.org_id` has an ancestor-or-self in the list (via `metrics.org_closure`) | `c.org_id`, same rule | `core.service.org_id` of `e.service_id`, same rule | `w.org_id`, same rule | `core.service.org_id`, same rule |
| `cluster_id` | `f.cluster_id` | — | — | — | — |
| `work_item_type` | — | — | — | `w.type` | — |
| `severity` | — | — | `e.severity` | — | — |
| `change_type` | — | `c.type` | — | — | — |

### 3.4 Windows and rendering (`windows.py`, `render.py`, `sql/_macros.sql.j2`, `sql/metric_wrapper.sql.j2`)

#### U04-29 herness.metrics.windows.Window

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) |
| Purpose | One resolved half-open time window in the business timezone. |
| Signature | Fields: `period: Period`; `start: date` (inclusive, local); `end: date` (exclusive, local); `as_of: date`; `tz: str`; `is_custom: bool`. Properties: `start_ts` / `end_ts` / `as_of_ts` → aware UTC `datetime` of local midnight of `start` / `end` / `as_of` (`datetime.combine(d, time(0), tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)`, `fold=0`). Method `binds() -> dict[str, object]` = `window_start` (`start_ts`), `window_end` (`end_ts`), `window_start_date` (`start`), `window_end_date` (`end`), `as_of` (`as_of`), `as_of_ts` (`as_of_ts`), `tz` (`tz`). |
| Preconditions | — |
| Postconditions | — |
| Invariants | `start < end`; `tz` loads with `ZoneInfo`; checked in `__post_init__` (`ConfigError`). |
| Algorithm | As signature. |
| Side effects | none |
| Errors | `ConfigError`. |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-27, UT04-28 |

#### U04-30 herness.metrics.windows.resolve_as_of

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Design 04 §3.1: `as_of` = date of `meta.build.started_at` in `business_timezone`, overridable by `scoring.as_of`. |
| Signature | `build_started_at: datetime`; `tz: str`; `override: date | None` (positional). Returns `date`. |
| Preconditions | `build_started_at` is aware, else `ConfigError("naive build start time")`. |
| Postconditions | `override` when not None, else `build_started_at.astimezone(ZoneInfo(tz)).date()`. |
| Invariants | — |
| Algorithm | As postconditions. |
| Side effects | none |
| Errors | `ConfigError`. |
| Concurrency | Pure. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-27 |

#### U04-31 herness.metrics.windows.default_window and DEFAULT_PERIOD_COUNTS

| Field | Content |
|-------|---------|
| Kind | function (pure) and constant |
| Purpose | Default windows: the last N complete periods before `as_of` for `week`/`month`/`quarter`; rolling 12 weeks / 12 months ending at `as_of` for `t12w`/`t12m` (design 04 §3.1, §4.3). |
| Signature | `period: Period`; `as_of: date`; `tz: str`; `counts: Mapping[str, int]` (positional; `defaults.windows`). Returns `Window` with `is_custom=False`. `DEFAULT_PERIOD_COUNTS: Final = {"week": 26, "month": 24, "quarter": 8}` (fallback for tests only; production passes `defaults.windows`). |
| Preconditions | `counts` has `week`, `month`, `quarter`. |
| Postconditions | A period is complete when its exclusive end ≤ `as_of`. |
| Invariants | — |
| Algorithm | `week`: `end = as_of − as_of.weekday()` days (Monday on or before `as_of`, matching DuckDB `date_trunc('week')`); `start = end − 7 × counts.week` days. `month`: `end` = first day of `as_of`'s month; `start` = `end` minus `counts.month` calendar months. `quarter`: `end` = first day of `as_of`'s quarter (months 1, 4, 7, 10); `start` = `end` minus `3 × counts.quarter` months. `t12w`: `end = as_of`; `start = as_of − 84` days. `t12m`: `end = as_of`; `start` = same day 12 months earlier, day clamped to the month length (2024-02-29 → 2023-02-28). |
| Side effects | none |
| Errors | Missing key → `ConfigError`. |
| Concurrency | Pure. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-27, UT04-28 |

#### U04-32 herness.metrics.windows.custom_window

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Caller-supplied window `(start, end)`: `start` inclusive, `end` exclusive, both local dates. |
| Signature | `period: Period`; `start: date`; `end: date`; `as_of: date`; `tz: str` (positional). Returns `Window` with `is_custom=True`. |
| Preconditions | `start < end`, else `ToolInputError("window start must be before end")`; `end ≤ start + 36 months` (calendar months, day clamped), else `ToolInputError("window longer than 36 months")`. |
| Postconditions | No alignment to period boundaries; rows whose period is not fully inside the window get flag `partial_period` in the wrapper. |
| Invariants | — |
| Algorithm | Validate; construct. `as_of` is kept for snapshot metrics (snapshot = `min(period_end, as_of)`). |
| Side effects | none |
| Errors | `ToolInputError`. |
| Concurrency | Pure. |
| Complexity and limits | 36-month cap (`MAX_WINDOW_MONTHS: Final = 36`). |
| Security notes | TH04-07. |
| Tests | UT04-29, ST04-07 |

#### U04-33 herness.metrics.render.make_environment

| Field | Content |
|-------|---------|
| Kind | function |
| Purpose | The one Jinja environment for all metric, fact and score SQL (design 04 §9: sandboxed). |
| Signature | No parameters. Returns `jinja2.sandbox.ImmutableSandboxedEnvironment`. |
| Preconditions | — |
| Postconditions | `loader = PackageLoader("herness.metrics", "sql")`; `undefined = StrictUndefined`; `autoescape = False` (SQL, not HTML); `trim_blocks = True`; `lstrip_blocks = True`; no extensions; `globals` emptied except `range`; built-in filters kept. |
| Invariants | A new environment per call (no module-level state). |
| Algorithm | Construct as stated. |
| Side effects | none |
| Errors | none |
| Concurrency | Each caller owns its environment. |
| Complexity and limits | — |
| Security notes | TH04-03. |
| Tests | ST04-03 |

#### U04-34 herness.metrics.render.BIND_TYPES

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | DuckDB type of every bind parameter; `p(name)` renders `CAST($name AS <type>)` so JSON-round-tripped values bind identically in the build and in the Verifier (DD04-09). |
| Signature | `BIND_TYPES: Final[Mapping[str, str]]`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Every name used by any template is a key. Entries (name → type): window binds from U04-29 (`window_start`, `window_end`, `as_of_ts` TIMESTAMPTZ; `window_start_date`, `window_end_date`, `as_of` DATE; `tz` VARCHAR); `entity_ids` VARCHAR[]; `min_n` BIGINT; `static_flags` VARCHAR[]; `metric`, `entity_type`, `period`, `unit` VARCHAR; `f_priority` SMALLINT[]; `f_service_id`, `f_team_id`, `f_org_id`, `f_cluster_id`, `f_work_item_type`, `f_severity`, `f_change_type` VARCHAR[]; defaults `d_exclude_incident_states`, `d_exclude_close_codes`, `d_noise_severities`, `d_failure_outcomes` VARCHAR[], `d_max_resolve_days`, `d_repeat_window_days` INTEGER, `d_cluster_min_membership`, `d_change_link_min_score` DOUBLE; scoring `s_cluster_weight`, `s_root_cause_weight`, `s_service_weight`, `s_trend_weight`, `s_min_weight_coverage` DOUBLE, `s_min_direct_links`, `s_window_days`, `s_min_peer_group`, `s_min_service_peers`, `s_top_entities` INTEGER, `s_org_metrics` VARCHAR[], `s_org_weights` DOUBLE[], `s_lower_better` VARCHAR[] (scorecard metrics with `better = lower`), `s_lever_models_k` VARCHAR[] (metric names with a `usd_model`), `s_lever_models_v` VARCHAR[] (their `usd_model`), `s_templates_k`, `s_templates_v` VARCHAR[], `s_metric_units_k`, `s_metric_units_v` VARCHAR[]; weights (U04-37 names) DOUBLE, DOUBLE[], INTEGER[], VARCHAR[], BOOLEAN as listed there; step binds `unconfirmed` BOOLEAN, `pg_entity_type`, `pg_entity_id`, `pg_metric` VARCHAR, `scenario` VARCHAR, `portfolio_team_k` VARCHAR[], `portfolio_team_v` DOUBLE[], `capacity_default` DOUBLE, `observed_days_min` INTEGER. |
| Algorithm | — |
| Side effects | none |
| Errors | none |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-01. |
| Tests | UT04-26 |

#### U04-35 herness/metrics/sql/_macros.sql.j2

| Field | Content |
|-------|---------|
| Kind | SQL file (Jinja macros) |
| Purpose | The macro contract of design 04 §4.1 plus helpers. Imported into every metric, fact and score render via `make_module(vars=context)`, so macros see `entity_type`, `period`, `filters` and the render state `rs`. |
| Signature | Context: `entity_type: EntityType`, `period: Period`, `filters: dict[FilterKey, str]` (key → bind name `f_<key>`; never values), `rs: RenderState` (Python object: `rs.p(name) -> str`, `rs.set_entity(expr) -> str` (returns empty text), `rs.entity() -> str`, `rs.use_source(source) -> str`, `rs.fail(message)` raises `ConfigError`). Macros: `p(name)`; `entity_col(source, alias)`; `entity_join(source, alias)`; `period_start(ts_expr)`; `period_start_date(date_expr)`; `period_spine()`; `filter_clause(source, alias)`; `entity_filter()`; `owner_team(service_expr, alias)`; `cat_at_join(ts_expr, item_alias, name)`; `cat_at(ts_expr, item_alias, name)`; `lkp(keys, vals, key_expr, default_expr)`; `team_bucket(team_expr)`. |
| Preconditions | `source ∈ Source`; aliases match `^[a-z][a-z0-9_]{0,15}$` (else `rs.fail`). |
| Postconditions | Output is SQL text containing only identifiers from this file, the aliases passed, `CAST($name AS type)` placeholders and SQL keywords. |
| Invariants | No macro emits a value; every runtime value is a `p()` placeholder. |
| Algorithm | `p(name)` → `rs.p(name)`: records `name` in the used set; unknown name → `ConfigError("template uses unknown parameter <name>")`; returns `CAST($<name> AS <BIND_TYPES[name]>)`. `entity_col(source, alias)` → calls `rs.use_source(source)`, computes the entity expression for `entity_type` from the grain table below, calls `rs.set_entity(expr)`, returns `expr`; an unsupported (source, grain) → `rs.fail("grain <g> not supported by source <s>")`. `entity_join(source, alias)` → the join text from the table below (empty when none). `period_start(ts)` → week/month/quarter: `CAST(date_trunc('<period>', <ts> AT TIME ZONE p('tz')) AS DATE)`; `t12w`/`t12m`: `p('window_start_date')`. `period_start_date(d)` → week/month/quarter: `CAST(date_trunc('<period>', <d>) AS DATE)`; `t12w`/`t12m`: `p('window_start_date')`. `period_spine()` → a derived table `spine(period_start DATE, period_end DATE, start_ts TIMESTAMPTZ, end_ts TIMESTAMPTZ, snap_ts TIMESTAMPTZ)`: for week/month/quarter one row per period with `period_start` from `date_trunc('<period>', window_start_date)` stepping one period while `period_start < window_end_date`, `period_end = period_start + 1 period`; for `t12w`/`t12m` one row (`window_start_date`, `window_end_date`); `start_ts`/`end_ts` = local midnight of `period_start`/`period_end` as TIMESTAMPTZ (`timezone(p('tz'), CAST(d AS TIMESTAMP))`); `snap_ts` = local midnight of `least(period_end, as_of)`. `filter_clause(source, alias)` → for each key of `filters` in sorted order: `AND list_contains(p('f_<key>'), <column>)` with the column from the `SOURCE_FILTERS` table; `org_id` → `AND EXISTS (SELECT 1 FROM metrics.org_closure ocf WHERE ocf.org_id = <org column> AND list_contains(p('f_org_id'), ocf.ancestor_org_id))`; a key the source does not support → `rs.fail`. `entity_filter()` → `AND (p('entity_ids') IS NULL OR list_contains(p('entity_ids'), <rs.entity()>))`. `owner_team(service_expr, alias)` → `LEFT JOIN (SELECT service_id, team_id FROM core.service_map WHERE role = 'owner' QUALIFY row_number() OVER (PARTITION BY service_id ORDER BY confidence DESC NULLS LAST, team_id) = 1) <alias> ON <alias>.service_id = <service_expr>`. `cat_at_join(ts, w, name)` → `ASOF LEFT JOIN (SELECT record_id, at, arg_max(to_category, CASE to_category WHEN 'done' THEN 3 WHEN 'in_progress' THEN 2 ELSE 1 END) AS to_category FROM core.work_item_transition GROUP BY record_id, at) <name> ON <name>.record_id = <w>.record_id AND <ts> >= <name>.at` (one category per (record, instant): done beats in_progress beats todo). `cat_at(ts, w, name)` → `coalesce(<name>.to_category, CASE WHEN <w>.created_at <= <ts> THEN 'todo' END)` (NULL means the item did not exist at `ts`). `lkp(keys, vals, key, default)` → `coalesce(list_extract(<vals>, list_position(<keys>, <key>)), <default>)`. `team_bucket(team_expr)` → `CASE` over the minimum `core.service.criticality` of services joined through `core.service_map` rows with `team_id = <team_expr>` and `role IN ('owner','support')`: 1–2 → `'hi'`, 3–4 → `'lo'`, NULL → `'none'`. |
| Side effects | Records used bind names and sources in `rs`. |
| Errors | `ConfigError` via `rs.fail` / `rs.p`. |
| Concurrency | Per-render state. |
| Complexity and limits | — |
| Security notes | TH04-01, TH04-03. |
| Tests | UT04-23, UT04-24, UT04-25 |

Grain resolution (design 04 §4.1 table; aliases suffixed with the source alias `a` to avoid collisions when a template uses two sources):

| Source (alias `a`) | service | team | org | cluster | work_item |
|--------------------|---------|------|-----|---------|-----------|
| `incident` (`metrics.incident_fact`) | `a.service_id` | `a.team_id` | join `metrics.org_closure oc_a ON oc_a.org_id = a.org_id`; `oc_a.ancestor_org_id` | `a.cluster_id` | fail |
| `change` (`metrics.change_fact`) | `a.service_id` | `a.team_id` | closure over `a.org_id` | fail | fail |
| `event` (`core.event`) | `a.service_id` | `owner_team(a.service_id, ow_a)`; `ow_a.team_id` | join `core.service sv_a ON sv_a.service_id = a.service_id` then closure over `sv_a.org_id` | fail | fail |
| `work_item` (`metrics.work_item_fact`) | `a.service_id` | `a.team_id` | closure over `a.org_id` | fail | join `metrics.work_item_closure wc_a ON wc_a.record_id = a.record_id`; `wc_a.ancestor_record_id` |
| `metric_daily` (`core.metric_daily`) | `a.service_id` | owner team as for events | closure over `core.service.org_id` | fail | fail |

#### U04-36 herness.metrics.render.default_binds

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Candidate `d_*` and `s_*` bind values from the catalog config. |
| Signature | `catalog: MetricCatalog` (positional). Returns `dict[str, object]`. |
| Preconditions | — |
| Postconditions | Keys: every `d_*` and `s_*` name of U04-34. Lists sorted ascending except `s_org_metrics`/`s_org_weights` (sorted by metric name, paired), `s_templates_k`/`_v` (sorted by model, paired), `s_lever_models_k`/`_v` (sorted by metric, paired), `s_metric_units_k`/`_v` (sorted by metric, paired). |
| Invariants | — |
| Algorithm | Read the values from `catalog.defaults` and `catalog.scoring`; build pairs. |
| Side effects | none |
| Errors | none |
| Concurrency | Pure. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-26 |

#### U04-37 herness.metrics.render.weight_binds

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Candidate `w_*` bind values (maps become paired key and value lists, looked up with `lkp`). |
| Signature | `weights: WeightsConfig` (positional). Returns `dict[str, object]`. |
| Preconditions | — |
| Postconditions | Keys and sources: `w_downtime_k` INTEGER[] / `w_downtime_v` DOUBLE[] (`cost_per_downtime_hour.by_criticality`), `w_downtime_default` DOUBLE; `w_engineer_hour` DOUBLE; `w_hours_per_point` DOUBLE; `w_prio_mult_k` INTEGER[] / `w_prio_mult_v` DOUBLE[]; `w_fallback_enabled` BOOLEAN; `w_fallback_max_priority` INTEGER; `w_outage_k` INTEGER[] / `w_outage_v` DOUBLE[]; `w_cap_hours` DOUBLE; `w_effort_k` INTEGER[] / `w_effort_v` DOUBLE[]; `w_business_share`, `w_max_toil_hours`, `w_triage_minutes`, `w_reassign_hours`, `w_reopen_factor`, `w_backout_hours`, `w_sla_penalty` DOUBLE; `w_sw_default`, `w_sw_clip_min`, `w_sw_clip_max` DOUBLE; `w_sw_portfolio_k` VARCHAR[] / `w_sw_portfolio_v` DOUBLE[]; `w_sw_org_k` VARCHAR[] / `w_sw_org_v` DOUBLE[]; `w_er_epic`, `w_er_feature`, `w_er_initiative`, `w_er_cluster_fix` DOUBLE; `w_er_override_k` VARCHAR[] / `w_er_override_v` DOUBLE[]; `w_cf_effort_hours` DOUBLE; `w_cf_min_incidents` INTEGER; `w_cf_min_pain` DOUBLE; `w_cf_max_linked_share` DOUBLE; `w_capacity_default` DOUBLE; `w_capacity_k` VARCHAR[] / `w_capacity_v` DOUBLE[]; `w_horizon_quarters` INTEGER. Key lists sorted ascending; value lists paired. Decimals become strings in the canonical params and are cast to DOUBLE in SQL. |
| Invariants | — |
| Algorithm | As postconditions. |
| Side effects | none |
| Errors | none |
| Concurrency | Pure. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-26 |

#### U04-38 herness.metrics.render.RenderedQuery and render_metric_query

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclass) and function (pure) |
| Purpose | Render one metric for one grain, period and window into the wrapped SELECT plus its bind values (design 04 §4.1 template contract). |
| Signature | `RenderedQuery`: `sql: str`, `bind: dict[str, object]`, `template: dict[str, object]`, `sources: frozenset[Source]`. `render_metric_query(metric: MetricDef, *, entity_type: EntityType, window: Window, filters: Mapping[FilterKey, list[object]], entity_ids: Sequence[str] | None, catalog: MetricCatalog, weights: WeightsConfig) -> RenderedQuery` (all keyword-only after `metric`). |
| Preconditions | Request already validated by U04-51. |
| Postconditions | `sql` is the wrapper around the metric's SELECT; `bind` contains exactly the names the template used; `template = {"name": metric.name, "entity_type": entity_type, "period": window.period, "filters": {k: sorted values}}`. |
| Invariants | No value from `filters` or `entity_ids` appears in `sql`. |
| Algorithm | 1. `env = make_environment()`; `rs = RenderState()`. 2. `ctx = {entity_type, period: window.period, filters: {k: "f_" + k for k in filters}, rs}`. 3. `macros = env.get_template("_macros.sql.j2").make_module(vars=ctx)`; add each macro to the render globals. 4. `inner = env.from_string(metric.sql).render(ctx + macros)`. 5. Output columns of `inner` via `sqlglot` (names of the outer projection); optional columns present = ordered subset of (`coverage`, `estimated_count`, `unweighted`). 6. `sql = env.get_template("metric_wrapper.sql.j2").render(inner_sql=inner, optional=<present>, rs, macros)`. 7. `static_flags` = sorted(`estimate` if `metric.estimate`) + (`unconfirmed_weights` if `unconfirmed_blocks(weights, metric.uses_weights)`). 8. Candidate binds = `window.binds()` ∪ `default_binds(catalog)` ∪ `weight_binds(weights)` ∪ {`entity_ids`: sorted unique list or None, `min_n`: `metric.min_sample_size`, `static_flags`, `metric`: name, `entity_type`, `period`, `unit`} ∪ {`f_<k>`: sorted unique values}. 9. `bind = {n: candidates[n] for n in sorted(rs.used)}`; a used name without a candidate → `ConfigError`. 10. Return. |
| Side effects | none |
| Errors | `ConfigError` (render or unknown bind). |
| Concurrency | Pure (per-call environment). |
| Complexity and limits | < 20 ms per render. |
| Security notes | TH04-01, TH04-03. |
| Tests | UT04-24, UT04-25, UT04-26, PT04-12, ST04-01 |

#### U04-39 herness.metrics.render.render_named

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Render a named score, peer or check template. |
| Signature | `name: Literal["peer_group", "funding_attribution", "funding_score", "org_score", "levers", "portfolio_input"] | str` (positional; check names `checks:<check_name>`); `context: Mapping[str, str | int | bool]` (positional; identifiers only, e.g. `entity_type`, `period`); `candidates: Mapping[str, object]` (positional; bind candidates). Returns `RenderedQuery`. |
| Preconditions | `name` in the allowlist or `checks:<name>` with `<name>` a check of U04-60. |
| Postconditions | `template = {"name": name, **context}`; `bind` = used names only. |
| Invariants | — |
| Algorithm | As U04-38 steps 1–4 and 9, using `<name>.sql.j2` (checks: the segment of `checks.sql.j2`, split like U04-46 on `-- @check <name>` lines). |
| Side effects | none |
| Errors | `ConfigError` (unknown name, render error, unknown bind). |
| Concurrency | Pure. |
| Complexity and limits | — |
| Security notes | TH04-03. |
| Tests | UT04-26 |

#### U04-40 herness/metrics/sql/metric_wrapper.sql.j2

| Field | Content |
|-------|---------|
| Kind | SQL file |
| Purpose | Apply the rules shared by every metric so stored `metric_value` rows and `compute_metric` rows are the same recorded SELECT. |
| Signature | Context: `inner_sql` (rendered catalog SELECT), `optional` (present optional columns). Output columns in order: `metric VARCHAR`, `entity_type VARCHAR`, `entity_id VARCHAR`, `period VARCHAR`, `period_start DATE`, `value DOUBLE`, `numerator DOUBLE`, `denominator DOUBLE`, `sample_size BIGINT`, `unit VARCHAR`, `flags VARCHAR[]`. |
| Preconditions | — |
| Postconditions | Rows with `entity_id IS NULL` are dropped. `sample_size` = `coalesce(q.sample_size, 0)`. `value` = `q.value` when `sample_size >= min_n`, else NULL. `flags` = sorted distinct union of `static_flags`; `insufficient_sample` when `sample_size < min_n`; `low_coverage` when `coverage < 0.8`; `estimate` when `estimated_count > 0`; `unweighted` when `unweighted` is true; `partial_period` when the window is custom and (`period_start < window_start_date` or `period_start + 1 period > window_end_date`) for week/month/quarter. Rows ordered by `entity_id, period_start`. `metric`, `entity_type`, `period`, `unit` come from binds. |
| Invariants | — |
| Algorithm | One `SELECT … FROM (<inner_sql>) q WHERE q.entity_id IS NOT NULL ORDER BY entity_id, period_start`. |
| Side effects | none |
| Errors | none |
| Concurrency | — |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-64, UT04-68 |

### 3.5 Facts (stage 400: `herness/model/sql/400_facts.sql`, `facts.py`)

`400_facts.sql` holds five SELECT bodies, each preceded by a marker line `-- @statement metrics.<table>` (the marker lines are removed before rendering, so no comment reaches recorded SQL). `materialize_facts` materializes each with `IntoSpec(table, "replace", "query_id")`, so every row carries its statement's `query_id` (design 04 §4.2). Bind names are those of U04-36/U04-37. Priority NULL is treated as 5 for every weight lookup. Column types are those of design 04 §4.2; the tables below give each column's derivation.

#### U04-41 400_facts.sql — `metrics.org_closure`

| Field | Content |
|-------|---------|
| Kind | SQL file (statement) |
| Purpose | Ancestor-or-self pairs of `core.org` for org roll-ups (design 04 §4.1 "Org grain rolls up descendants"). |
| Signature | Output: `org_id VARCHAR`, `ancestor_org_id VARCHAR`, `depth INTEGER`. |
| Preconditions | `core.org` exists. |
| Postconditions | One row per (org, ancestor) with the minimum depth; every org has its self row at depth 0. |
| Invariants | `depth ≤ 20` (`ORG_MAX_DEPTH`, constant in the SQL; cuts parent cycles). |
| Algorithm | Recursive CTE: seed (`org_id`, `org_id`, 0) for every `core.org` row; step joins the current ancestor to `core.org` and emits (`org_id`, `parent_org_id`, `depth + 1`) while `parent_org_id IS NOT NULL` and `depth < 20`. Final `GROUP BY org_id, ancestor_org_id` with `min(depth)`. |
| Side effects | none (materialized by U04-47) |
| Errors | — |
| Concurrency | — |
| Complexity and limits | Rows ≤ orgs × 21. |
| Security notes | TH04-13 (depth cap). |
| Tests | UT04-34, ST04-13 |

#### U04-42 400_facts.sql — `metrics.work_item_closure`

| Field | Content |
|-------|---------|
| Kind | SQL file (statement) |
| Purpose | Ancestor-or-self pairs over `parent_key`, with each item's nearest funding candidate (design 04 §5.4). |
| Signature | Output: `record_id VARCHAR`, `ancestor_record_id VARCHAR`, `candidate_record_id VARCHAR`, `depth INTEGER`. |
| Preconditions | `core.work_item` exists. |
| Postconditions | One row per (item, ancestor) with minimum depth; self row at depth 0; `candidate_record_id` identical on all rows of one `record_id`. |
| Invariants | `depth ≤ 10` (design 04 §5.4 cycle cut). |
| Algorithm | 1. Recursive CTE: seed (`record_id`, `record_id`, 0); step: join current ancestor `a` to `core.work_item p ON p.key = a.parent_key`, emit (`record_id`, `p.record_id`, `depth + 1`) while `depth < 10`. 2. `GROUP BY record_id, ancestor_record_id` with `min(depth)`. 3. Candidate = ancestor (incl. self) whose `core.work_item` row has `type IN ('initiative','epic','feature')` and `status_category IN ('todo','in_progress')`; per `record_id` pick the one with the smallest depth, ties lowest `ancestor_record_id`; NULL when none; attach to every row of the record. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | Rows ≤ items × 11. Rows at depth 10 are reported by check `score_work_item_cycle` (U04-60). |
| Security notes | TH04-13. |
| Tests | UT04-34, ST04-13 |

#### U04-43 400_facts.sql — `metrics.incident_fact`

| Field | Content |
|-------|---------|
| Kind | SQL file (statement) |
| Purpose | One row per `core.incident` with shared durations, cluster, flags and dollar values (design 04 §4.2, §5.1, §5.3). |
| Signature | Output columns in design 04 §4.2 order, derived per the table below. |
| Preconditions | `core.incident`, `core.team`, `core.service`, `enrich.cluster_member`, `enrich.incident_change_link` exist (the enrich tables may be empty; T02-12 (herness/model/sql/000_settings.sql) creates them). |
| Postconditions | Row count equals `core.incident` row count. |
| Invariants | `resolve_h`, `resolve_bh`, `impact_h`, `toil_h` are NULL or ≥ 0; money columns NULL exactly when `excluded`. |
| Algorithm | Per the column table. `is_repeat` uses a window over non-excluded incidents only. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | One scan of each input plus one window sort; target within BT04-01. |
| Security notes | TH04-04: selects no text column. |
| Tests | UT04-30, UT04-31, PT04-03 |

| Column | Derivation | NULL rule |
|--------|------------|-----------|
| `record_id`, `number`, `opened_at`, `resolved_at`, `priority`, `service_id`, `team_id`, `sla_breached` | copied from `core.incident` | as source |
| `org_id` | `core.team.org_id` of `team_id` (resolving team, D6) | NULL when team unknown |
| `criticality` | `core.service.criticality` of `service_id` | NULL when service unknown |
| `cluster_id`, `membership_prob` | the `enrich.cluster_member` row for this `record_id` with the highest `membership_prob` among rows with `membership_prob ≥ d_cluster_min_membership`; ties lowest `cluster_id` | NULL when no qualifying row (noise point) |
| `excluded` | `coalesce(state, '') IN d_exclude_incident_states OR coalesce(close_code, '') IN d_exclude_close_codes` | never NULL |
| `resolve_h` | `date_diff('second', opened_at, resolved_at) / 3600.0` when `resolved_at IS NOT NULL`, `opened_at IS NOT NULL`, `resolved_at ≥ opened_at` and `date_diff('second', opened_at, resolved_at) ≤ d_max_resolve_days × 86400` | NULL otherwise |
| `resolve_bh` | `business_duration_s / 3600.0` when `resolve_h IS NOT NULL` and `business_duration_s ≥ 0` | NULL otherwise |
| `impact_h` | excluded → NULL; `customer_impact_minutes ≥ 0` → `customer_impact_minutes / 60.0`; else when `w_fallback_enabled`, `coalesce(priority, 5) ≤ w_fallback_max_priority` and `resolve_h IS NOT NULL` → `least(resolve_h × lkp(w_outage_k, w_outage_v, coalesce(priority,5), 0), w_cap_hours)`; else `0` | NULL only when excluded |
| `impact_estimated` | true only in the fallback branch of `impact_h` | never NULL |
| `downtime_usd` | excluded → NULL; else `CAST(impact_h × lkp(w_downtime_k, w_downtime_v, criticality, w_downtime_default) × lkp(w_prio_mult_k, w_prio_mult_v, coalesce(priority,5), 0) AS DECIMAL(18,2))` | NULL only when excluded |
| `toil_h` | excluded → NULL; `base = coalesce(resolve_bh, resolve_h × w_business_share)`; `least(base × lkp(w_effort_k, w_effort_v, coalesce(priority,5), 0), w_max_toil_hours)` | NULL when excluded or `base` NULL (unresolved) |
| `toil_usd` | excluded → NULL; `CAST(coalesce(toil_h, 0) × w_engineer_hour AS DECIMAL(18,2))` | NULL only when excluded |
| `total_usd` | `downtime_usd + toil_usd` | NULL only when excluded |
| `is_repeat` | non-excluded, `cluster_id` and `service_id` NOT NULL, `opened_at` NOT NULL, and `date_diff('second', prev_opened_at, opened_at) ≤ d_repeat_window_days × 86400`, where `prev_opened_at = lag(opened_at) OVER (PARTITION BY cluster_id, service_id ORDER BY opened_at, record_id)` computed over non-excluded incidents only | false otherwise, never NULL |
| `is_reopened` | `coalesce(reopen_count, 0) > 0` | never NULL |
| `is_reassigned` | `coalesce(reassignment_count, 0) > 0` | never NULL |
| `change_caused` | `caused_by_change_id IS NOT NULL` (the change `record_id`) or an `enrich.incident_change_link` row with `incident_id = record_id` and `score ≥ d_change_link_min_score` | never NULL |

Fixture check (design 04 §10.1): with the design's four incidents and weights, the rows give `resolve_h` 2, 4, 6, NULL; I4 `excluded`; `downtime_usd` 15000.00, 0.00, 5000.00; `toil_h` 3.0, 0.8, 3.0; `total_usd` sum 20680.00.

#### U04-44 400_facts.sql — `metrics.change_fact`

| Field | Content |
|-------|---------|
| Kind | SQL file (statement) |
| Purpose | One row per `core.change` (design 04 §4.2, §5.1). |
| Signature | Output columns per design 04 §4.2 order. |
| Preconditions | `metrics.incident_fact` already materialized in this build (statement order). |
| Postconditions | Row count equals `core.change`. |
| Invariants | `failed ⇒ deployed`; `lead_time_h` NULL or ≥ 0; `linked_incident_count ≥ 0`. |
| Algorithm | Per the column table. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | — |
| Security notes | TH04-04. |
| Tests | UT04-32 |

| Column | Derivation |
|--------|------------|
| `record_id`, `type`, `service_id`, `team_id`, `actual_end` | copied from `core.change` |
| `org_id` | `core.team.org_id` of `team_id` |
| `deployed` | `actual_end IS NOT NULL AND coalesce(outcome, '') <> 'canceled'` |
| `linked_incident_count` | number of distinct non-excluded incidents (`metrics.incident_fact`) with `caused_by_change_id = record_id` or an `enrich.incident_change_link` row (`change_id = record_id`, `score ≥ d_change_link_min_score`); 0 when none |
| `failed` | `deployed AND (coalesce(outcome, '') IN d_failure_outcomes OR linked_incident_count > 0)` |
| `lead_time_h` | `date_diff('second', opened_at, actual_end) / 3600.0` when both NOT NULL and `opened_at ≤ actual_end`; else NULL |

#### U04-45 400_facts.sql — `metrics.work_item_fact`

| Field | Content |
|-------|---------|
| Kind | SQL file (statement) |
| Purpose | One row per `core.work_item` with delivery timestamps (design 04 §4.2, §5.1). |
| Signature | Output columns per design 04 §4.2 order. |
| Preconditions | `core.work_item`, `core.work_item_transition`, `core.work_item_link`, `core.team` exist. |
| Postconditions | Row count equals `core.work_item`. |
| Invariants | `cycle_days` NULL or ≥ 0. |
| Algorithm | Per the column table. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | — |
| Security notes | TH04-04 (`summary`, `description` never selected). |
| Tests | UT04-33 |

| Column | Derivation |
|--------|------------|
| `record_id`, `key`, `type`, `parent_key`, `status_category`, `service_id`, `team_id`, `story_points`, `created_at` | copied from `core.work_item` |
| `org_id` | `core.team.org_id` of `team_id`; NULL when `team_id` NULL |
| `first_in_progress_at` | `min(at)` of transitions with `to_category = 'in_progress'` |
| `done_at` | only when `status_category = 'done'`: `max(at)` of transitions with `to_category = 'done'`, else `resolved_at`; NULL for items not currently done (interpretation OI04-07) |
| `cycle_days` | `date_diff('second', first_in_progress_at, done_at) / 86400.0` when both NOT NULL and `done_at ≥ first_in_progress_at`; else NULL |
| `is_unplanned` | `type = 'bug'` or `key` appears as `from_key` or `to_key` of a `core.work_item_link` row with `link_type = 'mentions_incident'` |

#### U04-46 herness.metrics.facts.split_statements and FACT_TABLES

| Field | Content |
|-------|---------|
| Kind | function (pure) and constant |
| Purpose | Split the stage file into (table, SELECT template) segments. |
| Signature | `text: str` (positional). Returns `list[tuple[str, str]]`. `FACT_TABLES: Final[tuple[str, ...]] = ("metrics.org_closure", "metrics.work_item_closure", "metrics.incident_fact", "metrics.change_fact", "metrics.work_item_fact")`. |
| Preconditions | — |
| Postconditions | Segments in file order; tables exactly `FACT_TABLES` in that order. |
| Invariants | — |
| Algorithm | 1. Scan lines; a line matching `^-- @statement (metrics\.[a-z_]+)\s*$` starts a segment. 2. Text before the first marker must be whitespace or Jinja comments only. 3. Each body must be non-blank. 4. Compare the table list with `FACT_TABLES`. |
| Side effects | none |
| Errors | Any violation → `ConfigError("400_facts.sql: <reason>")`. |
| Concurrency | Pure. |
| Complexity and limits | — |
| Security notes | TH04-09 (only the five fact tables can be targets). |
| Tests | UT04-35 |

#### U04-47 herness.metrics.facts.materialize_facts

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | The stage 400 hook called by the spec 02 build runner (design 02 §4.1, 04 §3.1). |
| Signature | `con: duckdb.DuckDBPyConnection` (writable build connection); `build_id: str` (positional). Returns `list[str]` (five `query_id`s in `FACT_TABLES` order). |
| Preconditions | Inputs listed in U04-43…U04-45 plus `meta.build` and `meta.evidence` exist, else `SchemaViolation("stage 400 input <table> missing")`. |
| Postconditions | The five fact tables exist; each row's `query_id` has a `meta.evidence` row with producer `facts`. |
| Invariants | — |
| Algorithm | 1. Check input tables via `information_schema.tables`. 2. Read `400_facts.sql` with `importlib.resources.files("herness.model").joinpath("sql/400_facts.sql")`. 3. `split_statements`. 4. `catalog = catalog_from_config()`; `weights = get_config().weights`; `candidates = default_binds(catalog) ∪ weight_binds(weights)`. 5. `BEGIN TRANSACTION`. 6. For each (table, body): render as U04-39 with context `{}` (no grain), then `run_recorded(con, sql, {"bind": used binds, "template": {"name": "400_facts", "statement": table}}, "facts", build_id=build_id, into=IntoSpec(table, "replace", "query_id"))`; log `metrics.facts.materialized`. 7. `COMMIT`. On any error: `ROLLBACK`; `QueryError` → `SchemaViolation("400_facts.sql statement <table> failed: <message>")`; other taxonomy errors re-raised. |
| Side effects | Writes five `metrics.*` tables and five `meta.evidence` rows; logs. |
| Errors | `SchemaViolation`, `ConfigError`. |
| Concurrency | Single writer (build pipeline job). |
| Complexity and limits | BT04-01: < 90 s at 5M incidents. |
| Security notes | TH04-06, TH04-09. |
| Tests | UT04-35, IT04-01, FT04-05, BT04-01 |

### 3.6 Metric catalog content and compute API (`config/metrics.yaml`, `compute.py`)

#### U04-48 config/metrics.yaml — the 28 metric entries

| Field | Content |
|-------|---------|
| Kind | SQL file (28 catalog templates inside YAML) |
| Purpose | Design 04 §5.2 definitions as catalog entries; each `sql` is the inner SELECT wrapped by U04-40. |
| Signature | Output of each template: `entity_id, period_start, value, numerator, denominator, sample_size` plus the optional columns named in table A. |
| Preconditions | Fact tables materialized. |
| Postconditions | Every ratio in [0, 1]; durations ≥ 0; counts are non-negative integers (except `change_count`, a per-week rate). |
| Invariants | Anchor window: TIMESTAMPTZ anchors satisfy `anchor >= p('window_start') AND anchor < p('window_end')`; DATE anchors use `window_start_date`/`window_end_date`. Every template ends with `filter_clause(...)`, `entity_filter()` and `GROUP BY ALL`. `NX` below means `NOT f.excluded`. "Resolved" means `NX AND f.resolve_h IS NOT NULL`. `median` is DuckDB `median` (interpolated 0.5 quantile). |
| Algorithm | Tables A and B. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | One query per metric × grain × period. |
| Security notes | TH04-04: templates use only the columns named here. |
| Tests | UT04-36…UT04-63 (one per metric, in table order), PT04-03, PT04-04 |

Table A — computation (source alias in parentheses; "count" = `count(*)` over the population):

| # | Metric | Source, anchor | Population (after window) | value | numerator | denominator | sample_size | Optional columns |
|---|--------|----------------|---------------------------|-------|-----------|-------------|-------------|------------------|
| 1 | `incident_count` | incident (f), `f.opened_at` | NX | count | count | NULL | count | — |
| 2 | `p1p2_count` | incident, `opened_at` | NX and `priority <= 2` | count | count | NULL | count | — |
| 3 | `mttr_hours` | incident, `resolved_at` | resolved | `avg(resolve_h)` | `sum(resolve_h)` | `count(resolve_h)` | `count(resolve_h)` | — |
| 4 | `mttr_p50_hours` | incident, `resolved_at` | resolved | `median(resolve_h)` | NULL | `count(resolve_h)` | `count(resolve_h)` | — |
| 5 | `mttr_business_hours` | incident, `resolved_at` | resolved | `avg(resolve_bh)` (NULLs dropped) | `sum(resolve_bh)` | `count(resolve_bh)` | `count(resolve_bh)` | `coverage = count(resolve_bh) / count(*)` |
| 6 | `mtta_minutes` | incident joined to `core.incident i` on `record_id`, `f.opened_at` | NX; `ack = date_diff('second', f.opened_at, i.acknowledged_at) / 60.0` when `i.acknowledged_at ≥ f.opened_at`, else NULL | `avg(ack)` | `sum(ack)` | `count(ack)` | `count(ack)` | `coverage = count(ack) / count(*)` |
| 7 | `customer_impact_minutes` | incident, `opened_at` | NX | `sum(impact_h * 60)` | = value | NULL | count | `estimated_count = count(*) FILTER (impact_estimated)` |
| 8 | `repeat_incident_rate` | incident, `opened_at` | NX and `service_id IS NOT NULL` | num / den | `count(*) FILTER (is_repeat)` | count | count | — |
| 9 | `reopen_rate` | incident, `resolved_at` | resolved | num / den | `count(*) FILTER (is_reopened)` | count | count | — |
| 10 | `reassignment_rate` | incident, `resolved_at` | resolved | num / den | `count(*) FILTER (is_reassigned)` | count | count | — |
| 11 | `sla_breach_rate` | incident, `resolved_at` | resolved and `sla_breached IS NOT NULL` | num / den | `count(*) FILTER (sla_breached)` | count | count | — |
| 12 | `alert_noise_ratio` | event (e), `e.ts` | `list_contains(d_noise_severities, e.severity)` | num / den | `count(*) FILTER (e.incident_id IS NULL)` | count | count | — |
| 13 | `toil_hours_est` | incident, `resolved_at` | resolved | `sum(toil_h)` | = value | NULL | count | — |
| 14 | `incident_cost_usd` | incident, `opened_at` | NX | `CAST(sum(total_usd) AS DOUBLE)` | = value | NULL | count | — |
| 15 | `change_count` | change (c), `c.actual_end` | `deployed` | num / den (deployments per week) | count | weeks in the period ∩ window = `date_diff('day', greatest(ps, window_start_date), least(period_end_date(ps), window_end_date)) / 7.0` (DD04-05) | count | — |
| 16 | `change_failure_rate` | change, `actual_end` | `deployed` | num / den | `count(*) FILTER (failed)` | count | count | — |
| 17 | `change_caused_incident_count` | incident (f) joined to causing changes (c) — pairs (incident, change) from `f.change_caused` sources: `core.incident.caused_by_change_id` and `enrich.incident_change_link` rows with `score ≥ d_change_link_min_score`; entity from the change; anchor `f.opened_at` | NX | `count(DISTINCT f.record_id)` | = value | NULL | = value | — |
| 18 | `change_lead_time_hours` | change, `actual_end` | `deployed` | `median(lead_time_h)` | NULL | `count(lead_time_h)` | `count(lead_time_h)` | `coverage = count(lead_time_h) / count(*)` |
| 19 | `emergency_change_ratio` | change, `actual_end` | `deployed` | num / den | `count(*) FILTER (type = 'emergency')` | count | count | — |
| 20 | `throughput` | work_item (w), `w.done_at` | `type IN ('story','bug','task')` and `done_at IS NOT NULL` | count | count | NULL | count | — |
| 21 | `cycle_time_days` | work_item, `done_at` | as #20 and `cycle_days IS NOT NULL` | `median(cycle_days)` | NULL | count | count | — |
| 22 | `carryover_rate` | work_item × `period_spine()`; `period_start` = `spine.period_start` | types story/bug/task; `cat_at(spine.start_ts) = 'in_progress'` | num / den | `count(*) FILTER (coalesce(cat_at(spine.end_ts), '') <> 'done')` | count | count | — |
| 23 | `backlog_age_days` | work_item × `period_spine()`; snapshot `spine.snap_ts` | types story/bug/task; `cat_at(snap_ts) = 'todo'` | `median(date_diff('second', w.created_at, snap_ts) / 86400.0)` | NULL | count | count | — |
| 24 | `wip_count` | work_item × `period_spine()`; `snap_ts` | types story/bug/task; `cat_at(snap_ts) = 'in_progress'` | count | = value | NULL | count | — |
| 25 | `unplanned_work_ratio` | work_item, `done_at` | as #20 | num / den | `count(*) FILTER (is_unplanned)` | count | count | — |
| 26 | `epic_predictability` | work_item epics (alias e, `type = 'epic'`), anchor `e.done_at` | epics done in the window with ≥ 1 committed child; `e_start = coalesce(e.first_in_progress_at, e.created_at)`; committed children = `metrics.work_item_closure` descendants (`ancestor_record_id = e.record_id`, `depth ≥ 1`) of types story/bug/task with `created_at ≤ e_start`; `pts = coalesce(story_points, 1)`; per epic `committed = Σ pts`, `done = Σ pts` of committed children with `done_at IS NOT NULL AND done_at ≤ e.done_at` | `Σ done / Σ committed` (committed-weighted mean of per-epic ratios) | `Σ done` | `Σ committed` | `count(DISTINCT e.record_id)` | — |
| 27 | `availability_pct` | metric_daily (m), anchor `m.date` (DATE) | `metric_name = 'availability_pct'`; per (`date`, `service_id`): `v = avg(value)` over `source_tool` rows; `rc` = `max(value)` of `metric_name = 'request_count'` rows for the same (`date`, `service_id`) | service grain: `avg(v)`; org grain: `Σ v·rc / Σ rc` when every pair has `rc > 0`, else `avg(v)` | weighted: `Σ v·rc`; else `Σ v` | weighted: `Σ rc`; else count of pairs | `count(DISTINCT date)` | org grain: `unweighted = NOT (every pair has rc > 0)` |
| 28 | `error_rate` | metric_daily, `date` | `metric_name = 'error_rate'`; pairs as #27 | all grains: weighted rule of #27 | as #27 | as #27 | `count(DISTINCT date)` | `unweighted` as #27 |

`period_end_date(ps)` is a helper macro added to U04-35: week/month/quarter → `CAST(ps + INTERVAL 1 <period> AS DATE)`; `t12w`/`t12m` → `p('window_end_date')`. Carryover, backlog and WIP use `cat_at_join` twice or once with names `cs`, `ce`, `cn`.

Table B — catalog metadata (`owner`: `sre-analytics` for domains ops, monitoring and cost; `change-analytics` for change; `delivery-analytics` for delivery; `enabled: true` for all):

| # | Metric | Domain | Grains | Aggregation | Unit | Better | Min n | Estimate | uses_weights | usd_model | Filters | requires_columns |
|---|--------|--------|--------|-------------|------|--------|-------|----------|--------------|-----------|---------|------------------|
| 1 | `incident_count` | ops | service, team, org, cluster | count | count | lower | 1 | no | — | — | P S T O C | — |
| 2 | `p1p2_count` | ops | service, team, org, cluster | count | count | lower | 1 | no | — | — | P S T O C | — |
| 3 | `mttr_hours` | ops | service, team, org, cluster | mean | hours | lower | 10 | no | — | mttr | P S T O C | — |
| 4 | `mttr_p50_hours` | ops | service, team, org, cluster | median | hours | lower | 10 | no | — | mttr | P S T O C | — |
| 5 | `mttr_business_hours` | ops | service, team, org, cluster | mean | hours | lower | 10 | no | — | mttr | P S T O C | — |
| 6 | `mtta_minutes` | ops | service, team, org | mean | minutes | lower | 10 | no | — | — | P S T O | `core.incident.acknowledged_at` |
| 7 | `customer_impact_minutes` | ops | service, team, org, cluster | sum | minutes | lower | 1 | no | `impact_fallback` | — | P S T O C | — |
| 8 | `repeat_incident_rate` | ops | service, team, org, cluster | ratio | ratio | lower | 20 | no | — | repeat | P S T O C | — |
| 9 | `reopen_rate` | ops | service, team, org, cluster | ratio | ratio | lower | 20 | no | — | reopen | P S T O C | — |
| 10 | `reassignment_rate` | ops | service, team, org, cluster | ratio | ratio | lower | 20 | no | — | reassign | P S T O C | — |
| 11 | `sla_breach_rate` | ops | service, team, org, cluster | ratio | ratio | lower | 20 | no | — | sla | P S T O C | — |
| 12 | `alert_noise_ratio` | monitoring | service, team, org | ratio | ratio | lower | 50 | no | — | noise | S T O V | — |
| 13 | `toil_hours_est` | ops | service, team, org, cluster | sum | hours | lower | 1 | yes | `toil` | — | P S T O C | — |
| 14 | `incident_cost_usd` | cost | service, team, org, cluster | sum | usd | lower | 1 | yes | `cost_per_downtime_hour`, `priority_impact_multiplier`, `impact_fallback`, `toil`, `cost_per_engineer_hour` | — | P S T O C | — |
| 15 | `change_count` | change | service, team, org | ratio | count | higher | 1 | no | — | — | S T O X | — |
| 16 | `change_failure_rate` | change | service, team, org | ratio | ratio | lower | 10 | no | — | cfr | S T O X | — |
| 17 | `change_caused_incident_count` | change | service, team, org | count | count | lower | 1 | no | — | — | S T O X | — |
| 18 | `change_lead_time_hours` | change | service, team, org | median | hours | lower | 10 | no | — | — | S T O X | `core.change.opened_at` |
| 19 | `emergency_change_ratio` | change | service, team, org | ratio | ratio | lower | 10 | no | — | — | S T O X | — |
| 20 | `throughput` | delivery | service, team, org, work_item | count | count | higher | 1 | no | — | — | S T O W | — |
| 21 | `cycle_time_days` | delivery | service, team, org, work_item | median | days | lower | 10 | no | — | — | S T O W | — |
| 22 | `carryover_rate` | delivery | team, org, work_item | ratio | ratio | lower | 10 | no | — | — | S T O W | — |
| 23 | `backlog_age_days` | delivery | service, team, org, work_item | median | days | lower | 10 | no | — | — | S T O W | — |
| 24 | `wip_count` | delivery | team, org, work_item | snapshot | count | lower | 1 | no | — | — | S T O W | — |
| 25 | `unplanned_work_ratio` | delivery | service, team, org | ratio | ratio | lower | 20 | no | — | — | S T O W | — |
| 26 | `epic_predictability` | delivery | team, org, work_item | ratio | ratio | higher | 3 | no | — | — | S T O | — |
| 27 | `availability_pct` | monitoring | service, org | mean | pct | higher | 7 | no | — | — | S T O | — |
| 28 | `error_rate` | monitoring | service, org | mean | ratio | lower | 7 | no | — | — | S T O | — |

Filter letters: P `priority`, S `service_id`, T `team_id`, O `org_id`, C `cluster_id`, V `severity`, X `change_type`, W `work_item_type`. Metric #17 applies `filter_clause('change', 'c')`.

#### U04-49 herness.metrics.compute.MetricRow

| Field | Content |
|-------|---------|
| Kind | class (pydantic; frozen, forbid, strict) |
| Purpose | One metric row (design 04 §3.1). |
| Signature | `entity_id: str`; `period_start: date`; `value: float | None`; `numerator: float | None`; `denominator: float | None`; `sample_size: int` (≥ 0); `flags: list[str]` (each ∈ `METRIC_FLAGS`, sorted). |
| Preconditions | — |
| Postconditions | — |
| Invariants | As field constraints. |
| Algorithm | Built from wrapper rows (U04-40). |
| Side effects | none |
| Errors | `ValidationError` (converted to `SchemaViolation` by the caller). |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-64 |

#### U04-50 herness.metrics.compute.MetricResult

| Field | Content |
|-------|---------|
| Kind | class (pydantic; frozen, forbid, strict) |
| Purpose | Result of `compute_metric` with everything the tool layer persists to ops `evidence` (design 04 §3.1). |
| Signature | `metric: str`; `entity_type: EntityType`; `period: Period`; `unit: Unit`; `better: Better`; `rows: list[MetricRow]`; `query_id: str`; `sql: str`; `params: dict[str, Any]`; `result_hash: str`; `result_sample: list[dict[str, Any]]` (≤ 50); `row_count: int`; `build_id: str`; `catalog_version: str`; `flags: list[str]` (sorted union of the static flags). Method `recorded() -> RecordedQuery` rebuilds the `RecordedQuery` for `to_evidence`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `row_count == len(rows)`. |
| Algorithm | — |
| Side effects | none |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-64, UT04-67 |

#### U04-51 herness.metrics.compute.validate_metric_request

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | All input checks of design 04 §6 row 1 before any SQL runs. |
| Signature | `catalog: MetricCatalog`; `name: str`; `entity_type: str`; `entity_ids: Sequence[str] | None`; `period: str`; `filters: Mapping[str, Any] | None` (all positional). Returns `tuple[MetricDef, dict[FilterKey, list[object]], list[str] | None]` (normalized filters, sorted unique entity IDs). |
| Preconditions | — |
| Postconditions | Returned values are safe to bind. |
| Invariants | — |
| Algorithm | 1. `metric = catalog.get(name)`; `metric.enabled` false → `ToolInputError("metric <name> is disabled")`. 2. `period` ∉ `Period` → `ToolInputError("bad period <p>; allowed: week, month, quarter, t12w, t12m")`. 3. `entity_type` ∉ `metric.grains` → `ToolInputError("grain <g> not supported by <name>; allowed: <grains>")`. 4. `entity_ids`: None allowed; empty → `ToolInputError("entity_ids must be null or non-empty")`; > 500 → `ToolInputError("at most 500 entity_ids")`; each 1–256 printable characters without control characters. 5. Filters: each key ∈ `metric.filters`, else `ToolInputError("filter <k> not allowed for <name>; allowed: <filters>")`; values normalized to lists (scalars wrapped), 1–500 items, per-key type and enum checks of U04-28; `priority` items integers 1–5. |
| Side effects | none |
| Errors | `ToolInputError` as listed. |
| Concurrency | Pure. |
| Complexity and limits | O(ids + filter values). |
| Security notes | TH04-01, TH04-07. |
| Tests | UT04-65, ST04-01, ST04-07 |

#### U04-52 herness.metrics.compute.compute_metric

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Compute one metric for one grain and period over the default or a custom window on a read-only warehouse (design 04 §3.1). |
| Signature | As design 04 §3.1: `name: str`, `entity_type: EntityType`, `entity_ids: Sequence[str] | None`, `period: Period`, `filters: Mapping[str, Any] | None = None` (positional-or-keyword); `window: tuple[date, date] | None = None`, `con: duckdb.DuckDBPyConnection | None = None` (keyword-only). Returns `MetricResult`. |
| Preconditions | Config loaded (T10-03 (herness.core.config.get_config)). |
| Postconditions | Nothing is written to the warehouse or the ops store. Rows ordered by (`entity_id`, `period_start`). |
| Invariants | — |
| Algorithm | 1. `catalog = catalog_from_config()`; `weights = get_config().weights`. 2. `validate_metric_request`. 3. Connection: `con` as given (not closed here), else open `CURRENT` read-only with T02-09 (herness.store.warehouse.open_readonly)(None) (closed on exit). 4. Required columns: for each `requires_columns` entry, check presence in `information_schema.columns`; missing → `ToolInputError("metric <name> needs <column>, absent in this build")`. 5. Read `meta.build` (`build_id`, `started_at`); `as_of = resolve_as_of(started_at, weights.business_timezone, catalog.scoring.as_of)`. 6. Window: `custom_window(period, *window, as_of, tz)` when `window` is given, else `default_window(period, as_of, tz, catalog.defaults.windows)`. 7. `render_metric_query`. 8. `rq = run_recorded(con, sql, {"bind", "template"}, None, build_id=build_id, timeout_s=catalog.defaults.compute_timeout_s)`. 9. Build `MetricRow`s from `rq.rows` (columns by name from the wrapper). 10. Log `metrics.compute.completed`; count metric `herness_metrics_compute_calls_total{outcome}`. 11. Return `MetricResult` with `rq` fields, `catalog.version`, and `flags` = sorted static flags. |
| Side effects | Read-only queries; log; metric sample. |
| Errors | `ToolInputError` (steps 2, 4, 6); `QueryError` (timeout 30 s default, DuckDB error); `StoreBusy` from opening the warehouse (T02-09 (herness.store.warehouse.open_readonly)). |
| Concurrency | Thread-safe when each thread passes its own cursor or `con=None`. |
| Complexity and limits | p95 < 2 s (BT04-07); ≤ 500 IDs; ≤ 36-month window; ≤ `MAX_RESULT_ROWS` rows. |
| Security notes | TH04-01, TH04-07, TH04-09 (read-only connection). |
| Tests | UT04-64…UT04-70, UT04-36…UT04-63, ST04-01, ST04-07, IT04-09, BT04-07 |

#### U04-53 herness.metrics.compute.metric_series

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Series for spec 07 outcome baselines: exactly one query and one `query_id` (design 04 §3.1). |
| Signature | As design 04 §3.1: `metric: str`, `entity_type: EntityType`, `entity_ids: Sequence[str] | None`, `start: date`, `end: date`, `period: Period = "week"` (positional-or-keyword); `filters: Mapping[str, Any] | None = None`, `con: duckdb.DuckDBPyConnection | None = None`, `on_evidence: Callable[[RecordedQuery], None] | None = None` (keyword-only; DD04-03). Returns `tuple[list[MetricRow], str]`. |
| Preconditions | As U04-52. |
| Postconditions | Rows and `query_id` equal those of `compute_metric(..., window=(start, end))`; rows ordered by (`entity_id`, `period_start`). `on_evidence`, when given, is called once with the `RecordedQuery` so the caller can write ops `evidence`. |
| Invariants | — |
| Algorithm | `r = compute_metric(metric, entity_type, entity_ids, period, filters, window=(start, end), con=con)`; call `on_evidence(r.recorded())` when set; return `(r.rows, r.query_id)`. |
| Side effects | As U04-52; plus the callback. |
| Errors | As U04-52. |
| Concurrency | As U04-52. |
| Complexity and limits | As U04-52. |
| Security notes | As U04-52. |
| Tests | UT04-67, IT04-03 |

### 3.7 Scoring runner (`context.py`, `scoring.py`, `sql/checks.sql.j2`)

#### U04-54 herness.metrics.context.StepContext, StepResult and ScoringReport

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclasses; `ScoringReport` is a pydantic model, frozen, forbid, strict) |
| Purpose | Inputs shared by all steps, per-step outcome, and the `run_scoring` report (design 04 §3.1). `context.py` exists so step modules import it without importing `scoring.py` (no cycles). |
| Signature | `StepContext`: `build_id: str`; `catalog: MetricCatalog`; `weights: WeightsConfig`; `as_of: date`; `tz: str`; `disabled_metrics: frozenset[str]`; method `binds() -> dict[str, object]` = `default_binds(catalog) ∪ weight_binds(weights) ∪ default_window("t12w", as_of, tz, …).binds() ∪ {s_count_metrics, s_unconfirmed_models, unconfirmed}` (the last three computed from the catalog and `unconfirmed_blocks`). `StepResult`: `row_counts: dict[str, int]`; `warnings: list[str]`; `flags: list[str]`; `failed_checks: list[str]`. `ScoringReport`: `build_id: str`; `steps_done: list[str]`; `row_counts: dict[str, int]` (per table); `duration_ms: dict[str, int]` (per step); `flags: list[str]`; `warnings: list[str]`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Immutable. `unconfirmed` bind is the step-specific value set by each step (funding: `WEIGHT_USES["funding"]`; org: false; levers: per model through `s_unconfirmed_models`; portfolio: `WEIGHT_USES["portfolio"]`). |
| Algorithm | — |
| Side effects | none |
| Errors | none |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-110 |

`BIND_TYPES` (U04-34) also contains `s_count_metrics` VARCHAR[] (metrics with aggregation `count` or `snapshot`) and `s_unconfirmed_models` VARCHAR[] (lever models whose `WEIGHT_USES["lever_<model>"]` has an unconfirmed block).

#### U04-55 herness.metrics.scoring.STEPS

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | Step order of design 04 §3.3. |
| Signature | `STEPS: Final[tuple[str, ...]] = ("validate", "metrics", "funding", "org", "levers", "portfolio", "check")`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Fixed order. |
| Algorithm | — |
| Side effects | none |
| Errors | none |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-110 |

#### U04-56 herness.metrics.scoring.run_scoring

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Run the scoring steps on the writable build file, resumable through the job checkpoint (design 04 §3.1, §3.3, §6). Its production caller is the `build_pipeline` job handler (impl 02), which receives `ctx: JobContext` (R-42) and passes `con` and `ctx`. `herness score` never calls it directly: the CLI enqueues that job by default, and the admin-only `--inline` flag runs the same job in-process through `herness.core.jobs.run_inline`, which also supplies a `JobContext` (R-45, F04-14). |
| Signature | `build_id: str` (positional); `steps: Sequence[str] | None = None` (keyword-only); `con: duckdb.DuckDBPyConnection` (keyword-only, required: the build pipeline's writable connection; this package cannot open a build file for writing, C12); `ctx: herness.core.jobs.JobContext | None = None` (keyword-only; checkpointing only when given). `con` and `ctx` are DD04-02. Returns `ScoringReport`. |
| Preconditions | `meta.build` row `build_id` exists with `status = 'building'`, else `ConfigError("build <id> is <status>; scoring runs only before promotion")`. Every name in `steps` ∈ `STEPS`, else `ConfigError("unknown scoring step <s>")`. |
| Postconditions | Every requested step's tables are fully rewritten; `ScoringReport.steps_done` lists steps completed in this call or earlier (checkpoint). |
| Invariants | `validate` runs on every call (never checkpointed). `check` runs when `steps is None` or it is listed. |
| Algorithm | 1. Requested = `STEPS` when None, else the listed names in `STEPS` order, `validate` always first. 2. Connection: `con` (never closed here); a read-only `con` (`current_setting('access_mode')` is `read_only`) → `ConfigError("run_scoring needs the build pipeline's writable connection")`. 3. Read `meta.build` and check the precondition. 4. `cfg = get_config()`; `catalog = catalog_from_config(cfg)`; `as_of = resolve_as_of(started_at, weights.business_timezone, catalog.scoring.as_of)`. 5. Checkpoint: `state = ctx.load_state()` when `ctx`; `done = state["scoring"]["steps_done"]` when `state["scoring"]["build_id"] == build_id` and `state["scoring"]["config_hash"] == config_hash(cfg)` (T10-03 (herness.core.config.config_hash)), else empty. 6. Validate step (U04-57): fact tables present (`SchemaViolation("fact tables missing; stage 400 did not run")`), disabled metrics, `validate_catalog` errors (`ConfigError`); build `StepContext`. 7. For each remaining requested step: when in `done`, log `metrics.scoring.step_skipped` and continue; else log `metrics.scoring.step_started`; `BEGIN TRANSACTION`; call the step (`run_metrics_step`, `run_funding_step`, `run_org_step`, `run_levers_step`, `run_portfolio_step`, `run_check_step`); `COMMIT`; on error `ROLLBACK`, convert `QueryError` to `SchemaViolation("<step> failed: <message>")`, log `metrics.scoring.step_failed`, re-raise. 8. After commit: add to `done`; when `ctx`, `ctx.save_state({**state, "scoring": {"build_id", "config_hash", "steps_done": done}})` and `ctx.heartbeat("scoring:<step>")`; log `metrics.scoring.step_completed`; record `herness_metrics_step_duration_seconds`. 9. If `check` returned `failed_checks`, raise `SchemaViolation("scoring invariants failed: <names>")` after its commit. 10. When `ctx` and `ctx.should_yield()` after a step: return the report with flag `yielded` (the caller returns the spec 08 `yield` outcome). 11. Log `metrics.scoring.completed`; return the report. |
| Side effects | Writes `metrics.metric_value`, `score.*`, `meta.evidence`, `meta.dq_result`; job state via `ctx.save_state`; logs; metric samples. |
| Errors | `ConfigError`, `SchemaViolation`. The connection is opened by the caller (impl 02 `open_for_build`), so `StoreBusy` arises there, not here. |
| Concurrency | Single writer process per build file (design 00 §4). |
| Complexity and limits | BT04-06: stage 400 + full run < 5 min. |
| Security notes | TH04-09 (writes only allowlisted tables). |
| Tests | UT04-110, UT04-111, UT04-112, UT04-113, UT04-116, FT04-01, FT04-03, IT04-01 |

#### U04-57 herness.metrics.scoring.missing_required_columns and the validate step

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Decide which metrics are disabled for this build (design 04 §6 row "requires_columns missing") and write the `score_metric_disabled` warnings. |
| Signature | `missing_required_columns(con: DuckDBPyConnection, catalog: MetricCatalog) -> dict[str, list[str]]` (metric → missing columns). Private `_validate_step(con, catalog, weights) -> frozenset[str]` (disabled metric names). |
| Preconditions | — |
| Postconditions | A metric is disabled when any `requires_columns` entry is absent from `information_schema.columns` or has zero non-NULL values in its table. `meta.dq_result` holds one `warn` row `score_metric_disabled` per disabled metric (`value` = number of missing columns, `threshold` = 0, `passed` = false, `details` = `{"metric", "columns"}`), after deleting earlier rows of that check name. |
| Invariants | — |
| Algorithm | 1. For each enabled metric with `requires_columns`: check presence, then `SELECT count(<column>) FROM <table>` (identifiers from the validated pattern, quoted with DuckDB identifier quoting). 2. Write the rows. 3. Log `metrics.scoring.metric_disabled` per metric. 4. Check fact tables (`FACT_TABLES`) exist. |
| Side effects | `meta.dq_result` rows; logs. |
| Errors | `SchemaViolation` (fact tables). |
| Concurrency | Build writer. |
| Complexity and limits | One `count` per required column. |
| Security notes | Identifiers validated by pattern (U04-15) and quoted (ENG §3.5). |
| Tests | UT04-113, UT04-114 |

#### U04-58 herness.metrics.scoring.run_metrics_step

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Fill `metrics.metric_value` for every enabled, non-disabled metric × grain × period, over the default windows plus `t12w` and `t12m` at `as_of` (design 04 §4.3). |
| Signature | `con: DuckDBPyConnection`; `sc: StepContext` (positional). Returns `StepResult`. |
| Preconditions | Inside the caller's transaction. |
| Postconditions | Table rewritten; each row's `query_id` in `meta.evidence` (producer `metrics`). |
| Invariants | — |
| Algorithm | 1. `CREATE OR REPLACE TABLE metrics.metric_value (metric VARCHAR NOT NULL, entity_type VARCHAR NOT NULL, entity_id VARCHAR NOT NULL, period VARCHAR NOT NULL, period_start DATE NOT NULL, value DOUBLE, numerator DOUBLE, denominator DOUBLE, sample_size BIGINT NOT NULL, unit VARCHAR NOT NULL, flags VARCHAR[] NOT NULL, query_id VARCHAR NOT NULL)`. 2. For metric in `sc.catalog.names()` minus `sc.disabled_metrics` (sorted), grain in `metric.grains` (catalog order), period in (`week`, `month`, `quarter`, `t12w`, `t12m`): `window = default_window(period, sc.as_of, sc.tz, sc.catalog.defaults.windows)`; `render_metric_query(..., filters={}, entity_ids=None)`; `run_recorded(con, sql, params, "metrics", build_id=sc.build_id, into=IntoSpec("metrics.metric_value", "append", "query_id"))`; a `QueryError` becomes `SchemaViolation("metric <m> grain <g> period <p> failed: <message>")`. 3. Row count. |
| Side effects | Table and evidence writes. |
| Errors | `SchemaViolation`. |
| Concurrency | Build writer. |
| Complexity and limits | About 28 × 3.5 × 5 ≈ 490 queries; BT04-02 < 120 s. |
| Security notes | none |
| Tests | UT04-118, BT04-02 |

#### U04-59 herness.metrics.scoring.run_check_step

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Run the design 04 §10.2 invariants and write `meta.dq_result` rows prefixed `score_` (design 04 §3.3 `check`). |
| Signature | `con: DuckDBPyConnection`; `sc: StepContext` (positional). Returns `StepResult` with `failed_checks` = names of failed `error` checks. |
| Preconditions | Inside the caller's transaction. |
| Postconditions | One `meta.dq_result` row per check of U04-60 (earlier rows of those names deleted first). |
| Invariants | — |
| Algorithm | 1. `DELETE FROM meta.dq_result WHERE check_name IN (<U04-60 names except score_metric_disabled>)` with bound list. 2. For each check in U04-60 order: `render_named("checks:<name>", {}, sc.binds())`; `run_recorded(..., "score", build_id=sc.build_id)`; the single result row gives `value` and `n_bad`; `passed` per the table; insert `(check_name, severity, value, threshold, passed, details)` with `details = {"query_id", "n_bad"}`; a missing input table (the step whose table it checks has never run) → `passed = true`, `details.skipped = true`. 3. Log `metrics.scoring.check_failed` for each failed check; count `herness_metrics_check_failures_total`. |
| Side effects | `meta.dq_result`, `meta.evidence`; logs. |
| Errors | `SchemaViolation` on query failure. |
| Concurrency | Build writer. |
| Complexity and limits | 17 queries. |
| Security notes | TH04-06. |
| Tests | UT04-115, PT04-05, IT04-02 |

#### U04-60 herness/metrics/sql/checks.sql.j2

| Field | Content |
|-------|---------|
| Kind | SQL file (segments split on `-- @check <name>` lines) |
| Purpose | One SELECT per invariant returning one row `(value DOUBLE, n_bad BIGINT)`. |
| Signature | Checks, severity, value and pass rule: |
| Preconditions | — |
| Postconditions | — |
| Invariants | — |
| Algorithm | See the table below. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | — |
| Security notes | TH04-06. |
| Tests | UT04-115, PT04-03, PT04-05 |

| Check | Severity | `value` / `n_bad` | Passes when |
|-------|----------|-------------------|-------------|
| `score_fact_duration_negative` | error | count of fact rows with a negative `resolve_h`, `resolve_bh`, `impact_h`, `toil_h`, `lead_time_h` or `cycle_days` | 0 |
| `score_metric_ratio_range` | error | `metric_value` rows with `unit = 'ratio'` and `value` outside [0, 1] | 0 |
| `score_metric_pct_range` | error | rows with `unit = 'pct'` and `value` outside [0, 100] | 0 |
| `score_metric_negative` | error | rows with `value`, `numerator`, `denominator` or `sample_size` < 0 | 0 |
| `score_metric_count_integral` | error | rows of `s_count_metrics` with `value <> floor(value)` | 0 |
| `score_share_sum` | error | records in `score.funding_attribution` with `Σ share > 1 + 1e-9` | 0 |
| `score_share_range` | error | rows with `share <= 0` or `share > 1 + 1e-9` | 0 |
| `score_pain_total` | error | `value` = Σ over attribution rows of `share × record usd` (unrounded DOUBLE, record usd recomputed as in U04-64) minus the total usd of all attributable records in the window; both annualized | `value <= 0.01 + 1e-9 × total` (DD04-11) |
| `score_confidence_range` | error | `score.funding` rows with `confidence` outside [0.05, 1] | 0 |
| `score_funding_rank_unique` | error | `count(*) − count(DISTINCT rank)` | 0 |
| `score_org_z_finite` | error | `score.org` rows with non-finite `z_score`, `composite` or `trend_slope` | 0 |
| `score_portfolio_budget` | error | scenarios where `Σ ceil(effort_cost_usd)` of selected candidates > `floor(budget_usd)` | 0 |
| `score_portfolio_parent_child` | error | (scenario, parent, descendant) triples both selected | 0 |
| `score_portfolio_blockers` | error | selected candidates with an open candidate blocker not selected | 0 |
| `score_evidence_coverage` | error | distinct `query_id` values in `metrics.*.query_id` and elements of `score.*.query_ids` / `score.funding_attribution.query_id` absent from `meta.evidence` | 0 |
| `score_work_item_cycle` | warn | `metrics.work_item_closure` rows with `depth = 10` | 0 |
| `score_unconfirmed_rows` | warn | rows with `unconfirmed = true` across `score.funding`, `score.action_lever`, `score.org` | 0 (a failing warn drives the D2 banner in spec 09) |

### 3.8 Peer groups (`peers.py`, `sql/peer_group.sql.j2`)

#### U04-61 herness.metrics.peers.PeerGroupInfo

| Field | Content |
|-------|---------|
| Kind | class (pydantic; frozen, forbid, strict) |
| Purpose | Result of `peer_group` (design 04 §3.1). |
| Signature | `key: str`; `member_ids: list[str]` (sorted, entity itself excluded); `size: int` (= `len(member_ids)`); `fallback: Literal["all", "prior_year"] | None`; `query_id: str`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `size == len(member_ids)`. |
| Algorithm | — |
| Side effects | none |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-71 |

#### U04-62 herness.metrics.peers.peer_group

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Control group for spec 07 outcomes; same rules as `score.org` for team/org (design 04 §5.8 step 2) and §5.8.1 for service/work item. Re-exported from `compute.py`. |
| Signature | `entity_type: Literal["team", "org", "service", "work_item"]`; `entity_id: str` (positional); `metric: str | None = None`, `con: DuckDBPyConnection | None = None`, `on_evidence: Callable[[RecordedQuery], None] | None = None` (keyword-only; `on_evidence` is DD04-03). Returns `PeerGroupInfo`. |
| Preconditions | `entity_type` in the literal (else `ToolInputError`); `entity_id` 1–256 printable characters; `metric` None or an enabled catalog metric with grains ⊇ {team, org} when `entity_type ∈ {team, org}` (else `ToolInputError`). |
| Postconditions | Same key as `score.org.peer_group` for that entity and metric on the same build (team/org). |
| Invariants | — |
| Algorithm | 1. Validate. 2. Connection as U04-52. 3. `build_id`, `as_of` as U04-52. 4. `render_named("peer_group", {"entity_type": entity_type, "has_metric": metric is not None}, candidates)` with `pg_entity_id`, `pg_metric`, t12w window binds, `s_min_peer_group`, `s_min_service_peers`, `s_window_days`, `as_of_ts`, `d_*`. 5. `run_recorded(..., None, timeout_s=compute_timeout_s)`. 6. Result columns `entity_found BOOLEAN, key VARCHAR, fallback VARCHAR, member_id VARCHAR` (one row per member; one row with `member_id` NULL when there are none). `entity_found` false → `ToolInputError("unknown <entity_type> <entity_id>")`. 7. Call `on_evidence(rq)` when set. 8. Log `metrics.peer_group.resolved` (DEBUG). 9. Return. |
| Side effects | Read-only query; callback. |
| Errors | `ToolInputError`, `QueryError`, `StoreBusy`. |
| Concurrency | As U04-52. |
| Complexity and limits | p95 < 2 s (BT04-07). |
| Security notes | TH04-01 (ID bound, never rendered). |
| Tests | UT04-71…UT04-74, IT04-04 |

#### U04-63 herness/metrics/sql/peer_group.sql.j2

| Field | Content |
|-------|---------|
| Kind | SQL file |
| Purpose | One recorded query resolving the key, fallback and members. |
| Signature | Context `entity_type`, `has_metric`. Output as U04-62 step 6, ordered by `member_id`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Team buckets use `team_bucket`, the same macro as U04-67. |
| Algorithm | By `entity_type`: **team** — entity must exist in `core.team`; natural key `'team:crit_' || team_bucket(entity)`; candidates = active teams with the same bucket; when `has_metric`, count non-NULL `metrics.metric_value.value` for (`pg_metric`, `entity_type='team'`, `period='t12w'`, `period_start = window_start_date`) over the candidates plus the entity; if that count < `s_min_peer_group`, key `'team:all'`, candidates = all active teams, `fallback = 'all'`. **org** — entity in `core.org`; depth = `max(depth)` of its `metrics.org_closure` rows; key `'org:level' || depth`; candidates = orgs with the same depth; fallback rule as team with `'org:all'`. **service** — entity in `core.service`; key `'service:crit_' || coalesce(CAST(criticality AS VARCHAR), 'none')`; candidates = services with the same criticality (NULL matches NULL); `fallback = 'prior_year'` when fewer than `s_min_service_peers` members. **work_item** — `pg_entity_id` starting with `cluster_fix:`: owning service = the most frequent `service_id` among non-excluded `metrics.incident_fact` rows of that cluster with `opened_at` in [`as_of_ts − s_window_days` days, `as_of_ts`), ties lowest `service_id`; otherwise the item must exist in `core.work_item`: owning service = `service_id` when not NULL, else the `core.service_map` row with `role = 'owner'`, `jira_project = project`, and `jira_component` NULL or contained in `components`, highest `confidence`, ties lowest `service_id`. Resolved → the service rules above with the owning service excluded from members; unresolved → key `'work_item:unresolved'`, no members, `fallback = 'prior_year'`. In all cases the entity itself (or the owning service) is excluded from `member_id`. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | — |
| Security notes | TH04-01. |
| Tests | UT04-71…UT04-74 |

### 3.9 Funding, org score and levers

All three SQL templates run as one recorded `SELECT` each (self-contained over persisted tables, so the Verifier can re-run them). `observed_days` is the helper macro `observed_days()` added to U04-35: `greatest(1, least(s_window_days, date_diff('day', CAST(min(opened_at) AT TIME ZONE tz AS DATE), as_of)))` over non-excluded `metrics.incident_fact` rows (design 04 §5.5 with `window_days` in place of the literal 365; equal by default). "Window" below is [`as_of_ts − s_window_days` days, `as_of_ts`).

#### U04-64 herness/metrics/sql/funding_attribution.sql.j2

| Field | Content |
|-------|---------|
| Kind | SQL file |
| Purpose | Paths, tiers, both allocation passes and the stored pass-2 allocation (design 04 §5.4, §5.5). |
| Signature | Output columns in `score.funding_attribution` order without `query_id`: `candidate_id VARCHAR`, `record_id VARCHAR`, `record_kind VARCHAR`, `tier INTEGER`, `weight DOUBLE`, `share DOUBLE`, `pain_usd DECIMAL(18,2)`, `quality DOUBLE`. |
| Preconditions | Fact tables exist; `enrich.*` tables exist (may be empty). |
| Postconditions | For every attributed record `Σ share = 1` (within 1e-9); only best-tier, leaf-most paths remain. |
| Invariants | Uses no text column. |
| Algorithm | CTEs in order: 1. `cand`: `core.work_item` with `type IN ('initiative','epic','feature')` and `status_category IN ('todo','in_progress')` → `candidate_id = record_id`, `key`, `project`, `components`, `service_id`. 2. `cand_services(candidate_id, service_id, confidence)`: (`service_id`, 1.0) when not NULL, plus `core.service_map` rows with `jira_project = project` and (`jira_component IS NULL` or `list_contains(coalesce(components, []), jira_component)`), any role; max confidence per pair. 3. `cand_desc(parent_id, child_id)`: `metrics.work_item_closure` rows with `depth ≥ 1` where both record and ancestor are in `cand`. 4. `inc`: non-excluded `metrics.incident_fact` in the window (`record_id`, `number`, `service_id`, `cluster_id`, `membership_prob`, `total_usd`). 5. `rec_cost(record_id, record_kind, service_id, usd DOUBLE)`: incidents (`usd = total_usd`); events from `core.event` in the window with severity in `d_noise_severities` and `incident_id IS NULL` (`record_id = event_id`, `usd = w_triage_minutes / 60 × w_engineer_hour`); changes from `metrics.change_fact` joined to `core.change` with `failed`, `outcome ∈ d_failure_outcomes` and `actual_end` in the window (`usd = w_backout_hours × w_engineer_hour`). 6. `t1`: `core.work_item_link` rows with `link_type = 'mentions_incident'` where one side equals `inc.number` and the other side is a work-item `key` whose `work_item_closure.candidate_record_id` (self row) is not NULL → (`candidate_record_id`, incident, tier 1, w 1.0, q 1.0). 7. `t1_counts(candidate_id, cluster_id, n)`: distinct tier-1 incidents per candidate and cluster. 8. `t2c`: incident `i` with `cluster_id = C` and candidate `k` with `t1_counts(k, C).n ≥ s_min_direct_links` → tier 2, `w = s_cluster_weight × membership_prob`, `q = membership_prob`. 9. `dec`: `enrich.decision` rows with `question = 'root_cause'` on candidate record IDs, one per record: latest `decided_at`, ties highest `probability`, then lowest `decider`. 10. `t2r`: incident `i` in cluster `C` (`enrich.cluster`), candidate `k` with `dec.answer = C.root_cause_category` and `list_has_any(C.service_ids, services(k))` → tier 2, `w = s_root_cause_weight × membership_prob × dec.probability`, `q = membership_prob × dec.probability`. 11. `t3`: any `rec_cost` record whose `service_id` ∈ `cand_services(k)` → tier 3, `w = s_service_weight × confidence`, `q = 0.5 × confidence`. 12. `allocate(paths)` (a Jinja macro in this file applied twice): (a) per (record, candidate) keep the lowest tier then highest `w` (ties lowest candidate); (b) per record keep rows at the record's minimum tier; (c) drop (record, P) when (record, C) remains with (P, C) in `cand_desc` (leaf-most); (d) `share = w / Σ w` per record; `pain = share × usd` (DOUBLE). 13. Pass 1 = `allocate(t1 ∪ t2c ∪ t2r ∪ t3)`. 14. `cf`: per cluster `C` of `inc`: `n` = incidents, `pain` = Σ `total_usd`, `annual = pain × 365 / observed_days()`, `linked` = Σ pass-1 `pain` at tier ≤ 2 of C's incidents, `linked_share = linked / pain` (0 when pain = 0); qualifies when `n ≥ w_cf_min_incidents`, `annual ≥ w_cf_min_pain`, `linked_share < w_cf_max_linked_share`. 15. `t2f`: incidents of qualifying `C` → candidate `'cluster_fix:' || C`, tier 2, `w = s_cluster_weight × membership_prob`, `q = membership_prob`. 16. Pass 2 = `allocate(t1 ∪ t2c ∪ t2r ∪ t2f ∪ t3)`. 17. Output pass 2 with `pain_usd = CAST(pain AS DECIMAL(18,2))`. `services(k)` = the list of `cand_services` of `k`. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | BT04-03 (with U04-65) < 45 s. |
| Security notes | TH04-04. |
| Tests | UT04-75…UT04-80, UT04-86, PT04-05 |

#### U04-65 herness/metrics/sql/funding_score.sql.j2

| Field | Content |
|-------|---------|
| Kind | SQL file |
| Purpose | `score.funding` rows: annualized pain, addressable pain, confidence, strategic weight, effort, priority, WSJF, rank (design 04 §5.5–§5.7). |
| Signature | Output columns in `score.funding` order without `query_ids`: `candidate_id VARCHAR`, `candidate_type VARCHAR`, `title VARCHAR`, `annual_pain_usd DECIMAL(18,2)`, `addressable_pain_usd DECIMAL(18,2)`, `expected_reduction DOUBLE`, `n_incidents BIGINT`, `confidence DOUBLE`, `strategic_weight DOUBLE`, `effort_cost_usd DECIMAL(18,2)`, `priority DOUBLE`, `wsjf DOUBLE`, `rank BIGINT`, `unconfirmed BOOLEAN`, `flags VARCHAR[]`. |
| Preconditions | `score.funding_attribution` written in this build. |
| Postconditions | One row per candidate (work-item candidates and cluster-fix candidates present in the attribution), including pain-free candidates (priority 0). |
| Invariants | `confidence ∈ [0.05, 1]`; `rank` is 1..n without gaps. |
| Algorithm | Per candidate `k` with `R(k)` = attribution rows of `k` and of its candidate descendants (`cand_desc`): 1. `annual_pain_usd = CAST(Σ pain_usd × 365 / observed_days() AS DECIMAL(18,2))`. 2. `expected_reduction` = `lkp(w_er_override_k, w_er_override_v, key, <type value>)` where the type value is `w_er_epic`, `w_er_feature`, `w_er_initiative` or `w_er_cluster_fix`. 3. `addressable_pain_usd = CAST(annual × expected_reduction AS DECIMAL(18,2))`. 4. `n_incidents = CAST(round(Σ share over incident rows of R(k)) AS BIGINT)`. 5. `c_map = Σ pain·quality / Σ pain` (0 when Σ pain = 0); `c_sample = least(1, sqrt(n_incidents / 30.0))`; `c_hist = least(1, observed_days() / 365.0)`; `confidence = greatest(0.05, least(1.0, c_map × c_sample × c_hist))`. 6. `strategic_weight = least(w_sw_clip_max, greatest(w_sw_clip_min, lkp(w_sw_portfolio_k, w_sw_portfolio_v, portfolio(k), w_sw_default) × lkp(w_sw_org_k, w_sw_org_v, org(k), w_sw_default)))`; `portfolio(k)` = `key` of the deepest `initiative` ancestor via `work_item_closure` (max depth), else `project`; `org(k)` = `core.team.org_id` of `team_id`, else `core.service.org_id` of `service_id`; cluster-fix: portfolio NULL, org of the owning service (most frequent service of the cluster in the window, ties lowest ID). 7. `effort_cost_usd`: `estimate_cost_usd` when not NULL; else remaining points (Σ non-NULL `story_points` of subtree items via `work_item_closure` with `status_category <> 'done'`, NULL when none are non-NULL) × `w_engineer_hour × w_hours_per_point`; cluster-fix `w_cf_effort_hours × w_engineer_hour`; else NULL with flag `no_estimate`. Cast to `DECIMAL(18,2)`. 8. `priority = addressable × confidence × strategic_weight / effort` (NULL when effort NULL or 0). 9. WSJF: `fib(pr)` = 1 if `pr ≤ .15`, 2 if `≤ .30`, 3 if `≤ .45`, 5 if `≤ .60`, 8 if `≤ .75`, 13 if `≤ .90`, else 20, with `pr = percent_rank() OVER (ORDER BY input)` over candidates with non-NULL input. BV input `addressable × strategic_weight`. TC input: Theil–Sen slope of `k`'s weekly `Σ pain_usd` of `R(k)` over the 12 complete weeks before `as_of` (weeks by local `date_trunc('week')` of the record timestamp: incident `opened_at`, event `ts`, change `actual_end`; missing weeks = 0; slope = median of `(v_b − v_a) / (t_b − t_a)` over pairs `t_b > t_a`); TC = `fib(pr)` raised one step in (1, 2, 3, 5, 8, 13, 20) (cap 20) when `R(k)` has a priority-1 incident opened in the last 30 days. RR input: number of distinct candidates `B` with a `core.work_item_link` row `link_type = 'blocks'`, `from_key = k.key`, `to_key = B.key`, plus `5 − coalesce(min criticality of services(k), 5)`. JS input: effort. `wsjf = (BV + TC + RR) / JS`, NULL when effort NULL. 10. `rank = row_number() OVER (ORDER BY priority DESC NULLS LAST, addressable_pain_usd DESC, confidence DESC, candidate_id ASC)`. 11. `title` = work-item `key`; cluster-fix: `candidate_id` (DD04-12). 12. `unconfirmed` = bind; `flags` = sorted subset of `short_history` (`observed_days() < 180`), `no_estimate`. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | ~5k candidates. |
| Security notes | TH04-04 (key, not summary). |
| Tests | UT04-80…UT04-87, PT04-06 |

#### U04-66 herness.metrics.funding.run_funding_step

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Materialize attribution, then scores. |
| Signature | `con: DuckDBPyConnection`; `sc: StepContext` (positional). Returns `StepResult`. |
| Preconditions | Inside the caller's transaction. |
| Postconditions | `score.funding_attribution` (`query_id` column) and `score.funding` (`query_ids = [own, attribution]`) rewritten. |
| Invariants | — |
| Algorithm | 1. `binds = sc.binds()` with `unconfirmed = bool(unconfirmed_blocks(sc.weights, WEIGHT_USES["funding"]))`. 2. `a = run_recorded(con, *render_named("funding_attribution", {}, binds), "score", build_id, into=IntoSpec("score.funding_attribution", "replace", "query_id"))`. 3. `f = run_recorded(..."funding_score"..., into=IntoSpec("score.funding", "replace", "query_ids", (a.query_id,)))`. 4. Row counts; warning when `score.funding` is empty ("no funding candidates"). |
| Side effects | Tables, evidence. |
| Errors | `SchemaViolation` (converted by the runner). |
| Concurrency | Build writer. |
| Complexity and limits | BT04-03. |
| Security notes | none |
| Tests | UT04-75, UT04-84 |

#### U04-67 herness/metrics/sql/org_score.sql.j2

| Field | Content |
|-------|---------|
| Kind | SQL file |
| Purpose | `score.org` rows (design 04 §5.8 steps 1–6). |
| Signature | Output columns in `score.org` order without `query_ids`: `entity_type VARCHAR`, `entity_id VARCHAR`, `metric VARCHAR`, `value DOUBLE`, `peer_group VARCHAR`, `peer_median DOUBLE`, `z_score DOUBLE`, `trend_slope DOUBLE`, `sample_size BIGINT`, `composite DOUBLE`, `rank BIGINT`, `unconfirmed BOOLEAN`, `flags VARCHAR[]`. |
| Preconditions | `metrics.metric_value` written. |
| Postconditions | One row per (active team or org) × scorecard metric; `composite` and `rank` repeat on each row of an entity. |
| Invariants | `b` and `trend_b` clipped to [−5, 5]; `z_score` stored unclipped. |
| Algorithm | 1. Entities: `core.team` with `active` → `team`; all `core.org` → `org`. Scorecard: `unnest(s_org_metrics, s_org_weights)`; `sign = +1` if metric ∈ `s_lower_better` else −1. 2. `x` = `metric_value.value` at `period = 't12w'`, `period_start = window_start_date` (NULL when missing or insufficient); `sample_size` from the row (0 when missing). 3. Natural group: team `'team:crit_' || team_bucket(team_id)`; org `'org:level' || max(depth)`. When the natural group has fewer than `s_min_peer_group` non-NULL `x` for the metric, use `'team:all'` / `'org:all'` (flag `peer_fallback`). 4. Group stats over non-NULL `x`: `median_g = median(x)`; `mad_g = mad(x)` (median absolute deviation, unscaled; VI04-06); `meanad = avg(abs(x − median_g))`; `denom = 1.4826 × mad_g` when `mad_g > 0`, else `1.2533 × meanad` when `meanad > 0`, else NULL. 5. `z = (x − median_g) / denom`; `z = 0` when `x` not NULL and `denom` NULL; `b = clip(sign × z, −5, 5)`. 6. Trend: weekly `metric_value` rows (`period = 'week'`, value not NULL) with `period_start` in [`w0`, `w_end`) where `w_end = date_trunc('week', as_of)` and `w0 = w_end − 84 days`; `t = date_diff('week', w0, period_start)`; `trend_slope` = median of pairwise slopes over pairs with `t_b > t_a`; NULL when fewer than 6 distinct `t` (flag `no_trend`); `trend_b = clip(sign × slope × 12 / denom, −5, 5)`, NULL when `denom` NULL. 7. Per entity: `covered = Σ w_m` over metrics with `b` not NULL; `total = Σ w_m`; `composite = Σ w_m × (b + s_trend_weight × coalesce(trend_b, 0)) / covered` when `covered / total ≥ s_min_weight_coverage`, else NULL (flag `low_coverage` on all rows of the entity). 8. `rank = row_number() OVER (PARTITION BY entity_type ORDER BY composite DESC NULLS LAST, entity_id)`. 9. Row flags also include `insufficient_sample` (from the metric row) and `no_data` (no metric row). `unconfirmed = false`. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | BT04-04 (with levers) < 20 s. |
| Security notes | none |
| Tests | UT04-88…UT04-91, PT04-07 |

#### U04-68 herness.metrics.org.run_org_step

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Materialize `score.org`. |
| Signature | `con: DuckDBPyConnection`; `sc: StepContext` (positional). Returns `StepResult`. |
| Preconditions | `metrics.metric_value` exists, else `SchemaViolation("metric_value missing; run step metrics first")`. |
| Postconditions | `score.org` rewritten with `query_ids = [own]`. |
| Invariants | — |
| Algorithm | `run_recorded(con, *render_named("org_score", {}, sc.binds() with unconfirmed=false), "score", build_id, into=IntoSpec("score.org", "replace", "query_ids"))`; row count. |
| Side effects | Table, evidence. |
| Errors | `SchemaViolation`. |
| Concurrency | Build writer. |
| Complexity and limits | BT04-04. |
| Security notes | none |
| Tests | UT04-90 |

#### U04-69 herness/metrics/sql/levers.sql.j2

| Field | Content |
|-------|---------|
| Kind | SQL file |
| Purpose | `score.action_lever` rows (design 04 §5.9). |
| Signature | Output columns in `score.action_lever` order without `query_ids`: `entity_type VARCHAR`, `entity_id VARCHAR`, `metric VARCHAR`, `target_kind VARCHAR`, `current_value DOUBLE`, `target_value DOUBLE`, `delta_usd DECIMAL(18,2)`, `rationale_template VARCHAR`, `template_params JSON`, `unconfirmed BOOLEAN`. |
| Preconditions | `score.org` written. |
| Postconditions | Only improving targets with `delta_usd > 0`. |
| Invariants | `rationale_template` is a config string; `template_params` values come only from this query and catalog/config labels. |
| Algorithm | 1. Rows of `score.org` with `rank ≤ s_top_entities`, metric ∈ `s_lever_models_k` (model from `s_lever_models_v`), `z_score > 0` (lever metrics are lower-is-better, so `b = z`), `value` not NULL. 2. Targets: `peer_median` = `peer_median`; `top_quartile` = `quantile_cont(value, 0.25)` over `score.org` rows with the same `peer_group` and metric (0.75 if the metric is higher-is-better). Keep a target only when it improves (`target < x` for lower-is-better). 3. Base quantities over [`as_of_ts − INTERVAL 12 MONTH`, `as_of_ts`) for the entity (team: `team_id`, owner team for events; org: closure over `org_id`, service org for events): `N` = non-excluded incidents (by `opened_at`) with `Σ total_usd`, `Σ downtime_usd`, `Σ toil_usd`, `avg total_usd`, `avg toil_usd`; `D` = deployed changes and `F` = failed changes (by `actual_end`); `CC` = Σ `total_usd` of distinct non-excluded incidents caused by the entity's failed changes (pairs as metric #17); `E` = events with severity in `d_noise_severities`. 4. `delta` by model: `mttr` `(Σ downtime + Σ toil) × (x − t) / x` (skip when `x = 0`); `repeat` `N × (x − t) × avg total`; `reopen` `N × (x − t) × avg toil × w_reopen_factor`; `reassign` `N × (x − t) × w_reassign_hours × w_engineer_hour`; `sla` `N × (x − t) × w_sla_penalty` (skip when `w_sla_penalty = 0`); `cfr` `D × (x − t) × (w_backout_hours × w_engineer_hour + (CC / F when F > 0, else 0))`; `noise` `E × (x − t) × w_triage_minutes / 60 × w_engineer_hour`. 5. `delta_usd = CAST(delta × 365 / observed_days() AS DECIMAL(18,2))`; drop rows with `delta_usd ≤ 0`. 6. `rationale_template = lkp(s_templates_k, s_templates_v, model, NULL)`. 7. `template_params = json_object('entity_name', <core.team.name or core.org.name>, 'metric_label', metric, 'current_value', x, 'target_value', t, 'target_kind', kind, 'unit', lkp(s_metric_units_k, s_metric_units_v, metric, 'other'), 'delta_usd', CAST(delta_usd AS VARCHAR), 'n_basis', <N, D or E by model>, 'peer_group', peer_group, 'period', 't12w')`. 8. `unconfirmed = list_contains(s_unconfirmed_models, model)`. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | ≤ 2 × 50 entities per type × 10 metrics × 2 targets rows. |
| Security notes | TH04-10: placeholders fixed; values from the query. |
| Tests | UT04-92…UT04-100 |

#### U04-70 herness.metrics.levers.run_levers_step

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Materialize `score.action_lever`. |
| Signature | `con: DuckDBPyConnection`; `sc: StepContext` (positional). Returns `StepResult`. |
| Preconditions | `score.org` exists, else `SchemaViolation("score.org missing; run step org first")`. |
| Postconditions | `score.action_lever` rewritten with `query_ids = [own, org query id]` (the org query id read from `score.org.query_ids[1]`). |
| Invariants | — |
| Algorithm | Read the org query id; `run_recorded(con, *render_named("levers", {}, sc.binds()), "score", build_id, into=IntoSpec("score.action_lever", "replace", "query_ids", (org_qid,)))`. |
| Side effects | Table, evidence. |
| Errors | `SchemaViolation`. |
| Concurrency | Build writer. |
| Complexity and limits | BT04-04. |
| Security notes | none |
| Tests | UT04-99 |

#### U04-71 herness.metrics.levers.USD_MODELS and LEVER_PLACEHOLDERS

| Field | Content |
|-------|---------|
| Kind | constant |
| Purpose | Lever model names and the only placeholders allowed in `rationale_template` (design 04 §5.9). |
| Signature | `USD_MODELS: Final[tuple[str, ...]] = ("mttr", "repeat", "reopen", "reassign", "sla", "cfr", "noise")`; `LEVER_PLACEHOLDERS: Final[frozenset[str]] = {"entity_name", "metric_label", "current_value", "target_value", "target_kind", "unit", "delta_usd", "n_basis", "peer_group", "period"}`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | Keys of `template_params` equal `LEVER_PLACEHOLDERS`. |
| Algorithm | — |
| Side effects | none |
| Errors | none |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-10. |
| Tests | UT04-21, UT04-100 |

### 3.10 Portfolio optimizer (`_solver.py`, `portfolio.py`, `sql/portfolio_input.sql.j2`)

#### U04-72 herness.metrics._solver.PortfolioCandidate and SolveOutcome

| Field | Content |
|-------|---------|
| Kind | class (frozen dataclasses) |
| Purpose | Pure solver input and output. |
| Signature | `PortfolioCandidate`: `candidate_id: str`; `candidate_type: str`; `team_id: str | None`; `effort_cost_usd: Decimal | None`; `expected_impact_usd: Decimal`; `priority: float | None`; `points: float | None`; `descendants: tuple[str, ...]`; `blockers: tuple[str, ...]` (candidate IDs); `noncandidate_blockers: int`. `SolveOutcome`: `status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "MODEL_INVALID"]`; `selected: frozenset[str]`; `row_flags: Mapping[str, tuple[str, ...]]`; `flags: tuple[str, ...]`; `wall_time_s: float`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `selected` empty unless status is `OPTIMAL` or `FEASIBLE`. |
| Algorithm | — |
| Side effects | none |
| Errors | none |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-101 |

#### U04-73 herness.metrics._solver.solve_portfolio

| Field | Content |
|-------|---------|
| Kind | function (pure, deterministic) |
| Purpose | Build and solve the CP-SAT model of design 04 §5.10. |
| Signature | `candidates: Sequence[PortfolioCandidate]`; `budget_usd: Decimal`; `mandatory: frozenset[str]`; `excluded: frozenset[str]`; `capacities: Mapping[str, float] | None` (None = capacity not enforced); `capacity_default: float`; `horizon_quarters: int`; `solver: SolverConfig` (all keyword-only). Returns `SolveOutcome`. |
| Preconditions | `mandatory ∩ excluded = ∅`; every mandatory ID is a candidate (else `ConfigError("unknown mandatory candidate <id>")`); no mandatory candidate has NULL effort (else `ConfigError("mandatory candidate <id> has no effort estimate")`). |
| Postconditions | Same inputs → same output (single worker, fixed seed, deterministic time limit). |
| Invariants | — |
| Algorithm | 1. Sort candidates by `candidate_id`. 2. Candidates with NULL effort are not modeled (x = 0) and get row flag `no_estimate`. Unknown excluded IDs → global flag `unknown_excluded`. 3. One `NewBoolVar` per modeled candidate, in sorted order. 4. Objective: maximize `Σ floor(expected_impact_usd) × x`. 5. Budget: `Σ ceil(effort) × x ≤ floor(budget_usd)`. 6. When `capacities` is not None: per team `t`, `Σ ceil(points × 100) × x ≤ floor(cap_t × horizon_quarters × 100)` with `cap_t = capacities.get(t, capacity_default)`; points NULL counts 0 (row flag `no_points`); team NULL → row flag `no_team`, no capacity term. 7. `x = 1` for mandatory, `x = 0` for excluded. 8. For each B and each blocker A in `B.blockers`: `x_B ≤ x_A` when A is modeled; when A is not modeled, `x_B = 0` and row flag `blocked_by_unestimated`. `noncandidate_blockers > 0` → row flag `blocked_by_noncandidate`, no constraint. 9. For each P and C in `P.descendants`, both modeled: `x_P + x_C ≤ 1`. 10. Parameters: `num_workers = solver.num_workers` (1), `random_seed`, `max_deterministic_time`, `max_time_in_seconds`, `log_search_progress = False`. 11. Status: `OPTIMAL` → `OPTIMAL`; `FEASIBLE` → `FEASIBLE` with flag `not_proven_optimal`; `INFEASIBLE` → `INFEASIBLE`; `MODEL_INVALID` → `MODEL_INVALID`; `UNKNOWN` → `INFEASIBLE` with flag `no_solution_found` (DD04-10). Flag `wall_clock_limit` when `wall_time ≥ max_time_in_seconds − 0.5`. 12. `selected` = modeled candidates with value 1. |
| Side effects | none |
| Errors | `ConfigError` (preconditions). |
| Concurrency | Pure; one solver per call. |
| Complexity and limits | ~5k binary variables; `max_deterministic_time` 20, wall 60 s. |
| Security notes | TH04-08. |
| Tests | UT04-101, UT04-104, UT04-105, UT04-106, PT04-09, PT04-10, FT04-04 |

#### U04-74 herness.metrics._solver.order_selected

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | `order_rank` of selected candidates: topological over blocks links, ties `priority DESC, candidate_id` (design 04 §5.10). |
| Signature | `selected: Sequence[PortfolioCandidate]` (positional). Returns `tuple[dict[str, int], bool]` = (rank per candidate, 1..n; `had_cycle`). |
| Preconditions | — |
| Postconditions | For every selected A blocking selected B, `rank(A) < rank(B)` unless both are in a cycle. |
| Invariants | — |
| Algorithm | Kahn's algorithm over edges A → B (A in `B.blockers`, both selected) with a heap keyed `(−priority if not None else +inf, candidate_id)`. When the heap is empty but nodes remain (cycle), take the remaining node with the smallest key and set `had_cycle`. |
| Side effects | none |
| Errors | none |
| Concurrency | Pure. |
| Complexity and limits | O(n log n + edges). |
| Security notes | none |
| Tests | UT04-102, PT04-08 |

#### U04-75 herness.metrics._solver.binding_constraints

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Name the constraints that stopped further selection (`PortfolioResult.binding_constraints`). |
| Signature | `candidates`, `selected`, `budget_usd`, `mandatory`, `excluded`, `capacities`, `capacity_default`, `horizon_quarters` (keyword-only, as U04-73). Returns `list[str]` (sorted). |
| Preconditions | — |
| Postconditions | `budget` when some unselected, modeled, non-excluded candidate exists and `floor(budget) − Σ ceil(effort selected)` is smaller than the smallest `ceil(effort)` among them; `capacity:<team_id>` by the same rule per team on points; `mandatory` when `mandatory` is non-empty. |
| Invariants | — |
| Algorithm | As postconditions. |
| Side effects | none |
| Errors | none |
| Concurrency | Pure. |
| Complexity and limits | O(n). |
| Security notes | none |
| Tests | UT04-101 |

#### U04-76 herness.metrics.portfolio.Scenario

| Field | Content |
|-------|---------|
| Kind | class (pydantic; frozen, forbid, strict except `budget_usd` accepts int and decimal strings) |
| Purpose | Design 04 §3.1 `Scenario`. |
| Signature | `name: str` (`^[a-z0-9_]{1,64}$`); `budget_usd: Decimal` (≥ 0, ≤ 1e12; > 0 except for `unconstrained`); `mandatory: list[str] = []` (≤ 1000, IDs ≤ 256 chars); `excluded: list[str] = []` (same); `team_capacity_points: dict[str, float] | None = None` (values > 0); `enforce_team_capacity: bool = True`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `mandatory ∩ excluded = ∅`. |
| Algorithm | — |
| Side effects | none |
| Errors | `ValidationError` (→ `ConfigError`). |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | TH04-08 (bounded budget and list sizes from UI input). |
| Tests | UT04-103, ST04-08 |

#### U04-77 herness.metrics.portfolio.PortfolioRow and PortfolioResult

| Field | Content |
|-------|---------|
| Kind | class (pydantic; frozen) |
| Purpose | Design 04 §3.1 `PortfolioResult`. |
| Signature | `PortfolioRow`: `candidate_id: str`; `selected: bool`; `order_rank: int | None`; `expected_impact_usd: Decimal`; `flags: list[str]`. `PortfolioResult`: `scenario: str`; `budget_usd: Decimal`; `solver_status: Literal["OPTIMAL", "FEASIBLE", "INFEASIBLE", "MODEL_INVALID"]`; `rows: list[PortfolioRow]` (sorted by `candidate_id`); `selected: list[str]` (in `order_rank` order); `total_effort_usd: Decimal`; `total_expected_impact_usd: Decimal`; `binding_constraints: list[str]`; `query_ids: list[str]`; `flags: list[str]`. |
| Preconditions | — |
| Postconditions | — |
| Invariants | `order_rank` NULL iff not selected. |
| Algorithm | — |
| Side effects | none |
| Errors | — |
| Concurrency | Immutable. |
| Complexity and limits | — |
| Security notes | none |
| Tests | UT04-101 |

#### U04-78 herness.metrics.portfolio.resolve_scenario

| Field | Content |
|-------|---------|
| Kind | function (pure) |
| Purpose | Turn a name, a USD string or a `Scenario` into the effective scenario. |
| Signature | `scenario: Scenario | str`; `portfolio: PortfolioConfig`; `total_effort_usd: Decimal` (positional). Returns `Scenario`. |
| Preconditions | — |
| Postconditions | Effective `mandatory` = own ∪ `portfolio.mandatory`; `excluded` = own ∪ `portfolio.excluded`; overlap → `ConfigError("candidate <id> is both mandatory and excluded")`. |
| Invariants | — |
| Algorithm | `Scenario` object → merged lists. String: `unconstrained` → budget `total_effort_usd`, `enforce_team_capacity = False`; a configured name → its budget, `enforce_team_capacity = portfolio.enforce_team_capacity`; matches `^[0-9]{1,13}(\.[0-9]{1,2})?$` → `Scenario(name="custom_" + text with "." replaced by "_", budget_usd=Decimal(text))`; otherwise `ConfigError("unknown scenario <name>")`. |
| Side effects | none |
| Errors | `ConfigError`. |
| Concurrency | Pure. |
| Complexity and limits | — |
| Security notes | TH04-08. |
| Tests | UT04-103, UT04-109 |

#### U04-79 herness/metrics/sql/portfolio_input.sql.j2

| Field | Content |
|-------|---------|
| Kind | SQL file |
| Purpose | The one recorded optimizer input query over `score.funding`, points and links (design 04 §5.10). |
| Signature | Output ordered by `candidate_id`: `candidate_id VARCHAR`, `candidate_type VARCHAR`, `team_id VARCHAR`, `effort_cost_usd DECIMAL(18,2)`, `expected_impact_usd DECIMAL(18,2)` (= `CAST(addressable_pain_usd × confidence × strategic_weight AS DECIMAL(18,2))`), `priority DOUBLE`, `points DOUBLE`, `descendants VARCHAR[]`, `blockers VARCHAR[]`, `noncandidate_blockers BIGINT`. |
| Preconditions | `score.funding` exists. |
| Postconditions | — |
| Invariants | — |
| Algorithm | Work-item candidates: `team_id` from `core.work_item`; `points` = Σ non-NULL `story_points` of subtree items (via `work_item_closure`) with `status_category <> 'done'` (NULL when none). Cluster-fix: `team_id` = owner team (`owner_team`) of the owning service (U04-65 step 6); `points = w_cf_effort_hours / w_hours_per_point`. `descendants` = sorted candidate descendants (`cand_desc`). `blockers` = sorted candidate IDs `A` with a `core.work_item_link` row `link_type = 'blocks'`, `from_key = A.key`, `to_key = k.key`. `noncandidate_blockers` = count of such `from_key` items that are not candidates and have `status_category <> 'done'`. |
| Side effects | none |
| Errors | — |
| Concurrency | — |
| Complexity and limits | ~5k rows. |
| Security notes | none |
| Tests | UT04-101, IT04-07 |

#### U04-80 herness.metrics.portfolio.optimize_portfolio

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Design 04 §3.1 / §5.10 entry point for build scenarios (`persist=True`) and ad-hoc scenarios on the promoted warehouse (`persist=False`). |
| Signature | `scenario: Scenario | str` (positional); `persist: bool = True`, `build_id: str | None = None`, `run_id: str | None = None`, `con: DuckDBPyConnection | None = None` (keyword-only). Returns `PortfolioResult`. |
| Preconditions | `persist=True` needs the build pipeline's writable connection passed as `con`; `persist=True` without `con` → `ConfigError("persist=True needs the build writer's connection")` (this package never opens a build file for writing, C12). A read-only connection (`current_setting('access_mode')` is `read_only`, case-insensitive; VI04-03) with `persist=True` → `ConfigError("warehouse is read-only; use persist=False")`. When `build_id` is given it must equal `meta.build.build_id`, else `ConfigError("build_id mismatch")`. |
| Postconditions | `persist=True`: `score.portfolio` holds exactly this scenario's rows (none when not `OPTIMAL`/`FEASIBLE`). `persist=False`: nothing written to the warehouse; the input query written to ops `evidence`. Same scenario on the same build → same selection. |
| Invariants | — |
| Algorithm | 1. Config, catalog, weights. 2. Connection: `con`; else `persist` → the `ConfigError` of the preconditions; else T02-09 (herness.store.warehouse.open_readonly)(None). 3. Preconditions. 4. `rq = run_recorded(con, *render_named("portfolio_input", {}, binds), "score" if persist else None, build_id=…, timeout_s=None if persist else compute_timeout_s)`. 5. Candidates from rows. 6. `sc = resolve_scenario(scenario, weights.portfolio, Σ effort of candidates with effort)`. 7. `capacities` = None when `not sc.enforce_team_capacity`, else `sc.team_capacity_points` or `weights.team_capacity_points_per_quarter.teams`; `capacity_default` from weights. 8. `outcome = solve_portfolio(...)`; `ranks = order_selected(selected candidates)` (flag `blocks_cycle` when a cycle was broken); `binding_constraints(...)`. 9. Rows for every candidate (flags = row flags). Totals as `Decimal`. 10. `persist=True`: `DELETE FROM score.portfolio WHERE scenario = ?`; when status is `OPTIMAL`/`FEASIBLE`, parameterized `INSERT` of one row per candidate (`scenario`, `budget_usd`, `candidate_id`, `selected`, `order_rank`, `expected_impact_usd`, `solver_status`, `flags`, `query_ids = [rq.query_id]`); then `run_recorded(con, PORTFOLIO_READBACK_SQL, {"bind": {"scenario": name}, "template": {"name": "portfolio_readback", "scenario": name}}, "score", build_id)` where `PORTFOLIO_READBACK_SQL` selects every `score.portfolio` column except `query_ids` with `WHERE scenario = CAST($scenario AS VARCHAR)`. 11. `persist=False`: T05-12 (herness.store.ops.evidence.record_evidence)(`rq.to_evidence(run_id)`) (R-13). 12. Status `INFEASIBLE`/`MODEL_INVALID` → result flags include `infeasible_mandatory` with the mandatory count and budget in `warnings` of the step; log `metrics.portfolio.infeasible`. 13. Log `metrics.portfolio.solved`; histogram `herness_metrics_portfolio_solve_seconds`. |
| Side effects | `score.portfolio`, `meta.evidence` (persist) or ops `evidence` (not persist); logs. |
| Errors | `ConfigError`, `QueryError`, `StoreBusy`. |
| Concurrency | `persist=True` only in the build writer; `persist=False` any process (read-only). |
| Complexity and limits | < 30 s wall per scenario (BT04-05, BT04-08). |
| Security notes | TH04-08, TH04-09, TH04-14. |
| Tests | UT04-104…UT04-109, IT04-05, IT04-07, ST04-09, ST04-14, BT04-05, BT04-08 |

#### U04-81 herness.metrics.portfolio.run_portfolio_step

| Field | Content |
|-------|---------|
| Kind | function (adapter) |
| Purpose | Build-time portfolio step for configured scenarios plus `unconstrained` (design 04 §5.10). |
| Signature | `con: DuckDBPyConnection`; `sc: StepContext` (positional). Returns `StepResult`. |
| Preconditions | `score.funding` exists, else `SchemaViolation`. |
| Postconditions | `score.portfolio` rewritten. |
| Invariants | — |
| Algorithm | 1. `CREATE OR REPLACE TABLE score.portfolio (scenario VARCHAR NOT NULL, budget_usd DECIMAL(18,2) NOT NULL, candidate_id VARCHAR NOT NULL, selected BOOLEAN NOT NULL, order_rank INTEGER, expected_impact_usd DECIMAL(18,2) NOT NULL, solver_status VARCHAR NOT NULL, flags VARCHAR[] NOT NULL, query_ids VARCHAR[] NOT NULL)`. 2. For each configured scenario name in config order, then `unconstrained`: `optimize_portfolio(name, persist=True, build_id=sc.build_id, con=con)`; a `ConfigError` for one scenario becomes warning `scenario <name>: <message>` and the loop continues; non-solved statuses add warning `scenario <name>: <status>, mandatory=<n>, budget=<usd>`. |
| Side effects | Table, evidence. |
| Errors | `SchemaViolation`. |
| Concurrency | Build writer. |
| Complexity and limits | 4 scenarios × < 30 s. |
| Security notes | none |
| Tests | UT04-105, UT04-110 |

## 4. State and data

### 4.1 Warehouse tables owned (build file `wh-<build_id>.duckdb`)

Column lists and order are those of design 02 §4.5–4.6 and 04 §4.2; types below. Every table is written only by the build pipeline job, retained with its warehouse file (last 3 builds, design 00 §4).

| Table | Types (beyond design 04 §4.2) | Nullability | Idempotency of the write | Transaction |
|-------|-------------------------------|-------------|--------------------------|-------------|
| `metrics.org_closure`, `metrics.work_item_closure`, `metrics.incident_fact`, `metrics.change_fact`, `metrics.work_item_fact` | design 04 §4.2; `query_id VARCHAR NOT NULL` | per U04-41…U04-45 column tables | `CREATE OR REPLACE TABLE` (full rewrite) | one transaction for stage 400 |
| `metrics.metric_value` | U04-58 DDL | `value`, `numerator`, `denominator` nullable | `CREATE OR REPLACE` then appends; key (`metric`, `entity_type`, `entity_id`, `period`, `period_start`) unique by construction | step `metrics` |
| `score.funding_attribution` | U04-64 output + `query_id VARCHAR` | none nullable | `CREATE OR REPLACE` | step `funding` |
| `score.funding` | U04-65 output + `query_ids VARCHAR[]` | `effort_cost_usd`, `priority`, `wsjf` nullable | `CREATE OR REPLACE`; key `candidate_id` | step `funding` |
| `score.org` | U04-67 output + `query_ids` | `value`, `peer_median`, `z_score`, `trend_slope`, `composite` nullable | `CREATE OR REPLACE`; key (`entity_type`, `entity_id`, `metric`) | step `org` |
| `score.action_lever` | U04-69 output + `query_ids` | none nullable | `CREATE OR REPLACE`; key (`entity_type`, `entity_id`, `metric`, `target_kind`) | step `levers` |
| `score.portfolio` | U04-81 DDL | `order_rank` nullable | per-scenario `DELETE` + `INSERT`; key (`scenario`, `candidate_id`) | step `portfolio` |
| `meta.evidence` (rows with producer `facts`, `metrics`, `score`) | design 02 §4.7 | — | `INSERT … ON CONFLICT (query_id) DO NOTHING`; hash compare on conflict | caller's transaction |
| `meta.dq_result` (checks `score_*`) | design 02 §4.7 | — | delete rows of the same check names, then insert | step `check` / validate |

No indexes are created (DuckDB zone maps suffice for the scans). No migrations: warehouse tables are rebuilt per build, and this spec owns no ops table, so it uses no migration number of the R-11 ranges.

### 4.2 Ops store and job state

| Store | Write | Idempotency key | Owner of the function |
|-------|-------|-----------------|-----------------------|
| ops `evidence` | `optimize_portfolio(persist=False)` input query | `query_id` (`INSERT OR IGNORE`) | T05-12 (herness.store.ops.evidence.record_evidence) (R-13) |
| ops `metric_sample` (table from impl 02 migration 006, R-12) | metric samples (§8) | append-only | T08-05 (herness.store.ops.metrics.record_metric_samples) (R-12) |
| `job` state (`JobContext.save_state`) | key `scoring` = `{"build_id", "config_hash", "steps_done"}` | overwrite; other keys preserved | T08-03 (herness.core.jobs.JobContext) (R-02). This is job state, not the task checkpoint envelope of R-21, which only 05, 06 and 07 write. |
| `review_item` | none written here; payload schema `WeightChangePayload` defined here | — | impl 02 (`herness.store.ops.shared`, R-08; decisions through `herness.store.ops.shared.decide_review_item`, R-33) |

### 4.3 In-memory state

No module-level mutable state. Each render creates its own Jinja environment and `RenderState`; the catalog is rebuilt from the cached config per call; the hash process pool lives only inside one `run_recorded` call.

## 5. Control flows

| ID | Flow | Steps (unit → state change; failure effect) |
|----|------|---------------------------------------------|
| F04-01 | Stage 400 facts | 1. Spec 02 build runner calls U04-47 with its writable connection. 2. Input check → missing table: `SchemaViolation`, build fails, `CURRENT` untouched. 3. U04-46 split → `ConfigError` on malformed file. 4. `BEGIN`; per statement U04-39 render + U04-12 `into` replace → fact table + `meta.evidence`; SQL error → `ROLLBACK`, `SchemaViolation`. 5. `COMMIT`; return five query IDs. |
| F04-02 | Full scoring | 1. Build pipeline calls U04-56 `run_scoring(build_id, con=con, ctx=ctx)`. 2. Status check → `ConfigError` when not `building`. 3. Validate (U04-57) → `meta.dq_result` warnings; missing facts → `SchemaViolation`. 4. Steps `metrics` (U04-58), `funding` (U04-66), `org` (U04-68), `levers` (U04-70), `portfolio` (U04-81), `check` (U04-59), each in its own transaction followed by a checkpoint; a failure rolls back that step and fails the job (spec 08 retries per policy; finished steps are skipped). 5. Failed error checks → `SchemaViolation` after the dq rows commit; build not promoted. |
| F04-03 | Metric step | 1. Recreate `metric_value`. 2. For metric × grain × period: U04-31 window, U04-38 render, U04-12 append with evidence. 3. `QueryError` → `SchemaViolation` naming metric, grain, period. |
| F04-04 | Funding step | 1. U04-64 via U04-12 → `score.funding_attribution`. 2. U04-65 via U04-12 with upstream attribution ID → `score.funding`. Failure: step rollback. |
| F04-05 | Org and levers | 1. U04-67 → `score.org` (needs `metric_value`, else `SchemaViolation`). 2. U04-69 → `score.action_lever` (needs `score.org`). |
| F04-06 | Portfolio step | 1. Recreate `score.portfolio`. 2. Per scenario U04-80 (`persist=True`): input query (U04-79) → U04-78 → U04-73 → U04-74/U04-75 → delete/insert → recorded read-back. 3. `ConfigError` per scenario → warning; infeasible → warning, no rows. |
| F04-07 | Interactive metric (`get_metric`) | 1. Spec 05 tool calls U04-52 with its read-only cursor. 2. U04-51 validation → `ToolInputError` returned to the agent. 3. U04-38 render; U04-12 with timeout → `QueryError` returned with hint. 4. Tool writes ops `evidence` from `MetricResult`. |
| F04-08 | Outcome series and peers (spec 07) | 1. U04-62 `peer_group(..., on_evidence=cb)` → one query; callback writes ops evidence. 2. U04-53 `metric_series(..., on_evidence=cb)` → one query. Errors as F04-07. |
| F04-09 | Ad-hoc portfolio (spec 06, 09) | 1. U04-80 `persist=False` on `CURRENT` read-only. 2. Input query recorded to ops `evidence` via T05-12 (herness.store.ops.evidence.record_evidence). 3. Solve and return; nothing written to the warehouse. `persist=True` on read-only → `ConfigError`. |
| F04-10 | Check step | U04-59 over U04-60 checks → `meta.dq_result`; failed error checks returned to U04-56. |
| F04-11 | Recorded query | U04-12 steps 1–10. Conflicting hash for an existing `query_id` → `SchemaViolation` (nondeterminism). |
| F04-12 | Weight confirmation | 1. Spec 10 loader loads weights (`load_config`). 2. Impl 10's start-up validation hook (T10-12, R-71) runs U04-83, which calls U04-22 with the previous weights (last config snapshot), the current weights, the new `config_hash` and the approved `weight_change` payloads. 3. U04-83 converts each returned `WeightIssue` (U04-82) into a `ConfigIssue`; log `metrics.weights.confirmation_rejected`; the hook raises `ConfigError` at start-up on any `error` issue (exit 3, R-46), while `config validate` and `doctor` report it. |
| F04-13 | Resume after crash | 1. Job restarts; U04-56 reads `scoring` state. 2. Same build and `config_hash` → skip done steps; else start over. 3. The step that was running re-runs fully (its transaction never committed). |
| F04-14 | `herness score` (CLI, impl 09 command table, R-47) | 1. Without `--inline` the command enqueues a `build_pipeline` job whose payload names the `score` stage, the build and the steps (impl 09); `priority` is left `None`, so the per-kind default applies (R-41). When `herness.core.jobs.worker_alive()` is false, the command prints a warning naming the fix (start the worker, or rerun with `--inline`) and exits 0 with the job queued (R-44, R-45). 2. With the admin-only `--inline` flag, the command runs the same job in-process through `herness.core.jobs.run_inline` (R-45). 3. In both cases the `build_pipeline` handler (impl 02) calls `load_catalog` (U04-24) and then U04-56 with its `con` and `ctx`, so F04-02 and F04-13 apply unchanged. 4. A scoring failure makes the job fail; `--inline` then exits 1 (R-46). 5. `herness score --scenario` is the read-only F04-09 path and starts no job (impl 09). |

## 6. Error handling

| Failure condition | Class raised | Caught where | Retry / fallback | User-visible effect | Log event |
|-------------------|--------------|--------------|------------------|--------------------|-----------|
| Unknown metric, unsupported grain, bad period, disallowed filter, bad filter value, > 500 IDs, empty IDs, bad window, disabled metric, missing required column (interactive) | `ToolInputError` | spec 05 tool layer | none; agent retries with allowed values | error tool result listing allowed values | `metrics.compute.rejected` (WARNING) |
| SQL error or timeout in `compute_metric`/`metric_series`/`peer_group` (30 s) | `QueryError` | spec 05 tool layer, spec 07 | none | error tool result with hint | `metrics.query.failed` (ERROR) |
| Result above 1,000,000 rows (non-materialized) | `QueryError` | caller | none | error with hint | `metrics.query.failed` |
| Invalid catalog/weights YAML, forbidden column or placeholder, unknown bind, comment in SQL | `ConfigError` | CLI / job worker top level | none | scoring fails; build not promoted | `metrics.catalog.rejected` (ERROR) |
| Unconfirmed → confirmed without approval | `error` `ConfigIssue` from U04-83 (U04-22); `ConfigError` at start-up (R-71) | impl 10 start-up validation hook (T10-12) | none | config load refused | `metrics.weights.confirmation_rejected` (ERROR) |
| `requires_columns` missing in a build | none | U04-57 | metric skipped | `meta.dq_result` warn `score_metric_disabled` | `metrics.scoring.metric_disabled` (WARNING) |
| Warehouse locked when opening | `StoreBusy` | spec 08 resilience | retried per policy | delay | spec 08 events |
| `persist=True` on read-only; `build_id` mismatch; unknown scenario | `ConfigError` | caller (CLI, spec 06) | caller must use `persist=False` / fix input | error message | `metrics.portfolio.rejected` (ERROR) |
| Fact tables missing at scoring start; stage 400 input missing | `SchemaViolation` | job worker top level | none | build not promoted | `metrics.scoring.step_failed` (ERROR) |
| SQL error in a build step | `SchemaViolation` (from `QueryError`) | job worker top level | spec 08 job retry; finished steps skipped | build not promoted | `metrics.scoring.step_failed` |
| Invariant check failure | `SchemaViolation` | job worker top level | none | build not promoted; dq rows visible | `metrics.scoring.check_failed` (ERROR) |
| Nondeterministic result for a known `query_id` | `SchemaViolation` | job worker top level | none | build not promoted | `metrics.evidence.hash_conflict` (ERROR) |
| Mandatory candidate without effort / unknown mandatory | `ConfigError` | U04-81 (per scenario) | scenario skipped | warning in `ScoringReport.warnings` | `metrics.portfolio.rejected` (ERROR) |
| Solver infeasible, model invalid, time limit | none | U04-80 | flags | warning / flags; no rows for infeasible | `metrics.portfolio.infeasible` (WARNING) |
| Unsupported value type during hashing | `SchemaViolation` | caller | none | query fails | `metrics.query.failed` |

## 7. Security

### 7.1 Trust boundaries touched

| Boundary | How this component touches it |
|----------|-------------------------------|
| TB1 | Source record values (IDs, enums, timestamps, numbers) flow through facts into metrics and evidence samples |
| TB3 | Ticket text is never selected; the validator forbids text columns |
| TB4 | Model-chosen arguments of `get_metric` reach `compute_metric`; model-proposed weight changes reach the review queue payload |
| TB5 | `template_params`, `result_sample` and scores are rendered by spec 09 |
| TB7 | Dashboard custom budgets reach `optimize_portfolio(persist=False)` through spec 09 |
| TB10 | Operator-edited `metrics.yaml` (SQL templates) and `weights.yaml` |

### 7.2 STRIDE threats

| ID | Boundary | STRIDE | Threat | L | I | Control | Reference | Test |
|----|----------|--------|--------|---|---|---------|-----------|------|
| TH04-01 | TB4 | T, E | SQL injection through `entity_ids`, filter values or metric name chosen by a model | M | H | Allowlisted metric names, grains, filter keys and enums (U04-51); every value a typed bound parameter via `p()` (U04-35, U04-38); values never rendered | ASVS v5.0.0-V1.2; LLM05 | ST04-01, PT04-12 |
| TH04-02 | TB10 | T, E | Metric template runs DDL, reads files or attaches databases | L | H | Validator: one SELECT, schema allowlist, forbidden node types and functions (U04-26); build SQL runs on the build file only | ASVS v5.0.0-V1.2; ASVS v5.0.0-V15 | ST04-02 |
| TH04-03 | TB10 | E | Jinja template escapes the sandbox (SSTI) | L | H | `ImmutableSandboxedEnvironment`, `StrictUndefined`, empty globals, identifiers-only context (U04-33) | ASVS v5.0.0-V1 | ST04-03 |
| TH04-04 | TB3, TB5 | I | Personal data in metrics, scores or evidence samples via text columns | M | H | Forbidden identifier list in the validator; facts and score templates select no text column; `title` = key | ASVS v5.0.0-V14; LLM02 | ST04-04 |
| TH04-05 | TB10, TB4 | T, R | Silent weight tuning: marking weights confirmed, or a model-driven change, re-ranks funding | M | M | `check_weight_confirmations` gate tied to approved `weight_change` items and `config_hash`; payload has no free text; audit by spec 10 | ASVS v5.0.0-V2.3; LLM04 | ST04-05 |
| TH04-06 | TB5 | T, R | Stored numbers not reproducible from evidence (edited rows, nondeterministic SQL) | L | H | `query_id` + `result_hash` per stored query; hash conflict fails; `score_evidence_coverage` check; Verifier tolerance function | ASVS v5.0.0-V16; LLM09 | ST04-06 |
| TH04-07 | TB4 | D | Model requests huge ID lists, long windows or slow queries | M | M | 500-ID cap, 36-month cap, 30 s timeout, 1M-row cap | ASVS v5.0.0-V2.3; LLM10 | ST04-07 |
| TH04-08 | TB7, TB10 | D | Huge budgets or lists stall the optimizer | L | M | `Scenario` bounds; deterministic and wall-clock limits; one worker | ASVS v5.0.0-V2.2; LLM10 | ST04-08 |
| TH04-09 | TB4, TB7 | E, T | Read-only paths write to the warehouse, or recorded execution writes outside owned tables | L | H | Read-only connections for interactive APIs; `persist=True` refused on read-only; `WRITABLE_TABLES` allowlist | ASVS v5.0.0-V8 | ST04-09 |
| TH04-10 | TB10, TB5 | T, I | Rationale template placeholders with attribute access or format specs leak object internals | L | M | Placeholder allowlist, `^[a-z_]+$`, no conversion or spec (U04-26 step 9) | ASVS v5.0.0-V1 | ST04-10 |
| TH04-11 | — | I | Logs or error messages carry filter values or ticket text | L | M | Log fields are IDs and counts only; filter values logged only as counts; DuckDB messages never include ticket text because none is selected | ASVS v5.0.0-V16 | ST04-11 |
| TH04-12 | TB5 | T | Two different results hash equal (canonicalization ambiguity) | L | M | Typed header; quoted strings; type-driven encoding; manual row JSON keeps duplicate names | ASVS v5.0.0-V11 | ST04-12, PT04-02 |
| TH04-13 | TB1 | D | Cyclic `parent_key` or org parents explode recursion | M | M | Depth caps 10 and 20; min-depth dedupe; dq warning | ASVS v5.0.0-V2.3 | ST04-13 |
| TH04-14 | TB7 | S | Caller passes a `build_id` for another warehouse so evidence points at the wrong build | L | M | `build_id` checked against `meta.build` | ASVS v5.0.0-V2.2 | ST04-14 |

### 7.3 ASVS mapping

| ASVS section | Requirement area | Where |
|--------------|------------------|-------|
| ASVS v5.0.0-V1.2 | Injection prevention (parameterised queries) | U04-12, U04-35, U04-38 |
| ASVS v5.0.0-V2.2 | Input validation at boundaries | U04-51, U04-76, U04-15…U04-19 |
| ASVS v5.0.0-V2.3 | Business logic limits (caps, gates) | U04-22, U04-32, U04-51 |
| ASVS v5.0.0-V8 | Least privilege on data stores | U04-11, U04-80 |
| ASVS v5.0.0-V11 | Standard hashing (SHA-256 via `hashlib`) | U04-02, U04-05 |
| ASVS v5.0.0-V14 | No personal data in derived outputs | U04-26, U04-43…U04-45 |
| ASVS v5.0.0-V15 | Layering, safe deserialisation (`yaml.safe_load`, Arrow IPC, no pickle of data) | U04-04, U04-24 |
| ASVS v5.0.0-V16 | Logging without sensitive data; evidence integrity | §8, U04-12 |

### 7.4 LLM Top 10 and AI RMF

No model is called. The component bounds model-facing risks as follows: LLM01 not applicable (no prompts); LLM02 no text columns in outputs (TH04-04); LLM04 weight changes gated (TH04-05); LLM05 model arguments validated and bound (TH04-01); LLM09 every number from a recorded SELECT with hash (TH04-06); LLM10 caps and timeouts (TH04-07, TH04-08). AI RMF: Measure (invariant checks, planted-truth tests), Manage (failed checks block promotion; D2 banner through `unconfirmed`).

### 7.5 Secrets

None. The component reads no secret and holds no credential.

### 7.6 Data classification

| Field / artifact | Class |
|------------------|-------|
| Record IDs, numbers, service/team/org IDs, timestamps in facts and `metric_value` | internal |
| Dollar values (`*_usd`), weights, strategic weights, capacities | confidential |
| Team and org names in `template_params` | internal |
| `meta.evidence.sql`, `params`, `result_sample` | internal (samples hold no text columns) |
| `WeightChangePayload` | confidential |
| Log events and metric samples | internal |
| Personal data | none stored or emitted |

### 7.7 Accepted residual risks

| Risk | Reason | Owner |
|------|--------|-------|
| An operator with write access to `config/metrics.yaml` can write a misleading but valid metric | Config changes are audited (spec 10 `config_change`) and reviewed in git; operators are trusted | spec 10 security role |
| `mad`/`median` float order can differ between DuckDB versions | Verifier tolerance absorbs it; DuckDB is pinned | metrics owner |

## 8. Observability

### 8.1 Log events

| Event | Level | Fields | When |
|-------|-------|--------|------|
| `metrics.catalog.loaded` | INFO | `catalog_version`, `n_metrics`, `n_enabled` | U04-24 success |
| `metrics.catalog.rejected` | ERROR | `path`, `issue_count` | U04-24 failure |
| `metrics.query.recorded` | DEBUG | `query_id`, `producer`, `template`, `row_count`, `duration_ms`, `build_id` | U04-12 success |
| `metrics.query.failed` | ERROR | `query_id`, `template`, `error_class` | U04-12 failure |
| `metrics.evidence.hash_conflict` | ERROR | `query_id`, `build_id` | U04-12 step 8 |
| `metrics.facts.materialized` | INFO | `build_id`, `table`, `row_count`, `query_id`, `duration_ms` | U04-47 per table |
| `metrics.compute.completed` | INFO | `metric`, `entity_type`, `period`, `row_count`, `query_id`, `duration_ms` | U04-52 |
| `metrics.compute.rejected` | WARNING | `metric`, `reason` | U04-51 errors |
| `metrics.peer_group.resolved` | DEBUG | `entity_type`, `key`, `size`, `fallback`, `query_id` | U04-62 |
| `metrics.scoring.step_started` / `step_completed` / `step_skipped` / `step_failed` | INFO / INFO / INFO / ERROR | `build_id`, `step`, `duration_ms`, `rows` | U04-56 |
| `metrics.scoring.metric_disabled` | WARNING | `build_id`, `metric`, `missing_count` | U04-57 |
| `metrics.scoring.check_failed` | ERROR | `build_id`, `check_name`, `value`, `threshold` | U04-59 |
| `metrics.scoring.completed` | INFO | `build_id`, `steps_done`, `duration_ms` | U04-56 |
| `metrics.portfolio.solved` | INFO | `scenario`, `solver_status`, `n_candidates`, `n_selected`, `wall_s`, `persist` | U04-80 |
| `metrics.portfolio.infeasible` | WARNING | `scenario`, `mandatory_count`, `budget_usd` | U04-80 |
| `metrics.portfolio.rejected` | ERROR | `scenario`, `reason` | U04-80, U04-81 |
| `metrics.weights.confirmation_rejected` | ERROR | `blocks`, `config_hash` | F04-12 (logged by U04-83 with the U04-22 issues) |
| `metrics.weights.payload_invalid` | WARNING | `item_id` | U04-83 step 3 |

Component name `metrics`. No event carries filter values, SQL text above DEBUG, or any text column.

### 8.2 Metrics (ops `metric_sample`)

| Name | Type | Labels |
|------|------|--------|
| `herness_metrics_query_duration_seconds` | histogram | `producer` (`facts`, `metrics`, `score`, `none`) |
| `herness_metrics_query_rows_total` | counter | `producer` |
| `herness_metrics_step_duration_seconds` | histogram | `step` |
| `herness_metrics_compute_calls_total` | counter | `outcome` (`ok`, `input_error`, `query_error`) |
| `herness_metrics_check_failures_total` | counter | `check` |
| `herness_metrics_portfolio_solve_seconds` | histogram | `status`, `persist` |

### 8.3 Trace events and health

No `Tracer` events: spec 05's tool layer traces `get_metric` calls. No long-running component, so no `health()`; `herness doctor` and `herness config validate` run `validate_catalog` through the owner validator U04-83 registered on impl 10's start-up validation hook (T10-12, R-71), never from inside `herness.core.config` (R-03, DD04-20).

## 9. Configuration

All keys below are read through `get_config()` (spec 10); a change takes effect at the next process start or build (config is cached per process). Sensitivity: `metrics.yaml` internal; `weights.yaml` confidential.

| Key path | Type | Default | Validation | Restart | Sensitivity |
|----------|------|---------|------------|---------|-------------|
| `metrics.version` | int | 1 | `== 1` | yes | internal |
| `metrics.defaults.*` | see U04-16 | design 04 §4.1 | U04-16 ranges | yes | internal |
| `metrics.defaults.compute_timeout_s` | float | 30 | 1–300 (DD04-06) | yes | internal |
| `metrics.metrics[]` | list of `MetricDef` | 28 entries of U04-48 | U04-15, U04-26 | yes | internal |
| `metrics.scoring.as_of` | date or null | null | ISO date | yes | internal |
| `metrics.scoring.funding.tier_weights.*` | float | 0.8 / 0.6 / 0.3 | (0, 1] | yes | internal |
| `metrics.scoring.funding.min_direct_links` | int | 1 | 1–100 | yes | internal |
| `metrics.scoring.funding.window_days` | int | 365 | 30–1095 | yes | internal |
| `metrics.scoring.org.min_peer_group` | int | 5 | 2–100 | yes | internal |
| `metrics.scoring.org.trend_weight` | float | 0.5 | 0–5 | yes | internal |
| `metrics.scoring.org.min_weight_coverage` | float | 0.5 | 0–1 | yes | internal |
| `metrics.scoring.org.metrics` | map metric→weight | design 04 §7.1 | U04-26 step 8 | yes | internal |
| `metrics.scoring.peer_group.min_service_peers` | int | 3 | 1–100 | yes | internal |
| `metrics.scoring.levers.top_entities` | int | 50 | 1–10,000 | yes | internal |
| `metrics.scoring.levers.templates.<model>` | string | the design 04 §5.9 example sentence for every model | placeholders per U04-26 step 9 | yes | internal |
| `weights.business_timezone` | string | `America/New_York` | valid IANA zone | yes | internal |
| `weights.<block>.*` (14 blocks) | see U04-19 | design 04 §7.2 | U04-19 ranges | yes | confidential |
| `weights.<block>.unconfirmed` | bool | true | true→false gated by U04-22 | yes | confidential |
| `weights.portfolio.*` | see U04-19 | design 04 §7.2 | U04-19 | yes | confidential |

## 10. Performance and capacity

Hardware per design 02 §9 (16 cores, 64 GB RAM, NVMe); dataset `synth_data.py --scale full` (5M incidents, 0.5M changes, 2M events, 0.2M work items, ~5k candidates). All benchmarks carry marker `slow` in `tests/bench/test_metrics_bench.py`.

| ID | Measure | Threshold |
|----|---------|-----------|
| BT04-01 | `materialize_facts` wall time | < 90 s |
| BT04-02 | step `metrics` | < 120 s |
| BT04-03 | step `funding` (both passes) | < 45 s |
| BT04-04 | steps `org` + `levers` | < 20 s |
| BT04-05 | step `portfolio`, per scenario | < 30 s wall |
| BT04-06 | stage 400 + full `run_scoring` | < 5 min |
| BT04-07 | `compute_metric` (one metric, one grain, ≤ 24 periods) and `peer_group`, 50 calls | p95 < 2 s |
| BT04-08 | `optimize_portfolio(persist=False)`, one custom scenario | < 30 s wall |
| BT04-09 | parallel hashing of `metrics.incident_fact` (5M rows) | < 25 s |

Enforced limits: `MAX_RESULT_ROWS` 1,000,000; 500 entity IDs; 36-month windows; 30 s interactive timeout; `HASH_WORKERS` ≤ 8; Arrow batches of 10,000 rows; solver one worker, 20 deterministic seconds, 60 s wall; DuckDB threads and memory from `sources.yaml: build.threads`, `build.memory_limit` (set by spec 02 on the connection).

## 11. Test specification

Markers per design 11 §4.1. Property tests use Hypothesis profiles `commit`/`nightly` and carry marker `unit`. Fixture `metrics_tiny` = `tests/fixtures/metrics_tiny/*.csv` loaded by `tests/support/metrics_tiny.py` into an in-memory DuckDB with the spec 02 `core`/`enrich`/`meta` DDL (T02-12 (herness/model/sql/000_settings.sql) for the schemas, `meta.*` and empty `enrich.*` tables; T02-15…T02-17 for the `core.*` column shapes); weights as design 04 §10.1 (`cost_per_downtime_hour[1] = 10000`, `cost_per_engineer_hour = 100`). Time frozen with `freezegun`.

### 11.1 Unit tests (`unit`)

| ID | Unit / flow | Setup | Action | Expected |
|----|-------------|-------|--------|----------|
| UT04-01 | U04-01 | table of (value, type) cases for every type family | `encode_cell` | exact expected tokens; `fetchall` and Arrow values encode equally |
| UT04-02 | U04-05 | `tests/fixtures/result_hash_vectors.json` (≥ 12 vectors, created in T04-01) | `result_hash` | equals stored hashes |
| UT04-03 | U04-01, U04-05 | rows with `-0.0` vs `0.0` | hash | equal |
| UT04-04 | U04-05 | no rows | hash | `sha256(header + "\n")` |
| UT04-05 | U04-06 | 120 rows | `result_sample(limit=50)` | 50 rows, ascending digests |
| UT04-06 | U04-06 | float, decimal, timestamp rows | sample | `.9g` floats, decimal strings, ISO `Z` |
| UT04-07 | U04-12 | in-memory DuckDB with `meta.*` | run twice with `producer="metrics"` | one `meta.evidence` row; second call keeps it |
| UT04-08 | U04-12, U04-10 | same | `producer=None` | no evidence row; `rows` returned; `to_evidence` maps fields |
| UT04-09 | U04-11, U04-12 | table target | `into` replace/append; disallowed table | hash equals re-run of SELECT; `ConfigError` for `core.incident` |
| UT04-10 | U04-09, U04-12 | params with Decimal, date, datetime | canonicalize; compute `query_id` | JSON types; keys sorted; the params text equals `herness.core.ids.canonical_json` output and the ID equals `herness.core.ids.query_id` output (R-14) |
| UT04-11 | U04-07 | pairs differing by 1e-12 and by 1e-3 | compare | true / false |
| UT04-12 | U04-04, U04-08 | same rows via Arrow IPC and via lists | hash both ways | identical digests and hash |
| UT04-13 | U04-24, U04-23 | shipped `config/metrics.yaml` | load; reorder keys | loads; `version` unchanged by key order |
| UT04-14 | U04-26 | template selecting `short_description` | validate | error issue |
| UT04-15 | U04-15, U04-14 | unit `percent`, better `up` | validate | pydantic error |
| UT04-16 | U04-26 | two statements; missing `sample_size`; `severity` filter on incident | validate | three error issues |
| UT04-17 | U04-23 | — | `get("nope")` | `ToolInputError` listing names |
| UT04-18 | U04-23, U04-25 | — | `describe()` | keys per design; sorted |
| UT04-19 | U04-16…U04-19 | invalid values (missing priority key, clip reversed) | validate | errors with paths |
| UT04-20 | U04-21, U04-22 | previous unconfirmed, current confirmed; with and without matching approved payload | check | issue without; none with |
| UT04-21 | U04-26 | templates `{entity_name.__class__}`, `{delta_usd:>10}`, `{foo}` | validate | three errors |
| UT04-22 | U04-26 | scorecard includes `incident_count` | validate | error |
| UT04-23 | U04-35 | each (source, grain) | render `entity_col`/`entity_join` | expected expression; unsupported pairs fail |
| UT04-24 | U04-38 | filter value `x' OR 1=1 --` | render | value absent from SQL; present in bind |
| UT04-25 | U04-35 | periods week…t12m | `period_start`, `period_spine` | expected SQL fragments; one spine row for t12 |
| UT04-26 | U04-34, U04-36…U04-39 | render a metric | inspect bind | only used names; types from `BIND_TYPES`; unknown name → `ConfigError` |
| UT04-27 | U04-30, U04-31 | `as_of` on Monday, mid-week, month start | default windows | complete periods only |
| UT04-28 | U04-31 | `as_of` 2024-02-29 | t12m | start 2023-02-28 |
| UT04-29 | U04-32 | start ≥ end; 37-month span | custom window | `ToolInputError` |
| UT04-30 | U04-43 | `metrics_tiny` incidents I1–I4 | materialize | design 04 §10.1 values |
| UT04-31 | U04-43 | memberships 0.4/0.6/0.6 tie | materialize | highest ≥ 0.5, lowest ID on tie |
| UT04-32 | U04-44 | changes with outcomes and links | materialize | `deployed`, `failed`, counts, lead time |
| UT04-33 | U04-45 | items with transitions | materialize | timestamps, `cycle_days`, `is_unplanned` |
| UT04-34 | U04-41, U04-42 | org tree; parent_key cycle | materialize | closures; candidate; depth cap |
| UT04-35 | U04-46, U04-47 | stage file | split; materialize | five tables; five evidence rows; bad marker → `ConfigError` |
| UT04-36…UT04-63 | U04-48 metrics #1…#28 in table order | `metrics_tiny` rows per domain | `compute_metric` for every declared grain | hand-computed values (for #1–#14 the design 04 §10.1 values) |
| UT04-64 | U04-40, U04-52 | sample below `min_sample_size` | compute | value NULL, `insufficient_sample` |
| UT04-65 | U04-51 | each invalid input | compute | `ToolInputError` with allowed values |
| UT04-66 | U04-12, U04-52 | slow query, timeout 0.1 s | compute | `QueryError` timeout |
| UT04-67 | U04-53 | window (start, end) | series vs compute | same rows and `query_id`; callback once |
| UT04-68 | U04-40, U04-20 | unconfirmed weights; low coverage; partial period | compute | flags present |
| UT04-69 | U04-52 | `entity_ids` subset | compute | only those entities |
| UT04-70 | U04-52 | `priority=[1]`, `org_id` parent | compute | filtered counts; org filter includes descendants |
| UT04-71 | U04-62 | teams in buckets; metric with few values | `peer_group` | keys spelled `team:crit_hi` (R-61), fallback `team:all` |
| UT04-72 | U04-62 | services crit 2 | `peer_group` | `service:crit_2`, self excluded |
| UT04-73 | U04-62 | item with service; item via map; cluster_fix; unresolved | `peer_group` | owning service group; unresolved → size 0 `prior_year` |
| UT04-74 | U04-62 | 2 peers | `peer_group` | `prior_year`, members kept |
| UT04-75 | U04-64 | incident linked directly to two epics | funding | shares 0.5/0.5 |
| UT04-76 | U04-64 | direct, cluster and service paths | funding | only tier 1 kept |
| UT04-77 | U04-64 | root-cause decision matching cluster | funding | tier-2 root-cause weight |
| UT04-78 | U04-64 | epic under initiative, both service-matched | funding | only epic path |
| UT04-79 | U04-64 | cluster above/below thresholds | funding | cluster_fix candidate only above |
| UT04-80 | U04-65 | 180 days of history | funding | annualized × 365/180; `short_history` absent at 180, present at 179 |
| UT04-81 | U04-65 | estimate; points only; none; cluster_fix | funding | effort chain values; `no_estimate` |
| UT04-82 | U04-65 | known pain/quality/n | funding | confidence formula |
| UT04-83 | U04-65 | portfolio/org weights, clip | funding | clipped product; default 1.0 |
| UT04-84 | U04-65, U04-66 | 5-candidate table with ties | funding | priority and rank per design ties |
| UT04-85 | U04-65 | 5 candidates | funding | Fibonacci mapping, P1 step, WSJF |
| UT04-86 | U04-64 | noise events, failed change | funding | tier 3 only; usd per formula |
| UT04-87 | U04-65 | parent with child candidate | funding | parent pain = own + child |
| UT04-88 | U04-67 | values with MAD 0; all equal | org | 1.2533 fallback; z = 0 |
| UT04-89 | U04-67 | 12 weeks slope 2 with one outlier; 5 points | org | slope 2.0; NULL |
| UT04-90 | U04-67, U04-68 | missing metrics below coverage | org | composite NULL, `low_coverage`; ranks |
| UT04-91 | U04-67 | teams with crit 1, 3, none | org | buckets hi/lo/none |
| UT04-92…UT04-98 | U04-69 models mttr, repeat, reopen, reassign, sla, cfr, noise | hand-computed entity | levers | `delta_usd` per formula |
| UT04-99 | U04-69, U04-70 | target not improving; penalty 0; rank > top | levers | rows skipped; `unconfirmed` per model |
| UT04-100 | U04-69, U04-71 | any lever | inspect | `template_params` keys = `LEVER_PLACEHOLDERS` |
| UT04-101 | U04-72, U04-73, U04-75, U04-77, U04-79 | small instance | solve | constraints hold; binding names |
| UT04-102 | U04-74 | chain and cycle | order | topological; cycle flagged |
| UT04-103 | U04-76, U04-78 | name, `1500000`, unknown, overlap | resolve | scenarios; `ConfigError` |
| UT04-104 | U04-73 | mandatory without effort; unknown mandatory | solve | `ConfigError` |
| UT04-105 | U04-73, U04-80, U04-81 | mandatory above budget | solve / step | `INFEASIBLE`, no rows, warning |
| UT04-106 | U04-73 | instance with tiny deterministic limit | solve | `FEASIBLE` + `not_proven_optimal` |
| UT04-107 | U04-80 | read-only connection | `persist=True` | `ConfigError` |
| UT04-108 | U04-80 | read-only file, fake ops store | `persist=False` | ops evidence row; warehouse unchanged |
| UT04-109 | U04-78, U04-80 | `unconstrained` | solve | budget = Σ effort; capacity ignored |
| UT04-110 | U04-54, U04-55, U04-56 | tiny build | `run_scoring` | steps in order; report counts |
| UT04-111 | U04-56 | `steps=["org","levers"]`; `["bogus"]` | run | only those (plus validate); `ConfigError` |
| UT04-112 | U04-56 | fake `JobContext` with state | run | done steps skipped |
| UT04-113 | U04-57 | no fact tables | run | `SchemaViolation` |
| UT04-114 | U04-57 | `acknowledged_at` all NULL | run | `mtta_minutes` skipped; dq warn |
| UT04-115 | U04-59, U04-60 | corrupt a ratio | check | dq row failed; `SchemaViolation` |
| UT04-116 | U04-56 | build status `promoted` | run | `ConfigError` |
| UT04-117 | U04-27 | — | `unit_for` | design 04 §4.3 table |
| UT04-118 | U04-58 | tiny build | metrics step | every enabled metric × grain × 5 periods has rows or recorded evidence |
| UT04-119 | U04-22, U04-82 | previous unconfirmed, current confirmed, no approval; a `WeightIssue` built with an empty message | check; construct | returns `WeightIssue` objects (not `ConfigIssue`) with path `weights.<block>.unconfirmed`; empty message → `ConfigError`; an import scan of `herness/metrics/settings.py` finds only the standard library, pydantic, `herness.core.types` and `herness.core.errors` (R-03) |
| UT04-120 | U04-83 | fake ops store with one approved `weight_change` payload; config snapshot with the block unconfirmed; catalog with one forbidden column; missing `LAST` | run validator | catalog `error` issue kept; confirmed block with a matching payload gives no issue, without it one `error` `ConfigIssue` at `weights.<block>.unconfirmed` with `file` `weights.yaml`; missing `LAST` treated as first load; registered on the hook, `run_owner_validators` returns the same issues |
| UT04-121 | U04-56 | read-only connection; call without `con` | `run_scoring` | `ConfigError` for the read-only connection; a call without `con` is a `TypeError` (required keyword); an import scan of `herness/metrics/` finds no `herness.store._warehouse_rw` import (C12) |
| UT04-122 | U04-80 | `persist=True`, `build_id` given, no `con` | `optimize_portfolio` | `ConfigError("persist=True needs the build writer's connection")`; no file opened |

### 11.2 Property tests (`unit`, Hypothesis)

| ID | Property |
|----|----------|
| PT04-01 | `result_hash` invariant under row permutation |
| PT04-02 | Changing any single cell changes the hash (random typed rows) |
| PT04-03 | Random fact data: durations ≥ 0; ratios in [0, 1]; counts non-negative integers; SQL equals `metrics_oracle` |
| PT04-04 | Count metric at `quarter` equals the sum of its `month` values; ratios recomputed from summed numerator/denominator match |
| PT04-05 | Random link graphs: `Σ share ≤ 1 + 1e-9` per record; `Σ_k` own pain ≤ total + 0.01 |
| PT04-06 | `confidence ∈ [0.05, 1]` |
| PT04-07 | Robust z and Theil–Sen equal the oracle |
| PT04-08 | `order_selected` respects every blocks edge outside cycles |
| PT04-09 | Random instances ≤ 15 candidates: objective equals brute-force optimum |
| PT04-10 | Budget, capacity, mandatory, excluded, parent/descendant and blocker constraints hold |
| PT04-11 | `rows_equivalent` is symmetric and reflexive |
| PT04-12 | Random filter strings never appear in rendered SQL |

### 11.3 Integration tests (`integration`)

| ID | Scope | Expected |
|----|-------|----------|
| IT04-01 | Stage 400 + `run_scoring` on `lake_small`; re-run every `meta.evidence` query | every hash equal or `rows_equivalent` |
| IT04-02 | Same build | every `query_id` in `metrics.*`/`score.*` exists in `meta.evidence` |
| IT04-03 | `metric_series` vs `compute_metric` | identical rows, one `query_id` |
| IT04-04 | `peer_group` vs `score.org.peer_group` for every team/org × scorecard metric | equal keys |
| IT04-05 | `optimize_portfolio` 5 runs | identical output |
| IT04-06 | `meta.evidence.result_sample` | ≤ 50 rows equal to the first rows in hash order |
| IT04-07 | `persist=False` on promoted copy vs `persist=True` on build file | same selection; no warehouse writes |
| IT04-08 | `synth_data.py --scale small` (nightly `full`, marker `slow`) | design 04 §10.3 planted truths; the truth comparison lives under `tests/`, and nothing under `herness/metrics/` reads truth files (R-64) |
| IT04-09 | `compute_metric` with `con=None` on `CURRENT` | read-only; file mtime unchanged |
| IT04-10 | Repository scan | `result_hash`, `rows_equivalent` and `iter_batch_rows` defined only in `herness/metrics/evidence.py` (R-15) |
| IT04-11 | Repository scan of `herness/metrics/` | no definition of `canonical_json`, `normalize_sql` or `query_id` (they are imported from `herness.core.ids`, R-14); no reference to truth files (R-64); no import of `herness.enrich` or `herness.harness` |

### 11.4 Fault tests (`fault`)

| ID | Fault | Expected |
|----|-------|----------|
| FT04-01 | Terminate the job's child process (OS-level kill, no fault point, so impl 08's fault-point registry is unchanged, R-40) after `metrics.scoring.step_started` for `funding`, then restart the job | `metrics` skipped via checkpoint; funding fully rewritten; tables equal a clean run |
| FT04-02 | Warehouse file locked by another writer | `StoreBusy` raised on open |
| FT04-03 | Metric template raising a DuckDB error | `SchemaViolation`, step rolled back, build not promoted |
| FT04-04 | Solver wall limit 1 s on a 5k-candidate instance | `FEASIBLE` or flags `no_solution_found`/`wall_clock_limit`; no exception |
| FT04-05 | `enrich.cluster_member` missing | `materialize_facts` raises `SchemaViolation` |

### 11.5 Security tests (`unit` unless noted)

| ID | Threat | Attack and assertion |
|----|--------|----------------------|
| ST04-01 | TH04-01 | Injection strings in `entity_ids` and filters; SQL text unchanged, query returns no rows, no error from injected SQL |
| ST04-02 | TH04-02 | Templates with `COPY`, `ATTACH`, `read_csv`, `CREATE`; validator rejects each |
| ST04-03 | TH04-03 | Templates with `{{ ''.__class__.__mro__ }}` and `{{ cycler.__init__ }}`; `SecurityError`/`ConfigError` |
| ST04-04 | TH04-04 | Template selecting `description` through an alias; rejected; fact and score outputs contain no text column (schema scan) |
| ST04-05 | TH04-05 | Flip `unconfirmed` without approval, with approval for another hash; both refused |
| ST04-06 | TH04-06 | Edit a stored `score.funding` value, re-run; check `score_evidence_coverage`/rerun hash mismatch detected (integration) |
| ST04-07 | TH04-07 | 501 IDs; 40-month window; slow query; each rejected or timed out |
| ST04-08 | TH04-08 | Budget 1e13; 2000 mandatory IDs; rejected by `Scenario` |
| ST04-09 | TH04-09 | `persist=True` on read-only; `IntoSpec("core.incident")`; both refused |
| ST04-10 | TH04-10 | Placeholder attribute access and format spec; rejected |
| ST04-11 | TH04-11 | Capture logs during F04-07 with PII-like filter values; values absent from all records |
| ST04-12 | TH04-12 | Rows `["1"]` VARCHAR vs `[1]` INTEGER, and duplicate column names; hashes differ |
| ST04-13 | TH04-13 | 1,000-node `parent_key` cycle; closure bounded by depth 10; completes < 5 s |
| ST04-14 | TH04-14 | `build_id` of another build; `ConfigError` |

Benchmarks BT04-01…BT04-09 are in §10.

## 12. Task cards

All cards are Phase 2. Acceptance for every card also includes: `ruff check`, `ruff format --check`, `mypy --strict herness/metrics` and `lint-imports` pass.

#### T04-01 Result hash and encoding

| Field | Content |
|-------|---------|
| Goal | `result_hash`, `result_sample`, `rows_equivalent`, `iter_batch_rows`, `canonical_params` and the encoders exist with golden vectors. |
| Depends on | T00-03 (herness.core.errors), T00-05 (herness.core.ids.canonical_json) (R-14) |
| Units | U04-01…U04-09, U04-13 |
| Files | `herness/metrics/__init__.py`, `herness/metrics/_encode.py`, `herness/metrics/evidence.py` |
| Tests | UT04-01…UT04-06, UT04-11, UT04-12, PT04-01, PT04-02, PT04-11, ST04-12 |
| Threats | TH04-12 |
| Acceptance checks | `pytest -m unit -k "UT04-0 or UT04-11 or UT04-12 or PT04-0 or PT04-11 or ST04-12"` passes; `tests/fixtures/result_hash_vectors.json` committed with ≥ 12 vectors |
| Blocked by | VI04-02 |
| Size | M |

#### T04-02 Settings and config files

| Field | Content |
|-------|---------|
| Goal | Pydantic section models, weight gate, `weight_change` payload, `config/weights.yaml` and the non-metric sections of `config/metrics.yaml`. |
| Depends on | T00-03 (herness.core.errors), T00-08 (herness.core.types) (package skeleton, R-01) |
| Units | U04-14…U04-22, U04-82 |
| Files | `herness/metrics/settings.py`, `config/metrics.yaml`, `config/weights.yaml` |
| Tests | UT04-15, UT04-19, UT04-20, UT04-119, ST04-05 |
| Threats | TH04-05 |
| Acceptance checks | Tests pass; `lint-imports` confirms `settings.py` imports only the standard library, pydantic, `herness.core.types` and `herness.core.errors` (R-03) |
| Blocked by | none |
| Size | M |

#### T04-03 Catalog and validator

| Field | Content |
|-------|---------|
| Goal | `MetricCatalog`, loaders, validator, `SCORE_UNITS`, flags and filter matrix. |
| Depends on | T04-02, T04-04 (render used by the validator is stubbed until then; validator tests run after T04-04), T10-03 (herness.core.config.get_config), T10-03 (herness.core.config.ConfigIssue), T00-05 (herness.core.ids.canonical_json), T00-05 (herness.core.ids.sha256_hex), T10-12 (herness.core.config_validate.OwnerValidator), T10-05 (herness.core.audit.record_config_change) (snapshot files), T02-07 (herness.store.ops.shared.list_review_items) |
| Units | U04-23…U04-28, U04-83 |
| Files | `herness/metrics/catalog.py` |
| Tests | UT04-13, UT04-14, UT04-16…UT04-18, UT04-21, UT04-22, UT04-117, UT04-120, ST04-02, ST04-04, ST04-10 |
| Threats | TH04-02, TH04-04, TH04-10 |
| Acceptance checks | Tests pass; `herness metrics list` (spec 09) prints `describe()` output once wired |
| Blocked by | none |
| Size | M |

#### T04-04 Windows, rendering and macros

| Field | Content |
|-------|---------|
| Goal | Window arithmetic, sandboxed rendering, macros and the metric wrapper. |
| Depends on | T04-02 |
| Units | U04-29…U04-40 |
| Files | `herness/metrics/windows.py`, `herness/metrics/render.py`, `herness/metrics/sql/_macros.sql.j2`, `herness/metrics/sql/metric_wrapper.sql.j2` |
| Tests | UT04-23…UT04-29, PT04-12, ST04-01 (render part), ST04-03 |
| Threats | TH04-01, TH04-03, TH04-07 |
| Acceptance checks | Tests pass; rendered SQL for every grain parses with sqlglot |
| Blocked by | VI04-04, VI04-05 |
| Size | M |

#### T04-05 Recorded execution

| Field | Content |
|-------|---------|
| Goal | `RecordedQuery`, `IntoSpec`, `run_recorded` with evidence writes and parallel hashing. |
| Depends on | T04-01, T00-05 (herness.core.ids.query_id), T00-05 (herness.core.ids.normalize_sql), T00-04 (herness.core.time.now), T02-12 (herness/model/sql/000_settings.sql, `meta.evidence` table), T08-05 (herness.store.ops.metrics.record_metric_samples) |
| Units | U04-10…U04-12 |
| Files | `herness/metrics/evidence.py` |
| Tests | UT04-07…UT04-10, ST04-06 (unit part), ST04-09 (IntoSpec), BT04-09 |
| Threats | TH04-06, TH04-09 |
| Acceptance checks | Tests pass; `metrics.query.recorded` asserted in a log-capture test |
| Blocked by | VI04-01 |
| Size | M |

#### T04-06 Stage 400: closures and incident facts

| Field | Content |
|-------|---------|
| Goal | `400_facts.sql` with closures and `incident_fact`, and `materialize_facts`. |
| Depends on | T04-04, T04-05, T02-19 (herness.model.build._stage_score, which calls `materialize_facts`), T02-12 (herness/model/sql/000_settings.sql, schemas `core`/`enrich`/`meta` and the empty `enrich.*` tables), T02-15…T02-17 (`core.*` table shapes, files 200–280) |
| Units | U04-41, U04-42, U04-43, U04-46, U04-47 |
| Files | `herness/model/sql/400_facts.sql`, `herness/metrics/facts.py` |
| Tests | UT04-30, UT04-31, UT04-34, UT04-35, ST04-13, FT04-05 |
| Threats | TH04-13 |
| Acceptance checks | Tests pass on `metrics_tiny` (fixture rows created in this card) |
| Blocked by | none |
| Size | M |

#### T04-07 Stage 400: change and work-item facts

| Field | Content |
|-------|---------|
| Goal | `change_fact` and `work_item_fact` statements. |
| Depends on | T04-06 |
| Units | U04-44, U04-45 |
| Files | `herness/model/sql/400_facts.sql` |
| Tests | UT04-32, UT04-33 |
| Threats | TH04-04 |
| Acceptance checks | Tests pass; `materialize_facts` returns five query IDs |
| Blocked by | none |
| Size | S |

#### T04-08 Compute API and first metrics

| Field | Content |
|-------|---------|
| Goal | `compute_metric`, `metric_series`, request validation, and metrics #1–#4. |
| Depends on | T04-03, T04-07, T02-09 (herness.store.warehouse.open_readonly) |
| Units | U04-49…U04-53, U04-48 (#1–#4) |
| Files | `herness/metrics/compute.py`, `config/metrics.yaml` |
| Tests | UT04-36…UT04-39, UT04-64…UT04-70, ST04-01, ST04-07, ST04-11 |
| Threats | TH04-01, TH04-07, TH04-11 |
| Acceptance checks | Tests pass; `compute_metric("mttr_hours", "team", None, "quarter")` on `metrics_tiny` gives 4.0 |
| Blocked by | none |
| Size | M |

#### T04-09 Ops metrics #5–#14

| Field | Content |
|-------|---------|
| Goal | Catalog entries #5–#14. |
| Depends on | T04-08 |
| Units | U04-48 (#5–#14) |
| Files | `config/metrics.yaml` |
| Tests | UT04-40…UT04-49 |
| Threats | TH04-04 |
| Acceptance checks | Tests pass with design 04 §10.1 values |
| Blocked by | none |
| Size | M |

#### T04-10 Change and monitoring metrics #15–#19, #27–#28

| Field | Content |
|-------|---------|
| Goal | Catalog entries #15–#19, #27, #28 and macro `period_end_date`. |
| Depends on | T04-08 |
| Units | U04-48 (#15–#19, #27, #28), U04-35 (`period_end_date`) |
| Files | `config/metrics.yaml`, `herness/metrics/sql/_macros.sql.j2` |
| Tests | UT04-50…UT04-54, UT04-62, UT04-63 |
| Threats | none |
| Acceptance checks | Tests pass |
| Blocked by | none |
| Size | M |

#### T04-11 Delivery metrics #20–#26

| Field | Content |
|-------|---------|
| Goal | Catalog entries #20–#26 with `period_spine` and `cat_at`. |
| Depends on | T04-08 |
| Units | U04-48 (#20–#26) |
| Files | `config/metrics.yaml` |
| Tests | UT04-55…UT04-61, PT04-03, PT04-04 |
| Threats | none |
| Acceptance checks | Tests pass; `validate_catalog` returns no issues for the full catalog |
| Blocked by | none |
| Size | M |

#### T04-12 Scoring context

| Field | Content |
|-------|---------|
| Goal | `StepContext`, `StepResult`, `ScoringReport`. |
| Depends on | T04-04 |
| Units | U04-54 |
| Files | `herness/metrics/context.py` |
| Tests | UT04-110 (context part) |
| Threats | none |
| Acceptance checks | Tests pass |
| Blocked by | none |
| Size | S |

#### T04-13 Scoring runner, validate and metrics steps

| Field | Content |
|-------|---------|
| Goal | `run_scoring` with checkpointing, validate step and metrics step; base checks. |
| Depends on | T04-11, T04-12, T08-03 (herness.core.jobs.JobContext), T10-03 (herness.core.config.config_hash) |
| Units | U04-55…U04-60 |
| Files | `herness/metrics/scoring.py`, `herness/metrics/sql/checks.sql.j2` |
| Tests | UT04-110…UT04-116, UT04-118, UT04-121, FT04-01 (metrics part), FT04-03 |
| Threats | TH04-06 |
| Acceptance checks | Tests pass; `run_scoring(build_id, steps=["metrics","check"])` on the tiny build writes `metric_value` and dq rows; `herness score --inline` wiring (impl 09, R-45) needs no change here because it reaches U04-56 through the `build_pipeline` handler |
| Blocked by | none |
| Size | M |

#### T04-14 Funding attribution

| Field | Content |
|-------|---------|
| Goal | Both passes and `score.funding_attribution`. |
| Depends on | T04-13 |
| Units | U04-64, U04-66 (attribution part) |
| Files | `herness/metrics/sql/funding_attribution.sql.j2`, `herness/metrics/funding.py` |
| Tests | UT04-75…UT04-79, UT04-86, PT04-05 |
| Threats | TH04-04 |
| Acceptance checks | Tests pass |
| Blocked by | none (root-cause path inactive without decisions; OI04-04) |
| Size | M |

#### T04-15 Funding score

| Field | Content |
|-------|---------|
| Goal | `score.funding` with confidence, weights, effort, priority, WSJF and rank. |
| Depends on | T04-14 |
| Units | U04-65, U04-66 |
| Files | `herness/metrics/sql/funding_score.sql.j2`, `herness/metrics/funding.py` |
| Tests | UT04-80…UT04-85, UT04-87, PT04-06 |
| Threats | none |
| Acceptance checks | Tests pass |
| Blocked by | none |
| Size | M |

#### T04-16 Org score

| Field | Content |
|-------|---------|
| Goal | `score.org`. |
| Depends on | T04-13 |
| Units | U04-67, U04-68, U04-35 (`team_bucket`, `observed_days`) |
| Files | `herness/metrics/sql/org_score.sql.j2`, `herness/metrics/org.py`, `herness/metrics/sql/_macros.sql.j2` |
| Tests | UT04-88…UT04-91, PT04-07 |
| Threats | none |
| Acceptance checks | Tests pass |
| Blocked by | VI04-06 |
| Size | M |

#### T04-17 Peer groups

| Field | Content |
|-------|---------|
| Goal | `peer_group` and its query; re-export from `compute.py`. |
| Depends on | T04-16 |
| Units | U04-61…U04-63 |
| Files | `herness/metrics/peers.py`, `herness/metrics/sql/peer_group.sql.j2`, `herness/metrics/compute.py` |
| Tests | UT04-71…UT04-74 |
| Threats | TH04-01 |
| Acceptance checks | Tests pass |
| Blocked by | none |
| Size | M |

#### T04-18 Action levers

| Field | Content |
|-------|---------|
| Goal | `score.action_lever`. |
| Depends on | T04-16 |
| Units | U04-69…U04-71 |
| Files | `herness/metrics/sql/levers.sql.j2`, `herness/metrics/levers.py` |
| Tests | UT04-92…UT04-100 |
| Threats | TH04-10 |
| Acceptance checks | Tests pass |
| Blocked by | none |
| Size | M |

#### T04-19 Portfolio solver (pure)

| Field | Content |
|-------|---------|
| Goal | CP-SAT model, ordering and binding constraints. |
| Depends on | T04-02 |
| Units | U04-72…U04-75 |
| Files | `herness/metrics/_solver.py` |
| Tests | UT04-101, UT04-102, UT04-104…UT04-106, PT04-08…PT04-10, FT04-04 |
| Threats | TH04-08 |
| Acceptance checks | Tests pass; brute-force property test green at 200 examples |
| Blocked by | VI04-07 |
| Size | M |

#### T04-20 Portfolio API and step

| Field | Content |
|-------|---------|
| Goal | `Scenario`, `PortfolioResult`, `optimize_portfolio`, `run_portfolio_step`, wired into `run_scoring`. |
| Depends on | T04-15, T04-19, T05-12 (herness.store.ops.evidence.record_evidence), T05-02 (herness.core.types.harness.Evidence) |
| Units | U04-76…U04-81 |
| Files | `herness/metrics/portfolio.py`, `herness/metrics/sql/portfolio_input.sql.j2`, `herness/metrics/scoring.py` |
| Tests | UT04-103, UT04-107…UT04-109, UT04-122, ST04-08, ST04-09, ST04-14 |
| Threats | TH04-08, TH04-09, TH04-14 |
| Acceptance checks | Tests pass |
| Blocked by | VI04-03 |
| Size | M |

#### T04-21 Complete checks and wiring

| Field | Content |
|-------|---------|
| Goal | All U04-60 checks, all steps wired, observability metrics emitted. |
| Depends on | T04-15, T04-17, T04-18, T04-20 |
| Units | U04-56, U04-59, U04-60 |
| Files | `herness/metrics/scoring.py`, `herness/metrics/sql/checks.sql.j2` |
| Tests | UT04-115, FT04-01, FT04-02, ST04-06 |
| Threats | TH04-06 |
| Acceptance checks | Full `run_scoring` on the tiny build passes all checks; metric samples asserted |
| Blocked by | none |
| Size | S |

#### T04-22 Integration, planted truths and benchmarks

| Field | Content |
|-------|---------|
| Goal | Integration, planted-truth and benchmark tests (no production files). |
| Depends on | T04-21, T11-14 (tools/synth_data.py) |
| Units | — |
| Files | none (tests only) |
| Tests | IT04-01…IT04-11, BT04-01…BT04-08 |
| Threats | TH04-06 |
| Acceptance checks | `pytest -m integration -k IT04` passes; `pytest -m slow -k BT04` meets §10 on the dev box |
| Blocked by | none |
| Size | M |

## 13. Design deltas and open items

### 13.1 Design deltas (contract changes requested)

Status values follow the consistency pass: "Resolved by R-nn" (a ruling settled it), "Accepted (R-nn)" (a ruling adopted the delta as written) or "Still open" (no ruling covers it; the design spec change is still pending). All rulings are in [`DECISIONS.md`](DECISIONS.md).

| ID | Design spec | Change | Status |
|----|-------------|--------|--------|
| DD04-01 | 04 §3.1 | `run_recorded` gains keyword-only `build_id`, `into`, `timeout_s`, `max_rows` (needed for `query_id` and materialization) | Still open |
| DD04-02 | 04 §3.1, 08 | `run_scoring` gains keyword-only `con` (required, DD04-21) and `ctx: JobContext` (checkpointing needs the job context) | Still open (consistent with R-42 and R-45: the handler and `run_inline` both supply `ctx`) |
| DD04-03 | 04 §3.1, 07 | `metric_series` and `peer_group` gain keyword-only `on_evidence` callback; otherwise callers cannot persist `sql`/`params`/`result_hash` as §3.1 requires | Still open |
| DD04-04 | 04 §4.1 | Template contract allows optional columns `coverage`, `estimated_count`, `unweighted`; flag vocabulary adds `low_coverage`, `partial_period`, `unweighted` | Still open |
| DD04-05 | 04 §4.1, §5.2 | `change_count` is a per-week rate (aggregation `ratio`, denominator = weeks), an exception to "count metrics set numerator = value, denominator NULL" | Still open |
| DD04-06 | 04 §7.1 | New key `metrics.defaults.compute_timeout_s` (30) | Still open |
| DD04-07 | 00 §5.1 | Pin canonical encoding: compact separators, `ensure_ascii=False`, float token text `.9g` with `-0` normalized and non-finite as strings, DECIMAL quantized to scale, 6-digit UTC timestamps, type-driven encoding, header types = DuckDB type strings; golden vectors live in `tests/fixtures/result_hash_vectors.json` (00 §5.1 has none) | Accepted (R-15): impl 04 owns `result_hash` with these pinned rules; `canonical_json` (R-14) stays the encoder for `query_id` and `params` only |
| DD04-08 | 04 §4.4 | Stored tables append `query_id`/`query_ids`; the hash excludes that column so it equals a re-run of the recorded SELECT | Still open |
| DD04-09 | 04 §4.1, 05 | All bind parameters are cast in SQL and bound from JSON-round-tripped values so the Verifier's re-bind of `params.bind` is exact | Still open |
| DD04-10 | 04 §3.1 | CP-SAT `UNKNOWN` maps to `INFEASIBLE` + flag `no_solution_found`; alternative: add `UNKNOWN` to `solver_status` | Still open |
| DD04-11 | 04 §10.2 | Pain invariant evaluated on unrounded values (rounded cents across ~5k candidates can exceed 0.01) | Still open |
| DD04-12 | 04, 02 §4.6 | `score.funding.title` = work-item key / candidate ID (no text column; see D9) | Still open |
| DD04-13 | 11 §5.1.5 | Spec 11 writes `team:crithi`; spec 04 key is `team:crit_hi`; spec 11 should change | Resolved by R-61 (`team:crit_hi`; impl 11 changes its spelling) |
| DD04-14 | 05 §5.6 | Verifier should call `herness.metrics.evidence.rows_equivalent` on hash mismatch (04 §4.4 tolerance not reflected in spec 05) | Accepted (R-15) |
| DD04-15 | 05 §5.4.2 | `get_metric` caps IDs at 50, spec 04 at 500: consistent (tool stricter); spec 05 should state `window` end is exclusive | Still open |
| DD04-16 | 04 §4.1 | Forbidden identifiers extended with `text`, `label`, `top_terms`, `alert_name`, `host`, `answer` | Still open |
| DD04-17 | 04 §3.1 | Literals and `MetricDef` defined in `settings.py`, re-exported from `catalog.py` (spec 10 imports settings without DuckDB) | Still open (placement allowed by R-03) |
| DD04-18 | 04 §3.2 | `run_scoring` refuses builds whose status is not `building` | Still open |
| DD04-19 | 04 §3.2, 09 | `herness score` enqueues a `build_pipeline` job by default and runs it in-process only with the admin-only `--inline` flag through `herness.core.jobs.run_inline`; design 04 §3.2 says the command calls `load_catalog` and `run_scoring` directly (F04-14) | Resolved by R-45 |
| DD04-20 | 10 §3.1, 09 | `herness.core.config` may import only `settings.py` modules (R-03), so the config loader cannot run the catalog SQL cross-check `validate_catalog` (it lives in `catalog.py`, which needs `sqlglot` and `jinja2`), and `check_weight_confirmations` cannot return `ConfigIssue`. This spec moves both checks into the owner validator U04-83, which the composition root registers on impl 10's start-up validation hook (T10-12 (herness.core.config_validate.register_owner_validator)) and runs after `load_config` (T09-20 (herness.cli.run_startup_validation)); `config validate` and `doctor` run it through T10-12 (herness.core.config.validate). `check_weight_confirmations` keeps returning `WeightIssue` (U04-82); U04-83 converts it | Resolved by R-71 |
| DD04-21 | 04 §3.1, 02 | Write paths take the build pipeline's connection: `run_scoring` has a required keyword-only `con`, and `optimize_portfolio(persist=True)` without `con` raises `ConfigError`. This package never imports `herness.store._warehouse_rw` (impl 02 `store-rw-restricted` allows only `herness.model.build` and `herness.model.promote`) | Resolves impl 02 C12 (impl 04 side done) |

### 13.2 Interpretations adopted (open items with current defaults)

| ID | Item | Default |
|----|------|---------|
| OI04-01 | 04 §11.1 / D13 customer impact source | `core.incident.customer_impact_minutes`; P1/P2 fallback flagged `estimate` |
| OI04-02 | D2 weights | Placeholders, `unconfirmed: true` |
| OI04-03 | D6 accountability | Resolving team for org scores; service owner for funding |
| OI04-04 | 04 §11.4, open-questions (b)14 root_cause on work items | Tier-2 root-cause path inactive when no decisions exist |
| OI04-05 | D16 carryover | Period-based |
| OI04-06 | D15 lead time | Request `opened_at` → `actual_end` proxy |
| OI04-07 | `done_at` for items not currently done | NULL (only items with `status_category = 'done'` count as done) |
| OI04-08 | Custom window end | Exclusive |
| OI04-09 | Peer group members | Entity itself excluded for all types |
| OI04-10 | `services(k)` roles | All `service_map` roles |
| OI04-11 | Effort of parent candidates | Own estimate, else subtree points; descendant estimates not summed |
| OI04-12 | Priority NULL | Treated as 5 for weight lookups |
| OI04-13 | `caused_by_change_id` | Holds the change `record_id` |
| OI04-14 | Multiple monitoring tools per metric/day (D14) | Average value; `request_count` = max across tools |
| OI04-15 | Blocks link direction | `from_key` blocks `to_key` |
| OI04-16 | Empty `enrich.*` tables exist in Phase 2 (before spec 03 runs) | Provided: impl 02 creates them in `000_settings.sql` (T02-12, U02-107), so stage 400 works without enrichment |

### 13.3 Verification items

| ID | Item | Blocks |
|----|------|--------|
| VI04-01 | DuckDB accepts named parameters in `CREATE TABLE AS` / `INSERT … SELECT`; fallback: add the column, then parameterized `UPDATE` | T04-05 |
| VI04-02 | `str()` of DuckDB relation column types on the pinned version (freeze in the golden vectors) | T04-01 |
| VI04-03 | `current_setting('access_mode')` value on read-only connections | T04-20 |
| VI04-04 | ICU `AT TIME ZONE` and `timezone()` available on the agent connection with external access disabled | T04-04 |
| VI04-05 | Binding NULL and empty lists through `CAST($x AS T[])`; unused named parameters rejected | T04-04 |
| VI04-06 | DuckDB `mad()` is the unscaled median absolute deviation | T04-16 |
| VI04-07 | OR-Tools parameter names `num_workers`, `max_deterministic_time` on the pinned version | T04-19 |

## 14. Dependencies

### 14.1 Third-party

| Package | Min version | Licence | Use |
|---------|-------------|---------|-----|
| `duckdb` | 1.3 | MIT | execution |
| `pyarrow` | 17 | Apache-2.0 | batch streaming, IPC to hash workers |
| `pydantic` | 2.9 | MIT | models |
| `pyyaml` | 6 | MIT | `safe_load` |
| `jinja2` | 3.1 | BSD-3-Clause | sandboxed templates |
| `sqlglot` | pinned by spec 05 | MIT | catalog validator |
| `ortools` | 9.10 | Apache-2.0 | CP-SAT |
| `tzdata` | 2024.1 | Apache-2.0 | business timezone |
| `hypothesis`, `freezegun`, `pytest-benchmark` (dev) | spec 00 §9 | MPL-2.0, Apache-2.0, BSD-2-Clause | tests |

`polars` (listed in design 04 §12) is not used by this package.

### 14.2 Internal

| Spec | Units used |
|------|-----------|
| impl 00 | `herness.core.errors` taxonomy; `herness.core.ids.canonical_json`, `sha256_hex`, `normalize_sql`, `query_id` (single implementations, R-14); `herness.core.time.now` (T00-04); `herness.core.logging`; the settings import exception (R-03) |
| impl 02 | `herness.store.warehouse.open_readonly`; the writable build connection passed in by `_stage_score` (opened with `open_for_build`, which this package never imports, C12); build runner stage-400 hook (`_stage_score`, T02-19); `core.*`, `meta.*` DDL and the empty `enrich.*` tables (T02-12, T02-15…T02-17); `herness.store.ops.shared.list_review_items` (T02-07, read by U04-83) |
| impl 03 | `enrich.cluster`, `enrich.cluster_member`, `enrich.decision`, `enrich.incident_change_link` content |
| impl 05 | `herness.store.ops.evidence.record_evidence` (R-13); `herness.core.types.Evidence` (submodule `herness.core.types.harness`, R-01); consumer of `result_hash`, `rows_equivalent`, `compute_metric` |
| impl 07 | consumer of `metric_series`, `peer_group` |
| impl 08 | `herness.core.jobs.JobContext` (R-02); `herness.core.jobs.run_inline` and `herness.core.jobs.worker_alive` (used by the CLI path of F04-14, R-44, R-45); `herness.store.ops.metrics.record_metric_samples` (R-12) |
| impl 09 | consumer of `run_scoring` (through the `build_pipeline` job, R-45), `optimize_portfolio`, `describe()`, `SCORE_UNITS`; owns the CLI command table (R-47); runs impl 10's start-up validation hook in every composition root (T09-20, R-71), which runs U04-83 and so `validate_catalog` (DD04-20); the composition root registers U04-83 |
| impl 10 | `herness.core.config.get_config`, `config_hash`, `ConfigIssue`; start-up validation hook `herness.core.config_validate.register_owner_validator` / `run_owner_validators` and `herness.core.config.validate` (T10-12, R-71); config snapshot files of `herness.core.audit.record_config_change` (T10-05, read by U04-83) |
| impl 11 | `tools/synth_data.py` for planted truths and benchmarks |
