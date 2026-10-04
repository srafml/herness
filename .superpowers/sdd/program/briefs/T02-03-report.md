# Report for T02-03: Lake purge and retention

## Summary

Implemented `herness/store/lake_purge.py` (U02-20...U02-23): `LakePurgeResult`,
`purge_record_ids`, `LakeRetentionResult`, `purge_partitions_before`, plus the
constant `PURGE_BATCH_MAX = 10_000`. Tests added in
`tests/unit/store/test_store_lake_purge.py` (20 tests, all `pytest.mark.unit`).

The brief's own "Unit specs (verbatim)" section omitted U02-21 (`purge_record_ids`)
and U02-22 (`LakeRetentionResult`) - I read them directly from
`docs/impl/02-data-model.impl.md` lines 568-609 since the module map and test rows
(UT02-14, UT02-15, FT02-01) require U02-21's exact algorithm and error mapping.

## Files changed

- `herness/store/lake_purge.py` (new, 198 lines; budget 200 from the section 2 module map row)
- `tests/unit/store/test_store_lake_purge.py` (new, 20 tests)

`herness/store/lake.py` was not touched (stayed at 378/380 lines per the dispatch note).

## Design notes

- Reused `herness.store.lake`'s private `_BUSY_ERRNOS`, `_BUSY_WINERRORS`,
  `_PARQUET_OPTIONS`, `_fsync` and public `validate_name` rather than duplicating
  them, since lake.py is at its line budget and these are the "U02-15 Parquet
  settings" and busy-error classification the brief's algorithm cites by name.
- `purge_record_ids`: groups ids by (source, entity) after format/name validation
  (`ConfigError` on a malformed id, an invalid source/entity name, or a count
  outside `[1, PURGE_BATCH_MAX]`); scans only `_record_id` per file first
  (`pc.is_in`), reads the full file only on a match, then unlinks (all rows
  removed) or rewrites via temp-then-`os.replace` (some rows remain). OSError on
  replace/unlink maps to `StoreBusy` (EACCES/EBUSY/winerror 32-33) or
  `SchemaViolation` (anything else); on a busy replace the temp is deleted before
  re-raising so no temp is left and the original file is never touched.
- `purge_partitions_before`: validates `cutoff <= today - 30 days` (`ConfigError`
  otherwise), walks `root/<source>/<entity>/dt=*`, logs
  `store.lake.partition_unparsable` (WARNING) for names that don't match
  `dt=YYYY-MM-DD` or don't form a valid calendar date, does a containment check
  before each `shutil.rmtree` (defensive per TH02-01; unreachable through normal
  traversal, covered by a monkeypatched test), defers on `PermissionError`, and
  logs `store.lake.retention_applied` (INFO) with all four counts.

## RED evidence

    $ mv herness/store/lake_purge.py herness/store/lake_purge.py.bak
    $ PYTHONUTF8=1 uv run pytest tests/unit/store/test_store_lake_purge.py -q -p no:logging
    ERROR tests/unit/store/test_store_lake_purge.py
    ModuleNotFoundError: No module named 'herness.store.lake_purge'
    1 error in 1.13s
    $ mv herness/store/lake_purge.py.bak herness/store/lake_purge.py

## GREEN evidence

    $ PYTHONUTF8=1 uv run pytest tests/unit/store/test_store_lake_purge.py -v -p no:logging
    ... 20 passed in 1.53s

    $ PYTHONUTF8=1 uv run pytest -k "UT02_14 or UT02_15 or UT02_16 or FT02_01" -q -p no:logging
    20 passed, 225 deselected in 0.78s

## Coverage

    $ PYTHONUTF8=1 uv run pytest tests/unit/store/test_store_lake_purge.py -q -p no:logging \
        --cov=herness.store.lake_purge --cov-report=term-missing --cov-branch
    Name                          Stmts   Miss Branch BrPart  Cover   Missing
    herness\store\lake_purge.py     146      0     36      0   100%

100% line and branch coverage of the new module (budget: >=90% line, >=85% branch).

## Gate outputs

- `uv run ruff format --check .` - 45 files already formatted, clean.
- `uv run ruff check .` - All checks passed.
- `uv run mypy` - Success: no issues found in 20 source files.
- `uv run lint-imports` - 8 kept, 0 broken (no contract changes needed; `lake_purge.py`
  lives inside the existing `herness.store` package/layer).
- `uv run python -m tools.check_type_ownership` - exit 0 (no new `herness.core.types`
  submodules added).
- `PYTHONUTF8=1 uv run pytest -m "(unit or integration) and not slow" -q -p no:logging`
  - 241 passed, 4 deselected (pre-existing bench/fault-marked tests).

## Line counts vs budgets

- `herness/store/lake_purge.py`: 198 / 200 (module map row budget).
- `herness/store/lake.py`: unchanged at 378 / 380.

## Deviations from the brief

- The brief's own unit-spec excerpt omitted U02-21 and U02-22; I sourced their exact
  text from the spec file itself (cited above) rather than inventing behaviour -
  no invented symbols, just filling a copy gap in the brief.
- FT02-01 ("`os.replace` patched to raise `PermissionError`") is implemented as a
  deterministic `monkeypatch` test under `pytest.mark.unit` (named
  `test_ft02_01_replace_failure_leaves_original_and_no_temp`, ID-tagged and
  docstring-tagged for `-k FT02_01` selection) rather than under `pytest.mark.fault`
  with the `fault_plan`/`HERNESS_FAULTS` mechanism, because that mechanism belongs to
  impl 08 (`herness.core.resilience`), which does not exist yet on this branch. This
  mirrors the existing precedent in `tests/unit/store/test_store_lake.py`
  (`test_ut02_11_commit_retry_is_idempotent`), which tests the equivalent
  `StoreBusy`-on-`os.replace` scenario for `LakeWriter.commit()` the same way, under
  `unit`, not `fault`.
- Added several supplementary tests beyond the four spec-listed IDs (no-match file,
  cross-group purge, missing directory, count-range and id-format validation,
  non-busy OSError mapping to `SchemaViolation`, unparsable/invalid-calendar
  partition names, locked-partition deferral, missing root, and the containment
  branch) to reach the >=90%/>=85% coverage gate; these carry descriptive names/docstrings
  rather than spec test IDs since the spec table only names UT02-14, UT02-15,
  UT02-16 and FT02-01 for this module.

## Concerns

- None blocking. The only soft item is the FT02-01 marker choice explained above;
  happy to move it to `pytest.mark.fault` with a real `fault_plan` once impl 08 lands,
  if the controller prefers that over the lake.py precedent.
