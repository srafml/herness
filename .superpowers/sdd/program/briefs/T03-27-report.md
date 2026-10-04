# T03-27 report: Mapping suggestions

Status: DONE_WITH_CONCERNS (minor; see Concerns)
Worktree: D:\herness\.claude\worktrees\agent-aebe6290afe6fac15 (base 667fcf4)
Commits: 0725b25 wip(T03-27): mapping_suggest module and unit tests; 75e4416 feat(enrich): mapping suggestions (T03-27).

## Implemented
herness/enrich/mapping_suggest.py (358/360 lines):
- U03-111 `norm_name(s, *, abbreviations)`: lower, Unicode P* -> space, whitespace collapsed, whole-token expansion.
- U03-112 `mapping_scores(...)` -> `ScoreMatrices` (module-local frozen dataclass): rapidfuzz `process.cdist(..., scorer=fuzz.token_set_ratio, workers=-1)/100`; semantic = clip(S @ V.T, 0, 1); team rows w_f*f + w_s*s + w_c*c, Jira rows w_f*f + (w_s+w_c)*s; all float32, clipped to [0, 1]; empty shapes return zero matrices.
- U03-113 `prepare_mapping_vectors(wh, *, encoder, qs)` -> `MappingVectors` (module-local frozen dataclass: subjects, services, subject_vecs, service_vecs, option_vecs) plus `Subject`/`Service` row dataclasses. Queries per spec; texts per spec; names, "<project> <component>" headers, summaries and dynamic-option descriptions go through `get_redactor().redact_batch` (a failed item becomes "" - fail closed); incident snippets are `left(enrich.text_redacted.text, 200)` of the 20 most recent; encoded with `embed_texts(batch_size=128)`.
- U03-114 `run_suggest_stage(wh, *, vectors, cfg, report)`: skipped (report.status="skipped") when vectors is None; co-occurrence by SQL (team incidents per service_id / all team incidents); `mapping_scores`; per subject top `top_n` with score >= `min_score` ordered score desc then service_id (np.lexsort); payload per design 03 §4.6 with evidence_counts and algorithm_version "map-v1"; `create_if_absent("mapping_suggestion", ..., match_keys=(subject_type, jira_project, jira_component, team_id, service_id), blocking_statuses=("pending","rejected"), now=clock.now())`; metric `herness_enrich_mapping_suggestions_total` (created count) via T08-05 record_counter; log `enrich.suggest.emitted` (counts only); report.rows += created. Never writes core.service_map; never approves.
- `report` typed against module-private Protocol `_Report` (status, rows), as resolve.py does. `cfg` is `DecisionsConfig` (reads `cfg.mapping_suggest`).
- No edits to herness/enrich/__init__.py, embed.py, settings.py, review_items.py; embed_stage.py untouched. No import-linter / listing change needed (lint-imports 13 kept).

## Tests (all IDs carried in names and docstrings; module-level pytestmark)
- tests/unit/enrich/test_mapping_suggest.py (535 lines, unit): UT03-106 (x2), PT03-14 (hypothesis, 300 examples), UT03-107 (x2), UT03-108 (x4: stub redactor records inputs, every encoder text built from redacted names/summaries or text_redacted, 20-most-recent / 200-char cut, SQL-meta characters in names stay data; caps; empty; schema error), UT03-109 (x2: rejected (team, s1) not re-emitted, others pending, payload shape, evidence counts, metric, log, idempotent re-run; co-occurrence share + tie order by service_id + top_n), UT03-110 (x3: below 0.60 nothing emitted; skipped without vectors; SchemaViolation before any write).
- tests/integration/enrich/test_mapping_suggest_flow.py (integration): IT03-14 real core build 000-280 (build_harness) + real ops store + real redactor (redaction_on); suggestions pending, one approved via decide_review_item, rebuild: the only suggested_approved row is the approved one; the stage itself leaves core.service_map unchanged; re-run emits nothing.
- tests/integration/enrich/security/test_mapping_suggest_security.py (integration): ST03-12 high-score (>= 0.8) suggestion only pending, nothing decided, approved list empty, core.service_map identical before/after stage and after rebuild.
- tests/integration/enrich/_mapping_build.py: shared lake/build/encoder stand-in helper (85 lines).

Evidence:
- Card tests: `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_mapping_suggest.py tests/integration/enrich/test_mapping_suggest_flow.py tests/integration/enrich/security/test_mapping_suggest_security.py --cov=herness.enrich.mapping_suggest --cov-branch` -> 16 passed; coverage 100% line, 100% branch (182 stmts, 20 branches).
- tests/unit/enrich: 731 passed, 1 skipped (pre-existing symlink skip).
- Gates: ruff format --check (700 files formatted), ruff check clean, mypy (273 files) clean, lint-imports 13 kept 0 broken, check_module_size exit 0, check_type_ownership exit 0. Checkpoint commit ran all pre-commit hooks (incl. pytest-unit) green, no SKIP, no --no-verify.
- RED evidence: the module was written before its tests in this card (not strict test-first); the first test run failed on a test-config error (DecisionsConfig needs change_link.use_decider=False without the pair question), fixed in the test helper. Recorded honestly as a process deviation.

## Spec notes (spec-silent choices, most conservative reading)
1. Jira subject name (fuzzy and text header) is "<project> <component>"; the header is redacted too (spec only names summaries and team names; redacting every name given to the encoder is the conservative reading of the U03-113 invariant). Service names and dynamic-option descriptions are redacted as well.
2. Jira subjects additionally require `project IS NOT NULL` (a NULL project would be keyed as a team row by 220_service_map.sql).
3. Limits (≤ 2,000 subjects, ≤ 5,000 services, ≤ 1,000 options) are enforced by a deterministic cut with a warning log `enrich.suggest.capped` (Jira subjects first by work-item count desc, then teams by team_id; services by service_id; options by question order then label) rather than an error.
4. "Most recent" = `created_at` desc for work items, `opened_at` desc for incidents (ties by record_id). Team co-occurrence denominator = all incidents of the team (including NULL service_id); `evidence_counts.work_items` is 0 for team subjects, team counts are 0 for Jira subjects.
5. norm_name also cleans each expansion (lower/punct/whitespace), so the idempotence invariant holds whenever no *token* of an expansion is a key (spec's validator only rejects an expansion equal to a key; the literal algorithm is not idempotent for e.g. expansion "Pmt" or "x pmt"). PT03-14 assumes that stronger precondition.
6. Payload scores are rounded to 6 decimals; the min_score filter is applied before rounding.
7. Composed encoder texts are passed through `normalize_text` (NFKC, whitespace, 4,000-char cut) as the input bound.
8. A redaction failure (redact_batch -> None) yields "" for that name/summary (fail closed; the raw text is never used).
9. report.status is set to "skipped" when vectors is None (StageReport U03-142 not built; `_Report` Protocol carries status and rows).

## Concerns
- Module is at 358/360 lines (compact formatting with a few `# fmt: skip` blocks as elsewhere in enrich); little headroom for later changes.
- The work-item summaries query reads up to 20 summaries for every unmapped (project, component), not only the ≤ 2,000 kept subjects; fine at spec sizes, could be narrowed if Jira volumes grow.
- `evidence_counts.work_items` for team subjects is 0 by choice (spec silent).

## Files
- herness/enrich/mapping_suggest.py (new, 358 lines; budget 360)
- tests/unit/enrich/test_mapping_suggest.py (new, 535)
- tests/integration/enrich/_mapping_build.py (new, 85)
- tests/integration/enrich/test_mapping_suggest_flow.py (new, 65)
- tests/integration/enrich/security/test_mapping_suggest_security.py (new, 40)

## Final commit
75e4416 feat(enrich): mapping suggestions (T03-27) - all pre-commit hooks passed (no SKIP, no --no-verify); working tree clean.

## Fix round 1 (review: Approved; test-only)
Commit: 8962a27 test(enrich): mapping suggestion boundary and match-key tests (T03-27). All pre-commit hooks passed (no SKIP, no --no-verify). herness/enrich/mapping_suggest.py unchanged (358/360).
- M1: `test_ut03_110_min_score_boundary_is_inclusive` sets `min_score` to a pair's score taken from the same float32 `mapping_scores` path, so equality holds exactly; the pair is emitted and a lower one is not. The docstring records the float32 behaviour: scores are float32 compared with the float64 `min_score`, so a pair whose real-valued score is exactly 0.60 may round just below and be dropped.
- M2: `test_ut03_109_match_keys_separate_jira_components` rejects (PAY, a, s1); (PAY, b, s1) is still emitted pending, and (PAY, a, s1) is not re-emitted.
- Mutation probes (module reverted with `git checkout --` after each; working tree clean):
  - `row >= min_score` -> `row > min_score`: the boundary test FAILED (mutant killed).
  - `jira_component` removed from `_MATCH_KEYS`: the match-key test FAILED (mutant killed).
- tests/unit/enrich/test_mapping_suggest.py: 16 passed.
