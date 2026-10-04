# T11-12 report: dirty data, fetch simulation, flattening

Status: DONE_WITH_CONCERNS (all card tests green; concerns are spec notes for later cards)
Final commit: 1b101fd feat(synth): T11-12 dirty data, fetch simulation, flattening (all pre-commit hooks passed, including pytest-unit)

## Built
- `tools/synth/dirty.py` (U11-16): `DirtyCounters` (8 truth `dirty` keys, `add`, `as_dict`),
  `apply_dirty`, plus `effective_rates`, `DEFECTS`, `COST_FIELD`, `FUTURE_SHIFT`.
  One uniform per defect type per record (`rng.random((n, 8))`), heavy = 5 x default capped 1.0,
  none -> `[]` and records untouched. Schema drift: incidents gain `u_business_impact` at
  month_index >= 24; Jira shard at month_index == 29 drops `customfield_10050`.
  Re-emits per record in order duplicate, later version, tombstone
  (`{"__tombstone__": True, "key": <sys_id | Jira id>, "deleted_at": <source-format ts>}`).
- `tools/synth/fetch.py` (U11-17): `assign_fetch` (initial: background day end+1 + U(0,24h),
  re-emits day end+1+d, d in 1..14; daily: updated_at_of + U(0,6h)). Vectorised draws from rng only.
- `tools/synth/flatten.py` (U11-18): `to_lake_batch`, `source_updated_at` (usable as the
  `updated_at_of` callback), `SERVICENOW_FIELDS`, `SYNTH_CUSTOM_FIELD_IDS`, `MAX_ROWS`.
  ServiceNow via `rows.flatten_record(..., display_pairs=True)` with connector head columns
  (`sys_id`, `sys_updated_on`, `sys_class_name` on cmdb_ci) + per-entity fields; Jira via
  `herness.connectors.jira.flatten_issue` called through the module attribute; monitoring via
  `EVENT_COLUMNS`/`METRIC_COLUMNS`. Metadata from `herness.connectors.base.METADATA_SCHEMA`,
  `_record_id` via `base.record_id`. Missing key -> SchemaViolation; unknown source/entity or
  > 131,072 rows -> SynthUsageError.
- Tests: `tests/unit/tools/synth/test_synth_dirty.py` (UT11-19, PT11-03),
  `test_synth_fetch.py` (UT11-20, UT11-21), `test_synth_flatten.py` (UT11-22, UT11-23, UT11-118;
  UT11-22 also writes batches through the real `LakeWriter` to prove the spec 02 contract).

## Evidence
- RED: `pytest tests/unit/tools/synth/test_synth_dirty.py` -> `ModuleNotFoundError: No module named 'tools.synth.dirty'`;
  same for `tools.synth.fetch` / `tools.synth.flatten` before those modules existed.
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/tools/synth` -> 250 passed (card tests: 44).
- Coverage (line/branch, card modules): dirty 97 %, fetch 100 %, flatten 98 % (branch partials 3/1/0).
- ruff format/check clean, mypy clean on all touched files, lint-imports 13 kept,
  `python -m tools.check_module_size` exit 0.
- Line counts vs budget: dirty.py 248/250, fetch.py 53/150, flatten.py 193/200.
- Full `pytest -m "(unit or integration) and not slow"` not run separately: the pre-commit
  `pytest-unit` hook runs the whole unit suite (~24 min per commit on this machine).

## Deviations / process
- The first wip checkpoint commit failed on the `mixed-line-ending` hook (CRLF from an edit
  script; all files normalised to LF). Because each commit costs ~24 min of hooks, the work
  was landed as one final commit instead of several wip checkpoints.

## Spec notes
1. ServiceNow fetch fields (sub-controller ruling): `flatten.SERVICENOW_FIELDS` equals the
   generator's field set per entity, marked `# T11-16:` to be replaced by the synth profile's
   `sources.servicenow` field list.
2. Drift columns: a field no live row of the batch carries is left out of the batch
   (ServiceNow fields and Jira custom field ids), so `u_business_impact` only appears from
   month 25 and the drift Jira shard has no cost column (union_by_name is exercised). A
   connector with a configured field list would emit an all-NULL column instead.
3. `bare_priority=True` renders the priority display value as the bare value
   (`{"value": "2", "display_value": "2"}`) in payload and columns, for any ServiceNow record
   carrying a `priority` pair.
4. Tombstone `key` is the source key (sys_id / Jira id), not a record id (the fixture in
   test_servicenow_aux uses a record-id-shaped key; it only checks the flag).
5. Defect interplay: defects that do not fit a record are skipped and not counted (open
   incidents for resolved_before_opened; bad_timestamp never overwrites a field future_ts or
   resolved_before_opened just set, never touches sys_updated_on). Later versions advance
   incident state 1->2->6->7 and Jira status To Do->In Progress->Done; the changelog of a
   later Jira version is not extended. `u_business_impact` value is derived from priority
   (1/2 -> "1 - High", 3 -> "2 - Medium", else "3 - Low"); the spec leaves it open.
6. "first month >= 29" for the Jira cost drift is implemented as `month_index == 29`.

## Concerns
- future_ts (opened_at + 730 d, as specified) on a resolved incident also makes
  `resolved_at < opened_at`, so spec 02's `resolved_before_opened` DQ count will exceed
  `truth.dirty.resolved_before_opened` by about the future_ts count (~20 at small) -> IT11-09
  (+-1 row) may fail. Needs a ruling (e.g. shift the whole lifecycle, or count both).
- Generated `cmdb_ci`, `cmdb_ci_service` and `cmdb_rel_ci` rows (U11-07) carry no
  `sys_updated_on`; `to_lake_batch` (like the ServiceNow connector) raises SchemaViolation for
  them, so U11-19 cannot flatten those dimension shards until the generator stamps them.
- Later versions / tombstones are up to 10 d after the record's update; in `initial` mode
  their `_fetched_at` (end+2..end+15) can precede `_source_updated_at` for records updated
  near the span end.

## Fix round 1 (review: T11-12-review.md, I1, I2, M1, M2)
- I1: `_future` now shifts every non-empty incident lifecycle stamp (`opened_at`,
  `u_acknowledged_at`, `resolved_at`, `closed_at`) by +730 d and marks them all touched;
  `sys_updated_on` stays. The only observable defect is the future `opened_at` (for T11-17:
  IT11-09 should map `truth.dirty.future_ts` to `future_timestamp:core.incident.opened_at`,
  as `resolved_at`/`closed_at` also hold future values on those rows).
- I2: `_backdate` marks both `opened_at` and `resolved_at` touched, so `bad_timestamp` can no
  longer hide a counted resolved-before-opened. The UT11-19/PT11-03 observer now counts from
  the OUTPUT ALONE (future = parseable `opened_at` > span end + 1 y; resolved-before-opened =
  parseable `resolved_at < opened_at`; bad timestamp = `31/02/2024`, digits, or empty in a stamp
  a clean record of that state always fills; unparseable stamps tolerated). PT11-03 has
  `@example`s for seeds 805, 912, 1453 (incident, heavy, month 0).
- M1: new test `test_ut11_19_heavy_cap_and_consistent_future` uses a params file with
  `missing_service: 0.3`, `future_ts: 0.3`; heavy rates are 1.0 and all 500 incidents are hit;
  it also checks ack/closed never precede the future `opened_at`.
- M2: a later version advancing an incident state stamps the matching lifecycle field
  (2 -> 6 sets `resolved_at`, 6 -> 7 sets `closed_at`) at the new `sys_updated_on`, never before
  `opened_at`; an unparseable `opened_at` is tolerated. The reemit test asserts the output-only
  observer sees the same defects on later versions as on their records (no defect added).
- dirty.py trimmed to 250/250 lines (module docstring condensed, `_updated`/`_source_key`
  inlined into `_tombstone`, state table carries the stamped field). No sibling module.
- Evidence: `PYTHONUTF8=1 uv run pytest tests/unit/tools/synth` -> 251 passed; coverage dirty
  98 %, fetch 100 %, flatten 98 %; ruff/mypy clean; check_module_size exit 0; `git diff --check`
  clean, all files LF. Ad hoc probe: 1,500 seeds x {incident, change_request, issue} at heavy,
  300 records, month = seed % 36: 0 mismatches between counters and the output-only observer.
- Parked per controller: M3 (shared head-column helper, T11-16), M4 (accepted ruling).
- Fix round 1 commit: 4bac1dc (all pre-commit hooks passed, including pytest-unit).

## Fix round 2 (M5, test-only)
- Added `test_ut11_19_later_version_stamp_not_before_future_opened`: params file sets future_ts
  and later_versions to 0.3 (heavy -> 1.0, 400 incidents). Asserts later versions show the same
  output-only defect counts as their records and every newly stamped resolved_at/closed_at is
  >= the shifted opened_at. Mutation check: replacing the guard with `ts_pair(at)` turns it RED
  (`resolved_before_opened` 100 vs 0); guard restored, dirty.py unchanged.
  `tests/unit/tools/synth` 252 passed; `git diff --check` clean; LF.
- Fix round 2 commit: 1e39b3c (all pre-commit hooks passed, including pytest-unit).
