# T03-22 report: Ensemble stage (band selection and pooling)

Worktree: D:\herness\.claude\worktrees\agent-a4bc02d27edebf940 (branch worktree-agent-a4bc02d27edebf940, base 3f61b67)
Checkpoint: e909c41 wip(T03-22): ensemble band and pooling with unit tests (UT03-83, UT03-84)
Final: 35f0c53 feat(enrich): T03-22 ensemble stage (band selection and pooling) (first attempt failed on an unrelated Hypothesis deadline flake, PT04-12 in tests/unit/metrics, under host load; retried unchanged, hooks green)

## Built
- herness/enrich/ensemble_stage.py (260/260 lines, budget 260):
  - `ensemble_band(wh, *, cfg, qs) -> list[QueueItem]` (U03-88): enrich_resolved JOIN enrich_laya_cal on
    (record_id, question), scoring_use and non-pair questions of `qs`, p_cal < ensemble.band; grouped per
    record (question_ids sorted), ORDER BY opened_at DESC NULLS LAST, record_id, entity; LIMIT ensemble.max_rows;
    text from enrich.text_redacted. DuckDB error -> SchemaViolation("ensemble_band: <catalog msg | class>").
  - `run_ensemble_pool(wh, *, band, qs, cfg, cache, calibration, members, gold_accuracy, build_id, report) -> str`
    (U03-89): present members = members with at least one cached row for a band (hash, question); weights =
    gold_accuracy restricted to present deciders (warning `enrich.ensemble.weights_default` per band question
    lacking a present member's accuracy -> EnsembleDecider pools equal weights); `EnsembleDecider(cache,
    calibration, members=present, weights, qsv)` (T03-16, unchanged) is the ONLY combiner; `decide` per chunk of
    2,000 items; rows via CacheWriter under (ensemble, version), flush every 2,000 rows and in `finally`.
    Disagreements: latest current-version ensemble row per band (content_hash, question), backend_confidence
    < 2/3, ordered by DuckDB sha256(build_id|content_hash|question) (U03-81 rule), LIMIT
    disagreement_review_cap; created via review_items.create_if_absent("label_check", ...) with
    match_keys ("purpose","question","content_hash"), blocking ("pending","approved","rejected"),
    scope {"question_set_version": qsv}, payload per design §4.6 (purpose "ensemble_disagreement",
    decider "ensemble", text_ref "enrich.text_redacted", no text). Returns decider.version.
  - Restartable: pairs that already have a (ensemble, version) row are not pooled again
    (cache.existing_keys); reviews are selected from cached ensemble rows, so a rerun completes what an
    interrupted run missed; create_if_absent suppresses already-created items.
  - No GPU lock, no register_deciders, never calls a member decider or LLM client; `report` typed by a private
    `_Report` Protocol (status, note, decided) until StageReport (T03-28).
- No pyproject / import-linter change needed (package-level contracts; 13 kept).

## Tests (all green)
- tests/unit/enrich/test_ensemble_stage.py (unit): UT03-83 x4 (band around 0.9 / scoring only / order and NULL
  opened_at; max_rows cap and band config; no scoring questions; missing tables -> SchemaViolation);
  UT03-84 x10 (agreement 1/3: rows cached, items capped at 3 in hashlib-recomputed order, payload keys,
  calibration temperatures read per member/question, no text in logs/payloads; rerun idempotent; interrupted
  run completed by rerun with chunk size patched to 2; missing member drops out (version/weights over present);
  missing accuracy warns; rejected item blocks re-review + scope; no members / empty band skipped "no_work";
  cache OSError mapped via io_error; DuckDB error -> SchemaViolation and views unregistered; calibration
  temperature changes the pooled answer).
- tests/integration/enrich/test_ensemble_stage_flow.py (integration, real migrated tmp ops store):
  IT03-07 Laya rows -> resolve_frame -> ensemble_band -> OpenJev + LLM member rows -> run_ensemble_pool ->
  run_resolve with versions["ensemble"]: ensemble decisions for the band with that version, agreement set only
  for ensemble rows (1/3 and 1.0), laya elsewhere, disagreement items 1 <= cap 1 (2 candidates), no text.
- RED: `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_ensemble_stage.py` without the module ->
  "ImportError: cannot import name 'ensemble_stage' from 'herness.enrich'" (collection error).
- GREEN: card tests 15 passed; `pytest tests/unit/enrich -q -p no:logging` 884 passed, 1 skipped.
- Coverage (card tests): ensemble_stage.py 100% line, 100% branch (121 stmts, 18 branches).
- Gates: ruff format/check clean, mypy clean on touched files, lint-imports 13 kept, check_module_size exit 0,
  check_type_ownership exit 0. Checkpoint commit ran the real pre-commit hooks.

## Deviations / spec notes
1. `enrich_laya_cal` exists (resolve_decisions.sql, T03-19): no deviation. It has no `entity`, so the band
   joins on (record_id, question); a record_id shared by two entities would cross-match (ids are
   entity-prefixed in practice).
2. Review creation uses match_keys ("purpose","question","content_hash") + scope qsv, the same T03-20 spec note
   as U03-83 (U03-148 appends scope keys without dedup; impl 02 rejects duplicates).
3. "Present members" is decided by the stage (>=1 cached row for a band pair of that decider+version); weights
   are filtered to present deciders so the version (sha256 of members+weights) reflects the present set.
4. Per-night cap: applied to the deterministic hash-ordered list per call; with the same build_id a rerun
   yields the same first-cap set so created <= cap. Two runs with different build_ids in one night could
   exceed it (the spec gives no open-count rule for disagreements, unlike U03-149 for spot checks).
5. Log events not in §8.1: `enrich.ensemble.weights_default` (WARNING, question) for F03-08's warning
   `ensemble_weights_default`; `enrich.ensemble.pooled` (INFO: band, members, pooled, reviews). Review creation
   logs `enrich.spot_check.created` with purpose "ensemble_disagreement" (the table's `purpose` field).
6. `report.decided` += pooled (record, question) rows; no members/empty band -> status "skipped", note "no_work".
7. U03-89 has no JobContext, so no heartbeat/yield checkpoint; restart safety comes from the cache.
8. IT03-07 simulates the members by writing their rows through CacheWriter (the stage never calls members, so
   FakeLLMClient/LlmDecider were not needed). File named *_flow.py per the directory's convention.
9. Temp files: the dispatch named <worktree>/.agent-tmp, but controller addendum 6 forbids temp inside a
   worktree; the folder held only a commit-message file and was removed; later temp in the session scratchpad.

## Concerns
- Deviation 4 (cap across differing build_ids) for the controller to rule on if needed.

## Fix round 1 (review finding I-1) — commit b247246
- Added `test_ut03_84_agreement_two_thirds_is_not_a_disagreement` (tests/unit/enrich/test_ensemble_stage.py):
  laya {a .6}, openjev {a .5}, llm {b .6}; pooled argmax "a", exactly 2 of 3 members agree. Asserts the cached
  q_choice rows have answer "a" and backend_confidence == 2/3 (approx), and no `ensemble_disagreement` item.
  `_Env.member_rows` gained an optional `split` argument (default unchanged). No production change; the
  production file is identical to 35f0c53 (`git diff --quiet 35f0c53 -- herness/enrich/ensemble_stage.py`).
- Mutant kill evidence (each applied in place, test run, then `git checkout --` revert):
  - `backend_confidence < $agreement_min` -> `<=`: new test FAILED
    (`assert _items() == []` -> AssertionError: one item created).
  - `AGREEMENT_MIN: Final = 2 / 3` -> `1.0`: new test FAILED (same assertion).
- Card tests: 16 passed (unit 15 + IT03-07). ruff format/check clean; mypy clean on the test file.
- Minor findings M-1..M-7 left parked as instructed.
