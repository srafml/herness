# T11-11 report: Delivery plants T2, T2c, T6

Status: DONE_WITH_CONCERNS (spec notes only; every gate is green)
Commit: d560d60 feat(synth): delivery plants T2, T2c, T6 (T11-11)
Worktree: D:\herness\.claude\worktrees\agent-a487baa62b88cc0c0 (base b64ba41). This build resumed the uncommitted work of the killed agent. I reviewed that work against the brief, kept it, and fixed two existing tests plus one ruff finding (PT018).

## Files changed (lines after / budget)
| File | Lines | Budget | Change |
|------|-------|--------|--------|
| tools/synth/plants_delivery.py (new) | 277 | 300 | T2, T2c, the three planners (re-exports plant_t6 / planned_counts_t6), e2_links, t2_times, cluster_slots, epic_month |
| tools/synth/plants_delivery_t6.py (new, private sibling) | 233 | 400 default | T6 thinning, E6p/E6u, the shared EpicSpec/epic_issue builder, find_service |
| tools/synth/servicenow_incidents.py | 367 | 400 default | IncidentSpec gains priority, family, slots, repeat and customer_impact (all defaulted); `_impact(force=)` |
| tools/synth/catalog.py | 363 | 380 | default_planners() = ops planners + (t2, t2c, t6) |
| tests/unit/tools/synth/test_plants_delivery.py (new) | 400 | - | UT11-14 x7, UT11-18 x4 |
| tests/unit/tools/synth/test_plants_ops.py | +2 | - | the background check allows the T6 negative count |
| tests/unit/tools/synth/test_synth_catalog.py | +9 | - | default-plan totals include the T2/T2c clusters, 4 epics and the T6 shrink; plant_seq ids may come from plants_delivery |

## Design decisions
- T2 numbering: the cluster count is round(0.03 x preset.incidents), split over months by days (split_counts). Numbers come from plant_range(planned_counts_t2). Open times come from a stream keyed by (S2, month) (`t2_times`), not from the shard rng, so the Jira shard for the month of start + 30 d can recompute the numbers. `e2_links` returns the first 20 cluster incidents with opened_at > start + 30 d, walking months in order with numbers in open-time order. Text, MTTR and sys_id still come from the shard rng.
- Exact deterministic priorities: the record with global index k is P2 when floor((k+1) x pct / 100) > floor(k x pct / 100), else P3. That gives T2 14/36 P2 and T2c 18/30 P2, spread evenly.
- Fixed slot set: `cluster_slots` renders the family once, from a stream keyed by (family, service sys_id) with default TextParams. make_incident passes those slots to render_incident_text, which applies params.text.slot_variation (20 %). All plant incidents get repeat-flavored text, the repeat_issue label "true" and forced customer impact. The root cause comes from the family (capacity / access_identity). problem_id and caused_by stay empty and change_caused stays "false".
- E2 and E2d: written by the jira/issue shard of the month of start + 30 d (the planner books 2 there), created at start + 30 d.
  - E2: S2 project/component, 34 points / 120,000, one changelog step To Do -> In Progress one day later, 20 remote links (object.title = INC number, via jira_links.remote_link).
  - E2d: cat.plants.e2d_service (the catalog pick: the lowest-weight criticality-4 service), 400 points / 900,000, summary "Urgent: critical risk of outage exposure in <service>", To Do, no links.
- E6p/E6u: created at max(effective_at - 14 d, span start), In Progress one day later, Done at effective_at (resolutiondate = effective_at). Written by the Jira shard of the creation month (the planner books 2).
- T6 thinning reconciled with the plan (U11-15 Algorithm):
  - Plan: `planned_counts_t6` books a NEGATIVE count for each incident month that has days on or after effective_at + 14 d. The count is round(0.4 x E[S6p incidents on those days]), where E = S6p incident-weight share x the stub background month count x the post-effect share of the month arrival day weights. background_count shrinks, so seq_start and the plant ranges stay gap-free.
  - Drop: `plant_t6` walks `records` in order. Each S6p incident with opened_at >= effective_at + 14 d is dropped by Bernoulli(0.4) from the shard rng (deterministic by rng order).
  - Refill: the dropped slot is refilled in place by a background-like incident (make_incident) of a non-plant service drawn by background weights, with the same number and open time. Its labels are returned in PlantOutput.labels.
  - Kept S6p incidents are retimed with MTTR x 0.8 (retime). S6u and pre-effect S6p are untouched.
  - Result: the record count and numbering match the plan exactly, and S6p loses 40 % of its post-effect volume. Other services keep their expected volume, because the planned shrink (0.4 x expected S6p) offsets the replacements (0.4 x actual S6p).

## Invented constants
- E6p/E6u: 21 points, cost 70,000, created 14 days before effective_at, summary "Reduce incident volume on <service>".
- Epic start lag: 1 day (created -> In Progress). Epic updated = latest change + U(0, 2 h), as for background issues.
- E2 summary: "Rework database connection pooling for <S2>". Descriptions: "h3. Context" plus render_jira_text. The decoy description adds "This is urgent: a critical risk with broad outage exposure."
- Stream names: "plants:t2:times" (keyed by S2 and month) and "plants:cluster:slots" (keyed by family and service).

## Deviations
- T6 replaces dropped S6p incidents instead of deleting them (reason above). The background label rows (and any PII rows) of a dropped record become stale. U11-19 must discard label/PII rows whose record id is no longer in the shard records. The plant_t6 docstring says so.
- plant_t6 builds its own TemplateBank(), because its signature has no bank (same as plant_t5, parked minor m3).

## Spec notes for the controller
1. Module map: add a row for the new private sibling tools/synth/plants_delivery_t6.py (233 lines). T2 + T2c + T6 + the epic builder would be about 510 lines, over the 300 budget (plants_ops_t3 precedent).
2. U11-15: dropped S6p incidents are replaced in place by non-plant incidents (same number and open time). U11-19 must drop orphaned label/PII rows for replaced record ids. This is the only way to keep seq gap-free, because the Bernoulli outcomes are not known at plan time.
3. U11-11: T2 open times come from a stream keyed by (S2, month), so the E2 remotelinks are known in advance. The shard rng drives only text and MTTR.
4. U11-07: IncidentSpec gains priority/family/slots/repeat/customer_impact. The defaults keep the T11-10 behavior and the background draw order.
5. The existing UT11-05 and UT11-13 tests were updated to the larger default plan (T2/T2c clusters, 4 epics, T6 negative counts). No assertion was loosened beyond accounting for the new planners.

## Tests
- UT11-14 (7 tests):
  - Planner registration and counts.
  - Cluster = round(0.03 x 1,200) = 36: 14 P2 / 22 P3, customer impact, root cause capacity, planned numbers.
  - Open times are keyed.
  - Fixed slot set: at least 60 % of incidents carry the fixed symptom.
  - E2: 34 / 120,000, In Progress, exactly 20 remotelinks, equal to the first 20 cluster incidents after start + 30 d.
  - E2d: 400 / 900,000, criticality 4, alarming wording.
  - Other shards add nothing.
  - T2c: 30 incidents, 18 P2 / 12 P3, root cause access_identity, never linked.
- UT11-18 (4 tests):
  - The plan is negative in the post-effect month, within 1.5 of 0.4 x expected.
  - Thinning keeps the count, the numbers, S6u and pre-effect records. Kept MTTR = round(0.8 x old) +/- 1 s.
  - Synthetic uniform stream: S6p post/pre rate 0.60 +/- 0.05.
  - E6p/E6u are Done at effective_at.
- PYTHONUTF8=1 uv run pytest tests/unit/tools/synth -q -p no:logging: 193 passed. test_plants_delivery also passes with --require-test-ids.
- Coverage (branch): plants_delivery 100 %, plants_delivery_t6 99 % (one partial branch), servicenow_incidents 100 %.
- Broad run, pytest -m "(unit or integration) and not slow": 3357 passed, 5 skipped, 14 deselected, 1 xfailed (the IT00-02 marker, which was already there).
- Gates: ruff format/check clean; mypy 0 issues (130 files); lint-imports 13 kept; check_module_size exit 0; pre-commit hooks passed on commit.
## Fix round 1

Status: DONE
Commit: 1e89c87 fix(synth): unbiased T6 refill and replaced ids (T11-11), on top of 7276ef5. Files touched: tools/synth/plants_delivery_t6.py (263 lines), tools/synth/shards.py (62 lines), tests/unit/tools/synth/test_plants_delivery.py. servicenow_aux.py (T11-39) was not touched.

### I1: unbiased refill
- New `refill_pool(cat)` returns every service except S6p, with probabilities equal to the background incident weights renormalised without S6p. S6u, the T1 services, S2, S2c, S4, S5 and C3's service are all in the pool. `_thin` draws from it.
- Exact balance (extra to the review's one-liner): with renormalisation alone, a small positive bias remains (the planned shrink D was 0.4 x E[S6p post]. The expected refills are 0.4 x share x post x (N - D), and a service s gets the fraction w_s / (1 - share) of them). New `t6_drop(share, N, post)` solves D x (1 - share) = 0.4 x share x post x (N - D). The shrink therefore equals the expected refills landing on s, and every non-S6p service keeps exactly w_s x N in expectation, up to the integer rounding of D. `planned_counts_t6` books -round(t6_drop). The value is about 3 % larger than before at small/tiny (for example 0.4 x E / 0.97 when share = 0.05, post = 1).
- Boundary-month timing (review m4): the shrink is spread over the whole month, while refills land on post-effect days only. S6p's post/pre ratio is therefore about 0.6 x (1 - D/N). The module docstring says so.
- Plant order (consistency of refills): the docstrings now state that on an incident shard, U11-19 applies T6 to the background records first, then T1, T3 and T5. A refill is therefore a background record for them: a refill on a T1 service gets the T1 team, MTTR ramp and reassignments; on S5 it gets team T5; on C3 it is moved out of the control windows. `make_incident` draws the refill like a background incident (priority mix, the support team's MTTR model, text). As before, it has no problem-cluster membership and no PII. A test runs plant_t6 then plant_t1 and checks that every T1-service record, refills included, carries the T1 team.
- Background draw order of non-T6 paths is unchanged. T6 draws per dropped record are unchanged in count (one Bernoulli, one choice, then make_incident).

### m1: replaced ids
- `PlantOutput` gains `replaced: list[str] = []` (default, so the four spec fields and every existing construction stay as they were). T6 appends `servicenow:incident:<old sys_id>` for each replaced record. U11-19 drops label and PII rows whose record_id is in `replaced`.

### Tests (UT11-18, now 7 functions + 2 extra parametrised cases)
- thinning test: `out.replaced` equals exactly the original ids that are gone; refill labels are disjoint from `replaced`; len(labels) = 5 x len(replaced); refills are non-S6p (were non-plant).
- `test_ut11_18_refill_pool_is_every_service_but_s6p`: pool = all services minus S6p, in order, with renormalised weights (rtol 1e-12).
- `test_ut11_18_other_services_keep_expected_volume[tiny|small|full]`: per post-effect month, the plan equals round(t6_drop). For S6u, the T1 services and S5: w(N - D) + E[refills] x pool_p = w x N within rel 1/N. The old implementation fails this (about 2 % at small).
- `test_ut11_18_refills_follow_the_pool_and_get_t1_treatment_after_t6`: on the synthetic S6p stream (>300 refills), per-service refill counts are within 4 sd of n x p. S6u and the T1 services are hit. plant_t1 after plant_t6 gives every T1-service record the T1 team.
- Planner test: expectation updated to the t6_drop formula (still within 1.5).
- `PYTHONUTF8=1 uv run pytest tests/unit/tools/synth -q -p no:logging`: 206 passed. ruff format/check clean, mypy clean (131 files), lint-imports 13 kept, check_module_size exit 0, pre-commit hooks (including pytest-unit) passed.

### Spec notes for the controller
- U11-15: refill pool = every service except S6p (renormalised background weights). Planned shrink = t6_drop (D(1 - share) = 0.4 x share x post x (N - D)).
- U11-19: on incident shards, apply T6 before T1/T3/T5, only to background records. Drop label/PII rows of `PlantOutput.replaced`.
- U11-11 PlantOutput: new defaulted field `replaced`.
