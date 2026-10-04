# T11-09 review: Jira and monitoring generators (U11-08, U11-09)

Reviewed: commit 2c20e14 on base 2c49d82 (diff `briefs/T11-09-review.diff`). Focused check: `PYTHONUTF8=1 uv run pytest tests/unit/tools/synth -k "UT11_11 or UT11_12" -q -p no:logging` gave **26 passed**. That run used the working tree, which holds the T11-08 fix agent's uncommitted servicenow edits. The report says 17 + 11 = 28 tests; the files actually contain 16 + 10 = 26 (see m7).

### Spec Compliance
- ✅ Spec compliant. Every requirement is listed below.

**U11-08 `gen_issues` (tools/synth/jira.py and its siblings jira_changelog.py and jira_links.py)**
- ✅ `id` = `project.id_base + seq` as a numeric string; `key` = `<PROJECT>-<seq>` (jira.py:66-72, test at test_synth_jira.py:101-113).
- ✅ The `fields` set matches the spec (jira.py:226-243):
  - issuetype.name, parent.key, project.key, components[].name, labels
  - status.name and status.statusCategory.key (new, indeterminate, done)
  - created, resolutiondate
  - customfield_10016, customfield_10050, customfield_10060
  - summary, description (wiki text with an `h3.` prefix), updated
  - issuelinks (inside fields, as in Jira Cloud)
- ✅ `changelog` is a complete object {startAt, maxResults, total, histories}. There is one status history per kept transition, with from/to ids and strings (jira_changelog.py:69-78).
- ✅ Each `remotelinks` entry is `{id, object: {url, title}}` (jira_links.py:82-86).
- ✅ Type mix: .01/.05/.14/.48/.20/.12, i.e. .80 split .60/.25/.15 (param_groups.py:196-203; drawn at jira.py:99-101).
- ✅ Hierarchy follows the spec:
  - A feature's parent is an epic of the same project.
  - A leaf's parent is a feature or an epic at 0.5 each, falling back to the other kind.
  - Parents stay within the shard and the project (jira.py:110-123).
  - The epic → initiative link is an addition. It is consistent with the design's chain, and the tests allow it.
- ✅ Points use the set {1,2,3,5,8,13} and the spec probabilities, on stories only. An epic sums its stories, direct and through its features (jira.py:132-152).
- ✅ Cost on epics and initiatives = points × U(2500, 4000), rounded half-up to 1,000 (jira.py:154-159).
- ✅ Cycle time is lognormal with median 6 d and σ 0.8. Re-entry is 12 % and happens once; carry-over is 15 % (jira_changelog.py:33-60). The carry-over reading is noted in m1.
- ✅ 25 % of issues get no component, via `dirty_rates.missing_component`: 0 at `none`, ×5 capped at `heavy`. This matches the §5.1.6 heading (jira.py:178-182, 223).
- ✅ 3 % of issues mention an INC or CHG number, half in the description and half in remotelinks (jira.py:185-194). The INC/CHG source follows the controller ruling: the optional keyword-only `incident_index`, and the planned-range fallback.
- ✅ PII goes into 1 % of descriptions. PII rows use `record_id` `jira:issue:<id>` (jira.py:197-210).
- ✅ Wrong shard → SynthUsageError, per the group ruling.

**U11-09 (tools/synth/monitoring.py)**
- ✅ Event fields match the §5.1.2 list plus `_source_key` = `<source_tool>:<event_key>` (monitoring.py:118-131).
- ✅ Services are drawn by event weight, `source_tool` is uniform over the three tools, and severities are .05/.15/.30/.35/.15 (monitoring.py:155-160, param_groups.py:177-185).
- ✅ Near-incident placement (DD11-05):
  - Only non-info events are candidates. With p = 0.50 an event moves to a uniform integer offset within ±1800 s of a random same-service incident.
  - If the service has no incident, the time stays random.
  - With p = 0.90, `incident_ref` is set to that incident's number (monitoring.py:69-96, 108-110).
- ✅ `end_ts` = ts + lognormal (median 20 min); status is resolved or firing.
- ✅ metric_daily covers the first `preset.metric_services` services in catalog order:
  - `availability_pct` is U(99.5, 99.99), minus U(0.5, 3.0) on days in `p1_days`.
  - `error_rate` is U(0.001, 0.02) and `p95_latency_ms` is lognormal with median 250.
  - `request_count` = 10,000 × weight share × `_day_weight` × U(0.95, 1.05). `_day_weight` is the arrival-curve day factor: weekend, holidays and annual sinusoid.
  - `_source_key` = `<source_tool>|<metric_name>|<service>|<date>` (monitoring.py:169-218).
- ✅ `IncidentTimeIndex` is data-only in shards.py, per the ruling (shards.py:26-38).

**Tests**
- ✅ UT11-11 checks that every parent key exists (and has the same project and an allowed type), that points are in the set, and that changelog statuses agree with status, category and resolutiondate.
- ✅ UT11-12 checks that `incident_ref` appears only within ±30 min of the referenced incident on its service, and checks both `_source_key` formats.
- ✅ Naming follows the rules: names are `test_ut11_11_*` / `test_ut11_12_*`, docstrings start with the ID, and `pytestmark = pytest.mark.unit`.
- ✅ Limits hold:
  - The files are within budget: jira.py is 280 of 320 lines, monitoring.py 220 of 300, and the siblings (92 and 91 lines) are under the default 400.
  - Every function has ≤ 6 arguments, except `gen_issues`, whose `noqa PLR0913` is covered by the ruling.

**Invented constants and interpretations** (report items 1-10). All are accepted, and none contradicts the spec:
- the created → In Progress wait
- the placement of the re-entry bounce
- the labels and the link rate
- the status ids
- the id schemes for histories, remote links and issue links (checked unique within their namespaces)
- the event duration σ and the p95 σ
- the per-tool `event_key` formats
- the metric units
- the per-service metric tool (k % 3)
- `volume_by_day` being unused (a spec gap: the algorithm never references it)

m1-m3 flag two that stretch the literal spec.

- ⚠️ Cannot verify from the diff:
  - Downstream consumers (spec 01 flatten, spec 04 `alert_noise_ratio`) may read `dedup_key` or `unit` values. The chosen values have no spec anchor (m5).
  - The ×5 `heavy` rate and the "exact counts" truth recording for missing components happen in later units (U11-16 and the truth writer).
  - U11-19 must pass a 30-day-wide index for the Jira INC mentions (ruled as a spec note).

### Strengths
- Clean decomposition: draft → hierarchy → roll-up → render. The lifecycle and the links sit in small, pure siblings.
- The changelog is internally consistent by construction: status and resolutiondate are derived from the last kept transition, and transitions are truncated at the span end.
- The UT11-12 test builds a real index from `gen_incidents` output and checks each `incident_ref` against the actual incident's `opened_at` and service.
- Determinism is tested (equal rng state gives equal output). Edge cases are tested: empty shard, wrong shard, no ServiceNow source, empty or late index.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- **m1 Carry-over reading inflates the effective rate.** jira_changelog.py:39-41 puts the carried 15 % uniformly in the month after creation. Non-carried issues already spill into that month naturally: a 0-72 h wait plus a lognormal cycle with median 6 d and mean ≈ 8.3 d crosses the month end for about a third of issues. As built, about 40 % of issues resolve after their creation month, and the "15 %" is not distinguishable in the data. The spec phrase "resolution after the month of creation plus 1" can also be read as "after month m+1", i.e. in month m+2 or later, which would make the 15 % a distinct tail. No consumer depends on it today. Recommend a controller ruling or spec note.
- **m2 Background epics with no pointed descendants draw one story-point value** (jira.py:148-152). At tiny, most epics have few children in the shard, so many epics carry 1-13 points and a cost of $3k-$52k. This is invented to avoid a zero cost, and the literal spec says "sum of children". It is acceptable, but it should be logged as a spec note because funding scoring divides by cost.
- **m3 The effective mention rate is below 3 %.** `_mention` (jira.py:187-191) draws the 3 % first, and `pick_ticket` then often returns None:
  - with a strict same-service index window
  - early in the span, when the planned range is empty
  - when servicenow is not a source

  The test at test_synth_jira.py:282-285 accepts anything from 3 to 45 of 600 (0.5-7.5 %), so it would not catch a rate far from 3 %. Consider redrawing on None, or tightening the assertion.
- **m4 Near-incident events can land outside the shard month or after the span end.** In monitoring.py:73-78, an index incident at month ±1 day plus an offset of up to 30 min can place `ts` in the adjacent month, or up to 30 min past `span_end`, where it becomes a "firing" event in the future. The ts is not clamped.
- **m5 `dedup_key` collides across events.** `dedup_key` = sha256(tool, service, title)[:16] (monitoring.py:116), so every event with the same symptom, service and tool shares a key: about 60 symptoms × 20 services × 3 tools. That is plausible alert-grouping semantics, but the spec does not define it. Confirm that no dedupe or noise metric keys on it.
- **m6 Cross-module private imports.** `_day_weight` and `_shard_days` are imported from servicenow_common (monitoring.py:22), and T11-08's fix agent is editing that module concurrently. Make them public in a follow-up.
- **m7 The report's test counts are off.** It says "17 tests" for test_synth_jira.py and "11 tests" for test_synth_monitoring.py; the files contain 16 and 10. The report also shows no RED run for jira, because the tests were written after the code (stated honestly).
- **m8 A child can be created before its parent.** Parents are picked without regard to `created` (jira.py:117-123), so a story can be created before its epic. This is cosmetic, but visible in timelines.

### Assessment
**Task quality:** Approved
**Reasoning:** Both units match U11-08, U11-09 and DD11-05, including the JSON shapes, distributions, near-incident logic and `_source_key` formats. The tests assert what UT11-11 and UT11-12 require, and the budgets, limits and rulings are respected. The remaining items are interpretive (carry-over reading, epic point fallback, mention rate) or cosmetic.
