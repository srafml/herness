# T07-26 report: Privacy purge entry point (R-54)

Base: 10f9733 (worktree-agent-a332fb55e4127b2f0). Checkpoints: 7150dc1 wip(T07-26) purge_rows ops + UT07-89 (substring selector, superseded); second wip attempt was refused by detect-secrets (test constant named SECRET, renamed PLANTED) and folded into the final feat(memory) commit, which carries lifecycle/facade purge, the controller-ruling selector fix, tests and spec notes.

## Units done
- U07-101 `herness.store.ops.memory.purge_rows` (+ `PurgeRows` frozen dataclass): defined in memory.py; selection/delete/scrub steps in private sibling `_memory_rows.py` (`purge_selector`, `purge_select`, `purge_apply`). Exported in the 07 memory block of `herness.store.ops` (`PurgeRows`, `purge_rows`). Error: `ToolInputError("purge_rows needs exactly one selector")`.
- U07-57 `MemoryLifecycle.purge(*, author_ref, record_id, now)`: dry run -> `vectors.delete` (VectorIndex.delete, 200/chunk) -> one `run_write` (blank `{"content": ""}` + reject `system`/`purged` per linked review item, then `purge_rows(conn=conn)`). Vector failure: `memory.purge.vector_failed` ERROR (count) + `ModelUnavailable("memory vector purge failed")`. Logs `memory.purge.completed` (count, scrubbed, review_items). Error: `ToolInputError("purge needs exactly one of author_ref, record_id")`.
- U07-100 `MemoryStore.purge(record_id=None, *, author_ref=None, now=None)` in `_facade.py` (lazy re-export by the package `__init__`, unchanged): validates, delegates. Error: `ToolInputError("purge needs exactly one of record_id, author_ref")`.

## Files and budgets
- herness/store/ops/memory.py 389/390 (`__all__` written compactly with `# fmt: skip` to make room)
- herness/store/ops/_memory_rows.py 386/400 (new §2 row added, budget 400)
- herness/harness/memory/lifecycle.py 340/340 (AT budget)
- herness/harness/memory/_facade.py 280/300; __init__.py 103/330 unchanged
- herness/store/ops/__init__.py 318/400
- docs/impl/07-memory.impl.md: §2 rows (lifecycle public symbols + `purge_args_ok`; new `_memory_rows.py` row), T07-26 spec note before U07-101.
- No migration, no pyproject change (existing ops-areas-acyclic ignore entry covers `_memory_rows`).

## Tests (all green)
- UT07-89 tests/unit/store/ops/test_store_ops_memory_purge.py (dry run == real, content/data/provenance citing, FTS rows, chunks of 500 via trace [3, 500], caller transaction rollback, author delete + history scrub, approved_by-only item untouched, unknown record -> nothing, idempotent, selector validation, package export)
- UT07-38, UT07-88 tests/unit/harness/memory/test_memory_purge.py (vector fail once -> ModelUnavailable, SQLite+reviews unchanged, retry erases, review blank+rejected, logs counts only, rerun 0; author scrub; decided review blanked not redecided; unknown record 0; late citing item between dry run and tx; selector validation; facade delegation with fake lifecycle)
- IT07-11 tests/integration/harness/test_memory_purge_it.py (deletion request, step 3b, real facade + LanceDB, rerun 0)
- ST07-21 tests/security/test_st07_purge.py (FTS MATCH, fts_candidates, fts integrity, full iterdump scan text+hex incl. FTS5 shadow tables, vector search + recall + vectors(), unrelated items byte-identical, rerun 0)
- Run: tests/unit/harness/memory + tests/unit/store/ops + IT07-10/IT07-11 + ST07 lifecycle/purge: 1348 passed (before the 2 extra coverage tests; those 30 purge tests re-run green). Coverage of purge code 100% of new lines; lifecycle 98%/_memory_rows 99% over that set.
- Gates: ruff check/format clean repo-wide, mypy clean (herness/harness/memory, herness/store/ops, new tests), lint-imports 15 kept/0 broken, check_module_size 0, check_type_ownership 0.

## Deviations / readings (recorded in the T07-26 spec note)
1. ST07-21 found a real TH07-21 gap: FTS5 keeps a deleted row's tokens in older index segments (memory_fts_data blobs) after the delete trigger. `purge_apply` now runs `INSERT INTO memory_fts(memory_fts) VALUES('optimize')` in the same transaction when anything was deleted (cost O(index); erasure is rare).
2. (superseded by the controller finding below) the former raw/JSON-escaped substring match is gone.
3. `scrubbed_ids` = only items whose provenance_history actually lost an entry.
4. Lifecycle re-selects inside its transaction and blanks/rejects review items of that selection (covers items citing the record after the dry run); late rows' vectors deleted after commit, failure -> `memory.vector.sync_failed` WARNING (maintenance step 5 removes orphans). Returned count/log use the in-transaction result (equal to len(sel.deleted_ids) without concurrency).
5. `NotFoundError` from update_review_payload/decide_review_item ignored like ReviewItemConflict (a dangling review link must not block erasure forever).
6. Shared validator `lifecycle.purge_args_ok` (public, §2 row updated); each unit keeps its own message.
7. Facade method lives in `_facade.py` (T07-23 layout), not `__init__.py` as the card's Files row says.

## What impl 10's deletion step must call
`get_memory_store().purge(record_id)` (or `MemoryStore.purge(record_id, now=...)`) as step 3b; it returns the number of memory items removed (record it in the step counts). `ModelUnavailable` = vector store down: leave the request open and retry the step (idempotent; rerun returns 0 once done). `ToolInputError` = record_id off `^[a-z_]+:[a-z_]+:.+$` or > 300 chars. CLI `herness memory purge --author-ref` calls `purge(author_ref=...)`; callers audit, the facade does not.

## Carry-overs / concerns for ruling
- Review payloads keep fields other than `content`: derived review items (weight_change/mapping_suggestion) hold `statement` = the memory content; memory_write payloads hold `provenance` (may cite the record). Spec says blank `content` only; followed verbatim. Suggest blanking `statement` too (ruling needed).
- Freed SQLite pages keep deleted bytes unless `PRAGMA secure_delete` is on (impl 02 connection setup, core.py never edited here).
- lifecycle.py is at 340/340: the next lifecycle change needs a sibling or a budget ruling.
- `approved_by`/`rejected_by` of the person on other items are kept (audit facts), per spec scrub definition.

## Controller finding (w31-s07, on 7150dc1) and closure
Finding (CRITICAL): the record selector `instr(content|data|provenance, ?)` was a raw substring match: prefix collisions ("…:INC001" deleted items citing "…:INC0012"), free-text mentions and ids inside unrelated JSON fields were deleted as if they cited the record; scrub used `instr(data, ?)` too.
Closure:
- Record selection is now structured-citation equality only: `data.entities[*].id = record_id` OR a value of `data.numbers[*].row_key` = record_id (json_each with object guards). Author delete set unchanged (`provenance.author_ref` equality); scrub candidates = `json_each(data,'$.provenance_history')` entries whose `author_ref` equals the person (the Python filter removes exactly those entries).
- Spec mirrored in docs/impl/07-memory.impl.md in the same commit: U07-101 Postconditions/Algorithm steps 1-2 rewritten, and a dated "Spec note (T07-26, controller ruling w31-s07, 2026-10-03)" inside the T07-26 spec note.
- Tests: UT07-89 `test_ut07_89_prefix_sharing_and_mentions_are_not_citations` (INC001 vs INC0012 via entities and number row_key, free-text mention, id in another data field, id in provenance: all kept with FTS rows); author scrub keeps an entry naming the person under another key (`on_behalf_of`) and an `approved_by`-only item; UT07-38 first test keeps a content-mention item and its vector; ST07-21 keeps a prefix-record item, a mention-only item and an unrelated item with identical rows, vectors and FTS rows while the purged plant is gone from FTS/SQLite dump/vectors.
- Red-then-green: with the record predicate temporarily reverted to `instr(content, ?) > 0 OR instr(data, ?) > 0` (not committed) 3 tests failed (UT07-89 prefix/mention, UT07-38 vector failure/retry, ST07-21); restored: 33/33 purge tests green. The author-scrub widening is behaviour-neutral (the per-entry Python filter already requires equality on `author_ref`), so no test can turn red on that predicate alone.
- Consequence for callers: an item is erasable with a record only if it cites the record as an entity (or a number row key). Items that merely mention the id in text are kept (ruling). Impl 10 / writers of record-derived memories should cite records in `data.entities`.
- Touched-package run after the fix: tests/unit/harness/memory + tests/unit/store/ops + IT07-10/IT07-11 + ST07 lifecycle/purge: 1350 passed. Gates: ruff/format clean, mypy 0 (382 files), lint-imports 15/0, module size 0, type ownership 0.
- Additional concern: memory_write review payloads keep `entities` (so the record id itself) after blanking `content`; only `content` is blanked per spec.

## Final
Final commit 13b1a2b feat(memory): T07-26 privacy purge entry point (R-54); all pre-commit hooks passed (incl. pytest-unit, detect-secrets). Worktree clean.

## Fix round 1 (review T07-26-review.md: 0C/3I/7m) - controller rulings applied
- I#1 FIXED: new private sibling `herness/harness/memory/_purge_review.py` (38/60, new §2 row): `erase_review_item` overwrites `content`/`statement` ("") and `entities`/`numbers` ([]) and `provenance` ({}) through `update_review_payload` (merge semantics: absent keys are added blank; memory_id/source_memory_id/kind/flags remain as skeleton), then rejects `system`/`purged`; NotFoundError and ReviewItemConflict skipped. lifecycle.py now 333/340. U07-57 step 3 (a) amended in the spec. Tests: UT07-38 `review_payloads_keep_no_purged_data` (memory_write payload with entities + numbers by key + provenance, derived weight_change item with statement), `author_purge_leaves_no_author_in_reviews`; ST07-21 adds an approved business_rule with weight_change (derived review item) and scans every review_item payload for the text and the quoted record id, plus the whole dump for the quoted id; IT07-11 asserts entities/provenance blank and no id/text in the payload.
- I#2 FIXED: number citations follow R-77 (impl 06 findings `_cites`): a string at depth <= 4 inside a `data.numbers` element equal to the record id or its key. Duplicated (not imported) with an R-77 comment. SQL json_tree finds candidates; Python applies the depth rule. Purge steps moved from `_memory_rows.py` (now byte-identical to base) to new private sibling `herness/store/ops/_memory_purge.py` (147/160, §2 row; pyproject ops-areas-acyclic ignore entries `memory -> _memory_purge`, `_memory_purge -> _memory_rows`). memory.py 387/390. Test UT07-89 `number_cites_by_record_id_or_key_like_r77`: key-only `{"key": "INC001"}`, full id, depth-4 nested value erased; prefix key INC0012, other source full id `jira:issue:INC001`, depth-5 value and numeric value kept.
- I#3: no code change; spec note sentence + carry-over to T07-15/T07-16 writers (add data.entities).
- Minor #4: `test_ut07_38_dangling_review_link_does_not_block` (kills M20/M24). #5: duplicate kept, reason in spec note (L4 cannot import the private L1 sibling; exporting from memory.py would grow the public 07 ops block at 387/390). #7/#8/#9/#10: recorded in the spec note (9e-9h).
- Red-then-green: PURGED_FIELDS reduced to ("content",) -> 3 red (UT07-38 x2, ST07-21); key target removed from R-77 targets -> 2 red (UT07-89 R-77, UT07-38 payload); restored -> 37/37 green.
- Gates: ruff/format clean, mypy 384 files 0 errors, lint-imports 15/0, check_module_size 0, type ownership 0, detect-secrets clean. Touched packages + repo import-contract test: 1355 passed. Coverage: _purge_review 100%, lifecycle 100%, _memory_purge 97% (one defensive branch).
