# T03-26 report — Change linking (impl 03, stage `link`)

Worktree: D:\herness\.claude\worktrees\agent-aa74d0a11dbd3bbd2 (branch worktree-agent-aa74d0a11dbd3bbd2, base c3eee74).
Status: DONE_WITH_CONCERNS (one spec contradiction resolved toward the design, see Deviations 1). Final commit 3b1b356 (empty marker commit carrying the card subject; code in 173800f, be39987).

## What was built
- `herness/enrich/link_changes.py` (311/320 lines):
  - `heuristic_link_score` (U03-107): pure reference; ConfigError when neither same_ci nor same_service.
  - `link_candidates(wh, *, cfg: DecisionsConfig)` (module-map function running U03-108): loads
    `sql/link_candidates.sql` via `importlib.resources.files("herness.enrich")`, binds the seven
    `$` parameters from `cfg.change_link` (model_dump include), DuckDB errors -> SchemaViolation
    (catalog/binder first line or class name only, never row values).
  - `pair_inputs` (U03-109): band pairs [decider_band] of `time_ci_window` rows joined with both
    `enrich.text_redacted` rows (textless pairs skipped), ordered opened_at DESC, incident_id,
    change_id, capped at decider_max_pairs; record_id `<incident>|<change>`, entity incident,
    question_ids (change_caused_pair,), text = pair_text, content_hash(text). Pair index
    `data/cache/pairs/part-<build_id>.parquet` (incident_id, change_id, content_hash) written with
    `cache.replace_atomic` (overwrites same build; written even when empty). build_id validated
    (slug regex, no trailing dot) -> ConfigError. use_decider off -> [] and no file.
  - `run_link_stage` (U03-110): reads the cache partition of `pair_decider` (decider, version),
    question change_caused_pair with the current fingerprint, latest decided_at per content_hash;
    p' = calibrated P(true) via `calibrate.apply_temperature` (bool, T from
    `CalibrationStore.temperature`, 1.0 when absent); score = 0.5*h + 0.5*p', method decider;
    unanswered band pairs keep time_ci_window; ranking per incident source_field first, then
    score DESC, change_id, <= top_n; single INSERT ... QUALIFY ... RETURNING method. Metric
    `herness_enrich_links_total{method}` via `record_counter(component="enrich")` for the three
    methods (no catalog registration exists; name validated by the metrics module). Log
    `enrich.link.completed` (source_field, time_ci_window, decider counts). `report` typed as a
    module-private Protocol (rows, decided) until U03-142. Nothing written to enrich.decision.
- `herness/enrich/sql/link_candidates.sql` (114/120): one CTAS into TEMP `link_cand`; times as
  integer microseconds (exact window edges); each change unnested over the epoch-day buckets its
  window covers; two bucketed equi-joins (ci_id, service_id) UNIONed; per pair the flags decide m
  (CI weight when same CI) so SQL equals U03-107 exactly; source-field rows (score 1.0) drop the
  window row of the same pair; min_score; QUALIFY row_number() <= top_n. Ships as package data
  (hatch wheel `packages = ["herness"]` includes non-.py files, same as herness/model/sql).

## Tests (IDs)
- UT03-102: `test_ut03_102_heuristic_link_score_table` (14 rows: CI vs service, both flags, 12 h, 72 h edge, negative delta, 3 boosted outcomes, non-boosted, emergency, both boosts, cap), `test_ut03_102_neither_flag_is_config_error`.
- PT03-13: `test_pt03_13_score_bounded_and_non_increasing` (hypothesis, 300 examples, unit).
- UT03-103: `test_ut03_103_window_rules_match_heuristic` (CI/service, emergency+outcome, before edge in / 1 s past out, after edge in / 1 s past out, running change, start-only, planned_end, no time, other keys, NULL keys; each score == U03-107), `test_ut03_103_top3_min_score_and_source_override` (top 3 + change_id tie-break, min_score, source-field override, unresolved caused_by), `test_ut03_103_missing_table_is_schema_violation`.
- UT03-104: `test_ut03_104_pair_inputs_capped_ordered_index_written`, `test_ut03_104_band_excludes_skips_and_guards`, `test_ut03_104_without_link_cand_is_schema_violation`.
- UT03-105: `test_ut03_105_decider_blend_and_top_n` (T=2 calibration file, 0.5h+0.5p', source first, <=3), `test_ut03_105_no_answer_other_version_or_none_keeps_heuristic`, `test_ut03_105_latest_answer_uncalibrated_and_decider_off` (fake_clock), `test_ut03_105_without_link_cand_is_schema_violation`.
- IT03-09: `test_it03_09_pair_decisions_cached_links_decider_not_in_decision` (stage level, see carry-over).
- IT03-13: `test_it03_13_link_precision_and_recall_on_synthetic_truth[3|17|2026]` — measured precision/recall 0.868/0.898, 0.850/0.953, 0.837/0.885 at score >= 0.5.

Results: `pytest tests/unit/enrich tests/integration/enrich/test_link_changes_flow.py` -> 389 passed, 1 skipped (pre-existing symlink skip). Coverage of link_changes.py: 99 % (130 stmts, 0 missed; 1 partial branch).
Gates: ruff format/check clean, mypy (196 files) clean, lint-imports 13 kept, check_module_size exit 0, check_type_ownership exit 0. Pre-commit hooks passed on each commit.
Process note: the module was drafted before the unit test file (no separate RED run was captured); all tests were then written against the spec rows and run green.

## Deviations / spec notes
1. Window direction (CONCERN). U03-108 literally says `opened_at BETWEEN t_c − before_h AND t_c + after_h`; design 03 §5.10 step 2 says `t_c ∈ [opened_at − 72 h, opened_at + 1 h]` (before_h=72, after_h=1), i.e. `opened_at ∈ [t_c − after_h, t_c + before_h]`. The literal impl text would link incidents opened up to 72 h BEFORE a change with Δh floored to 0 (score = m), which contradicts the parameter names and the decay. Implemented the design semantics; the impl spec row should be corrected (swap before_h/after_h in U03-108 Postconditions).
2. Pair text length: DecisionInput.text is max 12,000 chars but two redacted texts can reach ~16,000. Each side is truncated to 5,990 chars before `pair_text` (20 framing chars) — spec silent; deterministic, so hashes are stable.
3. `cfg` of link_candidates / pair_inputs / run_link_stage typed as `DecisionsConfig` (uses `cfg.change_link`); `link_candidates` has no unit spec (module map only) — signature `(wh, *, cfg)`.
4. `pair_decider` names the single (decider, version) whose rows count (per the signature note); the caller resolves "primary, else first chain member with rows". Only that partition is read.
5. Missing `distribution['true']` counts as P(true) = 0 (clamped to 1e-9 by apply_temperature).
6. run_link_stage inserts only (table 3015 "one insert"); a re-run on the same warehouse duplicates rows (build restarts from scratch per §6).
7. PT03-13 domain bounded to delta_h ∈ [−200, 700], tau_h ∈ [1, 100] so exp() does not underflow to 0.0 (the (0, 1] invariant fails in floating point for delta_h/tau_h > ~745).
8. Heuristic `m`: when both same_ci and same_service, ci_weight is used (U03-107), and SQL mirrors that per pair instead of "max score per pair" — identical whenever ci_weight >= service_weight; exact U03-107 equality otherwise.

## Carry-overs
- IT03-13 uses a seeded synthetic set built in the test (spec 11 T3 absent) — rewire to T3 when it lands.
- IT03-09 is stage level (link_candidates -> pair_inputs -> fake decider -> CacheWriter -> run_link_stage); the full-pipeline half (pipeline / decide stage / StubDeciderServer) is a carry-over.
- `report` retype to StageReport (U03-142).
- Very long changes (actual_start..actual_end spanning many days) expand to one bucket row per day; no cap (spec silent).

## Line counts vs budgets
- herness/enrich/link_changes.py 311 / 320
- herness/enrich/sql/link_candidates.sql 114 / 120
- tests/unit/enrich/test_link_changes.py 591; tests/integration/enrich/test_link_changes_flow.py 244

## Fix round 1
- M1: `link_candidates.sql` now scores each match separately (m = ci_weight for the CI join, service_weight for the service join, carried in the UNION) and keeps `max(score)` per pair (`GROUP BY incident_id, change_id`), as U03-108 says ("max score per pair"). This supersedes deviation 8. The SQL is 115/120 lines. U03-107 still uses ci_weight when both flags are set, so the SQL and U03-107 differ only when ci_weight < service_weight; the SQL follows U03-108. New test `test_ut03_103_ci_and_service_match_keeps_max_score`: one pair matches on CI and service with ci_weight 0.4 < service_weight 0.9, and the service score (0.9·e^-0.25) wins.
- M4: the `run_link_stage` docstring now says the stage only inserts and expects the fresh, empty `enrich.incident_change_link` of this build's warehouse (one run per build). `link_changes.py` is 312/320 lines.
- Tests: `tests/unit/enrich` plus `test_link_changes_flow.py` gave 390 passed and 1 skipped (the skip was already there). ruff, mypy and check_module_size are clean.
