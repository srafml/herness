# T11-11 review (commit d560d60, base b64ba41)

### Spec Compliance
- ✅ Spec compliant, with one Important deviation in the T6 reconciliation (S6u and the other plant services lose volume; see I1)
- U11-11 T2 cluster size round(0.03 x preset.incidents), split over months by days: ✅ (plants_delivery.py:67-68, 75-83, 95-98; UT11-14 asserts 36)
- U11-11 T2 40 % P2 / 60 % P3 (exact, evenly spread by global index): ✅ (plants_delivery.py:106-108; 14/22 asserted)
- U11-11 T2 customer impact on all incidents: ✅ (servicenow_incidents.py `_impact(force=)`; see m2 for the "0 minutes" edge)
- U11-11 T2 family tpl_conn_pool, one fixed slot set, 20 % variation via params.text: ✅ (plants_delivery.py:111-125, 157-168)
- U11-11 T2 root cause capacity (from the family), problem_id/caused_by empty, change_caused false (controller ruling): ✅
- U11-11 E2 Epic, S2 project and component, connection-pooling summary, 34 / 120,000, In Progress: ✅ (plants_delivery.py:197-221)
- U11-11 exactly 20 remotelinks = first 20 cluster incidents by opened_at after start + 30 d, object.title = number: ✅ for the presets (plants_delivery.py:176-185; the test recomputes the list from the generated records); see m3 for short spans
- U11-11 decoy E2d on the lowest-weight criticality-4 service (catalog pick, catalog.py:272), 400 / 900,000, "urgent" / "critical risk" / "outage exposure": ✅
- U11-11 Jira records from the jira/issue shard of the month of start + 30 d: ✅ (epic_month, planner books 2 there)
- U11-11 T2c 10 per 30 days, 60/40, tpl_cert_expiry, access_identity, customer impact, no link: ✅ (18/12 asserted, never in E2 links)
- U11-15 E6p/E6u Done, resolutiondate = effective_at, S6p/S6u project and component: ✅ (plants_delivery_t6.py:183-203)
- U11-15 S6p post-effect (>= effective_at + 14 d) Bernoulli 0.4 thinning in rng order, MTTR x 0.8 on the rest, S6u records untouched: ✅ (plants_delivery_t6.py:144-176)
- U11-15 Algorithm "thinning before sequence numbers; plan reduces S6p's post-effect count": ✅ in substance (negative plan count keeps seq_start gap-free; refill-in-place keeps the numbers of the planned count). Deviation: refill pool excludes all plant services, which biases S6u (I1).
- UT11-14 (7 functions): ✅ all Expected items covered.
- UT11-18 (4 functions): ✅ E6p/E6u Done at effective_at; MTTR x 0.8 (±1 s); 0.60 ± 0.05 measured on a synthetic uniform S6p stream, not on the tiny catalog (acceptable: the tiny S6p volume is ~20 post-effect incidents, too noisy for ±0.05).
- Existing-test edits: legitimate. UT11-13 (test_plants_ops.py:121-124) only accounts for the new negative T6 count (`min(0, …)` ignores the +2 Jira epics); `plants >= 0` and the gap-free range walk are kept. UT11-05 (test_synth_catalog.py:292-314) adds the clusters, four epics and the T6 shrink to the totals and widens the planner-id prefix; nothing loosened beyond that.
- IncidentSpec changes keep the background draw order: ✅ (defaults priority=None → same rng.choice; family/slots None; repeat False → `_Draw.member` False as before; `_impact(force=False)` short-circuits in the same order; `generate()` untouched).
- Conventions: test names carry ut11_14/ut11_18, docstrings start with the ID, module-level pytestmark = unit: ✅
- Module size: plants_delivery.py 277/300, plants_delivery_t6.py 233 (private sibling, 400 default, controller-accepted), servicenow_incidents.py 367/400, catalog.py 363/380: ✅
- ⚠️ Cannot verify from diff: coverage figures (report: 100 % / 99 % / 100 %) and the gate runs; the downstream U11-19 filtering of stale label/PII rows (not built yet).

### Measured (focused check, risk "other services' volumes distorted")
Planned T6 shrink vs background month count (seed 42): tiny 2026-08: -8 of 413 (1.94 %); small 2026-05..08: -29/2828 (1.03 %), -55/2737, -57/2828, -57/2828 (2.0 %). S6p and S6u each carry 5 % of incident weight.

### Strengths
- Clean determinism story: T2 open times from a keyed (S2, month) stream so the Jira shard can name the incidents without the incident shard; exact deterministic P2/P3 split.
- Plan-level reconciliation keeps seq ranges gap-free and shard-independent; refill preserves number and open time.
- Epic builder shared across E2/E2d/E6p/E6u; tests assert the real behaviour (numbers from plant_range, links recomputed from generated records, S6u / pre-effect records byte-identical after plant_t6).

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
- I1 tools/synth/plants_delivery_t6.py:147-151 — the refill pool excludes every plant service, but the planned shrink (planned_counts_t6, :123-139) reduces the whole month's background for all services. Non-plant services get the volume back through the refills; plant services do not. S6u therefore loses about 0.4 x share(S6p) x post-fraction of its background in every post-effect month (measured ≈ 2 % at small and in tiny's August, ≈ 0.6 % at full), a step exactly at effective_at + 14 d. That contradicts "S6u unchanged" in expectation and contaminates the control arm of the planted effect (F05 `no_effect` for E6u). The same step hits S2/S2c/S3/S4/S5/T1 background. Fix: draw the refill service from every service except S6p (weights = background incident weights renormalised without S6p). Then every service other than S6p is unbiased in expectation. Add an assertion (e.g. S6u expected post/pre rate within tolerance, or refill pool = all services minus S6p).

#### Minor (Nice to Have)
- m1 tools/synth/plants_delivery_t6.py:144-172 / docstring :213-221 — the dropped record's background label rows (5) and any PII rows stay in the shard's generator output. U11-19 must drop label/PII rows whose record_id no longer appears in the shard records. This is only documented. Consider returning the replaced record ids (e.g. in PlantOutput.members or a dedicated list) so U11-19 can filter them mechanically. Carry as a spec note on U11-19.
- m2 tools/synth/servicenow_incidents.py:128-135 — `force` impact can round to "0" minutes for very short durations. Then "all with customer impact" is only formally true. The UT11-14 check `>= 0` (test_plants_delivery.py:141) would accept 0. Consider max(1, …) when forced.
- m3 tools/synth/plants_delivery.py:176-185 — e2_links silently returns fewer than 20 links when the span has fewer than 20 T2 incidents after start + 30 d (short custom spans). "exactly 20" is then violated without a signal. A span guard or a docstring note would help.
- m4 tools/synth/plants_delivery_t6.py:123-139 — the negative count is spread over the whole boundary month, including its pre-effect days, while refills land only on post-effect days. S6p's post/pre ratio is 0.6 x (1 − drop/N) ≈ 0.59, which is within tolerance. Note it in the docstring.
- m5 tools/synth/plants_delivery_t6.py:144 — `_thin` builds a TemplateBank() per call (same as parked T11-10 m3).
- m6 tools/synth/plants_delivery_t6.py:176-181 — if effective_at < span start + 1 d, the In Progress step (created + 1 d) can follow Done. This is an unreachable edge with the catalog's effective_at, so it is noted only.

### Spec notes for the controller
- Module map row for tools/synth/plants_delivery_t6.py (private sibling).
- U11-15 wording: dropped S6p slots are refilled in place (same number and open time) by a non-S6p incident. U11-19 discards orphaned label/PII rows.
- U11-07: IncidentSpec gains priority/family/slots/repeat/customer_impact (defaults keep background behaviour).

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Every T2/T2c/T6 postcondition is implemented and tested, and the background draw order is preserved. The T6 refill pool, however, systematically lowers S6u's (and the other plant services') post-effect volume by about 2 % at small, which breaks "S6u unchanged". The fix is one line.
