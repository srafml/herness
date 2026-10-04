# Review: T03-37 Review-item helpers

Worktree: D:\herness\.claude\worktrees\agent-a2fa711044f49450f
Base: cb9aa95 - Final commit: 0ebebd7 (checkpoint 1b37f33)

## Spec compliance by unit/test

- U03-147 iter_review_items -- OK. offset=0; loop calls list_review_items(kind=, status=, payload_match=, limit=page_size, offset=); yield from page; stops when len(page) < page_size, else offset += page_size (herness/enrich/review_items.py:39-68). Matches the algorithm verbatim. 1 <= page_size <= 5000 precondition enforced before any store call (line 53-55; 5000 matches herness/store/ops/shared.py:43 _MAX_LIMIT).
- UT03-136 -- OK. Verified by direct run: PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_review_items.py -q -p no:logging --cov=herness.enrich.review_items --cov-branch -> 12 passed, 100% line / 100% branch on review_items.py (target >=90%/>=85%). 1,203 pending + 5 approved items seeded with distinct created_at; test spies list_review_items and asserts exactly 3 calls and exact item-ID order (test_review_items.py:205-225). page_size precondition test (0 and 5001) present (line 228). SQL-grep acceptance test present and passing (line 236-244, regex over all of herness/enrich for FROM|INTO|UPDATE review_item).
- U03-148 create_if_absent -- OK. seen set of match tuples; per payload in order, key = tuple(payload.get(k) for k in match_keys); duplicate -> suppressed + continue; else call create_review_item_if_absent(kind, payload, match_keys=match_keys+tuple(scope), blocking_statuses=blocking_statuses, now=now), add to seen, count created/suppressed (review_items.py:120-139) -- matches the algorithm exactly, including combined_keys = match_keys + tuple(scope_map) (line 117) forwarded verbatim to impl 02. blocking_statuses forwarded unchanged (line 131). Preconditions (non-empty match_keys/blocking_statuses, <=8 combined keys via set(match_keys) | set(scope), every payload has every match key present -- None allowed since check is "k in payload" not truthiness -- and every payload agrees with scope) all raise ConfigError before any store call (lines 71-93, called at line 116 before the loop). No payload value appears in any of the five raise messages (checked by reading each msg=... line; also asserted by test_ut03_137_missing_match_key_field, line 334). Metric: only the accepted "# T08-05:" marker at the created branch (line 136) -- per controller ruling, not flagged.
- Scope enforcement (K3 case) -- OK, verified directly against the real store. K3 pre-exists pending under qsv B; the call (scope qsv A) does not suppress it because combined key (K3, B) != (K3, A), so a new K3 item is created under scope A. Test asserts (created, suppressed) == (2, 3) on the first run and the post-run pending set {K1, K3, K4} (test_review_items.py:262-278), and re-verified by inspection of create_review_item_if_absent's MATCH_LOOKUP (herness/store/ops/shared.py:325-349) which keys strictly on the combined match tuple.
- None match value -- OK. Builder traced impl 02's MATCH_LOOKUP use of SQL IS/IS NOT (null-safe) and added test_ut03_137_none_match_value exercising it against the real store (create then rerun, suppressed second time) -- passes. No store-side gap found; correctly reported as a concern per the controller ruling rather than silently accepted.
- UT03-137 -- OK. Exact brief scenario (K1 pending qsv A / K2 rejected qsv A / K3 pending qsv B; payloads K1,K2,K3,K4,K4, scope qsv A) run twice: first (2 created, 3 suppressed) with pending set {K1,K3,K4} all status == "pending"; second run (0, 5). Precondition tests present for empty match_keys, empty blocking_statuses, >8 keys, missing match field (with no-value-leak assertion), scope mismatch, and the None match case. Ran directly -- all pass, coverage 100%/100%.
- U03-149 open_label_counts -- OK. For each purpose, iterates iter_review_items("label_check", "pending", payload_match={"question_set_version": qsv, "purpose": purpose}) and counts by payload["question"] (review_items.py:142-154) -- matches algorithm exactly; "absent when zero" behaviour is the natural dict-only-on-hit result.
- UT03-138 -- OK. Exact brief scenario (3x spot_check q1 qsv A, 1x gold q1, 2x spot_check q2 qsv B) -> {"q1": 3}; plus an absent-default test. Both pass.
- Acceptance check "no SQL against review_item under herness/enrich/" -- OK, enforced by a real grep-based test (not just an assertion in prose), and independently confirmed by reading review_items.py: no SQL, only calls into herness.store.ops.
- TH03-03 (no ticket text/payload values in logs or errors) -- OK. No logging calls in the module; every ConfigError message is a fixed string naming no key, no value, no payload content. Verified by reading all five raise sites and by the missing-field test's explicit non-leak assertion.
- TH03-10 (items never approved here) -- OK. create_if_absent only calls create_review_item_if_absent, which only ever inserts via create_review_item (impl 02) at pending status; no path in this module sets any other status. Test asserts status == "pending" for all created items.
- Module budget -- OK, 146/160 lines (controller-accepted budget).
- Test IDs/docstrings/pytestmark -- OK. All 12 test functions carry their UT ID in the name and as the first token of the docstring; module sets pytestmark = pytest.mark.unit.
- Coverage -- OK, 100% line / 100% branch on herness/enrich/review_items.py (target >=90%/>=85%), confirmed by direct run, not just the report's claim.
- Ruff / mypy -- OK, confirmed directly: ruff check and ruff format --check clean on both touched files; mypy reports 0 issues on both files.

## Cannot verify / open items

- None outstanding beyond what the controller has already ruled on (metric marker, ops_store fixture name, budget).

## Findings

### Critical (Must Fix)
None.

### Important (Should Fix)
None.

### Minor (Nice to Have)
- open_label_counts (herness/enrich/review_items.py:152) accesses item.payload["question"] directly; a malformed label_check payload lacking "question" would raise an uncaught KeyError rather than one of the documented error types (U03-149's Errors row says "as U03-147", i.e. StoreBusy/ConfigError). U03-149 gives no precondition for malformed payloads, and the builder correctly flagged this as a documented Concern (report.md) resting on the invariant that only create_if_absent/decide_review_item write label_check payloads and always include question. Not a defect against this card's spec since no other writer of label_check payloads exists yet in the tree; worth a note for whichever future card first writes a label_check payload that might omit question, but nothing to fix here.
- The builder's reported "Interfaces section" deviation (defining a private type _Kind = Literal["label_check","mapping_suggestion"] instead of importing the wider herness.store.ops.ReviewKind) does not conflict with anything in the binding unit specs -- U03-147/U03-148's own Signature rows specify exactly this narrower Literal, not ReviewKind. No action needed; noting only because the report calls it a deviation.

## Assessment

All three units (U03-147-U03-149) match their algorithms verbatim, including the paging stop condition, dedupe-by-match-tuple, scope enforcement (K3 case), None match-value handling, and per-payload counting. All three test rows (UT03-136-UT03-138) are present, exercise the brief's exact scenarios against the real migrated store (not fakes), and were independently re-run: 12 passed, 100% line / 100% branch coverage. ruff check, ruff format --check, and mypy are clean on both touched files, confirmed by direct execution. TH03-03 (no payload text/values in errors or logs) and TH03-10 (items only ever created pending) are both satisfied and test-verified. No SQL against review_item exists under herness/enrich/, enforced by an executable test. Module is 146/160 lines, within the controller-accepted budget.

Verdict: Approved
