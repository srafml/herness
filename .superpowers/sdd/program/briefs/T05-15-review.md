# T05-15 review: Recording and formatting (U05-35, U05-36, U05-48)

Reviewer: verify agent. Worktree agent-ac52e3148173d683a, base 750ec27, head 4d05efa.
Checks run: card tests (32 passed, 4.8 s), mypy (0 issues on the two modules), ruff check and format (clean), check_module_size (clean). Line counts: tools.py 211/400, _tools_record.py 228/240.
Focused probes (scratchpad scripts, DuckDB 1.5.5): (a) a timeout that fires while rows stream; (b) a NaN/Inf cell in the evidence sample; (c) the text of a DuckDB conversion error on a redact-on-read column.

## Spec Compliance
- U05-35 execute_recorded / RecordedResult: ❌ (two issues). Steps 1-3 and 6-9 match the spec: query_id, guard vs internal guard, lru_cache(64) keyed by (build_id, sql), empty schemas dropped, the cache check comes after the guard, a cache hit still writes evidence and evidence_use, cache_put only on a miss with row_count <= return_rows, timer.cancel() in finally, one-pass hash/count/keep, the exact hint strings, the 500-char error cut, the 50-row sample from the kept rows, normalize_sql, clock.now, and the metric with the §8.2 `tool` label. Step 5 is wrong for timeouts that fire while rows stream (I1). Step 6 "JSON-safe" is wrong for non-finite floats (I2).
- U05-36 format_result: ✅. The header, the column and type lines, every cell rule, the 79+`…` cut, the untrusted wrap after the cut, the arbitrary-rows line, the shrink loop that recomputes `shown`, and a hard cut that keeps the bound all match.
- U05-48 wrap_untrusted: ✅. `&` is escaped first, then `<` and `>`. Attributes are filtered to `[A-Za-z0-9:_.-]`, and `record_id=""` when None (R-20).
- Tests: every ID has at least one function (UT05-61..67, UT05-124, FT05-02, FT05-04). Names, docstrings and pytestmark are correct (`fault` matches the existing tests/fault modules). UT05-61 compares the hash with spec 04 `result_hash` over all 300 rows fetched through `fetchall`, a path independent of the implementation. ✅
- Layering and security: no httpx in herness/harness. The imports are L0/L3 → L4. The `.secrets.baseline` change only moves the line of an existing docs/impl/05 entry (2647→2648, caused by the inserted §2 row) and updates the timestamp; no audited entry was dropped. ✅
- ⚠️ Cannot verify from diff: the dispatch-level parts of FT05-02/FT05-04 ("error result", "loop continues") need U05-34/U05-40, which are later cards. They are approximated with `ToolResult.from_error` and a follow-up call; that is acceptable for now. The real `tiny_build` does not exist yet; the tests use a stand-in build.

### Deviations (rulings)
1. RecordedResult is a frozen slots dataclass with `rows: list[tuple[object, ...]]`. **Acceptable; needs a spec note.** The spec contradicts itself: U05-36's cell rules need Decimal, date and datetime cells, which JsonValue excludes.
2. `to_arrow_reader` instead of `fetch_record_batch`. **Acceptable; needs a spec note** (deprecated in 1.5.5; impl 04 and the Verifier use the same call). It does not cause I1: `fetch_record_batch` also returns a pyarrow reader.
3. blocked_columns read from `get_config()`. **Acceptable; needs a spec note.** ToolContext carries no settings, and the Verifier does the same.
4. An injected `sql.query` timeout is mapped to the standard timeout QueryError. **Acceptable.**
5. A SchemaViolation from result_hash becomes QueryError("result has a value that cannot be recorded"). **Acceptable; needs a spec note.** The same care is missing on the sample path (I2).
6. Param-name and `$key` validation raises ToolInputError. **Acceptable:** it enforces preconditions the spec states. The regex check runs first, so the rf-pattern is safe.
7. TOOL_CONTENT_MAX_CHARS is a literal, cross-checked against ToolResult in UT05-66. **Acceptable.**
8. The runtime-checkable `_ResultCache` Protocol, because WarehouseHandle has no cache methods. **Acceptable; needs a spec note.**
9. The new `_tools_record.py` §2 row was added to the binding spec in the same commit. **Acceptable;** the controller should confirm.

## Strengths
- The streaming design is clean: one generator counts, keeps and bounds rows while spec 04 hashes, and nothing is reimplemented.
- The `_col_<i>` lineage mapping for unaliased computed columns is correct and tested (UT05-61 `upper(summary)`).
- The lru_cache key is exactly (build_id, sql): `_GuardInput` fields are compare=False, so they hash as constants.
- The tests are behavioural: cursor-count proof of the cache hit, a real locked SQLite for FT05-04, exact-string assertions for formatting, and 100 % coverage.

## Issues

### Critical (Must Fix)
None.

### Important (Should Fix)
- **I1: a timeout during row streaming escapes as a raw `OSError`, not `QueryError("timeout after …")`.** herness/harness/_tools_record.py:170-176. `cur.execute` returns once the stream is ready. When `cur.interrupt` fires while `iter_batch_rows` pulls batches from the arrow reader, pyarrow raises `OSError("INTERRUPT Error: Interrupted!")`, which is not a `duckdb.Error`. Reproduced through `execute_recorded`: timeout_s=0.3 on `SELECT i, md5(CAST(i AS VARCHAR)) AS h FROM range(900000) t(i)` gives `builtins.OSError`. This breaks the step 5 error mapping and the TH05-09 contract for exactly the large scans the timer exists for. UT05-63 passes only because its cross-join aggregate blocks inside `execute()`. Fix: map an interrupt surfacing from the reader to `timeout_error`. For example, catch `OSError`/`pyarrow.ArrowException` with "INTERRUPT" in the text, or check whether the timer fired (`timer.finished.is_set()`). Other reader errors should become QueryError with the DuckDB hint. Add a streaming-timeout case to UT05-63.
- **I2: a NaN or Infinity double in the first 50 kept rows makes `record_evidence` raise `SchemaViolation`.** herness/harness/_tools_record.py:191 (`json_safe` passes floats through) and :214 `sample_rows`. Ops `dump_json` rejects them ("JSON for evidence.result_sample contains NaN or Infinity"; reproduced). That error is not retryable and not a QueryError, so a query like `SELECT 'inf'::DOUBLE` escapes as a store schema error. `result_hash` itself accepts NaN. Step 6 requires a JSON-safe sample, so map non-finite floats to a string ("NaN", "Infinity", "-Infinity") or to None in `json_safe`, and add a test.
- **I3 (plan-mandated): the DuckDB error text can carry raw cell values to the model.** herness/harness/_tools_record.py:175. The spec requires "first 500 chars of the DuckDB message" as the QueryError message. DuckDB conversion errors quote the value. Reproduced: `SELECT CAST(summary AS INTEGER) AS x FROM core.work_item` returns `Conversion Error: Could not convert string 'db down' to INT32 ...`. `summary` is a redact-on-read, untrusted column, so this path bypasses both `redact_text` and `wrap_untrusted` (TH05-01) and breaks the global constraint "No ... ticket text or personal data in any error message" (ENG §3.4). Needs a spec ruling, for example passing the message through `redact_text` and replacing quoted literals with a placeholder. Suggested owner: spec 05 §3 U05-35 step 5.

### Minor (Nice to Have)
- **M1 (plan-mandated): nested retries.** herness/harness/tools.py:121-124. The outer `retry_call("sqlite_write")` wraps `record_evidence`/`record_evidence_use`, which already retry inside `run_write` (6 attempts, 10 s busy_timeout, 30 s max_elapsed). A persistently locked store blocks the worker for about 60 s or more instead of about 30 s; FT05-04 counts only the outer attempts (tests/fault/harness/test_tools_fault.py:118). Recommend a spec note: either drop the outer wrap for the real ops handle or document the compound bound. Not a correctness defect.
- **M2: the timer can fire late on the thread-cached cursor.** herness/harness/_tools_record.py:161/180 with warehouse.py:113. `cancel()` does not stop a callback that has already started, so a late `interrupt()` could land on that thread's next query. The window is small, and DuckDB resets the interrupt flag at query start; a comment would do.
- **M3: UT05-124 checks only two modules.** tests/unit/harness/test_tools_recording.py:297. It greps `hashlib`/`sha256` only in tools.py and _tools_record.py, while the spec says to grep herness/harness. The `result_hash` FunctionDef check does cover the whole tree. Consider an AST check for `hashlib.sha256` used over rows across herness/harness, with an allow-list.
- **M4: the stand-in helper lives under tests/unit.** tests/unit/harness/_tools_standin.py is imported by tests/fault/harness/test_tools_fault.py:36. Shared helpers belong in tests/support/.
- **M5: `json_safe` is public but not in the §2 exports.** herness/harness/tools.py:40 exports it in `__all__`, but the §2 exports row for tools.py does not list it. Add it to the row or keep it private until the Verifier card needs it.
- **M6: the cached RecordedResult is shared and mutable.** herness/harness/tools.py:63. The frozen dataclass holds mutable lists, and cache hits return the same object (UT05-64 asserts `second is first`). A caller that redacts `result.rows` in place (RunSql per U05-36 step 4) would corrupt the cache. Document "replace, never mutate" or store tuples.
- **M7: small formatting edge cases.** herness/harness/tools.py:174 can cut an escape sequence in half, leaving a trailing backslash. Line 188 does not escape column names or type text, so model-authored aliases containing `|` or a newline can break the table. The CRLF → single `\n` collapse is a harmless reading of the spec.

## Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation is clean and closely follows the spec, but a timeout during streaming (I1) and non-finite floats in the sample (I2) escape as the wrong exception types on reachable paths. I3 needs a spec ruling on DuckDB error text leaking redact-on-read values.


## Round 1 re-review (fix commit afbbb57, previous head 4d05efa)

Checks run: card tests (41 passed, 5.7 s). mypy on the two modules and on tests/support/tools_standin.py: 0 issues. ruff check: the only finding is TID251 in openai_compat.py, known red on base. ruff format: clean. check_module_size: rc 0. Line counts: tools.py 221/400, _tools_record.py 258/280 (new budget). pyarrow is already a declared dependency (pyproject.toml:20).

### Finding status
- **I1: fixed.** `_engine_error` maps `duckdb.Error`, `OSError` and `pa.ArrowException` to the timeout error when the timer fired or the text says INTERRUPT, and to the DuckDB hint otherwise. Your repro (timeout_s=0.3, md5 over range(900000)) now raises `QueryError('timeout after 0.3s')`. The new UT05-63 streaming test and the parametrized reader-error test cover it.
- **I2: fixed.** `json_safe` maps non-finite floats to "NaN", "Infinity" and "-Infinity". The new test writes the row to the real ops store and reads it back.
- **I3: partially fixed. Still leaks.** `_QUOTED_RE` (_tools_record.py, `safe_error_text`) masks only single-quoted literals. DuckDB also quotes values in double quotes and echoes them unquoted. Reproduced through `execute_recorded` against the stand-in build, with `redact_text` stubbed to identity to isolate the quoting step. The real `redact_text` finds secrets and PII patterns, not free ticket text, so it does not close these gaps:
  - `strptime(summary, '%Y-%m-%d')` → `Invalid Input Error: Could not parse string "db down" according to format specifier ... \ndb down\n^` (the value appears twice, once quoted, once bare)
  - `CAST(summary AS DATE)` → `invalid date field format: "db down", expected format ...`
  - `CAST(summary AS JSON)` → `Malformed JSON ... Input: "db down" when casting ...`

  This still breaks the ruling's intent: no redact-on-read ticket text in error messages (ENG §3.4, TH05-01). **Needs fixing.** Suggested fix inside the ruling's order:
  1. Keep only the first line of the DuckDB message. This drops the value echo and the `LINE 1:` SQL echo.
  2. Mask double-quoted segments as well as single-quoted ones. Double quotes also wrap identifiers, which is an acceptable loss because the hint already points to describe_table.
  3. Then run `redact_text` and apply the 500-char cut.

  Add test cases for the strptime, DATE-cast and JSON-cast forms.
- **M2: addressed.** There is now a comment, plus the `fired` event.
- **M3: fixed.** UT05-124 now runs an AST scan of every hash call site in herness/harness against an allow-list. I checked the five entries against the tree with grep:
  - sql_guard.py `_query_hash`: 16 hex of the SQL text
  - tracing.py `is_sampled`: sampling key
  - findings.py `compute_dedup_key`: SHA-1 dedup key
  - memory/policy.py `content_hash` and `keyed_hash`: `ids.sha256_hex` over memory text and key

  None of them hashes result rows. The allow-list is sound. The `<=` check tolerates stale entries, which is acceptable.
- **M4: fixed.** The helper moved to tests/support/tools_standin.py, and `StoreOps` is shared there.
- **M6: fixed.** `_copy` on both cache_get and cache_put. The test mutates the copy it got back and shows a later hit is unaffected.
- **M7: fixed.** The cut strips a dangling backslash before the ellipsis, and column names and types are escaped. Both have tests.
- **M1, M5:** recorded as spec notes (controller ruling).

### New findings
- **R1-I1 (Important):** the I3 gap described above. File: herness/harness/_tools_record.py, `_QUOTED_RE` and `safe_error_text`.
- **R1-M1 (Minor):** `_engine_error` treats any error text containing "INTERRUPT" as a timeout (`"INTERRUPT" in text`), including an identifier echoed in a binder error. The guard rejects unknown columns first, so I found no way for agent SQL to trigger this. Consider `text.startswith("INTERRUPT Error")`, or rely on `fired` alone.
- No other regressions found. The `rstrip("\\")` also drops a real trailing backslash from the source text. That is cosmetic because backslashes are not escaped in cells anyway.

### Round 1 assessment
**Task quality:** Needs fixes
**Reasoning:** I1, I2, M2-M4, M6 and M7 are resolved. The I3 fix covers only single-quoted values, but DuckDB also puts cell values in double quotes and echoes them unquoted on context lines, so redact-on-read ticket text still reaches the model through error messages.


## Round 2 re-review (fix commit 3d6544e, previous afbbb57)

Checks run: card tests (45 passed, 10.2 s). mypy: 0 issues. ruff check and format: clean. _tools_record.py is 265/280.

### Finding status
- **R1-M1: fixed.** `_engine_error` now maps to timeout only when `fired` is set or the exception is a `duckdb.InterruptException`. The parametrized test covers interrupt text arriving without the timer.
- **R1-I1: partially fixed. Ticket text still leaks when the cell value contains a line break.** Keeping the first line happens before masking, and each mask needs a closing quote on the same line. A multi-line value (common for ticket text) is cut after its first line, which leaves an opening quote with no closing quote, and its first line leaks. Reproduced with DuckDB 1.5.5, calling `safe_error_text` with `redact_text` stubbed to identity:
  - `CAST(s AS INTEGER)`, `s = 'line one secret\nline two'` → `Conversion Error: Could not convert string 'line one secret`
  - `CAST(s AS DATE)` → `... invalid date field format: "line one secret`
  - `strptime(s, '%Y')` → `Could not parse string "line one secret`
  - `CAST(s AS JSON)` with a `\r\n` value → `... Input: "tail secret\r`

  Values without a line break are masked correctly, including those that contain quote characters.

  Suggested fix, still within the ruling's order: after the paired masks, also mask an unmatched opening quote through the end of the line, for example `re.sub(r"['\"].*", "'<value>'", line)` applied once the paired substitutions are done. Alternatively, mask on the full text with `re.DOTALL` before taking the first line. Add a test where the ticket text contains a line break; the stand-in's `SUMMARIES` currently have none.
- **Carry-over (not blocking, per controller):** `error(summary)` passes SqlGuard and its value comes back unquoted. This belongs to the sql_guard owner.
- No other regressions: the empty-text edge case, the 500-char cut and the "query failed" fallback on failed redaction are all tested.

### Round 2 assessment
**Task quality:** Needs fixes
**Reasoning:** R1-M1 is resolved, and quoted values on a single line are masked. A multi-line cell value still leaks its first line into the model-facing error, because the closing quote falls on a line that was already dropped. A one-regex fix and a test will close it.


## Round 3 re-review (fix commit 89da3fc, previous 3d6544e)

Checks run: card tests (53 passed, 7.0 s). mypy: 0 issues. ruff check: clean. _tools_record.py is 274/280.

Probe: 8 ticket-like values (line breaks, CRLF, lone and paired `'`/`"`, doubled quotes) crossed with 10 error forms (casts to INTEGER, DATE, TIMESTAMP, DECIMAL, UUID, BOOLEAN, INTEGER[], STRUCT and JSON, plus strptime). I ran them against DuckDB 1.5.5 and passed each error through `safe_error_text` with `redact_text` stubbed to identity. The multi-line cases from round 2 (R2-I1) no longer leak.

### Remaining leak (R3-I1, Important)
The DOTALL mask runs from the first quote to the **last quote anywhere in the message**. When that last quote sits on a later echo line, the rest of that line gets joined onto the first line, which is the line that is kept. strptime repeats the value on line 2, so a value containing a pair of double quotes leaks its tail:

- raw: `Invalid Input Error: Could not parse string "say "hi" secret" according to format specifier "%Y"\nsay "hi" secret\n^\n...`
- output: `Invalid Input Error: Could not parse string "<value>" secret`

A second case, `quote ' inside " dq secret`, leaks the same way: `"<value>" dq secret`. These were 2 leaks out of 80 cases. Any ticket text that contains a quoted phrase (common: `user said "it's broken" again`) hits this through strptime.

Suggested fix, small: when a mask's match contains a line break, cut the text right after the token, since nothing after a masked multi-line span can be trusted. For example, in `_mask_quoted` use `m = re.search(...)`, and if `"\n" in m.group() or "\r" in m.group()`, return `text[:m.start()] + token`. Alternatively, apply the line cut to the original text at the start of the first masked span. Add a strptime case with a value containing a pair of double quotes to `test_ut05_61_multiline_cell_never_echoed` or `safe_error_text_rules`.

### Other
- No other regressions. The `\x00` sentinels could only collide with NUL characters in the error text, and those would sit inside masked spans; I found no practical issue.
- Carry-overs as recorded: the `error()` guard gap and M1 (not blocking).

### Round 3 assessment
**Task quality:** Needs fixes
**Reasoning:** Values with line breaks are now masked. But the full-text greedy mask pulls trailing text from strptime's echo line onto the kept first line, so a value containing a quoted phrase leaks part of itself (2 of 80 probe cases). The fix is a one-line cut after a mask that spans a line break, plus a test.

## Round 4 re-review (fix commit 651fead, previous 89da3fc)

Checks run: card tests (133 passed, 14.2 s, including 80 probe cases). mypy: 0 issues. ruff check: clean. _tools_record.py is 278/280.

- **R3-I1: fixed.** `_mask_quoted` now cuts at the token when the first-to-last-quote match spans lines, using any `splitlines()` separator. My 80-case probe (8 values x 10 DuckDB 1.5.5 error forms, `redact_text` stubbed to identity) reports **0 leaks**. It includes the paired-double strptime case (`say "hi" secret`) and the mixed-quote case that leaked in round 3.
- **New test `test_ut05_61_error_text_probe`:** its 8 values x 10 forms mirror my probe. For every case it asserts that a `QueryError` is raised, that `"secret"` is absent from the message, that `"secret"` is absent from the text passed to the stub redactor (so the masking happens before redaction), and that `<value>` is present. Every probe value carries "secret", so these assertions are real. The rules test adds the exact round 3 two-line strptime string.
- No regressions. Carry-overs as recorded: the `error()` guard gap and M1.

### Round 4 assessment
**Task quality:** Approved
**Reasoning:** All review findings are resolved or ruled as carry-overs. The error-text sanitizer now fails closed on quotes, line breaks and echo lines across the full probe matrix.
