# T01-09 review (Files connector) — verify agent

Commits reviewed: db1efe5..d4d3697 (6a4d1cb, 8cd089f, empty d4d3697).
Evidence run by reviewer: `pytest -k "UT01_45 … ST01_09 or UT01_94" --cov=herness.connectors.files --cov-branch`
→ 38 passed, 2 skipped (ST01-08 symlink: no privilege; UT01-49 XLSX: excel absent, O-3). Coverage files.py
226/226 stmts, branch 65/66 (partial 306->305, list_keys loop exit) = 99 %. No warnings.
Focused check (named risk: RE2 vs Python `re.fullmatch` divergence in the vectorised record id): compared
`files._record_ids` with `base.record_id` on 13 boundary keys (512/513 ASCII, 2-byte and 4-byte chars, `\n`,
`\x1f`, `\x7f`, U+2028, space, `|`) — identical accept/reject and identical values in every case.

### Spec Compliance
- ❌ Issues found (1, test-side): ST01-09 zip-bomb test passes vacuously on a machine without the excel
  extension (see Important I1); otherwise the units conform.

Per unit:
- ✅ U01-46 FilesConnector — ctor signature, `register("connector","files")`, `name`, `entities`; `check()`
  (link/junction/not-dir → `ConfigError("inbox missing or not a directory")`, missing entity folder →
  `connectors.files.entity_folder_missing` WARNING); `watermark_field` (updated_field else "mtime"); `sync`
  half-open `[since, until)` with open bounds in `(mtime, name)` order; `list_keys` (snapshot-only ConfigError
  with the spec message, "no snapshot file", newest = `found[-1]`, KEY_SCHEMA batches). Never moves/deletes files.
- ✅ U01-47 InboxFile / candidates — frozen dataclass with the 5 fields (aware mtime from `st_mtime_ns`); check
  order exactly as the algorithm: prefix skip → symlink/junction (`symlink`) → `lstat` → `not_regular` →
  `resolve(strict=True)` containment (`outside_root`) → `settling` (DEBUG only) → `too_large`; each non-settling
  skip logs `connectors.files.rejected` WARNING with `entity`, `file`=rel_path, `reason`; folder OSError →
  `SourceUnavailable("inbox unreadable")`. Size check is on `lstat` before any byte is read (TH01-09).
- ✅ U01-48 fingerprint_file — `open(...,"rb")`, 1 MiB chunks into `hashlib.sha256`, size + post-read
  `os.stat().st_mtime_ns` re-check → `InboxFileChanged(entity, rel_path)`; OSError → `SourceUnavailable(
  "inbox file unreadable")` with `file=rel_path` context (deviation 7 accepted: HernessError has one positional).
- ✅ U01-49 read_file — DuckDB `:memory:` with `memory_limit="1GB"`, `threads=2`, autoinstall/autoload off
  (asserted by a spy test); SQL is constant, `$path` and `$sheet` are bound (no interpolation; the query-builder
  test uses a path containing `'`); `LOAD excel` only for xlsx; snake renaming with collision → SchemaViolation;
  missing key column → "key field missing"; `_source_key` single cast / `binary_join_element_wise(…,"|")`;
  null → `null key in {rel_path} at row {n}`; `_source_updated_at` = `parse_arrow_timestamps` or file mtime;
  `_payload` per-row JSON with original names (byte-identical to `json.dumps` with the spec kwargs — the reused
  `JSONEncoder` has exactly the parameters json.dumps builds); `_fetched_at=clock()`, `_deleted=False`;
  metadata columns first in METADATA_SCHEMA order (order verified by value assertions, not only by type);
  post-read re-stat (missing or size/mtime_ns change → `InboxFileChanged`); `con.close()` in `finally`;
  `duckdb.Error`/`pa.ArrowException` → `SchemaViolation("unreadable inbox file …")` chained from the original.
  Metadata-name clash impossible: `to_snake` prefixes leading-underscore names with `f_`.

Per test row:
- ✅ UT01-45 (dotfile, `~$x.xlsx`, junction runs on Windows, symlink conditional, 2 s file, wrong suffix,
  directory, unreadable, linked entity folder, check, watermark_field, sync window, list_keys)
- ✅ UT01-46 (equal digests across names/chunks; patched-read append → InboxFileChanged; mtime change; missing)
- ✅ UT01-47 (composite `eu|7`, null key at row 1500 across batches, missing key, collision, invalid key w/o value)
- ✅ UT01-48 (parsed `Last Modified`; mtime fallback; missing/unparseable updated field)
- ✅ UT01-49 Parquet int64/decimal128 kept; XLSX part skips with the clear O-3 message (group ruling) — ⚠️ unverified here
- ✅ UT01-50 sparse 1 GiB+1 → `too_large` (exactly 1 GiB accepted); corrupt Parquet → SchemaViolation;
  corrupt XLSX only exercises the missing-extension path here (Minor m3)
- ✅ ST01-08 patterns `..\*.csv` / `../*.csv` / `sub/*.csv` rejected by settings; unvalidated `../*.csv` caught by
  containment (`outside_root`); symlink-to-win.ini part skipped (no privilege) — junction path covered in UT01-45
- ❌ ST01-09 sparse 1.1 GiB CSV rejected without `duckdb.connect`/`open` ✅; XLSX zip bomb ❌ vacuous here (I1)
- ⚠️ BT01-04 CSV pass, XLSX skip, Parquet FAIL (~246k vs 1M rows/s) — see I2
- ✅ UT01-94 contract (static `Connector`, runtime `SupportsKeyListing`, METADATA/KEY schemas, registration via
  probe module exec). `build_connector` (U01-55) not yet present — correctly not invented.
- ✅ IDs/markers: every function name carries its ID and the docstring starts with it; `pytestmark = unit` in
  both unit files, `[integration, slow]` in the bench. ST01-10 belongs to T01-10 (not this card).
- ✅ Budget 346/360; coverage 99 % line+branch (≥90/85); layering L2 only (core + connectors imports).

⚠️ Cannot verify from diff / this machine:
- XLSX reads (`read_xlsx` with `sheet`, all_varchar) and zip-bomb behaviour within the memory cap — excel
  extension absent (O-3); card acceptance "excel extension loads offline in CI" depends on T10-26.
- Symlink skip with a real symlink (no SeCreateSymbolicLinkPrivilege); must run on CI/Linux.
- With the extension present, the ST01-09 bomb builder deflates 1.2 GiB inside a `unit`-marked test — may exceed
  the 30 s "slow" threshold; check runtime once O-3 is satisfied.

Deviations judged:
1. `to_arrow_reader(batch_rows)` instead of deprecated `fetch_record_batch` — ACCEPT (same RecordBatchReader
   semantics; avoids a DeprecationWarning under `error:::herness`). Record in spec errata.
2. Vectorised `_record_id` — ACCEPT: first key goes through `record_id` (validates source/entity), all keys
   through an RE2 regex equivalent to `_KEY_RE` (verified above), same "invalid record key" message without the
   value. `_payload` via a reused JSONEncoder — ACCEPT (byte-identical).
3. Null-key row = 1-based data row across batches — ACCEPT (spec ambiguous; documented).
4. Silent dot/`~$` skip — ACCEPT with spec note: the §8.1 reason enum has no value for it, and logging every Office
   lock file as WARNING would be noise. `unreadable` for a per-file OSError — ACCEPT (value is in the §8.1 enum;
   failing the whole folder for one vanished file would be worse).
5. Linked entity folder rejected (`reason=symlink`, `file=entity`) — ACCEPT (TH01-08 hardening; tested).
6. "updated field missing" message — ACCEPT.
7. SourceUnavailable context `file=` — ACCEPT.
8. Unsupported suffix → SchemaViolation — ACCEPT (defensive; tested).
9. UT01-94 scope without `build_connector` — ACCEPT.
10. Sparse files cleaned up in `finally` — ACCEPT.

### Strengths
- Tight, readable module (346/360) with the U01-47 check order mirrored 1:1 in `_reason`.
- No SQL string building for paths/sheets; DuckDB config asserted by a spy.
- Security tests check the negative directly (ST01-09 fails if `duckdb.connect` or `open` is called).
- No secret/personal values in messages: the invalid-key test asserts the key value is absent; messages carry only
  rel_path/entity as the spec mandates.

### Issues
#### Critical (Must Fix)
- none

#### Important (Should Fix)
- I1 tests/unit/connectors/test_files_read.py:217-229 — `test_st01_09_xlsx_zip_bomb_fails_as_schema_violation`
  does not skip without the excel extension: it shrinks the bomb to 8 MiB (line 222) and passes because
  `LOAD excel` fails. It asserts nothing about TH01-09 (bomb within memory cap) on this machine while reporting
  green, contrary to the group ruling ("XLSX-reading tests skip with a clear message"). Fix: add
  `@pytest.mark.skipif(not _EXCEL, reason=d.EXCEL_SKIP)` and drop the small-bomb branch; keep the
  missing-extension → SchemaViolation behaviour in its own clearly named test.
- I2 tests/bench/connectors/test_connectors_files_bench.py:59,70 (plan-mandated conflict) — BT01-04 asserts the
  spec floors, so the Parquet case (10M rows, floor 1M rows/s) is a known deterministic FAIL (~246k rows/s) on
  every `-m "integration and slow"` run. Root cause is the spec-mandated per-row `json.dumps` for `_payload`
  (U01-49 step 4d): a few µs/row in CPython caps throughput near 250k rows/s regardless of the reader.
  Recommended handling: controller raises an open item / DECISIONS ruling (either relax the Parquet target to
  ~200k rows/s, or allow a vectorised payload whose bytes may differ, e.g. DuckDB `to_json`, which changes
  decimal/timestamp rendering and so is a contract change for downstream consumers). Until ruled, mark only the
  parquet parameter `pytest.param(..., marks=pytest.mark.xfail(reason="BT01-04 parquet target unreachable with
  per-row json payload; ruling O-xx", strict=False))` so the bench suite is not red by design. Keep the CSV floor.

#### Minor (Nice to Have)
- m1 herness/connectors/files.py:182 — ctor parameter `clock` shadows the module alias `clock` (default
  `clock.now` works only because defaults bind at def time). Spec names the parameter, so keep it; add a comment.
- m2 herness/connectors/files.py:240 — `p.resolve()` is called again after `_reason` already resolved it
  (213-227); return the resolved path from `_reason` to avoid the second syscall and a tiny TOCTOU window.
- m3 tests/unit/connectors/test_files_read.py:194 — UT01-50 "corrupt XLSX" is satisfied by the missing-extension
  path here (docstring admits it). It does assert the `duckdb.Error` cause, but the real corrupt-zip path is
  unverified until O-3; consider splitting like I1.
- m4 tests/unit/connectors/test_files.py:280 — ST01-08's symlink-to-system-file part always skips on Windows dev
  machines; a junction-based fallback (junction entry pointing at `%SYSTEMROOT%`) would let the threat row run
  without the symlink privilege (UT01-45 shows junctions work here).
- m5 herness/connectors/files.py:260 — silent `.`/`~$` skip diverges from U01-47 step 3 ("each skip except
  settling logs"); record in spec errata (add a reason value or state they are silent).
- m6 tests/bench/connectors/test_connectors_files_bench.py:16 — bench imports a helper from `tests.unit`; move
  `excel_available`/`EXCEL_SKIP` to `tests/support` if more benches need them.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** U01-46..49 conform to the spec and the reported deviations are sound (record-id vectorisation
verified equivalent), but the ST01-09 zip-bomb security test passes vacuously without the excel extension (I1) and
BT01-04 commits a known-failing Parquet threshold that needs a ruling plus an xfail marker (I2). Both fixes are
test-only and small.

---

## Re-review round 1 (fix 3b5f0ec, base d4d3697)

Scope: I1, I2, m1, m2 and any regressions. Parked by the sub-controller: m3, m4, m5 (spec note), m6.
Evidence: worktree HEAD 3b5f0ec; card filter (incl. UT01_94) with `--cov-branch` gives 38 passed, 3 skipped
(ST01-08 symlink privilege; UT01-49 XLSX and ST01-09 zip bomb, both with the O-3 EXCEL_SKIP message). files.py is
still 226/226 stmts with branch 65/66 (99 %), 346/360 lines. No warnings. The bench collects 3 params; not run.

- I1 ✅ Resolved. `test_st01_09_xlsx_zip_bomb_fails_as_schema_violation` now has
  `skipif(not _EXCEL, reason=d.EXCEL_SKIP)` and always builds the 1.2 GiB bomb, so it is no longer vacuous.
  The missing-extension path moved to the new `test_ut01_50_missing_excel_extension_is_schema_violation`. It uses
  a `_NoExcel` connection proxy whose `LOAD excel` raises `duckdb.IOException`, and it asserts both the
  SchemaViolation message and the `duckdb.Error` cause. It runs whether or not the extension is installed. The
  ID and docstring conventions are kept.
- I2 ✅ Resolved per the ruling. Only the Parquet param is wrapped in
  `pytest.param(..., marks=xfail(strict=False, reason="BT01-04 open item: … awaiting program ruling"))`. The CSV
  and XLSX floors are unchanged. Nit, not blocking: the reason says "open item" but has no O-number. Add the
  number once the program allocates one.
- m1 ✅ Comment added on the `clock` parameter; signature unchanged.
- m2 ✅ Resolved. `_reason(p, root, rel_path)` now builds the `InboxFile` from the path it resolved for the
  containment check, and there is no second `resolve()`. Regression check of `_eligible`:
  - settling is still DEBUG and returns None;
  - every other reason logs a WARNING and returns None;
  - an OSError still gives `unreadable`;
  - an eligible file returns the InboxFile.

  The check order is unchanged, the InboxFile fields are unchanged (path is resolved, mtime is the same
  `_mtime(st_mtime_ns)`), and UT01-45 passes.

New findings: none. There is one nit on the I2 reason (no O-number yet).

### Assessment (round 1)
**Task quality:** Approved
**Reasoning:** I1 and I2 are fixed as ruled, and m1 and m2 are applied without changing behaviour. Card tests and
coverage are unchanged apart from the expected extra skip without the extension. The parked items are left
unchanged.
