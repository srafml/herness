# T07-05 report — Write-policy checks

Status: DONE_WITH_CONCERNS (minor interpretation notes below)
Commit: 742873d feat(harness): add memory write-policy checks (T07-05)
Worktree/branch: .claude/worktrees/agent-a65d277c4bb1cf9a9 / worktree-agent-a65d277c4bb1cf9a9

## Built
`herness/harness/memory/policy.py` (342 lines, budget 360), pure, units U07-37..U07-43:
- U07-37 `normalize_content` (NFKC, ZERO_WIDTH removal, casefold, whitespace collapse, strip), `content_hash`, `keyed_hash` (sha256_hex[:32] via herness.core.ids, R-14); `ZERO_WIDTH` constant.
- U07-38 `find_uncited_numerals` = `list(herness.core.numbers.find_uncited(...))` (R-16), returns core `NumeralHit`.
- U07-39 `check_markers` -> frozen `MarkerReport(unknown, invalid, duplicate_ids, unused, ok)` over `parse_markers`.
- U07-40 `InjectionScanner(patterns)` with `scan` (NFKC + zero-width removal + whitespace collapse, IGNORECASE, sorted indices), `scan_payload` (iterative DFS over data string values, keys excluded, max 2,000 strings = `MAX_SCAN_STRINGS`), read-only `patterns` property.
- U07-41 `check_limits(kind, content, data, numbers, cfg: WriteConfig)` — order size.content, size.sql, data.depth (>8, iterative walk), size.data (canonical_json UTF-8 bytes), size.numbers, data.entities, data.required:<field>. Raises `PolicyViolation(msg, details={"rule": rule})`, message names rule + limit.
- U07-42 `decide_policy` + frozen `PolicyDecision(status, needs_review, expiry_days)`; matrix is a verbatim encoding of impl 07 §4.2; rules policy.not_allowed / policy.role (prefix match) / policy.via / provenance.required:<field>; instruction_like/conflict force pending_approval.
- U07-43 `agent_confidence`, `merge_confidence`; constants HUMAN_CONFIDENCE 0.9, SYSTEM_EPISODIC_CONFIDENCE 1.0, APPROVAL_FLOOR 0.8, MERGE_CAP 0.95.

## Tests
`tests/unit/harness/memory/test_memory_policy.py`, pytestmark unit, 50 tests: UT07-11..UT07-17 and PT07-08 (hypothesis, whitespace/zero-width/case-flip variants over ASCII words). UT07-16 iterates every (kind x author_type x via x role) = 539 combos against an independent copy of the §4.2 matrix, and also asserts chat/tool and agent paths are never `active` (TH07-23/TH07-02).
- RED: `uv run pytest tests/unit/harness/memory/test_memory_policy.py` -> ImportError: cannot import name 'policy' from 'herness.harness.memory'.
- GREEN: 50 passed; coverage of policy.py 100 % line, 100 % branch (188 stmts, 70 branches).

## Gates
ruff format --check: clean; ruff check: All checks passed; mypy: no issues (56 files); lint-imports: 10 kept 0 broken; check_type_ownership: exit 0; check_module_size: exit 0; `pytest -m "(unit or integration) and not slow" -q -p no:logging`: 1141 passed, 1 xfailed (pre-existing IT00-02 marker).

## Interpretations / concerns
1. Required data field JSON types (§4.2 lists names only): chose string/array/integer/number per the producers described in U07-81/U07-90 etc.; null also accepted for run_summary.question (U07-81 says "or null"), outcome_summary baseline/actual/delta/rel (nullable REAL columns), and the three spec-named nullable fields. `suggested_action` must be one of none|weight_change|mapping_suggestion. mapping's "one of team_id/jira_project/org_id" rule name is `data.required:team_id|jira_project|org_id`.
2. `size.content` is applied to every kind as the spec algorithm states, although MemoryItem allows sql_template/qa_pair content up to 8,000; procedural content is the first question (U07-90), so should be fine — flag for spec owner.
3. Required provenance names: `provenance.required:data.review_item_id` for mapping; insight's "finding_ids verified" is only checked for presence here (verification is U07-50 step 6c). sql_template/qa_pair require run_id + query_ids (§4.2 row).
4. Flags force pending_approval also for status `candidate` (literal reading of U07-42 step 6).
5. `check_limits`/`check_markers` parameter is named `numbers` per spec; the core module is imported as `core_numbers` to avoid shadowing.
6. Tooling note: an editing step turned `‌`-style escapes in the source into literal invisible characters; they were restored to escapes and verified (no raw zero-width/full-width chars in either file).

## Fix round 1
Commit: see `git log` — fix(harness): review fixes for memory write policy (T07-05)
1. `check_limits`: new `_data_bytes` helper wraps `ids.canonical_json`; `SchemaViolation` (NaN, Infinity, -Infinity) -> `PolicyViolation` rule `size.data` (spec names no better rule). New test `test_ut07_15_non_finite_data_is_size_data` (nan, inf, -inf, top-level and nested).
2. `check_markers`: duplicates via `collections.Counter`.
3. Test: `_human_without_author_ref(via)` helper replaces the `missing.get("author_ref", "") is None` branch.
4. UT07-14: indices looked up by pattern text through `_index(scanner, pattern)` (uses `InjectionScanner.patterns`), robust to reordering of config/injection_patterns.txt.
Gates: ruff check/format clean, mypy clean, lint-imports 10 kept, check_type_ownership 0, check_module_size 0 (policy.py 352/360 lines). Tests: 53 passed in test_memory_policy.py, policy.py 100 % line+branch; suite 1144 passed, 1 xfailed (pre-existing).
