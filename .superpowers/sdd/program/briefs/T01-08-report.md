# T01-08 Reconciliation — build report

Worktree: D:\herness\.claude\worktrees\agent-a35e2706adf8547c4 (branch worktree-agent-a35e2706adf8547c4, base 5a07621)
Status: DONE_WITH_CONCERNS (one small file outside the card's Files list, see Deviations)
Final commit: see "Final SHA" at the end.

## What was built
- `herness/connectors/reconcile.py` (new, 232/260):
  - `find_missing_keys` (U01-45): pathlib check for any committed file (`LAKE_FILE_PATTERN` from herness.store.lake, `[!.]*.parquet`) -> `(empty, 0)`; own `:memory:` DuckDB connection with `memory_limit`, `temp_directory`, `threads=4`; `deleted_ids` registered as an Arrow table; the spec's `latest`/`live` CTE verbatim with bound `$lake` / `$keys` parameters (each query gets only its own parameters — DuckDB rejects excess named params); count query + ANTI JOIN query fetched as Arrow (`to_arrow_table`, the non-deprecated API in duckdb 1.5); connection closed in `finally`; any `duckdb.Error` -> `SchemaViolation("reconcile query failed", entity=lake_dir.name) from exc`. Result always cast to `pa.string()`.
  - `reconcile_entity` (U01-44): guard (skipped for files) -> DeletionFilter reload -> scratch `data_root/tmp/reconcile` created -> key file `<source>-<entity>-<ulid>.parquet` written with `ParquetWriter(KEY_SCHEMA, compression="zstd")`, each batch checked/cast (`_key_batch`: sole column `_source_key`, string or large_string, no nulls, else `SchemaViolation("invalid key batch")`) -> `find_missing_keys(..., deleted=deletion.ids, temp_dir=scratch)` -> valve `live > 0 and missing*100 > max_delete_pct*live` (ERROR log `connectors.reconcile.aborted` with live_keys/missing_keys/max_delete_pct, counter `herness_connectors_reconcile_aborted_total{source,entity}`, `SchemaViolation("reconcile safety valve", source, entity)`) -> tombstones in `cfg.batch_rows` chunks via `tombstone_batch(deleted_at=deleted_at or clock(), fetched_at=clock())`, each through `deletion.apply`, into one `LakeWriter` from `runner.writer_factory`; commit; abort on any BaseException (a failing abort logged as `connectors.lake.abort_failed`, never masking the original, same rule as _write_loop) -> `finally` unlink key file -> INFO `connectors.reconcile.completed` (source, entity, live_keys, source_keys, tombstones) -> `SyncResult(mode="reconcile", rows=n, tombstones=n, skipped_deleted=<rows dropped by apply>, files, wm_text, wm_text)`.
- `herness/connectors/runner.py` (361 -> 381/390): `SyncRunner.run_reconcile` (U01-42): `ConfigError(f"{name} is not reconciled")` for `monitoring` or a connector that is not `SupportsKeyListing`; then `_prepare(entity)` (entity validation + one-time orphan cleanup) and `reconcile_entity(self, entity)` (lazy import, same pattern as backfill). Module docstring updated; `SupportsKeyListing` import (the import line had to be wrapped by ruff format, +5 lines). `# T01-10:` marker untouched.
- `tests/unit/connectors/test_reconcile.py` (new): 22 tests, UT01-40..44 and ST01-13, real Parquet lake files under the ops-store data root, FakeLake for tombstones, every test asserts the scratch folder is empty where the reconcile ran. No sleeps.
- `tests/unit/connectors/conftest.py`: `guard` fixture also patches `herness.connectors.reconcile.guard`.

## Deviations / interpretations
1. `herness/connectors/deletion.py` (61 -> 67/90, NOT in the card's Files list): added a read-only `DeletionFilter.ids` property. U01-44 step 5 says `deleted=deletion ids`, but DeletionFilter had no public accessor for its loaded set; the alternatives were reaching into `_ids` or a second `deleted_record_ids` query (two reads that could disagree). Empty array before `reload()`. Covered by UT01-42.
2. No LakeWriter is opened when nothing is missing (spec step 7 says write+commit; with zero missing keys that is an empty commit with no observable output). UT01-44 asserts no writer events.
3. `skipped_deleted` in the result = tombstone rows dropped by `deletion.apply` (normally 0 because the query already excludes the deletion set; non-zero only if the set is reported by the query but filtered at apply time). The spec's `SyncResult(rows=n, tombstones=n, ...)` leaves it open.
4. Key batches: `large_string` is accepted and cast to `string` ("each batch is cast to KEY_SCHEMA"); any other type, another column set, or a null key -> SchemaViolation.
5. No sync metrics (records_total etc.) are written for reconcile: U01-44 names only the aborted counter; `_finish` is not used. Consequently the T01-07 review note stands: the `wm is None` branch in `SyncRunner._finish` (runner.py ~356) is still not covered (as is `_default_writer`, line 123); reconcile does not make it reachable. Left as is.
6. `S608` on the SQL constants (constant concatenation of literal text) is suppressed with a reason comment; the paths are bound parameters.

## Spec notes
- §8.1 `connectors.reconcile.completed` / `aborted` fields emitted exactly as listed; §8.2 counter labels `source`, `entity`.
- §2 module-map rows unchanged (no budget overrun). No import-linter contract lists connectors submodules other than settings modules; none changed.

## Line counts
- herness/connectors/reconcile.py 232 / 260
- herness/connectors/runner.py 381 / 390 (9 left for T01-10)
- herness/connectors/deletion.py 67 / 90
- `uv run python -m tools.check_module_size` exit 0

## Tests / gates
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/connectors/test_reconcile.py -q -p no:logging` -> `ModuleNotFoundError: No module named 'herness.connectors.reconcile'` (collection error).
- GREEN: `PYTHONUTF8=1 uv run pytest -k "UT01_40 or UT01_41 or UT01_42 or UT01_43 or UT01_44 or ST01_13" tests/unit/connectors -q -p no:logging` -> 22 passed, 443 deselected.
- `PYTHONUTF8=1 uv run pytest tests/unit/connectors -q -p no:logging` -> 461 passed, 4 skipped.
- Coverage (connectors suite, branch): reconcile.py 100 % line / 100 % branch; deletion.py 100 %; runner.py 99 % (uncovered pre-existing: line 123 `_default_writer`, branch 356->359 `_finish` wm is None).
- ruff format / ruff check clean; mypy strict: no issues in 213 files; lint-imports 13 kept; check_module_size 0; pre-commit hooks pass except pytest-unit skipped (known-red on base, SKIP=pytest-unit).

## Final SHA
ce0d4ad feat(connectors): add key reconciliation with safety valve (T01-08) (on top of wip 766560c)

## Fix round 1
Commit: 507e524 fix(connectors): tighten reconcile tests and result construction (T01-08)
- m2: autouse fixture `_scratch_left_empty` in test_reconcile.py asserts after every test that `data/tmp/reconcile` is absent or empty (covers the five tests that lacked the check).
- m3: test_ut01_40_find_missing_keys_latest_row_rule adds `.part-4.parquet` (dot-prefixed, ends in .parquet, key `y`); the expected live=2 / missing=["d"] now fails if the `[!.]` exclusion breaks.
- m6: reconcile_entity builds SyncResult with keyword arguments. reconcile.py 232 -> 241/260.
- Gates: ruff format/check clean, mypy strict clean, check_module_size 0; card tests 22 passed; tests/unit/connectors 461 passed, 4 skipped. Committed with SKIP=pytest-unit (known-red base).
