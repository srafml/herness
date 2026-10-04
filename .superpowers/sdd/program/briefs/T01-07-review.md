# T01-07 Backfill slices — review (verify agent)

Worktree agent-a35e2706adf8547c4, base 84384c1, head 30eacad (one commit, Co-Authored-By trailer present).

### Spec Compliance
- ✅ Spec compliant.
  - ✅ U01-43 `run_backfill` (backfill.py:143-175): split_range by `backfill_for(entity).slice_days`, `ensure_slices`, `todo` = non-done, `workers = min(max_workers, len(todo))` only with a factory else 1, `ThreadPoolExecutor(thread_name_prefix=f"backfill-{key}")`; per slice stop-check -> `mark_slice_running` -> `guard(key)` -> factory/shared connector -> fresh `DeletionFilter` + `reload()` -> `_write_stream(..., cap=s1, advance_watermark=False)` -> `mark_slice_done(rows, files POSIX relative to data_root)` -> `slice_completed`; on any exception `mark_slice_failed("<Class>: <msg>")` + `slice_failed` + re-raise (backfill.py:95-139). Steps 5-8 in spec order: wait all, first FatalError else first error (slice order), skipped -> `stopped=True` + watermark unchanged, else `set_watermark(min(end, max committed of this call))` or `end` when no rows (binding carry-over) then `_finish` (completed log mode backfill + metrics).
  - ✅ U01-41 `SyncRunner.run_backfill` (runner.py:188-207): files -> `ConfigError("files does not backfill")`; aware `start < end <= now`; `_prepare` (entity + one-time cleanup); tools via `_over_tools` with `stream_key(tool)` and adapter `max_concurrency`; else key = connector name, `cfg.max_concurrency`. Interim `# T01-07:` seam replaced; `# T01-10:` marker intact (runner.py:182).
  - ✅ Log events (§8.1): `connectors.backfill.slice_completed` INFO with source/entity/stream/slice_start/slice_end/rows; `connectors.backfill.slice_failed` WARNING with the same + error_class, attempts.
  - ✅ UT01-37 (bounded concurrency via Barrier(3) + peak counter, all done, watermark min(end,max), cap at end, no-factory sequential, done never rerun, U01-41 validation, key/workers/fetch_of, relative files)
  - ✅ UT01-38 (slice 4 failed with text, others done, same exception raised, watermark unset; fatal-first; first-in-slice-order; resume reruns failed + running only)
  - ✅ UT01-39 (no rows -> watermark = end; stop after 2 -> 2 done/8 pending, stopped true, watermark unchanged; resume; stopped reset)
  - ✅ UT01-93 (slices keyed `monitoring:<tool>`, per-tool guard/watermarks, adapter workers, tool failure/open-breaker isolation)
  - ✅ TH01-11: a fresh DeletionFilter per slice, reloaded before the first write (and at checkpoints by the write loop); a missing reload fails loudly (deletion.py:48-50 raises SchemaViolation), so the control cannot silently lapse.
- ⚠️ Cannot verify / spec items for the controller:
  - U01-43 postcondition "max committed of THIS call" (D-8): a resume that reruns only an early slice sets the watermark to that slice's max, below rows committed by the earlier call (test_backfill.py UT01-38 resume asserts `_WINDOWS[7][0]`). Harmless (set_watermark never moves back; next incremental re-fetches a superset, lake gets duplicate rows that downstream dedupe must absorb) but should be settled in the D-8 revision (e.g. max over all done slices, or `end`). §13 D-8 is still marked "Still open" although U01-43 already states the `end` rule — docs follow-up, not this card's code.
  - FT01-03 (fault test for U01-43) is not in this card's test list; not verified here.

### Strengths
- Clean split: backfill.py 175/260, runner.py 361/390 (29 lines left for T01-08); `_StreamRun` is now a typed Protocol; `FetchOf` lets factory-built connectors fetch from their own instance.
- Commit -> mark_slice_done -> (after all slices) set_watermark ordering is correct; the watermark never moves while any slice is not done; each slice has its own LakeWriter (per WriteLoop), DeletionFilter and (with a factory) connector; ops access per thread (R-10); real LakeWriter names files by ULID so concurrent writers on one partition cannot collide (store/lake.py:296).
- Deterministic concurrency tests (Barrier with timeout, locks, fixed clock); no sleeps; contextvars copied per task so log bindings reach workers.
- Modified T01-06 tests are justified: UT01-32 "no rows -> no watermark" contradicted the binding carry-over; spy/guard patches follow the moved call sites; FakeLake writer numbering made thread-safe.

### Verification run (by reviewer)
- `pytest tests/unit/connectors`: 438 passed, 4 skipped (pre-existing platform skips). Acceptance `-k "UT01_37 or UT01_38 or UT01_39 or UT01_93" --require-test-ids`: 23 passed; test_backfill.py re-run 3x stable (~0.9 s each).
- Coverage (branch): backfill.py 100% (83 stmts, 8 branches); runner.py 99% (miss 117 pre-existing `_default_writer`, partial 336->339); _write_loop.py 100%.
- ruff check/format clean; mypy strict Success (212 files); lint-imports 13 kept; check_module_size exit 0.
- Test IDs in names and docstring first lines; `pytestmark = [unit, usefixtures("guard")]`.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
- None.

#### Minor (Nice to Have)
1. herness/connectors/backfill.py:76 — each ThreadPoolExecutor worker opens its own ops connection via the per-thread `core.connection()` and registers it in `_registry.connections` (store/ops/core.py:117-131); nothing closes it when the worker thread exits (only `reset_connections` or the same thread's `_drop_cached` do). A long-lived job worker therefore accumulates up to `workers` open SQLite handles per backfill call (per tool). Not fixable in this card (core.py is frozen, no per-thread close API); record as a carry-over to impl 02 (e.g. a `release_connection()` the slice task calls in a `finally`).
2. herness/connectors/backfill.py:103-110 — `slice_failed` logs `rows=0` even when the slice committed checkpoint(s) before failing (those rows are in the lake and will be re-ingested on rerun). Acceptable ("same" fields per §8.1) but the value is not the committed count; consider logging the committed rows, or document it.
3. herness/connectors/backfill.py:101-102 — if `mark_slice_failed` itself raises (e.g. StoreBusy after retries) it replaces the original slice error; the original remains only as `__context__`. Edge case; consider logging first or suppressing/chaining explicitly.
4. herness/connectors/_write_loop.py:156-157 (unchanged, now reached from worker threads) — `runner.progress` is called concurrently from slice threads; the U01-37 callback contract should say it must be thread-safe (the T08 job progress sink it will be wired to). ⚠️ for the wiring card.
5. herness/connectors/runner.py:336 — `wm is None` branch of `_finish` is unreachable from current flows (partial branch 336->339). Keep for T01-08/T01-10 as reported, or drop then.
6. herness/connectors/backfill.py:167-170 — a stopped run returns without `connectors.sync.completed` or metrics (spec step 7 does not require them); operators see only the slice logs. Fine per spec; mention in the ops runbook if needed.

### Assessment
**Task quality:** Approved
**Reasoning:** U01-41/U01-43 match the spec step for step, including the binding `end` carry-over, ordering, failure isolation, resume and TH01-11; all card tests pass deterministically with full coverage and clean gates. Remaining items are minor or cross-spec carry-overs (per-thread ops connection release, D-8 wording).

## Re-review 1 (fix round 1, commit 5a07621; scope: finding m3 only)

- ✅ Fix correct (backfill.py:103-107): `mark_slice_failed` is wrapped in `try/except Exception`; the slice's original exception is re-raised by the bare `raise` (the inner handler does not rebind it), the `slice_failed` log is still emitted with the original `error_class`, and BaseException from the bookkeeping (e.g. KeyboardInterrupt) is intentionally not swallowed. The slice stays `running` in the store in that case, which U01-43 reruns from its start — correct resume semantics.
- ✅ No leak: the note is `mark_slice_failed also failed: <ClassName>` (class name only, no message text, no secret/ticket data); no new log fields.
- ✅ Test `test_ut01_38_failing_bookkeeping_does_not_mask_slice_error`: patches `backfill.mark_slice_failed` to raise StoreBusy, asserts the same SourceUnavailable object is raised, the exact note, one slice_failed log with error_class SourceUnavailable, watermark unset. ID in name and docstring.
- Reviewer run: card `-k "UT01_37 or UT01_38 or UT01_39 or UT01_93" --require-test-ids` 24 passed; backfill.py 100% line/branch (86 stmts); ruff clean; mypy clean.
- Minor (no action required): the bookkeeping failure itself is visible only as an exception note, not as its own log event; acceptable since the raised error reaches the job layer with the note.

**Verdict:** Approved
