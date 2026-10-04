# T06-06 report: ops findings area

Status: DONE_WITH_CONCERNS (06 block line count over the 50-line figure; spec notes below)
Commit: f3053da feat(store): ops findings area (T06-06)
Worktree: D:\herness\.claude\worktrees\agent-abd7a537665317d61

## What was built
The previous build agent (stopped by a usage limit) had staged a complete implementation. I checked it against the brief, the design 06 section 6.5 table and the rulings, found nothing to change, ran all gates and committed it unchanged.
- herness/store/ops/findings.py (293/300 lines): insert_finding (U06-41), transition_finding (U06-42), query_findings, get_findings, list_task_findings (U06-43), query_verified_findings_recent (U06-44), scrub_record_from_findings (U06-144). No evidence or chat functions. Imports only core from within ops (ops-areas-acyclic kept); types come from herness.core.types; model_dump(mode="json") is used with no exclude_defaults.
- herness/store/ops/__init__.py: a new "# 06 findings" import block and __all__ block after "# 06 runs", with "# isort: split" separators and one name per line. The file is 232/400 lines.
- tests/unit/store/ops/test_store_ops_findings.py (489 lines): UT06-27, UT06-28, UT06-95. Every test function has an ID, docstrings start with the ID, and pytestmark = unit.

## Rulings applied
- insert_finding uses `INSERT ... ON CONFLICT(finding_id) DO NOTHING` and returns rowcount == 1. Test test_ut06_27_insert_check_violation_raises checks that confidence=1.5 raises SchemaViolation ("ops constraint failed") through run_write and that no row is written.
- UT06-27 covers all 7 rows of the design 06 section 6.5 table:
  - proposed->proposed with a challenge appended
  - proposed->challenged
  - proposed->rejected
  - challenged->rejected
  - challenged->revised
  - proposed->verified
  - proposed->merged
  - (new)->proposed is covered by the insert test.
  Each transition asserts True and that only the expected columns changed. There is one illegal transition for each of the 6 statuses: each returns False and leaves the row byte-identical. A meta-test checks that every status has an illegal case.

## Deviations and spec notes
1. U06-41 says INSERT OR IGNORE. It was replaced by ON CONFLICT DO NOTHING per the controller ruling. The spec text should be updated.
2. U06-144 step 2 searches `numbers` with instr() using the JSON-escaped form of record_id and of the key (the text between the quotes). This matches values containing a quote or backslash the way they are stored. Matching is still exact after load_json, and for ordinary ids the result is identical. There is a test for it.
3. scrub_record_from_findings raises SchemaViolation("invalid record_id") when record_id is outside 1-300 characters. The spec gives the constraint but does not name an error.
4. In query_findings and query_verified_findings_recent the optional filters default to None. The spec lists them without defaults. This is compatible with every caller in the spec.
5. The DDL (003 finding) is looser than the Finding type. In the DDL, entity_type, entity_id, confidence and challenge are nullable, numbers defaults to '[]', and author_role has no CHECK. Finding requires entity_type, entity_id and confidence, and needs 1-20 numbers. A NULL challenge is read back as []. A row that violates Finding raises SchemaViolation("finding invalid: finding_id=<id>") (UT06-28). No DDL change was made.

## Line counts
- findings.py: 293 / 300.
- 06 block in ops/__init__.py: 56 lines (58 with the two blank separator lines):
  - runs import block: 20
  - findings import block: 11
  - runs __all__: 17
  - findings __all__: 8
  This is over the 50-line figure in the dispatch, and the brief's section 2 row says "20 (06 block)". Following the rule of one import block and one __all__ block per area, with one name per line, the minimum is 56. I did not restructure. tools.check_module_size passes (the file is 232/400). The controller needs to rule on the 06-block budget in the section 2 row.

## Tests and gates
- Card tests: 30 passed.
- Coverage of findings.py: 100% line, 100% branch (141 statements, 38 branches).
- ruff format and ruff check: clean. mypy: no issues. lint-imports: 13 contracts kept. check_type_ownership: 0. check_module_size: 0.
- Full run `-m "(unit or integration) and not slow"`: 3871 passed, 5 skipped (symlink or coverage-json), 1 xfailed, 0 failed.
- Pre-commit hooks all passed on commit (no --no-verify; PRE_COMMIT_ALLOW_NO_CONFIG not set). No new detect-secrets baseline entries were needed: the ULID alphabet constant carries a pragma allowlist.

## Concerns
- The 06 block is 56 lines against a budget of 50 (or 20 in the brief). This needs a ruling in the spec section 2 row.
- IT06-16 (concurrent compare-and-set) belongs to a later card and is not in this card.

## Fix round 1

Commits: 7ae3026 `fix(store): skip fully scrubbed findings on reads, reject empty keys (T06-06)`; 3f8d482 `docs(impl): raise the 06 ops re-export block budget to 60 lines (T06-06)`.

- Important-1, ruling (a): all four finding reads (query_findings, get_findings, list_task_findings, query_verified_findings_recent) exclude rows with an empty `numbers` list in SQL (`_KEPT = (json_type(f.numbers) <> 'array' OR json_array_length(f.numbers) > 0)`), so LIMIT counts stay correct. The json_type guard keeps non-list `numbers` rows visible, so they still raise `finding invalid` (UT06-28 invalid-row test unchanged). `_SELECT` now aliases `finding AS f`; query_findings reuses it. transition_finding and the scrub are unchanged. New test test_ut06_95_reads_skip_fully_scrubbed_rows: after a full scrub every read works, the emptied row is absent and the other row is returned (LIMIT 1 checks the count).
- Minor-1: scrub_record_from_findings rejects a record_id whose key part is empty (`"src:kind:"`, `"::"`) with the same `SchemaViolation("invalid record_id")`; the check now runs after the split (`not key or len > 300`; `""` is covered by the empty key). Test ids added to the edge-id loop (exact-match regex).
- Minor-2: test_ut06_95_scrub_matches_string_at_depth_four (string at exactly depth 4 is dropped); the depth-5 test is kept.
- Minor-3: ValueError for out-of-range limit/max_runs kept as ruled.
- Docs: §2 module-map row for `herness/store/ops/__init__.py` (06 block) budget 50 → 60; nothing else changed. Minor-4 spec notes (deviations, ON CONFLICT, coalesce) were not part of this round's instructions and are not done.

Checks: ruff format/check clean, mypy clean, lint-imports 13 kept, check_module_size exit 0; findings.py 295 lines; card file 32 passed, findings.py coverage 100% line/branch; full `(unit or integration) and not slow`: 3900 passed, 5 skipped, 1 xfailed. Pre-commit hooks passed on both commits.
