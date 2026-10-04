# T11-12 verify review: dirty data, fetch simulation, flattening

Worktree `agent-ab8214bcf54453ba2`, commit `1b101fd` (base `bff6f4b`). Verifier: VERIFY agent (w28-s11).

**Verdict: Needs fixes.** Two Important findings in `tools/synth/dirty.py`. Some counted defects cannot be observed in the output, or are observed twice. One of the two also makes PT11-03 fail on reachable seeds. Fetch and flatten pass every check.

### Spec Compliance
- U11-16 `apply_dirty` / `DirtyCounters`: ❌. Rates, `none -> []` with zero counters, heavy = 5x default capped at 1.0, per-copy counting of duplicates and later versions, and the schema drift rules (`u_business_impact` at month_index >= 24, the `bare_priority` flag left to U11-18, the Jira cost field dropped at month_index == 29) all match the spec. However, the postcondition "counters equal defects observable" fails for `resolved_before_opened` (I1, I2).
- U11-17 `assign_fetch`: ✅. Initial mode: background rows land on day end+1 at 00:00Z + U(0, 24 h); re-emits land on days end+2..end+15. Daily mode: lag is 0..6 h. Every input record is placed exactly once, in input order (identity checked). `updated_at_of` is used only in daily mode, as specified.
- U11-18 `to_lake_batch`: ✅
  - The 8 metadata columns come from `herness.connectors.base.METADATA_SCHEMA`, so the types are exactly spec 02's (`_source_updated_at` and `_fetched_at` are `timestamp[us, tz=UTC]` not null; `_deleted` is bool; `_payload` is a nullable string).
  - ServiceNow columns use the real `flatten_record(fields=..., display_pairs=True)`. Jira columns use the real `flatten_issue`, called through the module attribute, so the spy sees every call.
  - Monitoring columns use `EVENT_COLUMNS` / `METRIC_COLUMNS` as strings.
  - Tombstones get `_deleted` true, `_payload` NULL and NULL fields.
  - A missing key raises `SchemaViolation`.
  - `_payload` is `json.dumps(sort_keys=True, separators=(",", ":"))`.
  - `SERVICENOW_FIELDS` is a module constant marked `# T11-16:`, as the sub-controller ruled.
- Tests: ✅ UT11-19 (none/default/heavy at 10,000 records per entity), ✅ UT11-20, ✅ UT11-21, ✅ UT11-22 (also written through the real `LakeWriter`), ✅ UT11-23, ✅ UT11-118 (spy, column order, values), ✅ PT11-03 (present and green in the suite run, but it can fail on reachable seeds; see I2). Every test name or docstring carries its ID, and every file sets `pytestmark = pytest.mark.unit`, the same as the existing PT11-01 and PT11-04 files.
- ⚠️ Cannot verify within this card (these are for T11-16 and T11-17):
  - (a) IT11-09 has not been run. The DQ `future_timestamp` check compares values against `meta.build.started_at`, so `opened_at + 730 d` only shows up when the dataset span ends within 730 days of build time.
  - (b) In initial mode, later versions and tombstones can be up to 10 days newer than their record. For records updated near the span end, `_fetched_at` (end+2..end+15) can be earlier than `_source_updated_at`. Spec U11-17 says this verbatim; it needs a ruling.
  - (c) Tombstoned incidents leave core. Any counted defect they carry (for example `missing_service`) is then invisible to core DQ. This is a small effect at tiny scale.
  - (d) The `bad_timestamp` "empty" variant is a NULL, not a cast failure, in DQ. IT11-09's mapping must take that into account.
  - (e) The builder notes that U11-07 `cmdb_ci*` rows have no `sys_updated_on`, so they cannot be flattened. That is a generator gap, not part of this card.

### Evidence (independent)
- `pytest tests/unit/tools/synth`: 250 passed. Coverage (line / branch-partials): dirty 97 % / 3 of 52, fetch 100 % / 0, flatten 98 % / 1 of 28. All are at least 90 % line and 85 % branch.
- `tools.check_module_size` exit 0. Line counts: dirty 248/250, fetch 53/150, flatten 193/200. `ruff check`, `ruff format --check` and `mypy` are clean on all 6 touched files. `lint-imports`: 13 contracts kept, 0 broken.
- Wide dirty probe: 4,320 runs (60 seeds x 6 entities x {none, default, heavy} x month_index {0, 24, 29, 35}, 400 records each).
  - `none`: no re-emits, records unchanged, zero counters in every run.
  - default and heavy: the builder's diff-based observer equals the counters in every run.
  - A standalone observer counts incidents in the output with a parseable `resolved_at < opened_at`. It found **252 such incidents against `counters.resolved_before_opened` = 148** (I1).
- Fixed-seed determinism holds for dirty (heavy, 3,000 records), fetch and flatten (`RecordBatch.equals`). Every draw comes from the passed `rng`.
- Fetch probe: 300 seeds x {initial, daily}, 0 violations.
- Flatten probe on generated Jira issues plus a tombstone: column set = `JIRA_ISSUE_COLUMNS` + custom ids; payloads exact; the tombstone row is all NULL.
- Red-then-green probes: I applied 17 mutations, one at a time, and reverted each with `git checkout -- <file>`. 16 turned the tests RED:
  - dirty: future_ts not applied, duplicates double-counted, `none` applied after drift, heavy x4, cost drift `>= 29`, impact drift at month 23, bad_timestamp not counted;
  - fetch: re-emit days 0..13, daily lag 7 h, background on end+2;
  - flatten: unsorted payload, tombstone payload not NULL, custom field ids reversed, no key check, `display_pairs=False`, `flatten_issue` imported directly (spy bypassed).
  - Not caught: removing the `min(1.0, ...)` cap (M1).
- `git status` is clean after the probes.

### Strengths
- The Jira and ServiceNow columns go through the real connector flatteners, and the spy-based UT11-118 proves that end to end. UT11-22 writes through the real `LakeWriter`, which makes the spec 02 contract check genuine.
- The defects are vectorised to one uniform per defect per record, in a fixed order, so results are deterministic per seed. Defects that do not fit a record are skipped and not counted.
- The modules are small and focused, and the tests are behavioural: the mutation probes show they bite.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- **I1. A `future_ts` incident also produces an uncounted `resolved_before_opened`** (`tools/synth/dirty.py:126-132`). `_future` moves only `opened_at` forward by 730 days. On a resolved incident (most of them), this leaves `resolved_at < opened_at` (and `closed_at` and `u_acknowledged_at` too). The output therefore shows more resolved-before-opened incidents than the counter (252 vs 148 in the probe). Spec 02's `resolved_before_opened` DQ check (incidents with both values set and `resolved_at < opened_at`) will exceed `truth.dirty.resolved_before_opened` by roughly the number of resolved `future_ts` incidents. That is about 20 at small scale, so IT11-09 (±1 row) will fail; at tiny scale it fails occasionally. This breaks PT11-03 as the spec intends it ("defects observable in the output"). The test's observer misses it because it diffs before/after (`tests/unit/tools/synth/test_synth_dirty.py:137-140`) instead of reading only the output.
  - Fix: in `_future`, shift `u_acknowledged_at`, `resolved_at` and `closed_at` by the same `FUTURE_SHIFT` (only non-empty ones), so the order of the lifecycle timestamps is unchanged. Add them all to `touched`. Leave `sys_updated_on` untouched.
  - For T11-17: state that IT11-09 maps `truth.dirty.future_ts` to `future_timestamp:core.incident.opened_at`, because the `resolved_at` and `closed_at` columns will also hold future values.
  - Rejected alternative: limiting `future_ts` to open incidents. Few incidents are open, so the 0.02 % rate would not be met.
- **I2. `bad_timestamp` can hide a counted `resolved_before_opened`, and PT11-03 fails on reachable seeds** (`tools/synth/dirty.py:140`, `:145`). `_backdate` adds only `resolved_at` to `touched`, so `_bad_timestamp` on the same record can still overwrite `opened_at` with `31/02/2024`, an epoch-ms string or an empty value. The record is then counted as resolved-before-opened, but downstream cannot observe it, because `opened_at` will not cast. Reproduction: `test_pt11_03_counters_equal_observable_defects.hypothesis.inner_test(seed=805, entity="incident", dirty="heavy", month_index=0)` raises `AssertionError` at `test_synth_dirty.py:110`. Seed 912 gives the same error; seed 1453 gives `ValueError: time data '31/02/2024'`. Hypothesis draws seeds from 0..2^32, so this is a latent flake at about 0.5 % per run, not just a theoretical gap.
  - Fix: `_backdate` must add both `opened_at` and `resolved_at` to `touched`.
  - Then make PT11-03 and UT11-19 count `resolved_before_opened` from the output alone: parseable `resolved_at < opened_at` must equal the counter. Make `_ts` in the observer tolerate unparseable values.
  - Also add a fixed regression example (`@example(seed=805, entity="incident", dirty="heavy", month_index=0)`).

#### Minor (Nice to Have)
- **M1.** The heavy cap of 1.0 (`tools/synth/dirty.py:95`) is never exercised. No default rate times 5 exceeds 1, and removing `min(1.0, ...)` leaves every test green. Add a UT11-19 case whose params raise one rate above 0.2 (for example through a `params_file` or `model_copy`) and assert that the heavy rate is 1.0.
- **M2.** Later versions advance the incident `state` (2->6->7) without setting `resolved_at` or `closed_at` (`tools/synth/dirty.py:196-197`). The result is "Resolved" or "Closed" rows with an empty `resolved_at`. This is harmless to the counters, but it is an implausible record shape that may affect downstream state and MTTR logic.
- **M3.** `_fetch_fields` (`tools/synth/flatten.py:130-137`) copies the connector's head-column rule (`herness/connectors/servicenow.py:327-330`). A shared helper, or a test asserting that the two agree, would stop them drifting. This can go into T11-16 together with the `SERVICENOW_FIELDS` swap.
- **M4.** Judgement on dropping absent custom-field columns (`tools/synth/flatten.py:183-184`, `:193`). The spec says "exactly `JIRA_ISSUE_COLUMNS` followed by the custom field ids". The builder's code meets that whenever a field is present in at least one row. It deviates only in the drift shard, where the cost field is absent from every row and the column is left out. A real connector would emit an all-NULL column instead.
  - I accept this as the intended way to simulate "drops the cost custom field": the lake then holds files without the column, which exercises `union_by_name`.
  - Record it as a spec note or ruling, because UT11-118's second test pins the behaviour.
  - Risk: the generator always writes all three keys (`tools/synth/jira.py:234-236`), so normal shards keep the full column set.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** Fetch partitions and lake batches are correct and well tested. Two cross-defect interactions in `apply_dirty` break the exact-count truth: future_ts produces an extra resolved-before-opened, and bad_timestamp can hide a counted one. The second one already makes PT11-03 fail for seed 805. Both are small local fixes in `_future` and `_backdate`, plus an observer in the tests that reads only the output.


---

## Re-review round 1 (commit 4bac1dc on 1b101fd; scope I1, I2, M1, M2)

**Verdict: Approved.** I1, I2, M1 and M2 are fixed. One new Minor finding (M5) is a missing regression test, not a defect.

### Fix verification
- **I1: ✅ fixed.** `_future` (`tools/synth/dirty.py`) now shifts every non-empty stamp in the incident lifecycle (`opened_at`, `u_acknowledged_at`, `resolved_at`, `closed_at`) by +730 days. It marks all of them touched and leaves `sys_updated_on` alone.
  - My independent output-only probe covered 4,800 runs: 400 seeds x 6 entities x {default, heavy}, 300 records each, month = seed % 36. Results:
    - parseable `resolved_at < opened_at` matched `counters.resolved_before_opened` in every run (0 mismatches; before the fix it was 252 vs 148);
    - `opened_at > end + 1 y` matched `future_ts` in every run (0 mismatches);
    - no run had acknowledgement before opening or closing before resolution.
  - An amplified sweep (future_ts, resolved_before_opened and bad_timestamp all at 10 %, heavy, 60 seeds x 6 entities x 1,000 records) also gave 0 mismatches.
- **I2: ✅ fixed.** `_backdate` now marks both `opened_at` and `resolved_at` as touched. The previously failing PT11-03 seeds 805, 912 and 1453 (incident, heavy) now pass, and they are pinned as `@example`s.
  - The observer now reads the output alone and tolerates unparseable stamps.
  - My independent bad_timestamp count (bad values among the stamps the clean record filled) equals the counter in every run.
- **M1: ✅ fixed.** `test_ut11_19_heavy_cap_and_consistent_future` uses a params file with rates of 0.3, so heavy is capped at 1.0 and all 500 records are hit. Removing the `min(1.0, ...)` cap now turns this test RED.
- **M2: ✅ fixed.** When a later version advances the state, it now stamps `resolved_at` (2->6) or `closed_at` (6->7) with max(opened_at, new sys_updated_on).
  - The builder's open point: on a `future_ts` record, the stamp lands after `sys_updated_on`. The probe found 8 such later versions in the default/heavy sweep and 1,884 in the amplified sweep, and **every one was on an already-future record**.
  - This adds no defect type or count mismatch:
    - no later version on a non-future record gets a future stamp (0 found);
    - no later version differs from its record in resolved-before-opened, future, bad-stamp, missing-service or unknown-enum;
    - spec 02 has no DQ check comparing a lifecycle stamp with `sys_updated_on`;
    - the future `resolved_at`/`closed_at` values sit on rows already counted under `future_ts`. This is covered by the I1 note for T11-17: map `truth.dirty.future_ts` to `future_timestamp:core.incident.opened_at`.
  - The `max(...)` guard is required. Without it, a later version of a future record would carry `resolved_at < opened_at`, an extra resolved-before-opened on the latest version, which core keeps.

### Red against the old behaviour
I reverted each fix in place, one at a time, ran the dirty tests, and restored the file with `git checkout -- tools/synth/dirty.py`:

| Mutation | Result | Tests that failed |
|---|---|---|
| Old `_future` (shifts `opened_at` only) | RED, 4 failed | UT11-19 counters for incident (default and heavy), the cap/future test, PT11-03 |
| Old `_backdate` (marks only `resolved_at` touched) | RED | PT11-03 (via its `@example` seeds) |
| No cap | RED | cap test |
| Later version without the lifecycle stamp | RED | `test_ut11_19_reemit_shapes` |
| Later version stamped at `at` without `max(opened_at, ...)` | **GREEN, not caught** | none (see M5) |

### Gates
- `pytest tests/unit/tools/synth`: 251 passed.
- Coverage (line, partial branches): dirty 98 % with 2 of 54 branches partial; fetch 100 %; flatten 98 %.
- `dirty.py` is 250/250 lines; `check_module_size` exit 0.
- ruff check, ruff format --check and mypy are clean on `dirty.py` and `test_synth_dirty.py`.
- `git status` is clean after the probes. I made no commits.

### New findings
- **M5 (Minor), `tools/synth/dirty.py` `_later_version` (the `max(at, opened or at)` stamp):** this guard against resolved-before-opened is untested; replacing it with `ts_pair(at)` leaves every test green. The reemit test's "later versions add no observable defect" assertion would catch it, but its fixture (seed 7, heavy, 2,000 records, future_ts 0.1 %) almost never contains a future incident that gets a later version. Fix: add a case with a params file setting `future_ts` and `later_versions` high enough to cap at 1.0 under heavy. Assert that `_observe` gives the same counts on the later versions as on their records, and that every stamped field is >= `opened_at`.
- **Note (no action):** `_at` uses `cast` on `parse_ts`, so an empty `sys_updated_on` would raise `TypeError` in `_later_version` / `_tombstone`. The old code tolerated that case. Generated incidents and issues always carry the watermark, and `to_lake_batch` rejects rows without it, so this is acceptable.
- Parked per the controller: M3 (shared head-column helper, T11-16) and M4 (accepted ruling).

**Task quality:** Approved
**Reasoning:** Counters now equal the defects observable in the output alone, across wide, amplified and previously failing seeds, and each fix is pinned by a test that goes red against the old behaviour. M5 is a regression-test gap only.
