# T03-10 Calibration — build report

Status: DONE
Commit: dc00c47 feat(enrich): calibration (T03-10), on HEAD 48f28a3, worktree agent-ad8bea210b9833b65

## Built
- `herness/enrich/calibrate.py` (273 lines; budget 280):
  - `apply_temperature` (U03-42). bool: clip, then sigmoid(logit/t), returning [p', 1-p']. choice/score: softmax(log(p+1e-9)/t) with max subtraction. Raises `ConfigError` when t <= 0 or non-finite, and also when the input is not 2-D or a bool matrix does not have 2 columns.
  - `fit_temperature` (U03-43): `minimize_scalar` bounded over (0.05, 10), xatol 1e-4, maxiter 500. Returns 1.0 when `res.success` is false. It shares a private `_fit` that returns None on failure, so `cross_fit` can mark the question uncalibrated.
  - `ece` (U03-44): as specified (stable sort, `array_split` into min(n_bins, n) bins, argmax ties go to the lowest index).
  - `CalibrationResult` (U03-45): frozen, slots. `__post_init__` enforces "uncalibrated implies T == 1.0" and raises `ConfigError` otherwise.
  - `cross_fit` (U03-46): steps 1-4 verbatim. Any failed fit (fold or all rows) also gives the uncalibrated result.
  - `CalibrationStore` (U03-47):
    - Paths: Laya files at `laya_dir(version)/calibration.json`, other deciders at `calibration_file(...)`.
    - Reads are capped at 1 MB and validated by private pydantic models (`extra="forbid"`, strict, bounds on T and on ece/accuracy). They are memoized per path and st_mtime_ns.
    - `save` merges with the existing entries, writes canonical_json atomically (private tmp + fsync + os.replace helper, temp file removed on failure), sets `fitted_at = clock.format_utc(clock.now())` and drops the memo entry.
    - `as_table` returns the five columns, with questions sorted within each (decider, version).
- `tests/unit/enrich/test_calibrate.py` (335 lines): UT03-39..UT03-44 and PT03-04 (hypothesis, 150 examples), all seeded and fast.

## Deviations / interpretations
1. Store file identity check (the spec leaves this open): a Laya file whose `question_set_version` differs from the requested qsv is treated as missing, as the spec says. Any other mismatch between the file's decider, decider_version or question_set_version and the requested key raises `ConfigError` naming the file, because it is a corrupt or misplaced file. For non-Laya files the qsv is part of the path.
2. When a Laya file for another qsv is saved over, the old entries are replaced rather than merged, because `load` returns {} for them. Only one qsv's temperatures are kept per Laya version.
3. Extra input validation in `apply_temperature` (shape check) that the spec does not list. It raises `ConfigError`, the error class the spec already uses.
4. `from scipy import optimize  # type: ignore[import-untyped]`: scipy ships no py.typed and the lock has no stubs. I used this local ignore instead of editing the mypy overrides in pyproject.
5. `CalibrationResult.__post_init__` raises `ConfigError` when the invariant is broken. The spec says "Errors: none" but lists the invariant, so I enforce it; a file that breaks it is reported as malformed.

## Tests / gates
- RED: collection ImportError (`cannot import name 'calibrate'`) before the module existed.
- GREEN: `pytest tests/unit/enrich` gives 293 passed, 1 skipped (a symlink privilege skip that was already there). `uv run` works again and gives the same result.
- UT03-40: the fitted T is within 5 % of 2 for both choice (n=6000, K=4) and bool. UT03-41 is exact to 1e-12 against a Fraction hand computation on shuffled input.
- Full `-m "(unit or integration) and not slow"`: 1867 passed, 3 skipped, 1 xfailed.
- Coverage of calibrate.py: 100 % line and 100 % branch (173 statements, 28 branches).
- Gates:
  - ruff format / check: clean.
  - mypy strict: 0 errors (81 files), and the test file is also clean under mypy.
  - lint-imports: 11 kept.
  - check_module_size: exit 0.
  - check_type_ownership: exit 0. I first imported via the `herness.core.types.decisions` submodule, the check flagged OWN041, and I fixed it to `from herness.core.types import QuestionType`.

## Carry-overs
- T03-08 cache and other consumers (`EnsembleDecider`, gate/resolve, `evaluate_candidate`) import `CalibrationStore` and `apply_temperature` from here.
- The degraded health reason `calibration_missing` will need a way to check whether a file exists; `load() == {}` covers it today.

## Fix round 1
Commit: 1df3ddd fix(enrich): address T03-10 review round 1 (T03-10)
- M2: `save` validates the merged document with the private `_File` model before writing. The entry bounds match the loader's: T in [0.05, 10], ece/ece_raw/accuracy in [0, 1], n >= 0. On a failure it raises ConfigError ("out of bounds") and writes nothing, and the existing file is left unchanged. New test: `test_ut03_44_store_save_rejects_out_of_bounds`, covering T=0.01 on a new key and T=20 merged into an existing file.
- M4: the file is read with a bounded `handle.read(1 MB + 1)` and rejected if longer. This replaced the stat size check, which was followed by an unbounded read.
- M3: UT03-41 now also pins the hand value as the literal Fraction(1693, 6000) and checks `ece` against 1693/6000 to 1e-12.
- M6: the memo test patches `Path.open` to fail and asserts that a second load with an unchanged mtime is served from the memo.
- M1, M5, M7 left unchanged, as instructed.
- Gates:
  - ruff format / check: clean.
  - mypy strict: 0 errors, and the test file is also clean.
  - lint-imports: 11 kept.
  - check_module_size: exit 0.
  - check_type_ownership: exit 0.
- Tests: enrich 294 passed, 1 skipped. Full "(unit or integration) and not slow": 1868 passed, 3 skipped, 1 xfailed.
- Coverage of calibrate.py: 100 % line and 100 % branch (180 statements).
- calibrate.py is now 280 lines, exactly at the budget.
