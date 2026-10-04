# T04-16 report — Org score (impl 04 U04-67, U04-68, U04-35 team_bucket/observed_days)

Worktree: D:\herness\.claude\worktrees\agent-ab7f9ecd397f34037 (branch worktree-agent-ab7f9ecd397f34037, base e41d62c).
Status: DONE. Checkpoint 7d1ada6 (wip) holds all code; final commit b1d2f74 "feat(metrics): org score step and template (T04-16)" (empty marker commit, hooks passed).

## Files (lines / budget)
- herness/metrics/org.py — 40 / 110 (new; `run_org_step(con, sc, /) -> StepResult`, `ORG_TABLE`)
- herness/metrics/sql/org_score.sql.j2 — 172 / 220 (new, static template; U04-67 steps 1-9)
- herness/metrics/sql/_macros.sql.j2 — 138 -> 154 / 260 (two blocks appended at the END only:
  `BEGIN team_bucket (U04-35; T04-16)` ... `END team_bucket`, then
  `BEGIN observed_days (U04-35; T04-14 + T04-16 shared)` ... `END observed_days`; no existing macro touched)
- tests/unit/metrics/test_metrics_org.py — new (UT04-88..UT04-91, PT04-07; 15 functions)
- tests/support/metrics_org_oracle.py — 66, new (pure-Python robust z, Theil-Sen, composite oracle)
- Not touched: scoring.py (STEPS "org" stays `None  # T04-21:`), _scoring_checks.py, render.py,
  _binds.py, shared metrics_tiny CSVs, metrics_oracle.py.

## Implementation
- run_org_step: precondition via `existing_tables` (information_schema) -> SchemaViolation("metric_value missing; run step metrics first");
  `render_named("org_score", {}, {**sc.binds(), "unconfirmed": False})`; ONE
  `run_recorded(..., "score", build_id=sc.build_id, into=IntoSpec("score.org", "replace", "query_ids"))`
  (query_ids = [own], shared result_hash, evidence row producer "score"); QueryError -> SchemaViolation("org score failed: ...")
  (same pattern as run_metrics_step). Row count from RecordedQuery.row_count. No Python arithmetic.
- Template binds used: s_org_metrics, s_org_weights, s_lower_better, window_start_date, as_of,
  s_min_peer_group, s_trend_weight, s_min_weight_coverage, unconfirmed (all BIND_TYPES keys).
- Rendered SQL (shipped catalog): 6,752 chars raw, 5,841 normalized (cap 20,000).

## VI04-06 result
DuckDB 1.5.5 `mad()` IS the unscaled median absolute deviation, with interpolated medians:
mad([1,2,3,4,100]) = 1.0, mad([1,2,4,7]) = 1.5, mad([1,1,1,5,9]) = 0.0. Asserted in
test_ut04_88_duckdb_mad_is_unscaled; the template uses `mad(x)` directly.

## Tests
- `pytest tests/unit/metrics/test_metrics_org.py`: 15 passed (~7.5 s; PT04-07 max_examples=25).
- Coverage herness/metrics/org.py: 100 % line, 100 % branch.
- `pytest tests/unit/metrics -q -p no:logging`: 647 passed (193 s).
- ruff format/check clean, mypy strict clean (334 files), lint-imports 13 kept, check_module_size exit 0, check_type_ownership exit 0.
- RED evidence: none recorded — the template and step were written before the test file in this
  session (tests went green on first run); flagged honestly here.

## Decisions / spec notes
1. Flags are emitted in a fixed sorted order: insufficient_sample, low_coverage, no_data, no_trend, peer_fallback.
2. x = metric_value.value unless the row carries `insufficient_sample` (then NULL; flag copied) — covers
   metrics whose wrapper already NULLs the value (min_sample_size) and the #27 org-only `unweighted` case
   (score.org never reads `unweighted`). Other metric_value flags are not copied (U04-67 step 9 lists only these).
3. `no_trend` is set whenever trend_slope is NULL (fewer than 6 distinct t, incl. no weekly rows).
   Distinct t is counted over all weekly points (design §5.8 sample SQL counts only `a.t`, which would miss the
   last week; U04-67 wording "fewer than 6 distinct t" followed).
4. Weekly rows: period='week', value NOT NULL, entity_type IN (team, org), scorecard metrics, period_start in
   [w0, w_end), w_end = date_trunc('week', as_of), w0 = w_end - 84 days; t = date_diff('week', w0, period_start).
5. `peer_fallback` is set on every row whose peer_group is the all-group (also when that row's x is NULL).
   Group stats are computed separately for every natural group and every all-group (all entities of the type).
6. DuckDB least/greatest skip NULLs, so clips are guarded: `CASE WHEN e IS NOT NULL THEN greatest(-5, least(5, e)) END`.
   trend_b is NULL when denom or slope is NULL; composite uses coalesce(trend_b, 0).
7. Coverage: composite NULL + low_coverage when covered = 0 (also when min_weight_coverage = 0, which would
   otherwise divide by zero) or covered/total < s_min_weight_coverage.
8. Determinism: sums/avg use `ORDER BY` inside the aggregate; final SELECT ordered by entity_type, entity_id, metric.
9. Org depth: `max(depth)` of metrics.org_closure rows of the org (root 0), as design §5.8 / U04-65.
10. team_bucket: correlated scalar subquery; min criticality 1-2 'hi', 3-4 'lo', anything else (NULL, 5, no mapping) 'none'.
11. observed_days(): exact line-1467 formula; with no non-excluded incidents least() ignores the NULL
    difference and the result is s_window_days (DuckDB semantics; noted for T04-14).
12. unconfirmed comes from the `unconfirmed` bind, overridden to false by run_org_step (U04-68).

## Concerns
- None blocking. T04-21 still has to wire `run_org_step` into scoring.STEPS and add the score.org checks.

## Fix round 1 (review T04-16-review.md; test-only)
Changed only tests/unit/metrics/test_metrics_org.py (template, org.py and macros untouched; no new bug found).
- Important 1: moved the out-of-window weekly points (-999 at w0 - 1 week, 999 at w_end) onto team B, which has
  5 in-window points; a leak at either bound gives 6 points and a non-NULL slope (asserted NULL + no_trend).
- Minor 2: new test_ut04_89_trend_weight_comes_from_config (trend_weight 2.0) and
  test_ut04_90_coverage_at_threshold_is_enough (covered/total == 0.5 == min_weight_coverage keeps the
  composite; 0.6 gives NULL + low_coverage). `_catalog` gained a `min_weight_coverage` keyword.
- Minor 3: team MX owns a crit-4 service and supports a crit-2 one -> 'team:crit_hi' (fallback run now uses
  min_peer_group=4 since the hi group has 3 teams).
- Minor 4: team D's MTTR row now has value 7.0 with flag insufficient_sample; asserts value/z_score NULL,
  the flag, and that B's z uses the group {1, 3, 2} only.
Mutants (applied one at a time by script, card tests run, file restored) — all KILLED:
`INTERVAL 84 DAY`->`91 DAY`; `period_start < w_end`->`<=`; `p('s_trend_weight')`->`0.5`;
`covered / total >=`->`>`; team_bucket `min(criticality)`->`max`; insufficient-sample guard on x removed.
Tests: card 17 passed; tests/unit/metrics 649 passed; ruff + mypy clean. Commit 9a45ce9 "test(metrics): tighten org score tests (T04-16 fix round 1)", hooks passed.
