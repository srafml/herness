# T04-16 review (verify agent) — Org score

Worktree agent-ab7f9ecd397f34037, head b1d2f74 (base e41d62c). Files: herness/metrics/org.py (40/110), herness/metrics/sql/org_score.sql.j2 (172/220), herness/metrics/sql/_macros.sql.j2 (154/260, +16 at EOF), tests/unit/metrics/test_metrics_org.py, tests/support/metrics_org_oracle.py.

Evidence gathered: test file run (15 passed; org.py 100 % line / 100 % branch); ruff check + format clean; project mypy clean (334 files); DuckDB 1.5.5 probes; 32 template/macro mutants run against the test file in a temp copy (C:\Users\santh\AppData\Local\Temp\t0416-verify\): 25 killed, 7 survived (listed below).

### Spec Compliance
- U04-67 org_score.sql.j2 ✅ — steps 1–9 match:
  - scorecard from `unnest(s_org_metrics, s_org_weights)`, sign +1 for `s_lower_better`, else −1 (:14-22)
  - entities are active `core.team` plus all `core.org`; org group is `'org:level' || max(depth)` (:23-35). The stage-400 closure includes the self row at depth 0.
  - x and sample_size at t12w/window_start_date; NULL when missing or insufficient; sample_size 0 when missing (:37-50)
  - fallback when the natural group has fewer than `s_min_peer_group` non-NULL x (:52-58)
  - median and unscaled `mad` (VI04-06 confirmed); denominators 1.4826 and 1.2533; NULL when there is no spread (:60-80)
  - z = 0 when x is not NULL and denom is NULL; z stored unclipped; b and trend_b clipped to [-5, 5] with a NULL guard. A probe confirmed that DuckDB `least`/`greatest` skip NULL, so the guard is needed (:8-10, :112-134).
  - Theil–Sen over [w_end−84d, w_end), with w_end = date_trunc('week', as_of); NULL below 6 distinct t; ×12/denom (:82-110)
  - coverage rule with low_coverage on every row of the entity (:136-149)
  - row_number rank per entity_type, ordered by composite DESC NULLS LAST, then entity_id (:151-158)
  - flags insufficient_sample, low_coverage, no_data, no_trend and peer_fallback; unconfirmed comes from the bind (:160-172)
  - Column order and types match U04-67; DESCRIBE is asserted in the tests.
  - The template is static: every runtime value goes through p(), and every p() name is a BIND_TYPES key. The literals that remain are spec constants. Rendered SQL is about 6.7k chars (limit 20k).
  - Division by zero gives inf/NaN in DuckDB 1.5.5 (probed). Every division is guarded: `covered > 0`, `denom IS NULL`, and `b.t > a.t`.
- U04-68 run_org_step ✅ — signature `(con, sc, /) -> StepResult` matches scoring.StepFn, and scoring.py is unchanged. The SchemaViolation text is exact. It makes one `run_recorded(..., "score", into=IntoSpec("score.org","replace","query_ids"))` call with `unconfirmed=False`. org.py does no arithmetic. The tests check the single evidence row, the 64-hex result_hash, and query_ids = [own].
- U04-35 additions ✅ — exactly two blocks at EOF; no existing macro changed. `team_bucket` returns 'hi' for minimum criticality 1–2 and 'lo' for 3–4, over owner/support mappings, and 'none' otherwise. `observed_days()` matches the line-1467 formula exactly.
- ⚠️ Cannot verify from the diff: the BT04-04 timing (< 20 s, levers included) belongs to the benchmark card. The T04-21 wiring is still pending (scoring.py:184).

### Strengths
- One self-contained SELECT. Aggregates are deterministic (ORDER BY inside sum/avg), and the final ORDER BY is deterministic too.
- The clip and NULL semantics were thought through. The test strength is mostly good: mutants of 1.4826, 1.2533, the clip bound, removing the clip, sign, z=0, the ≥6 threshold (both directions), ×12, the w0 bound, NULLS LAST/ASC, the peer threshold, scaled mad, avg in place of median, the no-guard composite, `mad_g >= 0`, sample_size coalesce, the active filter, min depth, covered, the fallback branch, the support role and the hi range were all killed.
- The PT04-07 oracle is independent: it uses Python `statistics.median` and its own MAD, mean absolute deviation and Theil–Sen. 59 of 150 generated series have ≥ 6 points, so the trend path is genuinely exercised.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. tests/unit/metrics/test_metrics_org.py:210-213 — the trend-window boundary assertions do not detect boundary errors. The comment says points outside [w0, w_end) are ignored. However, the out-of-window points (−999 at w0−1wk, 999 at w_end) are added to team A's 12-point series, and Theil–Sen's robustness absorbs them, so slope 2.0 survives either way. Confirmed by surviving mutants: `INTERVAL 84 DAY`→`91 DAY` and `period_start < w_end`→`<= w_end` both pass all 15 tests. Fix: put the out-of-window points on team B, which has 5 in-window points. Including either point would give 6 distinct t and a non-NULL slope, so `trend_slope is None` then pins both bounds.

#### Minor (Nice to Have)
2. test_metrics_org.py:53-67 — every test uses trend_weight 0.5 and min_weight_coverage 0.5, so hard-coding `0.5` in place of `p('s_trend_weight')` survives (org_score.sql.j2:140). A coverage exactly equal to the threshold is also never tested: `>=`→`>` survives (org_score.sql.j2:147). Add one case with trend_weight ≠ 0.5 and one with covered/total == s_min_weight_coverage.
3. test_metrics_org.py:306-333 — no team owns services of different criticality, so `min`→`max` in team_bucket survives (_macros.sql.j2:144). Add a team that owns crit 1 and crit 4 and expect 'hi'.
4. org_score.sql.j2:39-40 — NULLing x on the `insufficient_sample` flag is untested: removing it survives, because the test row already has value NULL (test_metrics_org.py:245). The metric wrapper already NULLs such values, so this is defence-in-depth. Add a row with a value and the flag, or accept it as is.
5. org_score.sql.j2:130 — the `z.denom IS NOT NULL` guard is redundant, since division by NULL is already NULL. It is harmless; a mutant removing it survives for that reason.
6. org_score.sql.j2:147 — spec deviation: `covered > 0` makes composite NULL and adds low_coverage when no metric is covered even if s_min_weight_coverage = 0. The spec formula would yield 0/0 there (NaN in DuckDB), so this choice is better. Record it as a spec note.
7. test_metrics_org.py:418 — PT04-07 hard-codes `max_examples=25`, which overrides the commit/nightly Hypothesis profiles. tests/unit/metrics/test_metrics_catalog_props.py:57 uses `min(settings().max_examples, cap)` instead.
8. _macros.sql.j2:151 (observed_days) — with no non-excluded incidents the result is s_window_days, because `least` skips the NULL. Behaviour follows the spec formula literally; flag it for T04-14.
9. Process: the builder reports no RED evidence (tests written after the code). The mutation run above stands in for it. Every claim in the report was verified.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation matches U04-67, U04-68 and U04-35 in every probed respect, and 25 of 32 mutants are killed. However, the UT04-89 trend-window test claims boundary coverage it does not have: two boundary mutants survive. This is a small test-only fix (Important 1). Minors 2–4 are cheap to fold into the same pass.

## Re-review round 1

Scope: commit 9a45ce9, base b1d2f74. Checked only Important 1 and Minors 2, 3 and 4.

- Diff scope: only tests/unit/metrics/test_metrics_org.py changed (+50/−11). No changes in herness/, the oracle or any other file.
- In the worktree, 17 tests pass and org.py still has 100 % line and branch coverage. ruff check and format are clean.
- I re-ran the targeted mutants in a temp copy (C:\Users\santh\AppData\Local\Temp\t0416-verify\mut_r1.py). The baseline passed 17 of 17, and every mutant is now caught:
  - Important 1: `INTERVAL 84 DAY`→`91 DAY` ✅ KILLED
  - Important 1: `period_start < w_end`→`<=` ✅ KILLED
  - Minor 2: `p('s_trend_weight')`→`0.5` ✅ KILLED (new test_ut04_89_trend_weight_comes_from_config)
  - Minor 2: coverage `>=`→`>` ✅ KILLED (new test_ut04_90_coverage_at_threshold_is_enough)
  - Minor 3: team_bucket `min`→`max` ✅ KILLED (team MX owns crit 4 and supports crit 2, so the expected bucket is hi)
  - Minor 4: insufficient_sample x guard removed ✅ KILLED (row D now has value 7.0 with the flag)
- The new tests follow the naming rules: ID in the function name and docstring.

Minors 5–9 were not in scope. They remain notes and do not block.

**Verdict: Approved**
