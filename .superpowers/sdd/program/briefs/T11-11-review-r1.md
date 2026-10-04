# T11-11 re-review, fix round 1 (1e89c87 on 7276ef5)

Scope: I1, m1, the new `t6_drop` shrink, `PlantOutput.replaced`, the documented plant order, and regressions.

## Findings from the original review
- I1 (T6 refill biased S6u and the other plant services): ✅ fixed. `refill_pool` (tools/synth/plants_delivery_t6.py:145-152) returns every service except S6p, with the background incident weights renormalised (taken from `service_index`, so the weights and order match the background draw). `_thin` draws from this pool (:162, :187). Tests: `test_ut11_18_refill_pool_is_every_service_but_s6p` checks the membership, order and renormalised p (rtol 1e-12). `test_ut11_18_refills_follow_the_pool_and_get_t1_treatment_after_t6` checks empirical refill counts against n x p within 4 sd over more than 300 refills, and that S6u and the T1 services get hit. `test_ut11_18_other_services_keep_expected_volume[tiny|small|full]` checks that the planned shrink equals the expected refill inflow per service.
- m1 (expose replaced ids): ✅ fixed. `PlantOutput.replaced` is a defaulted list (tools/synth/shards.py:59), and the four spec fields are unchanged. `_thin` appends `servicenow:incident:<old sys_id>` (plants_delivery_t6.py:189). The thinning test asserts that `replaced` equals exactly the vanished ids in order, that the refill labels are disjoint from `replaced`, and that there are 5 labels per replaced record.

## Builder's extra change: `t6_drop`
- Derivation: correct. The background produces N - D records. Expected S6p post-effect records = share x post x (N - D). 40 % of those are refilled, and service s gets the fraction w_s / (1 - share) of the refills. Setting w_s (N - D) + 0.4 share post (N - D) w_s / (1 - share) = w_s N gives D (1 - share) = 0.4 share post (N - D), so D = tN / (1 - share + t) with t = 0.4 share post. This matches `t6_drop` (:122-128). The denominator is always > 0.
- The planner's `post` fraction uses a date comparison, and the thinning uses a datetime comparison. `effective_at` is midnight UTC (catalog.py:296), so the two agree.
- S6p still passes UT11-18. The spec's 0.4 Bernoulli is unchanged, and the synthetic-stream test is unchanged and passes. In generated data, S6p's post-effect rate is 0.6 x (1 - D/N). With share 0.05 and post = 1, that is about 0.6 x 0.979 = 0.588, within 0.60 ± 0.05. The old shrink gave about 0.588 as well, so the fix introduces no regression here.
- Gap-free and deterministic seq: ✅. The mechanism is unchanged (negative plan count plus refill in place with the same number and open time), and only the magnitude of D changes. The per-drop rng draw count is unchanged (random, choice, make_incident). The thinning test still asserts identical numbers and counts.
- UT11-18 planner test change: legitimate. The expectation now follows the new formula with the same 1.5 tolerance and the same 29/31 approximation, and the background-count identity is kept. Nothing was loosened.
- Plant order "T6 before T1/T3/T5 on background records": acceptable. The spec (U11-15, U11-19) does not fix the order. This order is the one that makes refills indistinguishable from background incidents, and it is exercised by the plant_t6 then plant_t1 test. It is correctly carried as a U11-19 spec note.

## Gates (run here)
- `PYTHONUTF8=1 uv run pytest tests/unit/tools/synth -q -p no:logging`: 206 passed.
- Coverage of tools.synth.plants_delivery_t6 with branch coverage: 99 % (one partial branch at 153->147, the empty-records loop exit). This is fine.
- ruff check and ruff format --check are clean on tools/synth and tests/unit/tools/synth. mypy is clean on the touched modules.

## New findings
### Critical
None.
### Important
None.
### Minor
- n1 tools/synth/plants_delivery_t6.py:15-17 — the docstring places the 0.6 x (1 - drop / month count) effect in the boundary month ("there"). It actually holds in every post-effect month, because S6p's pre-thinning volume there is share x (N - D). The value is about 0.588 and stays within tolerance. Reword the docstring.
- n2 tests/unit/tools/synth/test_plants_delivery.py (test_ut11_18_other_services_keep_expected_volume) — the test is algebraic: it recomputes the expectation from the same formula and does not measure generated volumes. The empirical side is covered by the pool-distribution test, and the test does fail for the old shrink. Acceptable as is.
- (carried, unchanged) m2, m3, m5 and m6 from the original review. m4 is now documented.

## Verdict
**Approved**
