# T04-18 review: Action levers (`score.action_lever`)

Verifier: verify agent, worktree `agent-a73d386da299b6d89`, base de4f9ca, head bf07142.
Head bf07142 is an empty marker commit; all content is in wip 334cde5. That is acceptable under the process and noted only for the record.

**Verdict: Needs fixes.** The code matches the spec and both hand-checked numbers agree. The fix is to the tests: the UT04-99 skip tests for `rank > s_top_entities` and `z ≤ 0` pass whether or not those filters exist (one Important finding). No production code change is needed.

### Spec Compliance
- ✅ U04-69 `levers.sql.j2`: all 8 algorithm steps are implemented as written (details below).
- ✅ U04-70 `run_levers_step(con, sc, /) -> StepResult` matches the StepFn shape. One `run_recorded` call with producer `"score"` and `IntoSpec("score.action_lever", "replace", "query_ids", (org_qid,))`. The org query ID comes from `score.org.query_ids[1]` via `min(...)`. A missing table raises `SchemaViolation("score.org missing; run step org first")`.
- ✅ U04-71: `USD_MODELS` is in spec order. `LEVER_PLACEHOLDERS` is re-exported from `_catalog_checks` as a single source; it cannot live in `levers.py` because of an import cycle, and that reason is documented at `_catalog_checks.py:17`.
- ✅ Files that must stay untouched are untouched: `scoring.py` still has `STEPS["levers"] = None  # T04-21: ...` (`scoring.py:185`). `render.py`, `_macros.sql.j2` and `_binds.py` are unchanged; the diff touches only 4 files.
- ✅ Every stored number comes from one recorded SELECT. Python only reads the upstream query ID (`levers.py:28`, not stored as a value) and the row count.
- ✅ No hidden constants. Weights, thresholds, models, templates and units are all `p()` binds. The only literals are the spec constants `365`, `60`, `0.25`/`0.75`, `INTERVAL 12 MONTH`, `'t12w'` and `'other'`, plus the target-kind and entity-type strings.
- ⚠️ Cannot verify from the diff: wiring into STEPS. This is deferred to T04-21 by design.

#### Per unit / test row
| Row | Status | Notes |
|---|---|---|
| U04-69 step 1 (rank, lever metric, z>0, value not NULL) | ✅ code / ❌ tests | Filters are present (`levers.sql.j2:31-33`). The rank and z>0 filters are not tested by behaviour (see I-1). |
| U04-69 step 2 (median / quartile, improving only) | ✅ | Groups by (entity_type, peer_group, metric). Equivalent to the spec because peer_group strings carry a type prefix. |
| U04-69 step 3 (N, sums, D, F, CC, E) | ✅ | Rulings on the builder's readings are below. |
| U04-69 step 4 (deltas per model) | ✅ | Matches the spec text term by term (`:130-145`). |
| U04-69 steps 5-8 | ✅ | `observed_days()` macro reused; `lkp` used for the template and the unit; json keys exactly the placeholder set; `list_contains(s_unconfirmed_models, model)`. |
| U04-70 | ✅ | |
| U04-71 | ✅ | |
| UT04-92 mttr | ✅ | 60600.00 / 90900.00. Also covers the org entity through the closure and the x=0 skip (mutation M09 caught). |
| UT04-93 repeat | ✅ | 37875.00 / 56812.50 |
| UT04-94 reopen | ✅ | 187.50 / 281.25 |
| UT04-95 reassign | ✅ | 187.50 / 281.25 |
| UT04-96 sla | ✅ | 3750.00 / 5625.00 with penalty 1000 |
| UT04-97 cfr | ✅ | 20250.00 / 30375.00. The F=0 variant gives 1000.00 / 1500.00. |
| UT04-98 noise | ✅ | 25.00 / 37.50 |
| UT04-99 | ⚠️ partially vacuous | Covered by behaviour: target not improving, penalty 0, unconfirmed per model, missing table, query error, query_ids/evidence, empty table types, bind set. Not covered by behaviour: rank > top and z ≤ 0 (I-1). |
| UT04-100 | ✅ | Keys equal `LEVER_PLACEHOLDERS` across 14 rows (mutation M02 caught). The template comes from config (M03 caught). |
| Conventions | ✅ | Every test name carries `ut04_9x` / `ut04_100`. Each docstring's first line starts with the ID. `pytestmark = pytest.mark.unit` is set. |

### Hand computations (independent)
Test data: team T1 / org O1, service S1 (criticality 1). Weights are the tiny weights: downtime $10000/h for criticality 1, P1 multiplier 1.0, engineer $100/h, effort_factor[1] 1.5, backout 4 h, triage 3 min. as_of is 2026-04-01 and the window is [2025-04-01, 2026-04-01).

- **Incidents.** I1 (2026-01-18), I2 (2026-02-01) and I3 (2026-03-01) are all P1 with 60 impact minutes, so each has downtime = 1 h × 10000 × 1.0 = 10000. Σdowntime = 30000.
  - Toil exists only on I2: resolve_bh = 7200 s = 2 h, toil_h = min(2 × 1.5, 40) = 3, toil_usd = 300. Σtoil = 300, N = 3.
  - I8 (canceled) is outside the window anyway. I9 belongs to T9, whose org is NULL.
- **observed_days.** The earliest non-excluded incident opened on 2026-01-18; 2026-04-01 − 2026-01-18 = 14 + 28 + 31 = 73 days. least(365, 73) = 73, so the annualization factor is 365 / 73 = 5.
- **Targets.**
  - mttr: values (2, 4, 6, 8, 10). Median = 6. quantile_cont 0.25 sits at position 0.25 × 4 = 1, which is 4.
  - Rates: values (0.0625, 0.125, 0.25, 0.375, 0.5) with x = 0.5. Median = 0.25. q25 = 0.125.
- **mttr** (x = 10):
  - peer_median: (30000 + 300) × (10 − 6) / 10 = 12120; × 5 = **60600.00** ✓
  - top_quartile: 30300 × 0.6 = 18180; × 5 = **90900.00** ✓
- **cfr.** In-window changes are C1 (failed: it caused I3), C2 (successful) and C3 (backed_out, so failed). C4 was canceled and not deployed; C5 is from 2025-01-10, before the window. So D = 3 and F = 2.
  - CC = total_usd(I3) = 10000, so CC / F = 5000. Backout = 4 × 100 = 400.
  - peer_median: 3 × 0.25 × (400 + 5000) = 4050; × 5 = **20250.00** ✓
  - top_quartile: 3 × 0.375 × 5400 = 6075; × 5 = **30375.00** ✓
- **noise** (cross-check): E = 4 (E1-E4 are major; E5 is info; E6 is out of window). 4 × 0.25 × 3 / 60 × 100 = 5; × 5 = **25.00** ✓

### Rulings on the builder's spec readings
1. **CC is windowed on the change side only (by `actual_end`), not also on incident `opened_at`. Agree, with a caveat.**
   - Agree because "pairs as metric #17" names the pair sources: `caused_by_change_id` ∪ links with score ≥ `d_change_link_min_score`. The F changes are the population that the per-failed-change average divides by, so the numerator must be the incidents of exactly those changes.
   - Caveat: step 3 says "Base quantities over [`as_of_ts − 12 MONTH`, `as_of_ts`)". Today an incident opened at or after `as_of_ts` that was caused by an in-window change still counts. In a backdated build that is information from after `as_of`.
   - Recommend adding at least `f.opened_at < as_of_ts` (M-1). No test pins either reading.
2. **cfr uses CC / F. Agree.** Design §5.9 says "avg total_usd of change-caused incidents per failed change". U04-69 writes this as "CC / F when F > 0, else 0", and the impl spec is binding.
3. **noise E counts every in-window event with a noise severity; funding counts only events with `incident_id IS NULL`. Agree; they need not match.**
   - x for noise is `alert_noise_ratio` = unlinked / all noise-severity events (metric #12). The ratio's denominator is all such events, so E × (x − t) is the number of unlinked alerts avoided. That only works if E is the full denominator population, which is also exactly what U04-69 says ("events with severity in `d_noise_severities`").
   - Funding (U04-64 step 5, design §5.3 `noise_usd` "for each unlinked noise event") prices the unlinked events that actually occurred. It measures a different quantity.
4. **An empty `score.org` gives `query_ids` with only the levers query ID. Agree.**
   - An empty `score.org` has no readable org ID, and a lever needs `score.org` rows. So the table is empty and no row carries `query_ids`; the only loss is provenance on the evidence row.
   - The alternative, raising, is not in U04-70's preconditions (it only requires that the table exists).
5. **A `QueryError` is wrapped as `SchemaViolation("levers failed: ...")`. Agree.** U04-70's Errors field lists only `SchemaViolation`, and this mirrors `run_org_step` (`org.py:37-39`).

### Strengths
- The SQL is tight and readable. All values are binds; no prose leaks into the recorded SQL. Money sums stay DECIMAL until the delta arithmetic.
- TH04-10 holds two ways: the lever-model filter, and the model `CASE` that returns NULL. Removing the filter and also adding an `ELSE` to the `CASE` together is caught (M33).
- The hand-computed fixture runs through the real stage-400 facts, which is strong evidence for the 14 numeric expectations.
- Gates are clean (see Gates below).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I-1. The UT04-99 tests for the "rank > top_entities" and "z ≤ 0" skip rules pass whether or not those filters exist.**
  - Where: `tests/unit/metrics/test_metrics_levers.py:294-320`. Every entity other than T1 (T2, T3, ...) owns no incidents, changes or events. So the delta is 0 whether or not `levers.sql.j2:31` (`o.rank <= s_top_entities`) and `:33` (`o.z_score > 0`) filter it, and `WHERE u.delta_usd > 0` drops the row either way.
  - Mutation evidence:
    - M06 (drop `z_score > 0`) survived.
    - M34 (replace the rank filter with a tautology while keeping the bind) survived.
    - M08 (delete the rank filter) was caught only by the bind-set assertion in `test_ut04_99_rendered_sql_binds_and_cap`, not by behaviour.
  - Rank > top is a spec-listed UT04-99 setup, and the docstrings (`:295`, `:316`) claim this coverage.
  - Fix: run the rank and z cases on an entity that has facts. For example, give T1 rank 2 with `top_entities = 1`, and z = 0 (or −0.5) with an improving target, and assert there is no row.

#### Minor (Nice to Have)
- **M-1.** CC incidents are not bounded by `opened_at < as_of_ts` (`levers.sql.j2:99-109`); see ruling 1. A post-`as_of` incident can leak into a backdated build.
- **M-2. Survivors that come from fixture blind spots:**
  - The canceled incident I8 is out of window, so dropping `NOT f.excluded` from `inc` survives (M14, `test_metrics_levers.py:88`). The module docstring claims the canceled incident is excluded.
  - No excluded incident is change-caused (M13 survived).
  - No non-failed change has a caused incident (M12 survived).
  - N = D = 3, so `n_basis` for cfr set to N instead of D survives (M17).
  - Fix: use a fixture with D ≠ N, an excluded in-window incident, and an excluded caused incident.
- **M-3.** The org test has a single level, so using `oc.org_id` instead of `ancestor_org_id` survives (M20, `levers.sql.j2:18`). Add a parent org.
- **M-4.** The `unit` default `'other'` is untested (M26, `levers.sql.j2:165`).
- **M-5.** The higher-is-better branches (`q75`, `t > x`; `levers.sql.j2:46,54`) are untested (M05 survived). The validator's step 7 makes this branch unreachable in production, so it is acceptable as defensive code.
- **M-6.** All shipped lever templates are identical (`config/metrics.yaml:776-782`). `rationale_template == templates[model]` therefore cannot detect a model mix-up between templates (for example, keying `lkp` by the wrong model). Low risk.
- **M-7.** Redundant guards cannot be checked on their own: the sla penalty guard (M10) and `delta_usd > 0` (M11) / the improving filter (M23) each survive alone because the other filter covers the same case. The combination is caught (M35). There is no defect; this is noted for the record.

### Mutation results (levers.sql.j2 / levers.py, test file `test_metrics_levers.py`)
| ID | Mutation | Result |
|---|---|---|
| M01 | drop the `s_lever_models_k` filter | survived (the model `CASE` NULL is a second guard) |
| M33 | drop the filter and add `ELSE 1.0` to the delta `CASE` | **caught** (TH04-10 test) |
| M02 | extra `template_params` key | **caught** |
| M03 | template not via `lkp` | **caught** |
| M04 | lower-better uses q75 | **caught** |
| M05 | higher-better uses q25 | survived (unreachable branch) |
| M06 | drop `z_score > 0` | **survived** (I-1) |
| M07 | drop `value IS NOT NULL` | survived (redundant: a NULL x fails the improving filter) |
| M08 | drop the rank filter | caught only by the bind-set test |
| M34 | rank filter made a tautology | **survived** (I-1) |
| M09 | drop the mttr x≠0 guard | **caught** |
| M10 | drop the sla penalty guard | survived (`delta_usd > 0` covers it) |
| M11 | drop `delta_usd > 0` | survived (the improving filter covers it) |
| M35 | drop the improving filter and `delta_usd > 0` | **caught** |
| M12 | CC: drop `x.failed` | survived (M-2) |
| M13 | CC: drop the excluded filter | survived (M-2) |
| M14 | inc: drop the excluded filter | survived (M-2) |
| M15 | events: drop the severity filter | **caught** |
| M16 | drop the window lower bound | **caught** |
| M17 | `n_basis` cfr → N | survived (N = D) |
| M18 | cfr delta D → N | **caught** (F=0 test) |
| M19 | `unconfirmed` → false | **caught** |
| M20 | org closure → `org_id` | survived (M-3) |
| M21 | CC/F → CC/D | **caught** |
| M22 | F=0 term → 1000 | **caught** |
| M23 | improving uses `t <= x` | survived (`delta_usd > 0` covers it) |
| M24 | quartile without peer_group | **caught** |
| M25 | drop the reopen factor | **caught** |
| M26 | unit default changed | survived (M-4) |
| M27 | py: no upstream ID | **caught** |
| M28 | py: `append` instead of `replace` | **caught** |
| M29 | `entity_name` team only | **caught** |
| M30 | `lower_better` forced true | caught only by the bind-set test |
| M31 | noise E → N | **caught** |

All files were restored. The git blob hashes of `levers.sql.j2` (aebb8f0…) and `levers.py` (e98d36f…) are unchanged, and `git status` is clean.

### Gates (re-run)
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging`: **718 passed**.
- `levers.py` coverage: 100% line / 100% branch.
- `ruff check`: clean. `ruff format --check`: 938 files formatted.
- `mypy`: 0 issues in 344 files.
- `lint-imports`: 13 contracts kept.
- `tools.check_module_size`: exit 0.
- Sizes: `levers.py` 56 lines (budget 130); `levers.sql.j2` 176 lines (budget 200).
- No import cycle: `import _catalog_checks, levers` succeeds.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation is spec-faithful and the hand-checked dollar values are correct. Two UT04-99 skip rules (rank > top, z ≤ 0) are asserted only vacuously, so those mandated filters are unguarded by tests. This is a test-only fix.

---

## Re-review round 1 (bf07142..e82d92d)

**Verdict: Approved.** All in-scope findings (I-1, M-1, M-2, M-3, M-4) are fixed, and every re-run mutant now fails behaviour tests. Parked as agreed: M-5, M-6, M-7.

### Changes checked
- **M-1:** `levers.sql.j2:106` is now `WHERE x.failed AND f.opened_at < {{ p('as_of_ts') }}`. It uses only a bind and adds no literal. The upper bound alone is enough, since an in-window change cannot cause an incident before the window starts. `levers.sql.j2` stays at 176 lines (budget 200).
- **I-1:**
  - Rank: T1, which has facts, sits at rank 2. With `top_entities` 1 it gets 0 rows; with 2 it gets 1 row (control).
  - z-score: with z ∈ {0, −0.5} T1 gets 0 rows; the control with z 0.1 gets 1 row.
- **M-2:** the fixture now has:
  - I8: canceled, in window, caused by C3, with money forced onto its fact row so a missing exclusion would show;
  - I7: opened after as_of, caused by C3;
  - C6: a successful change, which makes D = 4 while N = 3;
  - a new test where C1 is not failed.
- **M-3:** org O1 now has a parent O0, and the org test uses O0, so the closure is two levels deep.
- **M-4:** a new test patches the units binds and asserts the default unit `'other'`.

### Hand re-check of the new values
Each check is D × (x − t) × (backout + CC/F) × 5. Backout is 400 and x − t is 0.25 to the median, 0.375 to the quartile.
- **cfr:** D = 4 (C1, C2, C3, C6), F = 2, CC = 10000 (I3 only), so CC/F = 5000. 4 × 0.25 × 5400 × 5 = **27000.00** and 4 × 0.375 × 5400 × 5 = **40500.00** ✓
- **C1 not failed:** F = 1 (C3). C3's incidents are I8 (excluded) and I7 (after as_of), so CC = 0. 4 × 0.25 × 400 × 5 = **2000.00** and 4 × 0.375 × 400 × 5 = **3000.00** ✓
- **F = 0, with C3 deleted:** D = 3. 3 × 0.25 × 400 × 5 = **1500.00** and 3 × 0.375 × 400 × 5 = **2250.00** ✓
- **Unchanged:** N, the sums, E and observed_days (73) do not move. I8 is excluded and I7 is outside the window.

### Mutants re-run (each restored byte-exact; levers.sql.j2 blob d85d625 before and after)
| ID | Mutation | Result | Killing tests |
|---|---|---|---|
| M06 | drop `z_score > 0` | **caught** | non_positive_z_is_skipped[0.0], [-0.5] |
| M34 | rank filter made a tautology | **caught** | rank_above_top_entities_is_skipped |
| M08 | drop the rank filter | **caught** (behaviour plus the bind-set test) | rank test, z tests |
| M12 | CC: drop `x.failed` | **caught** | cc_counts_only_failed_changes |
| M13 | CC: drop the excluded filter | **caught** | cfr_delta, cc_counts_only_failed_changes |
| M14 | inc: drop the excluded filter | **caught** | 8 tests (mttr, repeat, reopen, reassign, ...) |
| M17 | `n_basis` cfr → N | **caught** | UT04-100 template_params |
| M20 | org closure → `org_id` | **caught** | mttr_org_entity_uses_closure |
| M26 | unit default changed | **caught** | metric_without_unit_uses_other |
| M36 | drop the new CC `opened_at < as_of_ts` bound | **caught** | cfr_delta, cc_counts_only_failed_changes |

### Gates
- `test_metrics_levers.py`: 26 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/metrics -q -p no:logging`: **722 passed**.
- `ruff check` and `ruff format --check`: clean.
- `mypy`: 0 issues in 344 files.
- `lint-imports`: 13 contracts kept.
- `tools.check_module_size`: exit 0.
- `git status` clean, `git diff` empty.

### Open findings
- **Critical:** none. **Important:** none.
- **Minor, parked by the controller:**
  - M-5: the higher-is-better q75 branch is untested; production config cannot reach it.
  - M-6: the shipped templates are identical.
  - M-7: some guards overlap, so each survives removal on its own.
- **Note, not a defect:** the fixture writes money onto the excluded I8 fact row (`test_metrics_levers.py:159-162`). This is a deliberate test-only device so that a missing exclusion would show, and it is documented in the module docstring.

**Task quality:** Approved
**Reasoning:** Every in-scope finding is fixed, and the CC bound follows the spec's step-3 window. Each targeted mutant is now killed by a behaviour test, and the gates are clean.
