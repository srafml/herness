# T04-15 review (Funding score) - verify agent

Worktree agent-af699be9d7c4f68f4, a51221f..6999581. Tree left clean (`git status` empty after mutation runs).

### Spec Compliance
- ✅ Spec compliant (implementation). I found no behavioural defect in `funding_score.sql.j2` or `funding.py`.
- ⚠️ Cannot verify / needs a controller ruling: see W1-W3 below.

U04-66 `run_funding_step` (herness/metrics/funding.py:24-55)
- ✅ step 1: binds = sc.binds() + `unconfirmed = bool(unconfirmed_blocks(sc.weights, WEIGHT_USES["funding"]))`.
- ✅ step 2: attribution via `run_recorded(..., "score", into=IntoSpec("score.funding_attribution","replace","query_id"))`.
- ✅ step 3: ONE recorded SELECT `funding_score` with `IntoSpec("score.funding","replace","query_ids",(attribution.query_id,))`. Same `run_recorded` path, so it gets the shared result_hash and the evidence row. UT04-84 checks the evidence row (producer score, row_count 5, '"name":"funding_score"' in params) and `query_ids == [own, attribution]`.
- ✅ step 4: row_counts for both tables; warning "no funding candidates" when empty (UT04-84 empty test).
- ✅ StepFn signature `(con, sc, /) -> StepResult` is unchanged. scoring.py is untouched and so is _macros.sql.j2 (diff stat). No Python arithmetic on scores.
- ✅ Budgets: template 241/300 lines, funding.py 55/150 lines; `tools.check_module_size` exit 0. Rendered SQL is ~10.7k chars, under the 20k Evidence cap (asserted in tests).

U04-65 algorithm (herness/metrics/sql/funding_score.sql.j2)
- ✅ Candidates: work items initiative/epic/feature in todo/in_progress (same as U04-64 `cand`) plus cluster-fix IDs from this build's attribution (:27-53). Pain-free candidates still get a row (agg LEFT JOIN, coalesce 0); priority is 0 when effort is non-NULL.
- ✅ R(k): fam = self + cand_desc (closure depth>=1, both ends candidates) (:55-69). Pain comes only from score.funding_attribution (this build's table).
- ✅ 1 annual = CAST(Σpain×365/observed_days() AS DECIMAL(18,2)) (:175). See M2 on the DOUBLE sum.
- ✅ 2 expected_reduction = lkp(override_k, override_v, key, type value) (:165-177). Cluster-fix key NULL falls to w_er_cluster_fix.
- ✅ 3 addressable = CAST(annual×er) (:202). Builder note 3 (rounded annual) accepted.
- ✅ 4 n_incidents = CAST(round(Σshare over incident rows)) (:74, :178).
- ✅ 5 c_map (0 when Σpain=0), c_sample = least(1, sqrt(n/30.0)), c_hist = least(1, od/365.0), and the clamp greatest(0.05, least(1.0, ...)) (:179, :203-204).
- ✅ 6 strategic weight = least(clip_max, greatest(clip_min, lkp portfolio × lkp org)) with w_sw_default (:168-181). Org is team org, else service org. Cluster fix: portfolio NULL and org of the owning service (most frequent in-window non-excluded incident service, ties lowest ID, :34-42). See W1 on portfolio.
- ✅ 7 effort chain: estimate, then remaining subtree points (status <> done, NULL when none) × hour × hours/point, then cluster-fix hours × hour, else NULL with `no_estimate`. Cast to DECIMAL(18,2) (:182-186, :239).
- ✅ 8 priority NULL when effort is NULL or 0 (:209-211).
- ✅ 9 WSJF details:
  - The fib map matches the breakpoints.
  - percent_rank runs over non-NULL inputs only (PARTITION BY x IS NULL) (:12-18).
  - BV input = addressable × sw. JS input = effort.
  - TC Theil-Sen runs over t=0..11 from date_trunc('week', as_of)-84 days, using the local week (AT TIME ZONE tz, TIMESTAMPTZ columns) of opened_at/ts/actual_end. Missing weeks = 0. The slope is the median of pairwise slopes (:119-157).
  - P1 step: the ladder 1→2→3→5→8→13→20, capped at 20, applies to a P1 incident in R(k) opened in [as_of_ts-30d, as_of_ts) (:158-164, :225-227).
  - RR = distinct candidate B blocked (from_key = k.key, to_key = B.key) + 5 - coalesce(min criticality of services(k), 5). services(k) is the same as U04-64 cand_services (:95-117).
  - wsjf is NULL when effort is NULL (fib(NULL) = NULL).
- ✅ 10 rank uses row_number() with the exact ORDER BY (priority DESC NULLS LAST, addressable DESC, confidence DESC, candidate_id ASC), 1..n without gaps (:234-236).
- ✅ 11 title = key, or candidate_id for cluster fix (TH04-04, DD04-12) (:174).
- ✅ 12 unconfirmed comes from the bind. flags are a sorted subset ('no_estimate' before 'short_history'), with short_history when observed_days < 180 (:237-240).
- ✅ Weights and thresholds come only from p()/lkp() binds. UT04-84 pins the exact bind set. The literals are the spec's own constants (365, 365.0, 30.0, 0.05, 1.0, fib breakpoints and values, 84 = 12 weeks, 30 days, 180, criticality 5).
- ✅ Output columns, order and types match `score.funding` (DESCRIBE asserted in UT04-84).

Tests: every ID is present with the ID in the function name and the docstring: UT04-80 (×3), UT04-81, UT04-82 (×2), UT04-83, UT04-84 (×4), UT04-85 (×2), UT04-87, and PT04-06 (props file). Each file sets `pytestmark = unit`.

⚠️ items (spec interpretations; implementation acceptable, ruling or DECISIONS note wanted)
- W1 portfolio(k) (template :80-86, builder note 1): the code uses ancestor-or-self, so a top-level initiative candidate's portfolio is its own key. A literal reading of "deepest initiative *ancestor* ... else project" would give the top initiative its `project`. UT04-83 pins the builder's reading (TOP → 1.5). The design §5.5 intent ("key of top initiative ancestor") supports the builder's reading. Record it as a spec note.
- W2 services(k) for a cluster fix (RR criticality term) = its owning service (builder note 2). The spec does not define this case. Reasonable.
- W3 a self-referencing 'blocks' link (from_key = to_key = k.key) counts k as blocking itself (builder note 9). The spec literally allows it. Harmless; note only.

Builder spec notes 1-9 are all judged acceptable (1 → W1, 2 → W2, 9 → W3; 3-8 consistent with the spec text).

### Strengths
- The whole score is one static, recorded SELECT. Template-local macros keep _macros.sql.j2 untouched, and there are no SQL comments in the recorded SQL.
- The hand-computed tests are strong for:
  - annualization incl. the half-up DECIMAL cast
  - confidence
  - the effort chain
  - Theil-Sen (rising, falling, single-week, missing-week fill)
  - fib breakpoints at exactly k/20
  - cand_desc roll-up
  - the rank tie on candidate_id and NULLS LAST
- funding.py is minimal and fully covered (100% line/branch), with a clean `_store` helper.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- I1 Test gaps: several spec rules that the §11 rows (UT04-84 "rank per design ties", UT04-85 "P1 step") and the review focus name explicitly are not pinned. 12 of 38 template mutations survive. Discounting 3 equivalent or near-equivalent mutants (upper clamp 1.0→2.0, RR constant 5→4, c_map pain=0→1), 9 real survivors remain. All fixes are test-side in tests/unit/metrics/test_metrics_funding_score.py; the implementation looks correct.
  - a. Rank third key `confidence DESC` (template :235) is untested. test_ut04_84_priority_and_rank_ties (test :300) has no pair that ties on priority and addressable but differs in confidence.
    - Fix: add two candidates with equal priority and equal addressable but different confidence. The simplest is two pain-free candidates with estimates (priority 0, addressable 0) whose confidence differs. Another option is a pair whose priority/addressable tie while n_incidents differ.
  - b. P1 step cap and window are unpinned (test_ut04_85_fibonacci_mapping_p1_step_and_wsjf, test :397). The mutations "ELSE 20 → 21" (P1 on fib 20, template :227) and "to_days(30) → to_days(300)" (:163) survive.
    - Fix: give the TC-top candidate (B, fib 20) a P1 in the last 30 days and assert TC stays 20.
    - Fix: give another candidate a P1 opened 31-60 days before as_of (inside the 12 weeks) and assert no step.
  - c. "percent_rank over candidates with non-NULL input" is unpinned. Removing `PARTITION BY x IS NULL` (template :13) survives.
    - Fix: assert the wsjf values of estimated candidates in a run where other candidates have NULL effort. For example, in UT04-81 (test :135) assert A/B/cluster-fix wsjf; their JS ranks must ignore C/D/E.
  - d. Cluster-fix owning-service tie rule (lowest ID) is unpinned. The mutation `f.service_id DESC` at template :41 survives.
    - Fix: build a 2-incident cluster split 1/1 across services of different orgs/weights, and assert the strategic weight equals the lower service ID's org weight.
  - e. Org precedence "team org, else service org" is unpinned. Swapping the coalesce at template :170 survives.
    - Fix: in UT04-83 (test :245), add an item with both a team (org O3) and a service (org O1), and assert the team's org wins.
  - f. Sorted flags with both flags present are unpinned. Swapping the list_concat order (template :238-240) survives.
    - Fix: add a no-estimate candidate to the UT04-80 179-day case and assert `["no_estimate", "short_history"]`.
  - g. Priority NULL when effort = 0 is unpinned. Dropping `OR s.effort_cost_usd = 0` (template :209) survives.
    - Fix: add a candidate with estimate=0 and assert priority None and flags [] (builder note 5).
  - h. RR `blocks` direction and the expected-reduction `feature` value are weakly pinned.
    - Swapping from_key/to_key (template :110) survives, because the RR ordinal rank is unchanged.
    - The er-type bind swaps were caught only by the bind-set assertion, not by values; feature 0.15 is never asserted.
    - Fix: assert a feature candidate's expected_reduction (0.15).
    - Fix: make the RR fixture rank-sensitive to direction. Example: two blockers with different counts, or a blocked candidate that would gain counts under the swap.

#### Minor (Nice to Have)
- M1 PT04-06 commit profile never reaches the upper clamp (builder concern). The clamp at 1.0 is mathematically unreachable anyway, since each factor is ≤ 1 and quality is ≤ 1, and UT04-82 Z covers confidence = 1.0. No action.
- M2 annual_pain_usd sums `CAST(pain_usd AS DOUBLE)` (template :66, :72, :175) instead of the spec's `Σ pain_usd` over DECIMAL. Parallel DOUBLE-sum order can in rare cases flip a half-cent at the DECIMAL(18,2) cast, which would change result_hash or fail the Verifier tolerance for small values.
  - Fix: carry `a.pain_usd` (DECIMAL) through `r`/`agg` as an exact sum for the annual (`CAST(sum_dec * 365 / od.d AS DECIMAL(18,2))`). Use the DOUBLE copy only for pq/c_map and the weekly series.
- M3 The TC P1 ladder (template :226-227) maps a NULL fib to 20 via ELSE. tc_in is never NULL today, because every candidate has 12 series rows.
  - Fix: `... WHEN 13 THEN 20 WHEN 20 THEN 20 END`, so NULL stays NULL, is more robust.

### Mutation results (template only; each run = test_metrics_funding_score.py -x; template restored byte-exact)
Caught 26/38:
- fib .15 ≤→<
- fib .90→.95
- clamp 0.05→0.04
- annual 365→366
- c_hist 365→360
- c_sample 30→25
- rank id ASC→DESC
- rank addressable DESC→ASC
- NULLS LAST→FIRST
- P1 step 2→3 removed
- missing weeks not filled
- week start -84→-77
- short_history <180→≤180
- er feature→initiative bind (bind-set test only)
- er initiative→epic bind (bind-set test only)
- round→ceil
- cand_desc dropped
- portfolio arg_max→arg_min
- portfolio else-project dropped
- clip min dropped
- clip max dropped
- points include done
- cluster-fix effort dropped
- title key→candidate_id
- median→avg
- P1 priority 1→2

Survived 12:
- upper clamp 1.0→2.0 (equivalent)
- rank confidence DESC→ASC (I1a)
- P1 cap 20→21 (I1b)
- P1 window 30→300 days (I1b)
- flags order swap (I1f)
- org coalesce swap (I1e)
- effort=0 guard removed (I1g)
- RR criticality 5→4 (equivalent under percent_rank)
- RR blocks direction swap (I1h)
- cluster-fix owner tie DESC (I1d)
- percent_rank NULL partition removed (I1c)
- c_map pain=0→1 (near-equivalent: n_incidents = 0 then)

### Test run (verify)
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics/test_metrics_funding_score.py tests/unit/metrics/test_metrics_funding.py tests/unit/metrics/test_metrics_funding_props.py -q -p no:logging --cov=herness.metrics.funding --cov-branch`: 29 passed in 71 s. herness/metrics/funding.py coverage is 100% line and 100% branch.
- `python -m tools.check_module_size`: exit 0.
- Builder evidence is recorded in the report: RED (ImportError FUNDING_TABLE, then TemplateNotFound), then GREEN (funding 113 passed, props 2 passed, tests/unit/metrics 661 passed). The half-even vs half-up test-expectation fix is explained.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation matches U04-65/U04-66 step by step and stores one recorded SELECT with the right upstream query_ids. Several spec rules named by UT04-84/UT04-85 and the focus list are unpinned by tests (I1 a-h, test-side only). M2 (exact DECIMAL sum for annual pain) is a cheap determinism hardening worth folding into the same round.


## Re-review round 1 (f6b9385; scope I1 a-h, M2, M3)

Fix check:
- ✅ a. Confidence tie key is pinned by test_ut04_84_rank_confidence_breaks_ties.
- ✅ b. P1 cap and 30-day window are pinned by test_ut04_85_p1_step_cap_and_window.
- ✅ c. Non-NULL percent_rank is pinned by the UT04-81 wsjf asserts.
- ✅ d. Owner tie (lowest ID) is pinned by test_ut04_83_cluster_fix_owner_tie_lowest_service.
- ✅ e. Team org over service org is pinned by UT04-83 E6.
- ✅ f. Sorted flags are pinned by test_ut04_80_both_flags_sorted.
- ✅ g. Effort 0 giving priority NULL is pinned by test_ut04_84_zero_effort_priority_null.
- ✅ h. Blocks direction is pinned by test_ut04_85_blocks_direction. Expected reduction per type (incl. feature 0.15) is pinned in test_ut04_80_expected_reduction_override.
- ✅ M2 (code): annual = CAST(Σ pain_usd DECIMAL × 365 / od). Not distinguishable by a deterministic test: the 13333.33 assert also passes with a DOUBLE sum (the mutant survives). Accepted, since it is a determinism hardening.
- ✅ M3: the ladder ends `WHEN 13 THEN 20 WHEN 20 THEN 20 END`, with no ELSE.

Mutation re-run (43 mutants, template restored byte-exact, md5 f3754540... unchanged): 37/43 caught. All 9 real survivors from round 0 are now caught.

Remaining survivors:
- Equivalent:
  - upper clamp 1.0 → 2.0
  - RR constant 5 → 4
  - c_map pain=0 → 1
  - P1 NULL → ELSE 20 (tc_pr is never NULL)
- Not pinned:
  - DECIMAL → DOUBLE annual sum (M2, see above)
  - P1 rung 13 → 20. New Minor: no test steps a fib-13 TC. Optional: add a P1 to a candidate whose TC maps to 13.

Test run: my run of the three funding files was contaminated. A concurrent mutation script (C:/Users/santh/AppData/Local/Temp/w25-s04/mut_r1.py, PID 69468, started 02:44:33, apparently the sub-controller's) mutated funding_score.sql.j2 in this worktree during my pytest run. Result: 34 passed, 1 failed (PT04-06, consistent with its live "least(2.0" clamp mutant). funding.py coverage was 100% line/branch. I did not touch the file while that process ran.

The builder reports test_metrics_funding_score.py 20 passed and tests/unit/metrics 667 passed. **Re-run the three files once mut_r1.py has exited and git status is clean.**

**Verdict: Approved** (conditional on that clean re-run of the three funding files). Remaining: one Minor (P1 13→20 rung unpinned, optional).
