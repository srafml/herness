# T11-06 Catalog: review (verify agent)

Reviewed: commit 9b7c413 on base 8c11ea7, worktree agent-a4223eb3b67c31e20. Read-only.
Checks I ran myself: the UT11-05 file (23 passed, 0.29 s, `--require-test-ids`); ruff check and ruff format --check on tools/synth and its tests (clean); mypy --strict on the 3 new modules (clean); two scratch analysis scripts, results below.

### Spec Compliance
- ❌ Issues found: one invariant gap. See Important I-1: plant *teams* are only partly disjoint. Everything else matches the spec, or deviates in a way the report discloses and the evidence justifies.

Per-requirement check (U11-04 algorithm, step by step):

| Step / requirement | Status | Note |
|---|---|---|
| 1 Streams `catalog:<class>`, `plants:<class>` | ✅ | catalog.py build_catalog |
| 2 Orgs: 60-word pool, drawn without replacement | ✅ | pool has 60 unique words, min length 4 |
| 3 Teams: Poisson(12.5) clipped to [6,20], adjust largest org ±1 until 150; tiny 4 per org | ✅ | catalog.py:83-91. tiny uses `_split(preset.teams, [1]*orgs)` = 4 per org |
| 3 Team names `<Area> <Function> L1-3` (pools 40 × 25), unique | ✅ | sampled without replacement from 3,000 combos |
| 3 mttr = exp(N(0,0.2)); reassign_extra 0.8 when mult > 1.3 | ✅ | read from params (defaults 0.2 / 0.8 / 1.3) |
| 4 Service names (50 × 50), unique | ✅ | |
| 4 Criticality "drawn with p" | ⚠️ deviation, **justified** | exact largest-remainder quotas, shuffled (catalog.py:120). See the evaluation below |
| 4 Owner and support teams weighted by Pareto(1.5)+1 | ✅ | |
| 5 Incident weights Pareto(1.2)+1, ×1.5 for crit 1; bisection over [0.5, 3], 40 iterations, only when share(1) is outside the band | ✅ with a disclosed reorder | the S6 overrides are applied inside share(γ). See concern 4 |
| 5 Event weights independent Pareto(1.2)+1 | ✅ | |
| 6 CIs (service CI shares the service sys_id, appl, 1-3 servers, db for crit ≤ 2) and relations | ✅ | the test checks classes and the relation count |
| 7 Jira key: first 3-4 letters upper-cased, unique by suffix digit; one component per service | ✅ | e.g. Northwind/Northstar gives NORT / NOR2 |
| 8 Plants from rng_p, each excluded from the other plants' candidates; T6 has ≥ 5 free crit-3 peers | ✅ services | `_Picker`. S4/S5 are limited to crit {1,2,4} so every T6 peer stays unused, which is needed because peers = all free crit-3 |
| 8 T1: 3 crit-2 services within ±20 % of the median, support team moved to T1, T1 multiplier 1.0 / extra 0 | ✅ (with a clamp) | picked on base weights, then clamped to ±20 % of the final median (catalog.py:190). This is extra behaviour, disclosed, and it keeps the stated invariant true |
| 8 T5 is the only support team of S5 | ✅ | test asserts that S5 is T5's only supported service |
| Invariant: plant services and teams pairwise disjoint (design §5.1.5 "disjoint services and teams") | ❌ partly | services ✅. T1 ≠ T5 ✅. But the owners of other plants' services are not kept apart from T1/T5 or from each other. See I-1 |
| 9 S6p/S6u carry 5 % (small, tiny) or 1.5 % (full); S4 event weight 0 | ✅ | tested, including tiny |
| 10 effective_at (standard: first of end month − 3 months; tiny: end − 42 d) and peak_window | ✅ | 2026-05-01T00:00Z at small. tiny window = last 30 days, and a year-end wrap is handled |
| 11 Month split by days, largest remainder; planners added; seq_start as prefix sum | ✅ | per (source, entity) prefix starting at 1. Consistent with U11-07 `seq_start + i` if i is 0-based |
| Change schedule: 80 T3 changes, k ~ U{2..6} from `plants:t3` | ✅ | the 30 % caused_by subset is left to T3 (carry-over, see Minor M-4) |
| Errors: inconsistent preset raises SynthUsageError | ✅ | tested with a params file |
| Determinism; full < 1 s | ✅ | same seed gives an equal catalog; full builds in about 0.1 s |
| Test IDs: UT11-05 covered, every function carries its ID and docstring | ✅ | 23 functions |

**Provisional ruling: criticality quotas instead of independent draws. Verified; deviation justified.**
Binomial calculation at tiny (n = 20):
- P(crit-3 ≤ 6) = 0.2500, so 25 % of seeds cannot place T6 (it needs ≥ 7).
- P(no crit-1) = 0.9^20 = 0.1216, so 12 % of seeds have no S2.
- P(crit-2 ≤ 3) = 0.225, which the builder did not mention: 22.5 % of seeds cannot place S3 plus the three T1 services.

Independent draws would make about half of tiny seeds raise SynthUsageError, which cannot be the intent (tiny is the CI preset). Exact quotas keep the expected mix, make it exact, and leave the SynthUsageError path for params-file mixes. Recommend amending the spec text: "criticality counts = largest-remainder quotas of p, shuffled".

**Builder concern 4 (S6 overrides inside the γ calibration). Verified; justified.**
I re-ran seed 42 with the literal spec order (calibrate, then override):
- small: top-5 % share is 0.4000 before the override and **0.4457 after**, so UT11-05's [0.38, 0.42] check would fail.
- full: 0.4026 after the override, inside the band.

The spec is internally inconsistent here, and folding S6 into share(γ) is the smallest fix. Across 200 seeds at small, the builder's version keeps the share in [0.3806, 0.4199] with 0 seeds outside the band.

**Builder concern 5 (small vs full equal except S6 weights).** Partly correct. Step 9 forces S6 weights to differ, so strict equality is impossible under the spec. However, because γ now depends on the S6 fraction, *every* service's incident_weight differs between small and full, not only S6p/S6u. The test (test_synth_catalog.py:101) strips incident_weight from every service, so it cannot catch any other drift in weights. Plants, CIs, teams and the rest are equal (verified). Acceptable given concern 4. The spec's UT11-05 "catalogs equal" wording needs a controller or spec amendment. See Minor M-2.

**Builder concern 6 (values the spec leaves open).** Each choice fills a genuine gap:
- cost model
- cost_center format
- id_base
- CI names
- ChangeSlot.seq meaning
- seq_start starting at 1
- month_counts limited to time-sliced entities: consistent with U11-24's dimension shards (R-60)
- E2d counted as a plant service: consistent with U11-11's "lowest-weight criticality-4 service"

None contradicts the spec.

- ⚠️ Cannot verify from the diff:
  - "Retail" service S5: the catalog has no domain attribute, so any service qualifies.
  - Whether seq_start = 1 matches U11-07's `i` base: decided by the T11-08 card.
  - Whether spec 04's "owning team" (T3, T4 outputs) means owner_team or support team. This decides how serious I-1 is.

### Strengths
- Tight, readable implementation that fits the module budget (378/380); the split into sibling modules is clean.
- small and full produce identical plants because T1 is picked on base weights and E2d is chosen by a γ-monotone minimum.
- The planner hook passes a stub that already has plants and background counts, and it guards against negative totals (needed for T6 thinning in U11-15).
- Deviations are disclosed and backed by numbers, and all of them check out.
- Good tests: year-end peak wrap, source selection, 25-seed tiny robustness, the performance acceptance check.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- **I-1: Plant teams are not disjoint from the teams behind other plants.** catalog.py:254-255 and `_assign_plant_teams` at catalog.py:225.
  - What the code does: T1 and T5 are drawn uniformly over all teams. Only *support* assignments are cleaned up. Owner teams of S2, S2c, S3 (C3), S4, S6p and S6u, and the support teams of S3 and S4, may be T1, T5 or each other.
  - Why it matters: design §5.1.5 says plants "use disjoint services and teams", and its T3 and T4 outputs are stated per "C3's owning team" and "S4's owning team". A T5 that also owns S3 or S4 could be pushed into the top 5 of score.org, breaking T5's expected output. A T3 team that doubles as a T1 or T4 team muddles the rank checks.
  - Measured over 200 seeds, T1 or T5 is the owner of another plant's service in:
    - 13/200 (6.5 %) at small
    - 130/200 (65 %) at tiny. At seed 42 tiny, T5 owns one of the T1 services.
  - Also measured: the teams of S3 and S4 overlap in 17/200 small seeds. At seed 42 small and full (the BT11-05 seed), team 273f0b owns S3 and a T1 service and is S4's support team.
  - Tests: only T1 ≠ T5 is tested (test_synth_catalog.py:69-81).
  - Fix: draw T1 and T5 from teams that do not own or support any other plant service. Then either reassign the owner and support of S3/S4 so their teams are distinct from each other and from T1/T5, or state explicitly in the spec that only T1 and T5 count as plant teams. Add a test assertion either way.

#### Minor (Nice to Have)
- **M-1:** `_split` (catalog.py:73-80) breaks if `sum(counts) > total`. The `+1e-9` floor can overshoot, and then `order[:negative]` increments almost every bucket. Unreachable with integer day weights and the default mix. Guard with `max(0, …)` or assert the sum equals total.
- **M-2:** The small/full equality test (test_synth_catalog.py:101) strips *all* incident weights. Also assert that the non-S6 weights are identical up to γ, e.g. equal rank order, or that their ratio to base is w^γ. Ask the controller to amend UT11-05's "equal" wording in the spec.
- **M-3:** The bisection (catalog.py:182-187) settles silently on an endpoint when [0.5, 3] does not bracket 0.40. At tiny (k = 1 service) the share is outside the band in 38/200 seeds (range 0.20-0.68). UT11-05 does not require the band at tiny, but a comment or a spec note would prevent a surprise later. Step 3 has the same gap: adding +1 to the largest org can exceed the clip maximum of 20 (catalog.py:90). This follows the spec literally; note it.
- **M-4:** U11-12 says the T3 30 % caused_by subset "is chosen from that stream by index". To do that, the T3 card must replay the exact draw sequence of `_change_schedule` (catalog.py:278ff) on `plants:t3`, which is a fragile coupling. It is cleaner to draw the subset here and carry it on ChangeSlot (e.g. a per-slot caused_by count), or hand the stream state over explicitly. The controller should rule on this before T11-12.
- **M-5:** No test checks the per-org team counts (each within [6, 20] except the adjusted org; 4 per org at tiny). Only the total of 150 is asserted.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation follows U11-04 closely. The criticality-quota and calibration-order deviations are verified as necessary: literal-order small gives 0.4457, and independent draws break about half of tiny seeds. Only I-1 blocks approval: plant teams are not disjoint beyond T1 ≠ T5, which goes against design §5.1.5 and happens often at tiny. Fix it, or get a ruling that narrows "plant teams" to T1 and T5.

---

## Re-review round 1 (commit 6a942d9)

Scope: I-1, M-1, M-2 and M-5 only (M-3 and M-4 are parked). catalog_plan.py is accepted by ruling.

Checks I ran:
- UT11-05 with `--require-test-ids`: 55 passed in 1.77 s.
- ruff check: clean. mypy on tools/synth: clean (8 files).
- Line counts: catalog.py 347/380, catalog_plan.py 76.
- The seed-sweep probe, rerun on the new commit.

Seed-sweep results (200 seeds each at small and tiny; before the fix: 13/200 at small and 130/200 at tiny had T1 or T5 on another plant's service):

| Check | small | tiny |
|---|---|---|
| T1 or T5 holds any role on a plant service beyond its own (T1 supports its 3 services, T5 supports S5) | 0 | 0 |
| S3 and S4 share an owner or support team | 0 | 0 |
| S3's or S4's teams include T1 or T5 | 0 | 0 |
| Soft goal: S3's or S4's teams avoid all other plant services' teams | 0 | 57 |
| Top-5 % share range | [0.3806, 0.4199] (unchanged) | – |

The 57 tiny cases are expected: tiny has 12 teams and 11 plant services, so the documented fallback applies. The hard constraints still hold in every case.

At seed 42, the small and full catalogs have equal plants and equal teams.

- **I-1: ✅ Fixed.**
  - `_draw_team` and `_plant_teams` (catalog.py) prefer teams with no plant role and fall back only on the hard exclusion set.
  - Owners reassigned here flow through to jira_project and cost_center, because those are derived from the owner after the plants are chosen.
  - The new test `test_ut11_05_plant_teams_are_disjoint_over_seeds` asserts the hard constraints.
- **M-1: ✅ Fixed.** `split_counts` (catalog_plan.py:18-30) floors exactly and guards the remainder range. A quota slightly below an integer because of float noise is still corrected by the largest-remainder step.
- **M-2: ✅ Fixed.** The log-ratio assertion shows that every weight other than S6 and T1 differs only by the gamma exponent. This is sound because base weights are above 1, so their logs are positive.
- **M-5: ✅ Fixed.**
  - `test_ut11_05_team_counts_per_org` covers 30 seeds: small stays within [6, 20] and sums to 150; tiny is exactly 4 per org.
  - The adjustment now moves the largest org that still has room, so every org stays inside the clip range; if none has room it raises SynthUsageError. This is a reasonable reading of step 3 that honours the clip.

No new findings. M-3 and M-4 stay parked as ruled.

**Task quality:** Approved
**Reasoning:** All four scoped findings are fixed and backed by tests. The seed sweep shows no hard disjointness violation at small or tiny, and the calibration and small/full plant equality hold as before.
