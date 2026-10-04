# T11-08 report: ServiceNow generators (U11-07)

Status: DONE_WITH_CONCERNS (spec gaps filled, listed below; sibling split)
Commit: 2c49d82 feat(synth): ServiceNow generators (T11-08) (base ee452a6, worktree agent-adaaa1efa49d8b8ed)

## What was built
- `tools/synth/servicenow.py` (303 lines, budget 400): `IncidentBatch` (frozen dataclass: records, labels, pii), `gen_groups`, `gen_cis`, `gen_rels`, `gen_incidents` (checks the shard, delegates), `gen_changes`, `gen_problems`.
- `tools/synth/servicenow_incidents.py` (266, private sibling, default budget 400): the U11-07 incident algorithm steps 1-11 (`generate`, `priority_probs`).
- `tools/synth/servicenow_common.py` (204, private sibling, default budget 400): `{value, display_value}` pair helpers, internal UTC timestamp format, `sys_updated_on` rule, shard check, arrival model (step 1), `ServiceIndex` (incident-weight service draws, support team, CIs of a service), `cluster()`.
- `tools/synth/shards.py` (27): only the frozen `Shard(source, entity, month, n_records, seq_start, index)`; docstring says U11-19 adds plan_shards/run_shard later.
- `tests/unit/tools/synth/test_synth_servicenow.py` (417): 18 tests, all ID'd (UT11-09 x15, UT11-10 x3).

Sibling split: servicenow.py + servicenow_incidents.py + servicenow_common.py (same precedent as param_groups / catalog_rows / text_vocab). The spec §2 module map needs rows for the two siblings. check_module_size gives unlisted files the 400 default, so nothing was registered; docs/impl was not edited.

## Record shape
Every field is a `{value, display_value}` pair, sys_id included, the same as U11-77. Reference fields hold the target sys_id plus its name. Empty fields are `{"", ""}`. Timestamps are `YYYY-MM-DD HH:MM:SS` UTC; microseconds are drawn (step 1) but not written. Each pair is a new dict, so the lists share no mutable object: the cmdb_ci_service rows are copies of the matching cmdb_ci rows.

## Interpretations and spec gaps (controller to rule / spec notes)
1. Dimension `sys_updated_on`: groups are stamped `start 00:00:00`, the same as U11-77 departments. §5.1.2 lists no `sys_updated_on` for `cmdb_ci` or `cmdb_rel_ci`, so I followed the "field sets equal §5.1.2" invariant literally and left it out. U11-18/U11-19 pass the update time separately. Gap: flatten_record expects sys_updated_on, and the connector watermarks on it. Adding it later is a one-line change.
2. Groups: one row per org (parent empty, manager empty) and one per team (parent = org reference, manager = stable 32-hex id from the name, which is a sys_user the generator does not write). `type` = "itil", `active` = "true". The cost_center value and display are both the org's code (e.g. CC-1100).
3. CIs: `owned_by` = service owner team, `support_group` = support team, `company` = the owner team's org, `cost_center` = the service's. `sys_class_name` display labels are Business Service, Application, Server and Database Instance. `busines_criticality` is "1 - most critical" ... "4 - not critical" (value = display). Relation `type` = stable id with display "Depends on::Used by" / "Runs on::Runs".
4. Span end = midnight UTC after `params.end` (the same as catalog `_change_schedule`).
5. Incidents: after resolved > end the record is open (state 2 "In Progress"; resolved_at, closed_at, close_notes, close_code and business_duration empty). If resolved <= end but closed > end, the state is 6 "Resolved" with closed_at empty. Otherwise it is 7 "Closed". The ack is clamped to <= resolved_at and dropped when it falls after the span end.
6. made_sla / customer impact duration = resolved - opened, or end - opened for open records (the UT11-09 check uses the same definition).
7. The priority is drawn per incident with P1/P2 x `criticality1_high_multiplier` for criticality 1 and the difference taken from P4 (floored at 0, renormalized).
8. business_impact level by priority, which the spec leaves open: {1: 3, 2: 2, 3: 1, 4: 0, 5: 0}. The answer is str(level) and is also the impact_level given to render_incident_text.
9. Background change_flavored = False always, so the `change_caused` label is always "false". The spec gives no background share; T3 plants add change-flavored text. repeat_flavored = cluster member.
10. Clusters: the per-service count runs within the shard, and every 80th (`problem.incidents_per_problem`) incident of a service is a member. Its family and slots come from `cluster(bank, service_sys_id)`, which uses an rng seeded by `shard_key_hash(("servicenow_cluster", sid))`. Incident and problem shards therefore agree without shared state. The render still applies the 20 % slot variation. Members get close_code "Solved (Work Around)" and others "Solved (Permanently)" (value = display; the spec gives no incident close codes).
11. `problem_id` and `caused_by` are always empty. Problem sys_ids come from the problem shard's rng, so incidents cannot link to them (gap). caused_by belongs to T3/T11-12.
12. owning_team answer = the support (resolving) team's plain sys_id, since core.team team_id = sys_id and options label = team_id.
13. Label rows are `{record_id: "servicenow:incident:<sys_id>", question, answer}`. PII rows are `{record_id, field, start, end, type}`. U11-20 adds content_hash and pii_spans JSON. PII: `pii.incident_share` (3 %), n_spans = U{spans_min..spans_max} (1..3), field description, `text=params.text` passed.
14. business_duration = calendar duration (no business-hours calendar in the spec). value is the glide duration `1970-01-01 HH:MM:SS`+days; display is "D Days H Hours M Minutes".
15. `sys_updated_on` = the latest present timestamp that is <= span end (falling back to the first one) + U(0, 2 h). For incidents this equals the literal rule, because every written timestamp is <= end (except opened_at at a timezone edge). For changes it keeps a planned end_date after the span end from pushing sys_updated_on into the future (deviation from the literal "max of the record's timestamps").
16. Changes: the arrival model places start_date (planned start); services follow incident weight; cmdb_ci is a uniform pick among the service's non-service CIs (the same for incidents); assignment_group = support team. The planned window is U(1, 8) h and the work window is shifted U(+/-30 min) at each end (work_end >= work_start + 1 min). opened_at = start - U(1, 168) h lead (invented; the spec is silent). State: 3 Closed if work_end <= end, -1 Implement if only work_start <= end, else -2 Scheduled; close_code only when Closed. risk by type: standard 4 Low, normal 3 Moderate, emergency 2 High (deterministic, invented). close_code values are the params keys (successful, successful_with_issues, unsuccessful, backed_out) with title-case display; emergency multiplies the non-success weights x3 and renormalizes. Text comes from render_change_text.
17. Problems: services by incident weight; cause_notes = "Root cause: <cluster family cause>."; known_error p 0.40; resolution log-normal with a 30-day median (invented) and sigma = mttr.sigma. State is 106 Resolved, or 103 Root Cause Analysis when resolution is past end.
18. Arrival: days = shard month intersected with [start, end]. A shard month outside the span with n > 0 raises SynthUsageError; n = 0 returns empty. Times are sorted before numbering, so INC numbers rise with opened_at.

## Tests / gates
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/tools/synth/test_synth_servicenow.py` failed with ModuleNotFoundError: tools.synth.servicenow.
- GREEN: 18 passed. tests/unit/tools/synth: 130 passed. `pytest -m "(unit or integration) and not slow" -p no:logging`: 3175 passed, 5 skipped, 1 xfailed (pre-existing IT00-02).
- Branch coverage: servicenow.py 100 %, servicenow_incidents.py 100 %, servicenow_common.py 98 % (the empty-catalog guard is untested), shards.py 100 %.
- ruff format/check clean, mypy clean (116 files), lint-imports 12 kept, check_module_size exit 0, check_type_ownership exit 0. Pre-commit hooks passed, including detect-secrets; the baseline was not changed.
- No pii.py change. No plants and no T3 caused_by subset.

## Fix round 1

Commit `6d897a7` fix(synth): keep ServiceNow timestamps inside the span (T11-08) (on top of T11-09 2c20e14).

- **M1** `tools/synth/servicenow_common.py`: `arrival_times` now folds any time the business_timezone offset pushed outside the shard's UTC days (`[first day 00:00, last day + 1 00:00)`, i.e. shard month within the span) back by exactly one day, which keeps its local hour; a final clamp covers a one-day window. No extra rng draw, so it is deterministic; UTC output is unchanged (every time is already inside). `_shard_days` and `_day_weight` are unchanged (monitoring.py imports them). Test `test_ut11_09_non_utc_zone_keeps_incidents_inside_span` (America/New_York and Asia/Tokyo x Jan and Mar shards, 20,000 incidents each) checks opened_at inside the shard's span, impact >= 0 and resolved >= opened. All four cases fail on the pre-fix code.
- **M4** `tools/synth/servicenow.py`: change `opened_at = max(start - lead, span_start)`. Test `test_ut11_09_change_opened_at_not_before_span_start`.
- **M3**: `close_code_probs(params, kind)` split out of `_close_code` (same rng draw sequence) and exported. Tests `test_ut11_09_emergency_close_code_boost` (each non-success/success ratio is 3x the normal ratio) and `test_ut11_09_criticality1_doubles_p1_p2` (on `priority_probs`: P1/P2 doubled, P4 lowered by the difference, P3/P5 unchanged).

Gates: ruff format/check, mypy, lint-imports and check_module_size are clean; tests/unit/tools/synth has 163 passed; pre-commit hooks passed.
