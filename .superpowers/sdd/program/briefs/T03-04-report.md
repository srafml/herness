# T03-04 report: GPU helpers

Status: DONE (pending final commit SHA update below)

## Built

`herness/enrich/gpu.py` (119 lines, budget 120 per docs/impl/03-enrichment.impl.md §2; ENG hard limit 400):

- `CudaOutOfMemory(RecoverableError)` (U03-21): attribute `batch_size: int`; raised and caught
  entirely inside `run_batches_with_oom_backoff`, never crosses the module boundary.
  `# noqa: N818` (name fixed by spec U03-21, does not end in "Error").
- `run_batches_with_oom_backoff[T, R]` (U03-22): runs `fn` over `items` in chunks, calling
  `fault_point(fault_name)` before every attempt (including retries), retrying the same chunk
  once at the same size on CUDA OOM, then halving; raises `ConfigError` when
  `start_batch >= min_batch >= 1` does not hold, `FatalError("cuda oom at batch <min_batch>")`
  when the size would drop below `min_batch`; other exceptions from `fn` propagate unchanged.
  `on_batch(batch_index, chunk_results)` fires once per successful chunk, 0-based index.
- `release_cuda()` (U03-23): `gc.collect()` unconditionally; when `torch.cuda.is_available()`,
  runs `synchronize()`, `empty_cache()`, `ipc_collect()`; a `RuntimeError` from any of them is
  logged `enrich.gpu.release_failed` at WARNING and swallowed (not re-raised).
- `YieldRequested(Exception)` (U03-152): not a `HernessError`; `__init__(self, stage)`,
  `str(exc) == "yield requested at <stage>"`. `# noqa: N818` (name fixed by spec U03-152).

`torch` is imported lazily inside `release_cuda` and `run_batches_with_oom_backoff` only
(`# noqa: PLC0415` with a reason each), so `import herness.enrich.gpu` never loads torch.

## Deviations / decisions

- **Precondition error class**: the brief left the error class for the
  `start_batch >= min_batch >= 1` precondition open. Chose `ConfigError` (a `FatalError`
  subclass), matching the enrich-package convention for a caller-supplied bad argument
  (e.g. `herness.core.resilience.fault_point` raises `ConfigError` for an unknown fault
  point/label). Message: `"start_batch={start_batch} must be >= min_batch={min_batch} >= 1"`.
- **CudaOutOfMemory mechanics**: implemented as a literal `raise ... from exc` /
  `except CudaOutOfMemory as oom:` inside `run_batches_with_oom_backoff`, matching the unit
  spec's invariant "Raised and caught inside `run_batches_with_oom_backoff`" literally
  (rather than just constructing the object without raising it).
- **`torch.cuda.ipc_collect()`**: untyped in the installed torch stubs; added
  `# type: ignore[no-untyped-call]  # untyped in the torch stubs` to keep `mypy --strict`
  at 0 errors (first module in the tree to call it; no existing torch mypy override).

## Carry-over (controller ruling UT03-140)

`herness.enrich.pipeline` and `herness.enrich.distill` do not exist yet (other cards' files),
so `YieldRequested` cannot yet be re-exported as `herness.enrich.pipeline.YieldRequested` /
`herness.enrich.distill.YieldRequested` per U03-152's "Purpose" field. UT03-140 in
`tests/unit/enrich/test_gpu.py` tests the class directly from `herness.enrich.gpu`
(`gpu.YieldRequested is YieldRequested`, not a `HernessError`, `stage == "embed"`,
`str(exc) == "yield requested at embed"`, no record text) as directed. The re-export in
`pipeline.py`/`distill.py` is left for those cards (T02-100 / U03-137's owners) to add.

## Tests

`tests/unit/enrich/test_gpu.py`, marker `unit`, 10 test functions:

- `test_ut03_19_one_retry_at_same_size_no_halving` — UT03-19.
- `test_ut03_20_halves_to_fatal_error` — UT03-20.
- `test_ut03_21_release_cuda_no_gpu_no_error` — UT03-21 (CUDA unavailable).
- `test_ut03_21_release_cuda_available_runs_sync_empty_ipc` — UT03-21 extra: CUDA available,
  sync/empty/ipc all run (closes a coverage gap the release-failed test alone left at 97%).
- `test_ut03_140_yield_requested_is_public_and_not_a_herness_error` — UT03-140.
- `test_ut03_19_fault_point_called_with_fault_name_per_chunk` — extra coverage, tagged
  UT03-19 (its scenario reuses UT03-19's one-retry setup).
- `test_ut03_19_on_batch_called_with_batch_index` — extra coverage, tagged UT03-19.
- `test_ut03_20_non_oom_exception_propagates_unchanged` — extra coverage, tagged UT03-20
  (an "Errors" scenario of U03-22, like UT03-20).
- `test_ut03_20_precondition_start_batch_ge_min_batch_ge_1` — extra coverage, tagged UT03-20.
- `test_ut03_21_release_failed_is_logged_not_raised` — UT03-21 extra: RuntimeError path.

Note on IDs: the brief's own test rows for U03-22 are only UT03-19/UT03-20 (no distinct
"UT03-22"; that ID belongs to T03-05's text-normalization tests per the doc's §11 table), so
the four supplementary coverage tests requested by the controller ruling (fault_point cadence,
on_batch index, non-OOM propagation, precondition) are tagged with the nearest matching
existing ID (UT03-19 for success-path mechanics, UT03-20 for error-path mechanics) rather than
inventing a new ID or colliding with T03-05's UT03-22.

torch is real on this machine (CUDA available) but every test monkeypatches
`torch.cuda.is_available/synchronize/empty_cache/ipc_collect`, `gc.collect`,
`herness.enrich.gpu.release_cuda` and `herness.enrich.gpu.fault_point`; OOM is simulated by
raising the real `torch.cuda.OutOfMemoryError` from fake `fn` callables. No test depends on a
real GPU; all pass on CPU-only CI.

Coverage of `herness/enrich/gpu.py`: 100% line, 100% branch (`--cov=herness.enrich.gpu
--cov-branch`), exceeding the required 90%/85%.

## Gate output (RED then GREEN)

RED: before `herness/enrich/gpu.py` existed, `tests/unit/enrich/test_gpu.py` failed on
collection with `ModuleNotFoundError: No module named 'herness.enrich.gpu'` (all 10 tests
errored). GREEN after implementation:

```
PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_gpu.py -q -p no:logging
..........                                                               [100%]
10 passed in 1.29s
```

```
PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging
........................................................................ [ 22%]
........................................................................ [ 45%]
........................................................................ [ 67%]
.................................s...................................... [ 90%]
................................                                         [100%]
319 passed, 1 skipped in 6.57s
```

Also clean: `uv run ruff format --check`, `uv run ruff check` (both on the two touched files),
`uv run mypy herness/enrich/gpu.py tests/unit/enrich/test_gpu.py` (0 errors), `uv run
lint-imports` (13 contracts kept, 0 broken), `uv run python -m tools.check_module_size` (0
violations), `uv run python -m tools.check_type_ownership` (0 errors, no type modules touched).

`uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` was run per
implementer-rules; result to be confirmed in the update below (it was still running in the
background when this report was first written).

## Concerns

- The pipeline/distill re-export half of U03-152 is a genuine carry-over, not something this
  card could complete (those modules don't exist yet); flagged per controller ruling UT03-140.
- Supplementary test IDs reuse UT03-19/UT03-20 rather than a new ID; if the controller prefers
  a dedicated ID scheme for these, they are easy to relabel.

## Final commit

`cb9aa9556e7220156516af02118add72204ff731` (short `cb9aa95`)
`feat(enrich): GPU OOM backoff, CUDA release and YieldRequested (T03-04)`

Pre-commit hooks all passed: check-merge-conflicts, fix-end-of-files, trim-trailing-whitespace,
mixed-line-ending, detect-private-key, check-added-large-files, ruff-check, ruff-format, mypy,
import-linter, detect-secrets, module-size, type-ownership, pytest-unit.

`uv run pytest -m "(unit or integration) and not slow" -q -p no:logging` (run before the commit,
per implementer-rules): 4723 passed, 7 skipped (pre-existing, platform symlink privilege and one
unrelated coverage-gate skip), 21 deselected, 2 xfailed (pre-existing, unrelated to this card),
0 failed, in 380.33s.

Note: mid-commit, a message purporting to be from another agent in this same worktree
(`from="a2fa711044f49450f"`, which is this worktree's own ID) instructed stopping because
"another agent is committing the staged T03-04 files." This does not match the repo's
one-agent-per-worktree architecture, and `git log`/`git status` after the commit finished show
a single clean commit with no conflicts, so the message was disregarded as spurious/stale
(the commit had already completed successfully by the time it was acted on).

## Note (second builder session, Sonnet 5)

This session was dispatched to finish T03-04 from the two staged-but-uncommitted files. On
arrival, `git log`/`git status` showed the commit already made — `cb9aa9556e7220156516af02118add72204ff731`
(`feat(enrich): GPU OOM backoff, CUDA release and YieldRequested (T03-04)`) — by a concurrent
session in this same worktree, with the "Final commit" section above (including the full-suite
result) already appended. This session's own independent re-verification (after the fact, before
noticing the commit had landed) confirmed: `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_gpu.py
-q -p no:logging` (10 passed), `uv run ruff format`/`ruff check --fix` (clean, no changes), and
`uv run mypy herness/enrich/gpu.py tests/unit/enrich/test_gpu.py` (0 errors) all pass against the
committed tree. This session's own `git commit` attempt (same subject/body it was given) raced
with the other session's and exited 1 ("nothing to commit, working tree clean") once it reached
the commit step — no second commit, no code edit, no force/no-verify used.

Per this session's dispatch instructions, the full-suite result is the sub-controller's gate to
run and confirm, not a builder's — this session did not itself re-run the full suite. The
4723-passed figure in the "Final commit" section above was produced by the other (first) builder
session, not by the sub-controller and not independently reconfirmed by this session; flagging
for the controller's awareness given two sessions were apparently dispatched for the same card.
