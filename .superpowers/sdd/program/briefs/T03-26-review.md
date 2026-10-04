# T03-26 review — Change linking (impl 03, U03-107..U03-110)

Reviewed: worktree agent-aa74d0a11dbd3bbd2, c3eee74..3b1b356. Focused re-runs by the reviewer: `pytest tests/unit/enrich/test_link_changes.py tests/integration/enrich/test_link_changes_flow.py` 30 passed; link_changes.py coverage 99 % (130 stmts, 0 missed; 22 branches, 1 partial); ruff check and format clean; mypy clean; lint-imports 13 kept, 0 broken.

**Verdict: Approved** (Minor findings only.)

### Spec Compliance
- ✅ **U03-107 `heuristic_link_score`**: `min(1, m·exp(−max(Δh,0)/τ)·b)`. m = ci_weight when same_ci, else service_weight. b = 1.25 for the three boosted outcomes, then ×1.1 for emergency. Neither flag raises `ConfigError`, and the message holds no data (link_changes.py:73-95).
- ✅ **U03-108 `link_candidates.sql`**:
  - Source-field rows come from `caused_by_change_id = core.change.record_id` with method `source_field` and score 1.0 (sql:82-87, 104-109). Unresolved ids are dropped (tested).
  - t_c = coalesce(actual_end, actual_start, planned_end).
  - The window follows design §5.10 per the ruling: opened_at − t_c ∈ [−after_h, +before_h], or actual_start ≤ opened_at ≤ actual_end (sql:54-62).
  - The CI and service matches need non-NULL equal keys, since equi-join NULLs never match.
  - Δh is floored at 0. The score mirrors U03-107 exactly (sql:65-80).
  - Rows need score ≥ min_score and are cut to the top top_n per incident by score DESC, change_id (sql:89-100).
  - A window row is dropped when the same pair has a source-field row. Parameters are bound as named `$` parameters from `cfg.change_link` (link_changes.py:109-116).
  - The day-bucket range covers [min(t−after, start), max(t+before, end)]. That is a superset of both window rules, so no pair is lost. The 1 s edge tests and the running-change test confirm it.
  - "Max score per pair": the two joins are UNIONed to distinct pairs and scored once with U03-107's m rule (see Minor 1).
- ✅ **U03-109 `pair_inputs`**:
  - Pairs are `time_ci_window` rows with score in [band0, band1] (BETWEEN, inclusive).
  - Pairs without a `text_redacted` row are removed by the inner joins before the LIMIT, so skipped pairs do not use up the cap.
  - Order is opened_at DESC, incident_id, change_id, and the result is capped at decider_max_pairs.
  - record_id is `inc|chg`, entity is incident, question_ids is (change_caused_pair,), text is pair_text, and content_hash is taken from that text.
  - The index `cache/pairs/part-<build_id>.parquet` (incident_id, change_id, content_hash) is written through `replace_atomic` and overwritten for the same build (tested, with no tmp left behind).
  - build_id is validated against path traversal. When use_decider is off it returns [] and writes no file.
- ✅ **U03-110 `run_link_stage`**:
  - Answers are read from the cache partition of `pair_decider` (decider, version) for question `change_caused_pair` with the current fingerprint, keeping the latest decided_at per content_hash.
  - p' = `apply_temperature` with T from `CalibrationStore.temperature`, which gives 1.0 when absent. Column 0 is P(true), matching calibrate.py:54.
  - score = 0.5·h + 0.5·p' with method `decider`. It is applied only to band window pairs; unanswered pairs keep `time_ci_window`.
  - Per incident, source-field rows come first, then score DESC, change_id, up to top_n, in one INSERT … QUALIFY.
  - The metric `herness_enrich_links_total{method}` is recorded for each method. The log event `enrich.link.completed` carries the fields source_field, time_ci_window and decider, matching the §8 log table.
  - Nothing is written to `enrich.decision` (asserted in UT03-105 and IT03-09).
  - DuckDB errors become `SchemaViolation` with only the catalog or binder first line or the class name, so no row values appear.
  - The registered view is removed again in `finally`, and a test checks this.
- ✅ Test rows:
  - UT03-102: 14-row table plus the ConfigError case.
  - UT03-103: each rule equals U03-107; top 3 with tie order; min_score; source override; missing table raises SchemaViolation.
  - UT03-104: capped, ordered, index written and overwritten; band exclusion, textless skip and guards.
  - UT03-105: blend with T = 2; top_n with the source row first; latest answer; version mismatch and None keep the heuristic.
  - PT03-13: Hypothesis property test, 300 examples.
  - IT03-09: at stage level, per the ruling.
  - IT03-13: 3 seeds giving precision/recall 0.868/0.898, 0.850/0.953 and 0.837/0.885.
  - Every function name carries its ID, every docstring starts with its ID, and pytestmark is set in both files.
- ✅ Budgets: link_changes.py 311/320, link_candidates.sql 114/120. Layering is L3 → L0/L3 only; contracts kept.
- ✅ Rulings are implemented as stated: the private `_Report` Protocol (link_changes.py:57-65), hand-built core tables, the seeded synthetic IT03-13, the stage-level IT03-09, and the design §5.10 window direction.
- ⚠️ Cannot verify from diff:
  - The full-pipeline half of IT03-09 (decide stage, StubDeciderServer): carry-over.
  - The T3 rewire of IT03-13: carry-over.
  - "Any decider in (primary, chain order)": the resolution of `pair_decider` belongs to the caller (U03-142/pipeline), not this card.

### Builder concerns
- **Pair text truncated to 5,990 chars per side** (link_changes.py:43, 148): acceptable, and needed.
  - compose_text allows about 8,002 chars per side (two 4,000-char fields), so pair_text can reach about 16,024 chars. That exceeds `DecisionInput.text` max_length 12,000 (herness/core/types/decisions.py:136) and would otherwise raise a ValidationError.
  - The spec's "≤ 8,021 chars" for U03-27 is itself inconsistent with U03-25.
  - The truncation is deterministic, so hashes stay stable.
  - The spec row should record it: U03-109 or U03-27 should state the truncation.
- **PT03-13 bounded domain** (tests/unit/enrich/test_link_changes.py:230-231): acceptable.
  - The (0, 1] invariant only fails through float underflow when Δh/τ > ~745.
  - Δh in SQL is bounded by before_h.
  - See Minor 3 for the residual config edge.
- **Insert-only rerun duplicates** (link_changes.py:259-273): acceptable.
  - §9 lists the table as "one insert", and each build writes a new warehouse file (§9, text_redacted row: "the build file is new each run"). F03-01 step 11 has no resume-in-place path.
  - UT03-105 test 2 relies on this: it asserts three identical rows after three calls (test_link_changes.py:535). That is fine as a test device but documents the non-idempotence. A docstring note is suggested (Minor 4).

### Strengths
- The SQL computes exactly what U03-107 computes. Integer-microsecond comparison gives exact window edges, and the 1 s-past-edge tests confirm it.
- The day-bucket expansion also covers the running-change rule. A 150 h running change is tested.
- Error hygiene is good: no row values in any exception, the build_id is guarded, and the view is unregistered in `finally`.
- The tests are strong. They cover exact tie order, calibration with T = 2 against a closed-form expectation, latest-answer selection with FakeClock, and version isolation.

### Issues
#### Critical (Must Fix)
None.

#### Important (Should Fix)
None.

#### Minor (Nice to Have)
1. **herness/enrich/sql/link_candidates.sql:58-71**: U03-108's algorithm says "max score per pair" over the CI and service joins. The code instead dedups pairs (UNION) and applies U03-107's rule (ci_weight whenever the CIs match). The two agree while ci_weight ≥ service_weight, which holds for the defaults 1.0 and 0.6. If configured the other way round, a pair matching both would get the lower CI score. This follows U03-107, the unit UT03-103 pins it to, so it is a spec wording inconsistency. Also, no UT03-103 row has a pair that matches on both CI and service (the `_RULE_CHANGES` at tests/unit/enrich/test_link_changes.py:272 each match one key), so the UNION dedup and the CI-wins branch in SQL are untested.
2. **herness/enrich/sql/link_candidates.sql:43-49**: a change with a very long actual_start..actual_end span unnests one row per day, with no cap. The spec is silent; this is a noted carry-over.
3. **herness/enrich/settings.py:229-231** (ci_weight, service_weight, min_score are `_Fraction`, ge=0) together with sql:65-80:
   - A config of ci_weight = 0 or service_weight = 0 with min_score = 0 would emit score-0 window rows. That violates the U03-107 and U03-110 invariant score ∈ (0, 1].
   - A very large before_h/tau_h ratio with min_score = 0 does the same through exp underflow.
   - This is a config-validation gap (settings card), not this card's code. Log it for the settings owner.
4. **herness/enrich/link_changes.py:276-289**: the `run_link_stage` docstring does not say the stage is insert-only and expects a fresh `enrich.incident_change_link`. A one-line note would make the rerun contract explicit.
5. **herness/enrich/link_changes.py:236-252**: `_decided_rows` recomputes pair_text and content_hash for every band pair with no cap. Pairs beyond decider_max_pairs therefore also pick up answers cached by earlier builds. That is harmless and arguably desirable, but it is not stated.

### Assessment
**Task quality:** Approved
**Reasoning:** All U03-107..U03-110 postconditions and all eight test rows are implemented and verified, with gates green and 99 % coverage. The remaining items are spec wording and config-validation notes.
