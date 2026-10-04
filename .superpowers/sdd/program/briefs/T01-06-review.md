# T01-06 review: Sync runner, incremental flow and write loop

Reviewer: verify agent. Worktree agent-ab62b4aa515276044, base 221fd6e, head 4e6d0d6. Read-only.

**Verdict: Approved** (0 Critical, 0 Important, 6 Minor)

## Evidence run by the reviewer
- `pytest -k "UT01_29 or ... or UT01_92" -q -p no:logging`: 29 passed, no warnings.
- `pytest tests/unit/connectors --cov-branch`: 415 passed, 4 skipped (existing platform skips for symlink and DuckDB excel). Coverage: `_write_loop.py` 116 stmts, 20 branches, 100 %. `runner.py` 167 stmts, 22 branches, 99 %; only line 108 (`_default_writer` body) is missed.
- ruff check and ruff format --check: clean. mypy strict on runner.py, _write_loop.py and fake_lake.py: clean. Tests are outside the project mypy `files`.
- `python -m tools.check_module_size`: exit 0. runner.py is 307/390 lines and _write_loop.py is 183/200.

## Spec compliance

### Per unit
- ✅ **U01-36 SyncResult.** Frozen, slotted dataclass with the nine fields. `to_dict` returns a JSON-safe dict with files as POSIX paths relative to `paths.data`. The added keyword `data_root` does not break callers. Aggregation takes the minimum watermark, and gives `None` when any tool has `None` (runner.py:77-91).
- ✅ **U01-37 SyncRunner.** Constructor parameters, kinds and defaults match. A name mismatch raises `ConfigError`. `_cleaned` runs the orphan cleanup once per instance on `data_root/"raw"` with `now=clock()`. `skipped_open` and `stopped` are exposed and reset per run. `connector_factory` and `should_stop` are stored but unused until T01-07, as expected.
- ✅ **U01-38 run_incremental.**
  - Entity is validated before any call. The `# T01-10:` marker is present (ruling 1).
  - For tool streams, a `CircuitOpen` goes to `skipped_open` and is logged at WARNING. Any other `HernessError` is kept and logged as `connectors.sync.failed`.
  - Raise order is fatal, then retryable, then CircuitOpen only when every tool was skipped. Otherwise it returns the aggregate. Non-HernessError exceptions propagate immediately.
  - The builder extends step 3: any other kept error (a RecoverableError) is raised (see Minor 2).
- ✅ **U01-39 _incremental_stream.**
  - Order: guard, then get_watermark. With no watermark it goes to the backfill seam with `resolve_start(now)` and `now`.
  - `since = wm - overlap` and `until = now - settle`. When `since >= until` it returns a zero result with the watermark unchanged.
  - Then: started log, a `DeletionFilter` that is reloaded, and the write loop with `cap=until`, `ordered` taken from `UNORDERED_SOURCES`, and `advance_watermark=True`.
  - It re-reads the watermark, logs completed, and writes the §8.2 metrics through `record_metric_samples`.
  - Ruling 2 (interim `_backfill_stream`, `advance_watermark=False`, one `set_watermark(min(max_committed, end))` after the final commit, `# T01-07:` marker) is correct at runner.py:219-231.
- ✅ **U01-40 _write_stream / WriteLoop.**
  - Empty batches are skipped. The deletion filter runs before every write, and a batch that is empty after filtering is skipped.
  - On drift it logs, writes the metric, then checkpoints only when `cp_rows > 0`.
  - Tombstones are counted with `pc.sum`. A checkpoint happens on `checkpoint_rows` or on writer age ≥ 600 s.
  - Checkpoint order is commit → files and `max_committed` → `fault_point("connector.before_watermark", source=key)` → ordered `set_watermark(min(top, cap))` → `deletion.reload()` → progress → log → new writer (_write_loop.py:139-169). This matches step 3.
  - The final commit runs even when empty. The unordered watermark is set once with `min(max_committed, cap)`.
  - `except BaseException` aborts, which covers KeyboardInterrupt. If abort fails, the loop logs `connectors.lake.abort_failed` at ERROR and the original exception is re-raised.
  - After a commit, abort is safe: the real `LakeWriter.abort()` is a no-op after commit (lake.py:361-364), and the fake does the same. An exception after commit (set_watermark StoreBusy, reload, writer_factory) therefore cannot discard committed files.
  - The watermark is never moved back: `set_watermark` is monotonic (store/ops/ingest.py:126), so rows re-read through the overlap cannot regress it.

### Per test row
- ✅ **UT01-29.** Checks (since, until) equality, the full SyncResult, to_dict JSON round-trip with relative POSIX paths, the §8.1 started/completed fields, the §8.2 metrics and labels, and ConfigError for an unknown entity.
- ✅ **UT01-30.** Nothing yielded (and an empty batch) leaves the watermark unchanged with rows 0. With `since >= until` there is no source call and no writer.
- ✅ **UT01-31.** 20/50 rows give 3 commits. The call order `commit → set_watermark` is asserted for each checkpoint. The deletion request created after checkpoint 1 is dropped after the reload (`k45` absent, `skipped_deleted == 1`). Progress notes and checkpoint logs are asserted. An extra test covers the time-based checkpoint.
- ✅ **UT01-32.** The spy on `_backfill_stream` gets `(resolve_start(NOW), NOW)` and the mode is `backfill`. The interim path sets the watermark once, and no watermark is set when there are no rows.
- ✅ **UT01-33.** Covers the connector error, KeyboardInterrupt, a failing abort (logged, original raised) and a commit failure. Each asserts abort once, the error re-raised and the watermark unchanged.
- ✅ **UT01-34.** Asserts a commit before batch 2, two writers, the drift log fields and the drift metric. An extra test covers drift right after a checkpoint.
- ✅ **UT01-35.** Guard raising CircuitOpen means no connector call and no writer. A mismatched name raises ConfigError. Cleanup runs once per instance.
- ✅ **UT01-36.** Monitoring rows past `until` give a single `set_watermark` as the last event, equal to UNTIL.
- ✅ **UT01-92.**
  - datadog's `SourceUnavailable` is isolated and re-raised (same object). Prometheus is committed and advanced, and datadog's watermark is unchanged.
  - The writer is aborted once, and the `connectors.sync.failed` fields are asserted.
  - Extra tests: aggregate, skipped_open, all-open, fatal over retryable, the other-error case, and backfill inside the aggregate.
- ✅ **Conventions.** IDs are in test names and in the first line of docstrings. Each test file sets module-level `pytestmark = [pytest.mark.unit, ...]`.

### Rulings check
- ✅ Ruling 1: marker at runner.py:164.
- ✅ Ruling 2: see U01-39 above.
- ✅ Ruling 3: `_write_loop.py` has a module-map row with budget 200.
- ✅ Ruling 4: the known-red tests were not touched.

### ⚠️ Cannot verify here
- `job_id` on `connectors.sync.started` depends on the job layer calling `bind_ids(job_id=...)`. merge_contextvars is in the pipeline (core/logging.py:131,156), so it works once the handler binds it.
- For single-stream connectors, `connectors.sync.failed` and the CircuitOpen `skipped_open_circuit` outcome are left to `handle_sync` (deviation 9). The later handle_sync card must emit them.
- Deviation 10: an interim backfill with zero rows leaves no watermark, so the next run backfills again. T01-07 must set the watermark to `end` (carry-over recorded).
- Deviation 11: a StoreBusy on post-commit ops writes (metrics, logs, drift metric) fails the job after the data and watermark are committed. This is spec-consistent (no `except Exception`), and the retry is idempotent through overlap and dedup.

## Findings

### Critical
None.

### Important
None.

### Minor
1. **herness/connectors/runner.py:271-273: completed log uses the connector name for tool streams.** `connectors.sync.completed` sets `source` from `SyncResult.source`, the connector name. For monitoring tools, the per-tool completed events all read `source="monitoring"` with no `stream`, so datadog and prometheus completions cannot be told apart. This contradicts the builder's own deviation 5 rule: events without a `stream` field use the stream key, as done for schema_drift and abort_failed. Suggested fix: log `source=key` or add `stream=key`.
2. **herness/connectors/runner.py:94-104: raise order extends U01-38 step 3 (deviation 3).** Raising a kept non-fatal, non-retryable HernessError, where the spec would return the aggregate, is the safer reading because it swallows nothing. It is still a spec change. It should be ruled by the sub-controller and recorded in spec §13, or the spec text should be amended.
3. **herness/connectors/runner.py:41: `_StreamRun` erases the keyword signature.** `_StreamRun = Callable[..., SyncResult]` means mypy will not check the `run(entity, key=..., fetch=...)` call. T01-07 is expected to route backfill through `_over_tools`, so a small Protocol would catch signature drift.
4. **docs/impl/01-connectors.impl.md:75: module-map wording.** The new row says `StreamOutcome` is "re-exported by runner.py", but `runner.__all__` (runner.py:37) exports only `SyncResult` and `SyncRunner`. runner.py only imports it. Reword the row to "imported by runner.py".
5. **herness/connectors/runner.py:50-74: SyncResult invariants are not enforced.** They are documented only; there is no `__post_init__` check. Construction sites are all internal and the invariants hold by construction (monotonic `set_watermark`), so this is optional hardening.
6. **tests/unit/connectors/test_runner_write.py: fault_point placement is untested.** No unit test asserts that the `fault_point("connector.before_watermark")` call (_write_loop.py:146) falls between commit and set_watermark. The fault card (FT01-01) covers it; a cheap spy here would lock the placement now.

Note, not a finding: _write_loop.py:181 uses `except Exception` in `abort()`, which the §6 general rule forbids. It is required by U01-40 step 5 and the §6 `abort_failed` row, so it is plan-mandated and is annotated with noqa and a reason.

## Assessment
**Task quality:** Approved.
**Reasoning:** All five units and all nine test rows match the spec and the rulings. The watermark moves only after commit, with the fault point placed correctly. Aborting after a commit is safe, the deletion set is reloaded at each checkpoint, and every §8.1/§8.2 event and metric is emitted. Coverage is 99-100 % and all gates are clean. The remaining items are polish plus one spec deviation (raise order) that needs a recorded ruling.


## Re-review round 1 (fix commit 86d89bd; scope: m1, m4)

- ✅ **m1** (runner.py:271-273). `connectors.sync.completed` now logs `source = key`, the stream key. The event keeps the §8.1 fields (`source`, `entity`, `mode`, `rows`, `tombstones`, `skipped_deleted`, `files`, `watermark_before`, `watermark_after`, `duration_s`), with `files` as a count. `connectors.sync.started` still logs `source` as the connector name and `stream` as the key. This matches §8.1, which gives `started` a `stream` field and gives `completed` none. `SyncResult.source` stays the connector name, so the U01-36 result is unchanged. The new UT01-92 aggregate test asserts both events per tool. UT01-29 still holds because the servicenow stream key equals the connector name.
- ✅ **m4** (docs/impl/01-connectors.impl.md:75). The row now reads "imported by `runner.py`".
- Run: `pytest -k "UT01_92 or UT01_29" -q -p no:logging` gave 11 passed, no warnings.
- New findings: none. m2, m3, m5 and m6 remain open as Minor. They were out of scope for this round and do not block.

**Verdict (round 1): Approved.**
