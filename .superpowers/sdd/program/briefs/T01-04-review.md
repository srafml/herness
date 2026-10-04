# T01-04 review (verify agent) — head 0bf033e, base 8a073b4

### Spec Compliance
- ❌ Issues found:
  - U02-62 / impl 02 §2.3 rule 5 (import order): `herness/store/ops/__init__.py:23-37` imports `.ingest` BEFORE `.migrate` (ruff isort alphabetises the single import block). U02-62 Algorithm requires "explicit `from .<area> import` lines per block, in the import order of §2.3 rule 5" = core, migrate, shared, then other areas in table order. The file's own docstring (lines 3-6) states that order. The `__all__` block order (02 core, 02 migrate, 01 ingest) is correct.
- Per unit:
  - U01-27 Watermark/get_watermark ✅ — SQL verbatim (`_SELECT_WATERMARK` + WHERE), parse_utc on both stamps, corrupt → `SchemaViolation("corrupt watermark", source=, entity=)`.
  - U01-28 set_watermark ✅ — upsert SQL verbatim incl. `WHERE excluded.value > watermark.value`; `rowcount == 1`; `op="set_watermark"`; naive rejected via format_utc before the write.
  - U01-30 SliceRow/ensure_slices ✅ — upsert SQL verbatim incl. done-row exception; read-back via read_all in chunks of 500 slice starts, load_json decode, ordered by slice_start. (See Minor: 500 starts + 2 key params = 502 params per chunk.)
  - U01-31 mark_slice_* ✅ — statements match; op = function name; `rowcount == 0` raises `SchemaViolation("slice not found", source=, entity=)` inside the callback; error cut to 500 chars; files via dump_json.
  - U01-32 FileIngestRow/get/record ✅ — SQL verbatim; fingerprint fullmatch `[0-9a-f]{64}`; `INSERT OR IGNORE` rowcount == 1.
  - U01-92 list_watermarks ✅ — SQL verbatim, `max_rows=10_000`, per-row parse as U01-27.
  - Card acceptance: pytest -k selection 29 passed (re-run); UT02-68 passes; lint-imports 13 kept / 0 broken (herness/store has no herness.connectors import; ops-areas-acyclic kept); no `herness/store/ops_ingest.py`; project mypy clean (128 files); check_module_size clean; ingest.py 257/260; core.py untouched.
  - Coverage (re-run): ingest.py 139 stmts / 10 branches, 100% line, 100% branch.
- ⚠️ Cannot verify from diff: none material (all gates re-run locally).

### Mutation check (scratch copy, 13 mutants)
Killed: `>` → `>=` in watermark WHERE; drop done-row exception; flip `>`/`<` in exception; drop 500-char truncation; drop final sort; `rowcount == 0` → `< 0`; fullmatch → match; drop `last_error = NULL`; drop `attempts + 1`; record returns True unconditionally; corrupt-watermark except clause changed.
Survived: `_IN_CHUNK = 100000` (chunking not observable in tests); `max_rows=len(chunk)+5` (benign).

### Strengths
- Every SQL statement matches the unit specs verbatim; timestamps always through format_utc/parse_utc; monotonic watermark verified by exact-boundary tests (equal value and value − 1 µs both return False).
- Error raised inside the write callback so the transaction rolls back; context kwargs asserted in tests.
- Tests are behavioural and strong (mutation kill rate 11/13); the fixed-width stored text is asserted directly.
- Defensive list-of-strings check on `files` JSON is small and well-placed.

### Issues
#### Critical (Must Fix)
- None.

#### Important (Should Fix)
1. `herness/store/ops/__init__.py:23` — import block order violates U02-62 Algorithm / impl 02 §2.3 rule 5 (core, migrate, shared, then areas in table order); `.ingest` is imported before `.migrate`, contradicting the module's own docstring (lines 3-6). Every later area block (evidence, runs, jobs, …) will hit the same isort reordering, so the pattern is set here. Fix (verified in a scratch copy: ruff `I` clean, UT02-68 passes): move the `# 01 ingest` import block after the `# 02 migrate` block and put `# isort: split` on the line before `# 01 ingest`.

#### Minor (Nice to Have)
1. `herness/store/ops/ingest.py:25,177-180` — U01-30 says "chunks of 500 parameters"; each chunk binds 500 starts + source + entity = 502 parameters. Harmless under SQLite limits, but either use 498 starts per chunk or note the reading. 
2. `tests/unit/store/ops/test_store_ops_ingest.py:156-165` — `test_ut01_22_reads_in_chunks` does not prove chunking (mutant `_IN_CHUNK = 100000` survives). Could monkeypatch `ingest._IN_CHUNK` small or spy on `read_all` call count.
3. `tests/unit/store/ops/test_store_ops_ingest.py:124` — docstring says the longer-last-slice reset covers "running/failed/done" but only the done case is exercised there (failed is covered in the next test; running is not).
4. `tests/unit/store/ops/test_store_ops_ingest.py:270-274` — re-export identity test is labelled UT01-20 but is really a UT02-68-style check and covers only 5 of 12 names; consider looping over all 12 ingest names.
5. `herness/store/ops/ingest.py:139-144` — `_paths` raises SchemaViolation with no context (source/entity); acceptable, just less diagnosable than the watermark path.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The ingest area is correct, verbatim to spec and well tested (100%/100% coverage, 11/13 mutants killed); the one blocking item is the package import order required by U02-62 / §2.3 rule 5, a one-line `# isort: split` fix plus moving the block.

---

## Re-review 1 — fix commit c026d32

Scope: I1, M2, M3, M4, M5 (M1 is a spec-wording note only; no code change expected).

Gates re-run on c026d32 (clean tree):
- `PYTHONUTF8=1 uv run pytest -k "UT01_20 or UT01_22 or UT01_23 or UT01_24 or UT01_95 or UT02_68" -q -p no:logging`: 29 passed. ingest.py coverage is 100% line and 100% branch (139 stmts, 10 branches).
- ruff check herness tests: all checks passed. ruff format --check: clean.
- lint-imports: 13 kept, 0 broken. mypy (project config): no issues in 128 files. ingest.py: 257/260 lines.

Findings:
- I1 ✅ Resolved. `herness/store/ops/__init__.py` now imports core, then migrate, then `# isort: split`, then `# 01 ingest`, following U02-62 and impl 02 §2.3 rule 5. `__all__` order is unchanged and correct.
- M2 ✅ Resolved. `test_ut01_22_reads_in_chunks` wraps `read_all` and asserts `max_rows` per call is [500, 500, 201], so a mutant that stops chunking now fails.
- M3 ✅ Resolved. A running row with a longer planned end now resets to pending with rows 0 and files (); attempts stay at 2, which matches the verbatim SQL. The running, failed and done cases are all covered now.
- M4 ✅ Resolved. The identity test covers all 12 ingest names.
- M5 ✅ Resolved. `_paths(row, table)` raises `SchemaViolation("invalid JSON in <table>.files", source=, entity=)`, and the test asserts the context.
- M1: still open as a spec-wording note for the controller. Each chunk binds 502 parameters. This is harmless and does not block.

New findings: none.

**Task quality:** Approved
**Reasoning:** The one Important item and all four code-level Minor items are fixed without regressions, and every gate passes on c026d32.
