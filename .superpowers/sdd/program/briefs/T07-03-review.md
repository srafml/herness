# T07-03 review — Memory schema and item data access (HEAD 9d77331, base 6b70189)

### Spec Compliance
- ✅ Spec compliant (every unit), with the spec notes and one ruling conflict listed under ⚠️.

| Unit | Result | Note |
|------|--------|------|
| U07-20 070_memory_indexes.sql | ✅ | Ten `CREATE INDEX IF NOT EXISTS` statements in the §4.1 order, definitions match word for word; no other DDL or DML; 30/40 lines |
| U07-21 insert_memory_item | ✅ | All 12 columns checked before SQL (a missing key gives `SchemaViolation(... invalid <col>)`); dump_json; constant INSERT; IntegrityError mapped to SchemaViolation by run_write |
| U07-22 get_memory_items | ✅ | 0–500 ids, MEMORY_ID_RE, dedupe, input order, JSON parsed; C3 message `memory_item.data invalid JSON for <id>` |
| U07-23 find_memory_item | ✅ | Exactly one selector; three constant clauses; distinct 1–5 statuses; `ORDER BY created_at, memory_id LIMIT 1` |
| U07-24 update/touch | ✅ | SET columns come only from the UPDATABLE allowlist; UNCHANGED sentinel; `None` for expires_at sets NULL; increment 0–1000; touch takes 1–200 ids |
| U07-25 fts_candidates | ✅ | Runs on `conn` or `connection()` directly; `fts5: syntax error` returns []; any other error goes through C5 (see M1) |
| U07-26 entity_candidates | ✅ | Matches the spec SQL, plus a CASE guard for non-object entries (an extension) |
| U07-27 count_proposals | ✅ | Scope required; constant filter clauses in a fixed order |
| U07-28 existing_query_ids / finding_facts / evidence_rows | ✅ | Columns agree with 003; empty input runs no SQL; JSON parsed; NULL confidence becomes 0.0 (spec note) |
| U07-29 get_task_scratchpad | ✅ behaviour / ⚠️ R-68 | Behaves correctly and is tested, but it runs its own SQL on `task` (see ⚠️-1) |
| U07-30 maintenance_rows / fts_check_and_rebuild / pending_embedding_count | ✅ | Constant query per selector, ordered by memory_id, bounds checked; integrity-check uses `rank = 1` (spec note, justified) |
| U07-35 session_memory_ids | ✅ | Matches the spec SQL; length 1–64 |
| ops/__init__.py 07 block | ✅ | Placed after privacy with `# isort: split`; `__all__` block after privacy under `# noqa: RUF022`; UT02-68 passes |

- ⚠️ Cannot verify from the diff, or needs a controller ruling:
  1. **R-68 vs `ops-areas-acyclic`** (herness/store/ops/memory.py:317-329). U07-29 says "Memory runs no SQL on `task`" and calls `runs.get_task`. Impl 02 §2.3 rule 5 and the import-linter contract forbid one area importing another. The builder kept the contract and reads `SELECT checkpoint FROM task WHERE task_id = ?` itself. The result is the same (same column, NULL gives None, a non-object gives C3 SchemaViolation), but it goes against ruling R-68 and U07-29 step 1. Both sources are binding, so the controller has to choose:
     - (a) accept this and record a U07-29/R-68 carve-out in the spec; or
     - (b) add one narrow ignore `herness.store.ops.memory -> herness.store.ops.runs` and call `get_task`.
     The later closed_loop adapters (`task_spec`, `dead_task_count`, `recent_done_runs`) hit the same conflict, so one ruling should cover all of them.
  2. `_memory_rows.py` (289 lines) has no §2 module-map row. The controller notes allow a private sibling when a module-map spec note is recorded. The note is recorded in the report (decision 1) but not yet in the spec, so it has to be carried into a docs pass.
  3. Spec notes recorded by the builder (all reasonable):
     - `rank = 1` on the integrity check;
     - FTS `unterminated string` behaviour (M1);
     - NULL `finding.confidence` returns 0.0;
     - memory.py is at 389/390, which leaves no room for U07-101 (M3).

### C8 check (004 vs 070 vs §4.1)
- 070 holds only `CREATE INDEX IF NOT EXISTS`: no table, FTS table or trigger DDL, and no data change. IT07-08 checks this two ways: the text scan, and the upgrade diff, which adds only `index` objects, exactly these ten.
- It is exactly the ten §4.1 "070 indexes", in the listed order. Expressions, column lists and partial `WHERE` clauses match §4.1 exactly.
- None of them duplicates a 004 index. 004 creates `memory_item_layer_status`, `memory_item_kind_status`, `recommendation_run`, `decision_log_rec` and `outcome_rec_measurement`; the 070 names and definitions are all different.
- Every column the indexes reference exists in 004: `expires_at`, `data`, `layer`, `kind` and `provenance` on memory_item; `target_type`, `target_id` and `expected_metric` on recommendation.
- All ten are in UT02-32 `_LATER_INDEXES` (tests/unit/store/ops/test_store_ops_migrations.py:85-97). UT02-32 passes.
- The data access is written against the real 004 schema:
  - column names match;
  - `data` and `provenance` are NOT NULL `json_valid`, and object-ness is enforced by the app;
  - `confidence` is nullable in 004 but NOT NULL in the app (`_is_unit` rejects None);
  - `use_count` is an int (STRICT table), checked by `is_count`;
  - timestamps: the app only checks `str`, and the 004 GLOB CHECK enforces the format (a bad value becomes an IntegrityError, mapped to SchemaViolation);
  - `memory_item_au` fires on `content, kind` and is compatible with `update_memory_item`.
  - Reads of `finding`, `evidence` and `task` use 003 columns that exist.
- Completeness of the report's 004-vs-§4.1 list: accurate but not complete. Omitted:
  - (a) `recommendation`, `decision_log` and `outcome` are also `STRICT` (the report names STRICT only for memory_item);
  - (b) `recommendation.created_at` has the fixed-width timestamp CHECK in 004, while §4.1 says "—";
  - (c) 004's `recommendation` column order differs from §4.1: `kind` comes after `summary`, and `expected_usd` after `confidence`. This matters only if a later closed_loop card builds `RecommendationRow` positionally from `SELECT *`.
  - The rest is correct and covers everything else I found: memory_fts tokenizer, au-on-kind, the nullability of recommendation.confidence, decision_log.reason and outcome.query_id, and the timestamp CHECKs.

### Strengths
- Clean split: all SQL is constant; the IN lists are built from `marks(n)`; SET and filter column names come only from constant tuples (TH07-09).
- The C5 mapping carries `op=<function>`. Busy is tested through a fake connection.
- IT07-08 checks the full upgrade: a seeded store at 006 is upgraded, its schema equals a fresh one, the added objects are exactly the ten indexes, and the FTS index stays in sync, checked with `rank = 1`.
- UT07-06 uses EXPLAIN QUERY PLAN to show the 070 indexes serve the U07-23/27/30/35 lookups.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None as a builder defect. ⚠️-1 (R-68) needs a controller ruling before merge.

#### Minor (Nice to Have)
- **M1** herness/store/ops/memory.py:224. Only `fts5: syntax error` maps to []. An unbalanced quote gives SQLite's `unterminated string` (verified on this SQLite), which raises SchemaViolation, and tests/unit/store/ops/test_store_ops_memory.py:359 pins that. This follows the spec text, and U07-58 always balances its quotes. Under TH07-08's "syntax errors return no candidates" it arguably should also return []. The builder recorded this as a spec note.
- **M2** tests/unit/store/ops/test_store_ops_memory.py:455, 472, 514, 549, 562, 574, 586. The supporting tests for U07-27/28/30/35 carry ID UT07-07, but UT07-07 covers only U07-21–U07-24. Traceability is slightly off; the IDs that fit (UT07-28/29/36/81/84) belong to later cards. Acceptable, but record it for the later cards.
- **M3** herness/store/ops/memory.py is at 389/390, and the §2 row also assigns U07-101 (`purge`, R-54) to this module. The card that adds U07-101 will need `_memory_rows` room or a budget ruling. The report records this.
- **M4** C8 report omissions (a)–(c) above. They are documentation only.
- **M5** herness/store/ops/memory.py:307-313. In `evidence_rows` the comprehension target `sql` shadows the local query-text `sql`. It is correct only because comprehension scope is separate; renaming it (e.g. `text`) would be clearer.
- **M6** herness/store/ops/_memory_rows.py:248-254. With `conn=None`, reads go through `core.connection()` rather than `core.read_all` (C1 says read_one/read_all). That is justified so the C5 `op=<function>` naming survives, and every query is bounded (at most 10,000 rows). Record it as a C1 interpretation.
- **M7** herness/store/ops/memory.py:366-369. With a caller `conn`, the rebuild runs inside the caller's transaction instead of "a new run_write(op='fts_rebuild')". That is unavoidable, because a nested run_write raises ConfigError, and the behaviour is tested. It is not a defect.

### Gates run (worktree, HEAD 9d77331)
- `PYTHONUTF8=1 uv run pytest -q -p no:logging -k "UT07_06 or UT07_07 or UT07_08 or UT07_09 or IT07_08 or UT02_32 or UT02_68"`: 43 passed.
- Coverage (branch): memory.py 100% line and branch; _memory_rows.py 100% line and branch.
- mypy: 0 issues (145 files). lint-imports: 13 kept, 0 broken. check_module_size: exit 0. check_type_ownership: exit 0. ruff check and ruff format: clean.
- Budgets: 070 30/40; memory.py 389/390; _memory_rows.py 289 (default 400, no §2 row); ops/__init__.py 260/400.
- ops/core.py is unchanged in the diff (verified with git diff --stat).
- pyproject: a single ignore edge `herness.store.ops.memory -> herness.store.ops._memory_rows` with a comment. It is minimal and justified; `_memory_rows` itself is still covered by the `* -> core` rule and forbidden from importing other areas.
- Test naming: every function is `test_ut07_0x_…` or `test_it07_08_…` and its docstring starts with the ID; pytestmark is unit or integration.

### Assessment
**Task quality:** Approved

**Reasoning:** All units match their specs, and the C8 constraint holds exactly (indexes only, the ten §4.1 indexes in order, no duplicates, listed in `_LATER_INDEXES`), with 100% coverage and every gate green. The one open item is a spec conflict between R-68 and ops-areas-acyclic in U07-29, which needs a controller ruling (accept a spec carve-out, or add a narrow `memory -> runs` ignore), not a builder fix.
