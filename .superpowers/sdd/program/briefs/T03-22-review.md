# T03-22 review: Ensemble stage (verify agent)

Worktree: D:\herness\.claude\worktrees\agent-a4bc02d27edebf940 (head 35f0c53, base 3f61b67). Read-only; nothing modified.

Evidence run by the reviewer:
- `PYTHONUTF8=1 uv run pytest tests/unit/enrich/test_ensemble_stage.py tests/integration/enrich/test_ensemble_stage_flow.py -q -p no:logging --cov=herness.enrich.ensemble_stage --cov-branch --cov-report=term-missing`: 15 passed; ensemble_stage.py 121 stmts / 18 branches, 100 % line, 100 % branch.
- `uv run mypy herness/enrich/ensemble_stage.py`: Success, no issues.
- `uv run ruff check` and `ruff format --check` on the 3 files: clean.
- Mutation probes (plugin under %TEMP%\T03-22-verify patching module constants/SQL at pytest_configure; 14 mutants):
  - killed: band `<` -> `<=` (2 fail); `LIMIT $cap` -> `$cap + 1` (4 fail); hash order -> `ORDER BY content_hash` (8 fail); drop the version filter in the disagreement query (8 fail); drop the qs scoring-question filter (3 fail); ignore the restart `done` set (2 fail); treat all members as present (3 fail); AGREEMENT_MIN = 0.5 (1 fail).
  - SURVIVED: agreement `<` -> `<=`; AGREEMENT_MIN = 1.0; drop `r.scoring_use` (equivalent: also filtered by the qs list); `decided_at DESC` -> `ASC` (near-equivalent: the writer never writes a key twice); POOL_CHUNK = 1e9 (chunking unobservable); PAIR_QUESTIONS emptied (test pair question is non-scoring, so equivalent).

## Spec compliance

| Item | Status | Notes |
|------|--------|-------|
| U03-88 `ensemble_band` signature | ✅ | `wh`; kw-only `cfg`, `qs` -> `list[QueueItem]` |
| U03-88 postconditions | ✅ | scoring_use, non-pair questions; `p_cal < ensemble.band` (strict, boundary 0.90 tested); per-record question list (sorted); `LIMIT ensemble.max_rows`; `opened_at DESC` (NULLS LAST), `record_id` (+ `entity` tiebreak) |
| U03-88 algorithm (enrich_resolved JOIN enrich_laya_cal) | ✅ | `enrich_laya_cal (record_id, question, answer, p_cal)` confirmed in herness/enrich/sql/resolve_decisions.sql:46-49 (T03-19); no local temp table needed |
| U03-88 errors (SchemaViolation) | ✅ | ensemble_stage.py:117-120, catalog/binder first line or class name only |
| U03-89 signature | ✅ | exact keyword-only set, returns version str |
| U03-89 step 1: EnsembleDecider is the only combiner, calibration from CalibrationStore | ✅ | ensemble_stage.py:247; deciders/ensemble.py untouched (diff stat: 3 new files only); no member decider or LLM client imported or called; calibration spy test asserts temperature reads per member/question |
| U03-89 step 2: decide per chunk of 2,000, rows under (ensemble, version) | ✅ | ensemble_stage.py:173-180, writer flush_rows=2,000, flush in `finally` |
| U03-89 step 3: agreement < 2/3, U03-81 hash order, cap, create_if_absent with U03-83 keys/statuses/scope | ✅ impl / ❌ test strength (I-1) | SQL ensemble_stage.py:70-85; hash expression identical to _spot_checks.py:46; keys ("purpose","question","content_hash") + scope qsv per the T03-20 spec note; blocking (pending, approved, rejected); items created `pending` by U03-148 via herness.store.ops |
| U03-89 step 4: return version | ✅ | ensemble_stage.py:260 |
| U03-89 invariant: missing members drop out, weights renormalize | ✅ | `_present` + weights filtered to present deciders; tested |
| F03-08 step 4: missing accuracy -> equal weights + warning | ✅ | `enrich.ensemble.weights_default` (deviation 5) |
| Payload per design §4.6, no text | ✅ | exact 11 keys asserted; `text_ref = enrich.text_redacted` |
| Bounded: max_rows, 2,000 chunks, cap | ✅ | cap mutant killed; max_rows tested |
| Restartable / idempotent | ✅ | `existing_keys` skip; reviews selected from cached rows; rerun and interrupted-run tests |
| No ticket text in logs/errors/payloads (TH03-03) | ✅ | asserted in UT03-84 and IT03-07 (`secret body` absent); non-catalog DuckDB errors reduced to class name |
| UT03-83 | ✅ | 4 functions; rows < 0.9 for scoring questions only (0.89 / 0.90 / 0.899 and a low non-scoring row) |
| UT03-84 | ✅ (see I-1) | agreement 1/3: rows cached, items capped at 3 in hashlib-recomputed order |
| IT03-07 | ✅ | tests/integration/enrich/test_ensemble_stage_flow.py, `pytestmark = integration`, real migrated tmp ops store (`ops_store` fixture runs `migrate()`); asserts ensemble rows for the band with the returned version, `agreement` non-NULL only for ensemble (1/3 and 1.0), other rows laya with NULL agreement, disagreement items 1 <= cap 1 with 2 candidates |
| Test IDs / docstrings / pytestmark | ✅ | `test_ut03_83_*`, `test_ut03_84_*`, `test_it03_07_*`; docstrings start with the ID |
| Module budget 260 | ✅ | 260/260 (at limit) |
| Layering L3 | ✅ | imports herness.core.* and herness.enrich.* only |
| mypy strict, ruff, coverage >= 90/85 | ✅ | 100/100 |

### ⚠️ Items
- ⚠️ IT03-07 "Action: pipeline": `run_enrichment` (T03-28) does not exist yet, so the test chains resolve_frame -> ensemble_band -> member rows via CacheWriter -> run_ensemble_pool -> run_resolve. Acceptable for this card; T03-28 should exercise the stage from the real stage loop.
- ⚠️ `enrich_laya_cal` has no `entity` (SQL owned by T03-19), so the band joins on (record_id, question) (ensemble_stage.py:52). Sound in practice: impl 02 enforces `_record_id = source:entity:source_key` (02-data-model.impl.md:542), so record ids are unique across entities. Latent risk only if that contract changes; a later T03-19 touch could add `entity` or `content_hash` to the temp table.

## Findings

### Critical
None.

### Important
- I-1 tests/unit/enrich/test_ensemble_stage.py:254-284 (and IT03-07 fixtures at tests/integration/enrich/test_ensemble_stage_flow.py:52-55): the 2/3 boundary of the disagreement rule (herness/enrich/ensemble_stage.py:82 `backend_confidence < $agreement_min`; AGREEMENT_MIN at :38) is not pinned. Fixtures only produce agreement 1/3 and 1.0, so the mutants `<` -> `<=` and `AGREEMENT_MIN = 1.0` both survive all 15 tests. With three members 2/3 is the most common pooled agreement, so such a regression would queue a review for every 2-of-3 row (up to 500 per night) undetected. Fix: add a case where exactly 2 of 3 members match the pooled argmax, assert its cached `backend_confidence == 2/3` and that no `ensemble_disagreement` item is created for it. No production-code change needed.

### Minor
- M-1 ensemble_stage.py:83-84 per-night cap is per call (deviation 4): two builds with different build_ids in one night can create up to 2 x cap. Ruling: acceptable (one nightly build; a same-build_id rerun is idempotent and tested). Record a spec note on U03-89; if a hard nightly bound is wanted, count the ensemble_disagreement items created since the start of the night (analogue of U03-149) in a later card.
- M-2 ensemble_stage.py:98-101 `_duck_error` duplicates resolve.py:123-126 verbatim (private, budget-driven). Candidate for a shared private helper later.
- M-3 ensemble_stage.py:83 duplicates the U03-81 hash expression of _spot_checks.py:46 instead of sharing it; expressions are identical and the unit test recomputes the order with hashlib, so behaviour is pinned. Acceptable.
- M-4 ensemble_stage.py:131-135 `_present` does not filter on current question fingerprints, so a member with only stale-fingerprint band rows counts as present and enters the version/weights while contributing nothing. Deterministic and harmless to correctness.
- M-5 ensemble_stage.py:179-180 `writer.flush()` in `finally` can mask the original `decide` exception if the flush itself fails. Low risk.
- M-6 Surviving equivalent / near-equivalent mutants, no action required: `r.scoring_use` filter redundant with the qs list (:53); `decided_at DESC` tiebreak (:79); POOL_CHUNK chunking unobservable except in the patched interruption test; PAIR_QUESTIONS exclusion (:113) untested because the test pair question is non-scoring.
- M-7 ensemble_stage.py (whole file): heavy `# fmt: skip` compaction to reach 260/260; readability cost, within rules.

## Rulings on builder deviations
1. enrich_laya_cal exists (T03-19): confirmed, accepted; join soundness per the ⚠️ item above.
2. match_keys 3 + scope qsv: accepted (same T03-20 spec note as U03-83; the store still matches on all four keys).
3. Present members decided by the stage, weights filtered to present deciders: accepted (implements the U03-89 invariant; see M-4).
4. Per-night cap across differing build_ids: accepted as Minor (M-1), spec note recommended.
5. Log events `enrich.ensemble.weights_default` (the F03-08 warning `ensemble_weights_default`, component-prefixed) and `enrich.ensemble.pooled`; reuse of `enrich.spot_check.created` with `purpose`: accepted; §8.1 should gain the two rows via spec note.
6. `report.decided += pooled`; skipped / no_work: accepted.
7. No JobContext / heartbeat (signature has none): accepted; restart safety via the cache is tested.
8. IT03-07 simulates members through CacheWriter; file named *_flow.py: accepted (the stage never calls members by design; location matches §11).
9. Temp files moved out of the worktree: accepted.

## Verdict
**Needs fixes**: one Important (add the agreement == 2/3 boundary test; test-only). Everything else meets the spec; the implementation is correct by inspection, and every non-equivalent mutant except the two 2/3 mutants is killed.
