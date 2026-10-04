# T01-07 Backfill slices — build report

Worktree: D:\herness\.claude\worktrees\agent-a35e2706adf8547c4 (branch worktree-agent-a35e2706adf8547c4, base 84384c1)
Final commit: 30eacad feat(connectors): add resumable parallel backfill slices (T01-07) (the wip checkpoint a2f54cc was folded into it with a soft reset onto 84384c1: one commit per card)

## What was built
- `herness/connectors/backfill.py` (new, U01-43 `run_backfill`): split `[start, end)` by `backfill_for(entity).slice_days`
  (`split_range`), `ensure_slices`, run non-done slices in a `ThreadPoolExecutor(max_workers=min(max_workers, len(todo))`
  when `runner.connector_factory` is set else 1, `thread_name_prefix=f"backfill-{key}")`. Per slice: stop check ->
  `mark_slice_running` -> `guard(key)` -> factory connector or shared -> fresh `DeletionFilter` + `reload()` (TH01-11) ->
  `runner._write_stream(..., cap=s1, advance_watermark=False)` -> `mark_slice_done(rows, files as POSIX relative to
  data_root)` + `connectors.backfill.slice_completed`; on any exception `mark_slice_failed("<Class>: <msg>")` +
  `connectors.backfill.slice_failed` (WARNING, with error_class, attempts) and re-raise. After all slices: first
  FatalError else first error (slice order); a skipped slice -> `runner.stopped = True`, watermark unchanged, no
  completed log; else `set_watermark(min(end, max committed of this call))` or `end` when nothing was committed
  (binding carry-over, spec D-8), then `runner._finish` (completed log mode backfill + §8.2 metrics).
  Each task runs in a copy of the caller's contextvars (log bindings reach worker threads).
- `herness/connectors/runner.py`: `SyncRunner.run_backfill(entity, start, end)` (U01-41): `files` -> ConfigError("files does not
  backfill"); naive or not `start < end <= clock()` -> ConfigError; `_prepare` (entity check + one-time cleanup); tool
  streams through `_over_tools` (same per-tool error handling/aggregation as U01-38 step 3) with key `stream_key(tool)`
  and `max_workers` = adapter `max_concurrency`; otherwise key = connector name, `max_workers = cfg.max_concurrency`.
  The interim `# T01-07:` single-slice seam body of `_backfill_stream` is replaced by a call to U01-43 (lazy import,
  backfill imports runner). The no-watermark incremental path now runs the sliced backfill too.
- Parked m3 addressed: `_StreamRun` is now a `Protocol` with the keyword signature `(entity, *, key, fetch_of, workers)`;
  streams carry `fetch_of: FetchOf = Callable[[Connector], Fetch]` (needed so factory-built connectors fetch from their
  own instance; `partial(_tool_sync, tool)` for tools).
- `# T01-10:` marker untouched.

## Tests (tests/unit/connectors/test_backfill.py, helpers tests/unit/connectors/_backfill_data.py)
- UT01-37: 10 slices, factory, max_concurrency 3: a `threading.Barrier(3)` gate proves exactly 3 fetches overlap and the
  counter's peak is 3 (<= 3), 3 threads named `backfill-servicenow*`, 10 factory instances, all slices done with rows/files,
  watermark = min(end, max); cap at end for a late row; no factory -> sequential on the shared connector; done slices
  never rerun (second call fetches nothing, watermark = end); U01-41 validation (naive start/end, empty, reversed, future
  end, unknown entity, files); key/max_workers/fetch_of passed to U01-43; files relative to data_root.
- UT01-38: slice 4 SourceUnavailable -> slice 4 failed ("SourceUnavailable: source down", attempts 1), others done, same
  exception raised, watermark unset, slice_failed log fields; fatal-first; first-in-slice-order; resume reruns only the
  failed and a left-`running` slice (attempts 2/3), then watermark = min(end, max of this call).
- UT01-39: no rows anywhere -> all done, watermark = end; should_stop true after 2 slices -> 2 done, 8 pending, stopped
  true, watermark (seeded) unchanged, no completed log; the resume runs only the pending slices, watermark = end;
  `stopped` resets on the next run.
- UT01-93: monitoring backfill -> sync_slice rows keyed `monitoring:prometheus` / `monitoring:datadog`, guard per slice per
  tool key, per-tool watermarks; adapter max_concurrency (prometheus 4, datadog 2) and tool-bound fetch; a failing tool
  and an open-breaker tool are isolated (skipped_open, connectors.sync.failed mode backfill).
- No test sleeps (fixed clock + barrier/locks); ran test_backfill.py 4x, stable.

## Changed T01-06 tests / support (interim seam replaced)
- test_runner.py UT01-32: spy updated to the new `_backfill_stream` keywords (asserts workers + fetch_of);
  "interim single-slice" tests replaced by a sliced-backfill test and "no rows -> watermark = end" (the binding carry-over;
  the old test asserted no watermark).
- test_runner_write.py: the set_watermark spy patches `backfill.set_watermark` instead of `runner.set_watermark` (moved).
- conftest.py `guard` fixture also patches `herness.connectors.backfill.guard`.
- tests/support/fake_lake.py: writer creation under a lock so parallel slices get distinct writer numbers.

## Deviations / interpretations / spec notes
- slice_failed log carries `rows=0` ("same" fields as slice_completed) in addition to error_class and attempts;
  attempts = planned attempts + 1 (the value after mark_slice_running).
- Stopped run returns a SyncResult built directly (watermark_before == watermark_after), without the completed log or
  metrics (step 8 only).
- Spec note (U01-43 postcondition, D-8): "max committed of this call" means a resume that reruns only an early slice sets
  the watermark to that slice's max (lower than rows committed by earlier calls). Harmless (set_watermark never moves
  back; the next incremental re-fetches a superset) but a later spec revision may prefer the max over all done slices.
  D-8 in §13 is still marked open although U01-43 already states the `end` rule.
- `_finish`'s `wm is None` branch is no longer reachable from current flows (backfill always sets a watermark first);
  left for T01-08/T01-10.

## Gates
- ruff format/check clean; mypy strict: Success (212 files); lint-imports 13 kept; check_type_ownership exit 0;
  check_module_size exit 0; pre-commit hooks pass (pytest-unit SKIPped per known-red rule).
- `pytest tests/unit/connectors -q -p no:logging`: 438 passed, 4 skipped (also with --require-test-ids).
- Acceptance `-k "UT01_37 or UT01_38 or UT01_39 or UT01_93"`: 23 passed.
- RED: before backfill.py existed, `pytest tests/unit/connectors/test_backfill.py` -> ModuleNotFoundError: No module named
  'herness.connectors.backfill' (collection error).
- Coverage (branch): backfill.py 100% (83 stmts, 8 branches); runner.py 99% (line 117 `_default_writer` body pre-existing,
  branch 336->339 see above); _write_loop.py 100%.

## Line counts vs budget (§2)
- herness/connectors/backfill.py 175 / 260
- herness/connectors/runner.py 361 / 390 (was 308; 29 lines left for T01-08)
- herness/connectors/_write_loop.py 183 / 200 (unchanged)

## Fix round 1 (review m3)
- Commit: 5a07621 fix(connectors): keep the slice error when marking it failed fails (T01-07)
- backfill.py `run_slice`: `mark_slice_failed` is wrapped; if it raises (Exception), the slice's original exception
  still propagates and carries the note `mark_slice_failed also failed: <Class>` (class name only, no message);
  slice_failed is still logged. backfill.py now 178 / 260 lines.
- Test: `test_ut01_38_failing_bookkeeping_does_not_mask_slice_error` (mark_slice_failed raising StoreBusy -> the
  original SourceUnavailable is raised with the note; watermark unset).
- Gates: ruff/format clean, mypy Success, check_module_size exit 0; tests/unit/connectors 439 passed, 4 skipped;
  acceptance -k 24 passed; backfill.py coverage 100% line and branch.
