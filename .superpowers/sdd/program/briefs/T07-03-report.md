# T07-03 report — Memory schema and item data access

Status: DONE_WITH_CONCERNS (only spec notes; no blocker)
Commit: 9d77331 feat(store): add memory indexes migration and ops area memory (T07-03)
Worktree: D:\herness\.claude\worktrees\agent-a3620dd7b00aba8a7 (base 6b70189)

## Continuation of killed WIP
The previous agent had split row types / SQL constants / plumbing into `_memory_rows.py` but left
memory.py half-migrated (446 lines, duplicated constants, a broken `HEX16_RE` import). I finished
the split: memory.py now imports the constants and helpers from `_memory_rows` (renamed helper
`selector` -> `identity_clause` to avoid clashing with the `selector` parameter), fixed ruff/mypy
findings (S608 noqa on constant SQL, redundant isinstance checks flagged by mypy, EM101), fixed
the tests, added missing coverage and wrote IT07-08 (was absent).

## Files / line counts
- herness/store/migrations/070_memory_indexes.sql — 30 / 40
- herness/store/ops/memory.py — 389 / 390
- herness/store/ops/_memory_rows.py — 289 (not in the §2 module map; default 400 applies) — private
  sibling, imported only by memory.py
- herness/store/ops/__init__.py — 260 / 400 (07 block after privacy, `# isort: split`, __all__ block)
- pyproject.toml — one `ignore_imports` entry in `ops-areas-acyclic`:
  `herness.store.ops.memory -> herness.store.ops._memory_rows` (required: the wildcard contract
  forbids any ops module importing another ops module; minimal and justified)
- tests/unit/store/ops/test_store_ops_memory.py (UT07-06..UT07-09), 
  tests/integration/store/test_store_ops_memory_migration.py (IT07-08),
  tests/unit/store/ops/test_store_ops_migrations.py (`_LATER_INDEXES` += the ten 070 indexes, UT02-32)

## Tests / gates
- Card tests: 30 unit + 2 integration pass; coverage memory.py 100% line/branch, _memory_rows.py 100%.
- `pytest -m "(unit or integration) and not slow"`: 3873 passed, 5 skipped, 1 xfailed.
- ruff format/check clean, mypy clean (145 files), lint-imports 13 kept, check_type_ownership 0,
  check_module_size 0. All pre-commit hooks passed on commit (no --no-verify).
- RED evidence: the killed WIP did not import (HEX16_RE / LAYERS undefined) and
  test_ut07_08_fts_syntax_error_returns_empty failed on `"unterminated` (see decision 3); GREEN after fixes.

## Decisions
1. Private sibling `_memory_rows.py` (row TypedDicts MemoryItemRow/FindingFact/EvidenceRow,
   Unchanged/UNCHANGED, id regexes, constant SQL, C1/C2/C3/C5 helpers). memory.py re-exports the
   types. Needed: code + types do not fit 390 lines. SPEC NOTE (module map): add a §2 row for
   `herness/store/ops/_memory_rows.py` (private sibling of area memory; budget ~300), and note that
   the §3.5 row types are declared there and re-exported by memory.py.
2. get_task_scratchpad reads `SELECT checkpoint FROM task WHERE task_id = ?` itself instead of
   calling runs.get_task (U07-29 step 1), because `ops-areas-acyclic` forbids area->area imports
   (controller notes). It honours `conn` when given. SPEC NOTE: U07-29 / R-68 conflict with the
   ops-areas-acyclic contract; spec should say "read-only SQL on task.checkpoint" or move the read
   to a shared function.
3. fts_candidates maps only messages containing `fts5: syntax error` to [] (spec verbatim). SQLite
   3.50 reports an unbalanced quote as `unterminated string` (no fts5 prefix), which therefore
   raises SchemaViolation. U07-58 always emits balanced quoted tokens, so recall is unaffected.
   SPEC NOTE: consider also mapping `unterminated string` to [] (TH07-08).
4. fts_check_and_rebuild runs `INSERT INTO memory_fts(memory_fts, rank) VALUES('integrity-check', 1)`
   (spec text omits `rank`). rank=1 makes FTS5 compare the index against the external-content
   table, which is the only way to detect the rowid desync of a TEXT-PK table after VACUUM (known
   spec note). SPEC NOTE: put `rank = 1` into U07-30.
5. insert/update validate all 12 columns app-side (created_at str, expires_at/last_used_at str|None
   in addition to the spec list) so a missing key raises SchemaViolation("insert_memory_item:
   invalid <column>") instead of KeyError.
6. entity_candidates wraps `json_extract(e.value,'$.id')` in `CASE WHEN e.type='object'` so a
   non-object entry in data.entities is skipped instead of raising (malformed JSON path error).
7. finding_facts: NULL finding.confidence (003 allows NULL) is returned as 0.0 (FindingFact.confidence
   is float). SPEC NOTE.
8. Supporting tests of U07-27/28/30/35 carry ID UT07-07 (their own IDs UT07-28/29/36/81/84 belong
   to later cards' Tests rows).

## C8 spec notes — 004_memory.sql vs impl 07 §4.1 DDL
All ten §4.1 "070 indexes" reference columns that exist in 004; 070 adds only indexes (verified by
IT07-08: upgrade diff = exactly the ten indexes; schema equals fresh; FTS integrity-check(rank=1) OK).
memory_item:
- 004 table is STRICT; §4.1 does not mention STRICT.
- data / provenance: 004 has `CHECK (x IS NULL OR json_valid(x))` (any JSON, not object); §4.1 says
  "JSON object (app)" with no DDL constraint listed. Object-ness is app-enforced (U07-21/U07-24).
- confidence: 004 nullable, no range CHECK; §4.1 "no" null, 0-1, NOT NULL (app) — app-enforced.
- created_at / expires_at / last_used_at: 004 has fixed-width UTC GLOB/length CHECKs; §4.1 lists "—".
- layer CHECK and status CHECK (five statuses): match (004).
- kind: no CHECK in 004 (app, 11 kinds) — matches §4.1 "(app)".
- content length <= 8000: app only (no CHECK) — matches.
- use_count: 004 `NOT NULL DEFAULT 0`, no >= 0 CHECK — matches §4.1 (app).
- memory_id `mem_` prefix: app only — matches.
- Indexes 004: memory_item_layer_status, memory_item_kind_status — match.
memory_fts: 004 adds `tokenize = 'unicode61 remove_diacritics 2'` (not mentioned in §4.1);
  content='memory_item', content_rowid='rowid' — match.
Triggers: memory_item_au fires `AFTER UPDATE OF content, kind` (§4.1/U07-24 speak of content only).
recommendation:
- confidence: 004 nullable; §4.1 Null "no" (0-1 app).
- numbers / confidence_basis / finding_ids: 004 `x IS NULL OR json_valid(x)` with NOT NULL (+ defaults
  '{}' / '[]' on confidence_basis/finding_ids, not stated in §4.1).
- target_type four values, summary <= 400, rec_ prefix: app only — match.
decision_log:
- reason: 004 nullable; §4.1 Null "no" (<= 1000 app).
- decided_by / decided_at / effective_at NOT NULL; decided_at/effective_at carry timestamp CHECKs in 004.
- decision_log has REFERENCES recommendation(rec_id) — match.
outcome:
- query_id: 004 nullable; §4.1 Null "no".
- measured_at timestamp CHECK in 004 (§4.1 "—").
- details default '{}' + json_valid; measurement >= 1; verdict CHECK; UNIQUE (rec_id, measurement) — match.

## Concerns
- Spec notes above (module-map row for _memory_rows.py; U07-29 vs ops-areas-acyclic; fts5
  unterminated-string; `rank = 1`; NULL-confidence mapping; C8 nullability differences for
  recommendation.confidence, decision_log.reason, outcome.query_id which later closed_loop cards
  must enforce app-side).
- memory.py is at 389/390 — no room for U07-101 (listed in the §2 row: "functions of U07-21–U07-30,
  U07-35, U07-101"); the card that adds U07-101 will need to put it in _memory_rows.py or get a
  budget ruling.
