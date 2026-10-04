# T04-09 report — Ops metrics #5-#14

Status: DONE — commit 6f70ffe feat(metrics): ops metrics #5-#14 (T04-09); all pre-commit hooks passed incl. pytest-unit

## Implemented
- config/metrics.yaml: catalog entries #5-#14 (mttr_business_hours, mtta_minutes,
  customer_impact_minutes, repeat_incident_rate, reopen_rate, reassignment_rate, sla_breach_rate,
  alert_noise_ratio, toil_hours_est, incident_cost_usd) per U04-48 tables A and B, same style as
  #1-#4. Each is one recorded SELECT; every template ends filter_clause/entity_filter/GROUP BY ALL.
  Optional columns: coverage (#5, #6), estimated_count (#7). #6 joins core.incident i on record_id
  and computes ack via a template-local `{% set ACK %}` (no new macro; _macros.sql.j2 untouched).
  #12 reads core.event e with d_noise_severities (entity via existing event GRAIN/OWNER/SVC_CLOSURE).
- scoring.org.metrics: added repeat_incident_rate 0.15, sla_breach_rate 0.10, reopen_rate 0.05,
  reassignment_rate 0.05, alert_noise_ratio 0.10 (verified vs design 04 §7.1); marker comment now
  lists only T04-10 (change_failure_rate 0.15) and T04-11 (cycle_time_days 0.10,
  unplanned_work_ratio 0.10, epic_predictability 0.05). Partial sum 0.60.

## Files changed
- config/metrics.yaml (145 -> 405 lines; budget 760, leaves 355 for 14 metrics)
- tests/unit/metrics/test_metrics_ops.py (new, UT04-40 … UT04-49)
- tests/unit/metrics/test_metrics_catalog.py (UT04-13 shipped names list now #1-#14)
- tests/unit/metrics/test_metrics_settings.py (marker + scorecard expectation, partial sum 0.60)
- tests/fixtures/metrics_tiny/core.incident.csv (+acknowledged_at column: I1 +15 min, I2 +45 min,
  I3 before opened_at -> NULL, I4 empty)
- tests/fixtures/metrics_tiny/core.event.csv (new: E1-E6 on S1, Q1 2026)
- tests/fixtures/metrics_tiny/core.service_map.csv (new: S1 owner T1)

## Tests (compute_metric at quarter, every declared grain, unfiltered AND with every allowed filter;
value, numerator, denominator, sample_size, flags asserted; evidence query_id; SQL < 20,000)
Grains: service S1, team T1, org O1+O2, cluster C1 (no cluster for #6, #12).
| UT | Metric | min_n used | value | num | den | n | flags |
|----|--------|-----------|-------|-----|-----|---|-------|
| 40 | mttr_business_hours | 2 (shipped 10 -> NULL + insufficient_sample checked) | 4.0 | 12 | 3 | 3 | [] ; with I3 resolve_bh NULL: 3.0/6/2/2 [low_coverage] |
| 41 | mtta_minutes | 2 (shipped 10 checked) | 30.0 | 60 | 2 | 2 | [low_coverage] (coverage 2/3) |
| 42 | customer_impact_minutes | shipped 1 | 150 | 150 | NULL | 3 | [unconfirmed_weights]; with I2 impact_estimated -> [estimate, unconfirmed_weights] |
| 43 | repeat_incident_rate | 3 (shipped 20 checked) | 1/3 | 1 | 3 | 3 | [] |
| 44 | reopen_rate | 3 (shipped 20 checked) | 1/3 | 1 | 3 | 3 | [] |
| 45 | reassignment_rate | 3 (shipped 20 checked) | 2/3 | 2 | 3 | 3 | [] |
| 46 | sla_breach_rate | 2 (shipped 20 checked) | 1/3 | 1 | 3 | 3 | [] ; with I3 sla NULL: 1/2 over 2 |
| 47 | alert_noise_ratio | 5 (shipped 50 checked) | 3/5 | 3 | 5 | 5 | [] ; severity=major filter: 1/1 (value NULL at min 5) |
| 48 | toil_hours_est | shipped 1 | 6.8 | 6.8 | NULL | 3 | [estimate, unconfirmed_weights] |
| 49 | incident_cost_usd | shipped 1 | 20680 | 20680 | NULL | 3 | [estimate, unconfirmed_weights] |
Values for #7-#11, #13, #14 are the design 04 §10.1 values; #5 (4.0), #6 (30), #12 (3/5) are
hand-computed from the fixture rows added/present.

Results: tests/unit/metrics 571 passed; related fault/integration/security/config tests 228 passed.
Gates: ruff check/format clean, mypy clean, lint-imports 13 kept, check_type_ownership 0,
check_module_size 0.

## Longest rendered SQL
2,337 chars (mtta_minutes / org / quarter with all filters) — far under the 20,000 evidence cap.

## Deviations / spec notes
- Optional columns are not MetricRow fields (wrapper turns them into flags), so coverage and
  estimated_count are asserted through low_coverage / estimate flags, including in-test UPDATEs of
  metrics.incident_fact to exercise the flag paths without changing the §10.1 values.
- #6 mtta: `{%- set ACK = ... %}` inside the YAML template (one line > 100 chars in YAML) instead
  of a macro, per "do not edit _macros.sql.j2".
- Ratios use DuckDB integer `/` (returns DOUBLE); no NULLIF needed since count(*) >= 1 per group.

## Concerns
- None blocking. Commit hooks run the full unit suite (~6 min per commit), so only one checkpoint
  was attempted: the red-test wip commit was rejected by the hooks (ruff E501 + failing tests) and
  the work was committed once green as the final commit.
