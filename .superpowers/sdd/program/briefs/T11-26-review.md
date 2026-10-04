# T11-26 Grading: verify review

Reviewed: worktree agent-abbb912a8bb61713e, head badf70d (base 6a38cbf). Read-only.

Gates run by the reviewer:
- `pytest tests/unit/eval -q -p no:logging --require-test-ids`: 78 passed, no warnings.
- Coverage of herness/eval/grading.py: 214 stmts, 54 branches, 100 % line and 100 % branch.
- mypy: Success (127 files). ruff check: clean. ruff format --check: clean. check_module_size: exit 0. lint-imports: 13 kept, 0 broken.
- grading.py is 374 lines (budget 400). golden.py is untouched.

### Spec Compliance
- ✅ Spec compliant

Per unit:
- U11-56 grade_numeric / GradeResult / NumberCarrier: ✅ The reference is the `value` column, else the only column, and there must be exactly one row, otherwise SuiteError (grading.py:136-152). Candidates must be cited (checked with parse_markers) and have the matching unit (:165-173). rel with r == 0 needs |v| <= 1e-9, and abs is supported (:155-162). The detail holds reference, query_id and candidates. carriers_from covers ChatAnswer, Paragraph and RecommendationItem (headline + summary) (:110-121).
- U11-57 grade_entities / chat_entity_ids / kendall_tau_check: ✅ rank1, topk_contains:K:M, set_equals, and kendall tau-b with a missing item placed at len(ranked). n < 2 raises SuiteError and NaN fails (:179-213). chat_entity_ids matches whole words case-insensitively, longest names first, masks each match, and returns unique ids in order of first occurrence (:216-224).
- U11-58 split_sentences / grade_rules: ✅ The regex matches the spec (:57). must_mention handles a plain string and a MentionRule (entity and phrase in the same sentence). ClaimRule covers the final text and each verified claim. max_number_refs and number_signs are covered. One result per rule item (:227-285).
- U11-59 grade_rubric: ✅ None -> skipped/no_judge. ModelUnavailable -> skipped/judge_unavailable. Passes when mean >= min_score (:288-301).
- U11-60 count_unsupported / UnsupportedReport: ✅ Markers and stray numerals both come from herness.core.numbers (parse_markers, find_uncited; R-16). No local scanner or marker regex. Evidence and rerun are injected. Row selection and comparison reuse herness.harness.verifier row_matches/compare_value (spec 05 §5.6 steps 6-7). total = markers + stray. rate = unsupported/total, or 0 when total is 0 (:346-374).

Per test ID (every function name carries the ID and every docstring starts with it; both files set pytestmark = unit):
- UT11-78 ✅ test_ut11_78_relative_tolerance_passes (8.08 vs 8.0 at rel 0.01 passes and 8.09 fails), _zero_reference_and_value_column, _reference_shape_errors
- UT11-79 ✅ test_ut11_79_exact_and_unit_mismatch
- UT11-80 ✅ test_ut11_80_marker_absent_fails_usd_passes, _carriers_from_pipeline_types
- UT11-81 ✅ test_ut11_81_rank1
- UT11-82 ✅ test_ut11_82_topk_contains (5:3 passes, 5:4 fails)
- UT11-83 ✅ test_ut11_83_set_equals
- UT11-84 ✅ test_ut11_84_kendall_tau (a swap gives 0.8 and passes; a missing item gives 0.6 and fails), _kendall_tau_degenerate
- UT11-85 ✅ test_ut11_85_chat_entity_ids_longest_first
- UT11-86 ✅ test_ut11_86_split_sentences_and_must_mention
- UT11-87 ✅ test_ut11_87_mention_rule_same_sentence
- UT11-88 ✅ test_ut11_88_claim_rule_checks_verified_claims (the final text is clean and claim[1] fails)
- UT11-89 ✅ test_ut11_89_max_number_refs
- UT11-90 ✅ test_ut11_90_number_signs
- UT11-91 ✅ test_ut11_91_rubric_skipped_reasons (the two reasons differ)
- UT11-92 ✅ test_ut11_92_rubric_mean
- UT11-93 ✅ test_ut11_93_allowed_numerals_and_valid_ref. The patterns come from ReportsSection().compiled_numeral_patterns, the config model for `reports.allowed_numeral_patterns`. Its defaults are identical to config/app.yaml:15-16.
- UT11-94 ✅ test_ut11_94_stray_and_orphan (stray 42, orphan n2, rate 2/2)
- UT11-95 ✅ test_ut11_95_stale_refs (missing query id, changed value), _stale_rerun_failures_and_rows
- PT11-06 ✅ test_pt11_06_identity_and_reversal (hypothesis, 2-30 unique ids)
- PT11-07 ✅ test_pt11_07_allowed_text_has_no_unsupported

Builder concerns, assessed:
1. Tau `>= X - 1e-9` (grading.py:55,191): accepted. For one swap in 5 the exact tau is 0.8, but scipy returns 0.7999... For identical order it returns 0.9999... Without the epsilon, UT11-84 and PT11-06 from the spec itself would fail. Distinct tau-b values differ by about 1/n^2, which is at least 1e-3 for n <= 30, so the slack cannot flip a real verdict. This is a faithful implementation of the spec, not a deviation.
2. DuckDB type inferred from the Python cell (grading.py:304-308): accepted. compare_value branches only on DECIMAL*, the integer-type set, and everything else (verifier.py:48-64). DuckDB fetches DECIMAL as Decimal, every integer type (HUGEINT and UBIGINT included) as int, and FLOAT/DOUBLE as float. So the mapping Decimal -> DECIMAL, int -> BIGINT, else DOUBLE reaches every branch exactly. compare_value picks the unit-based branches itself from `unit`: usd -> Decimal half-even, count/rank -> exact. §5.6 step 7 is preserved: exact for integers, count and rank; Decimal half-even for USD; float by claimed decimals or float_rel_tol. Step 6 is preserved as well (:333-340): with row_key None there must be exactly one row, and 0 rows, more than 1 row, or a missing column make the ref unsupported.
3. Stale NumberRef without a marker (grading.py:361-366): this follows the spec literally and is not a bug relative to the brief. Step 3 says "each NumberRef", and step 5 counts only markers and stray numerals in total. Under unsupported_number_rate_max = 0 it fails closed. Two side effects: rate can exceed 1, and when total = 0 the rate is 0 even though unsupported > 0. That is no TH11-12 hole, because those numbers never appear in prose. See Minor 1. The controller could rule that only cited refs count, but that is a spec change.
4. grade_numeric compares every unit as Decimal (grading.py:124-162): accepted. Floats are converted through repr, so Decimal(repr(8.08)) == Decimal("8.08"), the value the user sees. The results differ from float arithmetic only at float-noise boundaries. UT11-78 sits exactly on the 1 % boundary and needs this. It is a superset of "USD compared as Decimal".

- ⚠️ Cannot verify from diff:
  - TH11-12 independence. count_unsupported runs its own evidence lookup and rerun. It never reads a Verifier verdict or VerifierReport. It shares only the pure step 6-7 functions (compare_value, row_matches) and the R-16 scanner. That is the required single implementation, not a reliance on the evidence checks of the Verifier. The remaining risk is that a defect in those shared pure functions would hit both the Verifier and eval. The spec accepts this by mandating the §5.6 step 7 comparison and R-16.
  - The real EvidenceLookup (ops evidence, then meta.evidence, per DD11-09) and RerunFn (read-only rerun) are composed by the later runner card. Here they are injected fakes.

### Strengths
- R-16 is respected: there is no local marker regex or numeral scanner. find_uncited blanks the markers itself, and the allowed patterns are injected.
- The verifier comparison is reused, not reimplemented. Each query id gets one cached rerun, and evidence is checked before any rerun (asserted by `env.reruns == [_Q2]`).
- SuiteError details and GradeResult details carry only counts, ids, values and suite text. The ClaimRule `where` names the source (claim[i]), not the sentence. No ticket text or answer prose leaks.
- The tests are behavioural and include negatives: 8.09 fails, 5:4 fails, split sentence vs same sentence, integer mismatch, ambiguous row, missing row, rerun failure.
- Layering: L5 imports only L4 and L0 modules, and import-linter is clean. The only PLR0913 noqa is on the signature the spec fixes.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. herness/eval/grading.py:361-374. Stale refs that no marker cites add to `unsupported` but not to `total`. So `rate` can exceed 1.0, and with total == 0 the report shows unsupported > 0 but rate == 0.0. This follows the spec literally (U11-60 steps 3 and 5). Record it as a spec note, or get a controller ruling on counting only cited refs. Either way, document the rate > 1 behaviour in the docstring.
2. herness/eval/grading.py:357-359. Duplicate NumberRef ids keep only the first ref (`setdefault`). A second ref with the same id but a different value is never checked. The Verifier presumably rejects duplicates upstream, but counting the duplicate as stale would keep this path fail-closed.
3. herness/eval/grading.py:323-324. `except Exception` turns any rerun failure into an unsupported ref. That fails closed, and the noqa gives a reason. But a programming error in the runner's RerunFn would then show up only as stale refs, with no reason attached. Consider a per-ref reason code later (no_evidence / rerun_failed / row / mismatch).
4. tests/unit/eval/test_grading_rules.py:22. UT11-93 takes the allowed patterns from the ReportsSection() defaults instead of parsing config/app.yaml. The two are identical today (settings.py:29-35 vs app.yaml:15-16). A test that loads the shipped app.yaml would catch drift.
5. tests/unit/eval/test_grading_rules.py:185-205. PT11-07 draws only integer count values. Adding float and USD-string refs would widen the property to more of compare_value.

### Assessment
**Task quality:** Approved
**Reasoning:** All five units follow the spec algorithms. They use the single R-16 numbers implementation and reuse the §5.6 steps 6-7 comparison with its semantics intact. Every test ID is present with meaningful assertions, coverage is 100 % line and branch, and all gates are clean. The four builder concerns are either sound implementations of the spec or follow the spec literally; none is a defect.
