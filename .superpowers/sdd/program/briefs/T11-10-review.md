# T11-10 review: Operations plants T1, T3, T4, T5 (b64ba41, base 6a38cbf)

**Verdict: Approved** (no Critical or Important findings; Minor items below are optional polish or notes for the controller)

### Spec Compliance
- ✅ U11-10 plant_t1: T1 services get the T1 group; MTTR x m(t) = 2.0 + (opened - start)/(end - start), with `end` = exclusive span end; reassignment + Poisson(1.0); `retime` recomputes resolved/closed/ack, state, made_sla, business_duration, impact minutes (scaled) and sys_updated_on; in place, input order. `planned_counts_t1` returns {}. Team name is neutral (catalog team).
- ✅ U11-12 plant_t3: 40 emergency + 40 normal C3 changes by work_end month (planner counts per month from `change_schedule`); k ~ U{2..6} drawn in the catalog; follow-up offsets are integers in [1, 7200] s, so they stay in (t, t+2h] after truncation; `work_end` <= span_end - 2 h (catalog.py `_change_schedule`), so follow-ups stay inside the span; cmdb_ci = C3, business_service = S3, change-flavored text, change_caused label "true". Exactly round(0.3 x total) `caused_by` flags are drawn once at catalog build from `plants:t3` after the slots are sorted and carried on `ChangeSlot.caused_by`. The plant never replays the stream (ruling 1 ✅). Control shift: +3 h, repeated while still inside a window, -3 h fallback at the span end (documented departure, acceptable). links, members and caused_by ref (sys_id + CHG number) are correct.
- ✅ U11-13 plant_t4: round(0.07 x events) split by days; 5-title set; minor/warning drawn 50/50 via `integers(2)`; incident_ref None; dedup_key = title slug; up to 3 redraws while within +/-30 min of an S4 incident (searchsorted on the sorted index); event keys from the plan's seq.
- ✅ U11-14 plant_t5: planner = round(1.8 x share_S5 x month background / days x peak days), where share = incident-weight share (the background draw probability). Extras are clones via make_incident on S5. The support team is T5 with mttr_multiplier 1.0 (catalog.py:344). They arrive only on peak days. S5 background gets T5. S5 request_count on peak days goes x2.8 (rounded to a whole count). I checked that S5 is inside `metric_services` for tiny, small and full (all services carry metrics).
- ✅ Background incidents: problem_id/caused_by stay empty and change_caused stays "false"; positives come only from T3 (`_labels(change_caused=)` defaults False).
- ✅ PlantOutput is in tools/synth/shards.py (frozen, data-only). Private sibling plants_ops_t3.py (188 lines) is acceptable under the spec note. All files are under budget: plants_ops 257/350, catalog 361/380, others < 400.
- ✅ UT11-13 (x3), UT11-15 (x7), UT11-16 (x4), UT11-17 (x4): IDs are in names and docstrings, `pytestmark = pytest.mark.unit`. A focused re-run of test_plants_ops, test_synth_catalog and test_synth_jira with --require-test-ids gave 90 passed. check_module_size is clean.
- ✅ Plant numbering (`plant_seq`/`plant_range`/`background_count`): each month is background [seq_start, seq_start + bg) followed by the planners' positive counts in planner order at the tail. The layout is gap-free, and the test checks it at test_plants_ops.py:106-133. Negative (T6) counts shrink `background_count` correctly because they are never booked. `planner_id` is module plus qualname of the same function object at build time and lookup time, so it is deterministic. It is a pure catalog value, so it is shard-independent. Background draw order is unchanged: `_change` keeps the draw sequence (kind, window, shift, lead, state, text, ci, sys_id); monitoring `_event` keeps lognormal before symptom; the caused_by draw comes after every existing `plants:t3` draw.
- ✅ Existing test changes are legitimate. test_synth_catalog uses `planners=()` for the pure background split and adds a default-planner totals test. test_synth_jira uses planned totals (background plus plant) as the CHG/INC bound: plant numbers are real records, so the bound follows the planned ranges and is not a weakening.
- ⚠️ Cannot verify from diff: IT11-05/07/08 (later cards); the truth writer's `generated_noise_ratio` (T4, truth card); that U11-19 builds `Shard.n_records = background_count(...)` and concatenates PlantOutput.records without doubling T5 extras (spec note 3, deviation 1). Those are follow-up obligations for T11-19.

### Departures (reviewed, acceptable)
1. plant_t5 returns the extras in PlantOutput.records instead of appending them to `records`. This is consistent with the PlantOutput contract, but U11-19 must concatenate them once.
2. plant_t3 dispatches on the shard entity (change_request or incident), within the single U11-12 signature.
3. UT11-13 checks 1e-9 on a constructed grid, because timestamps have 1 s resolution. Real background records are checked to within 0.5 s. Acceptable.
4. Control shift: iterative +3 h steps with a -3 h fallback at the span end. It keeps the postcondition "none within 2 h" and is pinned by a unit test.
5. Spec notes 1-7 (ChangeSlot.caused_by, Catalog.plant_seq, public helpers, private sibling) are for the controller to fold into the spec.

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. tools/synth/plants_ops.py:188: `planned_counts_t5` reads `cat.month_counts[key]` as the S5 baseline. That is correct only on the planner stub; called on a finished catalog it would include plant records. `background_count(cat, key)` gives the same result on the stub and is also correct on the final catalog.
2. tools/synth/plants_ops.py:188 and :209: the baseline uses the month's mean day, while the extras follow day weights over the peak days only. In partial-peak months (July 15-31) the weekend and holiday mix of the peak days differs slightly from the month's, so the extra ratio is about 1.8 x (mean weight of all days / mean weight of peak days). It is within sampling noise; noting it for the IT11-05 tolerance.
3. tools/synth/plants_ops.py:206: `TemplateBank()` is built on every incident-shard call (the signature has no bank). Consider caching it at module level if construction is not trivial.
4. tools/synth/plants_ops_t3.py:95: the seq is recovered by parsing the CHG number string back (`int(numbers[...][3:])`). Returning (seq, number) from `_change_numbers` would be cleaner.
5. tools/synth/plants_ops_t3.py:165 via servicenow_incidents `retime`: if a +3 h shift pushes a shifted C3 background incident's resolution past the span end, the incident silently becomes open. This is a rare edge case and consistent with retime's contract. Mention it in the docstring.
6. tests/unit/tools/synth/test_plants_ops.py:528: the UT11-17 incident bound adds a +0.1 fudge to a 4-sigma bound. It is computed as the spec asks but generous. Acceptable.

### Assessment
**Task quality:** Approved
**Reasoning:** Every postcondition of U11-10/12/13/14 is met and pinned by UT tests, and the controller rulings are followed (caused_by carried on ChangeSlot, background labels false). The new plant_seq numbering is gap-free, deterministic and leaves background draw order unchanged. The remaining items are polish and U11-19 integration notes.
