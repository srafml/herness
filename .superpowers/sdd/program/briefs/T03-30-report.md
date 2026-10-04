# T03-30 Evaluation — build report

Worktree: D:\herness\.claude\worktrees\agent-a55bd54dd4a377644, branch worktree-agent-a55bd54dd4a377644, base bdb61b7.

## What was built
- `herness/enrich/evaluate.py` (374/380 lines, L3; numpy, sklearn f1_score with local `# type: ignore[import-untyped]`):
  - U03-127 `question_metrics(probs, labels, folds, *, qtype, threshold) -> QuestionMetrics` (frozen dataclass with the 10 spec fields). cross_fit -> P' = apply_temperature(probs, T, qtype); accuracy on argmax P'; macro_f1 = f1_score(average="macro", zero_division=0.0) for choice; mae / within_one for score; ece = cal.ece; coverage = share max P' >= threshold; accuracy_at_threshold on covered rows (None when none). n = 0 -> zeros, all optional metrics None, uncalibrated.
  - U03-128 `macro_metric(metrics, qs)`: mean over scoring_use questions present of accuracy (choice/bool) or within_one (score, None -> 0.0); no question -> 0.0.
  - U03-129 `evaluate_candidate(*, version, qs, cfg, store, cache, calibration, teacher, paths, now, blocked=())`: frozen gold at the current fingerprint; latest cache row per (question, content_hash) at the current fingerprint for Laya (`laya`, version) and the teacher; per-question metrics for both; system accuracy (Laya when calibrated p >= threshold else teacher argmax, denominator n_gold); criteria = acceptance_for(cfg, qid).model_dump(exclude_none=True); passed per spec rule (missing metric -> false); accepted_proposed = all passed and not uncalibrated; macro_metric; eval.json written atomically (cache.replace_atomic, canonical JSON); Laya calibration.json via CalibrationStore.save("laya", version, ...); teacher calibration file updated (only questions where the teacher had gold rows); log `enrich.distill.candidate_evaluated` (version, accepted_proposed list, macro_metric); gauge `herness_enrich_gold_metric_ratio` (component enrich, labels question/metric/decider) per impl 03 §8.2 via T08-05 record_gauge.
- Tests: `tests/unit/enrich/test_evaluate.py` (UT03-123 x5, UT03-124, UT03-125 x7), `tests/unit/enrich/_evaluate_fixtures.py` (hand-built cache + frozen gold), `tests/eval/enrich/test_enrich_eval_crosscheck.py` (ET03-02, marker `eval`).

## Evidence
- RED: tests were written before evaluate.py existed (import error on collection); the first run after the module landed failed 8/13 on a fixture config error (change_link.use_decider needs change_caused_pair; fixed with `change_link: {use_decider: false}` in the fixture YAML), then green.
- GREEN: `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_evaluate.py tests/eval/enrich --require-test-ids -q -p no:logging` -> 15 passed.
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich -q -p no:logging` -> 506 passed, 1 skipped (pre-existing symlink skip).
- Coverage evaluate.py: 100% line, 100% branch (187 stmts, 40 branches).
- ruff format/check clean; mypy (project files) 0 issues; lint-imports 13 kept; check_type_ownership 0; check_module_size 0.
- Commits use `SKIP=pytest-unit` per the known-red rule (test_st05_13 / IT00-01 pre-existing); other hooks pass.

## Line counts
- herness/enrich/evaluate.py 374 / 380 budget.
- tests: test_evaluate.py ~290, _evaluate_fixtures.py ~196, test_enrich_eval_crosscheck.py 80.

## Deviations / interpretations
1. `blocked: Collection[str] = ()` keyword added to evaluate_candidate. The U03-129 invariant says questions blocked from training are absent from `questions`, but the signature has no input carrying the blocked set (it is computed by run_distill step 6). Optional, defaults to none blocked; run_distill (T03-32/33) should pass its blocked list. `# noqa: PLR0913` with reason.
2. `gold_sha256` = gold_digest over the frozen gold rows (current fingerprint) of every question in qs, including blocked ones (algorithm step 1 precedes per-question filtering). `laya accept` (U03-138) must recompute with the same selection.
3. `gold_path` is written relative to the data root's parent (e.g. `data/labels/<qsv>/gold/`), matching the design example.
4. `criteria` holds only the criteria that are set (exclude_none), so score questions carry max_mae / min_within_one etc. — the design example shows the choice set.
5. Teacher with no gold cache rows: `teacher.accuracy` and `teacher.ece` are null, max_gap_to_teacher fails, and no teacher calibration entry is written.
6. Label columns: bool = (true, false) (column 0 = p(true), as apply_temperature calibrates column 0); score = ("0".."3") so index == score; choice = static option order, then any other answer seen in gold or cache (sorted) — covers dynamic options.
7. Cache rows: latest `decided_at` per (question, content_hash) at the current fingerprint; distributions renormalized, an all-zero row becomes uniform. A gold row without a decider row is excluded from that decider's metrics; in system accuracy it falls through to the teacher, and counts as wrong if neither exists.
8. The ET03-02 test recomputes via cross_fit / question_metrics / gold_digest from raw parts in the test (the spec 11 runner `herness.eval.runner.run_classifier` does not exist yet).

## Carry-overs
- Spec 11 `tiny_build` has no enrichment cache/gold; UT03-125 and ET03-02 use a hand-built fixture (`tests/unit/enrich/_evaluate_fixtures.py`). Swap to the spec 11 fixture when it carries gold + cache.
- run_distill (U03-136) must pass `blocked=` and call evaluate_candidate after candidate inference.

## Concerns
- Deviation 1 (extra optional keyword) needs a controller ruling or a spec note on U03-129's signature.
- A gold answer outside the fixed bool/score label set would raise ValueError; upstream sync validates labels (U03-76), so not guarded here.

## Final
- Commits: 026e4bf wip(T03-30): evaluate module and unit tests; 2aba049 wip(T03-30): ET03-02 cross-check and no-cache test; b357bf0 feat(enrich): T03-30 evaluation (all with SKIP=pytest-unit, other hooks green).
- Status: DONE_WITH_CONCERNS (optional `blocked` keyword on evaluate_candidate needs a ruling).

## Fix round 1 (review T03-30-review.md)
- Important 1 (controller ruling): `gold_sha256 = gold_digest(store.read("gold"))`, taken over the whole `data/labels/<qsv>/gold/` table, which includes rows of unfrozen questions and older fingerprints. It matches spec 11 F11-09 step 2 / U11-67 and U03-138. Metrics still use only the rows of frozen questions at the current fingerprint. UT03-125 (keys test, no-frozen-gold test) and ET03-02 now recompute the digest from the full gold table. New test `test_ut03_125_gold_digest_covers_whole_gold_directory` checks that the fixture gold holds unfrozen (business_impact) and old-fingerprint rows, that the digest equals the full-table digest and differs from the frozen-only digest, and that the evaluated questions and n_gold are unchanged.
- Minor 2: a comment on `bool(passed)` explains the empty-criteria guard (all([]) is True).
- Minor 1, 3 and 4 are parked per the controller.
- Spec notes to record: (a) the U03-129 algorithm step 1 `gold_sha256` is taken over the whole gold directory, not only frozen-question rows; the interpretation 2 above is superseded. (b) The `blocked: Collection[str] = ()` keyword extends the U03-129 signature.
- Gates: 16 card tests pass (--require-test-ids); tests/unit/enrich 507 passed, 1 skipped; evaluate.py coverage 100% line and branch; ruff, format, mypy and check_module_size are clean. evaluate.py is 376/380 lines.
- Commits: cc180fb wip(T03-30): digest whole gold directory (fix round 1). The final commit follows (SKIP=pytest-unit only for the known-red hook).
