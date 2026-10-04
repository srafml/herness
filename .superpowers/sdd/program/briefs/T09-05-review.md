# T09-05 Report contract — verify review

Reviewed commit a4450c7 (base cd44be5) in worktree agent-a525b32efd7ec3b1e. Read-only review.

### Spec Compliance
- ✅ U09-03 constants: all seven verbatim (contract.py:34-43); only RUN_ID_RE / QUERY_ID_RE compiled; RUN_ID_RE equality test with `herness.core.types.reports._RUN_ID_RE` present (ruling 1, test_contract.py:119).
- ✅ U09-04 iter_text_fields: order title → sections[i].paragraphs[j] → recommendations[k].headline/summary → caveats[c] → prior_outcomes_commentary (contract.py:88-106). Refs on summary only, in order expected_usd, expected_delta, confidence, effort (nulls skipped), then `action_levers[m].delta_usd_ref`; bad lever message "recommendations[k].action_levers[m] is missing <key>" (contract.py:77-85). UT09-05 asserts the exact list and refs.
- ✅ U09-06 load_draft: RUN_ID_RE first; symlink + resolve-and-contain (contract.py:128); missing → exact message + draft_missing; size cap (reads at most MAX+1 bytes, then reports st_size — stricter than stat-first, no TOCTOU); schema error `details.where` = dotted locs joined by ", ", "(root)" for JSON errors, `from None` (contract.py:116-142); run id mismatch with draft_run_id. Error codes per ruling 4.
- ✅ U09-07: ops ids via `ui_evidence_ids_present` (chunks of 500 internally, ui_reads.py:119-135); warehouse `list_contains($ids, query_id)` bound, chunked at 500 (contract.py:177-181); `duckdb.Error` → QueryError naming build_id (contract.py:182-184); ops errors (StoreBusy) not caught, so they propagate; results intersected with argument ids. `build_id` kw-only per ruling 3.
- ✅ U09-08: schema_version, malformed/unresolved markers via `parse_markers`, duplicate ids, unknown ref / wrong unit (usd for expected_usd_ref, effort_usd_ref, every delta_usd_ref), invalid + unknown query ids at first where, unknown rec_id, unverified findings; message "{n} report contract violations in run {run_id}"; details code/where/rules in first-seen order, 200 cap + "… and N more" (ruling 2); WARNING log `reports.contract.violated` with run_id/count/rules (contract.py:208-287).
- ✅ U09-09: pure delegation to `herness.core.numbers.find_uncited`, text cut to UNCITED_TEXT_MAX, offsets kept (contract.py:293-299). No own marker/numeral regex; UT09-93 AST test checks exactly two `re.compile` calls assigned to RUN_ID_RE/QUERY_ID_RE, no regex-like string constants, and the core.numbers imports.
- ✅ U09-10: top-level and one nested level, `is True`, sorted (contract.py:302-315).
- ✅ Layering: imports only core, store.ops, duckdb, pydantic; no `app/`. lint-imports 13 kept.
- ✅ Tests: every ID in the Tests row has ≥1 function with ID in name and docstring; PT09-01 checks all four clauses of the property (field paths, offsets in text, no hit inside an allowed match or marker, every outside digit covered).
- ⚠️ Cannot verify here: file-symlink refusal on a real symlink (test skips on this host without privilege; covered by monkeypatched is_symlink and a junction-based outside-root test that runs). IT09-03/IT09-05 are other cards' integration tests.

Gates re-run in the worktree: ruff check, ruff format --check, mypy, lint-imports (13 kept), check_type_ownership 0, check_module_size 0 (contract.py 315/320); card tests with --require-test-ids: 58 passed, 1 skipped; contract.py coverage 100 % line / 100 % branch.

### Strengths
- Tight, readable module under budget; violations collected rather than fail-fast; lookups skipped when nothing to look up.
- No model text echoed into details: malformed marker inner text and invalid query ids are not repeated; schema errors chained `from None`.
- Tests assert exact strings for where/rules/messages, lookup call arguments, and the log event.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/reports/contract.py:85 — `str(entry["delta_usd_ref"])` turns a present-but-null `delta_usd_ref` into the ref id "None", reported as "unknown ref" rather than "is missing delta_usd_ref". Only reachable if the draft type lets lever values be null; harmless but slightly misleading.
2. herness/reports/contract.py:279 — details carry an extra `run_id` key beyond the spec's `{code, where, rules}`. Benign (string, non-sensitive), but it is an addition to the spec'd shape.
3. tests/unit/reports/test_contract.py:548-564 — the 1200-id test checks the result of the warehouse lookup but not that statements were issued in chunks of ≤ 500; a spy on the connection (or a wrapper) would pin the "500 ids per statement" limit. No test asserts that StoreBusy from the ops read propagates unchanged (behaviour is correct by construction: no except around the ops calls).
4. tests/unit/reports/test_contract.py:389 — `tmp_path` fixture argument unused in test_ut09_11_schema_version.

### Assessment
**Task quality:** Approved
**Reasoning:** All seven units match the spec and the sub-controller rulings; gates and card tests pass with full coverage, and the remaining items are cosmetic or test-strengthening only.
