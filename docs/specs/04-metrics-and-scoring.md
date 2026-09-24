# 04 — Metrics and Scoring

Status: Draft v2 · 2026-09-24 · Depends on: 00, 02, 03. Phase 2.

v2 aligns with shared contracts v2 (00 §5.1, §10 D6, §12.1) and data model v2 (02 §4.1, §4.5–4.7).

## 1. Purpose and scope

This spec defines the deterministic engine that turns the canonical model (`core.*`) and enrichment labels (`enrich.*`) into numbers: the metric catalog, metric computation, dollar conversion, the funding score, the org improvement score, action levers and the portfolio optimizer. Package: `herness/metrics/` (`catalog.py`, `compute.py`, `facts.py`, `scoring.py`, `levers.py`, `portfolio.py`, `evidence.py`, plus Jinja SQL templates in `herness/metrics/sql/`). The fact-table SQL lives in the build stage file `herness/model/sql/400_facts.sql` (spec 02 §4.1); this spec owns its content.

In scope: `config/metrics.yaml`, `config/weights.yaml`, all tables in `metrics.*` (02 §4.5) and `score.*` (02 §4.6), rows in `meta.evidence` with `producer IN ('facts','metrics','score')` (02 §4.7), the Python API used by the `get_metric` tool (spec 05), memory/outcomes (spec 07) and `herness score` (spec 09).

Out of scope: tool schema and agent prompts (05), labels and clusters (03), report rendering of rationale templates (09), synthetic data generation (11).

No model is called anywhere in this package. Every stored number is produced by a SQL `SELECT` whose `query_id` is recorded, except optimizer selections, which are a deterministic function of recorded query results (§5.10).

## 2. Responsibilities

- Load and validate the metric catalog; expose it read-only to the harness.
- Render and execute metric SQL for any supported grain, period and filter set, returning values plus `query_id`.
- Define `400_facts.sql`, which materializes per-record fact tables once per build so all metrics share one definition of durations, costs and exclusions.
- Convert operational pain to USD with configured weights; propagate `unconfirmed` weight flags.
- Compute `score.funding` (priority and WSJF), `score.org` (robust z, trend, composite), `score.action_lever`, `score.portfolio`.
- Record every producing query in `meta.evidence` with `result_hash` (00 §5.1) and `result_sample`; own the single implementation `herness.metrics.evidence.result_hash()`.
- Enforce invariants (rates in [0,1], non-negative durations, allocation shares sum ≤ 1) and fail the build step on violation.

## 3. Interfaces

### 3.1 Python API

```python
# herness/metrics/catalog.py
Period = Literal["week", "month", "quarter", "t12w", "t12m"]           # 02 §4.5; t12w/t12m = rolling windows ending at as_of
EntityType = Literal["service", "team", "org", "work_item", "cluster"]
Unit = Literal["count", "usd", "pct", "ratio", "hours", "minutes", "seconds", "days", "score", "rank", "other"]  # 00 §12.1

class MetricDef(BaseModel):            # one catalog entry, §4.1
    name: str; description: str; domain: Literal["ops", "change", "delivery", "monitoring", "cost"]
    grains: list[EntityType]; unit: Unit; better: Literal["higher", "lower"]
    aggregation: Literal["count", "sum", "mean", "median", "ratio", "snapshot"]
    min_sample_size: int; owner: str; estimate: bool; uses_weights: list[str]
    usd_model: str | None; filters: list[str]; enabled: bool; requires_columns: list[str]; sql: str

def load_catalog(path: Path = Path("config/metrics.yaml")) -> MetricCatalog   # raises ConfigError
class MetricCatalog:
    def get(self, name: str) -> MetricDef                                      # ToolInputError if unknown
    def describe(self) -> list[dict]      # name, description, grains, unit, better,
                                          # min_sample_size, filters, estimate, enabled — used by get_metric
    version: str                          # sha256[:12] of the canonical YAML

# herness/metrics/compute.py
class MetricRow(BaseModel):
    entity_id: str; period_start: date; value: float | None
    numerator: float | None; denominator: float | None; sample_size: int
    flags: list[str]                      # "insufficient_sample", "estimate", "unconfirmed_weights"

class MetricResult(BaseModel):
    metric: str; entity_type: EntityType; period: Period; unit: Unit; better: Literal["higher", "lower"]
    rows: list[MetricRow]; query_id: str; sql: str; params: dict; result_hash: str
    result_sample: list[dict]             # ≤ 50 rows, §4.4
    row_count: int; build_id: str; catalog_version: str; flags: list[str]

def compute_metric(name: str, entity_type: EntityType, entity_ids: Sequence[str] | None,
                   period: Period, filters: Mapping[str, Any] | None = None, *,
                   window: tuple[date, date] | None = None,
                   con: duckdb.DuckDBPyConnection | None = None) -> MetricResult

def metric_series(metric: str, entity_type: EntityType, entity_ids: Sequence[str] | None,
                  start: date, end: date, period: Period = "week", *,
                  filters: Mapping[str, Any] | None = None,
                  con: duckdb.DuckDBPyConnection | None = None) -> tuple[list[MetricRow], str]
# Used by spec 07 (outcome baselines/actuals). Exactly one SQL query → one query_id; rows ordered by
# (entity_id, period_start). Equivalent to compute_metric(..., window=(start, end)).

def peer_group(entity_type: Literal["team", "org", "service", "work_item"], entity_id: str, *,
               metric: str | None = None,
               con: duckdb.DuckDBPyConnection | None = None) -> PeerGroupInfo
# PeerGroupInfo: key (e.g. "team:crit_hi", "service:crit_2"), member_ids (sorted), size,
#   fallback (None | "all" | "prior_year", §5.8.1), query_id. Computed by one recorded query from core.*
#   and metric_value, so it works on the promoted read-only warehouse. Same rules as §5.8 / §5.8.1.

# herness/metrics/facts.py
def materialize_facts(con: duckdb.DuckDBPyConnection, build_id: str) -> list[str]   # returns query_ids
# Called by the build runner (spec 02) for stage 400: renders 400_facts.sql and executes each
# statement through run_recorded(producer="facts").

# herness/metrics/scoring.py
def run_scoring(build_id: str, *, steps: Sequence[str] | None = None) -> ScoringReport
# ScoringReport: build_id, steps_done, row_counts per table, duration_ms per step, flags, warnings

# herness/metrics/portfolio.py
class Scenario(BaseModel):
    name: str; budget_usd: Decimal; mandatory: list[str] = []; excluded: list[str] = []
    team_capacity_points: dict[str, float] | None = None; enforce_team_capacity: bool = True

def optimize_portfolio(scenario: Scenario | str, *, persist: bool = True,
                       build_id: str | None = None, run_id: str | None = None,
                       con: duckdb.DuckDBPyConnection | None = None) -> PortfolioResult
# PortfolioResult: scenario, budget_usd, solver_status ("OPTIMAL"|"FEASIBLE"|"INFEASIBLE"|"MODEL_INVALID"),
#   rows (one per candidate: candidate_id, selected, order_rank, expected_impact_usd, flags),
#   selected (ordered candidate_ids), total_effort_usd, total_expected_impact_usd,
#   binding_constraints, query_ids (input queries), flags

# herness/metrics/evidence.py
def result_hash(columns: Sequence[tuple[str, str]], rows: Iterable[Sequence[Any]]) -> str   # 00 §5.1
def result_sample(columns, rows, limit: int = 50) -> list[dict]
def run_recorded(con, sql: str, params: dict,
                 producer: Literal["facts", "metrics", "score"] | None) -> RecordedQuery
# RecordedQuery: query_id, sql, params, result_hash, row_count, result_sample, columns, rows (or relation)
# producer=None: nothing written to meta.evidence (read-only callers persist to ops evidence).
```

- `compute_metric`, `metric_series` and `peer_group` with `con=None` open `CURRENT` read-only (spec 00 §4). They never write; the caller (tool layer 05, memory 07) persists `sql`, `params`, `query_id`, `result_hash`, `row_count`, `result_sample` to ops `evidence`.
- `entity_ids=None` means all entities of the grain. The tool layer caps `entity_ids` at 500 and `window` at 36 months.
- Default `window`: the last N complete periods before `as_of` (N from `defaults.windows`, §7.1). `as_of` = date of `meta.build.started_at` in `business_timezone`, overridable by `scoring.as_of`.
- `optimize_portfolio(persist=True)` runs only inside the build pipeline job on the writable build file and writes `score.portfolio` plus `meta.evidence`. On a read-only connection it raises `ConfigError("warehouse is read-only; use persist=False")`.
- `optimize_portfolio(persist=False)` runs in-process against the promoted read-only warehouse (custom budgets from spec 09 after promotion). It writes nothing to the warehouse, returns the full `PortfolioResult` with `query_ids`, and records each input query in the ops `evidence` table via `herness.store.ops` (with `run_id` when given). Its output is reproducible from those query_ids and the scenario (§5.10).

### 3.2 CLI hook

Spec 09 owns `herness score`. It calls, in order: `load_catalog()` → `run_scoring(build_id)` (all steps) → nothing else. `herness score --step org` passes `steps=["org","levers"]`. `herness score --scenario <name|budget>` after promotion calls `optimize_portfolio(scenario, persist=False)`. `herness metrics list` prints `MetricCatalog.describe()`. The nightly pipeline (spec 02 §4.1) calls `materialize_facts` for stage 400 and then `run_scoring` in-process.

### 3.3 Scoring steps

Facts are built earlier, in stage `400_facts.sql` (`metrics.incident_fact`, `metrics.change_fact`, `metrics.work_item_fact`, `metrics.org_closure`, `metrics.work_item_closure`; producer `facts`). `run_scoring` then runs these steps in order; each step is idempotent (`CREATE OR REPLACE TABLE`) and its completion is checkpointed through the job checkpoint API of spec 08.

| Step | Writes | Reads |
|------|--------|-------|
| `validate` | — | config, catalog, required columns, presence of fact tables |
| `metrics` | `metrics.metric_value` | facts, `core.event`, `core.metric_daily` |
| `funding` | `score.funding_attribution`, `score.funding` | facts, `core.work_item*`, `enrich.cluster*`, `enrich.decision` |
| `org` | `score.org` | `metrics.metric_value` |
| `levers` | `score.action_lever` | `score.org`, facts |
| `portfolio` | `score.portfolio` | `score.funding`, `core.work_item_link` |
| `check` | `meta.dq_result` (checks prefixed `score_`) | all of the above |

## 4. Data contracts

### 4.1 Metric catalog (`config/metrics.yaml`)

```yaml
version: 1
defaults:
  min_sample_size: 10
  windows: {week: 26, month: 24, quarter: 8}
  exclude_incident_states: [canceled]
  exclude_close_codes: ["Duplicate", "Cancelled", "Not an incident"]
  max_resolve_days: 365            # durations above this are excluded (data errors)
  cluster_min_membership: 0.5
  change_link_min_score: 0.7
  repeat_window_days: 30
  noise_severities: [critical, major, minor, warning]   # info excluded from noise ratio
  failure_outcomes: [unsuccessful, backed_out]
metrics:
  - name: mttr_hours
    description: Mean wall-clock hours from opened_at to resolved_at of resolved incidents.
    domain: ops
    grains: [service, team, org, cluster]
    unit: hours                    # 00 §12.1 vocabulary only
    better: lower                  # higher | lower (single direction field)
    aggregation: mean
    min_sample_size: 10
    owner: sre-analytics
    estimate: false
    uses_weights: []
    usd_model: mttr
    filters: [priority, service_id, team_id, org_id, cluster_id]
    enabled: true
    requires_columns: []
    sql: |
      SELECT {{ entity_col('incident', 'f') }} AS entity_id,
             {{ period_start('f.resolved_at') }} AS period_start,
             avg(f.resolve_h) AS value, sum(f.resolve_h) AS numerator,
             count(f.resolve_h) AS denominator, count(f.resolve_h) AS sample_size
      FROM metrics.incident_fact f {{ entity_join('incident', 'f') }}
      WHERE f.resolved_at >= $window_start AND f.resolved_at < $window_end
        AND f.resolve_h IS NOT NULL AND NOT f.excluded
        {{ filter_clause('incident', 'f') }} {{ entity_filter() }}
      GROUP BY ALL
```

Template contract:
- Output columns exactly `entity_id, period_start, value, numerator, denominator, sample_size`. `count`/`sum` metrics set `numerator = value`, `denominator = NULL`. `ratio` metrics set `value = numerator / NULLIF(denominator, 0)`.
- Jinja variables: `entity_type`, `period`, `filters` (dict). Values never render into SQL text; only whitelisted identifiers do. Runtime values bind as DuckDB named parameters: `$window_start`, `$window_end` (TIMESTAMPTZ, business-timezone midnight), `$entity_ids` (VARCHAR[]), `$f_<filter>`, `$tz`, `$as_of`, plus defaults referenced as `$d_<key>`.
- Macros (in `herness/metrics/sql/_macros.sql.j2`): `entity_col(source, alias)`, `entity_join(source, alias)`, `period_start(ts_expr)` → `CAST(date_trunc('{{period}}', {{ts_expr}} AT TIME ZONE $tz) AS DATE)` (for `t12w`/`t12m` it returns `CAST($window_start AT TIME ZONE $tz AS DATE)`), `filter_clause(source, alias)`, `entity_filter()` → `AND ($entity_ids IS NULL OR list_contains($entity_ids, entity_id_expr))`, `cat_at(ts_expr)` (work-item category at a time, ASOF join).
- Allowed filter keys: `priority` (list of int), `service_id`, `team_id`, `org_id`, `cluster_id`, `work_item_type` (list), `severity` (list), `change_type` (list). The catalog validator rejects templates that reference `description`, `short_description`, `close_notes`, `summary`, `root_cause_text` or any `enrich.text_redacted` column.

Grain resolution used by `entity_col` / `entity_join`:

| Source | service | team | org | cluster | work_item |
|--------|---------|------|-----|---------|-----------|
| `incident_fact` | `service_id` | `team_id` (resolver group) | `org_closure.ancestor_org_id` over `org_id` of the team | `cluster_id` | — |
| `change_fact` | `service_id` | `team_id` | closure over team's `org_id` | — | — |
| `core.event` | `service_id` | owner team: `service_map` row with `role='owner'`, highest `confidence`, then lowest `team_id` | closure over `core.service.org_id` | — | — |
| `work_item_fact` | `service_id` | `team_id` | closure over team's `org_id` | — | `work_item_closure.ancestor_record_id` |
| `core.metric_daily` | `service_id` | owner team as for events | closure over `core.service.org_id` | — | — |

Org grain rolls up descendants (`org_closure` includes each org as its own ancestor).

Accountability (D6, spec 00 §10): incident metrics at team and org grain, and therefore `score.org` and action levers, count an incident against the **resolving team** (`core.incident.team_id`) and that team's org. Funding attribution (§5.5) follows the incident's **service and its owner** (`service_id`, `service_map` role `owner`), never the resolving team.

### 4.2 Fact tables (02 §4.5; materialized by `400_facts.sql`)

Each fact table is one `CREATE OR REPLACE TABLE ... AS SELECT` in `herness/model/sql/400_facts.sql`, Jinja-templated only with config values from `metrics.yaml: defaults` and `weights.yaml` (02 §4.1 rule). `materialize_facts` executes each statement through `run_recorded(producer="facts")`; every row carries that statement's `query_id`. Column types:

`metrics.incident_fact`, one row per `core.incident`: `record_id VARCHAR`, `number VARCHAR`, `opened_at TIMESTAMPTZ`, `resolved_at TIMESTAMPTZ`, `priority SMALLINT`, `service_id VARCHAR`, `team_id VARCHAR`, `org_id VARCHAR` (resolving team's org), `criticality SMALLINT`, `cluster_id VARCHAR`, `membership_prob DOUBLE`, `excluded BOOLEAN`, `resolve_h DOUBLE`, `resolve_bh DOUBLE`, `impact_h DOUBLE`, `impact_estimated BOOLEAN`, `toil_h DOUBLE`, `downtime_usd DECIMAL(18,2)`, `toil_usd DECIMAL(18,2)`, `total_usd DECIMAL(18,2)`, `is_repeat BOOLEAN`, `is_reopened BOOLEAN`, `is_reassigned BOOLEAN`, `sla_breached BOOLEAN`, `change_caused BOOLEAN`, `query_id VARCHAR`.

`metrics.change_fact`: `record_id VARCHAR`, `type VARCHAR`, `service_id VARCHAR`, `team_id VARCHAR`, `org_id VARCHAR`, `actual_end TIMESTAMPTZ`, `deployed BOOLEAN`, `failed BOOLEAN`, `linked_incident_count INTEGER`, `lead_time_h DOUBLE` (`actual_end − opened_at`, NULL when `opened_at` is NULL or later than `actual_end`), `query_id VARCHAR`.

`metrics.work_item_fact`: `record_id VARCHAR`, `key VARCHAR`, `type VARCHAR`, `parent_key VARCHAR`, `status_category VARCHAR`, `service_id VARCHAR`, `team_id VARCHAR`, `org_id VARCHAR`, `story_points DOUBLE`, `created_at TIMESTAMPTZ`, `first_in_progress_at TIMESTAMPTZ`, `done_at TIMESTAMPTZ`, `cycle_days DOUBLE`, `is_unplanned BOOLEAN`, `query_id VARCHAR`.

`metrics.org_closure`: `org_id VARCHAR`, `ancestor_org_id VARCHAR`, `depth INTEGER` (0 = self). `metrics.work_item_closure`: `record_id VARCHAR`, `ancestor_record_id VARCHAR`, `candidate_record_id VARCHAR` (nearest candidate ancestor-or-self, §5.4, NULL if none), `depth INTEGER`.

### 4.3 Output tables (02 §4.5–4.6)

- `metrics.metric_value`: one row per (`metric`, `entity_type`, `entity_id`, `period`, `period_start`) for every enabled metric × grain × period, over the default windows plus `t12w` and `t12m` at `as_of` (`period_start` = window start). `value` is NULL when `sample_size < min_sample_size` (flag `insufficient_sample`). `unit` is the catalog unit.
- `score.funding`, `score.funding_attribution`, `score.org`, `score.action_lever`, `score.portfolio`: columns exactly as 02 §4.6. `score.funding_attribution.record_kind` ∈ `incident` \| `event` \| `change`.

Units for NumberRef-facing columns (00 §12.1), declared in `herness/metrics/catalog.py: SCORE_UNITS` so the tool layer and Writer do not guess:

| Column | Unit |
|--------|------|
| `metric_value.value`, `score.org.value`, `peer_median`, `action_lever.current_value`, `target_value` | the metric's catalog unit |
| `annual_pain_usd`, `addressable_pain_usd`, `effort_cost_usd`, `delta_usd`, `expected_impact_usd`, `budget_usd`, `pain_usd` | `usd` |
| `confidence`, `strategic_weight`, `expected_reduction`, `share`, `priority` (USD of impact per USD of effort) | `ratio` |
| `wsjf`, `z_score`, `composite`, `trend_slope` | `score` |
| `rank`, `order_rank` | `rank` |
| `n_incidents`, `sample_size`, `row_count` | `count` |

### 4.4 Evidence and result hashing

Every producing `SELECT` goes through `run_recorded`:
1. `query_id` per spec 00 §5 over the rendered, normalized SQL, `params` and `build_id`. `params` = `{"bind": {...named parameters...}, "template": {"name": ..., "entity_type": ..., "period": ..., "filters": ...}}`, keys sorted. The Verifier re-executes `sql` with `params.bind`.
2. For stored results, the step runs `CREATE OR REPLACE TABLE x AS <select>` and then hashes `SELECT * FROM x`. Both equal the result of re-running the `SELECT` on the same build.
3. `meta.evidence` row (02 §4.7): `query_id`, `sql`, `params`, `result_hash`, `row_count`, `result_sample`, `executed_at`, `producer` (`facts` \| `metrics` \| `score`). An existing `query_id` is kept (same build, same result). Read-only callers (`compute_metric`, `metric_series`, `peer_group`, `optimize_portfolio(persist=False)`) get the same fields back for the ops `evidence` table.
4. `result_sample`: the first 50 rows of the result in the 00 §5.1 sort order (sorted by row hash), serialized as JSON objects with the same value encoding as the hash (floats `.9g`, DECIMAL as string, timestamps ISO `Z`). Every recorded query fills it, including fact and score tables; spec 09 displays it.

`result_hash` is defined in spec 00 §5.1 and implemented once in `herness.metrics.evidence.result_hash()`; the harness imports it. This spec adds no rule of its own.

Verifier tolerance (used by spec 05 when re-running a `query_id`):
- Exact hash match passes. On mismatch the Verifier compares cell by cell after sorting: numeric cells pass with `abs(a-b) <= 1e-9 + 1e-6 * max(|a|,|b|)`, all other cells exact. The 9-significant-digit rounding absorbs parallel float-sum order differences in DuckDB.

## 5. Behavior

### 5.1 Fact derivation (shared definitions)

Incident exclusion: `excluded = state IN exclude_incident_states OR close_code IN exclude_close_codes`. Excluded incidents appear in no metric and no pain.

```sql
-- resolution duration (wall clock); NULL when unresolved or invalid
resolve_h  = CASE WHEN resolved_at IS NOT NULL AND resolved_at >= opened_at
                   AND resolved_at - opened_at <= to_days($d_max_resolve_days)
                  THEN date_diff('second', opened_at, resolved_at) / 3600.0 END
resolve_bh = business_duration_s / 3600.0          -- source-computed business time; NULL if absent
-- repeat: previous incident in same cluster and service within 30 days
is_repeat  = cluster_id IS NOT NULL AND service_id IS NOT NULL AND
             opened_at - lag(opened_at) OVER (PARTITION BY cluster_id, service_id
                                              ORDER BY opened_at, record_id)
             <= to_days($d_repeat_window_days)
change_caused = caused_by_change_id IS NOT NULL OR EXISTS (
             SELECT 1 FROM enrich.incident_change_link l
             WHERE l.incident_id = record_id AND l.score >= $d_change_link_min_score)
```

`cluster_id` = the `enrich.cluster_member` row with the highest `membership_prob` ≥ `cluster_min_membership` (ties: lowest `cluster_id`). Noise points (no qualifying row) have `cluster_id = NULL` and are never repeats.

Change: `deployed = actual_end IS NOT NULL AND coalesce(outcome,'') <> 'canceled'`; `failed = deployed AND (outcome IN failure_outcomes OR linked_incident_count > 0)`, where `linked_incident_count` counts distinct non-excluded incidents with `caused_by_change_id = record_id` or a link with `score ≥ change_link_min_score`.

Work item: `first_in_progress_at = min(at) WHERE to_category='in_progress'`; `done_at = max(at) WHERE to_category='done'`, else `resolved_at` when `status_category='done'`; `cycle_days = date_diff('second', first_in_progress_at, done_at)/86400.0` when both exist and `done_at ≥ first_in_progress_at`; `is_unplanned = type='bug' OR key IN (from_key or to_key of work_item_link WHERE link_type='mentions_incident')`. Work-item category at time T uses `ASOF LEFT JOIN core.work_item_transition t ON t.record_id = w.record_id AND T >= t.at`, falling back to `'todo'` when `created_at <= T` and no transition exists.

### 5.2 Metric definitions

Anchor = timestamp that assigns a record to a period. All ratios are in [0,1]. "Resolved incidents" = non-excluded with `resolve_h IS NOT NULL`.

| # | Metric | Grains | Anchor | Definition | Unit | Better | Min n |
|---|--------|--------|--------|-----------|------|--------|-------|
| 1 | `incident_count` | svc, team, org, cluster | opened_at | count non-excluded incidents | count | lower | 1 |
| 2 | `p1p2_count` | svc, team, org, cluster | opened_at | count with `priority <= 2` | count | lower | 1 |
| 3 | `mttr_hours` | svc, team, org, cluster | resolved_at | mean `resolve_h` | hours | lower | 10 |
| 4 | `mttr_p50_hours` | svc, team, org, cluster | resolved_at | median `resolve_h` | hours | lower | 10 |
| 5 | `mttr_business_hours` | svc, team, org, cluster | resolved_at | mean `resolve_bh` (NULLs dropped; flag if < 80 % coverage) | hours | lower | 10 |
| 6 | `mtta_minutes` | svc, team, org | opened_at | mean minutes `opened_at` → `acknowledged_at` over incidents with `acknowledged_at` not NULL (`requires_columns: [core.incident.acknowledged_at]`; flag `low_coverage` if < 80 % of incidents have it) | minutes | lower | 10 |
| 7 | `customer_impact_minutes` | svc, team, org, cluster | opened_at | sum `impact_h × 60` (source value; fallback per §5.3 flagged `estimate`) | minutes | lower | 1 |
| 8 | `repeat_incident_rate` | svc, team, org, cluster | opened_at | `count(is_repeat) / count(*)` over non-excluded incidents with `service_id` not NULL | ratio | lower | 20 |
| 9 | `reopen_rate` | svc, team, org, cluster | resolved_at | resolved incidents with `reopen_count > 0` / resolved | ratio | lower | 20 |
| 10 | `reassignment_rate` | svc, team, org, cluster | resolved_at | resolved with `reassignment_count > 0` / resolved | ratio | lower | 20 |
| 11 | `sla_breach_rate` | svc, team, org, cluster | resolved_at | `sla_breached = true` / resolved with `sla_breached` not NULL | ratio | lower | 20 |
| 12 | `alert_noise_ratio` | svc, team, org | event ts | events with `severity IN noise_severities AND incident_id IS NULL` / events with `severity IN noise_severities` | ratio | lower | 50 |
| 13 | `toil_hours_est` | svc, team, org, cluster | resolved_at | sum `toil_h` (§5.3), `estimate: true` | hours | lower | 1 |
| 14 | `incident_cost_usd` | svc, team, org, cluster | opened_at | sum `total_usd`, `estimate: true` | usd | lower | 1 |
| 15 | `change_count` (deploy frequency) | svc, team, org | actual_end | count `deployed`; per-week value = count / weeks in period | count | higher | 1 |
| 16 | `change_failure_rate` | svc, team, org | actual_end | count `failed` / count `deployed` (DORA) | ratio | lower | 10 |
| 17 | `change_caused_incident_count` | svc, team, org | incident opened_at | distinct incidents with `change_caused`, grouped by the causing change's entity | count | lower | 1 |
| 18 | `change_lead_time_hours` | svc, team, org | actual_end | median `lead_time_h` over deployed changes with `opened_at` (proxy: request opened → deployed, not commit → deploy; flag `low_coverage` if < 80 %) | hours | lower | 10 |
| 19 | `emergency_change_ratio` | svc, team, org | actual_end | deployed with `type='emergency'` / deployed | ratio | lower | 10 |
| 20 | `throughput` | svc, team, org, work_item | done_at | count items `type IN (story,bug,task)` done | count | higher | 1 |
| 21 | `cycle_time_days` | svc, team, org, work_item | done_at | median `cycle_days` of those items | days | lower | 10 |
| 22 | `carryover_rate` | team, org, work_item | period_start | items with `cat_at(period_start)='in_progress'` and `cat_at(period_end)<>'done'` / items with `cat_at(period_start)='in_progress'` | ratio | lower | 10 |
| 23 | `backlog_age_days` | svc, team, org, work_item | snapshot at min(period_end, as_of) | median days since `created_at` of items with `cat_at(snapshot)='todo'`, types story/bug/task | days | lower | 10 |
| 24 | `wip_count` | team, org, work_item | snapshot | count items with `cat_at(snapshot)='in_progress'` | count | lower | 1 |
| 25 | `unplanned_work_ratio` | svc, team, org | done_at | `is_unplanned` items / items done (story, bug, task) | ratio | lower | 20 |
| 26 | `epic_predictability` | team, org, work_item | epic done_at | §5.2.1 | ratio | higher | 3 epics |
| 27 | `availability_pct` | svc, org | date | mean of daily `availability_pct` (org: `request_count`-weighted when present) | pct | higher | 7 days |
| 28 | `error_rate` | svc, org | date | `request_count`-weighted mean of daily `error_rate`; simple mean if no counts | ratio | lower | 7 days |

5.2.1 Epic predictability. For each epic `e` done in the period: `e_start` = first transition of `e` to `in_progress` (else `e.created_at`). Committed children = descendant items (story/bug/task) with `created_at <= e_start`. `pred(e) = Σ points(done children with done_at <= e.done_at) / Σ points(committed children)`, where `points = coalesce(story_points, 1)`. Epics with no committed children are skipped. Team/org value = committed-points-weighted mean of `pred(e)`; `sample_size` = number of epics.

SQL sketch for #16 (`change_failure_rate`):

```sql
SELECT {{ entity_col('change','c') }} AS entity_id, {{ period_start('c.actual_end') }} AS period_start,
       count(*) FILTER (WHERE c.failed)::DOUBLE / NULLIF(count(*), 0) AS value,
       count(*) FILTER (WHERE c.failed) AS numerator, count(*) AS denominator, count(*) AS sample_size
FROM metrics.change_fact c {{ entity_join('change','c') }}
WHERE c.deployed AND c.actual_end >= $window_start AND c.actual_end < $window_end
  {{ filter_clause('change','c') }} {{ entity_filter() }}
GROUP BY ALL
```

SQL sketch for #12 (`alert_noise_ratio`):

```sql
SELECT {{ entity_col('event','e') }} AS entity_id, {{ period_start('e.ts') }} AS period_start,
       count(*) FILTER (WHERE e.incident_id IS NULL)::DOUBLE / NULLIF(count(*),0) AS value,
       count(*) FILTER (WHERE e.incident_id IS NULL) AS numerator, count(*) AS denominator, count(*) AS sample_size
FROM core.event e {{ entity_join('event','e') }}
WHERE e.ts >= $window_start AND e.ts < $window_end AND list_contains($d_noise_severities, e.severity)
  {{ filter_clause('event','e') }} {{ entity_filter() }}
GROUP BY ALL
```

### 5.3 Dollar conversion

Inputs from `config/weights.yaml` (§7.2). For each non-excluded incident `i` on service with criticality `k` (NULL → `default`) and priority `p`:

```text
impact_h(i)   = customer_impact_minutes / 60                                  if not NULL
              = min(resolve_h × outage_fraction[p], cap_hours)  [impact_estimated] if impact_fallback.enabled and p <= max_priority
              = 0                                                             otherwise
downtime_usd  = impact_h × cost_per_downtime_hour[k] × priority_impact_multiplier[p]
toil_h(i)     = min(coalesce(resolve_bh, resolve_h × business_share) × effort_factor[p], max_hours_per_incident)
toil_usd      = toil_h × cost_per_engineer_hour
total_usd     = downtime_usd + toil_usd
noise_usd(e)  = triage_minutes_per_alert / 60 × cost_per_engineer_hour        for each unlinked noise event
backout_usd(c)= backout_effort_hours × cost_per_engineer_hour                 for each failed change with outcome in failure_outcomes
```

Toil is always flagged `estimate`: `business_duration_s` is elapsed business time, not logged effort; `effort_factor[p]` is the configured average hands-on hours per business hour open (people × engagement). Change-caused incident cost is already inside `total_usd`; only `backout_usd` is added for changes, so no double counting.

A value "uses" a weight when its formula reads it. Each `MetricDef.uses_weights` and each score lists its weights. If any used weight block has `unconfirmed: true`, the row gets flag `unconfirmed_weights` (score tables: `unconfirmed = true`). Reports show the D2 banner while any stored row has it (spec 09).

### 5.4 Funding candidates

- Work-item candidates: `core.work_item` rows with `type IN ('initiative','epic','feature')` and `status_category IN ('todo','in_progress')`. `candidate_id = record_id`.
- Candidate closure: every work item maps to its nearest candidate ancestor-or-self via recursive `parent_key` (`metrics.work_item_closure`). Links on stories roll up to that candidate. Cycles in `parent_key` are cut at depth 10 and reported in `meta.dq_result`.
- Cluster-fix candidates (§5.5 pass 2): `candidate_id = 'cluster_fix:' || cluster_id`, `candidate_type = 'cluster_fix'`.
- Pain is allocated to leaf-most candidates. A parent candidate's pain, effort and points are the sum over itself and its candidate descendants; the optimizer forbids selecting a parent together with a descendant (§5.10).

### 5.5 Attribution and allocation (no double counting)

Time window: `[as_of − 365 days, as_of)` on `opened_at` (events: `ts`, changes: `actual_end`). Paths from incident `i` to candidate `k`, each with a tier, weight `w` and quality `q`:

| Tier | Path | w | q |
|------|------|---|---|
| 1 direct | `work_item_link` with `link_type='mentions_incident'`, one side maps (closure) to `k`, other side equals `i.number` | 1.0 | 1.0 |
| 2 cluster | `i` in cluster `C` (membership ≥ threshold) and `k` has ≥ `min_direct_links` tier-1 incidents in `C` | `cluster_weight × membership_prob` | `membership_prob` |
| 2 root cause | `enrich.decision` on `k` (`question='root_cause'`) equals `C.root_cause_category`, and `list_has_any(C.service_ids, services(k))` | `root_cause_weight × membership_prob × decision.probability` | `membership_prob × probability` |
| 2 cluster_fix | `k = 'cluster_fix:'||C` and `i` in `C` | `cluster_weight × membership_prob` | `membership_prob` |
| 3 service | `i.service_id ∈ services(k)` | `service_weight × service_map.confidence` | `0.5 × confidence` |

`services(k)` = `k.service_id` plus `service_map.service_id` rows whose (`jira_project`, `jira_component`) match `k.project` and any of `k.components` (component NULL in the map matches any). When several paths join `i` and `k`, keep the one with the lowest tier, then the highest `w`.

Allocation rule: for each record, keep only paths in its best (lowest) tier; `share(i,k) = w(i,k) / Σ_k' w(i,k')` over that tier. So `Σ_k share(i,k) = 1` for every attributed record, and `Σ_k pain(k) ≤ total pain`. Events (`noise_usd`) and changes (`backout_usd`) use tier 3 only.

```sql
WITH best AS (SELECT record_id, min(tier) AS t FROM paths GROUP BY record_id)
SELECT p.candidate_id, p.record_id, p.record_kind, p.tier, p.weight, p.quality,
       p.weight / sum(p.weight) OVER (PARTITION BY p.record_id) AS share,
       (p.weight / sum(p.weight) OVER (PARTITION BY p.record_id)) * r.usd AS pain_usd
FROM paths p JOIN best b ON b.record_id = p.record_id AND b.t = p.tier
JOIN record_cost r ON r.record_id = p.record_id
```

Two passes: pass 1 without cluster-fix candidates. Then a cluster `C` becomes a cluster-fix candidate when, over the window, its non-excluded incident count ≥ `cluster_fix.min_incidents_12m`, its total annualized pain ≥ `min_annual_pain_usd`, and the share of its pain allocated at tier 1–2 in pass 1 < `max_linked_share`. Pass 2 recomputes allocation with them. Only pass 2 is stored.

Annualization: `observed_days = min(365, as_of − min(opened_at of non-excluded incidents))`; `annual_pain(k) = Σ pain_usd × 365 / observed_days`. Flag `short_history` when `observed_days < 180`.

```text
addressable_pain_usd(k) = annual_pain(k) × expected_reduction[type(k)]        (override by key if configured)
confidence(k) = clamp(c_map × c_sample × c_hist, 0.05, 1.0)
  c_map    = Σ pain_usd·quality / Σ pain_usd           over k's attribution rows
  c_sample = min(1, sqrt(n_incidents(k) / 30))          n = distinct attributed incidents (share-weighted count rounded)
  c_hist   = min(1, observed_days / 365)
strategic_weight(k) = clip(portfolio_weight[portfolio(k)] × org_weight[org(k)], clip_min, clip_max), default 1.0
  portfolio(k) = key of top initiative ancestor, else project; org(k) = team's org_id, else service org_id
effort_cost_usd(k) = estimate_cost_usd                                   if not NULL
                   = Σ remaining story_points(k subtree, status_category <> 'done') × cost_per_point
                   = cluster_fix.effort_hours × cost_per_engineer_hour   for cluster_fix
                   = NULL [flag no_estimate]                             otherwise
cost_per_point = cost_per_engineer_hour × hours_per_story_point
priority(k) = addressable_pain_usd × confidence × strategic_weight / effort_cost_usd     (NULL if effort NULL or 0)
```

Candidates with `annual_pain = 0` still get a row (priority 0) so reviews can see unfunded pain-free work.

### 5.6 WSJF variant

`wsjf = (BV + TC + RR) / JS`, each component mapped to the Fibonacci scale by `percent_rank()` across all candidates with non-NULL input: `pr ≤ .15→1, ≤.30→2, ≤.45→3, ≤.60→5, ≤.75→8, ≤.90→13, else 20` (JS uses the same map on effort, so larger effort → larger JS).

| Component | Input |
|-----------|-------|
| BV business value | `addressable_pain_usd × strategic_weight` |
| TC time criticality | Theil–Sen slope of `k`'s weekly attributed pain over the last 12 complete weeks (§5.7 SQL), plus 1 Fibonacci step when ≥ 1 attributed P1 in the last 30 days |
| RR risk reduction / opportunity enablement | `count of candidates k blocks (link_type 'blocks', open) + (5 − min criticality of services(k))` |
| JS job size | `effort_cost_usd` (NULL → wsjf NULL) |

### 5.7 Ranking and ties

`score.funding.rank = row_number() OVER (ORDER BY priority DESC NULLS LAST, addressable_pain_usd DESC, confidence DESC, candidate_id ASC)`. WSJF order is not stored as rank; reports sort by `wsjf` with the same tie keys.

### 5.8 Org improvement score

Entities: every active `core.team` and every `core.org`. Scorecard metrics and weights come from `scoring.org.metrics` (§7.1); only metrics with a `better` direction and no size dependence (rates, durations, per-period ratios) are allowed; counts are rejected by the validator.

1. Value `x(e,m)` = `metrics.metric_value` at `period='t12w'`, `as_of`. Values with `sample_size < min_sample_size` are NULL (flag `insufficient_sample`).
2. Peer group: team → `'team:crit_' || bucket`, where bucket = `hi` if min criticality of services with `service_map.role IN ('owner','support')` is 1–2, `lo` if 3–4, `none` otherwise. Org → `'org:level' || depth` (root depth 0, via `org_closure`). If a group has fewer than `min_peer_group` (5) non-NULL values for `m`, the entity uses `'team:all'` / `'org:all'` for that metric.
3. Robust z: `z = (x − median_g) / (1.4826 × MAD_g)`. If `MAD_g = 0`, use `1.2533 × mean|x − median_g|`; if still 0, `z = 0`. Badness `b = z` when lower is better, `−z` otherwise. Clip to [−5, 5].
4. Trend: Theil–Sen slope over the 12 weekly values ending at `as_of` (weeks with insufficient sample dropped; need ≥ 6 points, else NULL). `trend_b = sign × slope × 12 / (1.4826 × MAD_g)` clipped to [−5, 5], `sign = +1` if lower is better.

```sql
WITH w AS (SELECT entity_id, metric, date_diff('week', $w0, period_start) AS t, value
           FROM metrics.metric_value WHERE period='week' AND period_start >= $w0 AND value IS NOT NULL)
SELECT a.entity_id, a.metric, median((b.value - a.value) / (b.t - a.t)) AS trend_slope, count(DISTINCT a.t) AS n
FROM w a JOIN w b ON a.entity_id = b.entity_id AND a.metric = b.metric AND b.t > a.t
GROUP BY ALL
```

5. `composite(e) = Σ_m w_m × (b(e,m) + λ × coalesce(trend_b(e,m), 0)) / Σ_m w_m` over metrics with non-NULL `b`; λ = `scoring.org.trend_weight` (0.5). If covered weight < `min_weight_coverage` (0.5) of total, composite is NULL (flag `low_coverage`).
6. `rank` per `entity_type`: `row_number() OVER (PARTITION BY entity_type ORDER BY composite DESC NULLS LAST, entity_id)`. Higher composite = needs improvement more. `score.org` holds one row per (entity, metric); `composite` and `rank` repeat on each row of the entity.

All of steps 1–6 run as recorded SQL (DuckDB `median`, `mad`, `quantile_cont`).

#### 5.8.1 Peer groups for services and work items (`peer_group` only)

Spec 07 needs a control group for funded candidates when it measures outcomes. `score.org` does not score these entity types; only `peer_group()` returns them.
- `service`: key `'service:crit_' || criticality` (`crit_none` when NULL). Members are all services with the same criticality, excluding the entity itself.
- `work_item`: resolve the owning service as `work_item.service_id`. If that is NULL, use the highest-confidence `service_map` row with `role='owner'` matching the item's project/component (ties: lowest `service_id`). Then return that service's peer group, excluding the owning service itself. `cluster_fix:<cluster_id>` resolves to the cluster's most frequent service. If no service resolves, return `size = 0` with `fallback = "prior_year"`.
- If the resolved group has fewer than 3 members (`scoring.peer_group.min_service_peers`, default 3), return `fallback = "prior_year"` and keep the members found. Spec 07 then compares against the same entity's prior-year window instead of peers.
- For `team` and `org`, `fallback = "all"` when the §5.8 step 2 fallback applies; otherwise `None`.

### 5.9 Action levers

For each team and org with `rank ≤ scoring.levers.top_entities` (50) and each scorecard metric with `b > 0` and a `usd_model`, two targets: `peer_median` = `median_g`, `top_quartile` = `quantile_cont(x, 0.25)` if lower is better else `0.75`. Skip targets that are not an improvement. Base quantities come from the entity's facts over the trailing 12 months (`N` = non-excluded incidents, `D` = deployed changes, `E` = noise events):

| usd_model | Metrics | `delta_usd` (annual) |
|-----------|---------|----------------------|
| `mttr` | mttr_hours, mttr_p50_hours, mttr_business_hours | `Σ downtime_usd + Σ toil_usd` × `(x − target)/x` |
| `repeat` | repeat_incident_rate | `N × (x − target) × avg total_usd per incident` |
| `reopen` | reopen_rate | `N × (x − target) × avg toil_usd × reopen_rework_factor` |
| `reassign` | reassignment_rate | `N × (x − target) × reassignment_hours × cost_per_engineer_hour` |
| `sla` | sla_breach_rate | `N × (x − target) × sla_penalty_usd` (lever skipped if 0) |
| `cfr` | change_failure_rate | `D × (x − target) × (backout_usd + avg total_usd of change-caused incidents per failed change)` |
| `noise` | alert_noise_ratio | `E × (x − target) × triage_minutes_per_alert / 60 × cost_per_engineer_hour` |

All values are annualized as in §5.5. `rationale_template` is a fixed string chosen by `usd_model` from `scoring.levers.templates`; placeholders are limited to `{entity_name}`, `{metric_label}`, `{current_value}`, `{target_value}`, `{target_kind}`, `{unit}`, `{delta_usd}`, `{n_basis}`, `{peer_group}`, `{period}`. Their values are stored in `template_params` (02 §4.6) and come only from the lever query or catalog/config labels. The validator rejects templates with other placeholders. Example: `"Moving {metric_label} for {entity_name} from {current_value} to the {target_kind} of {target_value} {unit} is worth about {delta_usd} per year, based on {n_basis} records in the last 12 months."`

### 5.10 Portfolio optimizer (`portfolio.py`, OR-Tools CP-SAT)

Input: one recorded query over `score.funding` joined with points and dependency links. Candidates with `effort_cost_usd IS NULL` are dropped (flag) unless mandatory, which raises `ConfigError`.

```text
variables  x_k ∈ {0,1}
maximize   Σ_k x_k × floor(expected_impact_k)          expected_impact_k = addressable_pain_usd × confidence × strategic_weight
subject to Σ_k x_k × ceil(effort_cost_usd_k) ≤ floor(budget_usd)
           for each team t: Σ_{k: team(k)=t} x_k × ceil(points_k × 100) ≤ floor(capacity_t × horizon_quarters × 100)   (if enforced)
           x_k = 1 for k ∈ mandatory;  x_k = 0 for k ∈ excluded
           x_B ≤ x_A  for each open link "A blocks B" where A and B are candidates
           x_P + x_C ≤ 1 for each candidate P and candidate descendant C
```

- `points_k` = remaining story points of the subtree; cluster-fix: `effort_hours / hours_per_story_point`; team = candidate `team_id`, cluster-fix: owner team of the cluster's most frequent service. Candidates with no team skip the capacity constraint (flag `no_team`).
- A blocker that is not a candidate and not done adds flag `blocked_by_noncandidate` and does not constrain.
- Solver: `num_workers = 1`, `random_seed` fixed, `max_deterministic_time` (default 20) as the search limit, `max_time_in_seconds` (60) as a wall-clock safety net. Same input → same output. Status `FEASIBLE` (limit hit) is stored with flag `not_proven_optimal`.
- `order_rank` for selected rows: topological order over blocks links among selected candidates; ties by `priority DESC, candidate_id`. Unselected rows: `order_rank = NULL`.
- One `score.portfolio` row per (scenario, candidate). `expected_impact_usd` = `expected_impact_k`. Scenarios come from `portfolio.scenarios` plus an implicit `unconstrained` (budget = Σ effort).
- `INFEASIBLE` (for example mandatory items exceed budget) stores no rows for the scenario and reports the conflicting mandatory set and budget in `ScoringReport.warnings` (or `PortfolioResult.flags` when `persist=False`).
- `persist=True` (build pipeline, configured scenarios): rows go to `score.portfolio` with `query_ids` = the input query IDs, recorded in `meta.evidence` (producer `score`), plus a recorded `SELECT` over the written rows.
- `persist=False` (custom scenarios after promotion, spec 09): same model and solver settings against the read-only warehouse; the result is returned in memory with the input `query_ids`, and those queries are written to ops `evidence`. Because the solver is deterministic, re-running with the same scenario on the same build reproduces the selection; the Verifier checks the input numbers by `query_id`.

## 6. Errors and resilience

| Condition | Error (spec 00 §7) | Effect |
|-----------|-------------------|--------|
| Unknown metric, unsupported grain, disallowed filter key, bad period, >500 entity_ids | `ToolInputError` | returned to agent with the allowed values |
| SQL error or timeout in `compute_metric` (default 30 s) | `QueryError` | returned to agent with hint |
| Invalid catalog/weights YAML, missing required weight, template with forbidden column/placeholder | `ConfigError` | scoring step fails; build not promoted |
| `requires_columns` missing | — | metric skipped, `meta.dq_result` warn `score_metric_disabled` |
| Warehouse locked | `StoreBusy` | retried by spec 08 |
| `optimize_portfolio(persist=True)` on a read-only warehouse | `ConfigError` | caller must use `persist=False` |
| Fact tables missing when `run_scoring` starts (stage 400 skipped) | `SchemaViolation` | build not promoted |
| Invariant violation (§10.2 list) in step `check` | `SchemaViolation` | build not promoted |
| Mandatory candidate without effort | `ConfigError` | portfolio step fails for that scenario only |
| Solver infeasible or time limit | — | warning / flag, no exception |

`run_scoring` is resumable: finished steps are skipped on retry using the job checkpoint; every step rewrites its tables fully, so partial writes never survive.

## 7. Configuration

### 7.1 `config/metrics.yaml` — scoring section

```yaml
scoring:
  as_of: null                     # null = build date in business_timezone
  funding:
    tier_weights: {cluster_weight: 0.8, root_cause_weight: 0.6, service_weight: 0.3}
    min_direct_links: 1
    window_days: 365
  org:
    min_peer_group: 5
    trend_weight: 0.5
    min_weight_coverage: 0.5
    metrics: {mttr_hours: 0.15, repeat_incident_rate: 0.15, change_failure_rate: 0.15,
              sla_breach_rate: 0.10, reopen_rate: 0.05, reassignment_rate: 0.05,
              alert_noise_ratio: 0.10, cycle_time_days: 0.10, unplanned_work_ratio: 0.10,
              epic_predictability: 0.05}
  peer_group:
    min_service_peers: 3          # below this, service/work_item peer_group() returns fallback "prior_year"
  levers:
    top_entities: 50
    templates: {mttr: "...", repeat: "...", reopen: "...", reassign: "...", sla: "...", cfr: "...", noise: "..."}
```

### 7.2 `config/weights.yaml`

```yaml
version: 1
business_timezone: America/New_York
cost_per_downtime_hour: {unconfirmed: true, by_criticality: {1: 50000, 2: 10000, 3: 2000, 4: 500}, default: 500}
cost_per_engineer_hour: {unconfirmed: true, value: 95}
hours_per_story_point:  {unconfirmed: true, value: 6}
priority_impact_multiplier: {unconfirmed: true, values: {1: 1.0, 2: 0.5, 3: 0.1, 4: 0.0, 5: 0.0}}
impact_fallback: {unconfirmed: true, enabled: true, max_priority: 2, outage_fraction: {1: 1.0, 2: 0.5}, cap_hours: 24}
toil: {unconfirmed: true, effort_factor: {1: 1.5, 2: 0.5, 3: 0.2, 4: 0.1, 5: 0.05}, business_share: 0.33,
       max_hours_per_incident: 40, triage_minutes_per_alert: 3, reassignment_hours: 0.5, reopen_rework_factor: 0.5}
change: {unconfirmed: true, backout_effort_hours: 4}
sla_penalty_usd: {unconfirmed: true, value: 0}
strategic_weights: {unconfirmed: true, default: 1.0, clip: [0.5, 2.0], portfolio: {"PLAT-1": 1.5}, org: {"servicenow:org:ops": 1.2}}
expected_reduction: {unconfirmed: true, epic: 0.25, feature: 0.15, initiative: 0.25, cluster_fix: 0.40, overrides: {"PAY-123": 0.6}}
cluster_fix: {unconfirmed: true, effort_hours: 160, min_incidents_12m: 50, min_annual_pain_usd: 50000, max_linked_share: 0.2}
team_capacity_points_per_quarter: {unconfirmed: true, default: 120, teams: {}}
portfolio:
  horizon_quarters: 1
  scenarios: [{name: lean, budget_usd: 1000000}, {name: base, budget_usd: 2000000}, {name: stretch, budget_usd: 4000000}]
  mandatory: []
  excluded: []
  enforce_team_capacity: true
  solver: {num_workers: 1, random_seed: 20260924, max_deterministic_time: 20, max_time_in_seconds: 60}
```

Every block with a dollar or effort meaning carries `unconfirmed`. A user sets it to `false` only through a `review_item` of kind `weight_change` (spec 02 §5); the config loader refuses a change of `unconfirmed` from `true` to `false` without an approved item referencing the new `config_hash`. Weight and catalog files are part of `meta.build.config_hash`.

## 8. Performance targets

Single PC per spec 02 §9; 5M incidents, 0.5M changes, 2M events, 0.2M work items, ~5k candidates.

| Operation | Target |
|-----------|--------|
| Stage `400_facts.sql` (`materialize_facts`, part of the build, counted here) | < 90 s |
| Step `metrics` (all enabled metrics × grains × periods) | < 120 s |
| Step `funding` (both passes) | < 45 s |
| Steps `org` + `levers` | < 20 s |
| Step `portfolio`, per scenario | < 30 s wall |
| Stage 400 + full `run_scoring` | < 5 min |
| `compute_metric` / `metric_series`, one metric, one grain, ≤ 24 periods; `peer_group` (interactive) | p95 < 2 s |
| `optimize_portfolio(persist=False)`, one custom scenario | < 30 s wall |

Techniques: all metrics read the fact tables, not `core.*` text columns; one `GROUP BY ALL` query per metric × grain covers all periods of that grain; org closure pre-joined; `ASOF` joins for work-item snapshots; DuckDB threads per `build.threads`.

## 9. Security

- `compute_metric`, `metric_series`, `peer_group` and `optimize_portfolio(persist=False)` use a read-only DuckDB connection. `materialize_facts` and `run_scoring` run only in the build pipeline job and write only `metrics.*`, `score.*`, `meta.evidence`, `meta.dq_result`.
- `result_sample` never contains text columns (templates cannot select them, §4.1), so samples shown in reports carry no PII.
- Templates render in `jinja2.sandbox.SandboxedEnvironment`. Only whitelisted identifiers render into SQL; every runtime value is a bound parameter. No string built from agent input reaches SQL text.
- No text columns are selected (validator rule, §4.1), so no PII enters metrics, scores or evidence samples.
- `rationale_template` contains no free text from data or models; values come from `template_params`.
- Weight confirmation is gated by the review queue (§7.2) to stop silent tuning of rankings.

## 10. Tests and acceptance criteria

### 10.1 Unit fixtures (hand-computed)

`tests/fixtures/metrics_tiny/` builds a warehouse from ≤ 60 hand-written rows. Every catalog metric has at least one test with a hand-computed expected value, and every grain it declares is exercised once. Example (team T1, service S1 criticality 1, cluster C1, quarter 2026Q1, weights as §7.2 except `cost_per_downtime_hour[1]=10000`, `cost_per_engineer_hour=100`):

| Incident | P | Opened → resolved | Impact min | Business s | Reopen | Reassign | SLA breached | State |
|----------|---|-------------------|-----------|------------|--------|----------|-------------|-------|
| I1 | 1 | 01-05 10:00 → 12:00 | 90 | 7200 | 0 | 1 | true | closed |
| I2 | 3 | 01-20 09:00 → 13:00 | NULL | 14400 | 1 | 0 | false | closed |
| I3 | 2 | 02-25 08:00 → 14:00 | 60 | 21600 | 0 | 2 | false | closed |
| I4 | 3 | 03-01 08:00 → — | NULL | NULL | 0 | 0 | NULL | canceled |

Expected: `incident_count=3`, `p1p2_count=2`, `mttr_hours=4.0`, `repeat_incident_rate=1/3` (I2 is 15 days after I1; I3 is 36 days after I2), `reopen_rate=1/3`, `reassignment_rate=2/3`, `sla_breach_rate=1/3`, `customer_impact_minutes=150`, `downtime_usd = 1.5×10000×1.0 + 1.0×10000×0.5 = 20000`, `toil_hours_est = 2×1.5 + 4×0.2 + 6×0.5 = 6.8`, `incident_cost_usd = 20680`. Similar tables exist for changes (CFR, emergency ratio, change-caused count), events (noise ratio), work items (cycle time, carryover, backlog age, WIP, unplanned, predictability) and metric_daily.

Scoring unit tests: allocation (one incident linked directly to two epics → 0.5/0.5; direct beats cluster beats service), cluster-fix creation threshold, annualization with 180 days history, effort fallback chain, priority and WSJF on a 5-candidate table, robust z with MAD = 0 fallback, Theil–Sen on a known series (slope 2.0 with one outlier), lever deltas per `usd_model`, `result_hash()` equals the golden vectors of spec 00 §5.1 and is stable under row reordering and `-0.0`.

### 10.2 Property tests (hypothesis) and invariants (also run in step `check`)

- Durations (`resolve_h`, `cycle_days`, MTTR, lead time) ≥ 0; ratios and `confidence` in [0,1]; counts ≥ 0 integers.
- For every record: `Σ share ≤ 1 + 1e-9`; `Σ_k annual_pain(k) ≤ total annual pain + 0.01`.
- Metric at `quarter` for a count metric equals the sum of its `month` values; ratios recomputed from summed numerators/denominators match.
- Permuting input row order leaves every `result_hash` unchanged.
- `query_id` of every stored row exists in `meta.evidence`, and re-running it matches under the §4.4 Verifier tolerance.

### 10.3 Synthetic planted truths (spec 11)

On `synth_data.py --scale 5m`: the planted high-ROI epic has `score.funding.rank = 1`; the planted bad team has `rank = 1` among teams in `score.org`; its top lever is the planted metric; the planted recurring cluster without an epic appears as a `cluster_fix` candidate in the top 10. Full `run_scoring` meets §8.

### 10.4 Optimizer

Brute-force comparison on random instances with ≤ 15 candidates (objective equal to exhaustive optimum). Generated tests assert: budget respected, capacity respected, mandatory selected, excluded unselected, no parent+descendant pair, every selected B has its candidate blocker selected, identical output across 5 runs, infeasible mandatory set reported without rows. `persist=False` on a read-only warehouse returns the same selection as `persist=True` on the build file, writes nothing to the warehouse, and writes its input queries to ops `evidence`; `persist=True` on a read-only connection raises `ConfigError`.

### 10.5 API contracts for specs 07 and 09

- `metric_series` returns exactly one `query_id`, and its rows equal `compute_metric` over the same window.
- `peer_group(entity_type, entity_id, metric=m)` returns the same key as `score.org.peer_group` for that entity and metric on the same build. For a `work_item` it returns its owning service's `service:crit_<n>` group without the owning service. With fewer than 3 peers it returns `fallback = "prior_year"`, and an unresolvable service gives `size = 0` with `fallback = "prior_year"`.
- Every `meta.evidence` row has `result_sample` with ≤ 50 rows, equal to the first rows of the result in 00 §5.1 sort order.
- Every catalog entry has `better` ∈ {higher, lower} and a `unit` from the 00 §12.1 vocabulary; the validator rejects anything else.

## 11. Open questions

1. Which source fields define customer impact (ServiceNow `u_customer_impact_minutes`, outage records `cmdb_ci_outage`, or monitoring availability)? Default: `core.incident.customer_impact_minutes` (02 `custom_fields.servicenow.customer_impact_minutes`), with the P1/P2 fallback of §5.3 flagged `estimate`.
2. D2: all dollar weights, effort factors, expected reductions and team capacities. Defaults are placeholders flagged `unconfirmed`.
3. D6 is settled by default (resolving team for org scores, service owner for funding attribution, §4.1). If the answer changes, only the `entity_col` grain map and `incident_fact.org_id` change.
4. Does spec 03 classify work items with the `root_cause` question? If not, the tier-2 root-cause path is inactive.
5. Is sprint data needed for sprint-based carryover? Default uses period-based carryover.
6. Change lead time uses request `opened_at` → `actual_end` as a proxy. Is commit or merge time available anywhere to give true DORA lead time?
7. Resolved: stage 400 runs through the `materialize_facts` hook (02 v2 §4.1).
8. Resolved: ops `evidence.run_id` is nullable for ad-hoc calls (02 v2 §5.3), so `optimize_portfolio(persist=False, run_id=None)` is valid.

## 12. Dependencies

- Specs: 00 (IDs, `result_hash` §5.1, units §12.1, errors, money, time, D2, D6), 02 (all input and output tables, stage `400_facts.sql`, `meta.evidence`, ops `evidence`), 03 (`enrich.cluster*`, `enrich.decision`, `enrich.incident_change_link`), 05 (`get_metric` tool, Verifier re-run, imports `result_hash`), 07 (`metric_series`, `peer_group`, `better`), 08 (job checkpoint, `StoreBusy` retry), 09 (`herness score`, custom scenarios via `persist=False`, banner, template rendering, `result_sample` display), 10 (config loading, `config_hash`), 11 (planted truths).
- Libraries: `duckdb>=1.3`, `polars>=1.10`, `pydantic>=2.9`, `pyyaml`, `jinja2>=3.1` (sandbox), `ortools>=9.10`, `hypothesis`.

## 13. Contract changes (resolved)

1. `core.incident.acknowledged_at` → 02 §4.3 (`core.incident`); MTTA enabled (§5.2 #6).
2. `core.change.opened_at` → 02 §4.3 (`core.change`); lead time enabled as a proxy (§5.2 #18).
3. `metrics.metric_value` numerator/denominator/sample_size/flags and `t12w`/`t12m` periods → 02 §4.5.
4. Fact and closure tables → 02 §4.5; built by stage `400_facts.sql` → 02 §4.1; types in §4.2 here.
5. `score.funding_attribution` → 02 §4.6.
6. `score.funding` extra columns → 02 §4.6.
7. `score.org` extra columns → 02 §4.6.
8. `score.action_lever` extra columns → 02 §4.6.
9. `score.portfolio` extra columns → 02 §4.6.

Also adopted from v2: `result_hash` → 00 §5.1 (this spec's former rule removed); `meta.evidence.result_sample` and producer `facts` → 02 §4.7; units → 00 §12.1; D6 default → 00 §10. The two follow-up conflicts (stage-400 hook, nullable ops `evidence.run_id`) are resolved in 02 v2 (§11 items 7–8).
