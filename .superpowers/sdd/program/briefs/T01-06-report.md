# T01-06 report: Sync runner, incremental flow and write loop

Worktree: D:\herness\.claude\worktrees\agent-ab62b4aa515276044 (branch worktree-agent-ab62b4aa515276044, base 221fd6e)
Status: DONE_WITH_CONCERNS (a private sibling module was added to stay within budget; see Deviations)
Checkpoint commit: b68c599 wip(T01-06): runner, write loop and card tests green
Final commit: 4e6d0d6 feat(connectors): T01-06 sync runner incremental flow and write loop

## What was built
- `herness/connectors/runner.py` (307/390 lines):
  - `SyncResult` (U01-36): frozen dataclass with `to_dict`.
  - `SyncRunner` (U01-37): raises ConfigError on a connector-name mismatch; public attributes `skipped_open` and `stopped`; runs the orphan cleanup on `data_root/raw` once per instance.
  - `run_incremental` (U01-38): tool fan-out that isolates each tool's errors and applies the spec's raise order. A `# T01-10:` marker sits where the files dispatch goes.
  - `_incremental_stream` (U01-39): guard, watermark read, backfill seam or since/until window, zero result, started log, DeletionFilter reload, write loop, completed log and the §8.2 metrics.
  - `_backfill_stream` seam (ruling 2): interim single slice with `advance_watermark=False`. It calls `set_watermark(min(max_committed, end))` once, only when rows were committed, and returns mode "backfill". Marked `# T01-07: replaced by sliced U01-43`.
  - `_write_stream`: the U01-40 contract; delegates to the loop in the sibling module.
  - `_over_tools(entity, conn, mode, run)`: factored out so T01-07's `run_backfill` can reuse the same per-tool error handling and aggregation (U01-41 step 2).
- `herness/connectors/_write_loop.py` (183 lines, new private sibling):
  - `WriteLoop` (U01-40 steps 1-5):
    - skips empty batches and applies the deletion filter;
    - on drift, logs, emits the metric, and checkpoints when cp_rows > 0;
    - writes, then checkpoints on `checkpoint_rows` or a writer age of 600 s (`LAKE_MAX_OPEN_S`);
    - each checkpoint runs commit -> `fault_point("connector.before_watermark", source=key)` -> ordered `set_watermark(min(max, cap))` -> deletion reload -> progress -> `connectors.sync.checkpoint_committed` -> new writer;
    - the final commit runs even when the writer is empty; unordered streams set the watermark once at the end;
    - any BaseException aborts the writer. If the abort itself fails, it logs `connectors.lake.abort_failed` and the original error propagates.
  - Also defines `StreamSpec`, `StreamOutcome` and `metric()` (MetricSample builder, component `connectors`).
- `tests/support/fake_lake.py`: `FakeLake` writer factory and `FakeLakeWriter`.
  - One shared, ordered event log (open/write/commit/abort).
  - `commit()` returns a real `LakeFileSet` with fake paths under `root`, the row count and the max `_source_updated_at`.
  - `fail_on_commit` and `abort_error` knobs; `.factory()` returns a typed cast.
- Tests:
  - `tests/unit/connectors/test_runner.py`: UT01-29, -30, -32, -35.
  - `test_runner_write.py`: UT01-31, -33, -34, -36.
  - `test_runner_tools.py`: UT01-92.
  - `_runner_data.py`: fake connector, fake tool connector, FakeGuard, batch builder, settings builders.
  - `conftest.py`: `lake` and `guard` fixtures; the runner is imported lazily inside the fixture.
  - The tests use the real `ops_store` fixture for watermarks, deletion requests and metric_sample, a frozen clock, and structlog `capture_logs` for the §8.1 events.
- docs/impl/01-connectors.impl.md §2: added a module-map row for `_write_loop.py` (budget 200) with the build note.
- `.secrets.baseline`: line-number shift only, caused by the docs row insert; LF endings kept.

## Tests
- Card tests: `PYTHONUTF8=1 uv run pytest -k "UT01_29 or UT01_30 or UT01_31 or UT01_32 or UT01_33 or UT01_34 or UT01_35 or UT01_36 or UT01_92" -q -p no:logging` -> 29 passed.
- `tests/unit/connectors` -> 415 passed, 4 skipped (the symlink tests, skipped by host policy).
- RED evidence: before runner.py existed, all three test modules failed collection with `ModuleNotFoundError: No module named 'herness.connectors.runner'`.
- UT01-31 asserts:
  - the exact order open/commit/set_watermark, three times;
  - k45 is dropped after the checkpoint-2 reload (its deletion request is created after checkpoint 1);
  - the progress notes and the checkpoint logs.
- UT01-34 asserts a commit before the drifted batch, two writers, the drift log fields and the drift metric.
- UT01-36 asserts one set_watermark, after the last commit, capped at until.
- UT01-92 asserts datadog's error is isolated, prometheus is committed and advanced, datadog's watermark is unchanged, the error is re-raised, and the `connectors.sync.failed` fields. Extra cases cover:
  - the aggregate result;
  - `skipped_open` and the all-open case;
  - fatal raised over retryable;
  - an "other" error that is not swallowed;
  - a backfill inside the aggregate.
- Full suite not run (controller rule); the KNOWN-RED pair was not touched.

## Coverage (branch mode, tests/unit/connectors)
- runner.py: 167 statements, 1 missed (the `_default_writer` body, `LakeWriter(source, entity)`); 22 branches, 0 partial -> 99 %.
- _write_loop.py: 116 statements, 20 branches -> 100 %.

## Gates
- ruff format and ruff check: clean.
- mypy strict: no issues (203 files).
- lint-imports: 13 kept, 0 broken.
- check_module_size: exit 0.
- check_type_ownership: exit 0.
- pre-commit hooks pass; the pytest-unit hook was skipped with SKIP=pytest-unit per ruling 3.

## Deviations / interpretations
1. **Private sibling module `herness/connectors/_write_loop.py`.**
   - Reason: after `ruff format`, the single-file version was 503 lines against a 390 budget, and runner.py must still absorb T01-07 (`run_backfill` wrapper), T01-08 (`run_reconcile`) and T01-10 (files dispatch).
   - The loop state moved to the sibling. `SyncRunner._write_stream` keeps the U01-40 signature and delegates.
   - runner.py is now 307/390, leaving 83 lines of headroom. Module-map row added in §2 (budget 200).
2. **`SyncResult.to_dict(*, data_root: Path | None = None)`** is an additive keyword. The default is `get_config().paths.data` per the spec; a runner built with a custom `data_root` can pass it.
3. **Tool-stream raise order.** After FatalError and RetryableError, any other kept HernessError (such as a RecoverableError) is raised too, instead of returning an aggregate. The spec's order would have swallowed it. The all-skipped CircuitOpen rule comes after that.
4. **Aggregate mode and watermarks.** Aggregate `mode` is "backfill" when any tool ran the backfill path, else "incremental". Aggregate watermarks are the U01-36 minimum over the tools that produced a result; skipped or failed tools are not in the aggregate.
5. **Log `source` field.**
   - Events that have a `stream` field (`started`, `checkpoint_committed`, `failed`, `skipped_open_circuit`): `source` is the connector name and `stream` is the key.
   - Events without one in §8.1 (`schema_drift.detected`, `lake.abort_failed`): `source` is the stream key, so a monitoring tool stays identifiable.
   - Metrics use the stream key as `source` (§8.2).
6. **No `job_id` in `connectors.sync.started`.** The runner has no job id; the job layer binds it through `herness.core.logging.bind_ids(job_id=...)` (structlog contextvars).
7. **`checkpoint_committed` fields.** `rows` is the stream's cumulative row count (the same number as the progress note), `files` is that checkpoint's file count, and `watermark` is the value set (None for unordered or non-advancing streams). The event is not emitted for the final end-of-stream commit, since spec step 4 only applies the watermark rule there.
8. **`skipped_open` and `stopped`** are reset at the start of every public run.
9. **Single-stream CircuitOpen.** On a non-tool stream, `CircuitOpen` propagates without being added to `skipped_open` or logged. U01-38 step 4 returns `_incremental_stream` directly, and handle_sync owns the outcome.
10. **Interim backfill with no rows.** Per ruling 2, it sets no watermark, so the next run backfills again. U01-43 (T01-07) sets the watermark to `end` in that case.
11. **Post-commit ops-store writes.** Metric and log writes after a commit go to the ops store, and a StoreBusy there propagates, because the component allows no `except Exception`. The data and watermark are already committed at that point.

## Carry-overs
- T01-07: replace the `_backfill_stream` body with U01-43. Either keep the seam signature or route `run_backfill` through `_over_tools(entity, conn, "backfill", ...)`. Set the watermark to `end` when no rows were committed.
- T01-10: add the files dispatch at the `# T01-10:` marker in `run_incremental`.
- The repo config does not load as a whole on base: `metrics.yaml` has `metrics: []` until T04-08. Tests that need `get_config().paths.data` stub `runner.get_config` (the `paths_config` fixture in test_runner.py).
- The `_default_writer` line is uncovered, because building a real LakeWriter needs a loadable config.

## Concerns
- The sibling module is a structural choice made under ruling 4. Please confirm it.
- Deviation 3 (raising other kept errors) extends the spec's raise order.

## Fix round 1 (review: Approved with 6 minors; m1 and m4 fixed)
Commit: 86d89bd fix(connectors): address T01-06 review round 1
- m1: `connectors.sync.completed` now logs the stream key as `source`, because §8.1 gives that event no `stream` field. Per-tool monitoring completions now show `monitoring:<tool>`, which is consistent with deviation 5. `connectors.sync.started` has a `stream` field in §8.1, so it keeps `source` = connector name and `stream` = key. UT01-92 (`test_ut01_92_aggregate_result`) now asserts that the started events carry `stream = monitoring:<tool>` and the completed events carry `source = monitoring:<tool>`.
- m4: the §2 module-map row for `_write_loop.py` now says it is imported by `runner.py`; the earlier "re-exported" wording was wrong.
- Parked by ruling: m3, m5, m6. Deviation 3 accepted by ruling.
- Tests: card filter 29 passed; tests/unit/connectors 415 passed, 4 skipped. ruff, mypy and check_module_size are clean. runner.py is now 308/390 lines.
