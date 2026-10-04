# T07-26 review (verify agent, w31-s07) - Privacy purge entry point (R-54)

Worktree agent-a332fb55e4127b2f0, base 10f9733, head 13b1a2b. Reviewed: brief, impl 07 U07-57/U07-100/U07-101 + T07-26 spec note, builder report, diff, global constraints, reviewer rules. Probes ran in the worktree with temporary files only. All of them were removed, and the working tree is clean at the end.

**Verdict: Needs fixes** (0 Critical, 3 Important, 7 Minor). CF1 is CLOSED. The three Important items are erasure-completeness gaps that need controller rulings. Only #1 needs a code change, and it is small.

## CF1 closure status: CLOSED

- **Selector.** In `herness/store/ops/_memory_rows.py:306-312` a record is selected by `data.entities[*].id = ?` OR any value of `data.numbers[*].row_key = ?`, both through `json_each` with object guards. The scrub (`:314-318`) uses `provenance_history[*].author_ref = ?`, and the Python filter `_without` requires the same equality. No `instr` or substring match is left anywhere in purge.
- **Spec mirrored.** U07-101 Postconditions and Algorithm steps 1-2 are rewritten, and a dated controller-ruling note sits inside the T07-26 spec note (docs/impl/07-memory.impl.md:965, 984, 986).
- **Writers grep (CF1 check 1).** These are all the writers of `memory_item`:
  - write.py (propose: `entities`, `numbers`)
  - tools.py:167 (`entities` from the agent)
  - chat.py:219 (`entities` from the correction classifier)
  - recommend.py:270 (run_summary)
  - episodic.py:278 (decision_note)
  - outcome.py:213 (outcome_summary)
  - procedural.py (sql_template, qa_pair)

  `Provenance` (core/types/memory.py:70) has no source_ids, record_ids or evidence-ref field; it holds only query_ids and finding_ids. No data key named `record_ids` or `source_ids` exists. So no writer stores a record citation in a structured field other than `entities`/`numbers`. Two related gaps (content-only target labels and key-only row keys) are Important #2 and #3 below.
- **Tests (CF1 check 2).**
  - (a) The INC001 vs INC0012 prefix case is covered: UT07-89 `prefix_sharing_and_mentions_are_not_citations`, and ST07-21 keeps a prefix item with identical rows, vectors and FTS.
  - (b) An item citing B whose content mentions A survives (UT07-89 and the first UT07-38 test).
  - (c) Items citing A are removed from items, vectors and FTS (UT07-38, IT07-11, ST07-21).
  - An independent verifier probe on the real facade with LanceDB and FTS5 confirmed (a), (b) and (c). `purge("src:incident:INC001")` removed exactly 2. INC0012's entity item and the mention-only item kept their rows, vectors and FTS. A rerun returned 0, and an unknown id returned 0.
- **Mutation (CF1 check 3).** Reverting the predicate to `instr(content) OR instr(data)` was KILLED (3 red: UT07-38 vector failure/retry, UT07-89 prefix/mention, ST07-21). `instr(data)` alone was KILLED (2 red), and `instr(content)` alone was KILLED (9 red).

## Spec compliance

| Item | Status | Notes |
|---|---|---|
| U07-101 `purge_rows` | ✅ | Defined in memory.py:378-389; the steps live in `_memory_rows.py:295-386`. Message is verbatim `purge_rows needs exactly one selector`. Dry run and real run return the same ids. Deletes run in chunks of 500 (mutation to 499 was killed). Review ids include `derived_review_item_id` (mutation killed). Exported in the 07 memory block of `herness.store.ops` (UT02-68 green). Selector follows the controller ruling (spec note). |
| U07-57 `MemoryLifecycle.purge` | ✅ (see Important #1) | Order is vectors first, then one `run_write` (blank and reject `system`/`purged`, then `purge_rows(conn)`). `ReviewItemConflict` is ignored. A vector failure raises `ModelUnavailable("memory vector purge failed")` and logs `memory.purge.vector_failed` at ERROR (count only), with SQLite unchanged. `memory.purge.completed` carries counts only. Message is verbatim `purge needs exactly one of author_ref, record_id`. The deviations (in-transaction reselect, late-vector drop, NotFoundError ignored) are recorded in spec note (5). |
| U07-100 `MemoryStore.purge` | ✅ | `_facade.py:179-185`. Message is verbatim `purge needs exactly one of record_id, author_ref`. `record_id` is positional-or-keyword and the other two are keyword-only. No audit line. It lives in `_facade.py`, not `__init__.py` (T07-23 layout: §2 row 99 and T07-23 note (12)), and is reached through the lazy re-export. |
| UT07-38 | ✅ | `tests/unit/harness/memory/test_memory_purge.py`: fail-once vector fake, unchanged snapshot, retry, history scrub, decided item blanked but not redecided, unknown id returns 0, selector errors |
| UT07-88 | ✅ | Delegation through a fake lifecycle; both and neither raise ToolInputError |
| UT07-89 | ✅ | `tests/unit/store/ops/test_store_ops_memory_purge.py` |
| IT07-11 | ✅ | `tests/integration/harness/test_memory_purge_it.py`: deletion request, step 3b, real LanceDB, rerun returns 0 |
| ST07-21 | ✅ (seed gap, see Important #1) | FTS MATCH, FTS integrity, full iterdump scan as text and hex including the FTS5 shadow tables, vectors, recall; unrelated rows byte-identical |
| TH07-21 | ⚠️ | "blanks review payloads": only `content` is blanked (Important #1) |

**Gates (re-run by the verifier):**
- Card tests: 33 passed.
- Touched packages (tests/unit/store/ops, tests/unit/harness/memory, IT07-11, ST07-21): 1345 passed, no warnings in the summary.
- `mypy --strict herness/harness/memory herness/store/ops`: no issues in 55 files.
- `lint-imports`: 15 kept, 0 broken (ops-areas-acyclic KEPT).
- `tools.check_module_size`: rc 0.
- ruff: clean.
- `--require-test-ids`: collection OK, and every function carries a `test_ut07_38_`, `ut07_88`, `ut07_89`, `it07_11` or `st07_21` ID.
- core.py, the migrations and pyproject are untouched; no migration was added.
- The ops `__all__` 07 memory block gains `PurgeRows` and `purge_rows` inside the block (UT02-68 green).

⚠️ Not verifiable here: how the FTS `optimize` performs on a large production index (see Minor #7), and impl 10's real call site, which is not built yet.

## Findings

### Critical
None.

### Important

1. **Review payloads keep purged data, contrary to TH07-21 and ST07-21.** This is plan-mandated by U07-57 step 3a.
   - **Where.** `herness/harness/memory/lifecycle.py:300` merges `{"content": ""}` only (`update_review_payload` merges keys; shared.py:386).
   - **Verifier probe (derived item).** An approved `user_correction` with `suggested_action=weight_change` creates a derived review item. After `purge(record_id)`, that item is `rejected/purged`, but its payload still holds:
     - `statement`: the full purged text, verbatim
     - `entities`: the record id
     - a new `content: ""` key that the derived payload never had
   - **`memory_write` payloads** (`_write_steps.py:243`) keep `numbers` (values derived from the record), `entities` (the record id) and `provenance` (the erased person's `author_ref` on an author purge).
   - **Why it matters.** TH07-21's mitigation is "blanks review payloads". ST07-21 expects a "SQLite scan for the record's text" to find nothing; it passes only because its seed has no derived item. On an author purge, the erased person's own correction text stays in `review_item`.
   - **Fix (needs a ruling).** Blank `statement` as well. Better: replace a purged item's payload with a skeleton (`memory_id`/`source_memory_id`, `kind`, `flags`). Add a derived item to the ST07-21 seed.

2. **Number citations that use only the record's source key are not erased, unlike in the findings scrub (R-77).**
   - **Where.** In `herness/store/ops/_memory_rows.py:310-311`, a number cites a record only when a `row_key` value equals the full `record_id`.
   - **The sibling rule.** impl 06 `scrub_record_from_findings` (findings.py:259-273, U06-144, UT06-95 "one by key only") also treats a NumberRef as citing a record when a value equals the record's source key (the text after the second `:`). Memory stores the same NumberRef shape from the same warehouse queries (for example `core.work_item(record_id, key)`, where the agent picks the row_key column).
   - **Verifier probe.** An item with `row_key={"key": "INC001"}` survived `purge("src:incident:INC001")`, while an item with `row_key={"record_id": ...}` was purged. The record-derived number values and the text around them stay in SQLite, FTS and vectors.
   - **Ruling needed.** Should "values of row_key" also match the source key? That accepts the same cross-source key collision risk impl 06 accepted. Without it, impl 10's erasure removes less from memory than from findings.

3. **System-written items name work_item records only in their text, so purge never removes them.**
   - **Where.** `decision_note` (episodic.py:277-278) has content `"Recommendation rec_… (fund for work_item:<record_id>) …"` and data `{rec_id, decision}`. `run_summary` (recommend.py:247-272) lists `target_type:target_id` labels in its content and has no `entities`.
   - **Why it is missed.** In both, the target `record_id` (impl 06: `work_item → core.work_item(record_id, key)`) appears only in `content`, and the allowed numeral pattern `[A-Z][A-Z0-9]+-\d+` lets the label through. Under the ruling these are mentions, not citations, so they stay after `purge(record_id)`.
   - **Why Important, not Critical.** Only the id remains, and spec 00 §1823 says a record id is never personal by construction.
   - **Fix (cross-card, needs a ruling).** Either the T07-15/T07-16 writers add `data.entities=[{"type": target_type, "id": target_id}]`, or the spec note states that recommendation-target labels are outside R-54 scope.

### Minor

4. The `NotFoundError` suppression (lifecycle.py:299 and :301; deviation 5) has no test. Mutations removing either one survived (M20, M24).
5. The record-id rule is duplicated: `lifecycle.py:44-45` (`_RECORD_ID_RE`, `_RECORD_ID_MAX`) repeats `_memory_rows.py:297-298`. lifecycle already imports `herness.store.ops.memory`, so it can reuse the L1 constant (only L1 importing L4 is blocked).
6. Budgets were met by packing code under `# fmt: skip`: lifecycle.py is at 340/340 and memory.py at 389/390 (memory.py:84 `__all__` and :383; lifecycle.py:277-310). The next lifecycle change needs a sibling module or a budget ruling.
7. The FTS `optimize` (`_memory_rows.py:321, 383`) is correct for TH07-21. FTS5 external-content deletes leave tokens in older segments until a merge; the ST07-21 hex scan of the shadow tables shows this is needed, and removing the call (M2) is killed by ST07-21. Its cost is O(whole memory_fts index) inside the write transaction, which holds the SQLite write lock. That is acceptable for a rare erasure run as a job at today's scale, but on a large index it can surface as `StoreBusy` for other writers; worth recording in the spec note. It is skipped when nothing was deleted, which is correct because scrubs do not touch the FTS columns (content, kind).
8. `PRAGMA secure_delete` is off, so freed SQLite pages keep deleted bytes (builder carry-over). This belongs to impl 02's connection setup and is recorded for a ruling.
9. The T07-26 spec note does not record the Files-row deviation (facade in `_facade.py`, not `__init__.py`); only the builder report does. T07-23 note (12) and §2 row 99 cover the layout, so this only affects documentation.
10. An erased person's `approved_by`/`rejected_by` stay on other items, because the spec's scrub definition covers history only. This needs a ruling together with #1.

## Probe table

| # | Probe | Result |
|---|---|---|
| M1 | record predicate changed to `instr(content) OR instr(data)` | KILLED (3: UT07-38, UT07-89 prefix/mention, ST07-21) |
| M1b | record predicate changed to `instr(data)` only | KILLED (2) |
| M1c | record predicate changed to `instr(content)` only | KILLED (9) |
| M2 | drop the FTS `optimize` | KILLED (ST07-21) |
| M3 | drop `update_review_payload` | KILLED (5) |
| M4 | drop `decide_review_item` | KILLED (5) |
| M5 | stop ignoring `ReviewItemConflict` in purge | KILLED (UT07-38 decided item) |
| M6 | SQLite chunk 500 changed to 499 | KILLED (UT07-89 chunks) |
| M7 | drop the `memory.purge.vector_failed` log | KILLED |
| M8 | change the ModelUnavailable message | KILLED |
| M9 | rename `memory.purge.completed` | KILLED |
| M10-M12 | swap or alter each of the three ToolInputError texts | KILLED (each by its own unit's test) |
| M13 | skip the scrub UPDATE | KILLED (2) |
| M14 | skip the late-vector drop | KILLED |
| M15 | drop the `numbers.row_key` clause | KILLED (2) |
| M16 | delete vectors after the transaction instead of before | KILLED (2) |
| M17 | no in-transaction reselect of review ids | KILLED |
| M18 | author delete predicate widened to `instr(provenance)` | SURVIVED; equivalent by construction (no other provenance field can hold a 32-char lowercase hex value), so no finding |
| M19 | facade skips validation | KILLED (4) |
| M20 | NotFoundError not ignored when blanking the payload | SURVIVED (Minor #4) |
| M21 | vector chunk 200 changed to 199 | KILLED (ST07-21) |
| M22 | review ids drop `derived_review_item_id` | KILLED |
| M23 | return `len(sel)` instead of the in-transaction count | KILLED |
| M24 | NotFoundError not ignored on decide | SURVIVED (Minor #4) |
| P1 | CF1 on the real facade with LanceDB and FTS5: INC001 vs INC0012, mention-only item, unknown id, rerun | PASS: 2 removed, others intact, unknown id 0, rerun 0; the completed log has only count, scrubbed and review_items |
| P2 | real process kill (os._exit in a child) after the LanceDB delete, before the SQLite transaction | PASS: after the kill, SQLite and the review were unchanged (rows present, review pending, content intact) and the vectors were gone. The rerun returned 2 and removed everything; the review ended rejected/purged/blank. A second rerun returned 0. |
| P3 | real process kill inside the transaction (after decide, before commit) | PASS: the transaction rolled back (rows, review pending, payload intact), and the rerun completed as in P2 |
| P4 | key-only `row_key {"key": "INC001"}` vs `purge("src:incident:INC001")` | the key-only item SURVIVED; the full-id item was purged (Important #2) |
| P5 | derived review item after purge | `statement` keeps the full purged text; status is rejected/purged (Important #1) |

Note on the window between the vector delete and the SQLite commit (and after a kill there): the items exist without vectors, and a maintenance run in that window could re-embed them. The retried purge re-selects them and deletes those vectors again, so the store converges. This is consistent with the U07-57 invariant.

## Assessment
**Task quality:** Needs fixes

**Reasoning:** CF1 is closed, and the build matches U07-57/U07-100/U07-101 with strong, mutation-checked tests, including recovery from real process kills. But erasure still leaves record- or person-derived data in review payloads (the derived `statement`), and it misses number citations that use only the source key and work_item ids that appear only in item text. Each of these needs a controller ruling; the derived-payload fix itself is small.
