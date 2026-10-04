# T06-06 review (commit f3053da, base 6b70189)

**Verdict: Needs fixes** (one Important item, which needs a controller ruling because the spec leaves it open)

### Spec Compliance
- ✅ U06-41 insert_finding: `ON CONFLICT(finding_id) DO NOTHING` (as ruled). numbers, query_ids, challenge and verification are stored as JSON; returns `rowcount == 1`. A CHECK violation raises (test_ut06_27_insert_check_violation_raises).
- ✅ U06-42 transition_finding: compare-and-set `WHERE finding_id = ? AND status IN (...)`. Only status, challenge (append), verification and merged_into are set. The ConfigError rule `(to == "merged") != (merged_into is not None)` is correct in both directions. `json_insert(coalesce(challenge,'[]'), '$[#]', json(?))` fixes the spec's NULL-challenge gap (json_insert(NULL, ...) would return NULL) and is tested (test_ut06_27_unknown_finding_and_null_challenge). An empty allowed_from gives `IN ()`, which matches nothing, so the call returns False.
- ✅ U06-43: parameterised filters for every non-None argument, `ORDER BY created_at, finding_id`, `LIMIT ?`, limit 1–500, and `SchemaViolation("finding invalid: finding_id=<id>")` (exact-match test).
- ✅ U06-44: `kind IN ('funding_review','org_review') AND status='done' ORDER BY finished_at DESC [, run_id DESC] LIMIT max_runs`, then verified findings with the entity filters, ordered `r.finished_at DESC, r.run_id DESC, f.created_at, f.finding_id`. The test covers kind, status, max_runs, limit and order.
- ✅ U06-144 scrub_record_from_findings:
  - key = the text after the second `:` (whole id otherwise; split(":", 2) keeps colons inside the key).
  - Prefilter with instr on the JSON-escaped needles, then exact matching after load_json. Depth ≤ 4 inside each element.
  - Markers are found with parse_markers and replaced right to left. Status, query_ids, challenge and verification are untouched.
  - Idempotent. No logging. SchemaViolation is raised on bad or non-list JSON.
- ✅ ops/__init__.py 06 block: placed after `# 06 runs` and before `# 09 chat`, matching the U02-62 order (06 runs, findings; … 09 chat). `__all__` is in block order and UT02-68 passes (8 passed). The 56-line block count is covered by the controller ruling and is not a finding.
- ✅ Findings-only, as ruled (no evidence or chat functions).
- ✅ UT06-27: all 7 §6.5 rows (the (new)→proposed row via insert). There is one illegal transition per status (6/6); each asserts False and a byte-identical raw row, and a meta-test asserts the coverage.
- ✅ UT06-28: filters, order, limit bounds, get/list, invalid row, recent verified.
- ✅ UT06-95: one finding cited by record_id and one by key only, a non-cited finding, markers `[redacted]`, changed columns = {numbers, claim}, second call returns 0. There are extra tests for depth, invalid JSON and escaped ids.
- ⚠️ Cannot verify here:
  - IT06-16 (concurrent CAS) belongs to a later card.
  - The coverage figure (100%/100%) and the full-suite results are from the build report. I ran the card file myself: 30 passed.

### Strengths
- The SQL is fully parameterised. The only f-string parts are constant column lists, fixed column names from literal tuples, and `?` placeholder lists.
- Area isolation holds: the module imports only `herness.core.*` and `from . import core`.
- Error messages name only the finding_id and never row values. The invalid-record_id error does not echo the id.
- The tests compare whole raw rows before and after, so "row unchanged" and "only these columns changed" are real assertions.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
1. **A scrub can leave a finding that no read can load (spec gap, needs a ruling).**
   - Where: herness/store/ops/findings.py:285-291, together with `Finding.numbers: Field(min_length=1, max_length=20)` (herness/core/types/swarm/tasks.py:258).
   - What happens: when every element of a finding's `numbers` cites the deleted record, the scrub writes `numbers = '[]'`. From then on `_from_row` (findings.py:51-60) raises `SchemaViolation("finding invalid: ...")` for that row.
   - Effect: each call that includes that row fails as a whole, not only for the one row: query_findings for the run, get_findings, list_task_findings, and query_verified_findings_recent (chat context for that entity, for up to max_runs runs).
   - Test gap: test_ut06_95_scrub_key_only_and_edge_ids (test file :441-445) creates exactly this state (`numbers == "[]"`) but checks only the raw column, never a read.
   - Why a ruling is needed: U06-144 requires status to stay unchanged and says nothing about zero-number findings.
   - Possible fixes:
     - (a) the reads skip or exclude rows with `numbers = '[]'` (for example `AND json_array_length(numbers) > 0`), with a test;
     - (b) the scrub drops fully-scrubbed rows or marks them, via a spec change;
     - (c) Finding allows 0 numbers after a scrub.
   - Recommendation: (a), recorded as a spec note on U06-43/U06-44/U06-144, plus a UT06-95 assertion that the reads still work after a full scrub.

#### Minor (Nice to Have)
1. **An empty key matches too much.** herness/store/ops/findings.py:267-275.
   - Cause: for a record_id with an empty key (e.g. `"src:kind:"`), `instr(numbers, '')` is 1 for every row, and `""` becomes a target.
   - Effect: every numbers element that has any empty-string value at depth ≤ 4 is dropped.
   - The spec's `<source>:<kind>:<key>` implies a non-empty key. Suggest rejecting an empty key with the same `SchemaViolation("invalid record_id")`, or leaving `""` out of the targets.
2. **No positive depth-boundary test.** tests/unit/store/ops/test_store_ops_findings.py:451-461.
   - The existing test shows that depth 5 is not matched. No test shows that a string at exactly depth 4 is matched, so an off-by-one in `_cites` (findings.py:243) would go unnoticed. Add one element with a value at depth 4.
3. **Unrecorded choices for bad arguments.** findings.py:63-66, 219-221.
   - A limit outside 1–500 and `max_runs < 1` raise ValueError. The spec names no error, and sibling areas (chat.py:271, 283) clip instead.
   - Acceptable, but record it as a spec note next to the other deviations in the report, so callers (the chat tools) know these raise rather than clip.
4. **Deviations not yet in the spec.** The report lists deviations 2–4: JSON-escaped instr needles, the record_id length error, and None defaults on the filters. They should reach the spec in the fix-round docs commit, together with the ON CONFLICT ruling and the coalesce on the NULL challenge (findings.py:125), which differs from the spec's U06-42 algorithm text.

### Assessment
**Task quality:** Needs fixes
**Reasoning:** The implementation matches every unit and test row, and the CAS, SQL safety and isolation are correct. However, the specified scrub can produce rows that break every later finding read of that run and chat's recent-verified read. This needs a ruling and a small fix with a test before the card is trusted.

---

## Re-review round 1 (commits 7ae3026 code, 3f8d482 docs; range 5b6e60a..3f8d482)

**Verdict: Approved**

### Findings from the first review
- ✅ **Important-1 (ruling a): resolved.**
  - All four reads add `_KEPT = (json_type(f.numbers) <> 'array' OR json_array_length(f.numbers) > 0)` to the WHERE clause (findings.py `_KEPT`, query_findings, get_findings, list_task_findings, query_verified_findings_recent).
  - The filter sits in SQL before `LIMIT ?`, so an emptied row does not take up a LIMIT slot. test_ut06_95_reads_skip_fully_scrubbed_rows checks this with `limit=1` on both query_findings and query_verified_findings_recent.
  - The json_type guard is safe. `numbers` is `NOT NULL` with `CHECK json_valid`, so json_type cannot fail on malformed text. A non-array row (e.g. an object) stays visible and still raises `finding invalid`; the UT06-28 invalid-row test is unchanged and passes.
  - `_SELECT` now aliases `finding AS f`. The unqualified columns in get_findings and list_task_findings still resolve.
  - transition_finding and the scrub are unchanged apart from Minor-1.
- ✅ **Minor-1: resolved.**
  - The empty-key check now runs after the split: `not key or len(record_id) > 300`.
  - `""` gives key `""`, so it is rejected. `"src:kind:"` and `"::"` are rejected. The regex in the test is exact.
  - Ids without two colons still use the whole id as the key.
- ✅ **Minor-2: resolved.** test_ut06_95_scrub_matches_string_at_depth_four drops the element whose string sits at exactly depth 4 (element → a → b → c → "plain") and keeps the sibling. The depth-5 negative test is kept.
- ✅ **Minor-3:** ValueError is kept as ruled (spec note).
- ✅ **Minor-4:** recorded by the controller as spec notes; not an open item for this card.

### Scope checks
- **Docs:** the only docs change is the §2 row for `herness/store/ops/__init__.py` (06 block), 50 → 60. The 06 block is 56 lines, within the new budget. `ops/__init__.py` is not touched in this round.
- **Diff range:** 3 files. The herness/harness/findings.py changes belong to T06-07 and were ignored.
- **Size:** findings.py is 295/300 lines.
- **Tests:** the card file passes locally, 32 passed. The coverage and full-suite figures come from the build report.

### New issues
None.

**Task quality:** Approved
**Reasoning:** Every finding from the first review is resolved with a test. The read filter keeps LIMIT semantics and the non-list error path. No new defects were introduced, and the only docs change is the ruled budget row.
