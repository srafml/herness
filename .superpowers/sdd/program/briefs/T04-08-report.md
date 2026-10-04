# T04-08 report — Compute API and first metrics (build agent)

Worktree: D:\herness\.claude\worktrees\agent-aa288cfffa689d3a0 (branch worktree-agent-aa288cfffa689d3a0, base 02bd94a).
Commits: a322c1b `wip(T04-08): compute API, metrics #1-#4 and config carry-overs` (builder 1, also carries builder 2's three ruff fixes to test_metrics_compute.py); final 6cb7c65 `feat(metrics): compute API and metrics #1-#4 (T04-08)` (builder 2; all pre-commit hooks passed incl. pytest-unit, no skip).

## Built
- `herness/metrics/compute.py` (337/340; config/metrics.yaml now 145/760): `MetricRow` (U04-49), `MetricResult` (U04-50, frozen/forbid/strict; flags vocabulary+sorted validator; row_count == len(rows); `recorded()`), `validate_metric_request` (U04-51), `compute_metric` (U04-52), `metric_series` (U04-53). `# T04-17:` marker for the `peer_group`/`PeerGroupInfo` re-export.
- `herness/metrics/_request.py` (95, new private split, budget 110): U04-51 steps 4-5 (entity-ID and filter normalization, U04-28 per-key type/enum checks). Mirrored §2 module-map row added to docs/impl/04 in the same commit.
- `config/metrics.yaml` (143/760): entries #1 incident_count, #2 p1p2_count, #3 mttr_hours, #4 mttr_p50_hours per tables A and B (templates use p() binds, end with filter_clause, entity_filter, GROUP BY ALL). `version: 1`. `scoring.org.metrics` trimmed to `{mttr_hours: 0.15}` with a `# T04-09/10/11:` marker listing the spec weights to restore (coordinator ruling, see spec note 1).
- render.py 299, facts.py 148, evidence.py 386: untouched.

## Hard rules
- Rows are copied by column name from the recorded wrapper rows (`run_recorded(..., None, build_id=..., timeout_s=defaults.compute_timeout_s)`); no Python arithmetic on values. `MetricResult.flags` = sorted `static_flags` bind.
- Grain/period/filter keys allowlisted before render; all values typed binds. `con=None` → `open_readonly(None)`, closed in finally; a given con is never closed.
- Logs: `metrics.compute.completed` with metric, entity_type, period, row_count, query_id, duration_ms only. Counter `herness_metrics_compute_calls_total{outcome=ok|input_error|query_error}` via `record_counter`. Error messages name keys/rules, never filter values.

## Tests
- New tests/unit/metrics/test_metrics_compute.py: 42 tests covering UT04-36..39 (every grain on metrics_tiny, quarter; plus week spot check and all-filters render), UT04-64..70, ST04-01, ST04-07, ST04-11. Coverage compute.py 100 % line / 100 % branch; _request.py 100 %/100 %.
- Scoped run (builder 2, after the scorecard ruling): tests/unit/metrics + test_config_templates + test_config_load + test_runner: 625 passed, 0 failed; compute.py and _request.py 100 % line / 100 % branch. Checkpoint commit's pre-commit `pytest -m unit` hook: passed.
- Gates (re-run by builder 2): ruff format/check clean, mypy 0, lint-imports 13 kept, check_type_ownership 0, check_module_size exit 0.

## Rendered SQL lengths (#1-#4, every grain x period, with and without all five filters; pre-normalization)
incident_count 1246-1776; p1p2_count 1266-1796; mttr_hours 1318-1850; mttr_p50_hours 1309-1841 chars. All far below the 20,000 Evidence.sql cap.

## Carry-overs closed
- (a) UT04-13 strict xfail removed; UT04-13 now expects a CLEAN load (zero error issues from validate_catalog, load_catalog succeeds, names == #1-#4, scorecard ⊆ shipped names).
- (b) tests/support/config_tree.py metrics stand-in dropped (metrics.yaml copied verbatim); stale "until T04-08" text updated in metrics_render.py, metrics_tiny.py (patch_facts_config now uses the shipped catalog via new `shipped_catalog()` helper), test_config_templates.py (UT10-76: bare config/ now loads for local and synth), test_runner.py; test_config_load.py RF test updated for the new first entry.
- (c) T01-06: new `test_ut01_29_default_data_root_is_config_paths_data` loads the real repo config via init_config and checks SyncRunner.data_root and SyncResult.to_dict default to paths.data.

## Decisions / spec notes
- `MetricResult.recorded()`: the unit's field list lacks executed_at/duration_ms/columns needed by RecordedQuery/Evidence, so compute_metric keeps the RecordedQuery in a pydantic PrivateAttr and `recorded()` rebuilds a copy (dataclasses.replace); a MetricResult not produced by compute_metric raises ConfigError. No public fields added.
- `entity_ids` as a bare string is rejected ("must be null or a list of IDs"); windows must be a (date, date) tuple (datetimes rejected) before custom_window.
- Counter outcomes only for the three spec labels; other errors (SchemaViolation, StoreBusy, ConfigError) propagate uncounted.
- `meta.build` read for build_id + started_at: not exactly one row / missing table → SchemaViolation("meta.build must hold one row").

## Builder 2 (resume) changes
- config/metrics.yaml: scorecard trimmed per coordinator ruling (validator NOT weakened).
- tests/unit/metrics/test_metrics_catalog.py: UT04-13 rewritten to expect zero errors.
- tests/unit/metrics/test_metrics_render.py (UT04-26 default_binds): the test now injects the design 04 §7.1 scorecard (`SPEC_SCORECARD`) into its FakeCatalog so the s_org_* pairs and s_lower_better intersection are still exercised in full; assertions unchanged.
- tests/unit/metrics/test_metrics_settings.py: `sum(weights) == 1.0` replaced by `== {"mttr_hours": 0.15}` with a `# T04-09/10/11:` marker to restore the sum check.
- tests/unit/connectors/test_runner.py: builder 1's staged T01-06 test (`test_ut01_29_default_data_root_is_config_paths_data`) committed.
- ruff fixes in test_metrics_compute.py (RUF043 raw regex, RUF024 dict.fromkeys, PT012 raises block).

## Spec notes
1. (Coordinator ruling) U04-26 step 8 rejects scorecard names not in the catalog, so with only #1-#4 shipped the design §7.1 scorecard cannot ship intact. `scoring.org.metrics` = `{mttr_hours: 0.15}` until T04-09 (repeat_incident_rate 0.15, sla_breach_rate 0.10, reopen_rate 0.05, reassignment_rate 0.05, alert_noise_ratio 0.10), T04-10 (change_failure_rate 0.15) and T04-11 (cycle_time_days 0.10, unplanned_work_ratio 0.10, epic_predictability 0.05) restore their weights; those cards must also restore the UT04-13 names list, the settings-test sum==1.0 check and remove the markers. Remaining non-error issue: `warn` "usd_model unused" for mttr_p50_hours (spec-inherent: it has usd_model mttr but is not a scorecard metric).
2. (Coordinator ruling) Acceptance line: mttr_hours min_sample_size stays 10 (Table B). On metrics_tiny (3 resolved incidents) the shipped catalog yields value NULL + insufficient_sample (numerator 12, denominator 3, UT04-64); 4.0 holds with min_n ≤ 3, which UT04-38 and the acceptance test set via `shipped_catalog(mttr_hours=3)`. The spec acceptance line should say "with min_sample_size ≤ 3".

## Concerns
1. (Resolved by builder 2, see spec note 1.) The shipped catalog now validates with zero errors, so T09-20 may register the metrics owner validator at any time. Until T04-09..T04-11 land, org scoring runs on a one-metric scorecard.
2. Acceptance value vs min_sample_size: Table B sets mttr_hours min n = 10; metrics_tiny has 3 resolved incidents, so on the shipped catalog compute_metric("mttr_hours","team",None,"quarter") returns value NULL + insufficient_sample (numerator 12, denominator 3). 4.0 is produced when the catalog's min_sample_size is ≤ 3 (tests lower it via `shipped_catalog(mttr_hours=3)`); UT04-64 asserts the shipped-min behaviour. Controller may want to note this in the spec's acceptance line.
3. Environment (builder 1 only; C: since freed, builder 2 used default temp): C: was full (~0.9 GB free); the pre-commit unit hook fails UT01-50 (1 GiB sparse file) with ENOSPC under the default temp. Commits were run with TMP/TEMP=D:\t0408-pytest-tmp (a temp inside the repo breaks UT11-37's pytester run). No hook skipped.
