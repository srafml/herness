# T07-15 report — Recommendations and confidence feedback

Status: DONE_WITH_CONCERNS (minor spec readings, see below)
Worktree: D:\herness\.claude\worktrees\agent-aa32a9b972c0b2e15 (branch worktree-agent-aa32a9b972c0b2e15, base e41d62c)
Commits: 65716f7 `wip(T07-15): recommend module and tests green`; 172f809 `feat(memory): T07-15 recommendations and confidence feedback` (pre-commit hooks all passed)

## Built
- `herness/harness/memory/recommend.py` (322 lines; budget 330, hard limit 400): `SimInput`, `RecommendDeps`,
  `recommendation_similarity` (U07-79), `outcome_adjustment` (U07-80), `write_recommendations` (U07-78).
  - write: empty -> []; > 50 recs -> ReportContractError("recommendation count above 50"); duplicate rank -> "duplicate rank";
    unknown run -> MemoryNotFound("run", id); validation (reads outside tx, ops lookups chunked at 500 ids) with first failure
    `recommendation rank <r>: <check>` (checks: markers, numerals, expected_delta_ref, expected_usd_ref, expected_usd_unit,
    findings [verified AND same run], evidence); base = fmean(finding confidences); deps.adjust outside tx; one run_write
    (BEGIN IMMEDIATE): existing rows ordered by (confidence_basis.rank, rec_id) compared on (kind, target_type, target_id) ->
    same ids (reused) or `memory.recommendations.conflict` ERROR + ReportContractError("recommendations for run changed on resume");
    else insert_recommendations + run_summary via writer.insert_system_item(key_hash=keyed_hash("run_summary:"+run_id), conn=conn)
    with data run_kind, question (deps.redactor, then cut to 500, or null), top_finding_ids (first 10 distinct by rank), rec_ids,
    dead_task_count; provenance system/pipeline/run_id. After commit embed_after_commit; log `memory.recommendations.written`
    (run_id, n, reused). expected_usd = Decimal quantized 0.01 string; expected_delta = float.
  - adjust: verdict filter, step-1 kind/target prefilter, markers stripped (core ANY_MARKER_RE) + whitespace collapsed, s_text = dot;
    ModelUnavailable -> all 0 + `memory.feedback.degraded` WARNING; sim >= cfg.sim_threshold; v map; decay half-life 365 d
    (future measured_at -> age 0); summation in sorted order (sim desc, rec_id) for determinism; Δ = 0 with no qualifying prior;
    similar first 20, sim rounded 4, outcome_query_id "" when NULL.
- Tests: `tests/unit/harness/memory/test_memory_recommend.py` (UT07-63..66), `tests/unit/harness/memory/test_memory_recommend_props.py` (PT07-07).

## Evidence
- RED: `pytest tests/unit/harness/memory/test_memory_recommend*.py` -> ModuleNotFoundError herness.harness.memory.recommend (2 collection errors).
- GREEN: card tests 32 passed; `pytest tests/unit/harness/memory -q` 416 passed.
- Coverage recommend.py: 100 % line, 100 % branch (183 stmts, 32 branches).
- ruff format/check clean, mypy clean (touched files), lint-imports 13 kept, check_type_ownership ok, check_module_size exit 0;
  checkpoint commit ran full pre-commit (incl. pytest-unit) green.

## Spec readings / ambiguities
1. Bounds: Δ and confidence are clamped to the intersection of FeedbackConfig bounds and the ConfidenceAdjustment field bounds
   ([-0.25, 0.15], [0.05, 0.95]) so a wider config can never fail pydantic validation. base stored clamped to [0, 1].
2. `memory.feedback.degraded`: §8 lists field `run_id`, but outcome_adjustment has no run_id in its U07-80 signature; logs `priors=<n>` only.
   The MemoryStore facade (T07-23) could bind run_id via structlog contextvars.
3. `related(a, b)` is called as related(draft.target_id, prior.target_id), only when the target ids differ.
4. Priors with an unknown verdict are skipped (DB CHECK makes this unreachable).
5. RecommendationDraft arrives typed; the U07-07 "pydantic ValidationError -> ReportContractError" conversion is not done here
   (signature takes RecommendationDraft objects). If spec 06 passes dicts, the facade should convert.
6. Validation repeats on resume (spec order: validate, adjust, then tx), so a resumed call also re-embeds.

## Concerns / carry-overs
- run_summary content is built from up to 10 `target_type:target_id`; with very long target ids (200 chars each) it could exceed
  memory.write.max_content_chars (2000) and insert_system_item would raise PolicyViolation (rolled back, nothing written) rather than
  ReportContractError. Likewise a target_id containing a standalone numeral would trip the system-text numeral rule. Not handled; spec
  gives no truncation rule.
- MemoryStore facade wiring (T07-23): build RecommendDeps (conn_factory, writer, adjust = outcome_adjustment bound to
  outcomes_for_similarity(), Embedder.embed, RelatednessCache, cfg.feedback, now), redactor, allowed patterns.
- RecommendationDraft.target_type has no "candidate" (T06-11 gap) — unchanged here.
- No IT07-01/02, FT07-03, BT07-07 added (other cards).

## Fix round 1 (review D:\herness\.superpowers\sdd\program\briefs\T07-15-review.md)
- I1 (run_summary content): new `_content` builds the text from the first 10 targets by rank. Each label is
  `target_type:target_id[:150]`, and it is redacted through deps.redactor first. A label is left out of the text when
  `find_uncited_numerals(label, deps.allowed)` finds a numeral. When some labels are left out, the text ends with
  " and other targets." (or "for other targets." when none is shown). The ids stay in data.rec_ids. Size bound by
  construction: 10 x (<= 10 + 1 + 150) + separators + head (~80) + tail (18) is about 1,750, under 2,000.
  - Spec note: the "[[id]]" marker form was rejected. `parse_markers` treats `[[PROJ-1234]]` as a malformed marker,
    and `find_uncited` blanks only valid `[[nK]]` markers. So "1234" would still be an uncited numeral and trip
    numerals.system in writer.insert_system_item (and check_markers would report it invalid).
  - Omitting the numeral-bearing ids is the fallback the controller allowed.
  - Concern: the 2,000 bound assumes the default memory.write.max_content_chars. The setting can be configured as low
    as 100, and recommend.py has no access to the writer's cfg.
  - Residual PolicyViolation is not mapped to ReportContractError; the normal path now succeeds.
- m1 mutants: new tests cover each one.
  - sim rounded to 4 dp (0.75 + 0.25/3 -> 0.8333).
  - A prior exactly at sim_threshold = 0.75 is kept.
  - With sim_threshold 0.1, a prior whose kind and target both differ (sim 0.425) is dropped by the step-1 filter;
    a kind-matching prior is kept.
- Tests: PROJ-1234 work_item (rows written, text omits it); only-numeral targets ("svc 42", "api-2024"); eleven
  200-char targets (all 11 rows written, 10 labels cut to 150 chars, content <= 2,000).
- Compaction to stay in budget: same behaviour; the s_target conditional and half-life constant were folded, and
  question/content are now built inside `_summary_item`.
- recommend.py 329/330 lines. Card tests 38 passed, tests/unit/harness/memory 422 passed, coverage 100 % line/branch.
  ruff, mypy, lint-imports and module-size are clean.
- Not in this round (spec notes / carry-overs): the >50 cap (spec 06 must know), run_id on memory.feedback.degraded
  (T07-23 contextvars), dict-draft conversion (spec 06 / facade).
- Fix-round commit: c11f92a `fix(memory): T07-15 review round 1`. All pre-commit hooks passed.
  - The first attempt failed the mypy hook: list[str] was not accepted where a JsonValue dict entry was expected.
  - Fixed with `[*top]` and committed again; nothing was skipped.

## Fix round 2 (I1 residual: redaction growth)
- `_content` now redacts the full label `target_type:target_id` first, then cuts it to 150 chars.
- One total length budget replaces the per-label arithmetic. Labels are added in rank order while
  len(head) + 22 + sum(len(label) + 2) <= `deps.content_max`. The 22 chars are `_FRAME`: " for " plus the joins plus
  " and other targets.". This always reserves room for the tail form, so the final text fits whether or not the tail is
  used. Numeral-bearing labels are still skipped. The first label that does not fit stops the loop, and the text then ends
  " and other targets." (or "for other targets." when none is shown).
- New `RecommendDeps.content_max: int = 2000`. Carry-over to T07-23: the MemoryStore facade must pass
  cfg.write.max_content_chars. The minimum text ("Run <run_id> (<kind>) recorded recommendations for other targets.") is
  about 91 chars for org_review and about 95 for funding_review, so it fits the configured floor of 100.
- Tests added:
  - ten work_item ids of 27 "a@b.io" each: all 10 rows written, content <= content_max, no raw email in it;
  - content_max=100: write succeeds and the text is "... for other targets.";
  - M33: a planted email or name next to a plain id is shown masked ("service:owner …", "team:lead …") and the write passes.
  The max-length test now expects labels cut after redaction (142 'a's after "service:").
- Lines kept to the budget with no behaviour change: shorter module docstring, two signatures on two lines, and the
  weighted sums built from one list (same left-to-right order as the old loop).
- Results: recommend.py 330/330. Card tests 41 passed, tests/unit/harness/memory 425 passed, coverage 100 % line/branch.
  ruff, mypy, lint-imports and module-size are clean.
- Concern: this assumes the writer's redaction leaves already-redacted masks unchanged. The verifier's probe and the
  inflated-id test both show the content stays within the limit after the writer redacts again.
- Fix-round-2 commit: f7b8cf1 `fix(memory): T07-15 review round 2`. All pre-commit hooks passed.
