# T04-10 report — Change and monitoring metrics #15-#19, #27-#28 and macro period_end_date

Status: DONE_WITH_CONCERNS (budget note for T04-11) — commit 6667ce3

## Implemented
- config/metrics.yaml: catalog entries #15 change_count, #16 change_failure_rate,
  #17 change_caused_incident_count, #18 change_lead_time_hours, #19 emergency_change_ratio,
  #27 availability_pct, #28 error_rate per U04-48 tables A and B (inserted after #14; T04-11
  inserts #20-#26 between #19 and #27). Each is one recorded SELECT ending
  filter_clause / entity_filter / GROUP BY ALL. No Python arithmetic.
  - #15: `{% set PS = period_start('c.actual_end') %}`; weeks = date_diff('day',
    greatest(PS, window_start_date), least(period_end_date(PS), window_end_date)) / 7.0
    (DD04-05). value = count(*) / any_value(weeks), denominator any_value(weeks): DuckDB
    rejects mixing count(*) with the non-aggregated weeks expression under GROUP BY ALL
    ("Cannot mix aggregates with non-aggregated columns"); weeks is constant per
    (entity, period_start), so any_value is exact.
  - #17: incident_fact f JOIN (core.incident.caused_by_change_id UNION enrich links with
    score >= d_change_link_min_score) k JOIN change_fact c; entity from c; anchor f.opened_at;
    NOT f.excluded; filter_clause('change', 'c'); count(DISTINCT f.record_id).
  - #18: optional column coverage = count(lead_time_h) / count(*); requires_columns
    [core.change.opened_at].
  - #27/#28: derived pairs m(date, service_id, v = avg(value) of the metric's rows,
    rc = max(value) of request_count rows) over core.metric_daily; DATE anchor with
    period_start_date and window_start_date/window_end_date. Weighted test
    `bool_and(coalesce(m.rc > 0, false))`. #27: service grain avg(v) (sum v / pairs, no
    unweighted column), org grain weighted rule + `unweighted` column. #28: weighted rule +
    `unweighted` at every grain. sample_size = count(DISTINCT m.date).
- herness/metrics/sql/_macros.sql.j2: `period_end_date(ps)` (U04-35): week/month/quarter
  -> `CAST(ps + INTERVAL 1 <period> AS DATE)`; t12w/t12m -> `p('window_end_date')`.
- Optional column `unweighted`: already carried end to end (render._OPTIONAL_COLUMNS,
  _catalog_checks column order, metric_wrapper.sql.j2 `CASE WHEN q.unweighted THEN
  'unweighted' END`, METRIC_FLAGS). No compute.py / render.py change (render.py stays 299).
- scoring.org.metrics: added change_failure_rate 0.15 (verified vs design 04 §7.1); marker
  comments in YAML, test_metrics_settings.py and test_metrics_render.py now name only T04-11
  (cycle_time_days 0.10, unplanned_work_ratio 0.10, epic_predictability 0.05). Partial sum
  0.60 -> 0.75.

## Files changed
- config/metrics.yaml (405 -> 596 lines; budget 760)
- herness/metrics/sql/_macros.sql.j2 (132 -> 138; budget 260)
- tests/unit/metrics/test_metrics_change.py (new; UT04-50…54, UT04-62, UT04-63; 13 tests)
- tests/fixtures/metrics_tiny/core.change.csv (new: CH1-CH6)
- tests/fixtures/metrics_tiny/core.metric_daily.csv (new: 11 rows, S1/S2, 2026-01-05/06)
- tests/fixtures/metrics_tiny/enrich.incident_change_link.csv (+ I3 -> CH4 score 0.9)
- tests/unit/metrics/test_metrics_catalog.py (UT04-13 shipped names now #1-#19, #27, #28)
- tests/unit/metrics/test_metrics_settings.py (scorecard + partial sum 0.75, marker)
- tests/unit/metrics/test_metrics_render.py (marker comment only)
- tests/unit/metrics/test_metrics_facts_delivery.py (fixture DELETE list + core.change; the
  UT04-32 empty-input test deletes the new core.change rows first — same assertions)
- tests/unit/metrics/test_metrics_facts.py (stale "change_fact is empty" comment dropped)
Fixture: metrics_tiny now 47 rows (<= 60, design §10.1). #1-#14 expectations unchanged.

## Fixture (2026Q1, America/New_York; T1 in O2, T2 in O3, O3 < O2 < O1)
CH1 normal S1/T1 lead 6h, failed via I3 link 0.8; CH2 emergency S1/T1 lead 2h unsuccessful;
CH3 standard S1/T1 no opened_at; CH4 normal S2/T2 lead 24h, failed via I3 link 0.9;
CH5 no actual_end, CH6 canceled (not deployed). metric_daily pairs: 01-05/S1 v=avg(99,98)=98.5
rc 1000; 01-06/S1 v=100 rc 3000; 01-05/S2 v=97 rc=max(1000,500)=1000; error 0.02, 0.01, 0.05.

## Expected values (compute_metric, quarter, every declared grain, unfiltered and filtered)
Default quarter window [2024-04-01, 2026-04-01): Q1 = 90 days = 90/7 weeks.

| UT | Metric | min_n used | S1 / T1 | S2 / T2 | O1, O2 | O3 |
|----|--------|-----------|---------|---------|--------|----|
| 50 | change_count | shipped 1 | 3/(90/7), num 3, den 90/7, n 3 | 1/(90/7), n 1 | 4/(90/7), n 4 | 1/(90/7) |
| 51 | change_failure_rate | 1 (shipped 10 -> NULL checked) | 2/3 | 1/1 | 3/4 | 1/1 |
| 52 | change_caused_incident_count | shipped 1 | 1 | 1 | 1 (I3 once, DISTINCT) | 1 |
| 53 | change_lead_time_hours | 1 (shipped 10 checked) | 4.0, den 2, [low_coverage] | 24.0, den 1 | 6.0, den 3, [low_coverage] | 24.0 |
| 54 | emergency_change_ratio | 1 (shipped 10 checked) | 1/3 | 0/1 | 1/4 | 0/1 |
| 62 | availability_pct (service, org) | 1 (shipped 7 checked) | S1 99.25, num 198.5, den 2, n 2 | S2 97, 97, 1, n 1 | 99.1, 495500, 5000, n 2 | 97, 97000, 1000, n 1 |
| 63 | error_rate (service, org) | 1 (shipped 7 checked) | S1 0.0125, 50, 4000, n 2 | S2 0.05, 50, 1000 | 0.02, 100, 5000, n 2 | 0.05 |

Extra cases: #15 custom month window [01-05, 03-01) -> Jan 7/27 (den 27/7, partial_period),
Feb 2/4; t12w [01-07, 04-01) -> 3/12. #16 change_type=emergency -> T1 1/1. #17 after
I1.caused_by_change_id = CH2 and excluded I4 -> CH3: S1/T1/O1/O2 2, S2/T2/O3 1; emergency
filter -> S1 1. #27 team_id=T1 filter at O1 -> 99.625 (only owner-mapped S1); without the
01-06 S1 request_count: O1/O2 98.5, 295.5, 3, [unweighted]. #28 without it: S1 0.015, 0.03, 2,
[unweighted]; O1/O2 0.08/3, 0.08, 3, [unweighted]; S2/O3 stay weighted 0.05.
period_end_date macro rendered text checked for all five periods.

## Budgets / sizes
- config/metrics.yaml 596 / 760 (my 7 entries +191 lines incl. scoring/comments; ~27/entry).
- _macros.sql.j2 138 / 260. render.py 299 / 300 untouched. check_module_size exit 0.
- Longest rendered SQL: 2,626 chars (availability_pct / org / quarter, all filters,
  entity_ids set); per metric max: change_count 2,337, cfr 1,773, change_caused 2,141,
  lead_time 1,848, emergency 1,797, availability 2,626, error_rate 2,614. Far under 20,000.
- Catalog compaction: NOT possible as suggested. config/metrics.yaml is read by
  herness.core.config_sources._StrictLoader (metrics is in _STEMS), which rejects YAML
  anchors/aliases (U10-15 step 4), so `<<: *meta` merge keys fail load_config. MetricDef has
  no defaults (every key required) and flow mappings cannot hold the `|` sql block, so the
  16 metadata lines per entry are irreducible. #1-#14 were left byte-identical (verified:
  MetricsCatalogConfig of the base file vs new -> metrics[:14] and defaults equal).

## Tests / gates
- tests/unit/metrics: 584 passed; tests/unit/core + security config + harness: 3224 passed;
  metrics integration/fault/security subset: 616 passed.
- RED: new tests failed first (AttributeError: no period_end_date; ToolInputError unknown
  metric change_count); first GREEN attempt hit the DuckDB binder error fixed by any_value.
- ruff format/check clean, mypy clean, lint-imports 13 kept, check_type_ownership 0,
  check_module_size 0, --require-test-ids ok.

## Deviations / spec notes
- #15 uses any_value(weeks) (see above) — same value as the table A formula.
- #27/#28 pairs whose metric rows are all NULL-valued are dropped (`m.v IS NOT NULL`); the
  derived table pre-filters metric_name IN (<metric>, 'request_count').
- #27 at service grain renders `CASE WHEN false THEN … ELSE avg(m.v) END` (one template for
  both grains) and emits no `unweighted` column, per table A (org grain only).
- metric_daily team filter uses the owner team of the service (existing FILTER table), so
  services without an owner row (S2) drop out under a team_id filter (tested).

## Concerns
- Budget for T04-11: 164 lines remain in config/metrics.yaml for 7 entries (#20-#26) whose
  SQL is larger (spine, cat_at_join, epic closure). At 16 metadata lines each that leaves ~7
  SQL lines per entry — likely insufficient. Anchors are rejected by the config loader, so a
  budget ruling (e.g. ~820-850) or moving shared SQL into macros is needed before T04-11.

## Commit
6667ce3 feat(metrics): change and monitoring metrics #15-#19, #27-#28 (T04-10); all pre-commit hooks passed incl. pytest-unit.
