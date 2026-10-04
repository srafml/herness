# T03-08 report: Decision cache core

Status: DONE
Commit: f0b192d feat(enrich): decision cache core (T03-08)
Worktree: D:\herness\.claude\worktrees\agent-ad8bea210b9833b65

## What was built
- `herness/enrich/cache.py` (240 lines; budget 360, hard limit 400)
  - `CACHE_SCHEMA` (U03-36), verbatim from the brief. Partition columns `decider` and `decider_version` come from the Hive path.
  - `DecisionCache(paths, qsv)` (U03-37): `dataset()`, `register(con, view)`, `existing_keys(decider, decider_version, questions)`, `writer(decider, decider_version, *, questions, flush_rows)`.
    - `dataset()` lists only `decider=*/decider_version=*/part-*.parquet` under `cache_dir(qsv)` and builds `ds.dataset(files, partitioning=HivePartitioning(...), partition_base_dir=..., ignore_prefixes=[".", "_"], schema=CACHE_SCHEMA + partition fields)`. Returns None when there is no part. It then checks every fragment's physical schema against `CACHE_SCHEMA` (ignoring metadata) and raises `SchemaViolation("cache part schema mismatch: <file name>")` (TH03-18).
    - `register`: `con.register(view, dataset)`, or an empty table with the same columns when there are no parts.
    - `existing_keys`: filters on the partition plus `question_fingerprint` in the set's fingerprints and returns `(content_hash, question)` pairs.
  - `CacheWriter` (U03-38): `add` / `flush` / `close` / context manager (flushes on normal exit only).
    - The in-memory key set dedupes on (content_hash, question, question_fingerprint). Error outputs and unknown qids are skipped. A decider or version mismatch raises `SchemaViolation`. `add` auto-flushes at `flush_rows`.
    - `flush` writes `.part-<ulid>.parquet.tmp` with zstd, fsyncs it, `os.replace`s it to `part-<ulid>.parquet`, then calls `_fault_point("enrich.after_batch_write")`, then logs `enrich.cache.flushed` (DEBUG, decider, rows), then clears the buffer.
    - On an OSError: EACCES or EBUSY raises `StoreBusy`, anything else raises `FatalError`. The tmp file is removed and the buffer is kept.
  - T08-08 ruling applied: module-private `_fault_point(name)` no-op with a `T08-08:` comment. `herness.core.resilience` was not invented.
- `tests/unit/enrich/test_cache.py` (285 lines; 13 tests; UT03-33 x4, UT03-34 x2, UT03-35 x5 functions, one parametrized x3)

## Deviations / choices (with reasons)
1. **URL-decoding of `decider_version`.** The spec's wording is to precompute unquoted values in Python and join them in `register`. The code uses pyarrow's `HivePartitioning(segment_encoding="uri")` instead, which URL-decodes partition values natively. The result is the same (the view and the dataset expose decoded versions), with less code, and `dataset()` and `existing_keys` get decoded values too. The decider_version regex forbids `%`, so decoding is unambiguous. A test covers a version with `/` and `:` (`openjev-0.4.0/qwen:7b`).
2. **Explicit file list.** `dataset()` passes an explicit file list with `partition_base_dir` rather than the bare directory. `<qsv>/questions.json` and `_migrated_from_*.json` live in `cache_dir` and would otherwise be picked up as parquet. `ignore_prefixes` is still passed.
3. **Empty fingerprints.** When a Question has an empty `fingerprint` (a QuestionSet not built by `load_question_set`), the code falls back to `herness.enrich.questions.question_fingerprint(q)`.
4. **`flush_rows` validation.** `flush_rows < 1` raises `SchemaViolation`. This is a small guard the spec does not mention.
5. **Where the schema check runs.** It runs in `dataset()`, so `register`, `existing_keys` and any caller all go through it. UT03-33 exercises it through `register`.

## Tests / gates
- RED: `pytest tests/unit/enrich/test_cache.py` failed with `ImportError: cannot import name 'cache' from 'herness.enrich'` before the implementation existed.
- GREEN: `pytest tests/unit/enrich/test_cache.py -p no:logging --cov=herness.enrich.cache --cov-branch` gave 13 passed, with cache.py at 100% line and 100% branch (126 statements, 28 branches).
- Full `-m "(unit or integration) and not slow"`: 1832 passed, 3 skipped, 1 xfailed (pre-existing).
- ruff format (176 files unchanged), ruff check clean, mypy clean (80 files), lint-imports 11 kept / 0 broken, check_module_size exit 0, check_type_ownership exit 0.
- Note: `uv run` failed because `D:\herness\pyproject.toml` has merge-conflict markers (line 199). All gates were run through the worktree's `.venv\Scripts\python.exe -m ...` and `.venv\Scripts\lint-imports.exe`.

## Carry-overs
- FT03-03 (fault test on `enrich.after_batch_write`) belongs to the fault suite and is not covered here. UT03-35 asserts the fault point is called after the rename and is not called when the write fails.
- T08-08: replace `cache._fault_point` with `herness.core.resilience.fault_point` when it lands.
- The readers' `QUALIFY` dedupe (impl 03 §4.2) is for consumers of the registered view (later cards).
- No pyproject change was needed: `herness.enrich` is already in the contracts and mypy files.

## Fix round 1
Commit: 48f28a3 fix(enrich): address T03-08 review round 1 (T03-08)

- **Important 1.** In `flush`, the `except OSError` handler now wraps `tmp.unlink(missing_ok=True)` in `contextlib.suppress(OSError)`. A Windows lock that also blocks the unlink still maps to `StoreBusy` (EACCES/EBUSY) or `FatalError`. New test `test_ut03_35_os_error_mapping_when_unlink_fails_too` is parametrized over EACCES, EBUSY and ENOSPC, with both `os.replace` and `Path.unlink` failing.
- **Minor 2.** I chose the smaller change: `add()` validates `samples` up front and raises `ConfigError("samples out of range for the cache samples column")` unless it is None or 1..32767. The message carries no data values; the value goes only into error context. Once `samples` is validated, every other value going into the Arrow table has already been checked by the pydantic models, so no raw ArrowInvalid can escape. I used ConfigError, not SchemaViolation, because `samples` is a caller argument taken from decider settings, the same reasoning as Minor 3. New tests cover 0, -1 and 32768 (rejected, buffer left empty) and 32767 (accepted and round-tripped).
- **Minor 3.** `flush_rows < 1` now raises `ConfigError`, and the test is adjusted.
- **Minor 4.** Dropped the dead `ignore_prefixes` argument, since it has no effect with an explicit file list. The docstring now says the `part-*.parquet` glob in `_part_files` is what keeps `.tmp`, dot- or underscore-prefixed files and `questions.json` out.
- **Parked item (per the review).** `dataset()` still re-scans on every call; unchanged.

Gates: ruff format and check clean, mypy clean, lint-imports 11 kept / 0 broken, check_module_size exit 0.
Tests: 20 card tests pass, and cache.py is at 100% line and 100% branch coverage (132 statements, 30 branches). The full `(unit or integration) and not slow` run gives 1839 passed, 3 skipped and 1 xfailed; the xfail was already there.
Sizes: cache.py is 247 lines (budget 360); test_cache.py is 329 lines.
`uv run` still fails because of the merge conflict in `D:\herness\pyproject.toml`, so the gates were run through the worktree `.venv`.
