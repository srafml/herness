# T11-09 report: Jira and monitoring generators (U11-08, U11-09)

Worktree D:\herness\.claude\worktrees\agent-adaaa1efa49d8b8ed, base 2c49d82 (T11-08). Commit 2c20e14 `feat(synth): Jira and monitoring generators (T11-09)`.

## Built
- `tools/synth/jira.py` (280 / budget 320): `gen_issues(cat, params, shard, rng, bank, names, *, incident_index=None) -> IssueBatch(records, pii)`.
  The issue JSON follows the Jira Cloud shape:
  - `id` = project.id_base + seq; `key` = `<PROJECT>-<seq>`.
  - `fields`: issuetype.name, parent.key, project.key/name, components[].name, labels, status.name/id/statusCategory.key, created, resolutiondate, customfield_10016 (points), customfield_10050 (cost USD), customfield_10060 (team name), summary, description (wiki text), updated, issuelinks.
  - Top level: `changelog` {startAt, maxResults, total, histories} and `remotelinks` [{id, object{url, title}}].
  - PII rows: `{record_id: "jira:issue:<id>", field, start, end, type}`.
- `tools/synth/jira_changelog.py` (92; private sibling; default budget 400): status lifecycle, changelog histories, Jira timestamp format, status ids and categories.
- `tools/synth/jira_links.py` (91; private sibling; default budget 400): INC/CHG mention picking and the remote-link shape.
- `tools/synth/monitoring.py` (220 / budget 300): `gen_events(cat, params, shard, rng, incident_index) -> list[dict]` and `gen_metric_daily(cat, params, shard, rng, p1_days, volume_by_day) -> list[dict]`.
- `tools/synth/shards.py` (27 -> 45): adds a data-only frozen `IncidentTimeIndex(opened_at, sys_ids, numbers)`.
  - Each field is a Mapping keyed by the incident's `business_service` sys_id: a sorted int64 array of epoch seconds, the matching sys_ids and the matching numbers.
  - `eq=False` because the values are numpy arrays.
- Tests: `tests/unit/tools/synth/test_synth_jira.py` (378 lines, 17 tests, all UT11-11) and `tests/unit/tools/synth/test_synth_monitoring.py` (273 lines, 11 tests, all UT11-12).

Why jira.py is split (same as w02-s11 and T11-08): as one file it was 338 lines, over its 320 budget, so the lifecycle and mention helpers moved to two private siblings. The spec §2 module map needs rows for `jira_changelog.py` and `jira_links.py` (spec note).

## Interpretations and deviations (with reasons)

1. **INC/CHG mentions (step 6).** U11-08's signature has no incident index, and the catalog holds no incidents.
   - I added a keyword-only `incident_index: IncidentTimeIndex | None = None`, which needs `# noqa: PLR0913`.
   - INC or CHG is chosen with even odds.
   - **INC with an index:** a uniformly chosen incident of the same service with opened_at in [created − 30 d, created), found with searchsorted. If there is none, the issue gets no mention (same-service is kept strict).
   - **INC without an index, and CHG always** (there is no change index): a number from the catalog's planned sequence range for the 30 days before `created`.
     - Both ends of the window use `seq_start[(servicenow, entity, month)] + floor(month_counts × elapsed fraction of the month)`.
     - The number exists in the lake but is not guaranteed to belong to the same service.
   - No mention when servicenow is not in the sources or `created` is before the plan.
   - When U11-19 lands it can pass a merged index. The U11-09 index covers only the month ± 1 day, which is less than the 30-day window.
2. **Shard checks.** A shard with the wrong source or entity raises SynthUsageError in all three generators (controller ruling; consistent with U11-07).
3. **Hierarchy scope.** Parents are picked among issues of the same shard and the same project.
   - A feature's parent is an epic.
   - A story, bug or task gets a feature or an epic (0.5 each), falling back to the other kind when the project has none in the shard.
   - An epic gets an initiative of its project when one exists. The spec says nothing about epic parents; this follows the design's initiative → epic chain.
   - With no candidate the issue has no parent, so "every parent key exists" holds within the shard. At tiny (about 50 issues per shard, 3 projects) many leaves have no parent.
4. **Points.**
   - Only stories get points (the spec says "on stories"); bugs, tasks and features have None.
   - An epic's points are the sum of its stories, direct and through its features. An initiative's points are the sum of its epics.
   - An epic or initiative with no pointed descendants draws one value from the story-point distribution, so it still has a cost estimate (invented).
   - Cost = points × U(2500, 4000), rounded half-up to 1000.
5. **Status lifecycle.**
   - Cycle time runs from the first In Progress to Done, which matches spec 04 `cycle_days`.
   - The wait from created to first In Progress is U(0, 72 h) (invented).
   - Re-entry (12 %): In Progress → To Do → In Progress once, at U(0.2, 0.8) fractions of the cycle (placement invented).
   - Carry-over (15 %): Done falls at a uniform point in the calendar month after the creation month. This is my reading of "resolution after the month of creation plus 1".
   - Only transitions up to the span end are kept. Status is the target of the last kept transition (To Do if there is none). `resolutiondate` is that transition's time, set only when the status is Done.
6. **Missing component (step 5).**
   - The rate is `dirty_rates.missing_component` (0.25) at dirty `default` and 0 at `none`. At `heavy` it is multiplied by heavy_multiplier, capped at 1. This follows design §5.1.6 ("preset none = 0").
   - The count can be recovered from the records (`components == []`); IssueBatch has no counter field in its signature.
7. **Issue service and team.** Services are drawn uniformly over the catalog (the spec is silent). `customfield_10060` is the service's owner team name (the spec does not say owner or support).
8. **Event fields.**
   - `service` is the service name (the tool's tag).
   - `host` is one of the service's server CIs, else the service name.
   - `title` is "<Symptom> on <service>" using `text_vocab.SYMPTOMS`, because gen_events has no TemplateBank.
   - `severity_raw` is the plain severity key.
   - `event_key` depends on the tool: prometheus is 16-hex of seq, datadog the decimal seq, splunk `evt-<10 digits>`. Seq starts at shard.seq_start.
   - `dedup_key` = sha256(tool, service, title)[:16].
   - The near-incident offset is whole seconds in [−1800, 1800].
   - `status` is firing (and end_ts None) when ts + duration is after the span end, else resolved.
   - Times are ISO-8601 `...Z` at second precision; `incident_ref` is the incident `number`.
9. **Metric daily.**
   - `p1_days` and `volume_by_day` are keyed by `(service sys_id, date)`.
   - Each service's tool is TOOLS[catalog position % 3] (invented; the spec's uniform tool draw is stated for events).
   - Values are floats, with request_count a rounded float. Units are percent, ratio, ms and count (invented).
   - `volume_by_day` is accepted but unused, because U11-09's algorithm never references it (spec gap; T5 overrides request_count itself).
   - Services with event weight 0 (plant S4) get request_count 0 under the literal formula.
10. **Reuse of T11-08 helpers.** `monitoring.py` imports the private `_day_weight` and `_shard_days` from `servicenow_common.py` (the controller allowed reuse) rather than duplicating the arrival day factor. I suggest the T11-08 card or a later one makes them public. No T11-08 file was changed.

## Invented constants
**Jira**
- created → In Progress wait U(0, 72 h)
- re-entry fractions U(0.2, 0.8)
- updated lag U(0, 2 h), as for ServiceNow
- issue-link rate 10 % ("Relates", outwardIssue)
- labels: 0–2 from (reliability, tech-debt, customer, security, performance)
- status ids: To Do 10000, In Progress 3, Done 10001
- ids: history `<issue id><kk>`, remote link `<issue id>1`, issue link `<issue id>9`
- description wiki prefix `h3. Context`
- mention sentence "See <ticket> for the operational history."
- remote-link URL `https://servicenow.example.com/nav_to.do?uri=<table>.do?sysparm_query=number=<ticket>`

**Monitoring**
- event duration sigma 0.8 (the spec gives the 20 min median)
- p95 latency sigma 0.3 (the spec gives the 250 ms median)
- metric units
- per-tool event_key formats

## Spec gaps / notes for controller
- U11-08 says Errors "none", but the generators check the shard (already ruled).
- U11-08 needs an incident index, or something equivalent, for "same-service incidents of the preceding 30 days". The U11-09 index covers only the month ± 1 day.
- U11-09's algorithm does not use `volume_by_day`.
- The §2 module map needs rows for jira_changelog.py and jira_links.py.
- text.render_jira_text still takes lowercase issue types. jira.py passes lowercase and capitalises `issuetype.name` itself, which settles the w02-s11 carry-over at the caller.
- pii.py is unchanged; PiiType is still local.

## Tests / gates
- RED: `pytest tests/unit/tools/synth/test_synth_monitoring.py` failed with `ModuleNotFoundError: No module named 'tools.synth.monitoring'` before monitoring.py existed. jira.py was written before its tests, so there is no RED run for it; its tests passed on the first run.
- GREEN: tests/unit/tools/synth 156 passed (also with `--require-test-ids`). Line+branch coverage: jira 99 %, jira_changelog 100 %, jira_links 100 %, monitoring 100 %.
- Full `pytest -m "(unit or integration) and not slow"`: 3201 passed, 5 skipped, 10 deselected, 1 xfailed (the existing IT00-02 marker).
- ruff format and ruff check clean; mypy 0 issues (120 files); lint-imports 12 kept; check_type_ownership exit 0; check_module_size exit 0; all pre-commit hooks passed (no detect-secrets baseline change).
