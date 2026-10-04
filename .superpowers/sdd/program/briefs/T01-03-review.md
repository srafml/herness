# T01-03 review: Protocols, metadata constants and row helpers

Commit 7dd382c (base 6b70189), worktree agent-a267155bc6e5de08b. Read-only review; no builder report (build agent killed after commit), so evidence below is from my own runs.

### Spec Compliance
- ✅ Spec compliant. Unit by unit:
  - U01-16 `Connector`: ✅ plain Protocol; `name`, `entities`, `check()`, `sync(entity, since, until=None)`, `watermark_field` signatures match.
  - U01-17 `SupportsKeyListing`: ✅ runtime_checkable; `list_keys(entity)`.
  - U01-18 `SupportsToolStreams`: ✅ runtime_checkable; `tools`, `stream_key`, `sync_tool(tool, entity, since, until)` (`until` required, as the spec says).
  - U01-19 constants: ✅ `METADATA_FIELDS` order is exact; `METADATA_SCHEMA` types and nullability are exact (only `_payload` is nullable; the two timestamps are `timestamp("us","UTC")`). `KEY_SCHEMA`, `DEFAULT_BATCH_ROWS` 10000, `DEFAULT_CHECKPOINT_ROWS` 500000 and `UNORDERED_SOURCES` are exact.
  - U01-20 `record_id`: ✅ fullmatch patterns use `[0-9]`-style classes. The key rule is 1-512 characters with none below U+0020. The error is `SchemaViolation("invalid record key", source=, entity=)` and never carries the key.
  - U01-21 `split_range`: ✅ refuses naive input, a step <= 0 or a non-timedelta step, and more than 100,000 windows (a ceiling count is checked before the loop). It returns an empty list for start >= end. The `min(cur+step, end)` step is rewritten to avoid overflow near `datetime.max`; the result is the same.
  - U01-22 `to_snake`: ✅ steps 1-9 in order. Only ASCII letters and digits are kept; lower-to-upper is split before upper-to-upper-lower (`HTTPStatus` -> `http_status`). The `f_` prefix is added when the name has a leading `_` or starts with a digit. More than 256 input characters or more than 128 output characters gives the error "unusable field name".
  - U01-23 `flatten_record`: ✅ `text()` rules are exact (bool before int; float and dict/list via `json.dumps` with the compact separators, `ensure_ascii=False` and `default=str`; an aware datetime becomes UTC ISO ending in `Z`; Decimal and anything else via `str`). `fields` sets the order and missing keys give None. A display pair is split only when the key set is exactly `{value, display_value}`. A collision gives `column collision {col}`.
  - U01-24 `parse_source_timestamp` / `parse_arrow_timestamps`: ✅ SN format and date-only are read as UTC; `+HHMM` becomes `+HH:MM`, `Z` becomes `+00:00`, and fractions longer than 6 digits are truncated before `fromisoformat`. A result without an offset is refused, numbers need an `epoch_unit`, bool is excluded, and the year must be 1970-2100 after conversion to UTC. The message is fixed, no value is echoed, and the exception has no cause (`from None`). The array path covers tz-aware and naive timestamps, date32/date64, string and large_string, nulls, a vectorised range check and other types. Errors carry the bad-row count; casts truncate to us.
  - U01-25 `RowBatcher`: ✅ `METADATA_SCHEMA` columns come first, then `columns`, then field names in first-seen order. A column once seen stays in every later batch, null-filled, as `pa.string()`. `_fetched_at` comes from a single `clock()` per batch, and `rows_emitted` is tracked. A tombstone has `_deleted=True` and null payload and fields. A naive datetime gives `SchemaViolation(source, entity)`.
  - U01-26 `tombstone_batch`: ✅ vectorised; `_record_id` uses `binary_join_element_wise` and the constant columns use `pa.repeat`/`pa.nulls`. It checks 1-100,000 keys, none null, all of string type, plus the record_id key rule (via RE2). It validates source and entity through `record_id` and requires both datetimes to be aware. The result has exactly `METADATA_SCHEMA`.
- ⚠️ Items I could not settle from the spec:
  - The `http_client` re-export is absent. That is per the ruling (deferred to T01-14) and is noted in the base.py docstring.
  - `RowBatcher` validates `batch_rows` and raises `ConfigError` (rows.py:215-217). The spec lists the constraint >= 1 but names no error type, so this is extra but reasonable.
  - Acceptance text uses `-k "UT01-14 ..."` with hyphens. Test names use underscores per the global constraint, so the working selection is `-k "UT01_14 or ..."`, which selects 113 tests, all passing.

### Evidence (run in the worktree)
- `pytest tests/unit/connectors --cov=...base --cov=...rows --cov-branch`: 333 passed. Coverage is 100% line and 100% branch for both base.py and rows.py.
- The card -k selection (underscore form): 113 passed. `--require-test-ids` passes.
- `ruff check herness tests`: clean. `ruff format --check`: clean. `mypy`: 0 issues in 145 files. `lint-imports`: 13 kept, 0 broken.
- Budgets: base.py 164/170 and rows.py 318/320 lines (both within budget; rows.py has 2 lines of headroom).
- Layering: base.py and rows.py import only pyarrow, `herness.core.errors` and `herness.core.time` (as `clock`), so they sit correctly at L2. The pyproject contracts reference `herness.connectors` at package level, so no contract update is needed (not new settings modules).
- UT01-18 writes a RowBatcher batch containing a tombstone through a real `herness.store.lake.LakeWriter` (write, commit, read Parquet back), which meets the acceptance check.

### Strengths
- Every error path keeps values out of messages: record_id checks the context repr; timestamp errors raise `from None` and the tests assert `__cause__ is None`.
- The array paths are vectorised (range check and tombstone ids), and the tombstone key rule is checked in bulk with RE2 rather than per row.
- Tests cover the edge cases well: `datetime.max` overflow, exactly 100,000 windows, a 100,001-key limit, Devanagari digits, bytes and Decimal inputs, and ns-to-us truncation. Hypothesis properties cover PT01-01/03/04/05.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. rows.py:121-122: epoch values are rounded, not truncated, to us. `datetime.fromtimestamp` rounds half-even, so `1704164645.9999996` s becomes `03:04:06`. The U01-24 postcondition says "truncated to us"; the algorithm text says `fromtimestamp` (plan-mandated), so the error is sub-microsecond. It could be closed with integer arithmetic, e.g. `EPOCH + timedelta(microseconds=int(v * 10**6 // unit))`, or left as it is.
2. rows.py:230-238: `_note` registers field names one at a time before rejecting a metadata-named field. For `{"a": ..., "_payload": ...}` the row is refused, but column `a` stays registered and shows up null-filled in every later batch.
3. rows.py:281: a non-`str` field value (e.g. `{"a": 3}`) raises `pyarrow.ArrowTypeError` from `flush()`, not a taxonomy error. The bad row stays in `_buffer`, so every later `add` or `flush` fails again. The signature types the fields as `str | None`, so this is caller error, but a one-line check in `add` (or clearing the buffer before building) would fail cleanly with `SchemaViolation`.
4. rows.py:213: the parameter `clock` shadows the module alias `clock`. The spec requires both names, so this is harmless. However, the default `clock.now` is bound when the module is imported, so monkeypatching `herness.core.time.now` will not reach a default-constructed batcher (freezegun still works).
5. test_base.py:173: the protocol runtime-check test is labelled UT01-18. U01-17 and U01-18 map to UT01-94/79/92 (owned by later cards), so this label is loose.
6. test_base.py:101-102: the first assertion in `test_ut01_28_split_range_window_limit` uses a 1-day step (`timedelta(1)`, which gives 2 windows) and has nothing to do with its docstring "exactly 100,000 windows". Only line 103 tests what the docstring says.
7. rows.py is at 318 of its 320-line budget, so it has 2 lines of headroom. Any later fix in this module will need offsetting trims.

### Assessment
**Task quality:** Approved
**Reasoning:** All eleven units (U01-16 to U01-26) conform to the verbatim specs, with exact constants and schema and messages that carry no values. All card test IDs are present and pass, including the real LakeWriter check. Coverage is 100/100, and ruff, mypy and import-linter are clean. The remaining items are minor robustness and labelling polish.
