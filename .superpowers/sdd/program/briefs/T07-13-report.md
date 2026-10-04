# T07-13 report — Deterministic compaction steps

Status: DONE_WITH_CONCERNS (all gates green; concerns are hand-offs to T07-14 plus one size-forced sibling module)
Worktree: D:\herness\.claude\worktrees\agent-abe1e4ade320b58eb (branch worktree-agent-abe1e4ade320b58eb, base 667fcf4)
Commits: 5ff94e1 wip(T07-13): compact_build steps and unit tests; d495fac feat(memory): T07-13 deterministic compaction steps

## Files (lines / budget)
- herness/harness/memory/compact_build.py — 379 / 390. Public: Group, ParsedTable, split_groups, entry_from_result, cited_from_group, validate_notes, deterministic_notes, build_compacted, QUERY_ID_SCAN_RE (re-export).
- herness/harness/memory/_compact_text.py — 172 / 200 (NEW, size-forced private sibling; row added to docs/impl/07-memory.impl.md §2 module map after compact_build, following the _openai_map.py precedent). Holds parse_numeral, rounds_to, cut_raw, NotesRepair, arg_refs, canonical_args, repair_notes (U07-73 body). The first draft of compact_build was 514 lines; it could not fit 390 as one module.
- tests/unit/harness/memory/test_memory_compact_build.py — UT07-53..UT07-59 (23 tests)
- tests/unit/harness/memory/test_memory_compact_build_props.py — PT07-01 (pure part, 2 properties), PT07-02 (hypothesis, 60 examples each)
- tests/bench/test_memory_compact_build_bench.py — BT07-04 [integration, slow]: 100 messages, 30 dropped groups with 50x4 tables; best of 7 ~23 ms (< 50 ms). The first version took 105 ms; a float pre-filter before the exact Decimal half-even test fixed it.
- docs/impl/07-memory.impl.md — the module-map row only.
Not touched: working.py, render.py, store.py, memory/__init__.py, pyproject.toml (no contract change needed; lint-imports 13 kept).

## Units: U07-70..U07-75 done. No # T07-13 markers existed in the tree (the only grep hits are UT07-13 test ids).

## Evidence
- RED: `pytest tests/unit/harness/memory/test_memory_compact_build.py` -> collection error (module missing).
- GREEN: card tests 26 passed; `pytest tests/unit/harness/memory -q -p no:logging` 210 passed; bench 1 passed.
- Coverage (card tests): compact_build.py 100% line / 100% branch; _compact_text.py 100% / 100%.
- ruff format --check, ruff check: clean. mypy (memory pkg + the 3 test files): clean. lint-imports: 13 kept, 0 broken. check_module_size: exit 0. check_type_ownership: exit 0.
- Purity: no I/O, logger, clock, random or LLM anywhere in either module. PT07-02 checks that inputs are deep-equal before and after, that two runs are byte-identical, and that no output message, parts list or part is an input object.

## Spec readings / deviations (small)
1. U07-70 "a result whose call is in an earlier group is moved with that group": moving one message would break the postcondition that indices are contiguous and in order. So the groups from the call to the result are MERGED instead (either direction between tool groups). The preamble is never merged, so a preamble result whose call comes later stays there. The merged group takes its first member's step; later groups keep step = first_step + ordinal of their tool-call message. With duplicate tool_call_ids, the first call wins.
2. U07-71 rows: parsing stops at an empty line, at a line whose cell count differs from the column count, or after `shown=` rows when the header has it. The spec said "a line without ` | `", which would drop every one-column table (e.g. get_metric `value`), and the design 07 §5.5 example shows such a table with a sample. The header digits are bounded (\d{1,40}) so int() cannot fail. Ledger columns are capped at 50 (the LedgerEntry limit); sql_head is whitespace-collapsed, stripped and cut to 200. The error text, sql_head and table details go on the primary entry only.
3. U07-72: cell matching uses Decimal(repr(float(cell))) quantized half-even (exact decimal semantics). A cell NumberRef cannot hold (for example a column name over 128 chars) is skipped and the search goes on. Unmatched values are cut to 40 chars (the UnmatchedNumeral limit). Non-ASCII numerals and hits over 80 chars (the scanner cut) stay unmatched.
4. U07-73: besides the spec rules (unknown markers, stray numerals, hypothesis query_ids), malformed double-bracket tokens and query_ids in free text that are not in the ledger also become [[?]]; this enforces the postcondition "only ledger query_ids". The model's `steps` is dropped before validation, so a bad `steps` value does not cause a None. When [[?]] growth pushes a string past its limit, it is cut and re-repaired until it fits, so a cut can never expose a numeral.
5. U07-74: `rows=?` when a result has no spec 05 header or the call has no result. Canonical JSON falls back to json.dumps for non-finite floats (canonical_json raises on them), which keeps the function error-free.
6. U07-75: text longer than the 200,000-char TextPart limit (scratchpad or Claude transcript) is split across several TextParts of the same user message. Local profile: a kept user message that directly follows a user message is merged into it, so roles alternate (in practice this only happens when the kept list starts with a preamble). Claude transcript: user TextParts become `user: <text>` and orphan results become `result:\n<content>` (the spec is silent on both). Otherwise nothing would be lost.

## Concerns / hand-offs to T07-14 (U07-76)
- After the first compaction, state.messages[0] is the MERGED head (M0 parts + scratchpad (+ transcript)). If U07-76 passes that as m0, old scratchpads nest. It must keep or restore the original M0 parts (the PT07-01 driver passes the original M0).
- Claude profile: kept groups become transcript text in the head, so they never reach the ledger unless U07-76 ledgers them too (the PT07-01 driver ledgers ALL groups in fresh mode). Otherwise their query_ids can be lost at the next compaction when the head is rebuilt.
- Orphan tool results (no call in the group) have no ToolCall for entry_from_result; the PT07-01 driver uses a placeholder call named "unknown" so their query_ids are ledgered. U07-76 should do the same.
- Test files are longer than 400 lines (543 for the unit file); check_module_size does not cover tests/.
