# T03-05 Text stage — review (verify agent)

Worktree agent-ab2f686106ea93b0a, base a59bb45, head c96598f. Read-only review.

### Spec Compliance
- ✅ Spec compliant (with the controller rulings applied: private `_Report` Protocol with retype marker text.py:41-49; ST03-02 text-stage half with carry-over stated in the module docstring; hand-built warehouses).
  - U03-24 normalize_text ✅ (text.py:78-83: NFKC → `\s+`→" " → strip → [:4000] → strip; None → "").
  - U03-25 compose_text ✅ (text.py:86-90: a + "\n\n" + b, strip; "" for two empties; ≤ 8,002).
  - U03-26 content_hash ✅ (text.py:93-95; sha256 utf-8 hexdigest[:32]).
  - U03-27 pair_text ✅ (text.py:98-100 exact format).
  - U03-28 build_text_redacted ✅: copy only when record in both core tables with equal non-NULL `source_updated_at` (text.py:182-193); NULL-safe NOT EXISTS anti-join (text.py:202-209); 20,000-row Arrow chunks via a separate cursor (text.py:210-215); empty texts dropped and counted (text.py:230-236); `redact_table(tbl, ["text"], "record_id")` (text.py:238); NULL redaction counted failed, no row (text.py:239-240); entity + content_hash added, insert through registered view then unregister in finally (text.py:244-257); DETACH in finally, failure logged not raised (text.py:116-118, 150-154); counters rows/cache_hits/failed (+=) (text.py:119-121); log `enrich.text.redacted` with counts only.
  - Deviations acceptable: (1) `ATTACH ?` — verified myself: DuckDB 1.5.5 raises `ParserException: syntax error at or near "?"`; literal quoting with `''` doubling verified to attach a path containing `'`; DuckDB literals do not treat backslash as escape, control chars < 0x20 refused. No injection path found. (5) non-schema DuckDB errors carry only the class name (`from None`) — stricter than spec "<duckdb message>", required by ENG §3.4; acceptable.
- Tests: UT03-22 ✅, UT03-23 ✅, UT03-24 ✅, PT03-03 ✅ (hypothesis), UT03-25 ✅ (3 copied / 2 redacted / 1 failed / 1 empty, stub redactor, DETACH checked, logs contain no text), IT03-02 ✅ (300 records, 1 % changed, real redact_table + spy), ST03-02 ✅ text-stage half only (ruling). IDs in names, docstring first lines, module `pytestmark` all present.
- ⚠️ Cannot verify from diff / spec-level:
  - Acceptance "on tiny_build" — fixture absent (ruling).
  - Spec interaction: U03-28 Concurrency says "redaction uses spec 10's process pool", but stage chunks (20,000) equal `_redact_pool.CHUNK_ROWS` (20,000) and `redact_chunks` runs in-process when `count <= 1` (_redact_pool.py:99). So the text stage never uses the pool; redaction is single-core at nightly scale. Implementation follows the algorithm literally; controller/spec decision needed (e.g. read `workers * 20,000` rows per stage chunk).
  - Heartbeat/should_yield per chunk (F03-01 note) — U03-28 signature has no ctx; belongs to the pipeline card.

### Verification run
- `pytest` on the 3 new test files: 30 passed. Coverage herness/enrich/text.py 100 % line, 100 % branch (133 stmts, 22 branches).
- ruff check / format --check clean on touched files; `uv run mypy` (configured tree) 0 issues in 183 files. text.py 258/260 lines.
- Focused experiment (scratchpad script): `ATTACH ?` rejected on 1.5.5; quoted literal with `'` in path attaches.

### Strengths
- Security posture is careful: no text in logs/errors, class-name-only for value-bearing DuckDB errors, cause suppressed; ATTACH failure logs only `error_type`.
- NULL-safe anti-join and NULL-timestamp handling (re-redact rather than copy) are sound choices.
- DETACH in finally that cannot mask the stage's error; view always unregistered.
- Tests assert real behaviour (exact texts, hashes, which ids reached the redactor, counters, log fields).

### Issues
#### Critical (Must Fix)
None.
#### Important (Should Fix)
None.
#### Minor (Nice to Have)
1. text.py:196-215 — the streaming selection runs on a separate cursor (separate DuckDB connection). If a caller ever runs the stage inside an explicit transaction on `wh`, the cursor cannot see the uncommitted copied rows (or uncommitted tables): verified experimentally — with `BEGIN` on `wh`, copied rows were re-redacted and inserted again (copied=2, redacted=2, rows=4 duplicates; `enrich.text_redacted` has no key), then DETACH failed and the transaction aborted. Impl 02 documents auto-commit per statement for build connections (02 impl line 2931), so the current contract holds; add a docstring precondition ("wh in auto-commit mode") or a cheap guard, to avoid silent duplicates later.
2. text.py:205 — records with NULL `record_id` are silently skipped and not counted in `empty`/`failed`; consider counting them in the log.
3. text.py:168 — `raise ... from None` suppresses printing but `__context__` still holds the DuckDB exception (with possible row values); fine for tracebacks via logging, noting for any code that walks `__context__`.
4. tests/unit/enrich/test_enrich_text.py:311 — in-memory connection never closed (trivial).

### Assessment
**Task quality:** Approved
**Reasoning:** All five units match the spec exactly, the stage algorithm, counters, DETACH/unregister and security constraints are correct and fully covered (100 %/100 %); the ATTACH-literal deviation is verified necessary and safely escaped. Remaining items are minor hardening plus a spec-level pool-parallelism contradiction for the controller.
