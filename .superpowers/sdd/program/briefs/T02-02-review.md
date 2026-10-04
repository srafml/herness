# T02-02 Lake writer: review (verify agent)

**Verdict: Approved.** No Critical or Important findings. The Minor items below can be fixed now or later.

### Spec Compliance
- ✅ Spec compliant (checked against impl 02 §3.2 U02-08…U02-19 and the §11 rows)

| Req / test | Status | Note |
|---|---|---|
| Module constants (§3.2 preamble) | ✅ | BUFFER_ROWS, BUFFER_BYTES, ZSTD_LEVEL, PARQUET_VERSION, LAKE_FILE_PATTERN, NAME_RE, COLUMN_RE all match verbatim |
| U02-08 META_COLUMNS | ✅ | names, types and nullability exact; order enforced by `_validate_batch` |
| U02-09 validate_name | ✅ | message `invalid lake <kind> name (length <n>)`; the value is not echoed |
| U02-10 partition_dir | ✅ | validates, joins, checks containment with `resolve()` (a symlink escape is also caught) |
| U02-11 lake_glob | ✅ | `<root posix>/<src>/<ent>/**/[!.]*.parquet` |
| U02-12 LakeFileSet | ✅ | frozen and slotted; files sorted by POSIX string; the invariant holds (commit always flushes) |
| U02-13 invariants (a)–(e) | ✅ | (d) is enforced more strictly than the literal step 6: rotation also fires when the buffer is empty but a file is still open (lake.py:282). This is needed, because otherwise a new dt would be written into the old partition's open file. It is correct and tested (UT02-08 dt_change test) |
| U02-14 __init__ | ✅ | ranges [1 MiB, 1 GiB] and [1, 86400]; root=None uses data_layout().raw, resolved; no I/O |
| U02-15 write | ✅ | steps 1–8, temp name `.part-<ulidA>.parquet.tmp-<ulidB>`, Parquet options, row_group_size, size/age/dt_change/schema_change rotation, DEBUG `store.lake.file_rotated` with the spec fields, OSError mapping (EACCES/EBUSY/winerror 32,33 -> StoreBusy, else SchemaViolation with the errno name) |
| U02-16 commit | ✅ | flush and close("commit"); fsync then os.replace in creation order; skips on idempotent retry; directory fsync only on POSIX; state stays open on error; INFO `store.lake.committed` fields |
| U02-17 abort | ✅ | no-op after commit; suppresses only OSError/ArrowException on close; counts PermissionError; WARNING abort_leftover; clears the buffer |
| U02-18 context manager | ✅ | abort on exception; warning plus abort on a normal exit while open; returns False |
| U02-19 _validate_batch | ✅ | rules 1–8 in order, vectorised, binary_join_element_wise for rule 5; casts; reorder. See Minor M1 (the time cast runs before rules 4–8) |
| UT02-01 | ✅ | test_ut02_01_* (x2) |
| UT02-02 | ✅ | missing `_deleted` |
| UT02-03 | ✅ | bad_rows == 2; value not echoed |
| UT02-04 | ✅ | source_mismatch (plus entity_mismatch) |
| UT02-05 | ✅ | payload_null, naive timestamp -> type, plus the extra type/null/column_name/duplicate/overflow cases |
| UT02-06 | ✅ | 1 MiB target, >= 4 files, >= 1 MiB except the last (1.5 s on this machine) |
| UT02-07 | ✅ | fake clock, reasons [age, commit] |
| UT02-08 | ✅ | two partitions with counts 2/3 |
| UT02-09 | ✅ | two files, one schema each |
| UT02-10 | ✅ | abort, abort again, then write/commit -> LakeStateError; leftover logging |
| UT02-11 | ✅ | empty LakeFileSet, root not created; also tests commit retry and OSError mapping |
| UT02-12 | ✅ | temps deleted, exception propagates; normal-exit warning |
| UT02-13 | ✅ | four ConfigErrors plus ok_name; limits; default root |
| PT02-01 | ✅ | hypothesis over sizes, dt offsets, schema toggle, BUFFER_ROWS, max_open_s/clock; checks the id multiset, one dt per file, metadata first, no leftover files. target_bytes is not varied (its 1 MiB minimum makes that impractical) |
| ST02-01 | ✅ | the three spec attacks give ConfigError; tmp_path stays empty |
| ST02-02 | ✅ | DuckDB `glob()` before commit (read_parquet raises on zero files, so glob() is the right probe), plus a `.hidden.parquet` decoy; read_parquet count after commit. This settles OI-06: `[!.]` works |
| Module <= 380 lines | ✅ | exactly 380 (no headroom) |
| TH02-01 / TH02-02 mitigations | ✅ | allowlist, containment, dot-temp names, `[!.]` glob, rename on commit |

- ⚠️ Cannot verify from the diff:
  - Acceptance check "spec 11 generator smoke (`T11-14 tools/synth_data.py --scale tiny`)": the tool does not exist yet (impl 11). Carry it forward to T11-14.
  - OI-06 is still marked "Still open" in docs/impl/02-data-model.impl.md:4063 (and DD02-03 at :4043). ST02-02 now answers it (`[!.]` is honoured). The controller should record the resolution, since the doc is outside this card's files.
  - FT02-06 (kill between renames) belongs to a later fault-test card and is not in this card's test list.
  - Gate results (ruff, mypy, lint-imports, 213 passed, coverage 98 %) come from the report. I re-ran only UT02-06 and the overflow test (2 passed).

### Strengths
- Rotation is correct where the spec's literal step 6 has a gap (flushed-but-open file with a new dt or schema).
- Commit is idempotent on retry, and a test covers it with a fault injected on the second os.replace.
- Error messages carry no values. The OSError mapping uses only the errno name.
- Tests are strong and assert real behaviour: on-disk layout, Parquet schema and nullability, log events and levels, DuckDB visibility. The property test covers rotation interactions.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
- **M1: a chained exception leaks the value; the rule order drifts.** herness/store/lake.py:116-119. On a time-cast overflow, `_fail` raises inside `except pa.ArrowInvalid` without `from None`. The implicit `__context__` holds `ArrowInvalid('... out of bounds timestamp: 4611686018427387904')`, which I checked in the worktree. Any `exc_info` log would print the raw value, against U02-19 "Values never appear in errors". The error also reports `(0 rows)`. And because the cast runs inside `_meta_arrays` (before rules 4–8), overflow wins over, for example, `source_mismatch`. Overflow is not one of the spec rules, so this is only drift. Fix: `raise LakeContractError(...) from None` (or have `_fail` accept a cause), and optionally count the overflowing rows.
- **M2: abort can raise.** herness/store/lake.py:372-376. Only `PermissionError` is caught. Any other OSError from `unlink` (EIO, EROFS) propagates, although U02-17 says "Errors: None raised". Inside `__exit__` (lake.py:234) that would replace the user's original exception. Consider catching OSError, counting it and logging.
- **M3: a temp file can escape tracking.** herness/store/lake.py:298-299. `ParquetWriter(temp, ...)` runs before `self._temp_files.append(...)`. If the constructor fails after it has created the file, abort() never deletes that temp. The runner's 1 h sweep does remove it. Recording the pair before opening the writer (unlink uses missing_ok) closes the gap.
- **M4: magic column indices.** herness/store/lake.py:269, 274. `norm.column(5)` and `norm.column(4)` rely on the META_COLUMNS order. `norm.column("_fetched_at")` and `norm.column("_source_updated_at")` read better and cannot drift.
- **M5: abort after a partial commit leaves final files visible.** herness/store/lake.py:362-380. After a commit that raised StoreBusy partway, abort() deletes only the remaining temps. Files already renamed stay visible although the writer ends `aborted`. This matches the spec (U02-16 security note: staging dedupe collapses a rerun). Callers should know that "abort" does not mean "nothing visible" once a commit has been attempted. A docstring note would help.
- **M6: unused fixture.** tests/unit/store/test_store_lake.py:108. `test_ut02_01_metadata_reordered_and_normalised` takes the `writer` fixture but never uses it.
- **M7: pyproject.toml is outside the card's file list.** pyproject.toml mypy override (`ignore_missing_imports` for pyarrow). This is a needed and justified extra outside the card's Files list. It makes all pyarrow types `Any` under `--strict`, so pyarrow misuse is not type-checked. Consider locking `pyarrow-stubs` later.
- **M8: no line headroom.** herness/store/lake.py is at exactly its 380-line budget, so any review fix must be offset elsewhere. M4 shortens nothing, but M1 can reuse `_fail`.

### Assessment
**Task quality:** Approved
**Reasoning:** Every unit and test row on the card is implemented as specified. The one deviation from the literal algorithm (rotating on a new dt or schema while a file is open) is needed to keep invariant (d). The remaining items are small hardening and polish (the value leak through the chained exception, abort robustness, untracked-temp edge) and none blocks trust in the writer.


---

## Re-review round 1 (fix commit d36af6a)

Scope: M1, M2, M3, the extra per-group counting fix, and the line budget. I read the fix diff (T02-02-fix1.diff) and the "Fix round 1" notes, then checked two concrete risks with a scratchpad script run in the worktree. The script is not committed and the tree is unchanged.

### Per finding
| Finding | Status | Evidence |
|---|---|---|
| M1: the chained exception leaks the value; rule order | ✅ | `_cast_time` (lake.py:113) counts out-of-range s/ms values with int64 bounds and calls `_fail` outside any `except`, so there is no `__context__` and `bad_rows` is correct. The cast now runs in `_validate_batch` after rules 4–8. `_TIME_COLUMNS` is a tuple, so the order is deterministic. Tests assert `__cause__`/`__context__` is None, bad_rows == 2, and that source_mismatch wins. The negative bound `-limit` is one value stricter than the true minimum, which does not matter. |
| M2: abort can raise | ✅ | lake.py:372 catches `OSError`, counts it and logs it. The test is parametrised over PermissionError and EIO. |
| M3: a temp file can escape tracking | ✅ for abort, but it introduces N1 | The entry is recorded before the constructor (lake.py:295). On failure the file is unlinked and the entry popped; if the unlink fails, the entry is kept. abort() now deletes it (tested). Keeping the entry also exposes it to commit(); see N1. |
| Extra: rows/max counted per buffered group | ✅ | lake.py:281-285. `rows` now counts exactly the rows buffered or written, so a failed flush followed by a retried commit reports the right count. Partial batches (group 1 buffered, then a later group's flush fails) stay consistent between `rows` and the files. |
| Line budget 380 | ✅ | lake.py is 377 lines at d36af6a. |

### New findings
#### Important
- **N1: commit publishes a corrupt temp after a double fault.** herness/store/lake.py:295-302 together with the commit loop in `commit()`. When the ParquetWriter constructor fails and the cleanup unlink also fails, the `(temp, final)` entry is kept on purpose so that abort can retry. But the writer stays `open`, and a caller that retries the write and then calls `commit()` sends that entry through the rename loop. The temp exists and the final does not, so the junk temp is fsynced and renamed to `part-<ulid>.parquet`. I reproduced this in the worktree: the first write raises SchemaViolation, the retried write and commit succeed, and the result is `files 2 rows 2`, where one `part-*.parquet` fails `pq.read_metadata` with ArrowInvalid. That file then matches `lake_glob`, and the staging `read_parquet` over the entity fails for every later build. Before the fix, the same double fault left an invisible dot-temp (swept after 1 h); now it can produce a visible corrupt lake file. Fix: keep failed entries apart from the files to publish. For example, move the kept entry to a `_discard: list[Path]` that abort() and commit() unlink (best effort, counted in abort_leftover), or store it with `final=None` and skip it in commit. Add a test: constructor failure plus locked unlink, then write, then commit, assert every committed file is readable and the leftover temp is not renamed.

#### Minor
- **N2: a time value past year 9999 escapes as OverflowError.** herness/store/lake.py:283. A `_source_updated_at` value within the int64 microsecond range but past year 9999 passes `_cast_time`. `pc.max(...).as_py()` then raises a bare `OverflowError` ("date value out of range", reproduced). That is not a LakeContractError and not mapped. It is raised after the group has been appended to the buffer and counted in `rows`, so a caller that ignores it and commits writes that row anyway. This existed before the fix; the move into `_append` only changed where the state is left. Suggested fix: in `_cast_time`, also reject values outside the Python `datetime` range as rule `type` with a count (bounds known in microseconds). Then `max_source_updated_at`, typed `datetime`, can always represent the value.

### Re-review verdict
**Task quality:** Needs fixes
**Reasoning:** M1, M2, M3 and the counting fix are correct and tested, and the module is within budget. The M3 fix opened a path (N1) where a failed constructor's junk temp is renamed into a visible, unreadable lake file on a later commit. That must be closed before approval; N2 is optional in the same pass.


---

## Re-review round 2 (fix commit a34b01d)

Scope: N1 and N2, plus the line budget. I read T02-02-fix2.diff and the "Fix round 2" notes. I re-ran the round-1 scratchpad repro and the UT02-05/UT02-10 tests in the worktree (18 passed). The tree is unchanged.

### Per finding
| Finding | Status | Evidence |
|---|---|---|
| N1: commit publishes a corrupt temp after a double fault | ✅ | `_open_file` now appends to `_temp_files` only after the ParquetWriter constructor succeeds (lake.py:306). An undeletable failed temp goes to `_discard` (lake.py:225, 304). commit only tries to unlink those, with OSError suppressed and no rename (lake.py:344); a leftover still has its dot name, so `lake_glob` ignores it. abort unlinks them and counts failures (lake.py:370). `_close` reads `_temp_files[-1]`, and that entry is now always the open file. **Repro re-run:** first write SchemaViolation; retried write plus commit gives `files 1 rows 2`, the one committed file reads 2 rows, and the junk file is not published. New test `test_ut02_10_parquet_writer_failure_junk_never_published` covers this. |
| N2: a value past year 9999 escapes as OverflowError | ✅ | `_cast_time` checks s/ms/us against `_US_RANGE` (lake.py:46, 118). I checked the bound arithmetic: `-(-min // f)` is a ceiling and `max // f` a floor, giving exactly datetime.min/max per unit (s: -62135596800 … 253402300799). ns always fits. Repro re-run: year > 9999 now gives `LakeContractError ... type on _source_updated_at (1 rows)`, raised before anything is buffered. The parametrised test covers both edges and the values just outside them. |
| Line budget 380 | ✅ | lake.py is 378 lines at a34b01d. The condensed commit log call and return keep the same fields. |

### New findings
None. Minor findings M4–M8 from the first review still stand as optional polish.

### Re-review verdict
**Task quality:** Approved
**Reasoning:** N1 is closed at the root: failed opens can no longer reach the publish list, and my repro now commits only readable files. N2 now fails cleanly as rule "type" before any state changes. Tests pin both fixes and the module is within budget.
