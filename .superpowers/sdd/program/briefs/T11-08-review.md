# T11-08 review (ServiceNow generators, U11-07): commit 2c49d82 on base ee452a6

**Verdict: Approved** (no Critical or Important findings; Minor items and controller rulings below)

Evidence: I read the full diff. Focused checks: params defaults (`git show 2c49d82:tools/synth/param_groups.py`), the `inject_pii` and `render_incident_text` signatures, and the ruff limits (max-args 6, complexity 10). I re-ran the T11-08 tests with `pytest tests/unit/tools/synth -k "UT11_09 or UT11_10"`: 18 passed. I also ran one scratch probe (business_timezone=America/New_York, 20,000 incidents) to check the timezone-edge risk (see M1).

### Spec Compliance
- ✅ Signatures match U11-07: `gen_groups`, `gen_cis` (returns the tuple cmdb_ci, cmdb_ci_service), `gen_rels`, `gen_incidents(cat, params, shard, rng, bank, names) -> IncidentBatch(records, labels, pii)`, `gen_changes`, `gen_problems`.
- ✅ Field sets equal design §5.1.2 for sys_user_group, cmdb_ci, cmdb_rel_ci, incident, change_request and problem. The tests assert set equality for every entity.
- ✅ R-60: `cmdb_ci` rows have no `busines_criticality`. `cmdb_ci_service` rows are deep copies of the service CI rows plus `busines_criticality`. UT11-10 asserts both, and also asserts that the two lists share no pair.
- ✅ Team groups carry their org's `cost_center` (org rows do too). `sys_updated_on` is present on groups (start 00:00:00).
- ✅ `number` = INC/CHG/PRB + `f"{seq:07d}"` of `seq_start + i`. `sys_id` = `rng.bytes(16).hex()`. Timestamps are `%Y-%m-%d %H:%M:%S` UTC.
- ✅ `sys_updated_on` = latest timestamp + U(0, 2 h), with the documented `<= end` filter (see interpretations).
- ✅ Step 1, arrival: weekday factors 0.35 and 0.30; holiday factor 0.5 on the 10 dates (params); `1 + 0.1*sin(2*pi*doy/365)`. The 24-value curve (0.3x7, 1.0x3, 2.2x6, 1.0x4, 0.3x4) gives 0-6, 10-15 and 20-23 as specified, in `business_timezone`. Minute, second and microsecond are uniform, then converted to UTC.
- ✅ Step 2: service drawn by incident weight; priority shares {.01,.06,.38,.50,.05}; criticality-1 P1/P2 shares x2 with the difference taken from P4.
- ✅ Step 3: assignment_group = support team.
- ✅ Step 4: ack with p 0.85, medians {5,15,45,90,180}, sigma 0.9.
- ✅ Step 5: MTTR medians {3,8,30,72,240}, sigma 0.9, times the team multiplier. closed = resolved + U(0,72 h). Records with resolved > end stay open ("In Progress", resolved and closed empty).
- ✅ Step 6: reassignments Poisson(0.6 + reassign_extra); reopen 0.04, or 0.06 for P4/P5.
- ✅ Step 7: made_sla limits 4 h, 12 h, 3 d, 7 d, 30 d.
- ✅ Step 8: impact on 70 % of P1/P2, `round(duration_min * U(0.3,1.0))`, empty otherwise.
- ✅ Step 9: `render_incident_text(..., text=params.text)`. Every 80th incident per service is a cluster member and reuses the cluster's family and slots.
- ✅ Step 10, PII: `pii.incident_share` 0.03, `inject_pii(text, "description", rng, names, n_spans=U{1..3})`, spans recorded per record.
- ✅ Step 11: five label rows per incident (root_cause, change_caused, repeat_issue, business_impact, owning_team).
- ✅ Changes: types .60/.35/.05. Close codes .90/.05/.03/.02, with the non-success shares x3 for emergency and renormalized. Planned window U(1,8) h; work window within +/-30 min of plan.
- ✅ Problems: known_error with p 0.40; cause_notes from the cluster's root cause, using the same cluster derivation as incidents.
- ✅ A mismatched shard raises `SynthUsageError`. Output is pure given rng (determinism test).
- ✅ Tests: UT11-09 asserts the field set, INC numbering from seq_start, and made_sla false exactly when the duration exceeds the limit. UT11-10 asserts criticality only on service rows. Names are `test_ut11_09_*` and `test_ut11_10_*`, docstrings start with the ID, and the module sets `pytestmark = pytest.mark.unit`.
- ✅ Sizes: servicenow.py 303, servicenow_incidents.py 266, servicenow_common.py 204, shards.py 27. The sibling split is accepted by controller ruling w05-s11. The ruff max-args limit of 6 is respected (the 6-arg helpers sit at the limit).
- ⚠️ Cannot verify from the diff, or needs a controller ruling:
  - cmdb_ci and cmdb_rel_ci have no `sys_updated_on` (§5.1.2 read literally), but flatten and the connector watermark expect it (report item 1). Spec gap for U11-18/U11-19.
  - Background `change_caused` is always "false". The spec gives no background change-flavored share, and T3 plants supply the positives. This is an acceptable literal reading. It is worth a spec note, because the classifier eval then gets positives only from T3.
  - `problem_id` and `caused_by` are always empty, so incidents cannot link to problems (problem sys_ids come from the problem shard's rng). The spec does not require the link. Flag for T11-12/U11-19.
  - The cluster count restarts per shard (see M2). The spec says "every 80th incident per service" without saying over what scope.

### Builder interpretations (report items): judgement
Accepted (consistent with the spec, filling gaps where it is silent):
- 2: group shape, type "itil".
- 3: CI display labels, ServiceNow criticality labels, stable rel-type ids.
- 4: span end = midnight after `end`.
- 5: resolved-but-not-closed state 6, and the ack clamp.
- 6: duration for open records = end - opened.
- 7: priority renormalization.
- 8: impact level by priority {1:3, 2:2, 3:1, 4:0, 5:0}. The label equals the level passed to the renderer, so the truth stays consistent.
- 9: change_caused always false (see the ⚠️ above).
- 10: cluster derived from a sys_id hash, which keeps incident and problem shards consistent without shared state; the incident close codes.
- 11: problem_id empty (see the ⚠️ above).
- 12: owning_team = plain team sys_id.
- 13: label and PII row shapes.
- 14: business_duration as calendar time.
- 15: `sys_updated_on` ignores stamps after the span end. This is sensible because it avoids future watermarks. It deviates from "max of the record's timestamps" only for changes whose planned or work end falls beyond the span.
- 16: change lead U(1,168) h, risk fixed by type, the change state machine.
- 17: problem resolution median 30 d.
- 18: arrival clipped to [start, end]; times sorted.

None of these contradicts a binding constant.

### Strengths
- All constants come from `SynthParams` with no duplicated literals, so `--params` overrides apply.
- The split is clean: record shaping, the time model, labels and PII are small single-purpose functions, and they reference the algorithm step numbers.
- The service rows are deep copies, so the two CI lists share no data, and a test checks it.
- The tests check real behaviour. They recompute made_sla and impact independently from the written timestamps, and they recompute repeat-cluster membership from the records.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- M1 `tools/synth/servicenow_common.py:112-121` (`arrival_times`) with `tools/synth/servicenow_incidents.py:106-112` (`_times`): with a non-UTC `business_timezone`, local times on the last span day convert to UTC times after `span_end`. For zones east of UTC, times on the first day land before `params.start`. The probe (America/New_York, 20,000 March incidents) produced 9 records with `opened_at` 2024-04-01 01:xx UTC. Those records have a negative `duration`, so made_sla is "true" and `u_customer_impact_minutes` can go negative for P1/P2. They also lie outside the shard month. The default zone is UTC, so default runs are unaffected. Fix: redraw or clamp times to [span_start, span_end), or floor the duration at 0.
- M2 `tools/synth/servicenow_incidents.py:228-231`: the per-service "every 80th" counter runs per shard. A service with fewer than 80 incidents a month therefore never gets cluster members, even though over the span it has many multiples of 80. At `small`/`full` (about 2,800 incidents a month over 400 Pareto-weighted services), most services get no repeat-flavored incidents. Because generation is pure per shard, a global count needs an index. Ask the controller to rule (for example, offset by a per-service count from U11-19's plan) and record a spec note.
- M3 `tests/unit/tools/synth/test_synth_servicenow.py`: no test pins the emergency x3 close-code boost (`tools/synth/servicenow.py:602-608`) or the criticality-1 P1/P2 doubling (`priority_probs`, `tools/synth/servicenow_incidents.py:71-79`). Both are covered only by execution. A direct assertion on `priority_probs(params, 1)`, plus a close-code share comparison of emergency against standard, would lock these constants.
- M4 `tools/synth/servicenow.py:646`: change `opened_at` = start - U(1,168) h can fall before `params.start` in the first shard month (the lead time is invented). Harmless, but it produces pre-span opened_at values.
- M5 `tests/unit/tools/synth/test_synth_servicenow.py:281,307,323,342`: the change, problem, group and relation tests carry UT11-09, which is an incident test ID. Shared IDs are allowed, but the ID's "Expected" column covers incidents only. Consider noting this, or use a `test_rf_` id.
- M6 `tools/synth/servicenow.py:641`: the change-type draw passes `p` to `rng.choice` without renormalizing. It relies on the params `Dist` validator summing to 1, whereas the close-code path renormalizes. Handling both paths the same way would be safer against float drift from `--params` files.

### Assessment
**Task quality:** Approved
**Reasoning:** All U11-07 constants, formats and field sets (including R-60 and the team cost_center) match the spec, and so do the incident, change and problem algorithms. UT11-09 and UT11-10 assert what the spec requires. The remaining items are edge cases (the non-UTC timezone edge, the per-shard cluster count) and spec gaps that need a controller ruling.

## Re-review round 1

Scope: M1, M3 and M4 only. I checked fix commit `6d897a7` on `2c20e14` in worktree agent-adaaa1efa49d8b8ed.

**Verdict: Approved**

Evidence:
- `PYTHONUTF8=1 uv run pytest tests/unit/tools/synth -q -p no:logging`: 163 passed. `uv run python -m tools.check_module_size`: exit 0. servicenow_common.py is 221 lines and servicenow.py is 313.
- Scratch probe comparing the pre-fix `servicenow_common.py` from `2c20e14` with the fixed one:
  - UTC, Jan to Mar shards: `arrival_times` output is identical to pre-fix, so the default output is unchanged.
  - Non-UTC zones: America/New_York, Asia/Tokyo, Pacific/Kiritimati (+14), Pacific/Pago_Pago (-11) and Europe/Berlin (with a DST change on 2024-03-31), each with Jan, Feb and Mar shards of 20,000 records. Pre-fix, up to 523 times per shard fell outside the shard's UTC month. Post-fix there are none.
  - Output is identical across two runs with the same seed.
  - The histogram of local hours in `business_timezone` matches pre-fix exactly (diff 0 in every case), so the hour curve is still respected.

Findings:
- M1 resolved. `tools/synth/servicenow_common.py:129-146`: `_within` shifts an out-of-range time by exactly one day, which keeps its local hour and uses no rng draw, then clamps to `[lo, hi)` as a last resort. `hi` is the last shard day + 1 at 00:00 UTC, which is never later than `span_end`. So `opened_at < span_end`, and the duration, made_sla and customer impact can no longer go negative. `_shard_days` and `_day_weight` are unchanged, so monitoring.py's private imports still work. The change also applies to jira.py and monitoring.py through `arrival_times`, which is the same benefit and has no downside. The bound is the shard month, not the whole span, so the shard partition is also kept. Test `tests/unit/tools/synth/test_synth_servicenow.py:424` covers two zones east and west of UTC and the first and last shard.
- M4 resolved. `tools/synth/servicenow.py:225`: `opened = max(start - lead, span_start(params))`. The rng draw sequence is unchanged. Test at `test_synth_servicenow.py:450` asserts that clamping happened and that `opened_at <= start_date`.
- M3 resolved.
  - `tools/synth/servicenow.py:172-185`: `close_code_probs` keeps the same probability vector and key order, so `_close_code`'s rng draw sequence is unchanged.
  - Test at `test_synth_servicenow.py:462` pins the multiplier at 3.0. It asserts the renormalized sum is 1, that each non-success/success ratio is 3x the normal ratio, and that standard equals normal.
  - Test at `test_synth_servicenow.py:475` pins the 2.0 multiplier and asserts that P1/P2 double, P4 drops by base P1+P2, and P3/P5 are unchanged.
  - All four new tests follow the ID and docstring rules: the name contains `ut11_09` and the docstring starts with `UT11-09`.
- Minor, no action needed: the non-UTC test builds `hi` with `month.month + 1`, which would break for a December shard. Only the January and March parametrizations exist today.
