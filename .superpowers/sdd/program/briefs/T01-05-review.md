# Review for T01-05: Deletion filter and lake-file helpers

Commit under review: db1efe5 (base cd44be5)

## Spec conformance

### U01-33 herness.connectors.deletion.DeletionFilter
- Spec conformance: PASS. Constructor `DeletionFilter(source, entity)`, no store param (R-10) - deletion.py:35
- PASS. `reload()` loads `deleted_record_ids(source, entity)` into `pa.StringArray`, swaps under lock, returns size - deletion.py:41-46
- PASS. `apply()`: empty set returns `(batch, 0)` passthrough (same object identity, verified by `filtered is batch` test); else `batch.filter(pc.invert(pc.is_in(...)))` exactly as specified - deletion.py:48-62
- PASS. `size` property, 0 before first reload - deletion.py:64-68
- PASS. Precondition: `apply` before any `reload` raises `SchemaViolation("deletion set not loaded", source=..., entity=...)` - deletion.py:55-57; message verified against `SchemaViolation`/`HernessError.__init__` signature (positional message, `**context` kwargs) - matches "Errors: not loaded -> SchemaViolation(source, entity)"
- PASS. Postcondition `rows_in = rows_out + dropped` - covered by PT01-06 property test
- PASS. Concurrency: `reload` swaps reference under `threading.Lock`; `apply` reads the reference once (single lock-guarded read) then filters outside the lock - deletion.py:53-54
- PASS. Security (TH01-11): no ids logged anywhere in deletion.py; only counts/size ever surface
- PASS. Imports `deleted_record_ids` from `herness.store.ops.privacy` (owning submodule); verified this is the same object the module map's `herness.store.ops` re-exports, and its signature (`source: str, entity: str) -> list[str]`, sorted/unique, prefix-filtered) matches call-site usage exactly

### U01-34 herness.connectors.lakefiles.cleanup_orphan_temp_files
- PASS. Signature matches: `raw_root: Path, source: str, *, now: datetime, max_age: timedelta = timedelta(hours=1)` - lakefiles.py:147-149
- PASS. Algorithm order matches spec exactly: missing root -> 0; `os.walk(root, followlinks=False)`; per matching name: skip symlink/junction, skip if resolved path not under `root.resolve()`, then `lstat`, then age check, then `unlink` - lakefiles.py:104-171
- PASS. Regex uses `.fullmatch()` without literal anchors, equivalent to the spec's `^...$` pattern - lakefiles.py:101
- PASS. Symlink/junction skip: `path.is_symlink() or path.is_junction()` (Python 3.12 `Path.is_junction()`, correct for the pinned `>=3.12,<3.13` interpreter; the docstring correctly documents the always-False-on-POSIX / always-False-for-files-on-Windows behavior) - lakefiles.py:112-121
- PASS. Resolved-path containment: `_is_contained` resolves and checks `resolved == resolved_root or resolved_root in resolved.parents`, treating `OSError` as not-contained - lakefiles.py:104-109
- PASS. `PermissionError`/`FileNotFoundError` on unlink -> WARNING `connectors.lake.orphan_remove_failed`, continues - lakefiles.py:134-143
- PASS. INFO `connectors.lake.orphans_removed` logged only when count > 0, with a `count` field - lakefiles.py:169-170
- PASS. No ids/full paths in log fields beyond the relative filename (no secret/PII risk)
- PASS. Errors: raises nothing; OS errors logged per file, confirmed no propagation path

### U01-35 herness.connectors.lakefiles.SchemaTracker / SchemaDrift
- PASS. `SchemaDrift` frozen dataclass, `added`/`removed`/`changed` sorted tuples - lakefiles.py:174-180
- PASS. First `observe` returns `None`, baseline set - lakefiles.py:192-197
- PASS. Later calls: name-to-type dict comparison ignoring column order; equal returns `None`; else `SchemaDrift` computed via set difference/intersection, sorted, baseline updated unconditionally - lakefiles.py:198-205

## Test rows

- PASS UT01-25 (apply before/after reload, empty-set passthrough): all three behaviors present as `test_ut01_25_*` with IDs in the function name and as the first token of the docstring's first line; `pytestmark = pytest.mark.unit` at module level - test_deletion.py:229,248-283. A fourth, spec-adjacent test (`reload_only_matches_source_and_entity`) is a reasonable addition, correctly tagged.
- PASS UT01-26 (temp tree with old/new temp files, non-matching dotfile, symlink to outside): covered, plus extra determinism (a monkeypatch symlink test that never skips; live-symlink and junction tests skip gracefully when the host denies the needed privilege) - test_lakefiles.py:340,356-532
- PASS UT01-27 (added/removed/retyped columns; identical returns None): all covered - test_lakefiles.py:538-570
- PASS PT01-06 (output intersect deleted = empty set, rows_out + dropped = rows_in): implemented as a Hypothesis property test, correctly tagged (module `unit` pytestmark), monkeypatches the store call at the module-attribute level with a try/finally restore - test_deletion.py:292-317

All test IDs are present via underscore in the function name and as the first token of the first docstring line, satisfying ENG section 6 / global-constraints' `test_ut01_25_...` convention.

## Verification performed (beyond the report)

- Ran `PYTHONUTF8=1 uv run pytest -k "UT01_25 or UT01_26 or UT01_27 or PT01_06" -q -p no:logging`: 15 passed, 1 skipped (symlink creation denied on this host) - matches the report exactly.
- Ran coverage for `herness.connectors.deletion` / `herness.connectors.lakefiles` with `--cov-branch`: deletion.py 100%/100% (32/32 stmts, 4/4 branches); lakefiles.py 96% stmts (66/70) / 100% branch (20/20); missing lines 33-34, 55-56 are the two defensive `except OSError` races in `_is_contained` and `_remove_if_old` - both floors (>=90 line / >=85 branch) cleared with margin.
- Ran `lint-imports`: 13 contracts kept, 0 broken (herness layers, core-base, settings-are-leaves, etc.) - confirms no layering violation and that no pyproject.toml contract update was needed (herness.connectors is an existing layer member; no new package was created).
- Ran `uv run mypy` on both new modules: 0 errors (strict mode per project config).
- Ran `ruff check` on both new modules and both new test files: all checks passed.
- Read `herness/core/errors.py` to confirm `SchemaViolation`/`HernessError.__init__(message, /, *, hint=None, details=None, **context)` accepts `source=`/`entity=` as scalar context kwargs, matching the "SchemaViolation(source, entity)" error spec and the "deletion set not loaded" message assertion in the test.
- Read `herness/store/ops/privacy.py` to confirm `deleted_record_ids(source, entity) -> list[str]` (sorted, unique, prefix-filtered) matches the call site and the "reload_only_matches_source_and_entity" test's expectation.

## Budgets

- PASS `herness/connectors/deletion.py`: 61 lines (budget 90)
- PASS `herness/connectors/lakefiles.py`: 131 lines (budget 160)

## Items needing a flag

- Cannot fully verify from a single run on this host: the real-symlink (`test_ut01_26_skips_real_symlink_to_outside_target`) and junction (`test_ut01_26_skips_files_reached_through_a_junction`) tests both skip when the host denies the needed privilege (confirmed: the symlink test skipped in this run). The symlink-skip behavior is still covered deterministically by the monkeypatch test, but there is no privilege-independent test proving the junction-skip / resolved-path-containment path on a real junction in this environment. This is an environment limitation the report already discloses, not a code defect.

## Findings

No Critical or Important findings.

### Minor
- `tests/unit/connectors/test_deletion.py:302-304,316-317` (PT01-06): monkeypatches `deletion_module.deleted_record_ids` manually with a `try/finally` restore rather than using the `monkeypatch` fixture (which is used elsewhere in the sibling test file, e.g. `test_ut01_26_skips_symlinked_temp_files_deterministic`). Functionally equivalent and safe (the restore runs in `finally`), but inconsistent style with the rest of the suite.
- `herness/connectors/lakefiles.py:99-101`: the regex comment notes the ULID count is not itself validated against the strict ULID grammar. Worth a cross-check against T01-09's temp-file writer to confirm both sides agree on the exact naming pattern; out of scope for this card, not a defect here.

## Verdict

Approved
