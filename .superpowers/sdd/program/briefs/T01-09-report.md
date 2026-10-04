# T01-09 report (Files connector) — DONE_WITH_CONCERNS

Base: db1efe5. Worktree: D:\herness\.claude\worktrees\agent-a76a139fd9fac8f69 (branch worktree-agent-a76a139fd9fac8f69).
Commits: 6a4d1cb wip(T01-09): files connector with unit tests green; 8cd089f wip(T01-09): vectorise record ids, add BT01-04 benchmark;
final: d4d3697 feat(connectors): add files connector (T01-09) (empty marker commit on top of the wip commits).

## Built
- herness/connectors/files.py (346 / budget 360): MAX_INBOX_FILE_BYTES=1_073_741_824, FILE_READ_MEMORY_LIMIT="1GB",
  FINGERPRINT_CHUNK_BYTES=1_048_576, InboxFileChanged(SchemaViolation) (ctor (entity, rel_path); context entity/file),
  InboxFile (frozen, slots), fingerprint_file, FilesConnector (@register("connector","files"); check, watermark_field,
  candidates, sync, list_keys, read_file). Logs connectors.files.rejected (WARNING; settling at DEBUG) and
  connectors.files.entity_folder_missing (WARNING).
- tests/unit/connectors/_files_data.py (152): inbox builders; hand-built minimal XLSX and XLSX zip bomb (no openpyxl in lock); sparse files.
- tests/unit/connectors/test_files.py (370): UT01-45, UT01-46, UT01-50 (size), ST01-08, ST01-09 (sparse CSV), UT01-94.
- tests/unit/connectors/test_files_read.py (296): UT01-45 (sync/list_keys), UT01-47, UT01-48, UT01-49, UT01-50 (corrupt), ST01-09 (zip bomb).
- tests/bench/connectors/test_connectors_files_bench.py (70): BT01-04 [integration, slow]; XLSX part skips without excel.
- No pyproject / import-linter change needed (package-level contracts already cover herness.connectors.files).

## Tests and gates
- RED: collection ImportError (herness.connectors.files missing) before the module existed.
- `pytest -k "UT01_45 or UT01_46 or UT01_47 or UT01_48 or UT01_49 or UT01_50 or ST01_08 or ST01_09"`: 35 passed, 2 skipped
  (UT01-49 XLSX: excel extension absent, O-3; ST01-08 symlink-to-win.ini: no symlink privilege — pattern + containment parts run).
- `PYTHONUTF8=1 uv run pytest tests/unit/connectors -q -p no:logging`: 386 passed, 3 skipped.
- Coverage herness/connectors/files.py: 99 % line (226/226 stmts, 0 missed), branch 65/66 (one partial: list_keys loop exit).
- ruff format/check clean; mypy (153 files) clean; lint-imports 13 kept; check_module_size exit 0; check_type_ownership exit 0.
- Pre-commit hooks ran on the wip commits (all passed; no --no-verify, no PRE_COMMIT_ALLOW_NO_CONFIG).
- BT01-04 (run once, this laptop): CSV 2M rows PASS (>= 200k rows/s, after vectorising record ids); XLSX skipped (no extension);
  Parquet 10M rows FAIL at ~246k rows/s vs 1M rows/s target.

## Deviations / spec notes
1. `fetch_record_batch` is deprecated in DuckDB 1.5.5 (DeprecationWarning; pytest filterwarnings "error:::herness" would fail) —
   used the equivalent `to_arrow_reader(batch_rows)`.
2. `_record_id`: vectorised equivalent of `record_id` (first key validated by `record_id` for source/entity, keys checked with the
   same no-control-char 1..512 rule, then joined) — same values and the same "invalid record key" error without the key value.
   `_payload` uses one reused `json.JSONEncoder(ensure_ascii=False, separators=(",",":"), default=str)` — byte-identical to json.dumps.
3. Null-key row number in "null key in {rel_path} at row {n}" is the 1-based data row (header excluded) across batches.
4. Names starting with "." or "~$" are skipped silently (spec reason list has no value for them; U01-47 says each skip "except
   settling" logs, but the event's reason enum has no hidden/lock reason). Per-file OSError during inspection → rejected
   reason `unreadable` (in the §8.1 reason list) instead of failing the whole folder; folder listing OSError → SourceUnavailable.
5. Hardening beyond spec: an entity folder that is itself a symlink/junction is rejected (reason `symlink`, file=entity) and lists nothing (TH01-08).
6. SchemaViolation messages for missing columns: "key field missing" (spec) and "updated field missing" (spec only says SchemaViolation).
7. fingerprint_file SourceUnavailable carries context file=rel_path (HernessError has only one positional arg).
8. Unsupported suffix in read_file → SchemaViolation("unsupported inbox file type") (settings already restrict patterns).
9. UT01-94: FilesConnector checked against Connector (static typing) + SupportsKeyListing (runtime), METADATA_SCHEMA columns,
   KEY_SCHEMA key batches, and registration (module exec'd under a probe name after the autouse registry reset).
   build_connector (U01-55) does not exist yet — not exercised.
10. Sparse files: created with seek(size-1)+write (instant on NTFS here); the 1 GiB+ files are deleted in finally blocks.

## Concerns
- BT01-04 Parquet 1M rows/s is not reachable with the spec's per-row `json.dumps` payload in Python (~246k rows/s measured);
  CSV with updated_field is dominated by rows.parse_arrow_timestamps' per-row string parsing (rows.py, T01-04, budget full).
  Needs a ruling (vectorised payload via DuckDB to_json would change payload bytes, or relax the target). Bench is slow-only.
- XLSX paths (read_xlsx with sheet, zip bomb within the memory cap) are unverified on this machine (excel extension absent, O-3);
  the SQL/params are unit-checked and the failure path (LOAD excel error → SchemaViolation) is covered. Whether DuckDB's excel
  reader fails (rather than streams) on the 1.2 GiB single-cell bomb needs checking on a machine with the extension.
- Symlink cases skip here (no SeCreateSymbolicLinkPrivilege); junction cases run (Windows).

## Fix round 1 (commit 3b5f0ec fix(connectors): address T01-09 review round 1)
- I1: test_st01_09_xlsx_zip_bomb_fails_as_schema_violation now has skipif(not _EXCEL) and always builds the 1.2 GiB bomb.
  New test_ut01_50_missing_excel_extension_is_schema_violation proxies the DuckDB connection so `LOAD excel` fails
  (duckdb.IOException) and asserts SchemaViolation with the duckdb cause; it runs with or without the extension.
- I2: BT01-04 Parquet param wrapped in pytest.param(..., marks=xfail(strict=False, reason="BT01-04 open item: ...")); CSV/XLSX floors unchanged.
- m1: comment on the `clock` parameter (default is the module's clock.now, body uses the parameter); signature unchanged.
- m2: `_reason(p, root, rel_path)` now returns `(reason, InboxFile | None)` and builds the InboxFile from the path it resolved
  for the containment check (no second resolve). files.py still 346/360.
- Tests: card filter 35 passed, 3 skipped (2 excel, 1 symlink privilege); tests/unit/connectors 386 passed, 4 skipped;
  files.py coverage 100 % line, 65/66 branch; ruff, mypy clean; hooks passed.
