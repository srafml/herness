# T11-10 report: Operations plants T1, T3, T4, T5

Status: DONE_WITH_CONCERNS (only spec notes; every gate is green)
Commit: b64ba41 feat(synth): operations plants T1, T3, T4, T5 (T11-10)
Worktree: D:\herness\.claude\worktrees\agent-a487baa62b88cc0c0 (base 6a38cbf)

## Files changed (lines after / budget)
| File | Lines | Budget | Change |
|------|-------|--------|--------|
| tools/synth/plants_ops.py (new) | 257 | 350 | T1, T4, T5 and all four planners; re-exports plant_t3 / planned_counts_t3 |
| tools/synth/plants_ops_t3.py (new, private sibling) | 188 | 400 default | T3 changes, follow-up incidents, control-window shift |
| tools/synth/catalog.py | 361 | 380 | caused_by flags in `_change_schedule`; default_planners() returns the 4 planners |
| tools/synth/catalog_plan.py | 118 | 400 default | `span_months`, `planner_id`, `plant_range`, `background_count`; apply_planners records `plant_seq` |
| tools/synth/catalog_rows.py | 144 | 400 default | `ChangeSlot.caused_by`, `Catalog.plant_seq` (both defaulted) |
| tools/synth/shards.py | 59 | 350 | frozen data-only `PlantOutput` (default_factory lists) |
| tools/synth/servicenow_incidents.py | 354 | 400 default | public `IncidentSpec`, `make_incident`, `retime`; `_labels(change_caused=)` |
| tools/synth/servicenow.py | 345 | 400 | `_change` split into public `ChangeCtx`/`change_context`/`ChangeDraw`/`change_record` (same background draw order); `WINDOW_H`, `WORK_SHIFT_MIN` public |
| tools/synth/servicenow_common.py | 236 | 400 default | `_shard_days`/`_day_weight` made public as `shard_days`/`day_weight`; `arrival_times(..., days=)` keyword; `parse_ts` |
| tools/synth/monitoring.py | 234 | 300 | imports updated to the public helpers; `EVENT_KEY_FORMATS`, `iso_ts`, `service_hosts`, `event_end` public (same draw order) |
| tests/unit/tools/synth/test_plants_ops.py (new) | 564 | - | UT11-13, UT11-15, UT11-16, UT11-17 (18 tests) |
| tests/unit/tools/synth/test_synth_catalog.py | +28 | - | background-total test uses planners=(); planner test checks plant_seq; new default-planner totals test |
| tests/unit/tools/synth/test_synth_jira.py | +13 | - | CHG/INC upper bounds = planned totals (background plus plant records) instead of the preset constants 250/1200 |

## Design decisions
- Plant numbering (new, needed so plant records never share INC/CHG numbers or event keys): `apply_planners` lays out each month as background records first, then each planner's positive count in planner order, and records `Catalog.plant_seq[(planner_id, key)] = (first_seq, count)`. Negative counts (T6 thinning) shrink the background. Helpers: `plant_range(cat, planner, key)`, `background_count(cat, key)`. U11-19 must build shards with `n_records = background_count(cat, key)`. T11-11 plants should use `plant_range` with their own planner.
- T3 caused_by (ruling 1): after the slots are sorted, `_change_schedule` draws round(0.3 x total) indices from the same `plants:t3` stream and hands each ChangeSlot its slice as `caused_by: tuple[bool, ...]` (control changes get `()`). plant_t3 reads the flags and never replays the stream. small and full catalogs still have equal change schedules.
- T3 shards: plant_t3 works on the ServiceNow `change_request` shard (the month's C3 changes; the ChangeSlot sys_id replaces the drawn one; window before the fixed work_end; type emergency/normal; CI C3) and on the `incident` shard (follow-ups plus the background shift). Follow-up offsets are integer seconds in [1, 7200], so opened_at stays in (t, t+2h] after truncation to the second. A follow-up is made by the shard of its change's work_end month and may open up to 2 h into the next month. links = all planted pairs (every follow-up with its change); members = follow-up incident record ids; caused_by = ref(change sys_id, CHG number) on the flagged ones. The change_caused label is "true" for every T3 follow-up (ruling 3).
- Control shift: background incidents with cmdb_ci = C3 that open in (t, t+2h] of any control change (checked globally, not only the shard month) move in +3 h steps until they are outside every control window. If that would pass the span end, they move in -3 h steps instead. `retime` moves the whole record: ack offset, MTTR and close lag are kept, and dependent fields are recomputed.
- retime (shared by T1 and T3): takes a new opened_at and MTTR, then recomputes resolved/closed/ack (a resolution after the span end leaves the record open and clears the close fields), state, made_sla, business_duration and impact minutes (scaled by the duration ratio, which keeps the drawn factor), and redraws sys_updated_on (one rng draw).
- T1: new MTTR = round(old_seconds x m) with m = 2.0 + (opened - start)/(end - start). `end` is the exclusive span end (midnight after params.end). Reassignment + Poisson(1.0) is drawn before the sys_updated_on draw, record by record in input order. T1 records that are already open stay open.
- T4: times come from arrival_times. Each time is redrawn up to 3 times (arrival_times with n=1) while it lies within +/-30 min of an S4 incident in the index (searchsorted). Times are then sorted and keyed from the plan's seq. Tool, title and severity are drawn uniformly. dedup_key = lowercase title with spaces as hyphens.
- T5: planner extra = round(1.8 x share_S5 x month_background / days_in_month x peak_days). share_S5 = incident-weight share, which matches the background draw probability. On the incident shard, extras are clones from make_incident on S5 (priority mix, MTTR model of team T5 with multiplier 1.0). They arrive on the month's peak days through arrival_times(days=peak_days) (same day weights and hour curve), and S5 background incidents get the T5 assignment group. Extras are returned in PlantOutput.records and are not appended to `records`, so they are not doubled. On the metric_daily shard, S5 request_count on peak days becomes float(round(v x 2.8)). Other shards: nothing.
- plant_t5 builds its own TemplateBank(), because its signature has no bank.

## Invented constants
- T4 titles: "Health check flapping", "Heartbeat missed", "CPU usage above threshold", "Disk latency above threshold", "Queue depth above threshold".
- T3 change window: work_start = work_end - U(1, 8) h, planned window shifted by U(-30, 30) min (reuses the background constants), both clamped to the span start. Close code comes from close_code_probs (emergency multiplier applies).
- T5 request_count is rounded to a whole count after x2.8.

## Deviations
- plant_t5 returns the extra incidents rather than appending them to `records` (the spec's side effects say "appends incidents"). The runner concatenates PlantOutput.records.
- plant_t3 dispatches on the shard entity (change_request vs incident). The U11-12 signature is one function for both.

## Spec notes for the controller
1. U11-12 wording amended (ruling 1): "chosen by the plant stream ... via the incident index" becomes "drawn at catalog build from plants:t3 and carried on ChangeSlot.caused_by". U11-04 ChangeSlot gains caused_by.
2. Module map: new private sibling tools/synth/plants_ops_t3.py (188 lines, default 400). T1+T3+T4+T5 would be about 420 lines, over the 350 budget.
3. U11-04 Catalog gains plant_seq (plant records follow the background in planner order). U11-19 must set Shard.n_records = background_count(cat, key) and not month_counts[key]. T11-11 (T2 remotelinks need deterministic incident numbers) should use plant_range.
4. Public helper changes: servicenow_common shard_days/day_weight (formerly private and imported by monitoring; the import is updated), arrival_times(days=), parse_ts; servicenow ChangeCtx/change_context/ChangeDraw/change_record/WINDOW_H/WORK_SHIFT_MIN; servicenow_incidents IncidentSpec/make_incident/retime; monitoring EVENT_KEY_FORMATS/iso_ts/service_hosts/event_end. Background draw order is unchanged.
5. UT11-13 "within 1e-9": timestamps have one-second resolution, so the 1e-9 check uses 100 incidents on a grid where round(d x m) is exact (2160 s opening steps, whole-hour MTTRs). Real background T1 incidents are checked to within 0.5 s of d x m.
6. T3 follow-ups and shifted control-window incidents can land in the following month's time range (at most 2 h, or 3 h steps for the shift); the incident time index (month +/- 1 day) covers that.
7. The Jira mention tests assumed CHG numbers <= preset changes (250). The plan now also holds the 80 T3 changes (and INC plant records), so the bounds are the planned totals.

## Tests
- RED: pytest tests/unit/tools/synth/test_plants_ops.py failed with "ImportError: cannot import name 'background_count' from 'tools.synth.catalog_plan'".
- GREEN: PYTHONUTF8=1 uv run pytest tests/unit/tools/synth -q -p no:logging gave 182 passed. test_plants_ops has 18 tests (UT11-13 x3, UT11-15 x7, UT11-16 x4, UT11-17 x4) and also passes with --require-test-ids.
- Coverage (branch): plants_ops 100 %, plants_ops_t3 100 %, servicenow_incidents 100 %, servicenow 100 %, monitoring 100 %, shards 100 %, catalog_plan 97 %, catalog 97 %, servicenow_common 98 %.
- Broad run: pytest -m "(unit or integration) and not slow" gave 3346 passed, 5 skipped, 14 deselected, 1 xfailed (the IT00-02 marker, which was already there).
- Gates: ruff format/check clean; mypy 0 issues (128 files); lint-imports 13 kept; check_module_size exit 0; check_type_ownership exit 0; pre-commit hooks passed on commit.
