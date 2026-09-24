# 11 — Testing, Evaluation and Synthetic Data

Status: Draft v2 · 2026-09-24 · Depends on: 00–10. Phases 1–7.

v2 aligns with shared contracts v2 (spec 00) and specs 01–10. It uses their real interfaces: the fault points in spec 08, the decider wire shape in spec 03, metric names in spec 04, `ReportDraft` and `NumberRef` in specs 00 and 06, trace events in spec 05, and `MemoryStore` in spec 07.

## 1. Purpose and scope

This spec defines how Herness is proven correct. It covers four things:

1. The test strategy: the test pyramid, directory layout, pytest markers, coverage targets, and what runs when.
2. The synthetic data generator `tools/synth_data.py`. It writes the raw lake exactly as connectors would (spec 02 §3.1), at scales from 2k to about 8M records, plants known problems, and writes the planted truth somewhere pipelines cannot read.
3. The evaluation harness `herness/eval/` (`herness eval`). It covers the golden-question suite, classifier evaluation reports, profile and depth comparisons, and regression thresholds.
4. Phase acceptance gates: the concrete checks that close Phases 1–7.

In scope: test infrastructure (fake LLM client and stub servers, stub decider, connector fixtures, fake clock), the order and cadence of the fault, memory, SQL-guard, redaction and performance suites that other specs define, and CI.
Out of scope: the behavior under test. Metric formulas are in spec 04, the classifier gate criteria in spec 03, retry policy and fault-hook semantics in spec 08, and redaction rules in spec 10.

## 2. Responsibilities

- Give every package a test layer and a coverage target, and enforce both locally.
- Produce deterministic, realistic synthetic data in the raw lake format, with planted ground truth that the pipelines must find.
- Keep planted truth outside the pipeline data root, so no pipeline or model can read the answers.
- Evaluate end-to-end answers against reference SQL computed on the build the run used. Numbers are never hard-coded.
- Report classifier quality (Laya vs OpenJev vs LLM vs human), and cross-check the gate file that spec 03 writes.
- Provide the fakes and stubs that specs 03, 05, 06 and 08 use in their tests.
- Measure the performance targets of specs 01–10 and store the results.
- Define the phase acceptance gates and the CI entry points.

## 3. Interfaces

### 3.1 Synthetic data generator

```text
uv run python tools/synth_data.py
    --seed INT                      # required; same seed + scale + params → identical content
    --scale tiny|small|full|5m      # "5m" is an alias for full
    [--root PATH]                   # default data/synth/<seed>-<scale>/
    [--start 2023-09-01] [--end 2026-08-31]   # opened_at range (UTC dates); 3 years
    [--sources servicenow,jira,monitoring,files]
    [--dirty none|default|heavy]    # §5.1.6
    [--fetch-mode initial|daily]    # §5.1.7
    [--params FILE.yaml]            # overrides any §5.1.3 parameter
    [--workers INT]                 # default os.cpu_count(); never changes content
    [--overwrite]                   # otherwise refuses a non-empty root
    [--verify]                      # re-read, check the lake contract and the truth counts
uv run python tools/synth_data.py pii-corpus --seed INT --n 5000 --out tests/fixtures/pii_corpus.jsonl
uv run python tools/synth_data.py api-pages --seed INT --source servicenow --entity incident --rows 1000000 --out DIR
```

Output layout under `--root`:

| Path | Content | Readable by pipelines |
|------|---------|----------------------|
| `<root>/data/raw/<source>/<entity>/dt=…/part-<ulid>.parquet` | raw lake (spec 02 §3.1) | yes (the data root) |
| `<root>/data/inbox/service_costs/service_costs.csv` | CSV drop for the files connector (spec 01 §5.10) | yes |
| `<root>/truth/truth.json`, `truth_labels.parquet`, `t2_members.parquet`, `t3_pairs.parquet` | planted truth (§4.4) | no |
| `<root>/name_directory.csv` | the made-up person names used in text, for the redaction name directory (spec 10) | via the `synth` profile only |

Pipelines run on a synthetic dataset with `herness --profile synth --set paths.data=<root>/data <command>` (spec 10 precedence). The warehouse, ops store, vectors and traces then also live under `<root>/data/`, and no real data is touched.

- `pii-corpus` writes the spec 10 redaction corpus: synthetic ticket sentences with labeled spans.
- `api-pages` writes source-shaped JSON pages (ServiceNow Table API, Jira search) for the spec 01 replay throughput test and for respx fixtures.

Exit codes: 0 success, 2 bad arguments, 3 root not empty, 4 contract self-check failed.

For tests, the generator is also importable as `tools.synth_data.generate(seed, scale, root, **overrides) -> TruthManifest`.

### 3.2 Evaluation CLI (registered by spec 09 as `eval`)

```text
herness eval [--suite golden|classifier|PATH.yaml] [--profile NAME] [--compare PROFILE]   # spec 09 flags
             [--depth fast|standard|deep] [--ids G01,F01] [--tags TAG] [--repeat N]
             [--mock-llm SCRIPT_DIR] [--no-judge] [--resume RUN_ID]
             [--baseline NAME] [--set-baseline NAME] [--compare-runs RUN_ID,RUN_ID,...]
```

- The CLI enqueues one job (`job.kind = 'eval'`, spec 08). Its `gpu_class` is `reasoning`, or `none` with `--mock-llm` or `--suite classifier` on cached decisions. The CLI waits for the job to finish.
- The job creates one ops `run` row (`kind = 'eval'`, `depth`, `profile`, `build_id`, `config_hash`). `run.meta` holds `{"suite", "suite_version", "baseline", "compare_profile", "mock": bool, "dataset_kind"}`.
- `--compare PROFILE` runs the same suite a second time under `PROFILE` on the same build and writes a comparison.
- `--compare-runs` only renders a comparison of finished eval runs.
- Exit code 0 when all regression thresholds pass (§5.3.6), 1 when any fails, 2 on configuration errors.

### 3.3 Python modules

| Module | Public API |
|--------|-----------|
| `herness/eval/golden.py` | `load_suite(path) -> Suite`, `EvalQuestion`, `Expected` (pydantic), `resolve(question, truth, con) -> ResolvedQuestion` |
| `herness/eval/runner.py` | `run_golden(suite, profile, depth, *, repeat=1, judge=True, resume_run_id=None) -> EvalRun`, `run_classifier(qset_version, deciders) -> ClassifierReport`; job handler `handle_eval(ctx: JobContext, payload) -> JobOutcome` |
| `herness/eval/grading.py` | `grade_numeric`, `grade_entities`, `grade_rules`, `grade_rubric`, `count_unsupported(text, numbers, build_id) -> UnsupportedReport` |
| `herness/eval/report.py` | `write_report(eval_run, out_dir)`, `compare(run_ids) -> ComparisonTable` |
| `tests/support/fake_llm.py` | `FakeLLMClient(scripts)`: in-process `LLMClient` (spec 00 §6), registered as `registry.register("llm", "fake")`; `respx_router(scripts)`; `StubLLMServer(scripts, port=0)` (HTTP, OpenAI-compatible) |
| `tests/support/stub_decider.py` | `StubDeciderServer(mode="oracle"\|"hash", truth_labels=None, noise=0.0)` |
| `tests/support/fake_clock.py` | `FakeClock(start)`: patches `herness.core.time.now()` and `herness.core.time.sleep()`, `.advance(seconds)` |
| `tests/support/seed_ops.py` | `seed_prior_run(memory: MemoryStore, truth)`: plant T6 (§5.1.5) |
| `tests/support/builds.py` | session fixtures `tiny_root`, `tiny_build`, `small_build` (built once per seed under `data/synth/<seed>-<scale>/data`) |

## 4. Data contracts

### 4.1 Test layout and markers

Layout per spec 00 §3:

```text
tests/
  unit/          # pure functions, SQL macros on in-memory DuckDB; no network, no GPU; < 60 s total
  integration/   # multi-component on tiny/small builds with fake LLM and stub decider
  fault/         # spec 08 §10 F1–F12 plus the rows in §5.5
  eval/          # golden.yaml, baselines/, mock_scripts/
  bench/         # performance benchmarks (§8)
  support/       # fakes and shared fixtures (not collected)
  fixtures/
    lake_small/             # data root of `synth_data.py --seed 7 --scale tiny` (raw + inbox), committed
    truth/7-tiny/, 42-tiny/ # truth dirs for the committed seeds
    metrics_tiny/           # spec 04 hand-computed rows
    connectors/<source>/    # spec 01 respx cassettes (scrubbed)
    llm_scripts/            # spec 05/06 scripted model turns (§5.2)
    sql_ok/                 # spec 05 corpus of 200 valid analyst queries
    pii_corpus.jsonl        # spec 10 redaction corpus (≥ 5k, from `synth_data.py pii-corpus`)
    reports/                # spec 09 Writer drafts and golden renders
```

| Marker | Meaning | Default selection |
|--------|---------|-------------------|
| `unit` | No I/O beyond tmp files, no subprocess, no network | every commit |
| `integration` | Real DuckDB, SQLite and LanceDB on tiny or small builds, fake model clients and stub servers | pre-push, CI |
| `fault` | `HERNESS_ENV=test`, fault plans, subprocess kills | nightly; CI for the stub-server subset |
| `eval` | Golden or classifier evaluation | nightly (mock LLM) and on the dev box (real models) |
| `gpu` | Needs vLLM, OpenJev or Laya on CUDA | dev box only |
| `slow` | Over 30 s per test, or small/full scale | nightly and phase gates |

`pyproject.toml` sets `--strict-markers`. Every test file carries exactly one of `unit`, `integration`, `fault` or `eval`, and a file without one fails collection. Hypothesis profiles: `commit` (200 examples) and `nightly` (10,000 examples, the spec 05 and 07 minimum).

### 4.2 What runs when

| Trigger | Command | Budget |
|---------|---------|--------|
| Pre-commit | `ruff check`, `ruff format --check`, `mypy --strict herness/core herness/store herness/metrics herness/eval`, fixtures PII scan (§9), `pytest -m unit -q` | < 90 s |
| Pre-push and CI (CPU) | `pytest -m "(unit or integration or fault) and not gpu and not slow" --cov`, then `herness eval --mock-llm tests/eval/mock_scripts` | < 10 min |
| Nightly on dev box (job `eval`, chained after the build pipeline and reviews, spec 08) | `pytest -m "slow or fault or gpu" --hypothesis-profile=nightly`, `herness eval --suite golden --depth standard`, `herness eval --suite classifier` | < 2 h, inside the GPU windows of spec 08 |
| Phase gate | Everything above, plus the full-scale bench (§8) and the gate checks (§10.4) | one night |

### 4.3 Coverage targets (`pytest-cov`, line and branch)

| Package | Line | Branch |
|---------|------|--------|
| `herness/metrics` | ≥ 95 % | ≥ 90 % |
| `herness/core` | ≥ 90 % | ≥ 85 % |
| `herness/store`, `herness/model` (Python) | ≥ 90 % | ≥ 80 % |
| `herness/harness` | ≥ 85 % | ≥ 75 % |
| `herness/connectors` | ≥ 85 % | ≥ 75 % |
| `herness/enrich` (GPU-only paths marked `# pragma: gpu` are excluded) | ≥ 80 % | ≥ 70 % |
| `herness/eval`, `herness/reports`, `herness/cli.py` | ≥ 80 % | — |
| `app/` | smoke only: every page renders on `tiny_build` | — |

`tests/unit/test_coverage_targets.py` reads `coverage.json` and fails when a package falls below its target. SQL coverage: `tests/unit/test_sql_coverage.py` fails if any table created by `herness/model/sql/*.sql` has no test that asserts on it.

### 4.4 Truth files (`<root>/truth/`)

The truth directory sits outside the pipeline data root. `tests/unit/test_truth_isolation.py` fails when any module under `herness/` or `app/` references `truth.json`, `truth_labels` or `/truth/`. The fixtures that load truth live only in `tests/support/`.

```json
{
  "seed": 42, "scale": "small", "generator_version": "2.0.0", "params_hash": "sha256:…",
  "start": "2023-09-01", "end": "2026-08-31", "question_set_version": "qs-2026-10-01.1",
  "row_counts": {"servicenow/incident": 100412, "jira/issue": 4120},
  "dirty": {"bad_timestamp": 201, "future_ts": 20, "resolved_before_opened": 50, "missing_service": 8033,
            "duplicate_rows": 1004, "later_versions": 5020, "tombstones": 301, "unknown_enum": 100},
  "plants": {
    "T1_bad_team":    {"team_id": "servicenow:sys_user_group:9f…", "team_name": "Platform Ops L2",
                       "service_ids": ["servicenow:cmdb_ci:…"], "metric": "mttr_hours", "lever_usd_model": "mttr",
                       "multiplier_start": 2.0, "multiplier_end": 3.0},
    "T2_roi_epic":    {"epic_key": "CHK-1042", "epic_record_id": "jira:issue:10442", "service_id": "…",
                       "cluster_members_file": "t2_members.parquet", "decoy_epic_key": "DATA-310"},
    "T2c_cluster_fix":{"service_id": "…", "cluster_members_file": "t2_members.parquet", "template_id": "tpl_cert_expiry"},
    "T3_change_cluster": {"ci_id": "…", "owning_team_id": "…", "pairs_file": "t3_pairs.parquet", "source_field_share": 0.3},
    "T4_noisy_service":  {"service_id": "…", "generated_noise_ratio": 0.953},
    "T5_confounder":  {"team_id": "…", "service_id": "…", "peak_window": ["07-15", "08-31"], "volume_multiplier": 2.8},
    "T6_outcomes":    {"paid":   {"epic_key": "…", "service_id": "…", "metric": "incident_count", "effect": -0.40},
                       "unpaid": {"epic_key": "…", "service_id": "…", "metric": "incident_count", "effect": 0.0},
                       "effective_at": "2026-05-01T00:00:00.000000Z"}
  }
}
```

`truth_labels.parquet` has these columns: `record_id`, `content_hash` (computed with the spec 03 §4.2 text rule and the redactor of the `synth` profile), `question`, `answer`, and `pii_spans` (JSON list of `{field, start, end, type}`, with `type` from the spec 10 span types). There is one row per incident per question in the active `QuestionSet` (`root_cause`, `change_caused`, `repeat_issue`, `business_impact`, and `owning_team` from the resolving team).

### 4.5 Golden suite (`tests/eval/golden.yaml`)

```yaml
version: 3
defaults: {tolerance: {rel: 0.01}, datasets: [synthetic, real]}
questions:
  - id: G03
    pipeline: chat                     # chat | funding | org
    question: "Which assignment group has the highest median MTTR among groups with at least 200 incidents in the last 12 months?"
    tags: [mttr, ranking]
    expected:
      entities:
        reference_sql: |
          SELECT team_id FROM metrics.incident_fact
          WHERE NOT excluded AND resolve_h IS NOT NULL
            AND opened_at >= (SELECT max(opened_at) FROM metrics.incident_fact) - INTERVAL 12 MONTH
          GROUP BY team_id HAVING count(*) >= 200
          ORDER BY median(resolve_h) DESC, team_id LIMIT 3
        check: rank1                   # rank1 | topk_contains:K:M | set_equals | kendall_tau>=X
        truth_ref: plants.T1_bad_team.team_id     # synthetic only; must equal reference row 1
      must_mention: ["{entity_name}"]
  - id: F05
    pipeline: funding
    datasets: [synthetic]
    setup: seed_prior_run
    question: "Did the epics we funded last quarter pay off?"
    expected:
      rules:
        must_mention:
          - {entity: "{T6.paid.epic_key}",   with_any: ["paid off", "improved", "reduced"]}
          - {entity: "{T6.unpaid.epic_key}", with_any: ["no effect", "did not improve", "no measurable"]}
        must_not_claim:
          - {entity: "{T6.unpaid.epic_key}", pattern: "(paid off|improved|reduced incidents)"}
      rubric: {criteria: [cites_outcome_measurement, distinguishes_the_two_epics], min_score: 3}
```

Rules:
- `reference_sql` runs read-only at eval time on the run's `build_id`, and its `query_id` is recorded. The suite file never stores a result.
- Placeholders resolve from the truth file (synthetic builds) or from reference row 1. `{entity_name}` looks up the display name in `core.team`, `core.service`, `core.org` or `core.work_item`.
- `datasets: [synthetic]` questions are skipped when `meta.build.dataset_kind = 'real'`, and the skip is reported.
- `numeric` has a `reference_sql` returning one value, plus `unit` (spec 00 §12.1 units) and `tolerance` (`abs` or `rel`).
- `must_not_claim` entries are regexes, evaluated per sentence that names the entity.

### 4.6 Eval outputs (`data/reports/eval/<run_id>/`, spec 00 §4)

| File | Content |
|------|---------|
| `summary.json` | run metadata (`profile`, `depth`, `build_id`, `dataset_kind`, `git_sha`, `config_hash`, judge model), aggregate metrics (§5.3.4), threshold results |
| `results.jsonl` | one line per question × repeat: `id`, `passed`, per-check results, reference values and `query_id`s, final text, `NumberRef`s, unsupported numbers, tokens, cost, latency, retries, fallbacks, repairs, the pipeline `run_id`, trace path |
| `report.md`, `report.html` | failures first, diff against the baseline, per-profile/depth comparison when present |
| `classifier.json` | the §5.4 report |

Baselines: `tests/eval/baselines/<name>.json` holds committed aggregates only (synthetic seed 42, small scale). The judge cache is in `data/cache/judge/` and benchmark results are in `data/bench/` (spec 00 §4).

### 4.7 Classifier gold set and gate file (owned by spec 03)

- **Gold labels.** Stored at `data/labels/<question_set_version>/gold/` (spec 03 §4.5): Parquet parts with `content_hash`, `record_id`, `question`, `question_fingerprint`, `answer`, `labeled_by`, `labeled_at`, `item_id`, `fold`, `adjudicated`. There are 1,500 records by default, built by spec 03 §5.8 step 4 through `label_check` items, need two agreeing reviewers, and are frozen after that. This spec only reads them.
- **Gate result.** `data/models/laya/<version>/eval.json`. Its fields are defined by spec 03 §4.4, it is written by `distill.py` step 6, and the thresholds are in `config/decisions.yaml: acceptance`.
- **Synthetic labels.** `truth_labels.parquet` plays the role of gold on synthetic builds. It never writes to `data/labels/`, and it never produces an `eval.json`.

## 5. Behavior

### 5.1 Synthetic data generator

#### 5.1.1 Scale presets

| Preset | Orgs / teams / services | Incidents | Changes | Problems | Events | Jira issues | metric_daily rows | Span |
|--------|------------------------|-----------|---------|----------|--------|-------------|-------------------|------|
| `tiny` | 3 / 12 / 20 | 1,200 | 250 | 40 | 400 | 150 | 20 × 4 × 90 | 90 days |
| `small` | 12 / 150 / 400 | 100,000 | 10,000 | 1,500 | 40,000 | 4,000 | 400 × 4 × 1,095 | 3 years |
| `full` | 12 / 150 / 400 | ≈ 5,000,000 | 500,000 | 60,000 | 2,000,000 | 200,000 | 400 × 4 × 1,095 | 3 years |

`tiny` keeps every plant but shrinks them: T5's peak covers the last 30 days, and T6 is replaced by a 4-week version that is expected to be `inconclusive`. Planted IDs and names come from a separate seed stream, so they are the same at `small` and `full` for the same seed. Planted names are neutral (no "bad", "noisy" or "slow"), so a model cannot find a plant by its name.

#### 5.1.2 Entities written (raw entity names from spec 02 §3.1; source fields from spec 01 §5)

The generator builds source-shaped JSON records, which is what the API would return with `sysparm_display_value=all` or Jira `expand=changelog`. It flattens them with the spec 01 rules into `<field>` and `<field>_display` columns, and writes them through `herness.store.lake.LakeWriter` (spec 02 §3.2). That writer validates the metadata columns and does the tmp-then-rename commit.

| Source / entity | Fields generated |
|-----------------|------------------|
| `servicenow/sys_user_group` | `sys_id`, `name`, `parent`, `manager`, `cost_center`, `active`, `type`, `sys_updated_on` |
| `servicenow/cmdb_ci` | `sys_id`, `name`, `sys_class_name` (`cmdb_ci_service`, `cmdb_ci_appl`, `cmdb_ci_server`, `cmdb_ci_db_instance`), `busines_criticality` (on `cmdb_ci_service` only), `owned_by`, `support_group`, `cost_center`, `company` |
| `servicenow/cmdb_rel_ci` | `sys_id`, `parent`, `child`, `type` (`Depends on::Used by`, `Runs on::Runs`) |
| `servicenow/incident` | `sys_id`, `number`, `opened_at`, `u_acknowledged_at`, `resolved_at`, `closed_at`, `priority` (`1 - Critical`…`5 - Planning`), `state`, `business_service`, `cmdb_ci`, `assignment_group`, `reassignment_count`, `reopen_count`, `short_description`, `description`, `close_notes`, `close_code`, `problem_id`, `caused_by`, `made_sla`, `business_duration`, `u_customer_impact_minutes`, `sys_updated_on` |
| `servicenow/change_request` | `sys_id`, `number`, `type`, `state`, `risk`, `opened_at`, `start_date`, `end_date`, `work_start`, `work_end`, `business_service`, `cmdb_ci`, `assignment_group`, `close_code`, `short_description`, `description`, `sys_updated_on` |
| `servicenow/problem` | `sys_id`, `number`, `opened_at`, `resolved_at`, `state`, `business_service`, `assignment_group`, `known_error`, `cause_notes`, `sys_updated_on` |
| `jira/issue` | `id`, `key`, issue type, `parent` key (Cloud style), project, components, labels, status, status category, `created`, `resolutiondate`, custom fields for story points, cost estimate and team, `summary`, `description` (wiki text), `updated`, plus JSON columns `changelog`, `issuelinks`, `remotelinks` |
| `monitoring/event` | `source_tool` (`prometheus`, `datadog`, `splunk`), `event_key`, `ts`, `service`, `host`, `severity_raw`, `title`, `status`, `dedup_key`, `end_ts`, `incident_ref`; `_source_key` = `<source_tool>:<event_key>` |
| `monitoring/metric_daily` | `source_tool`, `date`, `service`, `metric_name` (`availability_pct`, `error_rate`, `p95_latency_ms`, `request_count`), `value`, `unit`; `_source_key` = `<source_tool>\|<metric_name>\|<service>\|<date>` |
| inbox `service_costs` | CSV with `service_name`, `cost_center`, `annual_run_cost_usd`, `downtime_cost_per_hour_usd`; ingested by the files connector (`key_field: service_name`, `mode: snapshot`) into `files/service_costs` |

`config/profiles/synth.yaml` (spec 00 §11) holds the matching `mappings.yaml` custom-field IDs, enum maps, the `files` source entry, and `acknowledged_at` ← `u_acknowledged_at` (spec 01 Q8). `tests/unit/test_synth_profile.py` checks that the generator's field names and the profile agree.

#### 5.1.3 Distributions (defaults; `--params` overrides)

| Aspect | Model |
|--------|-------|
| Org hierarchy | 12 orgs; teams per org ~ Poisson(12.5) clipped to [6, 20], 150 in total; 400 services with heavy-tailed counts per team; criticality 1: 10 %, 2: 25 %, 3: 40 %, 4: 25 % |
| Incident volume per service | Pareto (α = 1.2) weights; the top 5 % of services carry ≈ 40 % of incidents; criticality-1 weight × 1.5 |
| Priority | P1 1 %, P2 6 %, P3 38 %, P4 50 %, P5 5 %; criticality-1 services double the P1/P2 share |
| Arrival time | non-homogeneous Poisson: Saturday factor 0.35, Sunday 0.3; hour curve peaks 10:00–15:00 in `weights.yaml: business_timezone` (2.2 vs 0.3 at night); annual sinusoid ± 0.1; 10 fixed holiday dips |
| Acknowledge | `u_acknowledged_at` on 85 % of incidents; minutes ~ log-normal (median P1 5, P3 45) |
| MTTR | log-normal per priority: median P1 3 h, P2 8 h, P3 30 h, P4 72 h, P5 240 h, σ = 0.9; per-team multiplier ~ log-normal(0, 0.2) |
| Reassignment / reopen | reassignments ~ Poisson(0.6), plus 0.8 for teams with multiplier > 1.3; reopen probability 4 % (P4/P5 6 %) |
| SLA | `made_sla = false` when duration exceeds 4 h, 12 h, 3 d, 7 d or 30 d, by priority |
| Customer impact | on 70 % of P1/P2 incidents: duration × U(0.3, 1.0) minutes; NULL otherwise |
| Changes | standard 60 %, normal 35 %, emergency 5 %; `close_code` successful 90 %, with issues 5 %, unsuccessful 3 %, backed out 2 %; emergency changes fail 3× as often |
| Problems | one per ~80 incidents in recurring clusters; `known_error` 40 % |
| Events | per service ~ Pareto; severities critical 5 %, major 15 %, minor 30 %, warning 35 %, info 15 %; 20 % of non-info events lie within ±30 min of an incident on the same service, and half of those carry `incident_ref` |
| metric_daily | availability 99.5–99.99 % with dips on P1 days; `request_count` follows the arrival curve (T5 uses it) |
| Jira | initiatives ≈ 1 % → epics ≈ 5 % → features ≈ 14 % → stories/bugs/tasks ≈ 80 %; story points {1,2,3,5,8,13} with p = {.15,.25,.25,.2,.1,.05}; cycle time log-normal (median 6 d); 12 % re-enter In Progress; 15 % carried over; 3 % mention an INC/CHG number in `description` or `remotelinks` |

#### 5.1.4 Free text and PII

Text comes from templates, and no LLM is used. The generator embeds these vocabularies:
- symptoms (≈ 60)
- components (≈ 80, derived from service names)
- root causes (≈ 25), each mapped to exactly one `root_cause` option of spec 03 (`software_defect`, `config_change`, `capacity`, `infrastructure`, `dependency`, `data_issue`, `access_identity`, `user_error`, `unknown`)
- actions (≈ 40)

It uses about 30 template families with 3–6 sentence patterns each. Noise is added: 1 % typos, casing noise, and 5 % Spanish templates. Recurring clusters reuse one family and one slot set with 20 % slot variation. Truth labels come from the slots:
- `root_cause`: the template's cause.
- `change_caused`: the text uses a change-flavored pattern ("after the release…"). This follows the spec 03 instruction text, not the link truth.
- `repeat_issue`: the text says "again" or "recurring".
- `business_impact`: the impact phrase level.
- `owning_team`: the resolving team.

PII injection covers 3 % of incident and 1 % of Jira descriptions with 1–3 spans of the spec 10 types:
- `PERSON`: 500 made-up names in 3 formats, written to `name_directory.csv`.
- `EMAIL`: `@example.com` or `@example.org` only.
- `PHONE`: `+1-555-01xx`, in 3 formats.
- `IP`: `192.0.2.0/24` or `198.51.100.0/24`.
- `EMPLOYEE_ID`: `E\d{6}`.
- `CARD`: Luhn-valid test numbers of the `4111…` family.
- `CREDENTIAL`: `password=…`.
- `URL_TOKEN`: `?token=…`.

Every span goes to `truth_labels.parquet`. `INC`/`CHG` numbers appear next to PII so the "never masked" rule is exercised.

#### 5.1.5 Planted ground truth

Plants are generated after the background data and use disjoint services and teams. Each row names the metrics (spec 04 §5.2), tables and types that must show the plant.

| ID | How it is generated | What the system must output |
|----|---------------------|-----------------------------|
| **T1 bad team** | One team resolves incidents for 3 criticality-2 services with near-median volume (peer bucket `team:crithi`, spec 04 §5.8). Its MTTR multiplier rises linearly from 2.0 to 3.0 over the span, a mean of about 2.5× same-priority peers. Reassignments are +1 on average. Volume is normal. | `score.org` (entity_type `team`) has `rank = 1`. The `mttr_hours` row has `z_score ≥ 2.0` and `trend_slope > 0`. The top `score.action_lever` row names an MTTR metric (`usd_model` `mttr`). The org review `ReportDraft.ranked_entities[0]` is this team, and so is `recommendations[0].target_id`. Golden G03, O01 and O06. |
| **T2 high-ROI epic** | Service S2 (criticality 1) gets a recurring cluster: ≈ 3 % of incidents, 40 % P2, all with customer impact, one template family (`tpl_conn_pool`, root cause `capacity`). Epic E2 in the Jira project/component mapped to S2 is about pooling, with 34 points and a cost estimate of $120k, and 20 incidents in the cluster link to E2 by `remotelinks`. Decoy E2d sits on a low-pain service with 400 points, $900k and alarming words in its summary. | `enrich.cluster`: one cluster holds the T2 members with ARI ≥ 0.80 (spec 03 §10). `score.funding`: E2 `rank = 1`, and E2d is outside the top 5. `score.portfolio` selects E2 in every scenario whose budget ≥ E2 cost. Golden F01–F04 and F06. |
| **T2c cluster without epic** | Service S2c gets a second recurring cluster (`tpl_cert_expiry`, root cause `access_identity`). It has ≥ 80 incidents in the last 12 months, annual pain above `cluster_fix.min_annual_pain_usd`, and no linked work item. | A `cluster_fix` candidate for that cluster is in the top 10 of `score.funding` (spec 04 §10.3). |
| **T3 change-caused cluster** | CI C3 (the application CI of a criticality-2 service) gets 40 emergency changes spread over the span. After each `work_end` = t, 2–6 incidents open on C3 in (t, t + 2 h] with change-flavored text, and only 30 % have `caused_by` filled. Control: 40 normal changes on C3 with no follow-up incidents, and background incidents at the normal rate. | `enrich.incident_change_link` recovers the planted pairs at `score ≥ 0.5` with precision ≥ 0.80 and recall ≥ 0.70 (spec 03 §10). `change_failure_rate`, `emergency_change_ratio` and `change_caused_incident_count` put C3's owning team in the top 3 teams. Golden G08, G17 and O07. |
| **T4 noisy alerting service** | Service S4 emits ≈ 7 % of all events: a flapping `title` set with severities `minor`/`warning`, `incident_ref` NULL, and fewer than 5 % within ±30 min of an S4 incident. Generated `alert_noise_ratio` ≈ 0.95; all other services stay ≤ 0.6. | `alert_noise_ratio` for S4 is ≥ 0.9 and #1 among services. `score.action_lever` for S4's owning team includes `usd_model` `noise`. Golden G06, G07 and O05. |
| **T5 confounder trap** | Team T5 is the only resolving team of retail service S5. Every year from 07-15 to 08-31, S5 `request_count` and incident volume both rise × 2.8. Incidents per 1k requests and `mttr_hours` stay flat, and every prior year shows the same peak. `--end` falls at the end of a peak. | Any `Finding` that attributes the rise to T5's performance ends `rejected` or `revised`. Its `Challenge.checks` has `seasonality` with `result` `concern` or `fail`, and the verdict is `revise` or `reject` (spec 06 §4.3). T5 is outside the top 5 of `score.org`. No `ReportDraft` or `ChatAnswer` text claims that T5 degraded. Golden G15, G16 and O04. |
| **T6 funded outcomes** | Epics E6p (service S6p) and E6u (service S6u) both finish at `effective_at`. S6p and S6u are non-seasonal, have ≥ 5 flat peers each, and carry ≈ 1.5 % of incidents each at `full` (5 % at `small`). From `effective_at + 2 weeks`, S6p volume drops 40 % and MTTR 20 %; S6u stays unchanged. `seed_prior_run` calls `MemoryStore.write_recommendations` for a prior `funding_review` run (two `fund` drafts, `expected_metric = 'incident_count'`), then `MemoryStore.decide(rec_id, "accepted", …, effective_at=effective_at)`. Nothing is written to the lake. | The `outcome_measure` job (spec 07 §5.9; 12-week windows, 2-week settle) writes `verdict = 'paid_off'` for E6p. It writes `no_effect` for E6u at `full`; at `small`, `inconclusive` is also accepted because the power rule may not be met. The next funding review's Planner trace contains both `rec_id`s and verdicts. A new draft similar to E6u gets `delta < 0` (spec 07 §5.10). Golden F05. |

#### 5.1.6 Dirty data (preset `none` = 0, `default`, `heavy` = 5 × default)

| Defect | Default rate | Where | Expected handling (spec 02) |
|--------|-------------|-------|----------------------------|
| Unparseable or odd timestamps (`31/02/2024`, epoch-ms strings, empty) | 0.2 % | incident, change timestamps | cast failures counted in `meta.dq_result` |
| Future timestamps (+2 years) | 0.02 % | incident `opened_at` | DQ warn |
| `resolved_at < opened_at` | 0.05 % | incident | DQ warn; `resolve_h` NULL (spec 04 §5.1) |
| Missing CI and service | 8 % of incidents; 25 % of Jira issues without a mapped component | incident, issue | `service_id` NULL, below the DQ warn thresholds |
| Identical re-emits | 1 % | all entities | removed by dedupe |
| Later versions | 5 % of incidents and issues re-emitted with newer `sys_updated_on`/`updated` | incident, issue | latest wins |
| Tombstones | 0.3 % | incident, issue | `_deleted = true`, `_payload = NULL`, fields NULL (spec 01 §5); dropped |
| Schema drift | month 25+: new field `u_business_impact`; month 30+: `priority` as bare `2` in 50 % of files; one Jira file without the cost custom field | incident, issue | `union_by_name`; the enum macro handles both forms |
| Unknown enum values (`P2-ish`, `Emergency `) | 0.1 % | priority, change type | NULL and counted |

The truth file records exact counts. `tests/integration/test_build_dirty.py` asserts that `meta.dq_result` reports them within ±1 row, and that the build promotes at `default` rates.

#### 5.1.7 Fetch simulation

- **`initial`** (default): an initial load in `dt = end + 1 day`, then 14 daily increment partitions with the later versions, duplicates and tombstones. `_fetched_at` is the partition date plus a random time.
- **`daily`**: each record lands in the `dt` of `_source_updated_at + U(0, 6 h)`. This creates many small files, so it is allowed only at `tiny` and `small`, for watermark tests.

#### 5.1.8 Determinism and throughput

- Work is split into shards `(source, entity, month)`. Each shard gets its RNG from `numpy.random.SeedSequence(seed)` keyed by a stable hash of the shard key, so content does not depend on `--workers` or scheduling.
- A seed-derived catalog is built once in the parent process and passed read-only to the workers: orgs, teams, services, CIs, the change schedule, and plant targets. It resolves cross-entity references.
- Workers (`multiprocessing`, spawn context) stream Arrow batches of 131,072 rows into their own `LakeWriter` (`target_bytes` 128 MB, zstd). Peak RSS is < 2 GB per worker.
- Targets on the reference PC (16 cores, 64 GB, NVMe): `full` < 30 min, `small` < 90 s, `tiny` < 5 s.
- Determinism check: runs with `--workers 1` and `--workers 8` give equal per-entity content hashes. The hash is `md5(string_agg(_record_id || _source_updated_at || md5(_payload) ORDER BY 1))` in DuckDB. File names are ULIDs and are not compared.

### 5.2 Fakes and stubs

**LLM scripts** (`tests/fixtures/llm_scripts/*.yaml`, shared with specs 05 and 06). There is one format and three drivers.

```yaml
match: {role: analyst, dedup_key: "org:team:*:ops"}      # glob on TaskSpec.dedup_key; chat uses dedup_key "chat"
turns:                                                   # indexed by call_index within (role, dedup_key)
  - tool_calls: [{name: get_metric, arguments: {name: mttr_hours, entity_type: team}}]
  - tool_calls: [{name: run_sql, arguments: {sql: "SELECT ..."}}]
  - final: {post_finding: {claim: "Team {{row.team_name}} has the highest MTTR, [[n1]] hours", numbers_from: last_tool_result}}
faults: [{at: 2, kind: malformed_json}]                  # optional; http_500 | http_429 | malformed_json | hang | disconnect
```

- `FakeLLMClient` answers in process, keyed by `(model_role, dedup_key, call_index)` as spec 06 §10 requires.
- `respx_router` replays the scripts through the OpenAI-compatible and Anthropic adapters, for spec 05 adapter tests.
- `StubLLMServer` is a stdlib `ThreadingHTTPServer` exposing `GET /v1/models` and `POST /v1/chat/completions` (JSON and SSE, `tools`, `response_format`). Spec 08 F1 kills it through `kill_service:<name>`, because stub servers register under the compose service names.

`numbers_from: last_tool_result` builds `NumberRef`s (spec 00 §12.1) from the real tool result, so scripted findings pass the Verifier. An unmatched call raises (in-process) or returns HTTP 500 with the prompt hash (HTTP), so gaps in scripts are visible.

**Stub decider** (`StubDeciderServer`). It implements `POST /v1/systemone` and `GET /v1/models` with the spec 03 §3.3 request and response shape (`noul`, `choice` with `probabilities` and `confidence`, `score` with `legend`). Modes:
- `oracle`: returns the `truth_labels` answer with probability `1 − noise` and spreads the rest.
- `hash`: gives deterministic pseudo-labels from `content_hash`.

It supports status 429/529, `malformed_json` and kill.

**Connector fixtures.** These are respx cassettes in `tests/fixtures/connectors/<source>/` (spec 01 §10), recorded from sandboxes in Phase 6 and scrubbed. `api-pages` output seeds the pagination and throughput cases before sandboxes exist.

**Fake clock.** `FakeClock` patches `herness.core.time.now()` and `sleep()`, and `freezegun` covers third-party calls. Backoff, `Retry-After`, lease expiry and schedule tests advance the clock and never sleep.

### 5.3 Evaluation harness (golden questions)

#### 5.3.1 Run flow

1. Resolve the build from `CURRENT`. Read `meta.build.dataset_kind`. When it is `synthetic`, load the truth file for the dataset root given in `run.meta`.
2. For each question: run `setup` hooks, resolve placeholders, and run `reference_sql` read-only. Reference results are cached per `query_id`.
3. Invoke the pipeline:
   - `chat`: `ChatService.answer(session_id=<new>, text, user_ref="eval", mode)`. Consume the `ChatEvent`s up to the final `ChatAnswer`.
   - `funding` / `org`: `Swarm.start(RunRequest(kind, depth, profile, build_id=<pinned>))`, then read `data/reports/<run_id>/draft.json` (`ReportDraft`). One review run per (pipeline, profile, depth) is shared by all its questions. A question with `framing: true` gets its own run with `RunRequest.question` set.
4. Collect the final text, `NumberRef`s, `ranked_entities`, findings of the run (ops `finding`), the trace, and `run.token_usage` / `run.cost_usd`.
5. Grade (§5.3.2), compute the metrics (§5.3.4), append to `results.jsonl`, and compare with the baseline (§5.3.6).

Questions run sequentially on local profiles, and up to 8 run concurrently on `premium`. `--repeat N`: a question passes when a strict majority of repeats pass (deep defaults to 3).

#### 5.3.2 Grading

| Method | Rule |
|--------|------|
| Numeric | The answer carries a `NumberRef` (in `ChatAnswer.numbers`, `Paragraph.numbers` or `RecommendationItem.numbers`) whose `[[<id>]]` marker appears in the text, whose `unit` matches, and whose `value` is within tolerance of the reference. |
| Entities (funding/org) | Read `ReportDraft.ranked_entities` in rank order and compare `entity_id`s with the reference rows: `rank1`, `topk_contains:K:M` (at least M of the reference top K), `set_equals`, or `kendall_tau>=X`. |
| Entities (chat) | Entity names in `ChatAnswer.text` are mapped to IDs by exact, case-insensitive display name in `core.*`, then compared the same way. |
| `must_mention` | Each item matches in the final text. `with_any` requires one phrase in the same sentence as the entity. |
| `must_not_claim` | No sentence matches, in the final text or in any run finding with status `verified`. |
| `truth_ref` | Synthetic only. If the truth entity is not the reference SQL's row 1, the question is a `suite_error` (a generator, build or metric defect), not a model failure. |
| Rubric | An LLM judge scores 1–5 per criterion, for prose only (clarity, actionability, evidence cited, uncertainty stated). It never grades numbers or entities. The judge profile and model are fixed in `config/eval.yaml`. Results are cached in `data/cache/judge/` under SHA-256 of (judge model, rubric, question id, final text). |

**Unsupported numbers** (`count_unsupported`) is independent of the Verifier:
- (a) Every numeral outside `[[<id>]]` markers that does not match `config/app.yaml: reports.allowed_numeral_patterns` (years, ISO dates, quarters, record identifiers; spec 00 §12.1) counts as unsupported.
- (b) Every `NumberRef` counts as unsupported when its `query_id` is missing from ops `evidence` for the build, or its SQL re-executed read-only no longer gives `value` at (`column`, `row_key`) within the spec 05 tolerance.
- (c) Every marker without a `NumberRef` counts as unsupported.

The denominator is all markers plus all stray numerals.

#### 5.3.3 Golden question set (≥ 30; initial 36)

| ID | Pipeline | Question (abridged) | Grading |
|----|----------|---------------------|---------|
| G01 | chat | P1 incidents opened last quarter | numeric exact |
| G02 | chat | `mttr_hours` of P2 incidents on {T2.service}, last 90 days | numeric rel 1 % |
| G03 | chat | Group with highest median MTTR (≥ 200 incidents) | rank1, truth T1 |
| G04 | chat | `change_failure_rate` of org {largest org}, last 6 months | numeric abs 0.005 |
| G05 | chat | Top 3 orgs by `emergency_change_ratio` | kendall_tau ≥ 0.8 |
| G06 | chat | Service with highest `alert_noise_ratio` | rank1, truth T4 |
| G07 | chat | That service's `alert_noise_ratio` | numeric rel 1 % |
| G08 | chat | Incidents linked to changes on {T3.ci} | numeric exact (`enrich.incident_change_link`, score ≥ 0.5) |
| G09 | chat | `reopen_rate` for org {org}, last quarter | numeric abs 0.002 |
| G10 | chat | Top 5 recurring clusters by `customer_impact_minutes` | topk_contains:5:4 |
| G11 | chat | Epics in progress for {T2.service} | set_equals |
| G12 | chat | `cycle_time_days` in project {p}, last quarter | numeric rel 2 % |
| G13 | chat | Projects with the oldest backlog (`backlog_age_days`) | kendall_tau ≥ 0.8 |
| G14 | chat | P1 SLA breaches last month | numeric exact |
| G15 | chat | Is {T5.team} getting worse? | must_mention seasonal/volume; must_not_claim degraded; rubric |
| G16 | chat | Why did incidents on {T5.service} spike recently? | must_mention request volume; must_not_claim team attribution |
| G17 | chat | Which CI's changes caused the most incidents in 6 months? | rank1, truth T3 |
| G18 | chat | MTTR for service "Quantum Ledger" (does not exist) | must_mention not found; 0 NumberRefs |
| G19 | chat | Incident count in 2019 | must_mention no data in range |
| G20 | chat | `change_count` per week for team {t} | numeric rel 2 % |
| G21 | chat | Share of incidents without a mapped service | numeric abs 0.005 |
| G22 | chat | Team with the highest `reassignment_rate` | rank1 (reference SQL) |
| G23 | chat | `mtta_minutes` for P1 last quarter | numeric rel 2 % |
| F01 | funding | What should we fund next? | rank1, truth T2 epic |
| F02 | funding | Top 5 candidates | kendall_tau ≥ 0.8 vs `score.funding` |
| F03 | funding | Addressable pain $ of the top candidate | numeric rel 0.5 % |
| F04 | funding | With budget {base scenario}, what do we fund? | set_equals `score.portfolio` (selected) |
| F05 | funding | Did last quarter's funded epics pay off? | rules (§4.5), rubric |
| F06 | funding | Is {T2.decoy} a good investment? | must_not_claim top priority |
| F07 | funding | Executive summary quality | rubric mean ≥ 3.5 |
| O01 | org | Which group should improve first? | rank1, truth T1 |
| O02 | org | Which action yields the biggest $ impact for it? | top `score.action_lever` metric |
| O03 | org | Top 3 teams by composite | kendall_tau ≥ 0.8 vs `score.org` |
| O04 | org | Should {T5.team} be on the improvement list? | must_not_claim; must_mention seasonal |
| O05 | org | What should {T4.service}'s team do? | must_mention alert noise or tuning |
| O06 | org | Is {T1.team}'s MTTR trend improving? | must_mention rising; NumberRef sign of `trend_slope` > 0 |
| O07 | org | Why is change health poor for {T3 owning team}? | must_mention emergency changes; rubric |

On real builds, only questions without `truth_ref` and without `datasets: [synthetic]` run, and their placeholders resolve from reference SQL.

#### 5.3.4 Metrics per eval run

| Metric | Source |
|--------|--------|
| `answer_correctness` | passed / graded questions, overall and per pipeline and tag |
| `unsupported_number_rate` | §5.3.2, the Verifier escape rate |
| `verifier_rejection_rate` | `verifier_verdict` trace events with status fail / all; findings `rejected` with `verifier_fail` |
| `skeptic_catch` | T5 caught (bool, §5.1.5) |
| `tool_call_success_rate` | `tool_call` events with `ok = true` / all `tool_call` events |
| `retries_per_run`, `fallbacks_per_run`, `repairs_per_run`, `guard_stops_per_run` | trace events `retry`, `fallback`, `repair`, `guard_stop` (specs 05 and 08) |
| `tokens_in`, `tokens_out`, `cost_usd` | `run.token_usage`, `run.cost_usd` of each pipeline run |
| `latency_s` p50/p95 | wall time per chat question and per review run |
| `judge_mean` | mean rubric score |

#### 5.3.5 Comparisons

`--compare` and `--compare-runs` align runs by `(profile, depth)`: `local`, `hybrid` or `premium`, times `fast`, `standard` or `deep`. The output shows every §5.3.4 metric, the deltas, and per-question flips. On the same build, numbers and rankings must be identical across profiles (spec 00 principle 1). Any numeric-question disagreement is reported as a harness defect.

#### 5.3.6 Regression thresholds (`config/eval.yaml`)

| Check | Fails when |
|-------|-----------|
| Unsupported numbers | `unsupported_number_rate > 0` in any profile and depth |
| Correctness | drops more than 3 pp vs the baseline, or falls below the floor: local-standard 0.80, local-deep 0.85, hybrid 0.88, premium 0.90 |
| Planted truth | G03, G06, F01 or O01 fails on synthetic; T5 is not caught at `standard` or `deep` |
| Tool calls | `tool_call_success_rate` below 0.90 local (spec 05 Phase 3), or 0.95 hybrid/premium |
| Latency | chat p95 > 1.25 × baseline, or > 30 s local-standard; standard review > 90 min on `full` (spec 06 T20) |
| Cost | premium `cost_usd` > 1.2 × baseline |
| Suite errors | any `suite_error` |

### 5.4 Classifier evaluation

The acceptance decision belongs to spec 03. `herness eval --suite classifier` does three things:

1. **Cross-checks the gate file.** For the active Laya version (`data/models/laya/CURRENT`), it reads `eval.json` and recomputes each question's metrics on `data/labels/<qsv>/gold/` from the decision cache. It uses spec 03's functions: `herness.enrich.calibrate` for temperature and ECE (15 equal-mass bins, 2-fold cross-fit). It fails when any metric differs from `eval.json` by more than 0.005, or when `gold_sha256` does not match. It also fails when a question listed in `manifest.json: accepted_questions` does not have every criterion `passed`.
2. **Compares deciders.** Per question and decider (`laya`, `openjev`, `jev` when enabled, `llm`, `ensemble` in deep mode, and human agreement), it reports:
   - accuracy and macro-F1 (choice), MAE and within-one (score)
   - ECE with a reliability diagram
   - coverage and accuracy at the question threshold
   - a confusion matrix
   - bootstrap 95 % confidence intervals (1,000 resamples)

   This is the Laya vs OpenJev vs human report that the plan's verification section requires.
3. **Checks synthetic truth.** On synthetic builds it computes the same metrics of `enrich.decision` against `truth_labels.parquet` (a stratified 5k sample). Synthetic results are informational and never feed `eval.json`. For `change_caused` and `repeat_issue` they measure agreement with the text-level truth.

### 5.5 Fault-injection suite (`tests/fault`)

Faults are driven by `herness.core.resilience.fault_point(name)` with `HERNESS_ENV=test` and a `HERNESS_FAULTS` JSON plan (spec 08 §5.13). The suite implements spec 08 §10 F1–F12 exactly as written there, using `StubLLMServer`, `StubDeciderServer` and respx. This spec adds the following cases, each using spec 08's named points.

| # | Plan | Assertions |
|---|------|------------|
| X1 | `task.before_commit kill nth=3` during an org review, then `herness resume` | spec 06 T3/T4 invariants: no `done` task re-executes (fake client call counts), no duplicate `finding`, same `ReportDraft` as a clean run apart from timestamps |
| X2 | `llm.call error:ModelRefused count=1 role=skeptic` on a hybrid test profile with stub Anthropic | fallback to the next chain entry; `fallback` trace event |
| X3 | `decider.batch kill_service:openjev` during escalation (spec 03 §10 fault list) | escalation moves to the LLM decider; no duplicate cache rows after rerun |
| X4 | `embed.batch kill` mid-embedding | rerun re-embeds at most one flush (≤ 2,560 texts) |
| X5 | `sync.before_watermark kill` on the files connector | rerun ingests nothing twice (`file_ingest` fingerprint); watermark advances once |
| X6 | `job.before_complete kill` during an eval job | `herness eval --resume <run_id>` skips questions already in `results.jsonl` |
| X7 | `sql.query error:QueryError p=1.0 role=analyst` | tasks end `dead` after `max_attempts`; the dead-letter list appears in `ReportDraft.dead_tasks`; the run is `partial` when a must-cover task died (spec 06 T15) |

### 5.6 Suites owned by other specs (run here)

- **Memory** (spec 07 §10): the unit, property and integration cases 1–7 live in `tests/integration/memory/`. This spec provides the synthetic series and the T6 seeding. It also adds these cases:
  - Golden F05 runs end to end, with both prior recommendations visible to the Writer.
  - The LoRA export excludes items whose question has cosine ≥ 0.90 to any golden question. `load_suite()` is the source of that question list.
  - Recall p95 < 150 ms for k = 10 on 200k synthetic memory items (bench).
- **SQL guard** (spec 05 §5.4.3 and §10): Hypothesis properties use the `commit` profile per commit and the `nightly` profile (≥ 10,000 examples) at night. `tests/fixtures/sql_ok/` holds the 200-query no-false-rejection corpus, and it grows with every query that procedural memory promotes on synthetic data.
- **Redaction** (spec 10 §10): `tests/fixtures/pii_corpus.jsonl` has ≥ 5k records from `synth_data.py pii-corpus`, which uses the §5.1.4 types, several phone and name formats, and ticket numbers that must stay unmasked. Thresholds come from spec 10: recall ≥ 99 % on patterned types, ≥ 97 % on directory names, and precision ≥ 95 %. The egress spy test (spec 06 T13, spec 10) uses planted corpus spans.

## 6. Errors and resilience

- **Generator.** A `--verify` contract violation raises `SchemaViolation` and exits with code 4. A worker failure aborts the run and calls `LakeWriter.abort()` on every open writer, so no partial files remain. The root directory is left for inspection, and `--overwrite` clears it.
- **Eval.**
  - A pipeline exception fails the question with `error` set, and the run continues.
  - A `reference_sql` error marks `suite_error`.
  - When the judge is unavailable (`ModelUnavailable` after spec 08 retries), rubric checks are marked `skipped`. The question then does not pass, and the run exits 1 if any rubric check was required.
  - Per-question results are appended to `results.jsonl` as they finish, so `--resume` works after a crash.
- **Flaky tests.** None are allowed. A flaky test is quarantined with `@pytest.mark.skip(reason="flaky: <issue>")` and fixed within the phase. Model nondeterminism in evals is handled with `--repeat`, never with retries.

## 7. Configuration

`config/eval.yaml` (spec 00 §11, owner 11):

```yaml
suite: tests/eval/golden.yaml
judge: {profile: local-judge, temperature: 0, cache_dir: data/cache/judge}    # client key local-judge in models.yaml (spec 05 §5.1.3)
repeat: {fast: 1, standard: 1, deep: 3}
thresholds:
  unsupported_number_rate_max: 0
  correctness_drop_max_pp: 3
  correctness_floor: {local-standard: 0.80, local-deep: 0.85, hybrid: 0.88, premium: 0.90}
  tool_success_min: {local: 0.90, hybrid: 0.95, premium: 0.95}
  latency_p95_ratio_max: 1.25
  cost_ratio_max: 1.20
  bench_regression_max: 0.20
classifier: {gate_recompute_tolerance: 0.005, synthetic_sample: 5000, bootstrap: 1000}
baseline: synthetic-42-small
```

Generator parameters are embedded defaults (§5.1.3) that `--params` overrides. The effective parameters are hashed into `params_hash`. `pyproject.toml` holds the pytest markers, Hypothesis profiles, coverage settings, and the ruff and mypy settings.

## 8. Performance targets

Reference PC per spec 02 §9. Results are written to `data/bench/<YYYYMMDD-HHMMSS>-<git_sha>.json` (`pytest-benchmark --benchmark-json` plus timed-CLI records). A regression of more than 20 % against the previous phase-gate bench fails the gate.

| Target | Owner | How measured (`tests/bench/`, marker `slow`) |
|--------|-------|--------------------------------------------|
| Generator `full` < 30 min | 11 | timed CLI |
| Full build 000–299 < 10 min; DQ < 1 min; warehouse < 15 GB | 02 §9 | `herness pipeline` on the full synthetic root; stage timings from the job result and file size |
| `run_scoring` < 5 min; `compute_metric` p95 < 2 s | 04 §8 | timed step; `pytest-benchmark` over 50 metric × grain calls |
| `run_sql` typical aggregate p95 < 2 s; SQL guard p95 < 25 ms; `get_metric` p95 < 2 s | 02 §9, 05 §8 | 200 queries from `sql_ok/` against the full build |
| Dashboard page cold p95 < 2 s, warm < 0.5 s | 09 §8 | each page's data functions timed on the full build, plus a `streamlit.testing` render per page |
| Memory recall p95 < 150 ms (k = 10, 200k items) | 07 | `pytest-benchmark` |
| Redaction ≥ 5,000 records/s per core | 10 | corpus replayed to 100k records |
| Connector replay ≥ 5,000 rows/s | 01 | `api-pages` output of 1M ServiceNow rows through respx |
| Nightly enrichment < 20 min per 10k increment | 03 | `gpu`; stub decider on CPU for the non-GPU share |
| Standard review ≤ 90 min on `full` (local 30B) | 06 T17 | `gpu`; timed `herness review` |
| Unit suite < 60 s | 11 | pytest duration |
| Planted truths hold after the full pipeline | plan Verification | `tests/bench/test_truth_full.py` asserts the T1–T4 and T2c outputs of §5.1.5 on `full` |

## 9. Security

- Only synthetic data is committed. Generated text uses reserved PII ranges only (§5.1.4).
- Real gold labels (`data/labels/…/gold/`), eval results on real builds and real traces stay under `data/`. They are gitignored and use spec 10 folder ACLs.
- Pre-commit runs `python -m herness.core.redact --scan tests/fixtures` (spec 10 `Redactor.scan`). It fails on any detected span outside the reserved synthetic ranges, and on domains in the spec 10 deny list.
- On real builds, eval follows the active profile. The judge is local by default. A hosted judge needs an approved `hybrid` or `premium` profile, and its calls go through the egress guard with redacted final text and the rubric only.
- The truth isolation test (§4.4) keeps pipelines away from planted answers.

## 10. Tests and acceptance criteria

### 10.1 Tests of this spec's own code

- **Generator:**
  - determinism across worker counts
  - `--verify` passes on `tiny` and `small`
  - defect counts match the truth file
  - the synth profile agrees with the generated fields
  - distribution checks on `small`: priority mix ±1 pp, weekend/weekday 0.3 ± 0.05, P2 median MTTR 8 h ± 10 %, top 5 % of services ≥ 35 % of volume
- **Plants by plain SQL on `small`** (independent of spec 04): T1 MTTR ratio 2.3–2.7, T3 pair counts, T4 noise ≥ 0.9 with the spec 04 definition, T5 normalized rate flat within ±10 %.
- **Grading:** table-driven tests per method, including `count_unsupported` on allowed numerals (years, quarters, `INC…`), stray digits, orphan markers, and stale `query_id`s.
- **Fakes:** script matching, fault kinds, SSE framing, stub decider response shape against spec 03 §3.3 fixtures.

### 10.2 Acceptance criteria

1. `synth_data.py --seed 42 --scale full` finishes in < 30 min, and the full pipeline on it meets spec 02 §9 and spec 04 §8.
2. On that build: T1 at `score.org` rank 1; T2 at `score.funding` rank 1; T2c in the top 10; T4 first in `alert_noise_ratio`; T3 links at precision ≥ 0.80 and recall ≥ 0.70; T5 outside the top 5.
3. `herness eval --suite golden --profile local --depth standard` meets every §5.3.6 threshold on seed 42 `small`.
4. `herness eval --mock-llm tests/eval/mock_scripts` runs on CPU in CI in < 5 min, with `unsupported_number_rate = 0`.
5. Spec 08 F1–F7 and F11, and §5.5 X1–X7, pass on CPU with stubs.

### 10.3 Integration suites by component

| Spec | Must-have integration tests (defined in that spec, run by the §4.2 cadence) |
|------|-----------------------------------------------------------------------------|
| 01 | respx cassettes: pagination per source, incremental watermark, 429/5xx, auth failure, drift field; files connector on the generated inbox |
| 02 | `lake_small` golden build snapshot; dedupe/tombstones; drift; blue/green kill; DQ block on a 10 % drop |
| 03 | stub decider `oracle` ⇒ decision coverage ≥ 95 %; cache hit on rebuild; T2 ARI ≥ 0.80; T3 link precision and recall |
| 04 | `metrics_tiny` hand-computed values; property invariants; §10.3 planted truths on `small` |
| 05–06 | script-driven funding and org reviews, T1–T16 of spec 06; Verifier planted-error detection (spec 05 §10) |
| 07 | spec 07 §10 integration 1–7; §5.6 additions |
| 08 | F1–F12; X1–X7 |
| 09 | golden report snapshots; each page renders on `tiny_build`; chat answer shows evidence |
| 10 | profile gates; egress blocked in `local`; redaction corpus; secrets-never-leak |

### 10.4 Phase acceptance gates

| Phase | Gate checks (all must pass) |
|-------|----------------------------|
| 1 Foundation | Generator `tiny`/`small`/`full` meet §5.1.8 targets and determinism; files connector ingests the generated inbox; full build < 10 min, DQ < 1 min, < 15 GB; `lake_small` snapshot; dedupe, drift and blue/green tests; coverage targets for `core` and `store` |
| 2 Metrics & scoring | every catalog metric covered by `metrics_tiny`; `metrics` coverage ≥ 95 %; `run_scoring` < 5 min on `full`; T1 rank 1, T2 rank 1 (E2d outside the top 5), T2c in the top 10, T4 first, T5 outside the top 5, from spec 04 outputs alone; `meta.evidence` re-runs reproduce `result_hash` |
| 3 Harness | mock golden run in CI; real local model `herness eval --suite golden --depth standard` meets §5.3.6 with ≥ 30 questions; 0 unsupported numbers; spec 08 F1–F7 and F11 plus X1, X2, X6, X7 green; spec 06 T1–T16 green; spec 07 integration 1–3, 5, 6 green (4 once `outcome_measure` lands); SQL guard nightly clean; `herness resume` after a kill passes |
| 4 Enrichment | T2 ARI ≥ 0.80 and T3 links on `full`; gold set built (≥ 1,000 records, spec 03 §5.8); `eval.json` cross-check passes; decider comparison report published; decision coverage ≥ 95 %; escalation share ≤ 10 %; nightly enrichment < 20 min per 10k |
| 5 Outputs | funding and org reports render with the evidence appendix; every dashboard page meets spec 09 §8 on `full`; chat golden subset (G01–G23) passes; redaction corpus thresholds met |
| 6 Real connectors | cassette tests per source including 429 and the circuit breaker; two consecutive syncs add zero rows after dedupe; a real build passes DQ; the real-data golden subset meets the correctness floor; the fixtures PII scan is clean |
| 7 Accuracy & upgrade | `--compare` across local/hybrid (and premium when approved) × fast/standard/deep on synthetic and real; identical numbers across profiles; deep ≥ standard + 3 pp, or the gap is documented; a LoRA adapter or model upgrade is adopted only if it is not worse on the golden set (spec 10 upgrade flow) |

### 10.5 CI

- `.pre-commit-config.yaml`: ruff (lint and format); mypy `--strict` on `herness/core`, `herness/store`, `herness/metrics`, `herness/eval`; the fixtures PII scan; `pytest -m unit -x -q`.
- Optional `.github/workflows/ci.yml`: Ubuntu, Python 3.12, `uv sync --frozen`, the pre-push selection from §4.2, and the mock golden run. It uploads `coverage.xml` and the eval `report.md`. It needs no secrets, no GPU and no network egress beyond package installation.
- The dev box runs the nightly selection through the job queue (`job.kind = 'eval'`), and results land in `data/reports/eval/` and `data/bench/`.

## 11. Open questions

1. Resolved (spec 06 v2 §4.4): `ranked_entities: list[RankedEntity{rank, entity_type, entity_id}]` in rank order, derived by the swarm; grading compares `entity_id`s.
2. Resolved (spec 06 v2 §4.2, §4.4): findings and drafts use spec 05 `NumberRef` with `id`, markers `[[nX]]` and unit `usd` (spec 00 §12.1).
3. Resolved (spec 02 v2 §4.7): `meta.build.dataset_kind` is `synthetic` when the active profile is `synth` or any lake file comes from `data/synth/`, else `real`.
4. Resolved (spec 09 v2 §5.6): the `eval` row registers every §3.2 option.
5. **Question-scoped reviews.** Funding and org questions share one review run per (pipeline, profile, depth), so these questions are not independent. `RunRequest.question` framing is used only for `framing: true` questions.
6. **Name directory source for redaction** (spec 10 Q5). The `synth` profile points it at `name_directory.csv`. The real source is still open.
7. Resolved: spec 08 defines `fault_point` in §3.3 and its behavior in §5.13; this spec cites both.
8. Resolved (spec 05 v2 §5.1.3, §7): client `local-judge`, a different model family from the Writer's `local-30b`. The exact judge model is pinned at Phase 7.
9. **T6 `no_effect` needs statistical power.** It is reliable only at `full` because of the spec 07 power rule. At `small`, `inconclusive` is accepted.

## 12. Dependencies

- Specs:
  - 00: layout, storage, IDs, `NumberRef` markers, config files
  - 01: field names, files connector, fixtures
  - 02: lake contract and `LakeWriter`, tables, `dataset_kind`, `run.meta`
  - 03: decider wire shape, gold set, `eval.json`, calibration functions, question set
  - 04: metric catalog, score tables, peer groups, planted-truth acceptance
  - 05: trace events, SQL guard, Verifier tolerance, script format
  - 06: `RunRequest`/`Swarm`, `ReportDraft`, `Finding`/`Challenge`, `ChatService`, `FakeLLMClient` requirements
  - 07: `MemoryStore` episodic API, outcome job, LoRA export exclusion
  - 08: `fault_point` (§3.3, §5.13), `HERNESS_FAULTS`, F1–F12, job queue
  - 09: CLI registration, dashboard targets, report fixtures
  - 10: profiles, `--set`, redaction corpus thresholds, egress guard, `Redactor.scan`
- Packages (spec 00 §9): `pytest`, `pytest-asyncio`, `pytest-cov`, `pytest-benchmark`, `pytest-timeout`, `hypothesis`, `respx`, `freezegun`, `mongomock`, `numpy`, `scipy`, `ruff`, `mypy`, `pre-commit`.

## 13. Contract changes (resolved)

| # | Former request | Where it lives now |
|---|----------------|--------------------|
| 1 | Test and eval layout (`tests/eval/`, `tests/bench/`, `tests/support/`, `herness/eval/grading.py`, `report.py`) | spec 00 §3 |
| 2 | `config/eval.yaml` and `config/profiles/synth.yaml` | spec 00 §11 |
| 3 | Eval, judge cache, bench, synthetic, gold and inbox storage | spec 00 §4 (`data/reports/eval/`, `data/cache/judge/`, `data/bench/`, `data/synth/<seed>-<scale>/`, `data/labels/<qsv>/gold/`, `data/inbox/<entity>/`) |
| 4 | Test and data dependencies (`pytest-cov`, `pytest-benchmark`, `pytest-timeout`, `ruff`, `mypy`, `pre-commit`, `numpy`) | spec 00 §9 |
| 5 | Raw entity names for the generator | spec 02 §3.1 |
| 6 | `meta.build.dataset_kind` | spec 02 §4.7 (including how it is set) |
| 7 | `run.meta JSON` | spec 02 §5 `run` |
| 8 | Fault hook API and named points | spec 08 §5.13 (`fault_point`, `HERNESS_FAULTS`) |
| 9 | Classifier gate file | spec 03 §4.4 (`data/models/laya/<version>/eval.json`) |
| 10 | Writer ranked entities and cited numbers for grading | spec 00 §12.1–12.2, spec 06 §4.4 |
