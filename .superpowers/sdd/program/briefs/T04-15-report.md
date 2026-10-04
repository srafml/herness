# T04-15 Funding score — build report

Worktree: D:\herness\.claude\worktrees\agent-af699be9d7c4f68f4 (branch worktree-agent-af699be9d7c4f68f4, base a51221f)
Final commit: 6999581 feat(metrics): T04-15 funding score

## What was built
- `herness/metrics/sql/funding_score.sql.j2` (241 lines, budget 300): U04-65 as ONE SELECT.
  CTEs: od (observed_days() once), attr, cf / cf_owner (cluster-fix candidates from this build's
  attribution; owning service = most frequent service of the cluster's non-excluded in-window
  incidents, ties lowest ID), cand (work-item candidates + cluster-fix), fam (k + candidate
  descendants), r = R(k), agg (pain, pain*quality, incident share sum), port (deepest initiative
  ancestor-or-self key, arg_max by depth), pts (remaining subtree points), svc (services(k)), rr,
  w0/rec_ts/weekly/series/slope (Theil-Sen over 12 complete weeks, missing weeks 0), p1 (P1 in last
  30 days), base, scored, inputs, ranks (percent_rank over non-NULL inputs), fibs, final SELECT
  with rank row_number and flags. Template-local macros only (in_window, prank, fib, dbl); uses
  shared p(), lkp(), observed_days(); _macros.sql.j2 NOT modified.
  Only literals are spec constants (365, 180, 30.0, 0.05, fib breakpoints/values, 84 days =
  12 weeks, range(12), 30 days, criticality 5); all weights/thresholds via p() binds.
- `herness/metrics/funding.py` (55 lines, budget 150): U04-66 steps 3-4 at the T04-15 marker.
  New public FUNDING_TABLE = "score.funding", NO_CANDIDATES = "no funding candidates"; private
  `_store(con, sc, name, binds, into)` renders + run_recorded for both templates.
  score stored with IntoSpec("score.funding", "replace", "query_ids", (attribution.query_id,));
  row_counts {score.funding_attribution: n, score.funding: m}; warnings ["no funding candidates"]
  when m == 0. Signature unchanged (con, sc, /) -> StepResult. scoring.py untouched.
- Tests: new `tests/unit/metrics/test_metrics_funding_score.py` (473 lines, 15 tests: UT04-80 x3,
  UT04-81, UT04-82 x2, UT04-83, UT04-84 x4 (rank ties, step/evidence/query_ids, empty warning +
  DESCRIBE schema, binds/size), UT04-85 x2 (hand-computed WSJF incl. Theil-Sen +/-, P1 step, RR,
  JS; fib breakpoints .15/.30/.45/.60/.75/.90 on 21 candidates), UT04-87);
  PT04-06 added to `test_metrics_funding_props.py` (same graphs() strategy + random history
  1..800 days + 0..40 extra direct incidents; commit profile capped at 25 per T04-14 precedent;
  also asserts rank is 1..n). Support `tests/support/metrics_funding.py` extended additively
  (item kwargs project/team/estimate/points, SCORE_COLUMNS, cluster_fix_weights(), scores()).
  T04-14 UT04-75 step test updated: row_counts now also include score.funding (2).
- ST04-04 shipped-template scan globs every *.sql.j2, so funding_score is scanned automatically
  (passes; no allowance needed).

## Rendered SQL length
render_named("funding_score") 10,732 chars raw; 9,541 normalized (cap 20,000).

## Spec notes / decisions
1. portfolio(k) uses ancestor-or-self (depth >= 0): an initiative with no initiative ancestor is
   its own portfolio (design "top initiative ancestor"); max depth = top-most.
2. services(k) for a cluster fix (RR criticality term) = its owning service (same rule as org(k)).
3. addressable = CAST(stored annual_pain_usd x expected_reduction): rounded annual is used, so
   the stored columns recompute; DECIMAL cast rounds half away from zero (UT04-80 pins 5069.45).
4. priority / bv use the stored DECIMAL addressable and effort cast to DOUBLE.
5. Effort chain via coalesce(estimate, points x hour x hours/point, cluster-fix hours x hour);
   effort 0 gives priority NULL but no no_estimate flag (flag only when NULL).
6. TC weeks: week starts in [date_trunc('week', as_of) - 84 days, date_trunc('week', as_of)),
   local week of the record timestamp in tz; t = 0..11; slope unit = USD per week.
7. "R(k) has a P1 opened in the last 30 days": [as_of_ts - 30 days, as_of_ts).
8. percent_rank partitioned by (input IS NULL) so NULL inputs (JS for NULL effort) neither rank
   nor shift others; fib(NULL) = NULL so wsjf is NULL when effort NULL.
9. Self-referencing 'blocks' links are not excluded (B ranges over all candidates per spec).

## Evidence
RED: `PYTHONUTF8=1 uv run pytest tests/unit/metrics/test_metrics_funding_score.py` first ->
ImportError FUNDING_TABLE (collection error); after funding.py step 3-4 -> 14 failed with
`ConfigError: template render failed: TemplateNotFound: funding_score.sql.j2`.
First green run caught one test expectation bug (Python quantize half-even vs DuckDB half-up for
addressable 5069.445) — test fixed to the spec's CAST behaviour.
GREEN: funding_score + funding + catalog tests 113 passed; props 2 passed (PT04-06 stats over 25
examples: confidence interior 76 %, lower clamp 64 %; upper clamp covered by UT04-82 Z = 1.0);
`pytest tests/unit/metrics` 661 passed (8:21).
Coverage herness/metrics/funding.py: 100 % line, 100 % branch.
Gates: ruff format/check clean; mypy (project) 0 issues in 337 files; check_module_size exit 0.
First commit attempt: all hooks passed except mixed-line-ending (template written with CRLF;
hook normalized it to LF) -> re-committed.

## Deviations
- T04-14 test test_ut04_75_run_funding_step_records_evidence: row_counts assertion extended with
  score.funding (required by U04-66 step 4).
- No separate wip commit landed (first attempt rejected by mixed-line-ending hook); the card
  lands as one commit.

## Concerns
- None blocking. PT04-06 commit profile does not reach the upper clamp 1.0 (needs >= 30
  incidents, full history and c_map = 1); hand test UT04-82 covers it.

## Final commit
6999581 feat(metrics): T04-15 funding score. Every pre-commit hook passed, including mixed-line-ending and pytest-unit (full unit suite). No --no-verify, no SKIP, PRE_COMMIT_ALLOW_NO_CONFIG not set.

## Fix round 1 (review briefs/T04-15-review.md: I1 a-h, M2, M3; M1 parked)

Template (funding_score.sql.j2, 241 -> 244 lines, budget 300):
- M2: r carries pain_usd (DECIMAL) as well as its DOUBLE copy. agg.pain_dec = sum(pain_usd) is an exact
  DECIMAL sum, and annual_pain_usd = CAST(pain_dec * 365 / od.d AS DECIMAL(18,2)). The DOUBLE pain stays
  for c_map (pain*quality) and the weekly Theil-Sen series.
- M3: the P1 step ladder ends `WHEN 13 THEN 20 WHEN 20 THEN 20 END` without ELSE, so a NULL fib stays NULL.
- Rendered SQL now 10,847 chars raw, 9,623 normalized (cap 20,000).

Tests (test_metrics_funding_score.py, 473 -> 608 lines, 15 -> 20 tests):
- a. test_ut04_84_rank_confidence_breaks_ties: three features with estimates. Priority is 0 for all three
  and addressable pain is 0.00 (annual 0.00/0.01/0.02 x 0.15), with different confidence. Rank follows
  confidence DESC, which is the reverse of the ID order.
- b. test_ut04_85_p1_step_cap_and_window covers three cases:
  - U is the TC top (fib 20) with a P1 in the window and stays 20.
  - V has a P1 31 days before as_of and gets no step.
  - W has a P1 29 days before as_of and steps 1 -> 2.
  The exact wsjf values are asserted.
- c. UT04-81 now asserts the wsjf of A, B and the cluster fix. JS ranks only the 3 non-NULL efforts
  (1, 5, 20), and the NULL-effort candidates C, D and E do not shift them.
- d. test_ut04_83_cluster_fix_owner_tie_lowest_service: the cluster is split 1/1 over S1 (O1 1.6) and S2
  (O3 0.8). The weight is 1.6, so S1, the lowest ID, wins.
- e. UT04-83 adds E6 with team T3 (O3) and service S1 (O1). Its weight is 0.5 (clip of 0.2), so the team
  org wins.
- f. test_ut04_80_both_flags_sorted: flags == ["no_estimate", "short_history"].
- g. test_ut04_84_zero_effort_priority_null: estimate 0 gives effort 0.00, priority None and flags [].
- h. test_ut04_85_blocks_direction: P blocks Q and R, and Q blocks R, so the RR inputs are 2, 1 and 0.
  Counting either side of the link the wrong way changes the wsjf. test_ut04_80_expected_reduction_override
  asserts the values per type: initiative 0.2, epic 0.3, feature 0.15 (shipped), cluster_fix 0.45, and
  the override 0.6 by key.
- M2 assertion: UT04-82 split test asserts annual 13333.33 (= 10000.00 + 3333.33 exact cents).

Mutation re-check (the template was mutated, then test_metrics_funding_score.py run with -x; the template
was restored byte-exact, md5 unchanged). All 11 targeted mutants KILLED:
- confidence DESC->ASC
- P1 ladder 20->21
- to_days(30)->300
- PARTITION BY x IS NULL removed
- owner tie service_id DESC
- org coalesce swapped
- flags order swapped
- `OR effort = 0` removed
- blocks joined on from_key
- blocks from_key->to_key
- er feature bind -> epic bind

The first version of the h fixture let the "b.key = l.from_key" mutant survive (it counts k itself).
Adding the Q -> R link fixed that.

Spec notes (sub-controller rulings):
- W1: portfolio(k) is computed over ancestor-or-self. A top-level initiative's portfolio is its own key
  (design §5.5 "top initiative ancestor"); UT04-83 pins it.
- W2: services(k) for a cluster fix (the RR criticality term) is its owning service.
- W3: a self-referencing 'blocks' link (from_key = to_key = k.key) counts; the spec text allows it.

GREEN: test_metrics_funding_score.py 20 passed; `pytest tests/unit/metrics` 667 passed (8:26).
Coverage funding.py 100 % line / 100 % branch. ruff format/check clean; check_module_size exit 0.
Files written with LF endings.

Fix round 1 commit: f6b9385 fix(metrics): T04-15 review round 1 (decimal annual sum, P1 ladder, rule tests). All hooks passed incl. pytest-unit; no SKIP, no --no-verify.
