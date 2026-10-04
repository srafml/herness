# T07-15 review — Recommendations and confidence feedback (verifier, opus)
Head 172f809, base e41d62c. Saved by the sub-controller (verifier's isolation guard refused the shared path); content from the verifier's reply.

## Spec compliance
- U07-78 write_recommendations: ❌ — steps 1–7 in spec order; validations outside txn (markers, uncited numerals, delta/usd refs, usd unit, verified findings of run, evidence ids), first failure `recommendation rank <r>: <check>`; base=fmean, adjust outside txn; one run_write BEGIN IMMEDIATE; existing ordered by (rank, rec_id), mismatch -> memory.recommendations.conflict ERROR; rows per spec; run_summary via insert_system_item keyed_hash conn=conn, data fields, provenance system/pipeline/run_id; embed_after_commit after commit; written log run_id/n/reused. EXCEPT run_summary content not numeral-free (Important 1).
- U07-79 recommendation_similarity: ✅ (s_kind kind+metric; s_target 1/0.5/0.2/0 precedence; s_text clamped [0,1], NaN->0; weights 0.4/0.35/0.25).
- U07-80 outcome_adjustment: ✅ (step-1 filter, marker strip, dot, ModelUnavailable->0 + WARNING, sim>=threshold, v values, 365-day half-life with future age floored, Δ clamp, confidence clamp, similar sorted sim desc then rec_id, 20, 4 dp; deterministic sums in sorted order; injected now; no prior -> Δ=0, clamp(base)).
- ⚠️ IT07-01/02, FT07-03, BT07-07 other cards; RecommendDeps facade wiring T07-23.

## Focus checks
1 Bounds/direction: Δ clamped to config ∩ [−0.25,0.15], confidence to config ∩ [0.05,0.95]; paid_off/inconclusive only -> Δ>=0, worse/no_effect only -> Δ<=0; PT07-07 strategies wide (delta bounds ±0.9, alpha ≤5, k0 ≥0.01, 30 priors, ages −10..5000 d); clamp mutants M1,M2,M8,M9,M10 killed; reversed-input equality.
2 Ids only / redaction: question redacted then cut to 500, writer redacts again; only recommendation.confidence adjusted; validation before txn, failure inside rolls back (probed incl. PolicyViolation).
3 Idempotency: ids in input order via rank map; mismatch raises + ERROR log + nothing written; embed_after_commit only on fresh insert.
4 No model-facing text on this path (summaries only embedded with markers stripped); no re-implemented escaping.
5 Logs ids/counts only; errors rank+check only; no HTTP client.
7 Tests: IDs/docstrings/pytestmark OK; UT07-63/64/65/66 coverage as required. 32 card tests, 416 memory tests, 100% line/branch, ruff/mypy clean, module size 322/330.

## Issues
Critical: none.
Important 1 — run_summary content neither numeral-free nor size-safe (recommend.py:257, :267 raw `target_type:target_id`; writer at :313 applies numerals.system and size.content -> PolicyViolation, rollback, deterministic on every resume, not the ReportContractError spec 06 handles). Probes: PROJ-1234 -> numerals.system; `svc 42` -> numerals.system; api-2024 -> numerals.system; svc_42 OK; ten 200-char targets -> size.content. Fix: first 10 targets by rank, drop/replace numeral-bearing entries, cap id length and stop before 2000 chars; tests for PROJ-1234 work_item and ten max-length targets; optionally map residual PolicyViolation -> ReportContractError.
Minor 1 — surviving mutants: M11 sim 4-dp rounding, M23 step-1 filter (only below 0.6 threshold), M30 >= vs > at threshold; M22 base clamp and M18 question redaction equivalent. Suggested tests at recommend.py:142, :149, :162.
Minor 2 — recommend.py:283-285 rejects > 50 recs (spec gives ≤ 50 only as perf limit); acceptable, spec 06 must know.
Minor 3 — recommend.py:123 memory.feedback.degraded lacks run_id; carry-over T07-23.

## Rulings on builder concerns
(a) defect, fix here; (b) carry-over T07-23 (structlog contextvars); (c) bounds intersection accepted; (d) spec note — U07-07 errors row (spec line 242) conversion belongs to spec 06 pipeline.recommendation_drafts / facade; (e) accepted.

## Mutation: 30 mutants, 25 killed, 5 survived (M11, M18, M22, M23, M30).
Verdict: Needs fixes

# Scoped re-review round 1 (head c11f92a) — saved by the sub-controller from the verifier's reply
Verdict: Needs fixes (one I1 residual; m1 closed).
Results: 422 memory tests pass; recommend.py 100% line/branch (185 stmts, 28 branches); ruff/format/mypy clean; module size 329/330.
I1 probes re-run: PROJ-1234 / `svc 42` / api-2024 + svc_ok OK ("... and other targets."); numeral-only targets OK ("for other targets.", 91 chars); ten 200-char targets OK (1696 chars, labels cut to 150); omitted ids kept in data.rec_ids (rank order); shown ids redacted (masks numeral-free); builder's "[[id]]" claim confirmed (parse_markers flags [[PROJ-1234]] malformed, find_uncited still finds 1234).
Important 1 (residual) — recommend.py:253 (comment :51): the id is cut to 150 BEFORE redaction; masks inflate (a@b.io 6 -> [EMAIL_822a317aa9] 18; 150 chars of names -> 246), check_limits sees inflated content. Probe: ten work_item targets of short emails (199 chars + suffix) -> PolicyViolation size.content (limit 2000), nothing written. Fix: redact the full label then cut; better, a total budget (append labels in rank order only while len(text)+len(label)+tail fits); test with inflating ids. Recommendation: RecommendDeps.content_max: int = 2000 (facade T07-23 passes cfg.write.max_content_chars) — minimum content ~91 chars so a 100 floor is satisfiable.
m1 closed: M11, M23, M30 KILLED by new UT07-66 tests; M1, M2, M17, M19 still killed; new-code mutants M31 numeral filter, M32 150 cut, M34 tail KILLED; M33 label pre-redaction SURVIVED.
Minor 1 — M33 (recommend.py:253): writer re-redacts so stored text equal, but pre-redaction keeps redacted-away numerals (phone in id) from tripping the filter; optional test: target id with planted email/name, assert masked label and content passes.

# Scoped re-review round 2 (head f7b8cf1) — saved by the sub-controller from the verifier's reply
Verdict: Approved.
Results: 425 memory tests; recommend.py 100% line/branch (189 stmts, 30 branches); ruff/format/mypy clean; module size 330/330.
Budget probes (stored content after writer re-redaction, all rows written, data.rec_ids complete, no uncited numerals, no raw PII): inflating email ids / name ids / 200-char ids -> 1596 @2000, 91 @150/@100; numeral ids -> 110/110/91; mixed -> 1219/110/91. Writer check_limits (step 2) precedes its redaction (step 3); re-redaction length-stable. Minimum content 91 < config floor 100.
Arithmetic exact: _FRAME = 5 + 19 − 2 = 22; boundary probe 112 shown / 111 dropped; `< 0` check at recommend.py:255 accepts exact fit.
Refactor: weights from sorted `kept` — deterministic order. M1–M10, M13, N24 KILLED. N33 (label redaction dropped) KILLED; N31, N32, N35 KILLED.
Minor 1: boundary mutants N36 (_FRAME−1) and N37 (< 0 -> <= 0) survive (recommend.py:51, :253-256) — optional exact-boundary test. Minor 2: equivalent survivors N33b, N38 (break->continue), N39 (sum reversed). Minor 3 cosmetic: tail room always reserved (one label may drop unnecessarily); a mask may be cut mid-token at 150 (no leak, no numeral).
Carry-overs: T07-23 passes memory.write.max_content_chars -> RecommendDeps.content_max and binds run_id for memory.feedback.degraded; spec 06 respects >50 cap; dict-draft conversion in spec 06 / facade.
