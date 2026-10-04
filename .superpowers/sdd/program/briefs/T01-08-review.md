# T01-08 Reconciliation — review (verify agent)

Worktree agent-a35e2706adf8547c4, base 5a07621, head ce0d4ad (wip 766560c + ce0d4ad).

### Spec Compliance
- ✅ Spec compliant: I found no Missing, Extra or Misunderstood items against the brief. There are two small documented deviations; see ⚠️ and Minor.
  - U01-42 `SyncRunner.run_reconcile` ✅: `ConfigError(f"{name} is not reconciled")` for `monitoring` or a connector that is not `SupportsKeyListing`. Then `_prepare(entity)` (entity validation and one-time cleanup), then `reconcile_entity`. The watermark is not touched (runner.py:216-227). The `# T01-10:` marker is intact (runner.py:187).
  - U01-44 `reconcile_entity` ✅. Steps 1-9 follow the spec order (reconcile.py:185-232):
    - guard is skipped for files.
    - `DeletionFilter.reload()` runs first.
    - scratch is `data_root/tmp/reconcile`, and the key file is `<source>-<entity>-<ulid>.parquet`.
    - keys are written with `ParquetWriter(KEY_SCHEMA, compression="zstd")`. A batch with another column, another type or a null key raises `SchemaViolation`.
    - the valve condition is exactly `live > 0 and missing*100 > max_delete_pct*live` (reconcile.py:168-169).
    - the ERROR `connectors.reconcile.aborted` has the fields `live_keys`/`missing_keys`/`max_delete_pct` (plus `source`/`entity`).
    - `herness_connectors_reconcile_aborted_total{source,entity}` is emitted, then `SchemaViolation("reconcile safety valve", source=, entity=)` is raised.
    - tombstones go out in `batch_rows` chunks, each through `deletion.apply`, into one writer; `commit`, and `abort` on any error.
    - the key file is unlinked in `finally` with `missing_ok=True`.
    - INFO `connectors.reconcile.completed` has the §8.1 fields `source, entity, live_keys, source_keys, tombstones`.
    - it returns mode `reconcile` with `rows=tombstones=n` and `watermark_before == watermark_after ==` the current watermark text.
  - U01-45 `find_missing_keys` ✅:
    - no committed file gives `(empty, 0)`.
    - `:memory:` DuckDB runs with `memory_limit`, `temp_directory` and `threads=4`.
    - `deleted_ids` is registered from Arrow.
    - the CTE is the spec's text verbatim, with `$lake`/`$keys` bound (no string interpolation of values; the SQL constants are literal-only, so the `# noqa: S608` is justified).
    - the result is sorted and fetched as Arrow, and the connection is closed in `finally`.
    - `duckdb.Error` becomes `SchemaViolation("reconcile query failed", entity=lake_dir.name) from exc`.
  - TH01-13 ✅: an empty listing gives `missing == live > 0`, which trips the valve (spec range 0 < pct ≤ 100). Nothing is written and the scratch file is removed.
  - Test IDs:
    - UT01-40 ✅ (10 functions)
    - UT01-41 ✅ (trip plus exact 2 % boundary pass)
    - UT01-42 ✅ (2)
    - UT01-43 ✅ (2, monitoring plus no `list_keys`)
    - UT01-44 ✅ (already tombstoned plus empty lake)
    - ST01-13 ✅
    - Every function name carries its ID, every docstring starts with it, `pytestmark = [unit, usefixtures("guard")]` is set, and there are no sleeps.
- ⚠️ Cannot verify from the diff or unit tests:
  - IT01-05 (integration, real lake to staging). It is in the U01-44/45 Tests rows but not in this card's acceptance, so it belongs to a later card.
  - DuckDB honouring `[!.]` in the recursive glob. The unit test's dot-file is `.part-3.parquet.tmp-x`, which does not end in `.parquet`, so `*.parquet` would exclude it anyway. I relied on OI-06/ST02-02 (herness/store/lake.py:30) for this.
  - The DuckDB spill-file cleanup in `scratch` under real memory pressure. BT01-03 is not in this card.

### Gate evidence (re-run by the reviewer)
- The acceptance selection `-k "UT01_40 or UT01_41 or UT01_42 or UT01_43 or UT01_44 or ST01_13"` passed: 22 passed, 443 deselected.
- `tests/unit/connectors` passed: 461 passed, 4 skipped. The skips are pre-existing: symlink privilege (2) and the DuckDB excel extension (2).
- Coverage with branch, over the connectors suite:

  | Module | Coverage | Uncovered |
  |---|---|---|
  | reconcile.py | 100 % line / 100 % branch | — |
  | deletion.py | 100 % | — |
  | runner.py | 99 % | line 123 `_default_writer` and branch 356->359 `_finish` `wm is None`, both pre-existing from T01-07 |

- `ruff check`, `ruff format --check` (528 files), `mypy` strict (213 files, 0 issues) and `lint-imports` (13 kept) are all clean.
- `tools.check_module_size` exits 0:

  | File | Lines / budget |
  |---|---|
  | reconcile.py | 232/260 |
  | runner.py | 381/390 (9 lines left for T01-10) |
  | deletion.py | 67/90 |

### Strengths
- The valve runs strictly before any writer is opened. `lake.events == []` is asserted on every failure path, and the exact 2 %-boundary case is tested.
- SQL safety is sound:
  - values are bound parameters or come from an Arrow-registered table;
  - the query text is constant;
  - the DuckDB connection is closed in an inner `finally`;
  - a `duckdb.connect` failure is also mapped to `SchemaViolation`.
- The scratch key file is removed in `finally` on every path. These paths are tested: listing error, bad batch, query error, valve trip, commit failure with abort OK, commit failure with abort failing, and success.
- The abort-failure handling reuses the established `_write_loop` pattern (log `connectors.lake.abort_failed`, never mask the original).
- The tests use real Parquet lake files over hive partitions (latest-row rule, deleted rows, deletion set), not mocks of the query.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/connectors/deletion.py:57-61 — new `DeletionFilter.ids` property. The file is outside the card's Files list.
   - It is read-only, locked and returns an immutable Arrow array.
   - It is the cleanest way to meet "deleted=deletion ids" with a single consistent read. It is covered by UT01-42.
   - Accept, but record it as a card deviation for the integrator.
2. tests/unit/connectors/test_reconcile.py — the report says every test asserts the scratch folder is empty; several reconcile-running tests do not:
   - `test_ut01_40_files_skips_guard`
   - `test_ut01_40_large_string_keys_accepted`
   - `test_ut01_41_valve_boundary_passes`
   - `test_ut01_42_no_tombstone_under_deletion`
   - `test_ut01_42_deletion_filter_on_tombstones`

   The card acceptance says "scratch folder empty after each test". The code path is the same `finally`, so the risk is low, but a one-line assert (or an autouse fixture checking `data/tmp/reconcile` after each test) would make the acceptance literal.
3. tests/unit/connectors/test_reconcile.py (`test_ut01_40_find_missing_keys_latest_row_rule`, `.part-3.parquet.tmp-x`) — the dot-file does not end in `.parquet`, so it does not exercise the `[!.]` exclusion that its docstring claims. A file such as `.hidden.parquet` would test it.
4. herness/connectors/reconcile.py:141-143 — no `LakeWriter` is opened when nothing is missing. Spec step 7 reads as write-then-commit unconditionally. With zero rows there is no observable difference (`files=()`), and UT01-44 pins the behaviour. Acceptable interpretation.
5. herness/connectors/reconcile.py:124-130 — `_abort` duplicates the abort-and-log helper in herness/connectors/_write_loop.py:181.
   - The spec §6 rule allows `except Exception` only in the job handlers. Both sites carry the same documented `BLE001` exception, so this is consistent with the precedent.
   - Consider exporting one helper when runner.py budget pressure eases.
6. herness/connectors/reconcile.py:228-232 — `SyncResult` is built positionally with 9 arguments, and `skipped_deleted` is the count dropped by `apply` (normally 0 because the query already excludes the set). A keyword construction would be more robust against field reordering. The `skipped_deleted` meaning is a reasonable reading of the spec's "...".
7. herness/connectors/reconcile.py:78 — a `lake_dir` whose absolute path contains glob metacharacters (`[`, `*`, `?`) would be interpreted by DuckDB's glob. The path is still bound, so this is not an injection. The spec mandates the form, and `data_root` is operator-controlled. Note only.
8. The `_finish` `wm is None` branch (runner.py:356) and `_default_writer` (runner.py:123) stay uncovered. This is pre-existing and not reachable from reconcile, which does not use `_finish` or write sync metrics; that matches U01-44, which names only the aborted counter.

### Assessment
**Task quality:** Approved
**Reasoning:** All three units and all six test IDs meet the spec:
- the valve thresholds, error type and message are exact;
- the watermark does not change;
- the SQL uses bound parameters;
- scratch cleanup is in `finally`;
- the §8.1 events and fields are exact;
- all gates pass, with 100 % coverage on reconcile.py.

The remaining items are minor: test hardening, and one small, justified edit outside the Files list.


## Re-review 1 (fix round 1, commit 507e524; scope m2, m3, m6)

- **m2 ✅ resolved.** tests/unit/connectors/test_reconcile.py:85-90 adds the autouse fixture `_scratch_left_empty`.
  - After every test in the module it asserts that `data/tmp/reconcile` is either absent or empty. This covers the five tests that lacked the check.
  - A failure surfaces as a teardown error rather than a test failure, which is fine.
- **m3 ✅ resolved.** tests/unit/connectors/test_reconcile.py:157 adds `.part-4.parquet` (key `y`, later timestamp, not deleted, not in the source keys).
  - I checked that the test really detects a broken exclusion with a throwaway script in the scratchpad, using a temp directory; the worktree was not touched. On duckdb 1.5.5, `**/[!.]*.parquet` matched only `part-1.parquet`, while `**/*.parquet` also read `.part-4.parquet`. So DuckDB's `*` does not skip dot-files by itself.
  - Without the `[!.]` exclusion, `y` would therefore be live and missing: `live == 3` and `missing == ["d", "y"]`. The assertions `live == 2` and `missing == ["d"]` would fail, so the test now really guards the exclusion.
- **m6 ✅ resolved.** herness/connectors/reconcile.py:230-241 builds `SyncResult` with keyword arguments and the same values. The file is now 241/260 lines.
- **Gates re-run:**
  - The card selection `-k "UT01_40 or UT01_41 or UT01_42 or UT01_43 or UT01_44 or ST01_13"` passed: 22 passed, 443 deselected.
  - ruff and mypy are clean on both changed files.
- **New issues:** none.

**Task quality (re-review 1):** Approved
