# T11-06 Catalog — build report

Status: DONE_WITH_CONCERNS
Commit: 9b7c413 feat(synth): add seed-derived catalog and plant targets (T11-06)
Worktree: D:\herness\.claude\worktrees\agent-a4223eb3b67c31e20 (branch worktree-agent-a4223eb3b67c31e20)

## What was built

U11-04 `tools.synth.catalog.build_catalog(seed, params) -> Catalog`, following the 11 algorithm steps:

- **Streams.** `rng_c = stream_rng(seed, "catalog:<class>")` and `rng_p = stream_rng(seed, "plants:<class>")`. Draws from rng_c never depend on plant choices, so small and full give the same catalog.
- **Orgs.** Sampled without replacement from a pool of 60 neutral words.
- **Teams.** Poisson(12.5) clipped to [6, 20]; the current largest org is adjusted by ±1 until the total is 150. Tiny gets 4 per org.
  - Names are `<Area> <Function> L1..L3`, unique (pools of 40 and 25).
  - mttr_multiplier = exp(N(0, 0.2)); reassign_extra is 0.8 when the multiplier is above 1.3.
  - manager_name comes from invented first and last name pools (20 x 20).
  - Each team takes its org's cost_center.
- **Services.** `<Adjective> <Noun>` names, unique (pools of 50 x 50).
  - Owner and support teams are drawn with weights Pareto(1.5)+1.
  - Incident weights are Pareto(1.2)+1, x1.5 for criticality 1. Tail calibration bisects gamma over [0.5, 3] for 40 iterations when share(1) is outside 0.40 ± 0.02.
  - Event weights are an independent Pareto(1.2)+1.
- **CIs and relations** (step 6). The service CI reuses the service sys_id. Each service gets one appl, 1-3 servers, and a DB for criticality <= 2. Relations: service->appl and appl->db are `Depends on::Used by`; appl->server is `Runs on::Runs`.
- **Projects.** One per org. The key is the first 4 letters upper-cased, or the first 3 letters plus a digit on collision. id_base = 10000*(i+1). Each service's component is named after the service, and its project is the owner org's project.
- **Plants**, drawn from rng_p in this order:
  1. S6p and S6u (criticality 3, with at least 5 free criticality-3 peers, else SynthUsageError).
  2. S3/C3 (criticality 2; C3 is its appl CI).
  3. S2 (criticality 1).
  4. The three T1 services (criticality 2, near the median).
  5. S2c (criticality <= 2).
  6. S4 and S5 (criticality 1, 2 or 4, so the T6 peers stay free).
  7. The E2d service (the lowest-weight free criticality-4 service).
  8. Teams T1 and T5 (distinct).

  T1 is the only support team of exactly its 3 services, and T5 of exactly S5. Any other service these two teams supported is moved to a weighted non-plant team. Both teams get mttr_multiplier 1.0 and reassign_extra 0. S4's event weight is 0. S6p and S6u each carry 5 % of incident weight at small and tiny, 1.5 % at full.
- **effective_at and peak_window** as in step 10. `PlantTargets.in_peak(day)` handles a window that wraps the year end, which can happen at tiny.
- **change_schedule.** 40 emergency and 40 normal changes from `stream_rng(seed, "plants:t3")`, stratified uniformly over [start 00:00Z, end+1d - 2h]. Emergency changes get k ~ U{2..6} follow-up incidents, normal changes 0. `seq` is the 0-based position in work_end order.
- **month_counts.** Incident, change_request, problem, jira/issue and event totals are split across months by days in span (largest remainder). metric_daily is exact: metric_services x 4 x days. Only selected sources are included. Planner counts are then added and the keys sorted. seq_start is a prefix sum per (source, entity), starting at 1.

## Files

| File | Lines | Budget | Contents |
|------|-------|--------|----------|
| tools/synth/catalog.py | 378 | 380 | `build_catalog` and `default_planners`; re-exports `Catalog` and `Planner` |
| tools/synth/catalog_rows.py | 136 | not in module map (default 400) | Frozen row types OrgRow, TeamRow, ServiceRow, CiRow, RelRow, ProjectRow, ChangeSlot, PlantTargets, Catalog, plus MonthKey and Planner |
| tools/synth/catalog_pools.py | 67 | not in module map (default 400) | Neutral name pools and the cost model |
| tests/unit/tools/synth/test_synth_catalog.py | — | — | 23 test cases, all with ID UT11-05 |

## Evidence

- **RED:** `PYTHONUTF8=1 uv run pytest tests/unit/tools/synth/test_synth_catalog.py` failed with `ModuleNotFoundError: No module named 'tools.synth.catalog'`.
- **GREEN:** the same command gives 23 passed in 0.37 s. A test asserts the `full` catalog builds in under 1 s.
- **Coverage:** catalog.py 97 % line and 5 partial branches. The 4 missed lines are the error branches for T1, T6 and T2-decoy shortages and the project-key collision. catalog_rows.py is at 100 %.
- **Gates:**
  - ruff check: clean
  - ruff format --check: clean
  - mypy: "no issues found in 42 source files"
  - lint-imports: 8 kept, 0 broken
  - check_module_size: exit 0
  - check_type_ownership: exit 0
  - `pytest -m "(unit or integration) and not slow" -q -p no:logging`: 639 passed, 1 skipped, 1 xfailed (both pre-existing)
  - `--require-test-ids -k UT11_05`: 23 passed

## Deviations and concerns

1. **Sibling split (flagged).** catalog_rows.py and catalog_pools.py are new modules that are not in the §2 module map. This follows the T11-05 param_groups.py precedent. Before the split, catalog.py was about 550 lines after formatting. The controller should add both rows to the §2 map, or rule otherwise.
2. **Planner injection point (carry-over).** The fixed signature takes no planners, and U11-10..U11-15 don't exist yet.
   - `build_catalog` takes a keyword-only `planners: Sequence[Planner] | None = None`. None falls back to `default_planners()`, which returns `()` for now.
   - T11-10..T11-15 should return their `planned_counts` from `default_planners()`, using a lazy import to avoid a cycle (the plant modules import Catalog).
   - Planners receive a stub Catalog with the background month_counts and an empty seq_start. Their values are added only for selected sources. Values may be negative (T6 thinning), but a negative total raises SynthUsageError.
3. **Criticality mix.** `preset.services` is split into exact quotas of p (largest remainder) and shuffled with rng_c, instead of drawing each service independently. Independent draws at tiny (20 services) would often break the plant preconditions: about 25 % of seeds give fewer than 7 criticality-3 services, and about 12 % give no criticality-1 service. SynthUsageError is still raised for an inconsistent mix from a params file (tested).
4. **Calibration order.** The S6 overrides of step 9 are applied inside the step-5 gamma search. Otherwise S6's 5 % shares at small push the top-5 % share above 0.42, and UT11-05's [0.38, 0.42] check fails.
   - T1 services are picked on the base (uncalibrated) weights, so small and full choose the same plants.
   - After calibration, T1 weights are clamped to ±20 % of the final median and the S6 overrides are re-applied.
   - If fewer than 3 criticality-2 services lie in the band (possible at tiny), the 3 closest are taken and clamped.
5. **UT11-05 "small and full catalogs equal."** Equality holds for orgs, teams, CIs, relations, projects, change_schedule and plants, and for services apart from incident_weight. The spec requires S6p/S6u to carry 5 % at small and 1.5 % at full, so those weights, and the gamma that follows from them, must differ. month_counts also differ by preset volume.
6. **Values the spec leaves open, chosen here:**
   - Cost model: annual run cost medians by criticality 2.0M / 900k / 350k / 120k, and downtime per hour 40k / 12k / 3k / 800, each log-normal with sigma 0.4.
   - Org cost_center `CC-1100`, `CC-1200`, and so on.
   - Project id_base.
   - CI names: `<Service> App`, `<slug>-srvNN`, `<slug>-db`.
   - The meaning of ChangeSlot.seq.
   - seq_start starts at 1 (INC0000001).
   - month_counts covers only time-sliced entities; dimension entities come straight from the catalog tuples.
   - The E2d service counts as a plant service for disjointness.
7. **Carry-over for T3 (U11-12).** The 30 % caused_by subset "chosen from that stream by index" is not drawn here. The T3 card should draw it from `stream_rng(seed, "plants:t3")`, or the schedule could carry it if the controller prefers.
8. **T10-03** (herness.core.config) is not used by this card, so nothing is pending on it.

## Fix round 1

Commit: 6a942d9 fix(synth): review fixes for catalog (T11-06)

- I-1, plant teams disjoint: fixed.
  - T1 and T5 are drawn by team weight (via rng_p) from teams that have no owner or support role on any plant service. If fewer than two such teams exist, the fallback is any team other than the one already picked.
  - Any plant service that T1 or T5 owns, or supports outside its designated services, is reassigned to a non-plant team. T1 supports exactly its 3 services and owns none of them; T5 supports only S5.
  - S3's and S4's owner and support teams are redrawn where needed. They never overlap with T1/T5 or with each other, and where the pool allows they also avoid every other plant service's teams.
  - The logic lives in `_draw_team` and `_plant_teams` in catalog.py. Plants stay identical between small and full.
- M-1: `split_counts` now floors exactly (no epsilon) and raises AssertionError if the remainder falls outside [0, n], which cannot happen.
- M-5: the team-count adjustment moves the largest org that still has room, so every org stays inside [6, 20]. If no org has room, SynthUsageError is raised. This is a small refinement of "adjust the largest org", made so the clip range holds.
- M-2: the small/full test still strips incident_weight for row equality. It now also asserts that, for services other than S6 and T1, log(w_full) / log(w_small) is one constant: the weights differ only by the gamma exponent.
- New UT11-05 tests:
  - Plant-team disjointness over seeds 0..39 at small and 0..119 at tiny.
  - Per-org team counts over 30 seeds: small in [6, 20] summing to 150; tiny exactly 4 per org.
  - RED: both fail on the round-0 code (14 failures). GREEN after the fix.
- Budget: the record plan (`split_counts`, `month_counts`, `apply_planners`) moved to a new sibling module, tools/synth/catalog_plan.py (76 lines, not in the module map; flagged like catalog_rows and catalog_pools). Line counts are now catalog.py 347 (budget 380), catalog_plan.py 76, catalog_rows.py 136, catalog_pools.py 67.
- Gates:
  - ruff check and format: clean.
  - mypy: 43 files, clean.
  - lint-imports: 8 kept.
  - check_module_size: 0. check_type_ownership: 0.
  - UT11-05: 55 passed (`--require-test-ids`).
  - Coverage: catalog.py 97 %, catalog_plan.py 95 %.
  - Fast suite: 671 passed, 1 skipped, 1 xfailed.
