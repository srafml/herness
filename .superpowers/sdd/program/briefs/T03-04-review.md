# T03-04 review: GPU helpers

## Spec compliance

| Unit/Test | Result |
|---|---|
| U03-21 CudaOutOfMemory (RecoverableError, `batch_size: int`, raised+caught only inside `run_batches_with_oom_backoff`) | ✅ |
| U03-22 `run_batches_with_oom_backoff` algorithm (size=start_batch, i=0, retried=False loop; fault_point(fault_name) per attempt; retry same chunk once at same size, then halve+reset retried; `FatalError("cuda oom at batch <min_batch>")` when size<min_batch; on success append+`on_batch(batch_index, results)`+advance+reset retried) | ✅ verified by hand-tracing UT03-19/UT03-20 against the diff, matches exactly |
| U03-22 postconditions (input order preserved, size never grows) | ✅ |
| U03-22 errors (non-OOM exceptions propagate unchanged; precondition `start_batch>=min_batch>=1`) | ✅ — precondition violation raises `ConfigError`; verified this matches the codebase's own precedent (`herness/core/resilience/faults.py:335`, `fault_point` raises `ConfigError` for an unknown fault point/label), so the implementer's documented deviation is sound, not invented |
| U03-23 `release_cuda` (`gc.collect()` unconditional; when `torch.cuda.is_available()`: synchronize/empty_cache/ipc_collect; `RuntimeError` → WARNING `enrich.gpu.release_failed`, swallowed) | ✅ |
| U03-152 `YieldRequested` (`Exception` subclass, not `HernessError`; `stage` attr; `str(exc) == "yield requested at <stage>"`; no record text) | ✅ |
| U03-152 pipeline/distill re-export | ⚠️ Not done — accepted per controller ruling (modules don't exist yet); UT03-140 tests the class directly from `gpu`, as directed |
| UT03-19 (one retry at 128, no halving, 300 items, order preserved) | ✅ — traced by hand: call_sizes `[128,128,128,44]`, matches |
| UT03-20 (128,128,64,64,…,1,1 then FatalError) | ✅ — traced by hand, matches exactly; message `"cuda oom at batch 1"` |
| UT03-21 (CUDA unavailable → no error, `gc.collect` called) + release_failed WARNING path | ✅ |
| UT03-140 (re-export carry-over) | ✅ per controller ruling, tests direct class identity/behavior instead |
| Module budget (gpu.py ≤120 lines) | ✅ 119 lines (`wc -l` confirmed) |
| Coverage ≥90% line / ≥85% branch | ✅ ran `pytest tests/unit/enrich/test_gpu.py --cov=herness.enrich.gpu --cov-branch`: **100% line (65/65), 100% branch (12/12)** |
| Test IDs / docstrings / `pytestmark` | ✅ every test name/docstring starts with its ID; module sets `pytestmark = pytest.mark.unit` |
| Lint/type checks | ✅ ran independently: `ruff check` clean, `ruff format --check` clean, `mypy` 0 errors on both files |
| No record text in logs | ✅ `enrich.gpu.oom_retried` logs only `batch_size`/`retried`; `enrich.gpu.release_failed` logs no payload |
| Lazy torch import (controller ruling) | ✅ both `release_cuda` and `run_batches_with_oom_backoff` import torch locally with `# noqa: PLC0415` |

## Findings

### Critical
None.

### Important
None.

### Minor
- `herness/enrich/gpu.py:38-41` — `CudaOutOfMemory.__init__` passes `batch_size` into `super().__init__(msg, batch_size=batch_size)`, i.e. through `HernessError`'s `**context` kwarg, in addition to setting `self.batch_size` directly. Every sibling error class with an `_extra_attrs` entry in `herness/core/errors.py` (`RateLimited`, `CircuitOpen`, `ModelRefused`) deliberately keeps its extra attribute *out* of `**context` and only sets it as a dedicated attribute after calling `super().__init__(message, hint=hint, details=details, **context)`. Because `_herness_fields` (`herness/core/errors.py:356-365`) first emits `out[name]` for each `_extra_attrs` name, then emits `out[ctx_key]` (falling back to `ctx_<key>` if the name collides) for every `.context` item, `to_log_fields(CudaOutOfMemory(...))` would emit a duplicate field `ctx_batch_size == batch_size`. This is currently inert because U03-21's invariant holds in this diff — the exception never crosses the function boundary and is never routed through the generic `to_log_fields` path — but it deviates from the file's own established constructor convention and would be a latent duplicate-field bug if that invariant were ever broken by a future edit. Not blocking; a one-line fix (drop `batch_size=batch_size` from the `super().__init__` call) would align it with the rest of the file.
- Supplementary coverage tests `test_ut03_19_fault_point_called_with_fault_name_per_chunk`, `test_ut03_19_on_batch_called_with_batch_index`, `test_ut03_20_non_oom_exception_propagates_unchanged`, `test_ut03_20_precondition_start_batch_ge_min_batch_ge_1` reuse UT03-19/UT03-20 IDs for scenarios not literally described by those rows (e.g. the precondition check has no dedicated row in the brief's §11 table). This is disclosed by the builder in the report's "Concerns" section and does not violate `--require-test-ids` (each ID still has ≥1 function, several functions may share an ID per global-constraints.md), so it is a documentation/labeling nit only, not a defect.

## Verdict: Approved
